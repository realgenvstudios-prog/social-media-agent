"""
Poster Agent — run as:
  python3 poster.py a
  python3 poster.py b

Claude Haiku handles only the tricky visual questions (where's the caption box,
where's the post button). Playwright handles everything else with proper waits.
~3 Claude calls per platform = cheap and reliable.
"""

import os
import sys
import json
import time
import base64
import random
import tempfile
import logging
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, BrowserContext, Page
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

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

PLATFORMS = ["tiktok", "youtube", "facebook"]


# ---------------------------------------------------------------------------
# Targeted Claude vision — Haiku, ~3 calls per platform (cheap)
# ---------------------------------------------------------------------------

def ask(page: Page, question: str, extra_context: str = "") -> dict:
    """
    Ask Claude Haiku one specific visual question about the current page.
    Returns: { "found": bool, "x": int, "y": int, "answer": str }
    """
    img = base64.standard_b64encode(page.screenshot()).decode()
    prompt = f"""You are controlling a browser at 1280x800 resolution.
{extra_context}

Question: {question}

Reply ONLY with JSON (no markdown):
{{
  "found": true or false,
  "x": pixel x coordinate (center of element, 0 if not found),
  "y": pixel y coordinate (center of element, 0 if not found),
  "answer": "brief description of what you see"
}}"""

    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    raw = resp.content[0].text.strip().strip("```").strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()
    result = json.loads(raw)
    log.info(f"[claude] {question[:60]}… → {result['answer']}")
    return result


def click_at(page: Page, x: int, y: int, label: str = "") -> None:
    log.info(f"Clicking {label} at ({x}, {y})")
    page.mouse.click(x, y)
    time.sleep(1.5)


def wait_for_upload(page: Page, platform: str, timeout: int = 180) -> None:
    """Poll until Claude confirms the upload progress bar is gone / editor is ready."""
    log.info(f"[{platform}] Waiting for upload to finish (up to {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        result = ask(
            page,
            "Is there still an upload progress bar or 'uploading' spinner visible?",
            f"I just uploaded a video to {platform}.",
        )
        if not result["found"]:
            log.info(f"[{platform}] Upload complete.")
            return
        time.sleep(8)
    log.warning(f"[{platform}] Upload wait timed out — proceeding anyway.")


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------

def post_tiktok(context: BrowserContext, slot: str, video_path: str, caption: str) -> str:
    page = context.new_page()
    try:
        log.info(f"[{slot}/tiktok] Navigating to upload page...")
        page.goto("https://www.tiktok.com/upload", wait_until="domcontentloaded")
        time.sleep(4)

        # Set file — TikTok wraps the input inside an iframe
        uploaded = False
        for frame in page.frames:
            try:
                fi = frame.locator('input[type="file"]')
                if fi.count() > 0:
                    fi.first.set_input_files(video_path)
                    uploaded = True
                    log.info(f"[{slot}/tiktok] File set via iframe.")
                    break
            except Exception:
                continue
        if not uploaded:
            page.locator('input[type="file"]').first.set_input_files(video_path)
            log.info(f"[{slot}/tiktok] File set via main page.")

        # Wait for TikTok to process the video
        time.sleep(15)
        wait_for_upload(page, "TikTok", timeout=120)
        time.sleep(3)

        # Find caption input
        r = ask(page, "Where is the text input box for the video caption or description?", "TikTok upload page, video is processed.")
        if not r["found"]:
            raise RuntimeError("Could not find TikTok caption input")
        click_at(page, r["x"], r["y"], "caption box")
        page.keyboard.press("Control+a")
        time.sleep(0.3)
        page.keyboard.type(caption[:150], delay=70)
        time.sleep(2)

        # Find Post button
        r = ask(page, "Where is the Post or Publish button to submit the video?")
        if not r["found"]:
            raise RuntimeError("Could not find TikTok Post button")
        click_at(page, r["x"], r["y"], "Post button")
        time.sleep(15)

        # Verify
        r = ask(page, "Has the video been posted successfully? Look for a success message, redirect to your profile, or confirmation screen.")
        log.info(f"[{slot}/tiktok] Result: {r['answer']}")
        return "https://www.tiktok.com"
    finally:
        page.close()


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def post_youtube(context: BrowserContext, slot: str, video_path: str, caption: str) -> str:
    page = context.new_page()
    try:
        log.info(f"[{slot}/youtube] Navigating to YouTube Studio...")
        page.goto("https://studio.youtube.com", wait_until="domcontentloaded")
        time.sleep(4)

        # Click CREATE button
        r = ask(page, "Where is the CREATE button or upload button in the top area of YouTube Studio?")
        if not r["found"]:
            raise RuntimeError("Could not find YouTube CREATE button")
        click_at(page, r["x"], r["y"], "CREATE button")
        time.sleep(2)

        # Click Upload videos
        r = ask(page, "Where is the 'Upload videos' option in the dropdown menu?")
        if not r["found"]:
            raise RuntimeError("Could not find Upload videos option")
        click_at(page, r["x"], r["y"], "Upload videos")
        time.sleep(2)

        # Set file
        page.locator('input[type="file"]').first.set_input_files(video_path)
        log.info(f"[{slot}/youtube] File set. Waiting for upload...")
        time.sleep(10)
        wait_for_upload(page, "YouTube", timeout=180)
        time.sleep(3)

        # Fill title
        r = ask(page, "Where is the title input field for the video?", "YouTube upload dialog is open.")
        if r["found"]:
            click_at(page, r["x"], r["y"], "title field")
            page.keyboard.press("Control+a")
            page.keyboard.type(caption[:90], delay=60)
            time.sleep(1)

        # Click Next x3
        for i in range(3):
            r = ask(page, "Where is the Next button to proceed to the next step?")
            if r["found"]:
                click_at(page, r["x"], r["y"], f"Next ({i+1})")
                time.sleep(2)

        # Set Public
        r = ask(page, "Where is the Public visibility option or radio button?")
        if r["found"]:
            click_at(page, r["x"], r["y"], "Public")
            time.sleep(1)

        # Publish
        r = ask(page, "Where is the Publish or Save button to publish the video?")
        if not r["found"]:
            raise RuntimeError("Could not find YouTube Publish button")
        click_at(page, r["x"], r["y"], "Publish")
        time.sleep(15)

        r = ask(page, "Has the video been published successfully? Look for a confirmation or success message.")
        log.info(f"[{slot}/youtube] Result: {r['answer']}")
        return "https://studio.youtube.com"
    finally:
        page.close()


