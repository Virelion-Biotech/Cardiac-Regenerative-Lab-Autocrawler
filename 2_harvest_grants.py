"""
2_harvest_grants.py — Cardiac-Regenerative-Lab-Autocrawler
Virelion Biotech

Harvests active/recent grant data matching the domain search terms in
config.py from three sources:
    - NIH RePORTER (US) — v1, fully live-tested.
    - UKRI Gateway to Research (UK) — fast-follow, now implemented.
    - EU Funding & Tenders Portal / Horizon Europe (EU) — fast-follow, now implemented.

The UKRI and Horizon Europe integrations have different auth models and
response schemas from NIH RePORTER, and (like step 10's PatentsView
integration) haven't been exercised against a live call in this sandbox,
since gtr.ukri.org and api.tech.ec.europa.eu aren't in the network
allowlist. Parsing logic is written against each API's documented schema
and mocked accordingly — smoke test against the real APIs before relying on
this in a real run. See config.py for endpoint/caveat details.

Output: data/<year>/raw_grants.json
    A list of grant records, each tagged with its source and matched term.

Usage:
    python 2_harvest_grants.py
"""

import json
import time
from datetime import datetime
from typing import Any

import requests

import config

REPORTER_DELAY = 0.5  # polite delay between paginated requests
REPORTER_PAGE_SIZE = 500  # NIH RePORTER's max limit per request
REPORTER_LOOKBACK_YEARS = 5  # how many recent fiscal years to search

GTR_DELAY = 0.5
GTR_PAGE_SIZE = 100

HORIZON_EUROPE_DELAY = 0.5
HORIZON_EUROPE_PAGE_SIZE = 100


def _recent_fiscal_years(n: int = REPORTER_LOOKBACK_YEARS) -> list[int]:
    current_year = datetime.now().year
    return list(range(current_year - n + 1, current_year + 1))


