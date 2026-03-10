# HBS Alumni Finder

A full-stack tool that searches LinkedIn for Harvard Business School alumni currently working at VC/PE funds from your CSV file.

## Prerequisites

- Python 3.11+
- A LinkedIn account

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Authenticate with LinkedIn (one-time)

```bash
python setup_auth.py
```

A Chromium browser window will open. Log in to your LinkedIn account manually, then press **ENTER** in the terminal. Your session is saved to `linkedin_session.json` (never committed — it's in `.gitignore`).

### 3. Start the backend server

```bash
python main.py
```

The API will be available at `http://localhost:8000`.

### 4. Open the frontend

Open `index.html` directly in your browser (no build step needed):

```
open index.html        # macOS
xdg-open index.html    # Linux
# Or just double-click index.html in Finder/Explorer
```

### 5. Use the tool

1. **Upload** your CSV file (drag-and-drop or click to browse)
   - Expected columns: `Fund Name`, `AUM (Most Recent)`, `Category`, `HQ`
2. **Set filters** — school, graduation year range, titles to exclude
3. Click **Search LinkedIn** — results stream in real time as profiles are found
4. Click **Connect** on any row to send a personalized connection request

## CSV Format

The tool expects a CSV with at least these columns (headers are flexible, matched by keyword):

| # | Fund Name | Category | AUM (Most Recent) | HQ | Strategy | Investment Types & Check Sizes |
|---|-----------|----------|-------------------|----|----------|-------------------------------|
| 1 | Coatue Management | Crossover | ~$48–58B | New York, NY | ... | ... |

## Notes

- **Session expiry**: If LinkedIn logs you out, re-run `python setup_auth.py`.
- **CAPTCHA**: If LinkedIn triggers a CAPTCHA, the frontend shows a yellow banner. You may need to run the backend with `headless=False` (edit `main.py` → `make_context`) and solve it manually.
- **Rate limiting**: The tool adds 2–4 second random delays between page loads to stay within normal usage patterns.
- **Cap**: Results are capped at 10 profiles per fund to avoid aggressive scraping.

## Project Structure

```
linkedin-alumni-finder/
├── main.py          # FastAPI backend + Playwright scraping
├── setup_auth.py    # One-time LinkedIn auth setup
├── index.html       # Frontend (vanilla JS + Tailwind CDN)
├── requirements.txt # Python dependencies
├── .gitignore       # Excludes linkedin_session.json
└── README.md        # This file
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/upload` | Parse CSV, return fund list |
| `POST` | `/api/search` | Search LinkedIn, stream NDJSON results |
| `POST` | `/api/connect` | Send a LinkedIn connection request |

Interactive docs available at `http://localhost:8000/docs` when the server is running.
