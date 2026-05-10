"""
TikTok poster — Playwright + JS interactions.
Run as: python3 poster_tiktok.py a

First login: python3 login.py a tiktok
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
        data = supabase.storage.from_(SESSION_BUCKET).download(f"sessions/{slot}/tiktok.json")
        context.add_cookies(json.loads(data))
        log.info("TikTok session loaded.")
    except Exception:
        log.warning("No saved TikTok session — run: python3 login.py a tiktok")


def save_session(context: BrowserContext, slot: str) -> None:
    try:
        supabase.storage.from_(SESSION_BUCKET).upload(
            path=f"sessions/{slot}/tiktok.json",
            file=json.dumps(context.cookies()).encode(),
            file_options={"content-type": "application/json", "upsert": "true"},
        )
        log.info("TikTok session saved.")
    except Exception as e:
        log.warning(f"Could not save session: {e}")


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def is_logged_in(page: Page) -> bool:
    page.goto("https://www.tiktok.com", wait_until="domcontentloaded")
    time.sleep(3)
    # If we see a login button we're not logged in
    not_logged = page.evaluate('''() => {
        const txt = document.body.innerText || "";
        return txt.includes("Log in") && !txt.includes("Upload");
    }''')
    return not not_logged


def upload_to_tiktok(page: Page, video_path: str, caption: str) -> str:
    log.info("Navigating to TikTok upload...")
    page.goto("https://www.tiktok.com/upload", wait_until="domcontentloaded")
    time.sleep(5)

    # Check if redirected to login
    if "login" in page.url:
        raise RuntimeError("TikTok session expired. Run: python3 login.py a tiktok")

    # TikTok wraps the file input inside an iframe — try iframe first, then main page
    log.info("Setting video file...")
    file_set = False
    for frame in page.frames:
        try:
            fi = frame.locator('input[type="file"]')
            if fi.count() > 0:
                fi.first.set_input_files(video_path)
                file_set = True
                log.info("File set via iframe.")
                break
        except Exception:
            continue
    if not file_set:
        page.locator('input[type="file"]').first.set_input_files(video_path)
        log.info("File set via main page.")

    # Wait for upload to process (progress bar / loading state)
    log.info("Waiting for video to process...")
    time.sleep(8)
    _wait_for_processing(page)
    time.sleep(3)

    # Fill caption — search all frames for the caption input
    log.info("Filling caption...")
    _fill_caption(page, caption[:150])
    time.sleep(2)

    # Click Post button
    log.info("Clicking Post...")
    posted = _click_post(page)
    if not posted:
        raise RuntimeError("Could not find TikTok Post button.")

    # Wait for TikTok to process and redirect after posting
    log.info("Waiting for post confirmation...")
    deadline = time.time() + 30
    success = False
    while time.time() < deadline:
        result = page.evaluate('''() => {
            const url = window.location.href;
            const txt = document.body.innerText || "";
            const uploadPage = url.includes("/upload");
            const successIndicators = [
                txt.includes("Your video is being uploaded"),
                txt.includes("successfully"),
                txt.includes("Your video has been"),
                url.includes("/profile"),
                url.includes("/manage"),
                url.includes("/@"),
                // No longer on upload page = posted
                !uploadPage && url.includes("tiktok.com"),
            ];
            return { success: successIndicators.some(Boolean), url };
        }''')
        if result["success"]:
            log.info(f"Post confirmed. URL: {result['url']}")
            success = True
            break
        time.sleep(3)

    if not success:
        # Still on upload page after 30s = something went wrong
        current_url = page.url
        if "upload" in current_url:
            raise RuntimeError("Post may have failed — still on upload page after 30s.")
        log.info(f"Post likely succeeded (left upload page). URL: {current_url}")

    return "https://www.tiktok.com"


def _wait_for_processing(page: Page, timeout: int = 120) -> None:
    """Wait until TikTok finishes processing the uploaded video."""
    start = time.time()
    while time.time() - start < timeout:
        done = True
        for frame in [page] + page.frames:
            try:
                loading = frame.evaluate('''() => {
                    const txt = document.body ? document.body.innerText : "";
                    return txt.includes("Uploading") || txt.includes("processing") ||
                           !!document.querySelector(".upload-progress, [class*=progress]");
                }''')
                if loading:
                    done = False
                    break
            except Exception:
                continue
        if done:
            return
        time.sleep(5)
    log.warning("Processing wait timed out.")


def _fill_caption(page: Page, text: str) -> None:
    """Try to fill the caption field across main page and iframes."""
    for frame in [page] + page.frames:
        try:
            filled = frame.evaluate('''(caption) => {
                // Look for contenteditable caption box
                const boxes = document.querySelectorAll(
                    '[contenteditable="true"], textarea, input[placeholder*="caption"], input[placeholder*="describe"]'
                );
                for (const box of boxes) {
                    const ph = (box.getAttribute("placeholder") || "").toLowerCase();
                    const label = (box.getAttribute("aria-label") || "").toLowerCase();
                    if (ph.includes("caption") || ph.includes("describe") ||
                        ph.includes("title") || label.includes("caption") ||
                        box.getAttribute("contenteditable") === "true") {
                        box.focus();
                        if (box.isContentEditable) {
                            box.innerText = caption;
                        } else {
                            const nativeSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, "value"
                            ).set;
                            nativeSetter.call(box, caption);
                            box.dispatchEvent(new Event("input", { bubbles: true }));
                        }
                        return true;
                    }
                }
                return false;
            }''', text)
            if filled:
                log.info("Caption filled.")
                return
        except Exception:
            continue
    log.warning("Could not find caption box — posting without custom caption.")


def _click_post(page: Page) -> bool:
    """Click the Post/Publish button across main page and iframes."""
    for frame in [page] + page.frames:
        try:
            clicked = frame.evaluate('''() => {
                const buttons = document.querySelectorAll("button");
                for (const btn of buttons) {
                    const txt = (btn.innerText || btn.textContent || "").trim().toLowerCase();
                    if (txt === "post" || txt === "publish" || txt === "submit") {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }''')
            if clicked:
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def get_or_create_post_record(clip_id: str, slot: str) -> str:
    r = (
        supabase.table("posts").select("id")
        .eq("clip_id", clip_id).eq("poster_slot", slot).eq("platform", "tiktok")
        .execute()
    )
    if r.data:
        return r.data[0]["id"]
    return supabase.table("posts").insert({
        "clip_id": clip_id, "poster_slot": slot, "platform": "tiktok",
        "status": "pending", "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute().data[0]["id"]


def mark_post(post_id: str, status: str, error: str = None) -> None:
    supabase.table("posts").update({
        "status": status,
        "post_url": "https://www.tiktok.com" if status == "posted" else None,
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
    log.info(f"TikTok Poster {slot.upper()} — {datetime.now(timezone.utc).isoformat()}")

    # Find all post_a jobs (any status) whose clip hasn't been posted to TikTok yet
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
            .eq("platform", "tiktok").eq("status", "posted")
            .execute().data
        )
        if not already:
            jobs.append(job)

    log.info(f"{len(jobs)} clip(s) to post on TikTok.")
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
            supabase.table("jobs").update({
                "status": "running",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", job_id).execute()

            post_id = get_or_create_post_record(clip_id, slot)

            with tempfile.TemporaryDirectory() as tmpdir:
                video_path = os.path.join(tmpdir, "clip.mp4")
                log.info("Downloading clip...")
                download_clip(storage_path, video_path)
                log.info(f"Clip ready: {os.path.getsize(video_path)/1e6:.1f} MB")

                try:
                    upload_to_tiktok(page, video_path, caption)
                    mark_post(post_id, "posted")
                    supabase.table("jobs").update({
                        "status": "done",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", job_id).execute()
                    log.info(f"Job {job_id} done.")
                except Exception as exc:
                    log.error(f"Job {job_id} failed: {exc}", exc_info=True)
                    mark_post(post_id, "failed", error=str(exc))
                    supabase.table("jobs").update({
                        "status": "failed",
                        "error": str(exc)[:1000],
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", job_id).execute()

            time.sleep(5)

        save_session(context, slot)
        browser.close()

    log.info("Done.")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("a", "b"):
        print("Usage: python3 poster_tiktok.py a|b")
        sys.exit(1)
    main(sys.argv[1])
