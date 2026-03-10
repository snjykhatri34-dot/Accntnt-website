"""
main.py — FastAPI backend for LinkedIn HBS Alumni Finder.

Endpoints:
  POST /api/upload  — Parse CSV, return list of funds
  POST /api/search  — Search LinkedIn for HBS alumni per fund (streaming NDJSON)
  POST /api/connect — Send a LinkedIn connection request

Run with:
    python main.py
"""

import asyncio
import csv
import io
import json
import os
import random
import sys
import time
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SESSION_FILE = "linkedin_session.json"
MAX_PROFILES_PER_FUND = 10
DELAY_MIN = 2.0
DELAY_MAX = 4.0

CAPTCHA_PHRASES = [
    "unusual activity",
    "security verification",
    "captcha",
    "let's do a quick security check",
    "verify you're a human",
    "checkpoint",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="HBS Alumni Finder")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class FundInfo(BaseModel):
    name: str
    aum: Optional[str] = None
    category: Optional[str] = None
    hq: Optional[str] = None


class SearchFilters(BaseModel):
    school: str = "Harvard Business School"
    grad_year_min: Optional[int] = None
    grad_year_max: Optional[int] = None
    titles_exclude: list[str] = ["Intern", "Analyst"]


class SearchRequest(BaseModel):
    funds: list[FundInfo]
    filters: SearchFilters = SearchFilters()


class ConnectRequest(BaseModel):
    linkedin_url: str
    message: str


# ---------------------------------------------------------------------------
# Session / browser helpers
# ---------------------------------------------------------------------------


def load_session() -> dict:
    """Load the saved LinkedIn session or exit with an error."""
    if not os.path.exists(SESSION_FILE):
        print(f"ERROR: {SESSION_FILE} not found.")
        print("Please run: python setup_auth.py")
        sys.exit(1)
    with open(SESSION_FILE) as f:
        return json.load(f)


async def make_context(playwright_instance) -> BrowserContext:
    """Create a browser context with the saved LinkedIn session."""
    session = load_session()
    browser: Browser = await playwright_instance.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    context: BrowserContext = await browser.new_context(
        storage_state=session,
        viewport={"width": 1280, "height": 800},
        user_agent=USER_AGENT,
    )
    return context


async def is_session_valid(page: Page) -> bool:
    """Quick check: navigate to feed and see if we're still logged in."""
    try:
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1.5)
        url = page.url
        if "login" in url or "checkpoint" in url or "authwall" in url:
            return False
        return True
    except Exception:
        return False


def detect_captcha(html: str) -> bool:
    lower = html.lower()
    return any(phrase in lower for phrase in CAPTCHA_PHRASES)


async def random_delay():
    await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------


def parse_csv(content: bytes) -> list[dict]:
    """
    Parse the uploaded CSV. Expected columns (case-insensitive, order flexible):
      #, Fund Name, Category, AUM (Most Recent), HQ, Strategy, Investment Types & Check Sizes
    Returns a list of fund dicts.
    """
    text = content.decode("utf-8-sig")  # strip BOM if present
    reader = csv.DictReader(io.StringIO(text))

    # Normalize headers: strip whitespace, lowercase for matching
    def norm(s: str) -> str:
        return s.strip().lower()

    funds = []
    for row in reader:
        normalized = {norm(k): v.strip() for k, v in row.items() if k}

        # Try to find each field with flexible key matching
        def get(keys: list[str]) -> str:
            for k in keys:
                for nk, v in normalized.items():
                    if k in nk:
                        return v
            return ""

        name = get(["fund name", "fund"])
        if not name:
            continue  # skip rows without a fund name

        funds.append(
            {
                "name": name,
                "aum": get(["aum", "assets under management"]),
                "category": get(["category"]),
                "hq": get(["hq", "headquarter", "location"]),
            }
        )

    return funds


# ---------------------------------------------------------------------------
# LinkedIn scraping helpers
# ---------------------------------------------------------------------------


