"""
setup_auth.py — One-time LinkedIn session extractor.

Reuses your EXISTING logged-in Google Chrome profile so you never
have to log in again.  Runs fully headless (no visible window).

Prerequisites
-------------
1.  pip install playwright
2.  playwright install chrome   # installs Playwright's Chrome driver only

IMPORTANT: Close ALL Chrome windows before running this script.
Chrome locks its profile while it is open — the script will fail
if Chrome is already running.

Usage
-----
    python setup_auth.py

The session is saved to linkedin_session.json (owner-read-only).
Then run:  python main.py
"""

import asyncio
import json
import os
import platform
import stat
import sys
from pathlib import Path

from playwright.async_api import async_playwright

SESSION_FILE = "linkedin_session.json"


# ---------------------------------------------------------------------------
# Locate the default Chrome user-data directory for the current OS
# ---------------------------------------------------------------------------

def _chrome_user_data_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA", "")
        return Path(base) / "Google" / "Chrome" / "User Data"
    if system == "Darwin":  # macOS
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    # Linux / WSL
    return Path.home() / ".config" / "google-chrome"


def _validate_chrome_profile(user_data_dir: Path) -> None:
    """Raise a clear error if the Chrome profile directory is missing."""
    if not user_data_dir.exists():
        print(f"ERROR: Chrome user-data directory not found:\n  {user_data_dir}")
        print("\nMake sure Google Chrome is installed and you have logged in at least once.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def setup_auth() -> None:
    print("=" * 60)
    print("LinkedIn Session Setup  (headless · uses existing Chrome)")
    print("=" * 60)
    print()

    user_data_dir = _chrome_user_data_dir()
    _validate_chrome_profile(user_data_dir)

    print(f"Chrome profile : {user_data_dir}")
    print()
    print("IMPORTANT: Close ALL Google Chrome windows now, then press ENTER.")
    print("(Chrome locks its profile while it is open.)")
    print()
    input("Press ENTER when Chrome is fully closed: ")
    print()

    async with async_playwright() as p:
        # Launch headless Chrome using the system-installed Google Chrome
        # and the user's real profile so existing cookies are available.
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                channel="chrome",          # use your real Google Chrome
                headless=True,             # no visible window
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
        except Exception as exc:
            print(f"ERROR: Could not launch Chrome: {exc}")
            print()
            print("Possible reasons:")
            print("  • Google Chrome is not installed")
            print("  • Chrome is still running — close it and try again")
            print("  • Run:  playwright install chrome")
            sys.exit(1)

        page = await context.new_page()

        print("Navigating to LinkedIn…")
        try:
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            print(f"ERROR: Could not reach LinkedIn: {exc}")
            await context.close()
            sys.exit(1)

        await asyncio.sleep(2)

        # Verify we are actually logged in
        current_url = page.url
        if "login" in current_url or "checkpoint" in current_url or "authwall" in current_url:
            print()
            print("ERROR: LinkedIn redirected to a login/checkpoint page.")
            print("Your Chrome session may have expired or you were never logged in.")
            print()
            print("Fix: Open Google Chrome, log in to LinkedIn normally, then re-run this script.")
            await context.close()
            sys.exit(1)

        print("Logged-in session confirmed.")

        # Extract and save the session state
        try:
            storage_state = await context.storage_state()
        except Exception as exc:
            print(f"ERROR: Could not extract session state: {exc}")
            await context.close()
            sys.exit(1)

        # Write the session file
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(storage_state, f, indent=2)

        # Restrict permissions so only the owner can read the file (Unix only)
        if os.name != "nt":
            os.chmod(SESSION_FILE, stat.S_IRUSR | stat.S_IWUSR)

        await context.close()

    print()
    print(f"Session saved  →  {SESSION_FILE}")
    print()
    print("Next step:  python main.py")


if __name__ == "__main__":
    asyncio.run(setup_auth())
