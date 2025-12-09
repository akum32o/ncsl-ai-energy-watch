#!/usr/bin/env python3
"""
NCSL AI + Energy/Utilities Legislation Watcher

- Scrapes the 2025 AI legislation table:
  https://www.ncsl.org/technology-and-communication/artificial-intelligence-2025-legislation
- Filters to bills relevant to energy/utilities / grid / data centers.
- Every run:
    * If < DIGEST_DAYS since last email digest -> exit silently.
    * Otherwise, compute "new since last digest" and send an email
      ONLY if there are new relevant bills.
- State is tracked in a JSON file in the repo.

Env vars (via GitHub Actions secrets):

  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
  EMAIL_FROM, EMAIL_TO  (comma-separated list)
  DIGEST_DAYS (optional, default 14)
  STATE_FILE  (optional, default "ncsl_ai_state.json")
  FORCE_EMAIL="1" to ignore the 14-day guard and always send a digest.
"""

import os
import time
import json
import ssl
import smtplib
from typing import List, Dict, Set
from email.mime.text import MIMEText
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

PAGE_URL = "https://www.ncsl.org/technology-and-communication/artificial-intelligence-2025-legislation"

STATE_FILE = os.environ.get("STATE_FILE", "ncsl_ai_state.json")
DIGEST_DAYS = int(os.environ.get("DIGEST_DAYS", "14"))

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.office365.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_TO = [s.strip() for s in os.environ.get("EMAIL_TO", "").split(",") if s.strip()]

FORCE_EMAIL = os.environ.get("FORCE_EMAIL", "0") == "1"

# Pretend to be a normal Chrome browser
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.ncsl.org/",
}

# For grouping in the email
NE_PLUS_NY = [
    "Connecticut",
    "Maine",
    "Massachusetts",
    "New Hampshire",
    "Rhode Island",
    "Vermont",
    "New York",
]

# =======================
# OCC-TUNED KEYWORD GROUPS
# =======================

CORE_UTILITY = [
    "energy", "electric", "electricity", "utility", "utilities",
    "grid", "transmission", "distribution",
    "ratepayer", "rate payers", "ratemaking", "rate-making",
    "power plant", "power generation",
    "renewable", "solar", "wind",
    "battery", "storage", "energy storage",
    "microgrid", "micro-grid",
    "interconnection",
]

DATA_CENTER_INFRA = [
    "data center", "data centres", "data-center",
    "artificial intelligence infrastructure", "ai infrastructure",
    "compute infrastructure", "server farm",
    "high-performance computing", "hpc",
    "large load", "load growth", "peak load", "demand growth",
    "grid reliability", "capacity planning",
    "electric demand", "megawatt", "mw",
]

CONSUMER_PROTECTION = [
    "consumer protection", "consumer rights",
    "algorithmic pricing",
    "algorithmic decision-making", "automated decision making",
    "billing transparency",
    "public utility commission", "utility commission",
    "regulator", "regulation",
    "rate case", "tariff",
]

CLIMATE_POLICY = [
    "emissions", "carbon", "greenhouse gas", "ghg",
    "climate", "decarbonization", "electrification",
    "building performance", "energy efficiency",
    "demand response",
]

ALL_KEYWORDS = (
    CORE_UTILITY +
    DATA_CENTER_INFRA +
    CONSUMER_PROTECTION +
    CLIMATE_POLICY
)


def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {"seen_ids": [], "last_digest": 0}


def save_state(seen_ids: Set[str], last_digest: int):
    state = {
        "seen_ids": sorted(seen_ids),
        "last_digest": int(last_digest),
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_html() -> str:
    """
    Fetch the NCSL page HTML.

    Try cloudscraper if available (better for anti-bot); otherwise use plain
    requests. If all fail, raise a RuntimeError.
    """
    # Try optional cloudscraper
    scraper = None
    try:
        import cloudscraper  # optional dependency
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "mac", "mobile": False}
        )
    except ImportError:
        scraper = None

    if scraper is not None:
        resp = scraper.get(PAGE_URL, headers=HEADERS, timeout=60)
        if resp.status_code == 200:
            return resp.text

    # Fallback to plain requests
    resp2 = requests.get(PAGE_URL, headers=HEADERS, timeout=60)
    if resp2.status_code == 200:
        return resp2.text

    raise RuntimeError(
        f"Unable to fetch NCSL page. "
        f"cloudscraper_status={getattr(locals().get('resp', None), 'status_code', 'n/a')}, "
        f"requests_status={resp2.status_code}"
    )