async def search_profiles_for_fund(
    context: BrowserContext,
    fund: FundInfo,
    filters: SearchFilters,
) -> AsyncGenerator[dict, None]:
    """
    Search LinkedIn for people at a given fund who attended HBS.
    Yields profile dicts as they are found.
    """
    page: Page = await context.new_page()

    try:
        # Build search URL
        school_quoted = f'"{filters.school}"'
        fund_quoted = f'"{fund.name}"'
        search_query = f"{fund_quoted} {school_quoted}"
        search_url = (
            f"https://www.linkedin.com/search/results/people/"
            f"?keywords={search_query.replace(' ', '%20')}"
            f"&origin=GLOBAL_SEARCH_HEADER"
        )

        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await random_delay()

        # Check for CAPTCHA / unusual activity
        page_html = await page.content()
        if detect_captcha(page_html):
            print(
                f"\n⚠  CAPTCHA detected while searching for {fund.name}. "
                "Please resolve in the browser (run with headless=False) and restart."
            )
            yield {
                "__captcha__": True,
                "fund_name": fund.name,
            }
            return

        # Collect profile links from search results
        profile_links: list[str] = []

        # LinkedIn search results: look for anchors pointing to /in/ profiles
        anchors = await page.query_selector_all("a[href*='/in/']")
        seen = set()
        for anchor in anchors:
            href = await anchor.get_attribute("href")
            if href and "/in/" in href:
                # Normalize URL — strip query params
                clean = href.split("?")[0].rstrip("/")
                if clean not in seen:
                    seen.add(clean)
                    profile_links.append(clean)
            if len(profile_links) >= MAX_PROFILES_PER_FUND:
                break

        # Try next page if we haven't hit the cap yet (one extra page)
        if len(profile_links) < MAX_PROFILES_PER_FUND:
            try:
                next_btn = await page.query_selector("button[aria-label='Next']")
                if next_btn:
                    await next_btn.click()
                    await random_delay()
                    anchors2 = await page.query_selector_all("a[href*='/in/']")
                    for anchor in anchors2:
                        href = await anchor.get_attribute("href")
                        if href and "/in/" in href:
                            clean = href.split("?")[0].rstrip("/")
                            if clean not in seen:
                                seen.add(clean)
                                profile_links.append(clean)
                        if len(profile_links) >= MAX_PROFILES_PER_FUND:
                            break
            except Exception:
                pass

        # Visit each profile page and extract data
        count = 0
        for profile_url in profile_links[:MAX_PROFILES_PER_FUND]:
            try:
                profile_data = await extract_profile(
                    context, profile_url, fund, filters
                )
                if profile_data:
                    count += 1
                    yield profile_data
            except Exception as e:
                print(f"  Error extracting profile {profile_url}: {e}")
            await random_delay()

        if count == 0:
            # Yield a sentinel "no results" row
            yield {
                "__no_results__": True,
                "fund_name": fund.name,
                "fund_aum": fund.aum or "",
                "fund_category": fund.category or "",
                "fund_hq": fund.hq or "",
            }

    except Exception as e:
        print(f"  Error searching for fund {fund.name}: {e}")
    finally:
        await page.close()


async def extract_profile(
    context: BrowserContext,
    profile_url: str,
    fund: FundInfo,
    filters: SearchFilters,
) -> Optional[dict]:
    """
    Open a LinkedIn profile page and extract key fields.
    Returns None if the profile doesn't match filters.
    """
    page: Page = await context.new_page()
    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1.5)

        # CAPTCHA check
        page_html = await page.content()
        if detect_captcha(page_html):
            print(f"\n⚠  CAPTCHA detected on profile {profile_url}. Pausing.")
            return None

        # ---- Full name ----
        name = ""
        try:
            name_el = await page.query_selector("h1")
            if name_el:
                name = (await name_el.inner_text()).strip()
        except Exception:
            pass

        if not name:
            return None

        # ---- Profile photo ----
        photo_url = ""
        try:
            photo_el = await page.query_selector(
                "img.pv-top-card-profile-picture__image, "
                "img.profile-photo-edit__preview, "
                "img[data-delayed-url]"
            )
            if photo_el:
                photo_url = (
                    await photo_el.get_attribute("src")
                    or await photo_el.get_attribute("data-delayed-url")
                    or ""
                )
        except Exception:
            pass

        # ---- Current title ----
        title = ""
        try:
            title_el = await page.query_selector(
                "div.text-body-medium.break-words, "
                ".pv-top-card--list > li:first-child"
            )
            if title_el:
                title = (await title_el.inner_text()).strip()
        except Exception:
            pass

        # ---- Exclude by title ----
        if filters.titles_exclude:
            title_lower = title.lower()
            for excl in filters.titles_exclude:
                if excl.strip().lower() in title_lower:
                    return None

        # ---- HBS graduation year ----
        hbs_grad_year = None
        try:
            # Scroll to education section
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await asyncio.sleep(1)

            edu_section = await page.query_selector("#education")
            if not edu_section:
                # Try alternate selector
                edu_section = await page.query_selector(
                    "section[data-section='education']"
                )

            if edu_section:
                edu_html = await edu_section.inner_html()
                edu_text = await edu_section.inner_text()
            else:
                edu_text = ""

            # Look for HBS mention + year
            if "harvard" in edu_text.lower():
                import re

                # Try to find a 4-digit year near "Harvard" in the text
                lines = edu_text.split("\n")
                for i, line in enumerate(lines):
                    if "harvard" in line.lower():
                        # Check surrounding lines for a year
                        context_text = " ".join(lines[max(0, i - 2) : i + 4])
                        years = re.findall(r"\b(19[6-9]\d|20[0-3]\d)\b", context_text)
                        if years:
                            # Take the largest year as graduation year
                            hbs_grad_year = max(int(y) for y in years)
                            break
        except Exception:
            pass

        # ---- Filter by grad year ----
        if filters.grad_year_min and hbs_grad_year:
            if hbs_grad_year < filters.grad_year_min:
                return None
        if filters.grad_year_max and hbs_grad_year:
            if hbs_grad_year > filters.grad_year_max:
                return None

        return {
            "name": name,
            "linkedin_url": profile_url,
            "photo_url": photo_url,
            "title": title,
            "fund_name": fund.name,
            "fund_aum": fund.aum or "",
            "fund_category": fund.category or "",
            "fund_hq": fund.hq or "",
            "hbs_grad_year": hbs_grad_year,
        }

    except Exception as e:
        print(f"  Profile extraction error ({profile_url}): {e}")
        return None
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@app.post("/api/upload")
async def upload_csv(file: UploadFile = File(...)):
    """Parse an uploaded CSV file and return fund data."""
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a CSV.")

    content = await file.read()
    try:
        funds = parse_csv(content)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"CSV parse error: {e}")

    if not funds:
        raise HTTPException(status_code=422, detail="No fund names found in CSV.")

    return {"funds": funds}


