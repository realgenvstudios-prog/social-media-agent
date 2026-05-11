"""
Manual video processor — checks for watch_manual jobs every 5 minutes.
Submitted via the dashboard; runs the full pipeline in one shot:
download → transcribe → clip → upload → enqueue poster jobs.
"""

import os
import sys
import json
import subprocess
import tempfile
import logging
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp
import anthropic
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from supabase import create_client, Client

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLIP_BUCKET = "clips"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_whisper: WhisperModel | None = None


def get_whisper() -> WhisperModel:
    global _whisper
    if _whisper is None:
        log.info(f"Loading Whisper '{WHISPER_MODEL}'...")
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper


# ---------------------------------------------------------------------------
# Watcher pipeline
# ---------------------------------------------------------------------------

def fetch_metadata(url: str) -> dict:
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "id": info["id"],
        "title": info.get("title", ""),
        "url": url,
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "thumbnail": info.get("thumbnail"),
        "description": info.get("description", ""),
        "like_count": info.get("like_count"),
        "channel": info.get("channel", ""),
    }


def download_audio(url: str, stem: str) -> str:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": stem,
        "retries": 10,
        "fragment_retries": 10,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    path = stem + ".mp3"
    if not os.path.exists(path):
        raise FileNotFoundError(f"Audio download failed — expected {path}")
    return path


