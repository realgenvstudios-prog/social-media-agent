"""
Facebook Reels poster — Playwright + JS interactions.
Run as: python3 poster_facebook.py a

First login: python3 login.py a facebook
"""

import os
import sys
import json
import time
import tempfile
import logging
import platform
from datetime import datetime, timezone

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
CLIP_BUCKET = "clips"
SESSION_BUCKET = "sessions"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def load_session(context: BrowserContext, slot: str) -> None:
    try:
        data = supabase.storage.from_(SESSION_BUCKET).download(f"sessions/{slot}/facebook.json")
        context.add_cookies(json.loads(data))
        log.info("Facebook session loaded.")
    except Exception:
        log.warning("No saved Facebook session — run: python3 login.py a facebook")


def save_session(context: BrowserContext, slot: str) -> None:
    try:
        supabase.storage.from_(SESSION_BUCKET).upload(
            path=f"sessions/{slot}/facebook.json",
            file=json.dumps(context.cookies()).encode(),
            file_options={"content-type": "application/json", "upsert": "true"},
        )
        log.info("Facebook session saved.")
    except Exception as e:
        log.warning(f"Could not save session: {e}")


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_to_facebook(page: Page, video_path: str, caption: str) -> str:
    log.info("Navigating to Facebook...")
    page.goto("https://www.facebook.com", wait_until="domcontentloaded")
    time.sleep(5)

    if "login" in page.url or "checkpoint" in page.url:
        raise RuntimeError("Facebook session expired. Run: python3 login.py a facebook")

    # Make file input visible and set the file
    log.info("Setting video file...")
    _set_video_file(page, video_path)
    log.info("File set. Waiting for upload to process...")
    time.sleep(10)
    _wait_for_processing(page, timeout=180)
    time.sleep(3)

    # Fill caption
    log.info("Filling caption...")
    _fill_caption(page, caption[:500])
    time.sleep(2)

    # Click Share / Publish / Post
    log.info("Clicking Share...")
    _click_share(page)

    # Wait for confirmation
    log.info("Waiting for post confirmation...")
    deadline = time.time() + 40
    success = False
    while time.time() < deadline:
        result = page.evaluate('''() => {
            const url = window.location.href;
            const txt = document.body.innerText || "";
            return {
                success: (
                    txt.includes("Your reel is") ||
                    txt.includes("Your video is") ||
                    txt.includes("published") ||
                    txt.includes("Posted") ||
                    url.includes("/reel/") ||
                    url.includes("/videos/") ||
                    (url.includes("facebook.com") && !url.includes("reels/create"))
                ),
                url
            };
        }''')
        if result["success"]:
            log.info(f"Post confirmed. URL: {result['url']}")
            success = True
            break
        time.sleep(3)

    if not success:
        if "reels/create" in page.url or "create" in page.url:
            raise RuntimeError("Post may have failed — still on create page after 40s.")
        log.info(f"Post likely succeeded (left create page). URL: {page.url}")

    return f"https://www.facebook.com"


def _set_video_file(page: Page, video_path: str) -> None:
    """Find the file input (may be hidden) and set the video."""
    # Try visible input first
    for attempt in range(3):
        try:
            fi = page.locator('input[type="file"]').first
            fi.set_input_files(video_path)
            return
        except Exception:
            pass
        # Make hidden inputs visible via JS then retry
        page.evaluate('''() => {
            document.querySelectorAll('input[type="file"]').forEach(el => {
                el.style.display = "block";
                el.style.visibility = "visible";
                el.style.opacity = "1";
            });
        }''')
        time.sleep(1)
    raise RuntimeError("Could not find file input on Facebook page.")


