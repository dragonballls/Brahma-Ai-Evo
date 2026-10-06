from core.user_paths import get_user_data_dir
from core.runtime_paths import API_CONFIG_PATH
#youtube_video.py
import json
import re
import sys
import time
import subprocess
import shutil
import webbrowser
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus, urlparse

import pyautogui
import numpy as np

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    _TRANSCRIPT_OK = True
except ImportError:
    _TRANSCRIPT_OK = False

from config import get_os, is_windows, is_mac, is_linux

from actions.video_understanding import analyze_youtube, analyze_local_video
from actions.browser_control import browser_control, browser_evaluate_internal


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_YT_VIDEO_FILTER = "EgIQAQ%3D%3D"

def _open_url(url: str) -> bool:
    try:
        return webbrowser.open(url) is True
    except Exception as e:
        print(f"[YouTube] ⚠️ open_url failed: {e}")
        return False

def _scrape_first_video_url(query: str) -> str | None:

    if not _REQUESTS_OK:
        return None

    search_url = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}"
        f"&sp={_YT_VIDEO_FILTER}"
    )

    try:
        r    = requests.get(search_url, headers=HEADERS, timeout=10)
        html = r.text

        video_ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)

        seen = set()
        for vid in video_ids:
            if vid in seen:
                continue
            seen.add(vid)

            if f'/shorts/{vid}' in html:
                continue
            return f"https://www.youtube.com/watch?v={vid}"

    except Exception as e:
        print(f"[YouTube] ⚠️ scrape_first_video_url failed: {e}")

    return None

def _extract_video_id(url: str) -> str | None:
    match = re.search(
        r"(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([A-Za-z0-9_-]{11})", url
    )
    return match.group(1) if match else None


def _is_valid_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be"


def _ask_for_url(prompt_text: str = "YouTube video URL:") -> str | None:
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk._default_root
        if root is None:
            root = tk.Tk()
            root.withdraw()

        url = simpledialog.askstring("J.A.R.V.I.S", prompt_text, parent=root)
        return url.strip() if url else None
    except Exception as e:
        print(f"[YouTube] ⚠️ URL dialog failed: {e}")
        return None


def _get_transcript(video_id: str) -> str | None:
    if not _TRANSCRIPT_OK:
        return None
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript      = None

        lang_priority = ["en", "tr", "de", "fr", "es", "it", "pt", "ru", "ja", "ko", "ar", "zh"]

        try:
            transcript = transcript_list.find_manually_created_transcript(lang_priority)
        except Exception:
            pass

        if transcript is None:
            try:
                transcript = transcript_list.find_generated_transcript(lang_priority)
            except Exception:
                for t in transcript_list:
                    transcript = t
                    break

        if transcript is None:
            return None

        fetched = transcript.fetch()
        return " ".join(entry["text"] for entry in fetched)

    except Exception as e:
        print(f"[YouTube] ⚠️ Transcript fetch failed: {e}")
        return None


def _summarize_with_gemini(transcript: str, video_url: str) -> str:
    from llm_client import client

    max_chars = 80000
    truncated = transcript[:max_chars] + ("..." if len(transcript) > max_chars else "")

    return client.chat(
        f"Please summarize this YouTube video transcript:\n\n{truncated}",
        system=(
            "You are Brahma AI - Lite, an AI assistant. "
            "Summarize YouTube video transcripts clearly and concisely. "
            "Structure: 1-sentence overview, then 3-5 key points. "
            "Be direct. Address the user as 'sir'. "
            "Match the language of the transcript."
        ),
        max_tokens=2048,
    )


def _save_summary(content: str, video_url: str) -> str:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"youtube_summary_{ts}.txt"
    desktop  = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    filepath = desktop / filename

    header = (
        f"Brahma AI - YouTube Summary\n"
        f"{'─' * 50}\n"
        f"URL    : {video_url}\n"
        f"Date   : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"{'─' * 50}\n\n"
    )
    filepath.write_text(header + content, encoding="utf-8")

    try:
        if is_windows():
            subprocess.Popen(["notepad.exe", str(filepath)])
        elif is_mac():
            subprocess.Popen(["open", "-t", str(filepath)])
        else:
            subprocess.Popen(["xdg-open", str(filepath)])
    except Exception as e:
        print(f"[YouTube] ⚠️ Could not open text editor: {e}")

    return str(filepath)


