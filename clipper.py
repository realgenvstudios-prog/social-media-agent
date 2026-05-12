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
SESSION_BUCKET = "sessions"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_whisper: WhisperModel | None = None
_cookie_file: str | None = None


def get_cookie_file() -> str | None:
    """Download youtube_cookies.txt from Supabase Storage once per process."""
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
        _cookie_file = ""  # empty string = no cookies, don't retry
    return _cookie_file or None


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
You are a viral short-form content strategist for an African podcast.
Your job is to find moments that make viewers stop scrolling, watch to the end, rewatch, save, and share on TikTok, YouTube Shorts, and Facebook Reels.

AUDIENCE: Ambitious Africans aged 18–35, primarily West African. They are frustrated with barriers to wealth, proud of their identity, hungry for real talk about money and success, and respond strongly to content that validates their experience or challenges them to think differently.

WHAT STOPS A SCROLL (priority order):
1. Open loop — an incomplete thought or bold claim in the first 3 seconds that the viewer must stay to resolve
2. Pattern interrupt — an unexpected emotion, revelation, or stat that breaks autopilot scrolling
3. Identity resonance — "this is about me / my people / my reality"
4. Forbidden knowledge — a truth that feels like it is not supposed to be public
5. Social currency — something worth sharing because sharing it makes you look informed or culturally aware

MOMENTS THAT GO VIRAL IN THIS NICHE:
- Contrarian takes that directly contradict popular belief about money, Africa, or success
- Specific numbers: "I made X in Y months" or "X% of Africans..." — concrete data hits harder than vague claims
- Validation moments: articulating a frustration the viewer has always felt but never heard said this clearly
- Life-changing turning points: the exact moment a guest's mindset or trajectory shifted and why
- Uncomfortable truths the audience will recognise about themselves
- African identity moments: content that makes the audience feel proud, seen, or respectfully called out as a community
- Disagreement or pushback between host and guest — tension keeps viewers watching

CLIP STRUCTURE FOR MAXIMUM RETENTION:
- 0–3s  HOOK:    Open loop or emotional spike. Ideally starts mid-claim, mid-story, or with a bold statement
- 3–35s BUILD:   Raise the stakes, add context, increase tension
- 35–55s PEAK:   The most powerful, surprising, or emotional moment in the clip
- 55–75s LANDING: One memorable closing line — something quotable that people will repeat

EMOTIONS THAT DRIVE SHARES AND SAVES:
Awe ("I didn't know that was possible"), Validation ("finally someone said it"),
Aspiration ("I could do this"), Curiosity ("wait — say more"), Cultural identity ("this is so us"),
Constructive outrage ("this is wrong and we need to talk about it")

WHAT TO AVOID:
- Clips that only make sense if the viewer has watched the full episode
- Sections that are pure background or setup with no emotional tension
- Slow starts — if the first sentence is not compelling, the clip is wrong
- Vague motivational language without a specific story or stat behind it