def transcribe(audio_path: str) -> list[dict]:
    model = get_whisper()
    segments, _ = model.transcribe(audio_path, beam_size=5)
    return [
        {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
        for s in segments if s.text.strip()
    ]


def save_video(client_id: str, meta: dict, segments: list[dict]) -> str:
    full_text = " ".join(s["text"] for s in segments)
    result = supabase.table("videos").insert({
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
    }).execute()
    return result.data[0]["id"]


# ---------------------------------------------------------------------------
# Clipper pipeline
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a viral short-form content expert specialising in YouTube-to-TikTok repurposing.
Given a transcript with timestamps, identify the 3 best moments to clip for TikTok, YouTube Shorts, and Instagram Reels.

Selection criteria (in order of priority):
1. Self-contained — the moment makes sense without context
2. Strong hook in the first 5 seconds — surprising stat, bold claim, or relatable pain point
3. 45–90 seconds long — long enough to deliver value, short enough to retain viewers
4. High rewatch potential — insight, emotion, or humour

Return ONLY a valid JSON array with exactly 3 objects, no extra text:
[
  {
    "start": 123.4,
    "end": 187.2,
    "hook": "one sentence explaining what makes this clip hook viewers instantly",
    "caption": "engaging caption under 150 chars with 3-5 relevant hashtags"
  }
]"""


def ask_claude(segments: list[dict], title: str) -> list[dict]:
    segments = segments[:1000]
    lines = [
        f"[{int(s['start']//60):02d}:{int(s['start']%60):02d}] {s['text']}"
        for s in segments
    ]
    message = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Video title: {title}\n\nTranscript:\n" + "\n".join(lines)}],
    )
    raw = message.content[0].text.strip()
    log.info(f"Claude: {message.usage.input_tokens} in / {message.usage.output_tokens} out tokens")
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    moments = json.loads(raw)
    if not isinstance(moments, list) or len(moments) != 3:
        raise ValueError(f"Expected 3 moments, got: {raw}")
    return moments


def download_section(url: str, start: float, end: float, stem: str) -> str:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
        "merge_output_format": "mp4",
        "outtmpl": stem,
        "retries": 10,
        "fragment_retries": 10,
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    candidates = list(Path(stem).parent.glob(Path(stem).name + "*"))
    if not candidates:
        raise FileNotFoundError(f"No file found after downloading section at {stem}")
    return str(candidates[0])


def cut_clip(input_path: str, duration: float, output_path: str) -> None:
    result = subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-t", str(duration),
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr[-500:]}")


def upload_clip(local_path: str, storage_path: str) -> None:
    with open(local_path, "rb") as f:
        data = f.read()
    supabase.storage.from_(CLIP_BUCKET).upload(
        path=storage_path,
        file=data,
        file_options={"content-type": "video/mp4", "upsert": "true"},
    )
    log.info(f"Uploaded: {storage_path}")


def save_clip_record(client_id: str, video_id: str, index: int, moment: dict, storage_path: str) -> str:
    duration = round(moment["end"] - moment["start"], 2)
    result = supabase.table("clips").insert({
        "client_id": client_id,
        "video_id": video_id,
        "clip_index": index,
        "start_seconds": moment["start"],
        "end_seconds": moment["end"],
        "duration_seconds": duration,
        "hook": moment.get("hook", ""),
        "caption": moment.get("caption", ""),
        "storage_path": storage_path,
        "status": "ready",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return result.data[0]["id"]


def enqueue_poster_jobs(client_id: str, video_id: str, clip_id: str, storage_path: str, caption: str) -> None:
    payload = {"clip_id": clip_id, "storage_path": storage_path, "caption": caption}
    for job_type in ("post_a", "post_b"):
        supabase.table("jobs").insert({
            "client_id": client_id,
            "video_id": video_id,
            "job_type": job_type,
            "status": "pending",
            "payload": json.dumps(payload),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()


# ---------------------------------------------------------------------------
# Full pipeline for one manual job
# ---------------------------------------------------------------------------

def process_manual_job(job: dict) -> None:
    job_id = job["id"]
    client_id = job["client_id"]
    payload = json.loads(job["payload"])
    url = payload["url"]

    log.info(f"Manual job {job_id}: {url}")

    supabase.table("jobs").update({
        "status": "running",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

    try:
        # ── Step 1: fetch metadata and check if already transcribed ──────────
        log.info("Fetching video metadata...")
        meta = fetch_metadata(url)
        yt_id = meta["id"]

        existing = supabase.table("videos").select("id, title, transcript_segments").eq("youtube_video_id", yt_id).execute()

        if existing.data:
            video_db_id = existing.data[0]["id"]
            title = existing.data[0]["title"]
            segments = json.loads(existing.data[0]["transcript_segments"])
            log.info(f"Video already transcribed (db id={video_db_id}), skipping download.")
        else:
            title = meta["title"]
            log.info(f"Downloading audio for: {title}")
            with tempfile.TemporaryDirectory() as tmpdir:
                audio_path = download_audio(url, os.path.join(tmpdir, "audio"))
                log.info(f"Audio ready ({os.path.getsize(audio_path)/1e6:.1f} MB). Transcribing...")
                segments = transcribe(audio_path)
            log.info(f"Transcribed {len(segments)} segments.")
            video_db_id = save_video(client_id, meta, segments)
            log.info(f"Video saved (db id={video_db_id}).")

        # ── Step 2: check if clips already exist ─────────────────────────────
        existing_clips = supabase.table("clips").select("id").eq("video_id", video_db_id).execute()
        if existing_clips.data:
            log.info(f"Clips already exist for this video — skipping clipping.")
            supabase.table("jobs").update({
                "status": "done",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", job_id).execute()
            return

        # ── Step 3: ask Claude for best moments ──────────────────────────────
        log.info("Asking Claude for best moments...")
        moments = ask_claude(segments, title)
        log.info(f"Claude identified {len(moments)} moments.")

        # ── Step 4: download, cut, upload each clip ───────────────────────────
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, moment in enumerate(moments, start=1):
                start = float(moment["start"])
                end = float(moment["end"])
                duration = round(end - start, 1)
                log.info(f"Clip {i}: {start:.1f}s → {end:.1f}s ({duration}s)")

                section_stem = os.path.join(tmpdir, f"section_{i}")
                section_path = download_section(url, start, end, section_stem)

                clip_path = os.path.join(tmpdir, f"clip_{i}.mp4")
                cut_clip(section_path, duration, clip_path)

                storage_path = f"{client_id}/{video_db_id}/clip_{i}.mp4"
                upload_clip(clip_path, storage_path)

                clip_id = save_clip_record(client_id, video_db_id, i, moment, storage_path)
                enqueue_poster_jobs(client_id, video_db_id, clip_id, storage_path, moment["caption"])
                log.info(f"Clip {i} done, poster jobs enqueued.")

        supabase.table("jobs").update({
            "status": "done",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()
        log.info(f"Manual job {job_id} complete.")

    except Exception as exc:
        log.error(f"Manual job {job_id} failed: {exc}", exc_info=True)
        supabase.table("jobs").update({
            "status": "failed",
            "error": str(exc)[:2000],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(slot: str) -> None:
    log.info(f"Manual Processor {slot.upper()} — {datetime.now(timezone.utc).isoformat()}")

    clients = supabase.table("clients").select("*").eq("active", True).execute().data
    if not clients:
        log.info("No active clients, exiting.")
        return
    client_id = clients[0]["id"]

    jobs = (
        supabase.table("jobs").select("*")
        .eq("job_type", "watch_manual")
        .eq("status", "pending")
        .order("created_at")
        .execute().data
    )

    log.info(f"{len(jobs)} manual job(s) pending.")
    if not jobs:
        return

    for job in jobs:
        if not job.get("client_id"):
            supabase.table("jobs").update({"client_id": client_id}).eq("id", job["id"]).execute()
            job["client_id"] = client_id
        process_manual_job(job)

    log.info("Done.")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("a", "b"):
        print("Usage: python3 process_video.py a|b")
        sys.exit(1)
    main(sys.argv[1])