def nih_reporter_search(term: str, page_size: int = REPORTER_PAGE_SIZE) -> list[dict[str, Any]]:
    """
    Paginates through NIH RePORTER results for a single search term across
    recent fiscal years. Returns parsed grant records.
    """
    records = []
    offset = 0
    fiscal_years = _recent_fiscal_years()

    while True:
        body = {
            "criteria": {
                "advanced_text_search": {
                    "operator": "and",
                    "search_field": "projecttitle,abstracttext,terms",
                    "search_text": term,
                },
                "fiscal_years": fiscal_years,
            },
            "include_fields": [
                "ProjectNum", "ProjectTitle", "AbstractText", "FiscalYear",
                "AwardAmount", "OrgName", "OrgCity", "OrgState", "OrgCountry",
                "ContactPiName", "PrincipalInvestigators", "ProjectStartDate",
                "ProjectEndDate", "AgencyIcAdmin",
            ],
            "offset": offset,
            "limit": page_size,
        }

        resp = requests.post(config.NIH_REPORTER_BASE, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        for item in results:
            records.append(_parse_reporter_item(item))

        total = data.get("meta", {}).get("total", 0)
        offset += page_size
        time.sleep(REPORTER_DELAY)

        if offset >= total or not results:
            break

    return records


def _parse_reporter_item(item: dict) -> dict[str, Any]:
    pis = []
    for pi in item.get("principal_investigators", []) or []:
        pis.append({
            "full_name": pi.get("full_name", ""),
            "first_name": pi.get("first_name", ""),
            "last_name": pi.get("last_name", ""),
        })

    return {
        "source": "nih_reporter",
        "project_num": item.get("project_num", ""),
        "title": item.get("project_title", ""),
        "abstract": item.get("abstract_text", ""),
        "fiscal_year": item.get("fiscal_year", ""),
        "award_amount": item.get("award_amount", None),
        "org_name": item.get("org_name", ""),
        "org_city": item.get("org_city", ""),
        "org_state": item.get("org_state", ""),
        "org_country": item.get("org_country", ""),
        "contact_pi_name": item.get("contact_pi_name", ""),
        "principal_investigators": pis,
        "project_start_date": item.get("project_start_date", ""),
        "project_end_date": item.get("project_end_date", ""),
        "funding_agency": item.get("agency_ic_admin", {}).get("name", "") if item.get("agency_ic_admin") else "",
    }


# ---------------------------------------------------------------------------
# UKRI Gateway to Research (GtR)
# ---------------------------------------------------------------------------

def gtr_search(term: str, page_size: int = GTR_PAGE_SIZE) -> list[dict[str, Any]]:
    """
    Paginates through UKRI Gateway to Research project results for a single
    search term. GtR's json-v7 response embeds a "project" array plus a
    "totalPages" count; PI and organisation names are resolved via a
    secondary lookup on each project's "links" (see _resolve_gtr_links).
    """
    records = []
    page = 1
    headers = {"Accept": config.GTR_ACCEPT_HEADER}

    while True:
        params = {"q": term, "p": page, "s": page_size}
        resp = requests.get(config.GTR_PROJECTS_BASE, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        projects = data.get("project", [])
        if isinstance(projects, dict):  # GtR returns a bare object (not a list) for single-result pages
            projects = [projects]

        for item in projects:
            records.append(_parse_gtr_project(item))

        total_pages = data.get("totalPages", page)
        time.sleep(GTR_DELAY)

        if page >= total_pages or not projects:
            break
        page += 1

    return records


def _resolve_gtr_links(item: dict) -> dict[str, str]:
    """
    GtR represents the PI and lead organisation as hyperlinks (rel=PI_PER,
    rel=LEAD_ORG) rather than embedded fields, requiring a follow-up request
    per project to resolve names. To avoid one HTTP round-trip per grant
    record, this only follows links when the embedded link object already
    carries a "name"/"title" hint in its properties (GtR includes this for
    some but not all link types); otherwise the field is left blank rather
    than issuing an unbounded number of extra requests per search term.
    """
    resolved = {"pi_name": "", "lead_org": ""}
    for link in item.get("links", {}).get("link", []) or []:
        rel = link.get("rel", "")
        hint = link.get("title") or link.get("name") or ""
        if rel == "PI_PER" and hint:
            resolved["pi_name"] = hint
        elif rel == "LEAD_ORG" and hint:
            resolved["lead_org"] = hint
    return resolved


def _parse_gtr_project(item: dict) -> dict[str, Any]:
    fund = item.get("fund", {}) or {}
    value_pounds = (fund.get("valuePounds") or {}).get("amount")
    links = _resolve_gtr_links(item)

    return {
        "source": "ukri_gtr",
        "project_num": item.get("id", ""),
        "title": item.get("title", ""),
        "abstract": item.get("abstractText", ""),
        "fiscal_year": (fund.get("start") or "")[:4],
        # Converted at harvest time using a fixed approximate GBP->USD rate so
        # downstream funding totals (step 7/11) are comparable across
        # currencies. Flagged in the record itself so this approximation is
        # traceable and can be swapped for a live FX rate later.
        "award_amount": round(value_pounds * 1.27) if value_pounds else None,
        "award_amount_original_currency": "GBP",
        "award_amount_original_value": value_pounds,
        "org_name": links["lead_org"],
        "org_city": "",
        "org_state": "",
        "org_country": "United Kingdom",
        "contact_pi_name": links["pi_name"],
        "principal_investigators": [{"full_name": links["pi_name"], "first_name": "", "last_name": ""}] if links["pi_name"] else [],
        "project_start_date": fund.get("start", ""),
        "project_end_date": fund.get("end", ""),
        "funding_agency": item.get("leadFunder", ""),
    }


# ---------------------------------------------------------------------------
# Horizon Europe (EU Funding & Tenders Portal / SEDIA search API)
# ---------------------------------------------------------------------------

def horizon_europe_search(term: str, page_size: int = HORIZON_EUROPE_PAGE_SIZE) -> list[dict[str, Any]]:
    """
    Paginates through the EU Funding & Tenders Portal's SEDIA search API,
    scoped to funded Horizon Europe projects matching the search term.
    """
    records = []
    page_number = 1
    headers = {"Content-Type": "application/json", "apiKey": config.HORIZON_EUROPE_API_KEY}

    while True:
        body = {
            "query": term,
            "languages": ["en"],
            "type": "1",  # SEDIA's project-results content type
            "pageSize": page_size,
            "pageNumber": page_number,
        }
        resp = requests.post(config.HORIZON_EUROPE_SEARCH_BASE, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        for item in results:
            records.append(_parse_horizon_europe_item(item))

        total_results = data.get("totalResults", 0)
        time.sleep(HORIZON_EUROPE_DELAY)

        if page_number * page_size >= total_results or not results:
            break
        page_number += 1

    return records


def _parse_horizon_europe_item(item: dict) -> dict[str, Any]:
    metadata = item.get("metadata", {}) or {}

    def _first(field: str) -> str:
        values = metadata.get(field) or []
        return values[0] if values else ""

    coordinator = _first("coordinatorName")
    country = _first("coordinatorCountry")

    return {
        "source": "horizon_europe",
        "project_num": _first("projectNumber") or item.get("reference", ""),
        "title": _first("title"),
        "abstract": _first("objective") or _first("summary"),
        "fiscal_year": (_first("startDate") or "")[:4],
        "award_amount": _to_float(_first("ecMaxContribution")),
        "org_name": coordinator,
        "org_city": "",
        "org_state": "",
        "org_country": country,
        "contact_pi_name": "",  # SEDIA's project-results index doesn't expose an individual PI name
        "principal_investigators": [],
        "project_start_date": _first("startDate"),
        "project_end_date": _first("endDate"),
        "funding_agency": "European Commission (Horizon Europe)",
    }


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def harvest_term(term: str) -> list[dict[str, Any]]:
    results = []

    print(f"  NIH RePORTER: searching '{term}'")
    try:
        nih_results = nih_reporter_search(term)
        results.extend(nih_results)
        print(f"    -> {len(nih_results)} NIH grant records")
    except requests.RequestException as e:
        print(f"    [warn] NIH RePORTER search failed for '{term}': {e}")

    print(f"  UKRI Gateway to Research: searching '{term}'")
    try:
        gtr_results = gtr_search(term)
        results.extend(gtr_results)
        print(f"    -> {len(gtr_results)} UKRI grant records")
    except requests.RequestException as e:
        print(f"    [warn] UKRI GtR search failed for '{term}': {e}")

    print(f"  Horizon Europe (EU Funding & Tenders Portal): searching '{term}'")
    try:
        he_results = horizon_europe_search(term)
        results.extend(he_results)
        print(f"    -> {len(he_results)} Horizon Europe grant records")
    except requests.RequestException as e:
        print(f"    [warn] Horizon Europe search failed for '{term}': {e}")

    return results


def main():
    config.ensure_run_dir()

    print(f"Harvesting grants for {len(config.ALL_SEARCH_TERMS)} search terms across "
          f"NIH RePORTER, UKRI GtR, and Horizon Europe...\n")

    all_records = []
    for term in config.ALL_SEARCH_TERMS:
        print(f"[{term}]")
        term_records = harvest_term(term)
        for rec in term_records:
            rec["matched_term"] = term
        all_records.extend(term_records)

    out_path = config.run_path("raw_grants")
    with open(out_path, "w") as f:
        json.dump(all_records, f, indent=2)

    print(f"\nDone. Wrote {len(all_records)} raw grant records (pre-dedup) to {out_path}")


if __name__ == "__main__":
    main()