def _wait_for_processing(page: Page, timeout: int = 180) -> None:
    """Wait until Facebook finishes processing the uploaded video."""
    log.info(f"Waiting for video processing (up to {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        still_processing = page.evaluate('''() => {
            const txt = document.body.innerText || "";
            return (
                txt.includes("Uploading") ||
                txt.includes("Processing") ||
                txt.includes("preparing") ||
                !!document.querySelector('[role="progressbar"]')
            );
        }''')
        if not still_processing:
            log.info("Processing complete.")
            return
        time.sleep(5)
    log.warning("Processing wait timed out — proceeding anyway.")


def _fill_caption(page: Page, text: str) -> None:
    """Fill the caption field — scoped to the Create post dialog."""
    # Wait for the dialog to be present
    try:
        page.locator('[role="dialog"]').wait_for(state="visible", timeout=15000)
    except Exception:
        log.warning("Dialog not found — trying anyway.")

    # Scoped inside the dialog to avoid hitting search bar or news feed inputs
    selectors = [
        '[role="dialog"] [contenteditable][aria-label*="mind"]',
        '[role="dialog"] [contenteditable][aria-label*="Adjiano"]',
        '[role="dialog"] [contenteditable][aria-label*="caption"]',
        '[role="dialog"] [contenteditable="true"]',
        '[contenteditable="true"]',
    ]
    box = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                box = loc
                log.info(f"Caption field found via: {sel}")
                break
        except Exception:
            continue

    if box is None:
        log.warning("Could not find caption field.")
        return

    try:
        box.click(timeout=5000)
    except Exception as e:
        log.warning(f"Click on caption field failed: {e}")
        return

    time.sleep(0.5)
    page.keyboard.press("Control+a")
    time.sleep(0.2)
    page.keyboard.type(text, delay=30)
    log.info("Caption typed.")
    time.sleep(1)


def _click_share(page: Page) -> None:
    """Wait for Post button to be enabled then click it — scoped to dialog."""
    log.info("Waiting for Post button to become enabled...")
    post_selectors = [
        '[role="dialog"] div[aria-label="Post"]',
        '[role="dialog"] div[role="button"]:has-text("Post")',
        '[role="dialog"] button:has-text("Post")',
        '[role="dialog"] div[aria-label="Share"]',
        'div[aria-label="Post"]',
        'div[role="button"]:has-text("Post")',
        'button:has-text("Post")',
    ]
    deadline = time.time() + 30
    while time.time() < deadline:
        for selector in post_selectors:
            try:
                btn = page.locator(selector).first
                if btn.count() == 0:
                    continue
                aria_disabled = btn.get_attribute("aria-disabled")
                if aria_disabled == "true":
                    log.info("Post button found but disabled, waiting...")
                    break  # found but disabled — wait
                btn.click(timeout=5000)
                log.info(f"Post button clicked via: {selector}")
                return
            except Exception:
                continue
        time.sleep(1)

    log.warning("Could not click Post button.")


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def get_or_create_post_record(clip_id: str, slot: str) -> str:
    r = (
        supabase.table("posts").select("id")
        .eq("clip_id", clip_id).eq("poster_slot", slot).eq("platform", "facebook")
        .execute()
    )
    if r.data:
        return r.data[0]["id"]
    return supabase.table("posts").insert({
        "clip_id": clip_id, "poster_slot": slot, "platform": "facebook",
        "status": "pending", "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute().data[0]["id"]


def mark_post(post_id: str, status: str, error: str = None) -> None:
    supabase.table("posts").update({
        "status": status,
        "post_url": "https://www.facebook.com" if status == "posted" else None,
        **({"error": error[:1000]} if error else {}),
    }).eq("id", post_id).execute()


def _mark_posted_with_retry(post_id: str) -> None:
    for attempt in range(8):
        try:
            mark_post(post_id, "posted")
            return
        except Exception as e:
            if attempt < 7:
                wait = min(2 ** attempt * 2, 30)
                log.warning(f"mark_post attempt {attempt+1} failed ({e}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                log.critical(f"Upload succeeded but could not update DB after 8 attempts: {e}")


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
    log.info(f"Facebook Poster {slot.upper()} — {datetime.now(timezone.utc).isoformat()}")

    # Find all post jobs whose clip hasn't been posted to Facebook yet
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
            .eq("platform", "facebook").eq("status", "posted")
            .execute().data
        )
        if not already:
            jobs.append(job)

    log.info(f"{len(jobs)} clip(s) to post on Facebook.")
    if not jobs:
        return

    headless = os.environ.get("HEADLESS", "false").lower() == "true"

    with sync_playwright() as pw:
        launch_kwargs: dict = {
            "headless": headless,
            "args": ["--no-sandbox", "--disable-dev-shm-usage",
                     "--disable-blink-features=AutomationControlled"],
        }
        if platform.system() == "Darwin":
            launch_kwargs["executable_path"] = (
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            )
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        load_session(context, slot)
        page = context.new_page()

        for job in jobs:
            job_id = job["id"]
            payload = json.loads(job["payload"])
            clip_id = payload["clip_id"]
            storage_path = payload["storage_path"]
            clip_row = supabase.table("clips").select("caption").eq("id", clip_id).single().execute()
            caption = (clip_row.data or {}).get("caption") or payload.get("caption", "")

            log.info(f"Processing job {job_id}")
            post_id = get_or_create_post_record(clip_id, slot)

            with tempfile.TemporaryDirectory() as tmpdir:
                video_path = os.path.join(tmpdir, "clip.mp4")
                log.info("Downloading clip...")
                download_clip(storage_path, video_path)
                log.info(f"Clip ready: {os.path.getsize(video_path)/1e6:.1f} MB")

                try:
                    upload_to_facebook(page, video_path, caption)
                    _mark_posted_with_retry(post_id)
                    log.info(f"Job {job_id} done.")
                except Exception as exc:
                    log.error(f"Job {job_id} failed: {exc}", exc_info=True)
                    mark_post(post_id, "failed", error=str(exc))

            time.sleep(10)

        save_session(context, slot)
        browser.close()

    log.info("Done.")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("a", "b"):
        print("Usage: python3 poster_facebook.py a|b")
        sys.exit(1)
    main(sys.argv[1])
