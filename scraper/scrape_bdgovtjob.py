"""
Scraper for https://bdgovtjob.net/

Fetches the newest job circular posts from the site's listing pages,
extracts structured fields matching the app's data schema, and merges
them into a local JSON store (data/jobs.json) without duplicating
jobs already seen.

Designed to be run repeatedly (e.g. every few hours via a scheduled
GitHub Actions workflow) rather than doing one giant historical crawl.
Before deploying this on a schedule, check the site's robots.txt and
Terms of Use, and keep request rates polite.
"""

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://bdgovtjob.net/"
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "jobs.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; JobCircularBot/1.0; "
        "+https://github.com/) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    )
}

# How many listing pages to check per run. The site is a live feed of
# new circulars, so a small number is enough to catch anything new
# since the last run; raise it once if you need to backfill history.
PAGES_PER_RUN = 3
REQUEST_DELAY_SECONDS = 2

BN_DIGITS = "০১২৩৪৫৬৭৮৯"
EN_DIGITS = "0123456789"
BN_TO_EN = str.maketrans(BN_DIGITS, EN_DIGITS)

BN_MONTHS = {
    "জানুয়ারি": 1, "ফেব্রুয়ারি": 2, "মার্চ": 3, "এপ্রিল": 4,
    "মে": 5, "জুন": 6, "জুলাই": 7, "আগস্ট": 8,
    "সেপ্টেম্বর": 9, "অক্টোবর": 10, "নভেম্বর": 11, "ডিসেম্বর": 12,
}


def bn_digits_to_en(text):
    return text.translate(BN_TO_EN) if text else text


def parse_bangla_date(raw):
    """Best-effort parse of a Bangla date string like
    '২৭ অক্টোবর ২০২৬ বিকাল ৪:০০ টা' into ISO 'YYYY-MM-DD'.
    Returns None if it can't be confidently parsed.
    """
    if not raw:
        return None
    text = bn_digits_to_en(raw)
    for month_name, month_num in BN_MONTHS.items():
        if month_name in text:
            match = re.search(r"(\d{1,2}).{0,20}" + month_name + r".{0,20}(\d{4})", text)
            if match:
                day, year = int(match.group(1)), int(match.group(2))
                try:
                    return f"{year:04d}-{month_num:02d}-{day:02d}"
                except ValueError:
                    return None
    return None


def extract_field(block_text, label):
    """Pull the value following a Bangla field label like
    'আবেদনের শেষ তারিখ:' out of a post's flattened text block.
    """
    pattern = re.escape(label) + r"\s*:?\s*\n?\s*([^\n]+)"
    match = re.search(pattern, block_text)
    return match.group(1).strip() if match else None


def job_id_from_url(url):
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return slug or url


def fetch_page(page_number):
    url = BASE_URL if page_number == 1 else urljoin(BASE_URL, f"page/{page_number}/")
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text, url


def parse_listing_page(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    articles = soup.find_all("article")
    if not articles:
        # Fallback for theme variations that don't use <article> for posts.
        articles = soup.select(".post, .type-post, .elementor-post")

    jobs = []
    for article in articles:
        heading = article.find(["h1", "h2", "h3"])
        link_tag = heading.find("a", href=True) if heading else article.find("a", href=True)
        if not link_tag:
            continue

        title = link_tag.get_text(strip=True)
        source_url = urljoin(page_url, link_tag["href"])
        block_text = article.get_text("\n", strip=True)

        deadline_raw = extract_field(block_text, "আবেদনের শেষ তারিখ")
        positions_raw = extract_field(block_text, "পদের সংখ্যা")
        categories_raw = extract_field(block_text, "পদের ক্যাটাগরি")

        time_tag = article.find("time")
        publish_date = None
        if time_tag and time_tag.get("datetime"):
            publish_date = time_tag["datetime"][:10]

        category_links = [
            a.get_text(strip=True)
            for a in article.find_all("a", href=re.compile(r"/category/"))
        ]

        positions_clean = bn_digits_to_en(positions_raw) if positions_raw else None

        jobs.append({
            "id": job_id_from_url(source_url),
            "title": title,
            "organization": None,  # requires a detail-page fetch to extract reliably
            "category": category_links[0] if category_links else None,
            "tags": category_links,
            "publish_date": publish_date,
            "deadline": parse_bangla_date(deadline_raw),
            "deadline_raw": deadline_raw,
            "positions_count": positions_clean,
            "position_categories": categories_raw,
            "source_url": source_url,
            "educational_requirements": None,  # requires a detail-page fetch
            "experience_years": None,
        })
    return jobs


def load_existing():
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return {job["id"]: job for job in json.load(f)}
    return {}


def save_jobs(jobs_by_id):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        jobs_by_id.values(),
        key=lambda j: j.get("publish_date") or "",
        reverse=True,
    )
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)


def run():
    existing = load_existing()
    new_count = 0

    for page_number in range(1, PAGES_PER_RUN + 1):
        try:
            html, page_url = fetch_page(page_number)
        except requests.RequestException as exc:
            print(f"Failed to fetch page {page_number}: {exc}", file=sys.stderr)
            break

        jobs = parse_listing_page(html, page_url)
        if not jobs:
            print(f"No posts found on page {page_number}; stopping.")
            break

        for job in jobs:
            if job["id"] not in existing:
                new_count += 1
            existing[job["id"]] = job

        time.sleep(REQUEST_DELAY_SECONDS)

    save_jobs(existing)
    print(f"Done. {new_count} new job(s) added. {len(existing)} total in store.")


if __name__ == "__main__":
    run()
