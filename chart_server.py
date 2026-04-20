#!/usr/bin/env python3
"""
chart_server.py — Orpheus chart viewer
Run: python chart_server.py
Open: http://localhost:8745
"""
import os, json, glob, subprocess, sys, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote, urlencode
import requests
import sopsdotenv

sopsdotenv.load_sops_env()

LASTFM_API_KEY  = os.environ.get('LASTFM_API_KEY', '')
DISCOGS_TOKEN   = os.environ.get('DISCOGS_TOKEN', '')

PORT         = 8745
DATA_DIR     = os.path.dirname(os.path.abspath(__file__))
SCRIPT       = os.path.join(DATA_DIR, 'yt-chart.py')
REGISTRY     = os.path.join(DATA_DIR, 'chart_registry.json')
_SKIP_NAMES  = {'charts_orpheus', 'chart_registry'}

_job      = {'running': False, 'id': None, 'last_name': None, 'error': None, 'done': False, 'log': ''}
_job_lock = threading.Lock()


# ── Registry ──────────────────────────────────────────────────────────────────
def load_registry():
    if os.path.exists(REGISTRY):
        try:
            with open(REGISTRY, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_registry(entries):
    with open(REGISTRY, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

def register_collage(collage_id, name, count):
    import datetime
    reg = load_registry()
    # Update existing entry or append new one
    for e in reg:
        if e['name'] == name:
            e.update(id=collage_id, count=count, updated=str(datetime.date.today()))
            save_registry(reg)
            return
    reg.insert(0, {
        'id': collage_id, 'name': name, 'count': count,
        'fetched': str(datetime.date.today()),
    })
    save_registry(reg)


# ── Collage helpers ───────────────────────────────────────────────────────────
def list_collages():
    """Merge registry entries with any JSON files not yet registered."""
    reg  = {e['name']: e for e in load_registry()}
    seen = set(reg)

    # Discover JSON files not in registry
    for f in sorted(glob.glob(os.path.join(DATA_DIR, '*.json'))):
        name = os.path.splitext(os.path.basename(f))[0]
        if name in _SKIP_NAMES or name in seen:
            continue
        try:
            with open(f, encoding='utf-8') as fh:
                d = json.load(fh)
            if isinstance(d, list) and d and 'artist' in d[0]:
                reg[name] = {'name': name, 'count': len(d)}
        except Exception:
            pass

    # Return sorted newest-first (registry order) then alphabetical for unregistered
    result = list(load_registry())  # preserves insertion order
    reg_names = {e['name'] for e in result}
    for name, info in sorted(reg.items()):
        if name not in reg_names:
            result.append(info)
    return result


def read_collage(name):
    fp = os.path.join(DATA_DIR, name + '.json')
    if not os.path.exists(fp):
        return None
    with open(fp, encoding='utf-8') as f:
        return json.load(f)


def _run_fetch(collage_id):
    with _job_lock:
        _job.update(running=True, id=collage_id, last_name=None,
                    error=None, done=False, log='')

    # Snapshot JSON files before fetch to detect the new one
    before = {os.path.basename(f) for f in glob.glob(os.path.join(DATA_DIR, '*.json'))}

    try:
        proc = subprocess.run(
            [sys.executable, SCRIPT, '--id', str(collage_id)],
            capture_output=True, text=True, cwd=DATA_DIR
        )
        log = (proc.stdout + proc.stderr).strip()

        # Find newly created JSON file
        after    = {os.path.basename(f) for f in glob.glob(os.path.join(DATA_DIR, '*.json'))}
        new_file = next((f for f in after - before if f.endswith('.json')), None)
        new_name = os.path.splitext(new_file)[0] if new_file else None

        if new_name:
            data = read_collage(new_name)
            if data:
                register_collage(collage_id, new_name, len(data))

        if proc.returncode == 0:
            with _job_lock:
                _job.update(running=False, done=True, error=None,
                            log=log, last_name=new_name)
        else:
            with _job_lock:
                _job.update(running=False, done=False, last_name=new_name,
                            error=log or 'Script returned non-zero exit code')
    except Exception as e:
        with _job_lock:
            _job.update(running=False, done=False, error=str(e))


# ── Enrichment ───────────────────────────────────────────────────────────────

_LASTFM  = 'https://ws.audioscrobbler.com/2.0/'
_DISCOGS = 'https://api.discogs.com'
_DISC_HEADERS = {'User-Agent': 'ChartViewer/1.0', 'Authorization': f'Discogs token={DISCOGS_TOKEN}'}


def _lfm(params):
    params.update(api_key=LASTFM_API_KEY, format='json')
    try:
        r = requests.get(_LASTFM, params=params, timeout=8)
        return r.json() if r.ok else {}
    except Exception:
        return {}


def _disc_get(path, params=None):
    try:
        r = requests.get(_DISCOGS + path, params=params or {}, headers=_DISC_HEADERS, timeout=8)
        return r.json() if r.ok else {}
    except Exception:
        return {}


def get_lastfm_info(artist, album):
    out = {}

    # Artist info
    ai = _lfm({'method': 'artist.getinfo', 'artist': artist, 'autocorrect': 1})
    a  = ai.get('artist', {})
    if a:
        bio = a.get('bio', {}).get('summary', '')
        # Strip Last.fm "read more" link appended at end
        if '<a href=' in bio:
            bio = bio[:bio.rfind('<a href=')].strip().rstrip('.')
        out['artist_bio']       = bio or None
        out['artist_listeners'] = int(a.get('stats', {}).get('listeners') or 0)
        out['artist_playcount'] = int(a.get('stats', {}).get('playcount') or 0)
        out['similar']          = [s['name'] for s in a.get('similar', {}).get('artist', [])[:5]]

    # Album info
    alb = _lfm({'method': 'album.getinfo', 'artist': artist, 'album': album, 'autocorrect': 1})
    al  = alb.get('album', {})
    if al:
        wiki = al.get('wiki', {}).get('summary', '')
        if '<a href=' in wiki:
            wiki = wiki[:wiki.rfind('<a href=')].strip().rstrip('.')
        out['album_wiki']     = wiki or None
        out['album_listeners'] = int(al.get('listeners') or 0)
        out['album_playcount'] = int(al.get('playcount') or 0)
        out['tags']           = [t['name'] for t in al.get('tags', {}).get('tag', [])[:8]]
        out['tracklist']      = [t['name'] for t in al.get('tracks', {}).get('track', [])]

    return out


def get_discogs_info(artist, album):
    out = {}
    if not DISCOGS_TOKEN:
        return out

    # Search for master or release
    sr = _disc_get('/database/search', {
        'artist': artist, 'release_title': album,
        'type': 'master', 'per_page': 3,
    })
    results = sr.get('results', [])
    if not results:
        sr = _disc_get('/database/search', {
            'artist': artist, 'release_title': album,
            'type': 'release', 'per_page': 3,
        })
        results = sr.get('results', [])

    if not results:
        return out

    res = results[0]
    out['discogs_url'] = res.get('uri', '')
    if out['discogs_url'] and not out['discogs_url'].startswith('http'):
        out['discogs_url'] = 'https://www.discogs.com' + out['discogs_url']

    # Fetch full master/release for credits
    res_type = res.get('type', 'release')
    rid      = res.get('id')
    if rid:
        detail = _disc_get(f'/{res_type}s/{rid}')
        out['year']    = detail.get('year') or res.get('year')
        out['country'] = detail.get('country', '')
        out['labels']  = [l['name'] for l in detail.get('labels', [])[:3]]
        out['formats'] = list({
            f.get('name', '') for f in detail.get('formats', []) if f.get('name')
        })

        # Credits: extraartists
        credits = []
        seen = set()
        for ea in detail.get('extraartists', []):
            key = (ea.get('name', '').strip(), ea.get('role', '').strip())
            if key not in seen and key[0] and key[1]:
                seen.add(key)
                credits.append({'name': key[0], 'role': key[1]})
        out['credits'] = credits[:20]

        # Videos → YouTube links
        videos = []
        for v in detail.get('videos', []):
            url = v.get('uri', '') or v.get('url', '')
            title = v.get('title', '')
            if url:
                videos.append({'url': url, 'title': title})
        out['videos'] = videos[:6]

        # Social / external links from Discogs (urls field if present)
        out['links'] = [
            u for u in detail.get('urls', []) if u
        ]

    return out


def enrich(artist, album):
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        lfm_f  = ex.submit(get_lastfm_info,  artist, album)
        disc_f = ex.submit(get_discogs_info, artist, album)
        return {'lastfm': lfm_f.result(), 'discogs': disc_f.result()}


# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg" href="/books-svgrepo-com.svg" />
<title>Chart Viewer</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#e0e0e0;font-family:"Segoe UI",system-ui,sans-serif;
     display:flex;height:100vh;overflow:hidden}

/* ── Sidebar ── */
#sidebar{width:260px;min-width:220px;background:#111;border-right:1px solid #1e1e1e;
         display:flex;flex-direction:column;overflow:hidden;flex-shrink:0}

