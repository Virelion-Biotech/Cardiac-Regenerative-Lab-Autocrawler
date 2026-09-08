"""
8_score_and_enrich.py — Cardiac-Regenerative-Lab-Autocrawler
Virelion Biotech

Scores and enriches each deduplicated lab profile from step 7:

- Activity Verification Index (AVI): a recency-based status derived from the
  most recent dated source evidence (paper pub_year, grant fiscal_year, or
  trial start/completion year) attached to the lab during step 6/7.
  Categorizes each lab as Active / Aging / Inactive-Legacy / Unknown, using
  the windows defined in config.py (AVI_ACTIVE_WINDOW_MONTHS,
  AVI_INACTIVE_THRESHOLD_MONTHS).
- Funding totals: already summed in step 7 (grant_funding_usd) — carried
  through here unchanged.
- Electromechanical Risk Profile Tagging: flags labs whose research_focus
  includes electromechanical integration or arrhythmia mitigation vectors.
- Citation impact (fast-follow, now wired up): batch-looked-up via the
  Semantic Scholar Graph API, keyed off each lab's latest_pub_doi.
- Industry/startup spinoff affiliation (fast-follow, now wired up): no
  dedicated spinoff-affiliation data source exists, so this uses a heuristic
  proxy — cross-referencing each lab's patent_ids (from step 10) against
  raw_patents.json's assignee organizations, and flagging labs whose patents
  are assigned to a non-academic-looking organization distinct from their
  own institution. This is a proxy signal for a human reviewer, not a
  verified affiliation, and is labeled as such in the output.

Output: data/<year>/labs_final.json

Usage:
    python 8_score_and_enrich.py
"""

import json
import time
from datetime import datetime
from typing import Any

import requests

import config

CURRENT_YEAR = datetime.now().year

RISK_TAG_VECTORS = {"electromechanical integration", "arrhythmia mitigation"}


# ---------------------------------------------------------------------------
# Activity Verification Index
# ---------------------------------------------------------------------------

def _most_recent_year(lab: dict[str, Any]) -> int | None:
    years = [
        ref.get("year") for ref in lab.get("source_references", [])
        if isinstance(ref.get("year"), int)
    ]
    return max(years) if years else None


def compute_avi(lab: dict[str, Any]) -> dict[str, Any]:
    most_recent_year = _most_recent_year(lab)

    if most_recent_year is None:
        return {
            "status": "Unknown",
            "most_recent_activity_year": None,
            "months_since_last_activity": None,
            "avi_score": None,
            "note": "No dated source evidence (e.g. lab only appears via crawled pages, which aren't dated).",
        }

    months_since = (CURRENT_YEAR - most_recent_year) * 12

    if months_since <= config.AVI_ACTIVE_WINDOW_MONTHS:
        status = "Active"
    elif months_since <= config.AVI_INACTIVE_THRESHOLD_MONTHS:
        status = "Aging"
    else:
        status = "Inactive/Legacy"

    # Simple linear score: 100 at zero months since activity, 0 at the
    # inactive threshold and beyond. A rough, comparative measure only —
    # not a precision instrument, given year-level date granularity.
    avi_score = max(0, round(100 - (months_since / config.AVI_INACTIVE_THRESHOLD_MONTHS) * 100))

    return {
        "status": status,
        "most_recent_activity_year": most_recent_year,
        "months_since_last_activity": months_since,
        "avi_score": avi_score,
        "note": "",
    }


# ---------------------------------------------------------------------------
# Risk tagging
# ---------------------------------------------------------------------------

def compute_risk_flags(lab: dict[str, Any]) -> dict[str, Any]:
    vectors = set(lab.get("research_focus", {}).get("functional_vectors", []))
    matched = sorted(vectors & RISK_TAG_VECTORS)
    return {
        "electromechanical_risk_profile": bool(matched),
        "matched_risk_vectors": matched,
    }


# ---------------------------------------------------------------------------
# Citation impact (Semantic Scholar)
# ---------------------------------------------------------------------------

def fetch_citation_counts(dois: list[str]) -> dict[str, int]:
    """
    Batch-looks-up citationCount for a list of DOIs via Semantic Scholar's
    /paper/batch endpoint. Runs in chunks of config.SEMANTIC_SCHOLAR_BATCH_SIZE
    (S2's documented batch limit), politely rate-limited between chunks.
    Missing/unrecognized DOIs are simply absent from the returned dict rather
    than raising — S2's coverage isn't exhaustive, especially for very recent
    papers or non-journal sources.
    """
    counts: dict[str, int] = {}
    if not dois:
        return counts

    headers = {"Content-Type": "application/json"}
    if config.SEMANTIC_SCHOLAR_API_KEY:
        headers["x-api-key"] = config.SEMANTIC_SCHOLAR_API_KEY

    url = f"{config.SEMANTIC_SCHOLAR_BASE}/paper/batch"
    params = {"fields": "externalIds,citationCount"}

    for i in range(0, len(dois), config.SEMANTIC_SCHOLAR_BATCH_SIZE):
        chunk = dois[i:i + config.SEMANTIC_SCHOLAR_BATCH_SIZE]
        ids = [f"DOI:{doi}" for doi in chunk]
        try:
            resp = requests.post(url, params=params, headers=headers, json={"ids": ids}, timeout=30)
            resp.raise_for_status()
            results = resp.json()
            for doi, entry in zip(chunk, results):
                if entry and entry.get("citationCount") is not None:
                    counts[doi] = entry["citationCount"]
        except requests.RequestException as e:
            print(f"    [warn] Semantic Scholar batch lookup failed for a chunk of {len(chunk)} DOIs: {e}")

        time.sleep(config.SEMANTIC_SCHOLAR_DELAY_SECONDS)

    return counts