# ---------------------------------------------------------------------------
# Facebook
# ---------------------------------------------------------------------------

def post_facebook(context: BrowserContext, slot: str, video_path: str, caption: str) -> str:
    page = context.new_page()
    try:
        log.info(f"[{slot}/facebook] Navigating to Facebook Reels creator...")
        page.goto("https://www.facebook.com/reels/create", wait_until="domcontentloaded")
        time.sleep(5)

        # If redirected away, try the video composer via home page
        if "reels/create" not in page.url:
            page.goto("https://www.facebook.com", wait_until="domcontentloaded")
            time.sleep(3)
            r = ask(page, "Where is the Photo/video or Reel option to create a new video post?")
            if r["found"]:
                click_at(page, r["x"], r["y"], "video post option")
                time.sleep(2)

        # Set file
        try:
            fi = page.locator('input[type="file"]').first
            fi.set_input_files(video_path)
        except Exception:
            page.evaluate('document.querySelector(\'input[type="file"]\').style.display="block"')
            page.locator('input[type="file"]').first.set_input_files(video_path)

        log.info(f"[{slot}/facebook] File set. Waiting for upload...")
        time.sleep(15)
        wait_for_upload(page, "Facebook", timeout=120)
        time.sleep(3)

        # Find caption area
        r = ask(page, "Where is the caption or description text input for the video?", "Facebook video upload page.")
        if r["found"]:
            click_at(page, r["x"], r["y"], "caption")
            page.keyboard.type(caption[:200], delay=70)
            time.sleep(2)

        # Post / Share
        r = ask(page, "Where is the Share or Publish button to post this video?")
        if not r["found"]:
            raise RuntimeError("Could not find Facebook Share button")
        click_at(page, r["x"], r["y"], "Share button")
        time.sleep(15)

        r = ask(page, "Was the video posted successfully? Look for a confirmation, redirect, or success state.")
        log.info(f"[{slot}/facebook] Result: {r['answer']}")
        return "https://www.facebook.com"
    finally:
        page.close()


PLATFORM_FNS = {
    "tiktok": post_tiktok,
    "youtube": post_youtube,
    "facebook": post_facebook,
}


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def load_session(context: BrowserContext, slot: str, platform: str) -> None:
    try:
        data = supabase.storage.from_(SESSION_BUCKET).download(f"sessions/{slot}/{platform}.json")
        context.add_cookies(json.loads(data))
        log.info(f"[{slot}/{platform}] Session loaded.")
    except Exception:
        log.warning(f"[{slot}/{platform}] No saved session found.")


