"""
Unified Poster — TikTok, YouTube, Facebook, Instagram
Claude Haiku vision controls the browser like a human.
Auto-login using credentials from env vars — no manual auth needed.

Cron: */30 * * * *  (runs every 30 min, posts only at scheduled times)
Posting times (UTC): 09:00, 16:30, 18:00

Env vars per slot:
  POSTER_A_TIKTOK_EMAIL / POSTER_A_TIKTOK_PASSWORD
  POSTER_A_YOUTUBE_EMAIL / POSTER_A_YOUTUBE_PASSWORD
  POSTER_A_FACEBOOK_EMAIL / POSTER_A_FACEBOOK_PASSWORD
  POSTER_A_INSTAGRAM_EMAIL / POSTER_A_INSTAGRAM_PASSWORD
  (same for B: POSTER_B_...)
"""

import os
import sys
import json
import time
import base64
import random
import tempfile
import platform as sys_platform
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
CLIP_BUCKET = "clips"
SESSION_BUCKET = "sessions"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

PLATFORMS = ["tiktok", "youtube", "facebook", "instagram"]

# Post at 09:00, 16:30, 18:00 UTC — window of ±20 minutes
POSTING_TIMES_UTC = [(9, 0), (16, 30), (18, 0)]
WINDOW_MINUTES = 20


# ---------------------------------------------------------------------------
# Time gate
# ---------------------------------------------------------------------------

def is_posting_time() -> bool:
    now = datetime.now(timezone.utc)
    for hour, minute in POSTING_TIMES_UTC:
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        diff = abs((now - target).total_seconds() / 60)
        if diff <= WINDOW_MINUTES:
            return True
    return False


# ---------------------------------------------------------------------------
# Claude Haiku vision helpers
# ---------------------------------------------------------------------------