def _scrape_video_info(video_id: str) -> dict:
    if not _REQUESTS_OK:
        return {}
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r    = requests.get(url, headers=HEADERS, timeout=12)
        html = r.text
        info = {}

        for key, pattern in [
            ("title",    r'"title":\{"runs":\[\{"text":"([^"]+)"'),
            ("channel",  r'"ownerChannelName":"([^"]+)"'),
            ("views",    r'"viewCount":"(\d+)"'),
            ("duration", r'"lengthSeconds":"(\d+)"'),
            ("likes",    r'"label":"([0-9,]+ likes)"'),
        ]:
            match = re.search(pattern, html)
            if match:
                raw = match.group(1)
                if key == "views":
                    info[key] = f"{int(raw):,}"
                elif key == "duration":
                    secs = int(raw)
                    info[key] = f"{secs // 60}:{secs % 60:02d}"
                else:
                    info[key] = raw

        return info
    except Exception as e:
        print(f"[YouTube] ⚠️ Info scrape failed: {e}")
        return {}


def _scrape_trending(region: str = "TR", max_results: int = 8) -> list[dict]:
    if not _REQUESTS_OK:
        return []
    url = f"https://www.youtube.com/feed/trending?gl={region.upper()}"
    try:
        r    = requests.get(url, headers=HEADERS, timeout=12)
        html = r.text

        titles   = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"\}\]', html)
        channels = re.findall(r'"ownerText":\{"runs":\[\{"text":"([^"]+)"', html)

        results, seen = [], set()
        for i, title in enumerate(titles):
            if title in seen or len(title) < 5:
                continue
            seen.add(title)
            channel = channels[i] if i < len(channels) else "Unknown"
            results.append({"rank": len(results) + 1, "title": title, "channel": channel})
            if len(results) >= max_results:
                break

        return results
    except Exception as e:
        print(f"[YouTube] ⚠️ Trending scrape failed: {e}")
        return []

def _scrape_first_playlist_url(query: str) -> str | None:
    if not _REQUESTS_OK:
        return None
    search_url = (
        "https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}&sp=EgIQAw%3D%3D"
    )
    try:
        r = requests.get(search_url, headers=HEADERS, timeout=10)
        ids = re.findall(r'"playlistId":"([A-Za-z0-9_-]+)"', r.text)
        seen = set()
        for playlist_id in ids:
            if playlist_id in seen:
                continue
            seen.add(playlist_id)
            return f"https://www.youtube.com/playlist?list={playlist_id}"
    except Exception as exc:
        print(f"[YouTube] playlist search failed: {exc}")
    return None


def _parse_timecode(value) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    try:
        if ":" not in raw:
            return max(0.0, float(raw))
        parts = [float(x) for x in raw.split(":")]
        if len(parts) == 2:
            return max(0.0, parts[0] * 60 + parts[1])
        if len(parts) == 3:
            return max(0.0, parts[0] * 3600 + parts[1] * 60 + parts[2])
    except (TypeError, ValueError):
        return None
    return None


def _current_video_url() -> str:
    try:
        raw = browser_evaluate_internal("window.location.href")
        value = str(raw or "").strip().strip('"').strip("'")
        return value if re.match(r"^(?:https?|file)://", value, re.I) else ""
    except Exception:
        return ""


