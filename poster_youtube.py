"""
YouTube poster using the official YouTube Data API v3.
No browser automation — OAuth2 refresh token never expires.

First run:  python3 poster_youtube.py a --auth
After that: python3 poster_youtube.py a
"""

import os
import sys
import json
import time
import pickle
import tempfile
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client, Client

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
CLIP_BUCKET = "clips"
SESSION_BUCKET = "sessions"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

CLIENT_SECRET = next(
    (f for f in os.listdir(os.path.dirname(__file__) or ".")
     if f.startswith("client_secret") and f.endswith(".json")),
    None
)
if CLIENT_SECRET:
    CLIENT_SECRET = os.path.join(os.path.dirname(__file__) or ".", CLIENT_SECRET)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------------------------------------------------------------------
# Auth — token stored in Supabase so it works on Railway too
# ---------------------------------------------------------------------------

def load_token(slot: str) -> Credentials | None:
    try:
        data = supabase.storage.from_(SESSION_BUCKET).download(f"sessions/{slot}/youtube_token.pickle")
        return pickle.loads(data)
    except Exception:
        return None


def save_token(slot: str, creds: Credentials) -> None:
    supabase.storage.from_(SESSION_BUCKET).upload(
        path=f"sessions/{slot}/youtube_token.pickle",
        file=pickle.dumps(creds),
        file_options={"content-type": "application/octet-stream", "upsert": "true"},
    )
    log.info("Token saved to Supabase.")


def get_youtube_client(slot: str) -> object:
    creds = load_token(slot)

    if creds and creds.expired and creds.refresh_token:
        log.info("Refreshing expired token...")
        creds.refresh(Request())
        save_token(slot, creds)
    elif not creds or not creds.valid:
        raise RuntimeError(
            f"No valid token for slot {slot}. Run: python3 poster_youtube.py {slot} --auth"
        )

    return build("youtube", "v3", credentials=creds)


def run_auth(slot: str) -> None:
    if not CLIENT_SECRET:
        raise FileNotFoundError("client_secret*.json not found in project directory.")
    log.info("Opening browser for YouTube authorisation...")
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    save_token(slot, creds)
    log.info(f"Auth complete. Token saved for slot {slot}. You can now run without --auth.")


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_to_youtube(youtube, video_path: str, title: str) -> str:
    short_title = title[:87].rstrip() + " #Shorts"
    log.info(f"Uploading: {short_title[:80]}")

    body = {
        "snippet": {
            "title": short_title[:100],
            "description": "#Shorts",
            "tags": ["Shorts"],
            "categoryId": "22",  # People & Blogs
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True, chunksize=1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            log.info(f"Upload progress: {pct}%")

    video_id = response["id"]
    url = f"https://youtu.be/{video_id}"
    log.info(f"Uploaded: {url}")
    return url


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def get_or_create_post_record(clip_id: str, slot: str) -> str:
    r = (
        supabase.table("posts")
        .select("id")
        .eq("clip_id", clip_id)
        .eq("poster_slot", slot)
        .eq("platform", "youtube")
        .execute()
    )
    if r.data:
        return r.data[0]["id"]
    return supabase.table("posts").insert({
        "clip_id": clip_id,
        "poster_slot": slot,
        "platform": "youtube",
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute().data[0]["id"]


def mark_post(post_id: str, status: str, post_url: str = None, error: str = None) -> None:
    supabase.table("posts").update({
        "status": status,
        **({"post_url": post_url} if post_url else {}),
        **({"error": error[:1000]} if error else {}),
    }).eq("id", post_id).execute()


def download_clip(storage_path: str, local_path: str) -> None:
    for attempt in range(4):
        try:
            data = supabase.storage.from_(CLIP_BUCKET).download(storage_path)
            with open(local_path, "wb") as f:
                f.write(data)
            return
        except Exception as e:
            if attempt == 3:
                raise
            log.warning(f"Download attempt {attempt+1} failed ({e}), retrying...")
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(slot: str) -> None:
    log.info(f"YouTube Poster {slot.upper()} — {datetime.now(timezone.utc).isoformat()}")

    youtube = get_youtube_client(slot)

    # Find all post jobs whose clip hasn't been posted to YouTube yet
    all_jobs = (
        supabase.table("jobs").select("*")
        .eq("job_type", f"post_{slot}")
        .in_("status", ["pending", "done", "failed"])
        .order("created_at").execute().data
    )

    jobs = []
    for job in all_jobs:
        payload = json.loads(job["payload"])
        clip_id = payload["clip_id"]
        already = (
            supabase.table("posts").select("id")
            .eq("clip_id", clip_id).eq("poster_slot", slot)
            .eq("platform", "youtube").eq("status", "posted")
            .execute().data
        )
        if not already:
            jobs.append(job)

    log.info(f"{len(jobs)} clip(s) to post on YouTube.")
    if not jobs:
        return

    for job in jobs:
        job_id = job["id"]
        payload = json.loads(job["payload"])
        clip_id = payload["clip_id"]
        storage_path = payload["storage_path"]
        caption = payload.get("caption", "Konnected Minds clip")

        log.info(f"Processing job {job_id}")
        supabase.table("jobs").update({
            "status": "running",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()

        post_id = get_or_create_post_record(clip_id, slot)

        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = os.path.join(tmpdir, "clip.mp4")
            log.info("Downloading clip...")
            download_clip(storage_path, video_path)
            size = os.path.getsize(video_path) / 1e6
            log.info(f"Clip ready: {size:.1f} MB")

            try:
                url = upload_to_youtube(youtube, video_path, caption)
                mark_post(post_id, "posted", post_url=url)
                supabase.table("jobs").update({
                    "status": "done",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", job_id).execute()
                log.info(f"Job {job_id} done.")
            except HttpError as exc:
                log.error(f"YouTube API error: {exc}")
                mark_post(post_id, "failed", error=str(exc))
                supabase.table("jobs").update({
                    "status": "failed",
                    "error": str(exc)[:1000],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", job_id).execute()
            except Exception as exc:
                log.error(f"Job {job_id} failed: {exc}", exc_info=True)
                mark_post(post_id, "failed", error=str(exc))
                supabase.table("jobs").update({
                    "status": "failed",
                    "error": str(exc)[:1000],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", job_id).execute()

        time.sleep(5)

    log.info("Done.")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("a", "b"):
        print("Usage: python3 poster_youtube.py a|b [--auth]")
        sys.exit(1)

    slot = sys.argv[1]

    if "--auth" in sys.argv:
        run_auth(slot)
    else:
        main(slot)
