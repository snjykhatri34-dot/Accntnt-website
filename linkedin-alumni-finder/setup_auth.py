"""
setup_auth.py — One-time LinkedIn authentication setup.

Launches a visible Chromium browser, navigates to LinkedIn,
waits for manual login, then saves the session to linkedin_session.json.

Run once before starting main.py:
    python setup_auth.py
"""

import asyncio
import json
import sys
from playwright.async_api import async_playwright

SESSION_FILE = "linkedin_session.json"


async def setup_auth():
    print("=" * 60)
    print("LinkedIn Session Setup")
    print("=" * 60)
    print()
    print("A Chromium browser window will open.")
    print("Please log in to your LinkedIn account manually.")
    print("Once you are on the LinkedIn home/feed page, press ENTER here.")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        print("Opening LinkedIn...")
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        # Wait for the user to manually log in
        print()
        print(">>> Please log in to LinkedIn in the browser window. <<<")
        print(">>> After logging in and seeing your feed, press ENTER here. <<<")
        print()

        # Block until user presses enter
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, "Press ENTER after logging in: ")

        # Verify we are logged in by checking for a nav element or feed
        try:
            current_url = page.url
            if "feed" in current_url or "linkedin.com/in/" in current_url or "mynetwork" in current_url:
                print("Login detected — saving session...")
            else:
                # Try navigating to feed to confirm
                await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
                await asyncio.sleep(2)
                if "login" in page.url or "checkpoint" in page.url:
                    print("ERROR: It looks like you are not logged in.")
                    print("Please re-run setup_auth.py and complete the login.")
                    await browser.close()
                    sys.exit(1)

            # Save cookies and storage state
            storage_state = await context.storage_state()
            with open(SESSION_FILE, "w") as f:
                json.dump(storage_state, f, indent=2)

            print()
            print(f"Session saved to {SESSION_FILE}")
            print("You can now run: python main.py")

        except Exception as e:
            print(f"ERROR saving session: {e}")
            await browser.close()
            sys.exit(1)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(setup_auth())
