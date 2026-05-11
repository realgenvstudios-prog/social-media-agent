"""
Clipper Agent — polls for pending clip jobs.
For each job: fetches transcript, asks Claude Haiku for the 3 best short-form
moments, downloads only those video sections, uploads clips to Supabase Storage,
enqueues post_a and post_b jobs.
"""

import os
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
# Supabase helpers
# ---------------------------------------------------------------------------

def claim_job(job_id: str) -> bool:
    """Mark job as running. Returns False if already claimed by another process."""
    result = (
        supabase.table("jobs")
        .update({"status": "running", "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", job_id)
        .eq("status", "pending")
        .execute()
    )
    return len(result.data) > 0


def fail_job(job_id: str, error: str) -> None:
    supabase.table("jobs").update({
        "status": "failed",
        "error": error[:2000],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()


def complete_job(job_id: str) -> None:
    supabase.table("jobs").update({
        "status": "done",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()


def get_pending_clip_jobs() -> list[dict]:
    result = (
        supabase.table("jobs")
        .select("*")
        .eq("job_type", "clip")
        .eq("status", "pending")
        .order("created_at")
        .execute()
    )
    return result.data


def get_video(video_id: str) -> dict:
    result = supabase.table("videos").select("*").eq("id", video_id).single().execute()
    return result.data


def save_clip(client_id: str, video_id: str, index: int, moment: dict, storage_path: str) -> str:
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
# Claude — find best moments
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
    "caption": "engaging caption under 150 chars with 3–5 relevant hashtags"
  }
]"""


def ask_claude_for_moments(transcript_segments: list[dict], video_title: str) -> list[dict]:
    # Cap at 1000 segments to keep payload under ~8k tokens
    transcript_segments = transcript_segments[:1000]

    # Build a compact transcript string: [MM:SS] text
    lines = []
    for seg in transcript_segments:
        minutes = int(seg["start"] // 60)
        seconds = int(seg["start"] % 60)
        lines.append(f"[{minutes:02d}:{seconds:02d}] {seg['text']}")
    transcript_text = "\n".join(lines)

    log.info(f"Sending {len(transcript_segments)} segments (~{len(transcript_text.split())} words) to Claude Haiku...")

    message = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Video title: {video_title}\n\nTranscript:\n{transcript_text}"
        }],
    )

    raw = message.content[0].text.strip()
    log.info(f"Claude responded. Input tokens: {message.usage.input_tokens}, Output tokens: {message.usage.output_tokens}")

    # Strip markdown code fences if Claude wraps in them
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    moments = json.loads(raw)
    if not isinstance(moments, list) or len(moments) != 3:
        raise ValueError(f"Expected 3 moments from Claude, got: {raw}")
    return moments


# ---------------------------------------------------------------------------
# Video download + clip cutting
# ---------------------------------------------------------------------------

def download_video_section(video_url: str, start: float, end: float, output_path: str) -> None:
    """Download only the needed section of the video using yt-dlp."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
        "merge_output_format": "mp4",
        "outtmpl": output_path,
        "retries": 10,
        "fragment_retries": 10,
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])


def cut_clip(input_path: str, start: float, end: float, output_path: str) -> None:
    """Re-encode a precise clip from an already-downloaded video section."""
    duration = end - start
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-t", str(duration),
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr[-500:]}")


# ---------------------------------------------------------------------------
# Supabase Storage upload
# ---------------------------------------------------------------------------

def upload_clip(local_path: str, storage_path: str) -> None:
    with open(local_path, "rb") as f:
        data = f.read()
    supabase.storage.from_(CLIP_BUCKET).upload(
        path=storage_path,
        file=data,
        file_options={"content-type": "video/mp4", "upsert": "true"},
    )
    log.info(f"Uploaded to storage: {storage_path}")


# ---------------------------------------------------------------------------
# Per-job logic
# ---------------------------------------------------------------------------

def process_job(job: dict, already_claimed: bool = False) -> None:
    job_id = job["id"]
    client_id = job["client_id"]
    video_id = job["video_id"]

    if not already_claimed and not claim_job(job_id):
        log.info(f"Job {job_id} already claimed, skipping.")
        return

    log.info(f"Processing clip job {job_id}")

    try:
        video = get_video(video_id)
        title = video["title"]
        url = video["url"]
        segments = json.loads(video["transcript_segments"])

        # 1. Ask Claude for the 3 best moments
        moments = ask_claude_for_moments(segments, title)
        log.info(f"Claude identified {len(moments)} moments.")

        with tempfile.TemporaryDirectory() as tmpdir:
            for i, moment in enumerate(moments, start=1):
                start = float(moment["start"])
                end = float(moment["end"])
                duration = round(end - start, 1)
                log.info(f"Clip {i}: {start:.1f}s → {end:.1f}s ({duration}s)")

                # 2. Download only this section
                section_stem = os.path.join(tmpdir, f"section_{i}")
                section_path = section_stem + ".mp4"
                log.info(f"Clip {i}: Downloading section from YouTube...")
                download_video_section(url, start, end, section_stem)

                # yt-dlp may produce slightly different filename — find it
                candidates = list(Path(tmpdir).glob(f"section_{i}*"))
                if not candidates:
                    raise FileNotFoundError(f"No downloaded file found for clip {i}")
                section_path = str(candidates[0])

                # 3. Re-encode for clean cut
                clip_filename = f"clip_{i}.mp4"
                clip_path = os.path.join(tmpdir, clip_filename)
                log.info(f"Clip {i}: Cutting with FFmpeg...")
                cut_clip(section_path, 0, duration, clip_path)

                # 4. Upload to Supabase Storage
                storage_path = f"{client_id}/{video_id}/clip_{i}.mp4"
                upload_clip(clip_path, storage_path)

                # 5. Save clip record
                clip_id = save_clip(client_id, video_id, i, moment, storage_path)

                # 6. Enqueue poster jobs
                enqueue_poster_jobs(client_id, video_id, clip_id, storage_path, moment["caption"])
                log.info(f"Clip {i}: Done. Poster jobs enqueued.")

        complete_job(job_id)
        log.info(f"Clip job {job_id} complete.")

    except Exception as exc:
        log.error(f"Clip job {job_id} failed: {exc}", exc_info=True)
        fail_job(job_id, str(exc))
        raise


