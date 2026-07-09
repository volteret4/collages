import requests
import sqlite3
import time
import json
import subprocess
import sopsdotenv
import os
import argparse

try:
    sopsdotenv.load_sops_env()
except FileNotFoundError:
    # En Docker las variables llegan ya descifradas vía docker-compose (scripts/up.sh);
    # no hay .encrypted.env ni binario `sops` instalado en la imagen.
    pass

DB = "charts_orpheus.db"
URL = "https://orpheus.network/ajax.php"

import re

API_KEY       = os.environ.get("ORPHEUS_API_KEY")
DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "")
_DISC_HEADERS = {"User-Agent": "ChartViewer/1.0",
                 "Authorization": f"Discogs token={DISCOGS_TOKEN}"}


# -------------------------
# DB
# -------------------------

def db():
    return sqlite3.connect(DB)


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS artists (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS albums (
        id INTEGER PRIMARY KEY,
        artist_id INTEGER,
        name TEXT,
        youtube_url TEXT,
        yt_id TEXT,
        cover_url TEXT,
        orpheus_group_id INTEGER
    )
    """)
    # Migrate existing DBs that lack the column
    try:
        cur.execute("ALTER TABLE albums ADD COLUMN orpheus_group_id INTEGER")
        con.commit()
    except sqlite3.OperationalError:
        pass

    cur.execute("""
    CREATE TABLE IF NOT EXISTS collections (
        id INTEGER PRIMARY KEY,
        slug TEXT,
        name TEXT,
        source_url TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS collection_albums (
        id INTEGER PRIMARY KEY,
        collection_id INTEGER,
        album_id INTEGER,
        rank INTEGER
    )
    """)

    con.commit()
    con.close()


# -------------------------
# API helper
# -------------------------

def api_call(params, _retries=5):
    headers = {"Authorization": f"token {API_KEY}"}

    for attempt in range(_retries):
        r = requests.get(URL, params=params, headers=headers)

        if r.status_code == 429 or (r.status_code == 200 and
                "application/json" in r.headers.get("content-type", "") and
                r.json().get("error") == "Rate limit exceeded"):
            wait = 10 * (2 ** attempt)
            print(f"  Rate limit — esperando {wait}s antes de reintentar…")
            time.sleep(wait)
            continue

        if r.status_code != 200:
            raise Exception(f"HTTP error: {r.status_code}")

        if "application/json" not in r.headers.get("content-type", ""):
            raise Exception(f"Not JSON response:\n{r.text[:500]}")

        try:
            data = r.json()
        except Exception:
            raise Exception(f"Invalid JSON:\n{r.text[:500]}")

        if data.get("status") != "success":
            if data.get("error") == "Rate limit exceeded":
                wait = 20 * (2 ** attempt)
                print(f"  Rate limit — esperando {wait}s antes de reintentar…")
                time.sleep(wait)
                continue
            raise Exception(f"API error: {data.get('error')}")

        return data["response"]

    raise Exception("Rate limit persistente tras varios reintentos")

# -------------------------
# Gazelle COLLAGE
# -------------------------


def extract_artist(group_wrapper):
    # Accedemos al sub-diccionario 'group'
    group = group_wrapper.get("group", {})

    # Buscamos la info musical dentro de ese sub-diccionario
    music_info = group.get("musicInfo", {})
    artists = music_info.get("artists", [])

    if not artists:
        return "Unknown Artist"

    return ", ".join(a["name"] for a in artists)

def get_collage(COLLAGE_ID):
    all_albums = []
    current_page = 1
    total_pages  = None
    collage_name = "Unnamed Collage"

    while True:
        print(f"Descargando página {current_page}" +
              (f"/{total_pages}" if total_pages else "") + "…")

        data = api_call({
            "action": "collage",
            "id": COLLAGE_ID,
            "page": current_page
        })

        if current_page == 1:
            collage_name = data.get("name", "Unnamed Collage")
            total_pages  = data.get("pages") or data.get("totalPages") or data.get("numPages")
            if total_pages:
                total_pages = int(total_pages)
                print(f"Total páginas: {total_pages}  —  collage: {collage_name!r}")

        torrent_groups = data.get("torrentgroups", [])

        if not torrent_groups:
            break

        for item in torrent_groups:
            group_data = item.get("group", {})
            album_name = group_data.get("name") or group_data.get("groupName") or "Unknown Album"
            all_albums.append({
                "rank":     len(all_albums) + 1,
                "artist":   extract_artist(item),
                "album":    album_name,
                "cover":    group_data.get("wikiImage"),
                "group_id": group_data.get("id") or group_data.get("groupId"),
            })

        if total_pages:
            if current_page >= total_pages:
                break
        else:
            # Fallback: if page size is unknown, stop when we get fewer results
            # than the first page (works for any page size)
            if current_page == 1:
                _first_page_size = len(torrent_groups)
            if current_page > 1 and len(torrent_groups) < _first_page_size:
                break
            if not torrent_groups:
                break

        current_page += 1
        time.sleep(4)

    return collage_name, all_albums

# -------------------------
# Discogs video lookup
# -------------------------

def discogs_youtube_url(artist, album):
    """Return (yt_url, yt_id) from Discogs videos, or (None, None) if not found."""
    if not DISCOGS_TOKEN:
        return None, None
    try:
        def _search(rtype):
            r = requests.get("https://api.discogs.com/database/search",
                             params={"artist": artist, "release_title": album,
                                     "type": rtype, "per_page": 3},
                             headers=_DISC_HEADERS, timeout=8)
            return r.json().get("results", []) if r.ok else []

        results = _search("master") or _search("release")
        if not results:
            return None, None

        res      = results[0]
        res_type = res.get("type", "release")
        rid      = res.get("id")
        if not rid:
            return None, None

        detail = requests.get(f"https://api.discogs.com/{res_type}s/{rid}",
                              headers=_DISC_HEADERS, timeout=8).json()
        for v in detail.get("videos", []):
            url = v.get("uri", "") or v.get("url", "")
            if "youtube.com" in url or "youtu.be" in url:
                m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
                if m:
                    yt_id = m.group(1)
                    return f"https://www.youtube.com/watch?v={yt_id}", yt_id
    except Exception as e:
        print(f"Discogs lookup error: {e}")
    return None, None


# -------------------------
# YouTube
# -------------------------



def search_youtube(artist, album):
    query = f"{artist} - {album} full album"

    # Construimos el comando como lo harías en la terminal
    # --dump-single-json nos devuelve la info en formato JSON para leerla fácil
    command = [
        "yt-dlp",
        f"ytsearch5:{query}",
        "--dump-single-json",
        "--no-playlist",
        "--quiet"
    ]

    try:
        # Ejecutamos el comando y capturamos la salida
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)

        if "entries" in data:
            for v in data["entries"]:
                # Verificamos si el video dura más de 20 minutos (1200 seg)
                if v.get("duration") and v["duration"] > 1200:
                    return v["webpage_url"], v["id"]

    except Exception as e:
        print(f"Error al ejecutar yt-dlp del sistema: {e}")

    return None, None


# -------------------------
# DB helpers
# -------------------------

def get_or_create_artist(name):
    con = db()
    cur = con.cursor()

    cur.execute("SELECT id FROM artists WHERE name=?", (name,))
    row = cur.fetchone()

    if row:
        con.close()
        return row[0]

    cur.execute("INSERT INTO artists (name) VALUES (?)", (name,))
    con.commit()
    artist_id = cur.lastrowid
    con.close()
    return artist_id


def get_or_create_album(artist_id, name, group_id=None):
    con = db()
    cur = con.cursor()

    cur.execute("""
        SELECT id, youtube_url FROM albums
        WHERE artist_id=? AND name=?
    """, (artist_id, name))

    row = cur.fetchone()

    if row:
        if group_id:
            cur.execute("UPDATE albums SET orpheus_group_id=? WHERE id=?", (group_id, row[0]))
            con.commit()
        con.close()
        return row

    cur.execute("""
        INSERT INTO albums (artist_id, name, orpheus_group_id)
        VALUES (?, ?, ?)
    """, (artist_id, name, group_id))

    con.commit()
    album_id = cur.lastrowid
    con.close()
    return album_id, None


def update_album(album_id, yt_url, yt_id, cover):
    con = db()
    cur = con.cursor()

    cur.execute("""
        UPDATE albums
        SET youtube_url=?, yt_id=?, cover_url=?
        WHERE id=?
    """, (yt_url, yt_id, cover, album_id))

    con.commit()
    con.close()


# -------------------------
# MAIN
# -------------------------

def main():
    init_db()
    parser = argparse.ArgumentParser(description="mustdiscover — comparación entre usuarios")
    parser.add_argument("--id",           type=int)

    args = parser.parse_args()

    COLLAGE_ID = (args.id)

    collage_name, albums = get_collage(COLLAGE_ID)

    # Usamos un context manager (with) para asegurar que se cierre y guarde
    with db() as con:
        cur = con.cursor()

        # 1. Limpiar colección anterior
        cur.execute("DELETE FROM collection_albums WHERE collection_id=1")

        # 2. Actualizar/Insertar colección
        cur.execute("""
            INSERT OR REPLACE INTO collections (id, slug, name, source_url)
            VALUES (1, ?, ?, ?)
        """, (
            f"collage-{COLLAGE_ID}",
            collage_name,
            f"https://orpheus.network/collages.php?id={COLLAGE_ID}"
        ))
        con.commit()

    # Ahora que la conexión de arriba está cerrada, el bucle puede trabajar
    for a in albums:
        artist_id = get_or_create_artist(a["artist"])
        album_id, yt = get_or_create_album(artist_id, a["album"], group_id=a.get("group_id"))

        if not yt:
            yt_url, yt_id = discogs_youtube_url(a["artist"], a["album"])
            if yt_url:
                print(f"  ↳ Discogs video: {yt_id}")
            else:
                yt_url, yt_id = search_youtube(a["artist"], a["album"])
                time.sleep(1)
            update_album(album_id, yt_url, yt_id, a["cover"])

        # Volvemos a abrir para insertar el ranking del álbum
        with db() as con:
            cur = con.cursor()
            cur.execute("""
                INSERT INTO collection_albums (collection_id, album_id, rank)
                VALUES (1, ?, ?)
            """, (album_id, a["rank"]))
            con.commit()

        print(f"{a['rank']}. {a['artist']} - {a['album']}")

    export_json(collage_name)
    generate_html(collage_name)


# -------------------------
# EXPORT
# -------------------------

def export_json(collage_name):
    con = db()
    cur = con.cursor()

    rows = cur.execute("""
        SELECT a.name, al.name, al.youtube_url, al.yt_id, al.cover_url, ca.rank,
               al.orpheus_group_id
        FROM collection_albums ca
        JOIN albums al ON ca.album_id = al.id
        JOIN artists a  ON al.artist_id = a.id
        WHERE ca.collection_id = 1
        ORDER BY ca.rank
    """).fetchall()

    data = [
        {
            "rank":        r[5],
            "artist":      r[0],
            "album":       r[1],
            "youtube":     r[2],
            "embed":       f"https://www.youtube.com/embed/{r[3]}?rel=0" if r[3] else None,
            "cover":       r[4],
            "orpheus_url": f"https://orpheus.network/torrents.php?id={r[6]}" if r[6] else None,
        }
        for r in rows
    ]

    con.close()

    with open(f"{collage_name}.json", "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# -------------------------
# HTML
# -------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__NAME__</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#e0e0e0;font-family:"Segoe UI",system-ui,sans-serif;
     display:flex;height:100vh;overflow:hidden}

/* ── Grid side ── */
#grid-panel{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}

#toolbar{padding:11px 14px;background:#111;border-bottom:1px solid #1e1e1e;
         display:flex;align-items:center;gap:10px;flex-shrink:0}
#toolbar h1{font-size:.9rem;font-weight:700;color:#fff;letter-spacing:.04em;
            white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}
#search{flex:1;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:6px;
        color:#e0e0e0;padding:6px 11px;font-size:.82rem;outline:none}
#search:focus{border-color:#444}
#search::placeholder{color:#444}
#count{font-size:.72rem;color:#444;white-space:nowrap}

#grid-wrap{flex:1;overflow-y:auto;padding:10px}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:7px}

/* ── Album card ── */
.card{position:relative;cursor:pointer;border-radius:5px;overflow:hidden;
      aspect-ratio:1;background:#1a1a1a;transition:transform .15s,box-shadow .15s}
.card:hover{transform:scale(1.05);box-shadow:0 6px 24px rgba(0,0,0,.7);z-index:2}
.card.active{outline:2px solid #fff;transform:scale(1.05)}
.card img{width:100%;height:100%;object-fit:cover;display:block}
.card .no-cover{width:100%;height:100%;display:flex;align-items:center;
               justify-content:center;font-size:1.8rem;color:#333}

.rank{position:absolute;top:4px;left:4px;background:rgba(0,0,0,.78);
      color:#fff;font-size:.6rem;font-weight:700;padding:2px 5px;
      border-radius:3px;line-height:1.4}

.hover-info{position:absolute;bottom:0;left:0;right:0;
            background:linear-gradient(transparent,rgba(0,0,0,.9));
            padding:20px 6px 6px;opacity:0;transition:opacity .15s}
.card:hover .hover-info{opacity:1}
.hi-artist{font-size:.58rem;color:#aaa;white-space:nowrap;
           overflow:hidden;text-overflow:ellipsis}
.hi-album{font-size:.68rem;font-weight:600;color:#fff;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ── Detail side ── */
#detail{width:320px;min-width:280px;background:#111;
        border-left:1px solid #1e1e1e;display:flex;
        flex-direction:column;overflow:hidden;flex-shrink:0}
#d-placeholder{flex:1;display:flex;align-items:center;justify-content:center;
               color:#2a2a2a;font-size:.9rem}

#d-cover-wrap{width:100%;aspect-ratio:1;overflow:hidden;
              background:#1a1a1a;flex-shrink:0}
#d-cover{width:100%;height:100%;object-fit:cover;display:block}
#d-cover-none{width:100%;height:100%;display:flex;align-items:center;
              justify-content:center;font-size:4rem;color:#222}

#d-info{padding:12px 14px 10px;border-bottom:1px solid #1e1e1e;flex-shrink:0}
#d-rank{font-size:.65rem;font-weight:700;color:#555;
        letter-spacing:.07em;text-transform:uppercase;margin-bottom:3px}
#d-artist{font-size:.8rem;color:#888;margin-bottom:3px;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#d-album{font-size:1rem;font-weight:700;color:#fff;line-height:1.3}

#d-actions{padding:8px 14px 10px;border-bottom:1px solid #1e1e1e;flex-shrink:0}
#d-yt-btn{display:inline-flex;align-items:center;gap:6px;
          background:#c00;color:#fff;border:none;border-radius:5px;
          padding:6px 14px;font-size:.78rem;font-weight:600;
          cursor:pointer;text-decoration:none}
#d-yt-btn:hover{background:#e00}
.no-yt{color:#333;font-size:.78rem}

#d-player-wrap{flex:1;display:flex;flex-direction:column;background:#000}
#d-player{width:100%;aspect-ratio:16/9;border:none;flex-shrink:0}
#d-no-video{padding:20px;color:#2a2a2a;font-size:.8rem;text-align:center}

::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#222;border-radius:2px}
</style>
</head>
<body>

<div id="grid-panel">
  <div id="toolbar">
    <h1>__NAME__</h1>
    <input id="search" type="text" placeholder="Buscar artista o álbum…" autocomplete="off">
    <span id="count"></span>
  </div>
  <div id="grid-wrap"><div id="grid"></div></div>
</div>

<div id="detail">
  <div id="d-placeholder">← Selecciona un álbum</div>
</div>

<script>
let ALL = [], current = -1;

fetch("__NAME__.json")
  .then(r => r.json())
  .then(data => { ALL = data; render(data); });

function esc(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function render(data) {
  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  document.getElementById("count").textContent = data.length + " álbumes";
  data.forEach(a => {
    const card = document.createElement("div");
    card.className = "card";
    card.dataset.rank = a.rank;
    const img = a.cover
      ? `<img src="${esc(a.cover)}" loading="lazy" alt="" onerror="this.style.display='none'">`
      : `<div class="no-cover">🎵</div>`;
    card.innerHTML = img
      + `<div class="rank">#${a.rank}</div>`
      + `<div class="hover-info">`
      + `<div class="hi-artist">${esc(a.artist)}</div>`
      + `<div class="hi-album">${esc(a.album)}</div>`
      + `</div>`;
    card.addEventListener("click", () => select(a, card));
    grid.appendChild(card);
  });
}

function select(a, cardEl) {
  current = a.rank;
  document.querySelectorAll(".card").forEach(c => c.classList.remove("active"));
  if (cardEl) { cardEl.classList.add("active"); cardEl.scrollIntoView({block:"nearest"}); }

  const cover = a.cover
    ? `<img id="d-cover" src="${esc(a.cover)}" alt="">`
    : `<div id="d-cover-none">🎵</div>`;

  const ytBtn = a.youtube
    ? `<a id="d-yt-btn" href="${esc(a.youtube)}" target="_blank">▶ Abrir en YouTube</a>`
    : `<span class="no-yt">Sin enlace de YouTube</span>`;

  const player = a.embed
    ? `<iframe id="d-player" src="${esc(a.embed)}" allowfullscreen allow="encrypted-media"></iframe>`
    : `<div id="d-no-video">Sin vídeo disponible</div>`;

  document.getElementById("detail").innerHTML =
    `<div id="d-cover-wrap">${cover}</div>`
    + `<div id="d-info">`
    +   `<div id="d-rank">Puesto #${a.rank}</div>`
    +   `<div id="d-artist">${esc(a.artist)}</div>`
    +   `<div id="d-album">${esc(a.album)}</div>`
    + `</div>`
    + `<div id="d-actions">${ytBtn}</div>`
    + `<div id="d-player-wrap">${player}</div>`;
}

// Search
document.getElementById("search").addEventListener("input", e => {
  const q = e.target.value.trim().toLowerCase();
  render(q ? ALL.filter(a =>
    a.artist.toLowerCase().includes(q) || a.album.toLowerCase().includes(q)) : ALL);
});

// Keyboard navigation (← → or ↑ ↓)
document.addEventListener("keydown", e => {
  if (document.activeElement === document.getElementById("search")) return;
  if (!["ArrowRight","ArrowLeft","ArrowDown","ArrowUp"].includes(e.key)) return;
  e.preventDefault();
  const cards = [...document.querySelectorAll(".card")];
  if (!cards.length) return;
  const idx = cards.findIndex(c => +c.dataset.rank === current);
  const next = (e.key === "ArrowRight" || e.key === "ArrowDown")
    ? Math.min(idx + 1, cards.length - 1)
    : Math.max(idx - 1, 0);
  const card = cards[next];
  const rank = +card.dataset.rank;
  select(ALL.find(a => a.rank === rank), card);
});
</script>
</body>
</html>"""


def generate_html(collage_name):
    html = _HTML_TEMPLATE.replace("__NAME__", collage_name)
    with open(f"{collage_name}.html", "w") as f:
        f.write(html)


if __name__ == "__main__":
    main()