def fetch_table_rows() -> List[Dict]:
    """Scrape the NCSL page and return all rows as dicts."""
    html = fetch_html()
    soup = BeautifulSoup(html, "html.parser")

    # Find table with correct headers
    target_table = None
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if "Jurisdiction and Summary" in headers and "Bill Number" in headers:
            target_table = table
            break

    if not target_table:
        raise RuntimeError("Could not locate legislation table on NCSL page")

    tbody = target_table.find("tbody") or target_table
    rows = []
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 6:
            continue

        jurisdiction = cells[0].get_text(strip=True)
        bill_cell = cells[1]
        link = bill_cell.find("a")
        bill_number = link.get_text(strip=True) if link else bill_cell.get_text(strip=True)
        bill_url = urljoin(PAGE_URL, link["href"]) if link and link.get("href") else PAGE_URL

        title = cells[2].get_text(strip=True)
        status = cells[3].get_text(strip=True)
        summary = cells[4].get_text(" ", strip=True)
        category = cells[5].get_text(strip=True)

        bill_id = f"{jurisdiction}::{bill_number}"

        rows.append(
            {
                "id": bill_id,
                "state": jurisdiction,
                "bill_number": bill_number,
                "title": title,
                "status": status,
                "summary": summary,
                "category": category,
                "url": bill_url,
            }
        )

    return rows


def is_energy_relevant(row: Dict) -> bool:
    """Check bill title + summary + category for any energy/utility/data-center/consumer/climate relevance."""
    text = " ".join(
        [
            row.get("title", ""),
            row.get("summary", ""),
            row.get("category", ""),
        ]
    ).lower()

    for kw in ALL_KEYWORDS:
        if kw.lower() in text:
            return True

    return False


def filter_relevant(rows: List[Dict]) -> List[Dict]:
    return [r for r in rows if is_energy_relevant(r)]


def group_by_state(new_rows: List[Dict]):
    """Split new rows into {NE+NY states} and {other states}, each mapping state -> list[rows]."""
    top = {s: [] for s in NE_PLUS_NY}
    others: Dict[str, List[Dict]] = {}

    for r in new_rows:
        state = r["state"]
        if state in top:
            top[state].append(r)
        else:
            others.setdefault(state, []).append(r)

    # Drop empty states from "top"
    top = {s: bills for s, bills in top.items() if bills}
    return top, others


def format_email(new_rows: List[Dict], last_digest_ts: int) -> str:
    lines: List[str] = []
    lines.append("NCSL – Artificial Intelligence 2025 Legislation (Energy / Utilities Focus)")
    lines.append(PAGE_URL)
    lines.append("")

    if last_digest_ts:
        last_str = time.strftime("%Y-%m-%d", time.localtime(last_digest_ts))
        lines.append(f"This digest includes bills NEW since the last email on {last_str}.")
    else:
        lines.append("This is the first digest; all listed bills are treated as new.")
    lines.append("")

    lines.append(f"New relevant bills this digest: {len(new_rows)}")
    lines.append("")

    if not new_rows:
        lines.append("No new relevant bills since the last digest.")
        return "\n".join(lines)

    top, others = group_by_state(new_rows)

    # New England + New York section
    if top:
        lines.append("=== New England + New York ===")
        for state in NE_PLUS_NY:
            bills = top.get(state)
            if not bills:
                continue
            lines.append("")
            lines.append(state)
            lines.append("-" * len(state))
            for b in bills:
                lines.append(f"* {b['bill_number']} – {b['title']} ({b['status']})")
                lines.append(f"  {b['summary']}")
                lines.append(f"  {b['url']}")
        lines.append("")

    # Other states section
    if others:
        lines.append("=== Other States ===")
        for state in sorted(others.keys()):
            bills = others[state]
            lines.append("")
            lines.append(state)
            lines.append("-" * len(state))
            for b in bills:
                lines.append(f"* {b['bill_number']} – {b['title']} ({b['status']})")
                lines.append(f"  {b['summary']}")
                lines.append(f"  {b['url']}")
        lines.append("")

    return "\n".join(lines)


def send_email(subject: str, body: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_TO):
        print("Email not configured (missing SMTP_* or EMAIL_TO). Skipping send.")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM or SMTP_USER
    msg["To"] = ", ".join(EMAIL_TO)

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as server:
        server.starttls(context=ctx)
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(msg["From"], EMAIL_TO, msg.as_string())


def main():
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    last_digest = state.get("last_digest", 0)

    now = time.time()
    # DIGEST_DAYS guard – unless FORCE_EMAIL is set
    if not FORCE_EMAIL and last_digest:
        if now - last_digest < DIGEST_DAYS * 24 * 3600:
            print(f"Skipping digest: <{DIGEST_DAYS} days since last email.")
            return

    try:
        all_rows = fetch_table_rows()
    except Exception as e:
        print(f"[NCSL AI Watch] ERROR fetching table: {e}")
        return

    relevant = filter_relevant(all_rows)

    # "New since last digest" = relevant IDs not in seen_ids
    new_rows = [r for r in relevant if r["id"] not in seen_ids]

    if not new_rows and not FORCE_EMAIL:
        print("No new relevant bills since last digest; not sending email.")
        return

    subject_suffix = f"{len(new_rows)} new bill(s)" if new_rows else "Digest (no new bills)"
    subject = f"[NCSL AI+Energy Watch] {subject_suffix}"

    body = format_email(new_rows, last_digest)
    print(body)
    send_email(subject, body)

    # Update state ONLY when a digest is actually sent
    new_seen = seen_ids.union(r["id"] for r in relevant)
    save_state(new_seen, now)


if __name__ == "__main__":
    main()
