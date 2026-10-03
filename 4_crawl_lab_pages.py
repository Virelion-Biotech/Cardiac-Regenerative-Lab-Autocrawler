"""
4_crawl_lab_pages.py — Cardiac-Regenerative-Lab-Autocrawler
Virelion Biotech

Crawls the public, non-gated institutional directory pages listed in
config.CRAWL_SEED_DIRECTORIES: fetches each seed page, discovers likely
faculty/lab profile links on it, and fetches those pages' text content for
downstream LLM extraction (step 6).

Deliberately scoped to public university/institute directories only —
gated society membership directories are excluded pending a per-source ToS
review (see config.py comments). This script will find nothing until
CRAWL_SEED_DIRECTORIES is populated with real URLs.

Respects robots.txt, rate-limits per domain, and identifies itself with a
clear user-agent and contact string (see config.CRAWL_USER_AGENT).

Output: data/<year>/raw_lab_pages.json
    A list of {url, source_directory, title, text} records.

Usage:
    python 4_crawl_lab_pages.py
"""

import json
import re
import time
import urllib.robotparser
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config

# Tracks last request time per domain, for rate limiting
_last_request_time: dict[str, float] = {}

# Cache of robots.txt parsers per domain, so we don't refetch it per page
_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}

# Heuristic keywords suggesting a link points to a faculty/lab profile page
PROFILE_LINK_HINTS = [
    "faculty", "profile", "people", "lab", "laboratory", "pi", "staff",
    "researcher", "directory",
]


def _get_robots_parser(domain: str) -> urllib.robotparser.RobotFileParser:
    if domain not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"https://{domain}/robots.txt")
        try:
            rp.read()
        except Exception:
            # If robots.txt can't be fetched, be conservative and treat as allowed
            # only for the seed page itself; unexpected errors here shouldn't crash the run.
            pass
        _robots_cache[domain] = rp
    return _robots_cache[domain]


def _is_allowed(url: str) -> bool:
    if not config.CRAWL_RESPECT_ROBOTS_TXT:
        return True
    domain = urlparse(url).netloc
    rp = _get_robots_parser(domain)
    try:
        return rp.can_fetch(config.CRAWL_USER_AGENT, url)
    except Exception:
        return True


def _rate_limit(domain: str):
    last = _last_request_time.get(domain, 0)
    elapsed = time.time() - last
    wait = config.CRAWL_REQUEST_DELAY_SECONDS - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_request_time[domain] = time.time()


def fetch_page(url: str) -> requests.Response | None:
    """Fetches a URL, respecting robots.txt and per-domain rate limiting. Returns None if disallowed or failed."""
    domain = urlparse(url).netloc

    if not _is_allowed(url):
        print(f"    [skip] disallowed by robots.txt: {url}")
        return None

    _rate_limit(domain)

    headers = {"User-Agent": config.CRAWL_USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=config.CRAWL_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        print(f"    [warn] fetch failed for {url}: {e}")
        return None


def discover_profile_links(seed_url: str, html: str, max_links: int) -> list[str]:
    """
    Finds links on a directory page that look like they lead to individual
    faculty/lab profile pages, based on URL and link-text heuristics.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(seed_url, href)

        # Only follow links on the same domain as the seed page
        if urlparse(full_url).netloc != urlparse(seed_url).netloc:
            continue

        link_text = (a.get_text() or "").strip().lower()
        href_lower = href.lower()

        if any(hint in href_lower or hint in link_text for hint in PROFILE_LINK_HINTS):
            if full_url not in seen:
                seen.add(full_url)
                candidates.append(full_url)

        if len(candidates) >= max_links:
            break

    return candidates


def extract_page_text(html: str) -> tuple[str, str]:
    """Returns (title, visible_text) from a page's HTML, stripping scripts/styles/nav noise."""
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text().strip() if soup.title else ""

    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return title, text


def crawl_seed(seed_url: str) -> list[dict[str, Any]]:
    """Crawls a single seed directory page and its discovered profile links."""
    records = []

    print(f"  Seed: {seed_url}")
    resp = fetch_page(seed_url)
    if resp is None:
        return records

    profile_links = discover_profile_links(seed_url, resp.text, config.CRAWL_MAX_PAGES_PER_DOMAIN)
    print(f"    -> discovered {len(profile_links)} candidate profile links")

    for link in profile_links:
        page_resp = fetch_page(link)
        if page_resp is None:
            continue
        title, text = extract_page_text(page_resp.text)
        if len(text) < 200:
            # Too short to be a useful profile page — likely a nav stub or redirect
            continue
        records.append({
            "source": "web_crawl",
            "url": link,
            "source_directory": seed_url,
            "title": title,
            "text": text[:20000],  # cap per-page text to keep the LLM extraction step's input manageable
        })

    return records


def main():
    config.ensure_run_dir()

    if not config.CRAWL_SEED_DIRECTORIES:
        print(
            "No seed directories configured in config.CRAWL_SEED_DIRECTORIES.\n"
            "Add real, public, non-gated university/institute directory URLs there "
            "before running this script. Writing an empty raw_lab_pages.json so the "
            "rest of the pipeline can still run."
        )
        out_path = config.run_path("raw_lab_pages")
        with open(out_path, "w") as f:
            json.dump([], f, indent=2)
        return

    all_records = []
    for seed_url in config.CRAWL_SEED_DIRECTORIES:
        all_records.extend(crawl_seed(seed_url))

    out_path = config.run_path("raw_lab_pages")
    with open(out_path, "w") as f:
        json.dump(all_records, f, indent=2)

    print(f"\nDone. Wrote {len(all_records)} crawled pages to {out_path}")


if __name__ == "__main__":
    main()
