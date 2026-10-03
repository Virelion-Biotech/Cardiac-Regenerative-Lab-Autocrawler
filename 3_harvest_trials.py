"""
3_harvest_trials.py — Cardiac-Regenerative-Lab-Autocrawler
Virelion Biotech

Harvests trial records matching the domain search terms in config.py from
two sources:
    - ClinicalTrials.gov (API v2, US-centric but includes many international
      sites) — v1, fully live-tested.
    - EU Clinical Trials Information System (CTIS) — fast-follow, now
      implemented.

CTIS has a different schema and auth model from ClinicalTrials.gov v2, and
(like step 10's PatentsView integration) hasn't been exercised against a
live call in this sandbox, since euclinicaltrials.eu isn't in the network
allowlist. Parsing logic is written against CTIS's documented public search
schema — smoke test against the real API before relying on this in a real
run, particularly the exact search endpoint path, which has moved across
CTIS releases (see config.py).

Output: data/<year>/raw_trials.json
    A list of trial records, each tagged with its source and matched term.

Usage:
    python 3_harvest_trials.py
"""

import json
import time
from typing import Any

import requests

import config

CTGOV_DELAY = 0.3
CTGOV_PAGE_SIZE = 100  # ClinicalTrials.gov v2 API max pageSize

CTIS_DELAY = 0.5
CTIS_PAGE_SIZE = 50


