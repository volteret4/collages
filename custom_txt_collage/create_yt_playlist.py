import re
import subprocess
import json
import argparse
import os

from sopsdotenv import load_sops_env
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Cargar .env
load_sops_env()

API_KEY = os.getenv("YOUTUBE_API_KEY")  # opcional realmente

SCOPES = ["https://www.googleapis.com/auth/youtube"]


def authenticate_youtube():
    flow = InstalledAppFlow.from_client_secrets_file(
        "client_secret.json", SCOPES
    )
    creds = flow.run_local_server(port=0)
    return build("youtube", "v3", credentials=creds)


def create_playlist(youtube, title, description):
    response = youtube.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": description
            },
            "status": {"privacyStatus": "private"}
        }
    ).execute()
    return response["id"]


def search_video(query):
    try:
        cmd = [
            "yt-dlp",
            "ytsearch1:" + query + " official audio",
            "--dump-json",
            "--skip-download",
            "--quiet"
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )

        data = json.loads(result.stdout)
        return data.get("id")

    except Exception as e:
        print(f"Error buscando '{query}': {e}")
        return None


def add_video_to_playlist(youtube, playlist_id, video_id):
    youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id
                }
            }
        }
    ).execute()


def clean_line(line):
    return re.sub(r"^\d+\.\s*", "", line).strip()


def main():
    parser = argparse.ArgumentParser(description="Crear playlist de YouTube desde TXT")
    parser.add_argument("file", help="Archivo TXT con canciones")
    parser.add_argument("--title", help="Título de la playlist")
    parser.add_argument("--description", help="Descripción")
    parser.add_argument("--playlist-id", help="ID de playlist existente")

    args = parser.parse_args()

    youtube = authenticate_youtube()

    if args.playlist_id:
        playlist_id = args.playlist_id
        print(f"Usando playlist existente: {playlist_id}")
    else:
        playlist_id = create_playlist(youtube, args.title, args.description)
        print(f"Playlist creada: {playlist_id}")

    with open(args.file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    remaining_lines = []

    for i, line in enumerate(lines):
        query = clean_line(line)
        if not query:
            remaining_lines.append(line)
            continue

        print(f"Buscando: {query}")
        video_id = search_video(query)

        if not video_id:
            print("No encontrado")
            remaining_lines.append(line)
            continue

        try:
            add_video_to_playlist(youtube, playlist_id, video_id)
            print(f"Añadido: {video_id}")

        except Exception as e:
            print(f"Error añadiendo vídeo: {e}")

            # guardar esta línea + todo lo que queda SIN romper índices
            remaining_lines.append(line)
            remaining_lines.extend(lines[i+1:])
            break

    with open(args.file, "w", encoding="utf-8") as f:
        f.writelines(remaining_lines)

    print("¡Hecho!")


if __name__ == "__main__":
    main()