def save_session(context: BrowserContext, slot: str, platform: str) -> None:
    try:
        supabase.storage.from_(SESSION_BUCKET).upload(
            path=f"sessions/{slot}/{platform}.json",
            file=json.dumps(context.cookies()).encode(),
            file_options={"content-type": "application/json", "upsert": "true"},
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Supabase job helpers
# ---------------------------------------------------------------------------

def get_creds(slot: str, platform: str) -> dict:
    p = f"POSTER_{slot.upper()}_{platform.upper()}"
    return {"email": os.environ.get(f"{p}_EMAIL", ""), "password": os.environ.get(f"{p}_PASSWORD", "")}


def get_or_create_post_record(clip_id: str, slot: str, platform: str) -> str:
    r = supabase.table("posts").select("id").eq("clip_id", clip_id).eq("poster_slot", slot).eq("platform", platform).execute()
    if r.data:
        return r.data[0]["id"]
    return supabase.table("posts").insert({
        "clip_id": clip_id, "poster_slot": slot, "platform": platform,
        "status": "pending", "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute().data[0]["id"]


def mark_post(post_id: str, status: str, post_url: str = None, error: str = None) -> None:
    supabase.table("posts").update({
        "status": status,
        **({"post_url": post_url} if post_url else {}),
        **({"error": error[:1000]} if error else {}),
    }).eq("id", post_id).execute()


def download_clip(storage_path: str, local_path: str) -> None:
    with open(local_path, "wb") as f:
        f.write(supabase.storage.from_(CLIP_BUCKET).download(storage_path))


def all_done(clip_id: str, slot: str) -> bool:
    r = supabase.table("posts").select("status").eq("clip_id", clip_id).eq("poster_slot", slot).execute()
    return len([x for x in r.data if x["status"] == "posted"]) >= len(PLATFORMS)


# ---------------------------------------------------------------------------
# Process one job
# ---------------------------------------------------------------------------

def process_job(job: dict, slot: str, context: BrowserContext) -> None:
    job_id = job["id"]
    payload = json.loads(job["payload"])
    clip_id, storage_path, caption = payload["clip_id"], payload["storage_path"], payload.get("caption", "")

    log.info(f"[{slot}] Job {job_id}")
    supabase.table("jobs").update({"status": "running", "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", job_id).execute()

    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, "clip.mp4")
        download_clip(storage_path, video_path)
        log.info(f"[{slot}] Clip ready: {os.path.getsize(video_path)/1e6:.1f} MB")

        all_ok = True
        for platform in PLATFORMS:
            creds = get_creds(slot, platform)
            if not creds["email"]:
                log.info(f"[{slot}/{platform}] No credentials, skipping.")
                continue

            post_id = get_or_create_post_record(clip_id, slot, platform)
            try:
                url = PLATFORM_FNS[platform](context, slot, video_path, caption)
                mark_post(post_id, "posted", post_url=url)
            except Exception as exc:
                log.error(f"[{slot}/{platform}] Failed: {exc}")
                mark_post(post_id, "failed", error=str(exc))
                all_ok = False
            time.sleep(random.uniform(4, 8))

    supabase.table("jobs").update({
        "status": "done" if all_ok else "failed",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

    if all_done(clip_id, slot):
        try:
            supabase.storage.from_(CLIP_BUCKET).remove([storage_path])
            log.info(f"Clip deleted from storage.")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(slot: str) -> None:
    log.info(f"Poster {slot.upper()} starting — {datetime.now(timezone.utc).isoformat()}")
    jobs = supabase.table("jobs").select("*").eq("job_type", f"post_{slot}").eq("status", "pending").order("created_at").execute().data
    log.info(f"{len(jobs)} pending job(s).")
    if not jobs:
        return

    headless = os.environ.get("HEADLESS", "false").lower() == "true"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )

        for platform in PLATFORMS:
            load_session(context, slot, platform)

        for job in jobs:
            try:
                process_job(job, slot, context)
            except Exception as exc:
                log.error(f"Job {job['id']} crashed: {exc}", exc_info=True)
                supabase.table("jobs").update({
                    "status": "failed", "error": str(exc)[:1000],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", job["id"]).execute()

        for platform in PLATFORMS:
            save_session(context, slot, platform)

        browser.close()

    log.info(f"Poster {slot.upper()} done.")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("a", "b"):
        print("Usage: python3 poster.py a|b")
        sys.exit(1)
    main(sys.argv[1])
