"""
Watcher Agent — runs every 6 hours via cron.
Checks each active client's YouTube channel for new videos,
downloads audio-only, transcribes with Whisper, saves to Supabase,
then enqueues a clipper job.
"""

import os
import json
import tempfile
import time
from dotenv import load_dotenv

load_dotenv(override=True)
import logging
from datetime import datetime, timezone

import yt_dlp
from faster_whisper import WhisperModel
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# "tiny" for Railway free tier with very tight RAM; "base" for better accuracy
# "base" peak RAM with int8 ≈ 150MB — fits in Railway 512MB
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "tiny")

SESSION_BUCKET = "sessions"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

_cookie_file: str | None = None


def get_cookie_file() -> str | None:
    global _cookie_file
    if _cookie_file is not None:
        return _cookie_file
    try:
        data = supabase.storage.from_(SESSION_BUCKET).download("sessions/youtube_cookies.txt")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="wb")
        tmp.write(data)
        tmp.close()
        _cookie_file = tmp.name
        log.info("YouTube cookies loaded.")
    except Exception:
        _cookie_file = ""
    return _cookie_file or None


def _ydl_opts(extra: dict = {}) -> dict:
    cookie = get_cookie_file()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        **({"cookiefile": cookie} if cookie else {}),
    }
    opts.update(extra)
    return opts


# Load Whisper once per process — not once per video
_whisper_model: WhisperModel | None = None


def get_whisper_model() -> WhisperModel:
    global _whisper_model
    if _whisper_model is None:
        log.info(f"Loading Whisper model '{WHISPER_MODEL}' (int8, cpu)...")
        _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper_model


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def get_active_clients() -> list[dict]:
    result = supabase.table("clients").select("*").eq("active", True).execute()
    return result.data


def get_processed_video_ids(client_id: str) -> set[str]:
    result = (
        supabase.table("videos")
        .select("youtube_video_id")
        .eq("client_id", client_id)
        .execute()
    )
    return {row["youtube_video_id"] for row in result.data}