def _control_video(parameters: dict) -> str:
    command = str(
        parameters.get("command")
        or parameters.get("control")
        or parameters.get("subaction")
        or "status"
    ).lower().strip()
    speed = parameters.get("speed", parameters.get("playback_rate"))
    position = parameters.get("position", parameters.get("seek"))
    volume = parameters.get("volume")

    js = ""
    if command in {"play", "resume"}:
        js = """(() => { const v=document.querySelector('video'); if(!v) return JSON.stringify({ok:false,error:'No HTML5 video found.'}); v.play(); return JSON.stringify({ok:true,playing:true,currentTime:v.currentTime,rate:v.playbackRate}); })()"""
    elif command in {"pause", "stop"}:
        js = """(() => { const v=document.querySelector('video'); if(!v) return JSON.stringify({ok:false,error:'No HTML5 video found.'}); v.pause(); return JSON.stringify({ok:true,playing:false,currentTime:v.currentTime,rate:v.playbackRate}); })()"""
    elif command in {"toggle", "playpause"}:
        js = """(() => { const v=document.querySelector('video'); if(!v) return JSON.stringify({ok:false,error:'No HTML5 video found.'}); if(v.paused){v.play();}else{v.pause();} return JSON.stringify({ok:true,playing:!v.paused,currentTime:v.currentTime,rate:v.playbackRate}); })()"""
    elif command in {"speed", "rate", "set_speed"}:
        try:
            rate = float(speed)
        except (TypeError, ValueError):
            return "Please provide a numeric playback speed, sir."
        if rate <= 0 or rate > 16:
            return "Playback speed must be greater than 0 and no more than 16x, sir."
        js = f"""(() => {{ const v=document.querySelector('video'); if(!v) return JSON.stringify({{ok:false,error:'No HTML5 video found.'}}); v.playbackRate={rate!r}; return JSON.stringify({{ok:true,rate:v.playbackRate,currentTime:v.currentTime,playing:!v.paused}}); }})()"""
    elif command in {"seek", "goto", "jump"}:
        seconds = _parse_timecode(position)
        if seconds is None:
            return "Please provide a seek position in seconds or mm:ss, sir."
        js = f"""(() => {{ const v=document.querySelector('video'); if(!v) return JSON.stringify({{ok:false,error:'No HTML5 video found.'}}); v.currentTime={seconds!r}; return JSON.stringify({{ok:true,currentTime:v.currentTime,rate:v.playbackRate}}); }})()"""
    elif command in {"volume", "set_volume"}:
        try:
            val = float(volume)
        except (TypeError, ValueError):
            return "Please provide a volume from 0 to 100, sir."
        if val < 0 or val > 100:
            return "Volume must be between 0 and 100, sir."
        js = f"""(() => {{ const v=document.querySelector('video'); if(!v) return JSON.stringify({{ok:false,error:'No HTML5 video found.'}}); v.volume={val/100!r}; v.muted=false; return JSON.stringify({{ok:true,volume:Math.round(v.volume*100),muted:v.muted}}); }})()"""
    elif command in {"mute", "unmute"}:
        muted = command == "mute"
        js = f"""(() => {{ const v=document.querySelector('video'); if(!v) return JSON.stringify({{ok:false,error:'No HTML5 video found.'}}); v.muted={str(muted).lower()}; return JSON.stringify({{ok:true,muted:v.muted,volume:Math.round(v.volume*100)}}); }})()"""
    elif command in {"fullscreen", "full_screen"}:
        js = """(() => { const v=document.querySelector('video'); if(!v) return JSON.stringify({ok:false,error:'No HTML5 video found.'}); const el=v.parentElement || v; (el.requestFullscreen || v.requestFullscreen)?.(); return JSON.stringify({ok:true}); })()"""
    elif command in {"status", "current"}:
        js = """(() => { const v=document.querySelector('video'); if(!v) return JSON.stringify({ok:false,error:'No HTML5 video found.'}); return JSON.stringify({ok:true,playing:!v.paused,currentTime:v.currentTime,duration:v.duration,rate:v.playbackRate,volume:Math.round(v.volume*100),muted:v.muted,src:location.href}); })()"""
    else:
        return (
            "Supported video controls are play, pause, toggle, speed, seek, "
            "volume, mute, unmute, fullscreen, and status, sir."
        )

    result = browser_evaluate_internal(js)
    raw = str(result or "").strip()
    if raw.startswith("{") or raw.startswith('"'):
        try:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = json.loads(raw.strip('"').strip("'"))
            if not data.get("ok"):
                return str(data.get("error") or "Video control failed.")
            if "rate" in data:
                return f"Done, sir. Video speed is {data['rate']}x."
            if "currentTime" in data and command in {"seek", "goto", "jump"}:
                return f"Done, sir. Video moved to {data['currentTime']:.1f} seconds."
            return "Done, sir."
        except Exception:
            pass
    return raw or "Video control completed."


