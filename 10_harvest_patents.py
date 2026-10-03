"""
10_harvest_patents.py — Cardiac-Regenerative-Lab-Autocrawler
Virelion Biotech

Harvests patents matching the domain search terms in config.py from USPTO's
PatentsView API (v1). US-only coverage in v1 — see config.py's comment on
the PatentsView vs. Lens.org tradeoff.

IMPORTANT — unverified against a live API response: this sandbox's network
allowlist doesn't include search.patentsview.org, so unlike the parsing
logic (which is tested against realistic mocked responses matching
PatentsView's documented schema), the exact request/pagination mechanics
below have not been exercised against a live call. PatentsView's query
parameter format (q/f/o as JSON-encoded strings) has changed before across
API versions — worth a quick smoke test against the real API before relying
on this in a real run, and adjusting the pagination cursor logic if
PatentsView's current docs differ from what's implemented here.

Output: data/<year>/raw_patents.json

Usage:
    python 10_harvest_patents.py
"""

import json
import time
from typing import Any

import requests

import config

PATENTSVIEW_DELAY = 0.5
PATENTSVIEW_PAGE_SIZE = 100

PATENT_FIELDS = [
    "patent_id", "patent_title", "patent_abstract", "patent_date",
    "assignees.assignee_organization", "assignees.assignee_country",
    "inventors.inventor_name_first", "inventors.inventor_name_last",
]


def patentsview_search(term: str, page_size: int = PATENTSVIEW_PAGE_SIZE) -> list[dict[str, Any]]:
    """
    Paginates through PatentsView results for a single search term, matching
    against patent title or abstract.
    """
    records = []
    after = None
    headers = {}
    if config.USPTO_PATENTSVIEW_API_KEY:
        headers["X-Api-Key"] = config.USPTO_PATENTSVIEW_API_KEY

    while True:
        query = {
            "_or": [
                {"_text_any": {"patent_title": term}},
                {"_text_any": {"patent_abstract": term}},
            ]
        }
        options = {"size": page_size}
        if after:
            options["after"] = after

        params = {
            "q": json.dumps(query),
            "f": json.dumps(PATENT_FIELDS),
            "o": json.dumps(options),
        }

        resp = requests.get(config.USPTO_PATENTSVIEW_BASE, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        patents = data.get("patents", [])
        for p in patents:
            records.append(_parse_patent(p))

        time.sleep(PATENTSVIEW_DELAY)

        if len(patents) < page_size or not patents:
            break
        # Cursor for the next page — PatentsView v1 paginates by the last
        # result's sort key (defaults to patent_id). Verify this matches
        # current API docs; adjust if PatentsView expects a different cursor field.
        after = patents[-1].get("patent_id")

    return records


def _parse_patent(item: dict) -> dict[str, Any]:
    assignees = item.get("assignees", []) or []
    assignee_orgs = [a.get("assignee_organization", "") for a in assignees if a.get("assignee_organization")]
    assignee_countries = [a.get("assignee_country", "") for a in assignees if a.get("assignee_country")]

    inventors = []
    for inv in item.get("inventors", []) or []:
        first = inv.get("inventor_name_first", "")
        last = inv.get("inventor_name_last", "")
        if last:
            inventors.append({"first_name": first, "last_name": last})

    return {
        "source": "uspto_patentsview",
        "patent_id": item.get("patent_id", ""),
        "title": item.get("patent_title", ""),
        "abstract": item.get("patent_abstract", ""),
        "patent_date": item.get("patent_date", ""),
        "assignee_organizations": assignee_orgs,
        "assignee_countries": assignee_countries,
        "inventors": inventors,
    }


def main():
    config.ensure_run_dir()

    if not config.USPTO_PATENTSVIEW_API_KEY:
        print(
            "[warn] USPTO_PATENTSVIEW_API_KEY not set. PatentsView requires a free API key "
            "(register at https://patentsview.org/apis/keyrequest). Attempting requests "
            "without one — they will likely be rejected.\n"
        )

    print(f"Harvesting patents for {len(config.ALL_SEARCH_TERMS)} search terms...")
    all_records = []
    for term in config.ALL_SEARCH_TERMS:
        print(f"[{term}]")
        try:
            term_records = patentsview_search(term)
            for rec in term_records:
                rec["matched_term"] = term
            all_records.extend(term_records)
            print(f"  -> {len(term_records)} patent records")
        except requests.RequestException as e:
            print(f"  [warn] PatentsView search failed for '{term}': {e}")

    out_path = config.run_path("raw_patents")
    with open(out_path, "w") as f:
        json.dump(all_records, f, indent=2)

    print(f"\nDone. Wrote {len(all_records)} raw patent records (pre-dedup) to {out_path}")


if __name__ == "__main__":
    main()