def save_video(client_id: str, meta: dict, segments: list[dict]) -> str:
    full_text = " ".join(s["text"] for s in segments)
    row = {
        "client_id": client_id,
        "youtube_video_id": meta["id"],
        "title": meta["title"],
        "url": meta["url"],
        "duration_seconds": int(meta["duration"]) if meta.get("duration") else None,
        "upload_date": meta.get("upload_date"),
        "view_count": meta.get("view_count"),
        "thumbnail_url": meta.get("thumbnail"),
        "description": meta.get("description"),
        "like_count": meta.get("like_count"),
        "channel_name": meta.get("channel"),
        "transcript_segments": json.dumps(segments),
        "transcript_text": full_text,
        "status": "transcribed",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = supabase.table("videos").insert(row).execute()
    return result.data[0]["id"]


def enqueue_clip_job(client_id: str, video_db_id: str) -> None:
    supabase.table("jobs").insert({
        "client_id": client_id,
        "video_id": video_db_id,
        "job_type": "clip",
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

def fetch_recent_videos(channel_url: str, limit: int = 5) -> list[dict]:
    """Return metadata for the N most recent videos without downloading."""
    with yt_dlp.YoutubeDL(_ydl_opts({"extract_flat": True, "playlistend": limit})) as ydl:
        info = ydl.extract_info(channel_url, download=False)

    entries = info.get("entries") or []
    videos = []
    for entry in entries:
        if not entry or not entry.get("id"):
            continue
        videos.append({
            "id": entry["id"],
            "title": entry.get("title", ""),
            "url": f"https://www.youtube.com/watch?v={entry['id']}",
            "duration": entry.get("duration"),
            "upload_date": entry.get("upload_date"),
            "view_count": entry.get("view_count"),
            "thumbnail": entry.get("thumbnail"),
        })
    return videos


def fetch_full_metadata(video_url: str) -> dict:
    """Fetch complete metadata for a single video (no download)."""
    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        info = ydl.extract_info(video_url, download=False)
    return {
        "id": info["id"],
        "title": info.get("title", ""),
        "url": video_url,
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "thumbnail": info.get("thumbnail"),
        "description": info.get("description", ""),
        "like_count": info.get("like_count"),
        "channel": info.get("channel", ""),
    }


def download_audio(video_url: str, output_stem: str) -> str:
    """
    Download audio-only (mp3). Much faster and smaller than full video.
    The clipper agent will re-download video segments it needs separately.
    Returns the path to the mp3 file.
    """
    with yt_dlp.YoutubeDL(_ydl_opts({
        "format": "bestaudio/best",
        "outtmpl": output_stem,
        "retries": 10,
        "fragment_retries": 10,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
    })) as ydl:
        ydl.download([video_url])

    mp3_path = output_stem + ".mp3"
    if not os.path.exists(mp3_path):
        raise FileNotFoundError(f"Audio download failed — expected {mp3_path}")
    return mp3_path


# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------

def _transcribe_chunk(model: WhisperModel, audio_path: str, offset: float = 0.0) -> list[dict]:
    segments, _ = model.transcribe(audio_path, beam_size=5, word_timestamps=True)
    result = []
    for seg in segments:
        if not seg.text.strip():
            continue
        words = [
            {"start": round(w.start + offset, 2), "end": round(w.end + offset, 2), "word": w.word.strip()}
            for w in (seg.words or []) if w.word.strip()
        ]
        result.append({"start": round(seg.start + offset, 2), "end": round(seg.end + offset, 2),
                        "text": seg.text.strip(), "words": words})
    return result


def transcribe(audio_path: str) -> list[dict]:
    model = get_whisper_model()
    CHUNK = 600  # 10 minutes

    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True,
    )
    try:
        duration = float(r.stdout.strip())
    except ValueError:
        duration = 0.0

    if duration <= CHUNK * 1.2:
        return _transcribe_chunk(model, audio_path)

    log.info(f"Long audio ({duration/60:.1f} min) — transcribing in 10-min chunks.")
    all_segments: list[dict] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        chunk_start = 0.0
        idx = 0
        while chunk_start < duration:
            chunk_path = os.path.join(tmpdir, f"chunk_{idx}.mp3")
            subprocess.run([
                "ffmpeg", "-y", "-i", audio_path,
                "-ss", str(chunk_start), "-t", str(CHUNK),
                "-c", "copy", chunk_path,
            ], capture_output=True)
            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 1000:
                log.info(f"Transcribing chunk {idx} (offset {chunk_start/60:.1f} min)...")
                all_segments.extend(_transcribe_chunk(model, chunk_path, offset=chunk_start))
            chunk_start += CHUNK
            idx += 1
    return all_segments


# ---------------------------------------------------------------------------
# Per-client logic
# ---------------------------------------------------------------------------

def process_client(client: dict) -> None:
    client_id: str = client["id"]
    name: str = client["name"]
    channel_url: str = client["youtube_channel_url"]

    log.info(f"[{name}] Checking: {channel_url}")

    processed_ids = get_processed_video_ids(client_id)
    recent = fetch_recent_videos(channel_url, limit=5)
    new_videos = [v for v in recent if v["id"] not in processed_ids]

    if not new_videos:
        log.info(f"[{name}] No new videos.")
        return

    log.info(f"[{name}] {len(new_videos)} new video(s). Processing the latest.")

    # Process one per run to stay within Railway memory + time limits.
    # Remaining will be picked up on the next 6-hour cron tick.
    video = new_videos[0]
    log.info(f"[{name}] Video: \"{video['title']}\" ({video['id']})")

    log.info(f"[{name}] Fetching full metadata...")
    video = fetch_full_metadata(video["url"])

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_stem = os.path.join(tmpdir, "audio")

        log.info(f"[{name}] Downloading audio...")
        audio_path = download_audio(video["url"], audio_stem)
        size_mb = os.path.getsize(audio_path) / 1_000_000
        log.info(f"[{name}] Audio downloaded ({size_mb:.1f} MB). Transcribing...")

        segments = transcribe(audio_path)
        word_count = sum(len(s["text"].split()) for s in segments)
        log.info(f"[{name}] Transcribed {len(segments)} segments, ~{word_count} words.")

    # tmpdir (and audio file) is now deleted automatically

    video_db_id = save_video(client_id, video, segments)
    enqueue_clip_job(client_id, video_db_id)
    log.info(f"[{name}] Saved (db id={video_db_id}), clipper job enqueued. Done.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info(f"Watcher starting — {datetime.now(timezone.utc).isoformat()}")
    clients = get_active_clients()
    log.info(f"{len(clients)} active client(s).")

    for client in clients:
        try:
            process_client(client)
        except Exception as exc:
            log.error(f"[{client.get('name', client['id'])}] Failed: {exc}", exc_info=True)

    log.info("Watcher run complete.")


if __name__ == "__main__":
    main()
