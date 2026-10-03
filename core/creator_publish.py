"""Direct YouTube publishing bridge with Studio fallback."""
from __future__ import annotations

import os
import webbrowser
from pathlib import Path
from typing import Any

from core.user_paths import get_user_data_dir

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

def _secret_path() -> Path | None:
    env = os.environ.get("BRAHMA_YOUTUBE_CLIENT_SECRET", "").strip()
    candidates = [Path(env).expanduser()] if env else []
    candidates += [
        get_user_data_dir() / "config" / "youtube_client_secret.json",
        get_user_data_dir() / "config" / "google_client_secret.json",
    ]
    return next((x.resolve() for x in candidates if x.is_file()), None)

def publish_project(
    manifest: dict[str, Any],
    *,
    privacy: str = "private",
    playlist_id: str | None = None,
) -> dict[str, Any]:
    video = Path(str(manifest.get("rendered_video") or "")).expanduser().resolve()
    if not video.is_file():
        raise RuntimeError("Render the Creator project before publishing.")
    metadata = manifest.get("metadata") or {}
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError("YouTube API dependencies are not installed.") from exc

    token_path = get_user_data_dir() / "config" / "youtube_token.json"
    credentials = None
    if token_path.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception:
            credentials = None
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        secret = _secret_path()
        if not secret:
            webbrowser.open("https://studio.youtube.com/")
            return {
                "ok": False,
                "mode": "studio_fallback",
                "video": str(video),
                "metadata": metadata,
                "message": "Direct YouTube API credentials are not configured; YouTube Studio was opened with the package ready.",
            }
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
        credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")

    service = build("youtube", "v3", credentials=credentials)
    description = str(metadata.get("description") or "")
    tags = " ".join(str(x) for x in metadata.get("hashtags", [])[:15])
    if tags:
        description = description.rstrip() + "\n\n" + tags
    body = {
        "snippet": {
            "title": str(metadata.get("title") or video.stem)[:100],
            "description": description[:5000],
            "tags": [str(x) for x in metadata.get("tags", [])[:500]],
            "categoryId": str(metadata.get("category_id") or "22"),
        },
        "status": {
            "privacyStatus": privacy if privacy in {"private", "unlisted", "public"} else "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    request = service.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True),
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    video_id = response.get("id")
    thumbnail = manifest.get("thumbnail_path")
    if video_id and thumbnail and Path(thumbnail).is_file():
        service.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(Path(thumbnail).resolve()), mimetype="image/jpeg"),
        ).execute()
    if video_id and playlist_id:
        service.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        ).execute()
    return {
        "ok": True,
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "privacy": body["status"]["privacyStatus"],
    }