def _handle_playlist(parameters: dict, player) -> str:
    query = str(parameters.get("query") or parameters.get("url") or "").strip()
    if not query:
        return "Please tell me the YouTube playlist name or URL, sir."
    url = query if _is_valid_youtube_url(query) else _scrape_first_playlist_url(query)
    if not url:
        return f"I couldn't find a YouTube playlist for '{query}', sir."
    browser_control({"action": "go_to", "url": url}, None, player, None)
    if player:
        player.write_log(f"[YouTube] Playlist: {url}")
    return f"Playing the YouTube playlist '{query}'."


def _handle_control(parameters: dict, player) -> str:
    url = str(parameters.get("url") or "").strip()
    if url:
        if not _is_valid_youtube_url(url):
            return "That is not a valid YouTube URL, sir."
        browser_control({"action": "go_to", "url": url}, None, player, None)
        time.sleep(1.5)
    current = _current_video_url()
    if not current:
        return "I don't currently have a browser video open in JARVIS, sir."
    return _control_video(parameters)


def _handle_watch(parameters: dict, player, speak) -> str:
    url = str(parameters.get("url") or parameters.get("query") or "").strip()
    file_path = str(parameters.get("file_path") or "").strip()
    question = str(
        parameters.get("question")
        or parameters.get("text")
        or "Watch this video and tell me what is happening, including important on-screen text and spoken details."
    ).strip()
    start_time = str(parameters.get("start_time") or parameters.get("start") or "").strip()
    end_time = str(parameters.get("end_time") or parameters.get("end") or "").strip()

    try:
        if url:
            if not _is_valid_youtube_url(url):
                return "Please provide a public YouTube URL, sir."
            if parameters.get("play", False):
                browser_control({"action": "go_to", "url": url}, None, player, None)
            result = analyze_youtube(
                url,
                question=question,
                start_time=start_time,
                end_time=end_time,
            )
        elif file_path:
            result = analyze_local_video(
                file_path,
                question=question,
                start_time=start_time,
                end_time=end_time,
            )
        else:
            current = _current_video_url()
            if not current:
                return "Give me the video URL or open the video in JARVIS first, sir."
            result = analyze_youtube(
                current,
                question=question,
                start_time=start_time,
                end_time=end_time,
            )

        if speak:
            speak(result)
        return result
    except Exception as exc:
        return f"Video analysis failed, sir: {exc}"


def _handle_play(parameters: dict, player) -> str:
    query = str(parameters.get("query") or parameters.get("url") or "").strip()
    if not query:
        return "Please tell me what you'd like to watch, sir."

    if _is_valid_youtube_url(query):
        navigation = browser_control({"action": "go_to", "url": query}, None, player, None)
        if isinstance(navigation, str) and (
            navigation.startswith("Navigation blocked:")
            or navigation.startswith("Timeout loading:")
            or navigation.startswith("Navigation error:")
        ):
            return f"I couldn't open that YouTube video, sir: {navigation}"
        if player:
            player.write_log(f"[YouTube] Opening URL: {query}")
        return f"Playing that YouTube video, sir."

    if player:
        player.write_log(f"[YouTube] Searching: {query}")

    print(f"[YouTube] 🔍 Scraping first non-Shorts video for: {query}")

    video_url = _scrape_first_video_url(query)

    if video_url:
        print(f"[YouTube] ▶️ Opening: {video_url}")
        if not _open_url(video_url):
            return "I found a YouTube video, but the browser could not be opened."
        return f"Playing: {query}"

    print(f"[YouTube] ⚠️ Scrape failed, opening filtered search page")
    fallback_url = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}"
        f"&sp={_YT_VIDEO_FILTER}"
    )
    if not _open_url(fallback_url):
        return "I couldn't open the YouTube search page."
    return f"Opened YouTube search for: {query} (manual selection required)"