CAPTION RULES:
- First line must work as a standalone hook — viewers see it before the video plays on some platforms
- Trigger exactly ONE primary emotion per caption
- End with a question or soft CTA ("save this", "share with someone who needs to hear this", "what do you think?")
- Mix hashtags: 2 high-volume (#Africa #Wealth #Podcast) + 2–3 niche (#KonnectedMinds #AfricanEntrepreneur #GhanaTwitter)
- Keep the caption line under 150 characters; hashtags go on a separate line

Return ONLY a valid JSON array with exactly 10 objects, no extra text:
[
  {
    "start": 123.4,
    "end": 187.2,
    "hook": "one sentence explaining what makes this clip stop a scroll",
    "caption": "first-line hook under 150 chars\\n\\n#KonnectedMinds #Africa #Wealth"
  }
]"""


def ask_claude_for_moments(transcript_segments: list[dict], video_title: str) -> list[dict]:
    lines = []
    for seg in transcript_segments:
        minutes = int(seg["start"] // 60)
        seconds = int(seg["start"] % 60)
        lines.append(f"[{minutes:02d}:{seconds:02d}] {seg['text']}")
    transcript_text = "\n".join(lines)

    log.info(f"Sending {len(transcript_segments)} segments (~{len(transcript_text.split())} words) to Claude Sonnet...")

    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Video title: {video_title}\n\nTranscript:\n{transcript_text}"
        }],
    )

    raw = message.content[0].text.strip()
    log.info(f"Claude responded. Input tokens: {message.usage.input_tokens}, Output tokens: {message.usage.output_tokens}")

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    moments = json.loads(raw)
    if not isinstance(moments, list) or len(moments) < 5:
        raise ValueError(f"Expected 10 moments from Claude, got {len(moments) if isinstance(moments, list) else 0}: {raw[:200]}")
    return moments


# ---------------------------------------------------------------------------
# Video download + clip cutting
# ---------------------------------------------------------------------------

def _format_ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _extract_words_for_clip(segments: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    """Return word-level timestamps within the clip, offset to clip-relative time."""
    words = []
    for seg in segments:
        for w in seg.get("words", []):
            if w["start"] >= clip_start - 0.1 and w["end"] <= clip_end + 0.1:
                words.append({
                    "start": round(w["start"] - clip_start, 2),
                    "end": round(w["end"] - clip_start, 2),
                    "word": w["word"],
                })
    return words


def _write_ass_subtitles(words: list[dict], output_path: str) -> None:
    """Generate a TikTok-style word-by-word ASS subtitle file (3 words per chunk)."""
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,54,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "1,0,0,0,100,100,1,0,1,3,1,2,10,10,55,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [header]
    chunk_size = 3
    for i in range(0, len(words), chunk_size):
        chunk = words[i : i + chunk_size]
        start = _format_ass_time(chunk[0]["start"])
        end = _format_ass_time(chunk[-1]["end"])
        text = "  ".join(w["word"].upper() for w in chunk)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def download_video_section(video_url: str, start: float, end: float, output_path: str) -> None:
    """Download only the needed section of the video using yt-dlp."""
    ydl_opts = _ydl_opts({
        "format": "bestvideo[height<=720]+bestaudio/bestvideo+bestaudio/best[height<=720]/best",
        "merge_output_format": "mp4",
        "outtmpl": output_path,
        "retries": 10,
        "fragment_retries": 10,
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
    })
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])


def cut_clip(input_path: str, start: float, end: float, output_path: str, words: list[dict] | None = None) -> None:
    """Re-encode a clip and optionally burn word-by-word subtitles."""
    duration = end - start

    ass_path = output_path.replace(".mp4", ".ass")
    use_subs = bool(words)
    if use_subs:
        _write_ass_subtitles(words, ass_path)

    cmd = ["ffmpeg", "-y", "-i", input_path, "-t", str(duration)]
    if use_subs:
        cmd += ["-vf", f"ass={ass_path}"]
    cmd += [
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if use_subs and os.path.exists(ass_path):
        os.remove(ass_path)
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

                # 3. Re-encode for clean cut with word-by-word subtitles
                clip_filename = f"clip_{i}.mp4"
                clip_path = os.path.join(tmpdir, clip_filename)
                log.info(f"Clip {i}: Cutting with FFmpeg + subtitles...")
                clip_words = _extract_words_for_clip(segments, start, end)
                cut_clip(section_path, 0, duration, clip_path, words=clip_words or None)

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
# Top-up: find more viral moments when the posting queue runs low
# ---------------------------------------------------------------------------

def _ask_claude_for_more_moments(segments: list[dict], title: str, existing_ranges: list[tuple]) -> list[dict]:
    """Ask Claude for additional moments, explicitly excluding already-clipped ranges."""
    lines = []
    for seg in segments:
        minutes = int(seg["start"] // 60)
        seconds = int(seg["start"] % 60)
        lines.append(f"[{minutes:02d}:{seconds:02d}] {seg['text']}")
    transcript_text = "\n".join(lines)

    excluded = "\n".join(f"- {s:.0f}s to {e:.0f}s" for s, e in existing_ranges)
    user_content = (
        f"Video title: {title}\n\n"
        f"Already clipped sections — DO NOT overlap with these:\n{excluded}\n\n"
        f"Find 3–5 additional viral moments from different parts of the video.\n\n"
        f"Transcript:\n{transcript_text}"
    )

    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    moments = json.loads(raw)
    return moments if isinstance(moments, list) else []


def run_topup(slot: str) -> None:
    """Top up the clip queue when it drops below 6 unposted clips."""
    pending_count = len(
        supabase.table("jobs").select("id")
        .eq("job_type", f"post_{slot}").eq("status", "pending")
        .execute().data
    )
    if pending_count >= 6:
        return

    log.info(f"Queue has {pending_count} pending post jobs — running top-up.")

    videos = supabase.table("videos").select("id, title, url, transcript_segments, client_id").execute().data
    if not videos:
        log.info("No videos available for top-up.")
        return

    for video in videos:
        video_id = video["id"]
        existing_clips = (
            supabase.table("clips").select("start_seconds, end_seconds, client_id")
            .eq("video_id", video_id).execute().data
        )
        if len(existing_clips) >= 20:
            continue

        existing_ranges = [(c["start_seconds"], c["end_seconds"]) for c in existing_clips]
        client_id = (existing_clips[0]["client_id"] if existing_clips else video.get("client_id"))
        if not client_id:
            continue

        segments = json.loads(video["transcript_segments"])
        log.info(f"Top-up: finding more moments from '{video['title']}'...")

        try:
            new_moments = _ask_claude_for_more_moments(segments, video["title"], existing_ranges)
        except Exception as e:
            log.warning(f"Top-up Claude call failed: {e}")
            continue

        if not new_moments:
            continue

        url = video["url"]
        start_index = len(existing_clips) + 1

        with tempfile.TemporaryDirectory() as tmpdir:
            for i, moment in enumerate(new_moments, start=start_index):
                start = float(moment["start"])
                end = float(moment["end"])
                duration = round(end - start, 1)
                log.info(f"Top-up clip {i}: {start:.1f}s → {end:.1f}s")
                try:
                    section_stem = os.path.join(tmpdir, f"topup_{i}")
                    section_path = download_video_section(url, start, end, section_stem)
                    candidates = list(Path(tmpdir).glob(f"topup_{i}*"))
                    if not candidates:
                        raise FileNotFoundError(f"No file for top-up clip {i}")
                    section_path = str(candidates[0])

                    clip_path = os.path.join(tmpdir, f"topup_clip_{i}.mp4")
                    clip_words = _extract_words_for_clip(segments, start, end)
                    cut_clip(section_path, 0, duration, clip_path, words=clip_words or None)

                    storage_path = f"{client_id}/{video_id}/clip_{i}.mp4"
                    upload_clip(clip_path, storage_path)

                    clip_id = save_clip(client_id, video_id, i, moment, storage_path)
                    enqueue_poster_jobs(client_id, video_id, clip_id, storage_path, moment.get("caption", ""))
                    log.info(f"Top-up clip {i} done.")
                except Exception as e:
                    log.error(f"Top-up clip {i} failed: {e}", exc_info=True)

        new_pending = len(
            supabase.table("jobs").select("id")
            .eq("job_type", f"post_{slot}").eq("status", "pending")
            .execute().data
        )
        if new_pending >= 6:
            log.info(f"Queue now at {new_pending} — top-up complete.")
            break


# ---------------------------------------------------------------------------
# Manual job pipeline (watch_manual jobs submitted via dashboard)
# ---------------------------------------------------------------------------

def _ydl_opts(extra: dict = {}) -> dict:
    opts = {"quiet": True, "no_warnings": True, **({"cookiefile": get_cookie_file()} if get_cookie_file() else {})}
    opts.update(extra)
    return opts


def _fetch_metadata(url: str) -> dict:
    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "id": info["id"], "title": info.get("title", ""), "url": url,
        "duration": info.get("duration"), "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"), "thumbnail": info.get("thumbnail"),
        "description": info.get("description", ""), "like_count": info.get("like_count"),
        "channel": info.get("channel", ""),
    }


def _download_audio(url: str, stem: str) -> str:
    ydl_opts = _ydl_opts({
        "format": "bestaudio/best", "outtmpl": stem, "retries": 10, "fragment_retries": 10,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
    })
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    path = stem + ".mp3"
    if not os.path.exists(path):
        raise FileNotFoundError(f"Audio download failed — expected {path}")
    return path


def _transcribe(audio_path: str) -> list[dict]:
    model = get_whisper()
    segments, _ = model.transcribe(audio_path, beam_size=5, word_timestamps=True)
    result = []
    for seg in segments:
        if not seg.text.strip():
            continue
        words = [
            {"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word.strip()}
            for w in (seg.words or []) if w.word.strip()
        ]
        result.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip(), "words": words})
    return result


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

def main(slot: str = "a") -> None:
    log.info(f"Clipper starting — {datetime.now(timezone.utc).isoformat()}")

    # 1. Handle dashboard-submitted manual video requests first
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

    # 2. Process regular clip jobs
    jobs = get_pending_clip_jobs()
    log.info(f"{len(jobs)} pending clip job(s).")

    for job in jobs:
        try:
            process_job(job)
        except Exception:
            pass  # already logged and marked failed, continue to next job

    # 3. Top up the posting queue if it's running low
    try:
        run_topup(slot)
    except Exception as e:
        log.error(f"Top-up failed: {e}", exc_info=True)

    log.info("Clipper run complete.")


if __name__ == "__main__":
    import sys
    slot = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("a", "b") else "a"
    main(slot)
