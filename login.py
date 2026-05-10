"""
One-time login script — run this once per platform to save sessions.
  python3 login.py a tiktok
  python3 login.py a youtube
  python3 login.py a facebook

It opens Chrome visibly, logs in, pauses for you to complete any
2FA / verification code, then saves the session to Supabase Storage.
Poster.py will reuse these sessions automatically.
"""

import os
import sys
import json
import time
import logging
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from supabase import create_client, Client

load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SESSION_BUCKET = "sessions"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def get_creds(slot: str, platform: str) -> dict:
    prefix = f"POSTER_{slot.upper()}_{platform.upper()}"
    return {
        "email": os.environ.get(f"{prefix}_EMAIL", ""),
        "password": os.environ.get(f"{prefix}_PASSWORD", ""),
    }


def save_session(context, slot: str, platform: str) -> None:
    cookies = context.cookies()
    path = f"sessions/{slot}/{platform}.json"
    supabase.storage.from_(SESSION_BUCKET).upload(
        path=path,
        file=json.dumps(cookies).encode(),
        file_options={"content-type": "application/json", "upsert": "true"},
    )
    log.info(f"Session saved to Supabase Storage: {path}")


def wait_for_user(prompt: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {prompt}")
    print(f"  Press ENTER here when done...")
    print(f"{'='*60}\n")
    input()


def login_tiktok(context, slot: str) -> None:
    page = context.new_page()
    log.info("Opening TikTok — log in manually...")
    page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")
    time.sleep(3)
    wait_for_user("Log into TikTok fully in the Chrome window, then press ENTER here.")
    time.sleep(2)
    save_session(context, slot, "tiktok")
    page.close()


def login_youtube(context, slot: str) -> None:
    page = context.new_page()
    log.info("Opening YouTube Studio — log in if prompted...")
    page.goto("https://studio.youtube.com", wait_until="domcontentloaded")
    time.sleep(3)
    wait_for_user("Log into Google/YouTube Studio fully in the Chrome window, then press ENTER here.")
    time.sleep(2)
    save_session(context, slot, "youtube")
    page.close()


def login_facebook(context, slot: str) -> None:
    page = context.new_page()
    log.info("Opening Facebook — log in manually...")
    page.goto("https://www.facebook.com", wait_until="domcontentloaded")
    time.sleep(3)
    wait_for_user("Log into Facebook fully in the Chrome window, then press ENTER here.")
    time.sleep(2)
    save_session(context, slot, "facebook")
    page.close()


LOGIN_FNS = {
    "tiktok": login_tiktok,
    "youtube": login_youtube,
    "facebook": login_facebook,
}


def main(slot: str, platform: str) -> None:
    if platform not in LOGIN_FNS:
        print(f"Unknown platform: {platform}. Choose from: {list(LOGIN_FNS)}")
        sys.exit(1)

    log.info(f"Starting login for Poster {slot.upper()} / {platform}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )

        LOGIN_FNS[platform](context, slot)

        browser.close()

    log.info(f"Done. Session for {platform} saved. You can now run poster.py.")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("a", "b") or sys.argv[2] not in LOGIN_FNS:
        print("Usage: python3 login.py <slot> <platform>")
        print("  slot:     a | b")
        print("  platform: tiktok | youtube | facebook")
        print("")
        print("Examples:")
        print("  python3 login.py a tiktok")
        print("  python3 login.py a youtube")
        print("  python3 login.py a facebook")
        sys.exit(1)

    main(sys.argv[1], sys.argv[2])