def _handle_summarize(parameters: dict, player, speak) -> str:
    if not _TRANSCRIPT_OK:
        return "youtube-transcript-api is not installed. Run: pip install youtube-transcript-api"

    url = str(parameters.get("url") or parameters.get("query") or "").strip() or _ask_for_url("Please paste the YouTube video URL:")
    if not url:
        return "No URL provided, sir. Summary cancelled."
    if not _is_valid_youtube_url(url):
        return "That doesn't appear to be a valid YouTube URL, sir."

    video_id = _extract_video_id(url)
    if not video_id:
        return "Could not extract video ID from that URL, sir."

    if player:
        player.write_log(f"[YouTube] Summarizing: {url}")
    if speak:
        speak("Fetching the transcript now, sir. One moment.")

    transcript = _get_transcript(video_id)
    if not transcript:
        try:
            summary = analyze_youtube(
                url,
                question="Summarize this video clearly. Include the main idea, key events or arguments, and important visual/on-screen details.",
                start_time=str(parameters.get("start_time") or parameters.get("start") or ""),
                end_time=str(parameters.get("end_time") or parameters.get("end") or ""),
            )
        except Exception as e:
            return f"I couldn't retrieve a transcript or perform video understanding, sir: {e}"
    else:
        if speak:
            speak("Transcript retrieved. Generating summary now.")
        try:
            summary = _summarize_with_gemini(transcript, url)
        except Exception as e:
            return f"Summary generation failed, sir: {e}"

    if speak:
        speak(summary)

    if parameters.get("save", False):
        saved_path = _save_summary(summary, url)
        return f"Summary complete and saved to Desktop: {saved_path}"

    return summary


def _handle_get_info(parameters: dict, player, speak) -> str:
    url = parameters.get("url", "").strip()
    if not url:
        url = _ask_for_url("Please paste the YouTube video URL:")
    if not url or not _is_valid_youtube_url(url):
        return "Please provide a valid YouTube URL, sir."

    video_id = _extract_video_id(url)
    if not video_id:
        return "Could not extract video ID, sir."

    if player:
        player.write_log(f"[YouTube] Getting info: {url}")

    info = _scrape_video_info(video_id)
    if not info:
        return "Could not retrieve video information, sir."

    lines = [
        f"{key.capitalize()}: {info[key]}"
        for key in ("title", "channel", "views", "duration", "likes")
        if key in info
    ]
    result = "\n".join(lines)

    if speak:
        speak(f"Here's the video info, sir. {result.replace(chr(10), '. ')}")

    return result


def _handle_trending(parameters: dict, player, speak) -> str:
    region = parameters.get("region", "TR").upper()

    if player:
        player.write_log(f"[YouTube] Trending: {region}")

    trending = _scrape_trending(region=region, max_results=8)
    if not trending:
        return f"Could not fetch trending videos for region {region}, sir."

    lines  = [f"Top trending videos in {region}:"]
    lines += [f"{v['rank']}. {v['title']} — {v['channel']}" for v in trending]
    result = "\n".join(lines)

    if speak:
        top3   = trending[:3]
        spoken = "Here are the top trending videos, sir. " + ". ".join(
            f"Number {v['rank']}: {v['title']} by {v['channel']}" for v in top3
        )
        speak(spoken)

    return result

_ACTION_MAP = {
    "play":      _handle_play,
    "playlist":  _handle_playlist,
    "play_playlist": _handle_playlist,
    "control":   _handle_control,
    "watch":     _handle_watch,
    "analyze":   _handle_watch,
    "analyze_section": _handle_watch,
    "summarize": _handle_summarize,
    "get_info":  _handle_get_info,
    "trending":  _handle_trending,
}


def youtube_video(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "play").lower().strip()

    if player:
        player.write_log(f"[YouTube] Action: {action}")
    print(f"[YouTube] ▶️  Action: {action}  Params: {params}")

    handler = _ACTION_MAP.get(action)
    if handler is None:
        return (
            f"Unknown YouTube action: '{action}'. "
            "Available: play, playlist, play_playlist, control, watch, analyze, analyze_section, "
            "summarize, get_info, trending."
        )

    try:
        if action == "play":
            return handler(params, player) or "Done."
        return handler(params, player, speak) or "Done."
    except Exception as e:
        print(f"[YouTube] ❌ Error in {action}: {e}")
        return f"YouTube {action} failed, sir: {e}"