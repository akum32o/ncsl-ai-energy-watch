#!/usr/bin/env python3
"""
NCSL AI + Energy/Utilities Legislation Watcher

- Scrapes the 2025 AI legislation table from NCSL
- Filters for bills related to energy, utilities, and consumers
- Emails any *new or changed* relevant bills since the last digest

Config is via environment variables (for SMTP + recipients), so nothing
sensitive is hard-coded. Designed to be run via GitHub Actions on a schedule.
"""

import os, json, time, smtplib, ssl
from typing import List, Dict, Tuple
from email.mime.text import MIMEText
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

NCSL_URL = "https://www.ncsl.org/technology-and-communication/artificial-intelligence-2025-legislation"
STATE_FILE = os.environ.get("NCSL_STATE_FILE", "ncsl_ai_energy_state.json")

# ---- Email / SMTP config (reuses same style as your CSC script) ----

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.office365.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")         # e.g., "anubhav.kumaria@ct.gov"
SMTP_PASS = os.environ.get("SMTP_PASS")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_TO = [s.strip() for s in os.environ.get("EMAIL_TO", "").split(",") if s.strip()]

# Optional: if you want to force a digest even if nothing changed
FORCE_EMAIL = os.environ.get("FORCE_EMAIL", "0") == "1"

HEADERS = {
    "User-Agent": "NCSL-AI-Energy-Watch/1.0 (contact: {})".format(EMAIL_FROM or "noreply@example.com")
}

# ---- Filtering logic ----
# These are deliberately a bit broad; tweak as you see what comes through.

ENERGY_WORDS = [
    "energy", "electric", "electricity", "power", "grid",
    "generation", "nuclear", "solar", "wind", "renewable",
    "transmission", "distribution", "microgrid", "demand response",
    "load", "capacity", "efficiency"
]

UTILITY_WORDS = [
    "utility", "utilities", "public utility", "public utilities",
    "ratepayer", "rate payers", "ratepayer", "rates", "billing",
    "natural gas", "gas utility", "water utility", "telecommunications",
    "regulated utility"
]

CONSUMER_WORDS = [
    "consumer", "customers", "customer", "data privacy",
    "privacy", "profiling", "disclosure", "notification",
    "fraud", "scam", "deceptive", "harassment"
]

def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {"bills": {}, "last_digest": 0}

def save_state(bills_snapshot: Dict[str, Dict]):
    state = {
        "bills": bills_snapshot,
        "last_digest": int(time.time()),
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def fetch_all_bills() -> List[Dict]:
    """Scrape the NCSL table and return all rows as structured dicts."""
    resp = requests.get(NCSL_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = soup.select("table tbody tr")
    bills = []

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        jurisdiction = cells[0].get_text(" ", strip=True)

        bill_cell = cells[1]
        bill_number = bill_cell.get_text(" ", strip=True)
        link_tag = bill_cell.find("a")
        bill_url = urljoin(NCSL_URL, link_tag["href"]) if link_tag and link_tag.get("href") else NCSL_URL

        title = cells[2].get_text(" ", strip=True)
        status = cells[3].get_text(" ", strip=True)
        category = cells[4].get_text(" ", strip=True)

        bill_id = f"{jurisdiction}::{bill_number}"

        bills.append({
            "id": bill_id,
            "jurisdiction": jurisdiction,
            "bill_number": bill_number,
            "title": title,
            "status": status,
            "category": category,
            "url": bill_url,
        })

    return bills

def is_relevant(bill: Dict) -> bool:
    """Return True if the bill looks energy/utility/consumer-relevant."""
    haystack = " ".join([
        bill.get("title", ""),
        bill.get("category", ""),
    ]).lower()

    def any_word(words):
        return any(w in haystack for w in words)

    # We’re already on the AI page, so everything is AI-related.
    # Filter down to AI x (energy OR utilities OR consumers).
    if any_word(ENERGY_WORDS) or any_word(UTILITY_WORDS):
        return True

    # Consumer-only triggers (privacy, profiling) could be relevant for OCC
    if any_word(CONSUMER_WORDS):
        return True

    return False

def filter_relevant(bills: List[Dict]) -> List[Dict]:
    return [b for b in bills if is_relevant(b)]

def diff_against_state(relevant: List[Dict], state: Dict) -> Tuple[List[Dict], Dict[str, Dict]]:
    """Return (new_or_updated_bills, new_snapshot_dict)."""
    prev = state.get("bills", {})
    snapshot: Dict[str, Dict] = {}
    changed: List[Dict] = []

    for b in relevant:
        meta = {
            "title": b["title"],
            "status": b["status"],
            "category": b["category"],
            "url": b["url"],
        }
        snapshot[b["id"]] = meta

        if b["id"] not in prev or prev[b["id"]] != meta:
            changed.append(b)

    return changed, snapshot

def format_email(changed: List[Dict], total_relevant: int) -> str:
    lines = []
    lines.append("NCSL — AI + Energy/Utilities Legislation Digest")
    lines.append(NCSL_URL)
    lines.append("")
    lines.append(f"Total relevant AI+energy/utility bills on NCSL: {total_relevant}")
    lines.append("")

    if changed:
        lines.append(f"New or updated relevant bills since last digest ({len(changed)}):")
        lines.append("")
        for b in changed:
            lines.append(f"- {b['jurisdiction']} {b['bill_number']} — {b['title']}")
            lines.append(f"  Status: {b['status']}")
            lines.append(f"  Category: {b['category']}")
            lines.append(f"  Link: {b['url']}")
            lines.append("")
    else:
        lines.append("No new or updated relevant bills since the last digest.")
        lines.append("")

    return "\n".join(lines)

def send_email(subject: str, body: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_TO):
        print("Email not configured (missing SMTP_* or EMAIL_TO). Skipping email send.")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM or SMTP_USER
    msg["To"] = ", ".join(EMAIL_TO)

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(msg["From"], EMAIL_TO, msg.as_string())

def main():
    state = load_state()
    all_bills = fetch_all_bills()
    relevant = filter_relevant(all_bills)

    changed, snapshot = diff_against_state(relevant, state)

    if changed or FORCE_EMAIL:
        subject_suffix = f"{len(changed)} new/updated" if changed else "No changes (forced digest)"
        subject = f"[NCSL AI Energy Watch] {subject_suffix}"
        body = format_email(changed, total_relevant=len(relevant))
        print(body)
        send_email(subject, body)
        save_state(snapshot)
    else:
        print("No new or updated relevant bills; not sending email.")
        # Still update state, in case NCSL quietly changes descriptions/status
        save_state(snapshot)

if __name__ == "__main__":
    main()