@app.post("/api/search")
async def search_linkedin(request: SearchRequest):
    """
    Stream NDJSON results as profiles are found across all funds.
    Each line is a JSON object: a profile dict, a progress update,
    a captcha warning, or a no-results sentinel.
    """

    async def generate() -> AsyncGenerator[bytes, None]:
        async with async_playwright() as playwright:
            context = await make_context(playwright)

            # Validate session
            validation_page = await context.new_page()
            valid = await is_session_valid(validation_page)
            await validation_page.close()

            if not valid:
                error_line = json.dumps(
                    {
                        "__error__": True,
                        "message": "Session expired — please re-run setup_auth.py",
                    }
                )
                yield (error_line + "\n").encode()
                await context.browser.close()
                return

            total = len(request.funds)
            for idx, fund in enumerate(request.funds, start=1):
                # Emit progress update
                progress = json.dumps(
                    {
                        "__progress__": True,
                        "current": idx,
                        "total": total,
                        "fund_name": fund.name,
                    }
                )
                yield (progress + "\n").encode()

                async for result in search_profiles_for_fund(
                    context, fund, request.filters
                ):
                    yield (json.dumps(result) + "\n").encode()

            await context.browser.close()

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.post("/api/connect")
async def send_connection(request: ConnectRequest):
    """Send a LinkedIn connection request to a profile."""
    async with async_playwright() as playwright:
        context = await make_context(playwright)
        page: Page = await context.new_page()

        try:
            await page.goto(request.linkedin_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            # CAPTCHA check
            html = await page.content()
            if detect_captcha(html):
                return {
                    "success": False,
                    "error": "CAPTCHA detected — please resolve in browser and try again.",
                }

            # Click "Connect" button
            connect_btn = None

            # Try the top-card connect button
            try:
                connect_btn = await page.query_selector(
                    "button[aria-label*='Connect'], "
                    "button[data-control-name='connect']"
                )
            except Exception:
                pass

            if not connect_btn:
                # Sometimes it's inside a More... dropdown
                try:
                    more_btn = await page.query_selector(
                        "button[aria-label*='More actions']"
                    )
                    if more_btn:
                        await more_btn.click()
                        await asyncio.sleep(1)
                        connect_btn = await page.query_selector(
                            "div[aria-label*='Connect'], "
                            "span:text('Connect')"
                        )
                except Exception:
                    pass

            if not connect_btn:
                return {
                    "success": False,
                    "error": "Connect button not found — profile may have connection restrictions.",
                }

            await connect_btn.click()
            await asyncio.sleep(1.5)

            # Click "Add a note"
            try:
                add_note_btn = await page.query_selector(
                    "button[aria-label*='Add a note'], "
                    "button:has-text('Add a note')"
                )
                if add_note_btn:
                    await add_note_btn.click()
                    await asyncio.sleep(1)
            except Exception:
                pass

            # Fill in the message
            try:
                textarea = await page.query_selector(
                    "textarea[name='message'], "
                    "textarea#custom-message"
                )
                if textarea:
                    await textarea.fill(request.message)
                    await asyncio.sleep(0.5)
            except Exception:
                pass

            # Click Send
            try:
                send_btn = await page.query_selector(
                    "button[aria-label*='Send invitation'], "
                    "button:has-text('Send')"
                )
                if send_btn:
                    await send_btn.click()
                    await asyncio.sleep(1.5)
                    return {"success": True}
                else:
                    return {
                        "success": False,
                        "error": "Send button not found.",
                    }
            except Exception as e:
                return {"success": False, "error": str(e)}

        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            await page.close()
            await context.browser.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Verify session file exists before starting
    if not os.path.exists(SESSION_FILE):
        print(f"ERROR: {SESSION_FILE} not found.")
        print("Please run: python setup_auth.py")
        sys.exit(1)

    print("Starting HBS Alumni Finder backend on http://localhost:8000")
    print("Open index.html in your browser to use the tool.")
    uvicorn.run(app, host="0.0.0.0", port=8000)