#sb-head{padding:12px 14px;border-bottom:1px solid #1e1e1e;flex-shrink:0}
#sb-head h1{font-size:.88rem;font-weight:700;color:#fff;letter-spacing:.05em;
             text-transform:uppercase}
#sb-head h1 span{color:#c00}

#sb-list{flex:1;overflow-y:auto;padding:6px 0}
.col-item{padding:8px 14px;cursor:pointer;font-size:.83rem;color:#888;
           border-left:3px solid transparent;display:flex;
           justify-content:space-between;align-items:center;transition:background .1s}
.col-item:hover{background:#1a1a1a;color:#ccc;border-left-color:#333}
.col-item.active{background:#1a1a1a;color:#fff;border-left-color:#c00;font-weight:600}
.col-count{font-size:.68rem;color:#444}

#sb-fetch{padding:12px 14px;border-top:1px solid #1e1e1e;flex-shrink:0}
#sb-fetch label{font-size:.7rem;color:#555;text-transform:uppercase;
                letter-spacing:.06em;display:block;margin-bottom:6px}
#fetch-row{display:flex;gap:6px}
#fetch-id{flex:1;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:5px;
          color:#e0e0e0;padding:6px 9px;font-size:.82rem;outline:none}
#fetch-id:focus{border-color:#c00}
#fetch-id::placeholder{color:#3a3a3a}
#fetch-btn{background:#c00;color:#fff;border:none;border-radius:5px;
           padding:6px 12px;font-size:.8rem;font-weight:600;cursor:pointer;
           white-space:nowrap;flex-shrink:0}
#fetch-btn:hover{background:#e00}
#fetch-btn:disabled{background:#444;cursor:not-allowed}

#job-status{margin-top:8px;font-size:.72rem;min-height:1.2em}
#job-status.running{color:#f0883e}
#job-status.done{color:#3fb950}
#job-status.err{color:#f85149}

/* ── Main area ── */
#main{flex:1;display:flex;overflow:hidden;min-width:0}

/* ── Grid panel ── */
#grid-panel{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
#toolbar{padding:10px 14px;background:#0f0f0f;border-bottom:1px solid #1e1e1e;
         display:flex;align-items:center;gap:10px;flex-shrink:0}