def ask(page: Page, question: str, context: str = "") -> dict:
    img = base64.standard_b64encode(page.screenshot()).decode()
    prompt = f"""You are controlling a browser at 1280x800 resolution.
{context}

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
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
            {"type": "text", "text": prompt},
        ]}],
    )
    raw = resp.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    result = json.loads(raw)
    log.info(f"[claude] {question[:70]}… → {result['answer']}")
    return result


def click_at(page: Page, x: int, y: int, label: str = "") -> None:
    log.info(f"Clicking {label} at ({x},{y})")
    page.mouse.click(x, y)
    time.sleep(1.5)


def type_into(page: Page, x: int, y: int, text: str, label: str = "") -> None:
    click_at(page, x, y, label)
    page.keyboard.press("Control+a")
    time.sleep(0.2)
    page.keyboard.type(text, delay=60)
    time.sleep(0.5)


def wait_for_upload(page: Page, platform: str, timeout: int = 180) -> None:
    log.info(f"[{platform}] Waiting for upload (up to {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        r = ask(page, "Is there still an upload progress bar or 'uploading' spinner visible?",
                f"I just uploaded a video to {platform}.")
        if not r["found"]:
            log.info(f"[{platform}] Upload complete.")
            return
        time.sleep(8)
    log.warning(f"[{platform}] Upload wait timed out — proceeding anyway.")


# ---------------------------------------------------------------------------
# Login helpers
# ---------------------------------------------------------------------------

def is_logged_in(page: Page, platform: str) -> bool:
    r = ask(page,
            f"Are you logged into {platform}? Answer found=true if you see a home feed, profile, "
            f"dashboard or content. Answer found=false if you see a login/sign-in/create account page.")
    return bool(r["found"])


def login_tiktok(page: Page, email: str, password: str) -> bool:
    log.info("[login/tiktok] Attempting login...")
    page.goto("https://www.tiktok.com/login/phone-or-email/email", wait_until="domcontentloaded")
    time.sleep(3)
    r = ask(page, "Where is the email or username input field on the login form?")
    if not r["found"]:
        return False
    type_into(page, r["x"], r["y"], email, "email field")
    r = ask(page, "Where is the password input field?")
    if not r["found"]:
        return False
    type_into(page, r["x"], r["y"], password, "password field")
    r = ask(page, "Where is the Log in or Sign in button?")
    if not r["found"]:
        return False
    click_at(page, r["x"], r["y"], "login button")
    time.sleep(5)
    return is_logged_in(page, "TikTok")


def login_google(page: Page, email: str, password: str) -> bool:
    log.info("[login/youtube] Attempting Google login...")
    page.goto("https://accounts.google.com/signin/v2/identifier", wait_until="domcontentloaded")
    time.sleep(3)
    r = ask(page, "Where is the email or phone input field?")
    if not r["found"]:
        return False
    type_into(page, r["x"], r["y"], email, "email field")
    r = ask(page, "Where is the Next button?")
    if r["found"]:
        click_at(page, r["x"], r["y"], "Next")
    time.sleep(3)
    r = ask(page, "Where is the password input field?")
    if not r["found"]:
        return False
    type_into(page, r["x"], r["y"], password, "password field")
    r = ask(page, "Where is the Next button?")
    if r["found"]:
        click_at(page, r["x"], r["y"], "Next")
    time.sleep(5)
    page.goto("https://studio.youtube.com", wait_until="domcontentloaded")
    time.sleep(3)
    return is_logged_in(page, "YouTube Studio")


def login_facebook(page: Page, email: str, password: str) -> bool:
    log.info("[login/facebook] Attempting login...")
    page.goto("https://www.facebook.com", wait_until="domcontentloaded")
    time.sleep(3)
    r = ask(page, "Where is the email or phone number input field on the login form?")
    if not r["found"]:
        return False
    type_into(page, r["x"], r["y"], email, "email field")
    r = ask(page, "Where is the password input field?")
    if not r["found"]:
        return False
    type_into(page, r["x"], r["y"], password, "password field")
    r = ask(page, "Where is the Log In button?")
    if not r["found"]:
        return False
    click_at(page, r["x"], r["y"], "Log In button")
    time.sleep(5)
    return is_logged_in(page, "Facebook")


def login_instagram(page: Page, email: str, password: str) -> bool:
    log.info("[login/instagram] Attempting login...")
    page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
    time.sleep(3)
    r = ask(page, "Where is the username or email input field on the login form?")
    if not r["found"]:
        return False
    type_into(page, r["x"], r["y"], email, "username field")
    r = ask(page, "Where is the password input field?")
    if not r["found"]:
        return False
    type_into(page, r["x"], r["y"], password, "password field")
    r = ask(page, "Where is the Log in button?")
    if not r["found"]:
        return False
    click_at(page, r["x"], r["y"], "Log in button")
    time.sleep(5)
    return is_logged_in(page, "Instagram")


LOGIN_FNS = {
    "tiktok": login_tiktok,
    "youtube": login_google,
    "facebook": login_facebook,
    "instagram": login_instagram,
}


def ensure_logged_in(page: Page, platform: str, creds: dict) -> bool:
    if is_logged_in(page, platform):
        log.info(f"[{platform}] Already logged in.")
        return True
    email = creds.get("email", "")
    password = creds.get("password", "")
    if not email or not password:
        log.warning(f"[{platform}] Not logged in and no credentials provided.")
        return False
    return LOGIN_FNS[platform](page, email, password)


# ---------------------------------------------------------------------------
# Platform posters
# ---------------------------------------------------------------------------

def post_tiktok(context: BrowserContext, slot: str, video_path: str, caption: str, creds: dict) -> str:
    page = context.new_page()
    try:
        page.goto("https://www.tiktok.com", wait_until="domcontentloaded")
        time.sleep(3)
        if not ensure_logged_in(page, "tiktok", creds):
            raise RuntimeError("Could not log in to TikTok.")

        log.info(f"[{slot}/tiktok] Navigating to upload...")
        page.goto("https://www.tiktok.com/tiktokstudio/upload", wait_until="domcontentloaded")
        time.sleep(5)

        # Set file — TikTok wraps input in an iframe
        uploaded = False
        for frame in [page] + page.frames:
            try:
                fi = frame.locator('input[type="file"]')
                if fi.count() > 0:
                    fi.first.set_input_files(video_path)
                    uploaded = True
                    log.info(f"[{slot}/tiktok] File set.")
                    break
            except Exception:
                continue
        if not uploaded:
            raise RuntimeError("Could not find TikTok file input.")

        time.sleep(10)
        wait_for_upload(page, "TikTok", timeout=120)
        time.sleep(3)

        r = ask(page, "Where is the caption or description text input box for the video?", "TikTok upload page.")
        if r["found"]:
            type_into(page, r["x"], r["y"], caption[:150], "caption")
            time.sleep(2)

        r = ask(page, "Where is the Post or Publish button to submit the video?")
        if not r["found"]:
            raise RuntimeError("Could not find TikTok Post button.")
        click_at(page, r["x"], r["y"], "Post button")
        time.sleep(15)

        r = ask(page, "Was the video posted successfully? Look for a success message or redirect away from upload page.")
        log.info(f"[{slot}/tiktok] Result: {r['answer']}")
        return "https://www.tiktok.com"
    finally:
        page.close()


def post_youtube(context: BrowserContext, slot: str, video_path: str, caption: str, creds: dict) -> str:
    page = context.new_page()
    try:
        page.goto("https://studio.youtube.com", wait_until="domcontentloaded")
        time.sleep(4)
        if not ensure_logged_in(page, "youtube", creds):
            raise RuntimeError("Could not log in to YouTube.")

        r = ask(page, "Where is the CREATE or Upload button in YouTube Studio?")
        if not r["found"]:
            raise RuntimeError("Could not find YouTube CREATE button.")
        click_at(page, r["x"], r["y"], "CREATE")
        time.sleep(2)

        r = ask(page, "Where is the 'Upload videos' option in the menu?")
        if r["found"]:
            click_at(page, r["x"], r["y"], "Upload videos")
            time.sleep(2)

        page.locator('input[type="file"]').first.set_input_files(video_path)
        log.info(f"[{slot}/youtube] File set. Waiting...")
        time.sleep(10)
        wait_for_upload(page, "YouTube", timeout=180)
        time.sleep(3)

        r = ask(page, "Where is the title input field for the video?", "YouTube upload dialog is open.")
        if r["found"]:
            type_into(page, r["x"], r["y"], caption[:90], "title")
            time.sleep(1)

        for i in range(3):
            r = ask(page, "Where is the Next button to proceed?")
            if r["found"]:
                click_at(page, r["x"], r["y"], f"Next ({i+1})")
                time.sleep(2)

        r = ask(page, "Where is the Public visibility option?")
        if r["found"]:
            click_at(page, r["x"], r["y"], "Public")
            time.sleep(1)

        r = ask(page, "Where is the Publish or Save button?")
        if not r["found"]:
            raise RuntimeError("Could not find YouTube Publish button.")
        click_at(page, r["x"], r["y"], "Publish")
        time.sleep(15)

        r = ask(page, "Was the video published successfully?")
        log.info(f"[{slot}/youtube] Result: {r['answer']}")
        return "https://studio.youtube.com"
    finally:
        page.close()


def post_facebook(context: BrowserContext, slot: str, video_path: str, caption: str, creds: dict) -> str:
    page = context.new_page()
    try:
        page.goto("https://www.facebook.com", wait_until="domcontentloaded")
        time.sleep(4)
        if not ensure_logged_in(page, "facebook", creds):
            raise RuntimeError("Could not log in to Facebook.")

        page.goto("https://www.facebook.com/reels/create", wait_until="domcontentloaded")
        time.sleep(5)

        if "reels/create" not in page.url:
            r = ask(page, "Where is the option to create a Reel or upload a video?")
            if r["found"]:
                click_at(page, r["x"], r["y"], "create reel")
                time.sleep(3)

        try:
            fi = page.locator('input[type="file"]').first
            fi.set_input_files(video_path)
        except Exception:
            page.evaluate('document.querySelectorAll(\'input[type="file"]\').forEach(e=>{ e.style.display="block"; e.style.opacity="1"; })')
            page.locator('input[type="file"]').first.set_input_files(video_path)

        log.info(f"[{slot}/facebook] File set. Waiting...")
        time.sleep(15)
        wait_for_upload(page, "Facebook", timeout=120)
        time.sleep(3)

        r = ask(page, "Where is the caption or description text input for the video?", "Facebook video upload.")
        if r["found"]:
            type_into(page, r["x"], r["y"], caption[:200], "caption")
            time.sleep(2)

        r = ask(page, "Where is the Share or Publish button to post this video?")
        if not r["found"]:
            raise RuntimeError("Could not find Facebook Share button.")
        click_at(page, r["x"], r["y"], "Share")
        time.sleep(15)

        r = ask(page, "Was the video posted successfully?")
        log.info(f"[{slot}/facebook] Result: {r['answer']}")
        return "https://www.facebook.com"
    finally:
        page.close()


def post_instagram(context: BrowserContext, slot: str, video_path: str, caption: str, creds: dict) -> str:
    page = context.new_page()
    try:
        page.goto("https://www.instagram.com", wait_until="domcontentloaded")
        time.sleep(4)
        if not ensure_logged_in(page, "instagram", creds):
            raise RuntimeError("Could not log in to Instagram.")

        r = ask(page, "Where is the Create or + button to make a new post or reel?", "Instagram home feed.")
        if not r["found"]:
            raise RuntimeError("Could not find Instagram Create button.")
        click_at(page, r["x"], r["y"], "Create button")
        time.sleep(2)

        r = ask(page, "Where is the 'Post' or 'Reel' option in the menu?")
        if r["found"]:
            click_at(page, r["x"], r["y"], "Reel option")
            time.sleep(2)

        # Set file
        try:
            fi = page.locator('input[type="file"]').first
            fi.set_input_files(video_path)
        except Exception:
            page.evaluate('document.querySelectorAll(\'input[type="file"]\').forEach(e=>{ e.style.display="block"; e.style.opacity="1"; })')
            page.locator('input[type="file"]').first.set_input_files(video_path)

        log.info(f"[{slot}/instagram] File set. Waiting...")
        time.sleep(10)
        wait_for_upload(page, "Instagram", timeout=120)
        time.sleep(3)

        # Next through Instagram's multi-step flow
        for i in range(3):
            r = ask(page, "Where is the Next button to proceed to the next step?")
            if r["found"]:
                click_at(page, r["x"], r["y"], f"Next ({i+1})")
                time.sleep(2)
            else:
                break

        r = ask(page, "Where is the caption or write a caption text input area?", "Instagram final step before sharing.")
        if r["found"]:
            type_into(page, r["x"], r["y"], caption[:2200], "caption")
            time.sleep(2)

        r = ask(page, "Where is the Share or Post button to publish this reel?")
        if not r["found"]:
            raise RuntimeError("Could not find Instagram Share button.")
        click_at(page, r["x"], r["y"], "Share")
        time.sleep(15)

        r = ask(page, "Was the reel posted successfully? Look for a success message or redirect to feed.")
        log.info(f"[{slot}/instagram] Result: {r['answer']}")
        return "https://www.instagram.com"
    finally:
        page.close()


PLATFORM_FNS = {
    "tiktok": post_tiktok,
    "youtube": post_youtube,
    "facebook": post_facebook,
    "instagram": post_instagram,
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
        pass


def save_session(context: BrowserContext, slot: str, platform: str) -> None:
    try:
        supabase.storage.from_(SESSION_BUCKET).upload(
            path=f"sessions/{slot}/{platform}.json",
            file=json.dumps(context.cookies()).encode(),
            file_options={"content-type": "application/json", "upsert": "true"},
        )
    except Exception as e:
        log.warning(f"[{slot}/{platform}] Could not save session: {e}")


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def get_creds(slot: str, platform: str) -> dict:
    prefix = f"POSTER_{slot.upper()}_{platform.upper()}"
    return {
        "email": os.environ.get(f"{prefix}_EMAIL", ""),
        "password": os.environ.get(f"{prefix}_PASSWORD", ""),
    }


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


def mark_post_with_retry(post_id: str, status: str, post_url: str = None) -> None:
    for attempt in range(5):
        try:
            mark_post(post_id, status, post_url=post_url)
            return
        except Exception as e:
            if attempt < 4:
                time.sleep(2 ** attempt)
            else:
                log.error(f"mark_post failed after 5 attempts: {e}")


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
            log.warning(f"Download attempt {attempt+1} failed: {e}")
            time.sleep(5)


# ---------------------------------------------------------------------------
# Process one job
# ---------------------------------------------------------------------------

def process_job(job: dict, slot: str, context: BrowserContext) -> None:
    job_id = job["id"]
    payload = json.loads(job["payload"])
    clip_id = payload["clip_id"]
    storage_path = payload["storage_path"]
    caption = payload.get("caption", "")

    log.info(f"[{slot}] Processing job {job_id}")
    supabase.table("jobs").update({
        "status": "running", "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

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
            already = supabase.table("posts").select("status").eq("id", post_id).single().execute()
            if already.data and already.data["status"] == "posted":
                log.info(f"[{slot}/{platform}] Already posted, skipping.")
                continue

            try:
                url = PLATFORM_FNS[platform](context, slot, video_path, caption, creds)
                mark_post_with_retry(post_id, "posted", post_url=url)
                log.info(f"[{slot}/{platform}] Posted ✓")
            except Exception as exc:
                log.error(f"[{slot}/{platform}] Failed: {exc}", exc_info=True)
                mark_post(post_id, "failed", error=str(exc))
                all_ok = False

            save_session(context, slot, platform)
            time.sleep(random.uniform(5, 10))

    supabase.table("jobs").update({
        "status": "done" if all_ok else "failed",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()


# ---------------------------------------------------------------------------
# Run one slot
# ---------------------------------------------------------------------------

def run_slot(slot: str) -> None:
    log.info(f"=== Slot {slot.upper()} ===")
    jobs = (
        supabase.table("jobs").select("*")
        .eq("job_type", f"post_{slot}").eq("status", "pending")
        .order("created_at").limit(1).execute().data
    )
    if not jobs:
        log.info(f"[{slot}] No pending jobs.")
        return

    launch_kwargs = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage",
                 "--disable-blink-features=AutomationControlled",
                 "--disable-gpu", "--disable-software-rasterizer"],
    }
    if sys_platform.system() == "Darwin":
        launch_kwargs["executable_path"] = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        launch_kwargs["headless"] = os.environ.get("HEADLESS", "false").lower() == "true"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        for platform in PLATFORMS:
            load_session(context, slot, platform)

        for job in jobs:
            try:
                process_job(job, slot, context)
            except Exception as exc:
                log.error(f"[{slot}] Job {job['id']} crashed: {exc}", exc_info=True)
                supabase.table("jobs").update({
                    "status": "failed", "error": str(exc)[:1000],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", job["id"]).execute()

        browser.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info(f"Poster starting — {datetime.now(timezone.utc).isoformat()}")

    if not is_posting_time():
        log.info("Not a posting time — exiting.")
        return

    log.info("Posting time confirmed. Running slots A and B.")
    for slot in ["a", "b"]:
        try:
            run_slot(slot)
        except Exception as exc:
            log.error(f"Slot {slot} crashed: {exc}", exc_info=True)

    log.info("Poster done.")


if __name__ == "__main__":
    # Allow `python3 poster.py` (both slots) or `python3 poster.py a` (single slot, skip time check)
    if len(sys.argv) == 2 and sys.argv[1] in ("a", "b"):
        run_slot(sys.argv[1])
    else:
        main()