# ---------------------------------------------------------------------------
# Industry / startup spinoff affiliation heuristic
# ---------------------------------------------------------------------------

def _load_raw_patents() -> dict[str, dict[str, Any]]:
    """Loads raw_patents.json (step 10's output) into a {patent_id: record} lookup."""
    path = config.run_path("raw_patents")
    if not path.exists():
        return {}
    with open(path) as f:
        patents = json.load(f)
    return {p.get("patent_id", ""): p for p in patents if p.get("patent_id")}


def _looks_academic(org: str) -> bool:
    org_lower = org.lower()
    return any(keyword in org_lower for keyword in config.SPINOFF_ACADEMIC_ORG_KEYWORDS)


def _orgs_distinct(assignee_org: str, lab_institution: str) -> bool:
    """True if the assignee org doesn't look like the same entity as the lab's
    institution — cheap token-overlap check, not full institution matching
    (avoiding a cross-module import for a heuristic this approximate)."""
    a = set(assignee_org.lower().split())
    b = set(lab_institution.lower().split())
    if not a or not b:
        return True
    return not (a & b)


def compute_industry_spinoff_flag(lab: dict[str, Any], patents_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    patent_ids = lab.get("metrics", {}).get("patent_ids", [])
    if not patent_ids:
        return {
            "flag": None,
            "matched_organizations": [],
            "note": "No associated patents to evaluate — not computed rather than assumed false.",
        }

    lab_institution = lab.get("institution", "")
    matched_orgs = set()

    for patent_id in patent_ids:
        patent = patents_by_id.get(patent_id)
        if not patent:
            continue
        for org in patent.get("assignee_organizations", []) or []:
            if org and not _looks_academic(org) and _orgs_distinct(org, lab_institution):
                matched_orgs.add(org)

    return {
        "flag": bool(matched_orgs),
        "matched_organizations": sorted(matched_orgs),
        "note": "Heuristic proxy based on patent assignee organizations, not a verified spinoff/affiliation database.",
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    config.ensure_run_dir()

    in_path = config.run_path("labs_deduped")
    if not in_path.exists():
        print(f"{in_path.name} not found — run step 7 first.")
        return

    with open(in_path) as f:
        labs = json.load(f)

    print(f"Scoring and enriching {len(labs)} deduplicated lab profiles...")

    all_dois = sorted({
        lab.get("digital_footprint", {}).get("latest_pub_doi", "")
        for lab in labs
        if lab.get("digital_footprint", {}).get("latest_pub_doi")
    })
    print(f"  Looking up citation counts for {len(all_dois)} distinct DOIs via Semantic Scholar...")
    citation_counts = fetch_citation_counts(all_dois)
    print(f"  -> found citation counts for {len(citation_counts)}/{len(all_dois)} DOIs")

    patents_by_id = _load_raw_patents()

    final = []
    status_counts: dict[str, int] = {}
    spinoff_flagged = 0

    for lab in labs:
        avi = compute_avi(lab)
        risk = compute_risk_flags(lab)
        status_counts[avi["status"]] = status_counts.get(avi["status"], 0) + 1

        doi = lab.get("digital_footprint", {}).get("latest_pub_doi", "")
        citation_impact = citation_counts.get(doi) if doi else None

        spinoff = compute_industry_spinoff_flag(lab, patents_by_id)
        if spinoff["flag"]:
            spinoff_flagged += 1

        enriched = {
            **lab,
            "activity_verification_index": avi,
            "risk_flags": risk,
            "translation_scale_metrics": {
                "grant_funding_usd": lab.get("metrics", {}).get("grant_funding_usd"),
                "citation_impact": citation_impact,
                "citation_impact_note": "" if doi else "No DOI available for citation lookup.",
                "industry_spinoff_affiliation": spinoff["flag"],
                "industry_spinoff_matched_organizations": spinoff["matched_organizations"],
                "industry_spinoff_note": spinoff["note"],
            },
        }
        final.append(enriched)

    # Sort by AVI score (most active first), unscored labs last
    final.sort(key=lambda lab: (lab["activity_verification_index"]["avi_score"] is None,
                                 -(lab["activity_verification_index"]["avi_score"] or 0)))

    out_path = config.run_path("labs_final")
    with open(out_path, "w") as f:
        json.dump(final, f, indent=2)

    print("  Activity status breakdown:")
    for status, count in sorted(status_counts.items(), key=lambda kv: -kv[1]):
        print(f"    {status}: {count}")

    risk_count = sum(1 for lab in final if lab["risk_flags"]["electromechanical_risk_profile"])
    print(f"  {risk_count} labs flagged with an electromechanical risk profile")
    print(f"  {spinoff_flagged} labs flagged with a possible industry/startup spinoff affiliation (heuristic)")

    print(f"\nDone. Wrote {len(final)} scored/enriched lab profiles to {out_path}")


if __name__ == "__main__":
    main()