#col-title{font-size:.92rem;font-weight:700;color:#fff;white-space:nowrap;
           overflow:hidden;text-overflow:ellipsis;max-width:260px}
#search{flex:1;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:5px;
        color:#e0e0e0;padding:5px 10px;font-size:.82rem;outline:none}
#search:focus{border-color:#555}
#search::placeholder{color:#3a3a3a}
#grid-count{font-size:.7rem;color:#444;white-space:nowrap}

#grid-wrap{flex:1;overflow-y:auto;padding:10px}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(105px,1fr));gap:6px}

#placeholder{flex:1;display:flex;align-items:center;justify-content:center;
             color:#222;font-size:1rem;flex-direction:column;gap:8px}
#placeholder span{font-size:2.5rem}

/* ── Album cards ── */
.card{position:relative;cursor:pointer;border-radius:4px;overflow:hidden;
      aspect-ratio:1;background:#1a1a1a;transition:transform .15s,box-shadow .15s}
.card:hover{transform:scale(1.05);box-shadow:0 6px 22px rgba(0,0,0,.7);z-index:2}
.card.active{outline:2px solid #fff;transform:scale(1.04)}
.card img{width:100%;height:100%;object-fit:cover;display:block}
.no-cover{width:100%;height:100%;display:flex;align-items:center;
          justify-content:center;font-size:2rem;color:#2a2a2a}
.rank{position:absolute;top:4px;left:4px;background:rgba(0,0,0,.78);
      color:#fff;font-size:.58rem;font-weight:700;padding:2px 5px;border-radius:3px}
.hover-info{position:absolute;bottom:0;left:0;right:0;
            background:linear-gradient(transparent,rgba(0,0,0,.92));
            padding:18px 5px 5px;opacity:0;transition:opacity .15s}
.card:hover .hover-info,.card.active .hover-info{opacity:1}
.hi-artist{font-size:.58rem;color:#999;white-space:nowrap;
           overflow:hidden;text-overflow:ellipsis}
.hi-album{font-size:.66rem;font-weight:600;color:#fff;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ── Detail panel ── */
#detail{width:300px;min-width:260px;background:#111;border-left:1px solid #1e1e1e;
        display:flex;flex-direction:column;overflow:hidden;flex-shrink:0}
#d-placeholder{flex:1;display:flex;align-items:center;justify-content:center;
               color:#2a2a2a;font-size:.88rem;text-align:center;padding:20px}

#d-cover-wrap{width:100%;aspect-ratio:1;overflow:hidden;background:#1a1a1a;flex-shrink:0}
#d-cover{width:100%;height:100%;object-fit:cover;display:block}
#d-no-cover{width:100%;height:100%;display:flex;align-items:center;
            justify-content:center;font-size:5rem;color:#1e1e1e}

#d-info{padding:12px 13px 9px;border-bottom:1px solid #1e1e1e;flex-shrink:0}
#d-rank{font-size:.62rem;font-weight:700;color:#555;letter-spacing:.07em;
        text-transform:uppercase;margin-bottom:3px}
#d-artist{font-size:.78rem;color:#777;margin-bottom:3px;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#d-album{font-size:.98rem;font-weight:700;color:#fff;line-height:1.3}

#d-link{padding:7px 13px 8px;border-bottom:1px solid #1e1e1e;flex-shrink:0;
        display:flex;gap:6px;flex-wrap:wrap}
.d-btn{display:inline-flex;align-items:center;gap:5px;border:none;border-radius:4px;
       padding:5px 12px;font-size:.74rem;font-weight:600;cursor:pointer;
       text-decoration:none;white-space:nowrap}
.d-btn-orpheus{background:#2a4a2a;color:#7bc97b}
.d-btn-orpheus:hover{background:#335533}
.d-btn-yt{background:#c00;color:#fff}
.d-btn-yt:hover{background:#e00}

#d-player-wrap{background:#000;flex-shrink:0}
#d-player{width:100%;aspect-ratio:16/9;border:none;display:block}
#d-no-video{padding:14px;color:#2a2a2a;font-size:.78rem;text-align:center;aspect-ratio:16/9;
            display:flex;align-items:center;justify-content:center}

#d-extra{flex:1;overflow-y:auto;padding:10px 13px 14px;display:flex;flex-direction:column;gap:9px}
.d-loading{color:#333;font-size:.74rem;text-align:center;padding:12px 0}
.d-section{display:flex;flex-direction:column;gap:4px}
.d-section-title{font-size:.58rem;font-weight:700;color:#444;text-transform:uppercase;
                 letter-spacing:.08em;margin-bottom:2px}
.d-text{font-size:.73rem;color:#666;line-height:1.5;
        display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical;overflow:hidden}
.d-text.expanded{-webkit-line-clamp:unset}
.d-expand{font-size:.68rem;color:#555;cursor:pointer;background:none;border:none;
          padding:2px 0;text-align:left}
.d-expand:hover{color:#888}
.d-tags{display:flex;flex-wrap:wrap;gap:4px}
.d-tag{background:#1e1e1e;color:#666;font-size:.63rem;padding:2px 7px;border-radius:10px}
.d-credits{display:flex;flex-direction:column;gap:3px}
.d-credit{font-size:.7rem;color:#555;display:flex;gap:6px}
.d-credit-role{color:#444;font-size:.63rem;flex-shrink:0;min-width:70px}
.d-credit-name{color:#777}
.d-videos{display:flex;flex-direction:column;gap:4px}
.d-video-link{font-size:.72rem;color:#8ab4f8;text-decoration:none;
              white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.d-video-link:hover{color:#aecbfa;text-decoration:underline}
.d-stats{font-size:.68rem;color:#3a3a3a}
.d-meta{font-size:.68rem;color:#3a3a3a;display:flex;flex-wrap:wrap;gap:5px}
.d-meta span{background:#161616;padding:1px 6px;border-radius:3px}
.d-ext-link{font-size:.72rem;color:#8ab4f8;text-decoration:none}
.d-ext-link:hover{text-decoration:underline}

::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#222;border-radius:2px}
</style>
</head>
<body>

<div id="sidebar">
  <div id="sb-head"><h1><span>▶</span> Chart Viewer</h1></div>
  <div id="sb-list"></div>
  <div id="sb-fetch">
    <label>Obtener collage</label>
    <div id="fetch-row">
      <input id="fetch-id" type="number" placeholder="ID del collage…">
      <button id="fetch-btn">Obtener</button>
    </div>
    <div id="job-status"></div>
  </div>
</div>

<div id="main">
  <div id="grid-panel">
    <div id="toolbar" style="display:none">
      <span id="col-title"></span>
      <input id="search" type="text" placeholder="Buscar artista o álbum…" autocomplete="off">
      <span id="grid-count"></span>
    </div>
    <div id="grid-wrap" style="display:none"><div id="grid"></div></div>
    <div id="placeholder">
      <span>🎵</span>
      Selecciona un collage del panel izquierdo
    </div>
  </div>
  <div id="detail">
    <div id="d-placeholder">← Selecciona un álbum</div>
  </div>
</div>

<script>
let ALL = [], current = -1, pollTimer = null;

// ── Helpers ──────────────────────────────────────────────────────────────────
function esc(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
async function api(ep, body) {
  const opts = body !== undefined
    ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}
    : {method:'GET'};
  return (await fetch(ep, opts)).json();
}

// ── Sidebar collage list ──────────────────────────────────────────────────────
async function loadList(selectName) {
  const cols = await api('/api/list');
  const el   = document.getElementById('sb-list');
  el.innerHTML = '';
  for (const c of cols) {
    const div = document.createElement('div');
    div.className = 'col-item';
    div.dataset.name = c.name;
    const meta = [c.count + ' álb.', c.fetched || c.updated || ''].filter(Boolean).join(' · ');
    div.innerHTML = `<span>${esc(c.name)}</span><span class="col-count">${meta}</span>`;
    div.addEventListener('click', () => loadCollage(c.name, div));
    el.appendChild(div);
    if (c.name === selectName) div.click();
  }
}

// ── Load collage data ─────────────────────────────────────────────────────────
async function loadCollage(name, sidebarEl) {
  document.querySelectorAll('.col-item').forEach(e => e.classList.remove('active'));
  if (sidebarEl) sidebarEl.classList.add('active');

  const data = await api('/api/data/' + encodeURIComponent(name));
  if (!data || data.error) return;

  ALL     = data;
  current = -1;

  document.getElementById('col-title').textContent = name;
  document.getElementById('toolbar').style.display   = '';
  document.getElementById('grid-wrap').style.display = '';
  document.getElementById('placeholder').style.display = 'none';
  document.getElementById('search').value = '';
  document.getElementById('detail').innerHTML =
    '<div id="d-placeholder">← Selecciona un álbum</div>';

  render(ALL);
}

// ── Render grid ───────────────────────────────────────────────────────────────
function render(data) {
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  document.getElementById('grid-count').textContent = data.length + ' álbumes';

  data.forEach(a => {
    const card = document.createElement('div');
    card.className = 'card';
    card.dataset.rank = a.rank ?? 0;

    const img = a.cover
      ? `<img src="${esc(a.cover)}" loading="lazy" alt="" onerror="this.style.display='none'">`
      : `<div class="no-cover">🎵</div>`;

    card.innerHTML = img
      + `<div class="rank">#${a.rank ?? '?'}</div>`
      + `<div class="hover-info"><div class="hi-artist">${esc(a.artist)}</div>`
      + `<div class="hi-album">${esc(a.album)}</div></div>`;

    card.addEventListener('click', () => select(a, card));
    grid.appendChild(card);
  });
}

// ── Select album ──────────────────────────────────────────────────────────────
function select(a, cardEl) {
  current = a.rank ?? 0;
  document.querySelectorAll('.card').forEach(c => c.classList.remove('active'));
  if (cardEl) { cardEl.classList.add('active'); cardEl.scrollIntoView({block:'nearest'}); }

  const cover = a.cover
    ? `<img id="d-cover" src="${esc(a.cover)}" alt="" onerror="this.style.opacity=0">`
    : `<div id="d-no-cover">🎵</div>`;

  const orpheusBtn = a.orpheus_url
    ? `<a class="d-btn d-btn-orpheus" href="${esc(a.orpheus_url)}" target="_blank">◈ Orpheus</a>`
    : '';

  const ytSearchUrl = `https://www.youtube.com/results?search_query=${encodeURIComponent((a.artist||'')+' '+(a.album||'')+' full album')}`;
  const ytBtn = a.embed
    ? ''
    : `<a class="d-btn d-btn-yt" href="${esc(ytSearchUrl)}" target="_blank">▶ Buscar en YouTube</a>`;

  const player = a.embed
    ? `<iframe id="d-player" src="${esc(a.embed)}" allowfullscreen allow="encrypted-media"></iframe>`
    : `<div id="d-no-video">Sin vídeo disponible</div>`;

  document.getElementById('detail').innerHTML =
    `<div id="d-cover-wrap">${cover}</div>`
    + `<div id="d-info"><div id="d-rank">Puesto #${a.rank ?? '?'} de ${ALL.length}</div>`
    + `<div id="d-artist">${esc(a.artist)}</div><div id="d-album">${esc(a.album)}</div></div>`
    + `<div id="d-link">${orpheusBtn}${ytBtn}</div>`
    + `<div id="d-player-wrap">${player}</div>`
    + `<div id="d-extra"><div class="d-loading">Cargando info…</div></div>`;

  enrichDetail(a.artist, a.album);
}

// ── Enrichment ────────────────────────────────────────────────────────────────
let _enrichArtist = null, _enrichAlbum = null;

async function enrichDetail(artist, album) {
  _enrichArtist = artist; _enrichAlbum = album;
  let data;
  try {
    const r = await fetch('/api/enrich?' + new URLSearchParams({artist, album}));
    data = await r.json();
  } catch(e) {
    const el = document.getElementById('d-extra');
    if (el && _enrichArtist === artist) el.innerHTML = '<div class="d-loading">Error al cargar</div>';
    return;
  }
  const el = document.getElementById('d-extra');
  if (!el || _enrichArtist !== artist || _enrichAlbum !== album) return;

  const lf = data.lastfm  || {};
  const dc = data.discogs || {};
  const parts = [];

  // Tags
  if (lf.tags && lf.tags.length)
    parts.push(`<div class="d-section"><div class="d-section-title">Géneros</div>`
      + `<div class="d-tags">${lf.tags.map(t=>`<span class="d-tag">${esc(t)}</span>`).join('')}</div></div>`);

  // Album wiki
  if (lf.album_wiki)
    parts.push(`<div class="d-section"><div class="d-section-title">Sobre el álbum</div>`
      + `<div class="d-text" id="d-album-bio">${esc(lf.album_wiki)}</div>`
      + `<button class="d-expand" onclick="toggleExpand('d-album-bio',this)">Leer más ▾</button></div>`);

  // Stats
  const stats = [];
  if (lf.album_listeners) stats.push(`${fmt(lf.album_listeners)} oyentes`);
  if (lf.album_playcount) stats.push(`${fmt(lf.album_playcount)} scrobbles`);
  if (dc.year)    stats.push(String(dc.year));
  if (dc.country) stats.push(dc.country);
  if (stats.length)
    parts.push(`<div class="d-section"><div class="d-stats">${esc(stats.join('  ·  '))}</div></div>`);

  // Formats / labels
  const meta = [...(dc.formats||[]), ...(dc.labels||[])];
  if (meta.length)
    parts.push(`<div class="d-section"><div class="d-meta">${meta.map(m=>`<span>${esc(m)}</span>`).join('')}</div></div>`);

  // Tracklist
  if (lf.tracklist && lf.tracklist.length)
    parts.push(`<div class="d-section"><div class="d-section-title">Canciones</div>`
      + `<div class="d-text" id="d-tracks">${lf.tracklist.map((t,i)=>`${i+1}. ${esc(t)}`).join('<br>')}</div>`
      + `<button class="d-expand" onclick="toggleExpand('d-tracks',this)">Leer más ▾</button></div>`);

  // Credits
  if (dc.credits && dc.credits.length)
    parts.push(`<div class="d-section"><div class="d-section-title">Créditos</div>`
      + `<div class="d-credits">${dc.credits.map(c=>
          `<div class="d-credit"><span class="d-credit-role">${esc(c.role)}</span>`
          + `<span class="d-credit-name">${esc(c.name)}</span></div>`).join('')}</div></div>`);

  // Artist bio
  if (lf.artist_bio)
    parts.push(`<div class="d-section"><div class="d-section-title">Artista</div>`
      + `<div class="d-text" id="d-artist-bio">${esc(lf.artist_bio)}</div>`
      + `<button class="d-expand" onclick="toggleExpand('d-artist-bio',this)">Leer más ▾</button></div>`);

  // Similar artists
  if (lf.similar && lf.similar.length)
    parts.push(`<div class="d-section"><div class="d-section-title">Artistas similares</div>`
      + `<div class="d-tags">${lf.similar.map(s=>`<span class="d-tag">${esc(s)}</span>`).join('')}</div></div>`);

  // Videos from Discogs
  if (dc.videos && dc.videos.length)
    parts.push(`<div class="d-section"><div class="d-section-title">Vídeos</div>`
      + `<div class="d-videos">${dc.videos.map(v=>
          `<a class="d-video-link" href="${esc(v.url)}" target="_blank">▶ ${esc(v.title||v.url)}</a>`
        ).join('')}</div></div>`);

  // External links
  const extLinks = [];
  if (dc.discogs_url) extLinks.push(`<a class="d-ext-link" href="${esc(dc.discogs_url)}" target="_blank">Discogs</a>`);
  for (const u of (dc.links||[])) extLinks.push(`<a class="d-ext-link" href="${esc(u)}" target="_blank">${esc(new URL(u).hostname.replace('www.',''))}</a>`);
  if (extLinks.length)
    parts.push(`<div class="d-section" style="gap:5px">${extLinks.join('')}</div>`);

  el.innerHTML = parts.length ? parts.join('') : '<div class="d-loading">Sin datos adicionales</div>';
}

function fmt(n) {
  if (n >= 1e6) return (n/1e6).toFixed(1).replace(/\.0$/,'') + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(0) + 'K';
  return String(n);
}

function toggleExpand(id, btn) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle('expanded');
  btn.textContent = el.classList.contains('expanded') ? 'Leer menos ▴' : 'Leer más ▾';
}

// ── Search ────────────────────────────────────────────────────────────────────
document.getElementById('search').addEventListener('input', e => {
  const q = e.target.value.trim().toLowerCase();
  render(q ? ALL.filter(a =>
    (a.artist||'').toLowerCase().includes(q) || (a.album||'').toLowerCase().includes(q)) : ALL);
});

// ── Keyboard navigation ───────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (document.activeElement === document.getElementById('search') ||
      document.activeElement === document.getElementById('fetch-id')) return;
  if (!['ArrowRight','ArrowLeft','ArrowDown','ArrowUp'].includes(e.key)) return;
  e.preventDefault();
  const cards = [...document.querySelectorAll('.card')];
  if (!cards.length) return;
  const idx  = cards.findIndex(c => +c.dataset.rank === current);
  const next = (e.key === 'ArrowRight' || e.key === 'ArrowDown')
    ? Math.min(idx + 1, cards.length - 1)
    : Math.max(idx - 1, 0);
  const card = cards[next];
  const rank = +card.dataset.rank;
  const item = ALL.find(a => (a.rank ?? 0) === rank);
  if (item) select(item, card);
});

// ── Fetch new collage ─────────────────────────────────────────────────────────
const fetchBtn   = document.getElementById('fetch-btn');
const fetchInput = document.getElementById('fetch-id');
const statusEl   = document.getElementById('job-status');

function setStatus(msg, cls) {
  statusEl.className = cls || '';
  statusEl.textContent = msg;
}

async function startFetch() {
  const id = fetchInput.value.trim();
  if (!id || isNaN(+id)) { setStatus('Introduce un ID válido', 'err'); return; }
  fetchBtn.disabled = true;
  setStatus('Iniciando…', 'running');
  const r = await api('/api/fetch', {collage_id: +id});
  if (r.error) { setStatus(r.error, 'err'); fetchBtn.disabled = false; return; }
  pollJob();
}

function pollJob() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    const s = await api('/api/job');
    if (s.running) {
      setStatus('⏳ Descargando… ' + (s.log ? s.log.split('\n').pop() : ''), 'running');
      pollJob();
    } else if (s.done) {
      setStatus('✓ Completado', 'done');
      fetchBtn.disabled = false;
      await loadList(s.last_name);
    } else if (s.error) {
      setStatus('✗ ' + s.error.split('\n')[0], 'err');
      fetchBtn.disabled = false;
    } else {
      fetchBtn.disabled = false;
    }
  }, 2000);
}

fetchBtn.addEventListener('click', startFetch);
fetchInput.addEventListener('keydown', e => { if (e.key === 'Enter') startFetch(); });

// ── Boot ──────────────────────────────────────────────────────────────────────
loadList();
</script>
</body>
</html>"""


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _send(self, body, ctype='text/html; charset=utf-8', code=200):
        if isinstance(body, str): body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(json.dumps(data, ensure_ascii=False),
                   'application/json; charset=utf-8', code)

    def _body(self):
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/api/list':
            cols = list_collages()
            # Enrich with file metadata if not already present
            for c in cols:
                fp = os.path.join(DATA_DIR, c['name'] + '.json')
                c.setdefault('count', 0)
                if os.path.exists(fp) and not c.get('count'):
                    try:
                        with open(fp, encoding='utf-8') as f:
                            c['count'] = len(json.load(f))
                    except Exception:
                        pass
            self._json(cols); return

        if path.startswith('/api/data/'):
            name = unquote(path[len('/api/data/'):])
            data = read_collage(name)
            if data is None: self._json({'error': 'Not found'}, 404); return
            self._json(data); return

        if path == '/api/job':
            with _job_lock: status = dict(_job)
            self._json(status); return

        if path == '/api/enrich':
            from urllib.parse import parse_qs
            qs     = parse_qs(urlparse(self.path).query)
            artist = qs.get('artist', [''])[0].strip()
            album  = qs.get('album',  [''])[0].strip()
            if not artist or not album:
                self._json({'error': 'artist and album required'}, 400); return
            self._json(enrich(artist, album)); return

        self._send(HTML)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            d = self._body()
        except Exception as e:
            self._json({'error': str(e)}, 400); return

        if path == '/api/fetch':
            with _job_lock:
                if _job['running']:
                    self._json({'error': 'Ya hay una tarea en curso'}); return
            cid = d.get('collage_id')
            if not cid:
                self._json({'error': 'collage_id requerido'}, 400); return
            threading.Thread(target=_run_fetch, args=(cid,), daemon=True).start()
            self._json({'ok': True}); return

        self._json({'error': 'Unknown endpoint'}, 404)


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f'Chart Viewer → http://localhost:{PORT}')
    print('Ctrl+C to stop')
    HTTPServer(('', PORT), Handler).serve_forever()