# ---------------------------------------------------------------------------
# Manual job pipeline (watch_manual jobs submitted via dashboard)
# ---------------------------------------------------------------------------

def _fetch_metadata(url: str) -> dict:
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "id": info["id"], "title": info.get("title", ""), "url": url,
        "duration": info.get("duration"), "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"), "thumbnail": info.get("thumbnail"),
        "description": info.get("description", ""), "like_count": info.get("like_count"),
        "channel": info.get("channel", ""),
    }


def _download_audio(url: str, stem: str) -> str:
    ydl_opts = {
        "quiet": True, "no_warnings": True, "format": "bestaudio/best",
        "outtmpl": stem, "retries": 10, "fragment_retries": 10,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    path = stem + ".mp3"
    if not os.path.exists(path):
        raise FileNotFoundError(f"Audio download failed — expected {path}")
    return path


def _transcribe(audio_path: str) -> list[dict]:
    model = get_whisper()
    segments, _ = model.transcribe(audio_path, beam_size=5)
    return [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments if s.text.strip()]


def _save_video(client_id: str, meta: dict, segments: list[dict]) -> str:
    full_text = " ".join(s["text"] for s in segments)
    result = supabase.table("videos").insert({
        "client_id": client_id, "youtube_video_id": meta["id"],
        "title": meta["title"], "url": meta["url"],
        "duration_seconds": int(meta["duration"]) if meta.get("duration") else None,
        "upload_date": meta.get("upload_date"), "view_count": meta.get("view_count"),
        "thumbnail_url": meta.get("thumbnail"), "description": meta.get("description"),
        "like_count": meta.get("like_count"), "channel_name": meta.get("channel"),
        "transcript_segments": json.dumps(segments), "transcript_text": full_text,
        "status": "transcribed", "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return result.data[0]["id"]


def process_manual_job(job: dict) -> None:
    job_id = job["id"]
    client_id = job["client_id"]
    url = json.loads(job["payload"])["url"]
    log.info(f"Manual job {job_id}: {url}")

    supabase.table("jobs").update({
        "status": "running", "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

    try:
        # Step 1 — transcribe (skip if already done)
        log.info("Fetching video metadata...")
        meta = _fetch_metadata(url)
        existing = supabase.table("videos").select("id, title, transcript_segments").eq("youtube_video_id", meta["id"]).execute()

        if existing.data:
            video_db_id = existing.data[0]["id"]
            title = existing.data[0]["title"]
            segments = json.loads(existing.data[0]["transcript_segments"])
            log.info(f"Already transcribed (id={video_db_id}), skipping download.")
        else:
            title = meta["title"]
            log.info(f"Downloading audio: {title}")
            with tempfile.TemporaryDirectory() as tmpdir:
                audio_path = _download_audio(url, os.path.join(tmpdir, "audio"))
                log.info(f"Audio ready ({os.path.getsize(audio_path)/1e6:.1f} MB). Transcribing...")
                segments = _transcribe(audio_path)
            log.info(f"Transcribed {len(segments)} segments.")
            video_db_id = _save_video(client_id, meta, segments)
            log.info(f"Video saved (id={video_db_id}).")

        # Step 2 — skip clipping if clips already exist
        if supabase.table("clips").select("id").eq("video_id", video_db_id).execute().data:
            log.info("Clips already exist — skipping clipping.")
            supabase.table("jobs").update({"status": "done", "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", job_id).execute()
            return

        # Step 3 — clip (reuse existing clipper logic)
        fake_job = {"id": job_id, "client_id": client_id, "video_id": video_db_id}
        supabase.table("jobs").update({"video_id": video_db_id}).eq("id", job_id).execute()
        process_job(fake_job, already_claimed=True)

    except Exception as exc:
        log.error(f"Manual job {job_id} failed: {exc}", exc_info=True)
        fail_job(job_id, str(exc))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info(f"Clipper starting — {datetime.now(timezone.utc).isoformat()}")

    # Handle any dashboard-submitted manual video requests first
    manual_jobs = (
        supabase.table("jobs").select("*")
        .eq("job_type", "watch_manual").eq("status", "pending")
        .order("created_at").execute().data
    )
    if manual_jobs:
        log.info(f"{len(manual_jobs)} manual job(s) to process.")
        for job in manual_jobs:
            try:
                process_manual_job(job)
            except Exception:
                pass

    jobs = get_pending_clip_jobs()
    log.info(f"{len(jobs)} pending clip job(s).")

    for job in jobs:
        try:
            process_job(job)
        except Exception:
            pass  # already logged and marked failed, continue to next job

    log.info("Clipper run complete.")


if __name__ == "__main__":
    main()