def ctgov_search(term: str, page_size: int = CTGOV_PAGE_SIZE) -> list[dict[str, Any]]:
    """
    Paginates through ClinicalTrials.gov v2 API results for a single search
    term. Uses the API's cursor-based pageToken pagination.
    """
    records = []
    page_token = None

    fields = ",".join([
        "NCTId", "BriefTitle", "OfficialTitle", "OverallStatus", "Phase",
        "Condition", "InterventionName", "InterventionType",
        "LeadSponsorName", "ResponsiblePartyInvestigatorFullName",
        "ResponsiblePartyInvestigatorAffiliation", "LocationFacility",
        "LocationCity", "LocationCountry", "StartDate", "CompletionDate",
        "BriefSummary",
    ])

    while True:
        params = {
            "query.term": term,
            "fields": fields,
            "pageSize": page_size,
            "format": "json",
        }
        if page_token:
            params["pageToken"] = page_token

        resp = requests.get(config.CLINICALTRIALS_BASE, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        studies = data.get("studies", [])
        for study in studies:
            records.append(_parse_ctgov_study(study))

        page_token = data.get("nextPageToken")
        time.sleep(CTGOV_DELAY)

        if not page_token or not studies:
            break

    return records


def _parse_ctgov_study(study: dict) -> dict[str, Any]:
    protocol = study.get("protocolSection", {})

    ident = protocol.get("identificationModule", {})
    status_mod = protocol.get("statusModule", {})
    sponsor_mod = protocol.get("sponsorCollaboratorsModule", {})
    design_mod = protocol.get("designModule", {})
    conditions_mod = protocol.get("conditionsModule", {})
    interventions_mod = protocol.get("armsInterventionsModule", {})
    contacts_mod = protocol.get("contactsLocationsModule", {})
    description_mod = protocol.get("descriptionModule", {})

    locations = []
    for loc in contacts_mod.get("locations", []) or []:
        locations.append({
            "facility": loc.get("facility", ""),
            "city": loc.get("city", ""),
            "country": loc.get("country", ""),
        })

    investigators = []
    for official in contacts_mod.get("overallOfficials", []) or []:
        investigators.append({
            "name": official.get("name", ""),
            "affiliation": official.get("affiliation", ""),
            "role": official.get("role", ""),
        })

    interventions = [
        i.get("name", "") for i in interventions_mod.get("interventions", []) or []
    ]

    return {
        "source": "clinicaltrials_gov",
        "nct_id": ident.get("nctId", ""),
        "title": ident.get("briefTitle", ""),
        "official_title": ident.get("officialTitle", ""),
        "status": status_mod.get("overallStatus", ""),
        "phase": (design_mod.get("phases") or [""])[0],
        "conditions": conditions_mod.get("conditions", []),
        "interventions": interventions,
        "lead_sponsor": sponsor_mod.get("leadSponsor", {}).get("name", ""),
        "investigators": investigators,
        "locations": locations,
        "start_date": status_mod.get("startDateStruct", {}).get("date", ""),
        "completion_date": status_mod.get("completionDateStruct", {}).get("date", ""),
        "brief_summary": description_mod.get("briefSummary", ""),
    }


# ---------------------------------------------------------------------------
# EU Clinical Trials Information System (CTIS)
# ---------------------------------------------------------------------------

def ctis_search(term: str, page_size: int = CTIS_PAGE_SIZE) -> list[dict[str, Any]]:
    """
    Paginates through CTIS's public search results for a single search term.
    CTIS's public API is offset/page based and returns trial "applications"
    rather than ClinicalTrials.gov's flatter "studies" — each application
    can bundle multiple per-member-state authorizations, which are collapsed
    here into a single record per EU CT number (see _parse_ctis_application).
    """
    records = []
    page = 0
    headers = {"Content-Type": "application/json"}

    while True:
        body = {
            "searchCriteria": {"containAll": term},
            "pagination": {"page": page, "size": page_size},
        }
        resp = requests.post(config.CTIS_SEARCH_BASE, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        applications = data.get("data", []) or data.get("content", [])
        for app in applications:
            records.append(_parse_ctis_application(app))

        total_pages = data.get("totalPages", page + 1)
        time.sleep(CTIS_DELAY)

        if page + 1 >= total_pages or not applications:
            break
        page += 1

    return records


def _parse_ctis_application(app: dict) -> dict[str, Any]:
    locations = []
    countries = set()
    for member_state in app.get("authorizedApplications", app.get("memberStatesConcerned", [])) or []:
        country = member_state.get("country") if isinstance(member_state, dict) else member_state
        if country:
            countries.add(country)
            locations.append({"facility": "", "city": "", "country": country})

    sponsor = app.get("sponsor", {}) or {}

    return {
        "source": "eu_ctis",
        "nct_id": app.get("ctNumber", ""),  # EU CT number, kept in the nct_id field for a single downstream schema
        "title": app.get("title", ""),
        "official_title": app.get("publicTitle", app.get("title", "")),
        "status": app.get("overallStatus", app.get("ctStatus", "")),
        "phase": app.get("trialPhase", ""),
        "conditions": app.get("therapeuticAreas", []) or app.get("medicalConditions", []),
        "interventions": [p.get("name", "") for p in app.get("investigationalMedicinalProducts", []) or []],
        "lead_sponsor": sponsor.get("name", ""),
        "investigators": [],  # CTIS's public search doesn't expose individual investigator names
        "locations": locations,
        "start_date": app.get("trialStartDate", ""),
        "completion_date": app.get("trialEndDate", ""),
        "brief_summary": app.get("primaryObjective", app.get("shortDescription", "")),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def harvest_term(term: str) -> list[dict[str, Any]]:
    results = []

    print(f"  ClinicalTrials.gov: searching '{term}'")
    try:
        ct_results = ctgov_search(term)
        results.extend(ct_results)
        print(f"    -> {len(ct_results)} trial records")
    except requests.RequestException as e:
        print(f"    [warn] ClinicalTrials.gov search failed for '{term}': {e}")

    print(f"  EU CTIS: searching '{term}'")
    try:
        ctis_results = ctis_search(term)
        results.extend(ctis_results)
        print(f"    -> {len(ctis_results)} EU CTIS trial records")
    except requests.RequestException as e:
        print(f"    [warn] EU CTIS search failed for '{term}': {e}")

    return results


def main():
    config.ensure_run_dir()

    print(f"Harvesting trials for {len(config.ALL_SEARCH_TERMS)} search terms across "
          f"ClinicalTrials.gov and EU CTIS...\n")

    all_records = []
    for term in config.ALL_SEARCH_TERMS:
        print(f"[{term}]")
        term_records = harvest_term(term)
        for rec in term_records:
            rec["matched_term"] = term
        all_records.extend(term_records)

    out_path = config.run_path("raw_trials")
    with open(out_path, "w") as f:
        json.dump(all_records, f, indent=2)

    print(f"\nDone. Wrote {len(all_records)} raw trial records (pre-dedup) to {out_path}")


if __name__ == "__main__":
    main()
