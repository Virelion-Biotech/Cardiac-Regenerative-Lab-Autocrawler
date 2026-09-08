"""
9_export_csv.py — Cardiac-Regenerative-Lab-Autocrawler
Virelion Biotech

Final step: flattens labs_final.json into a spreadsheet-friendly CSV, and —
if a prior year's run exists on disk — generates a human-readable
year-over-year diff summary (new labs, funding shifts, newly inactive labs,
institutional migrations).

Diffing reuses the same PI/institution matching logic as step 7's entity
resolution (ORCID/email exact match, or name+institution fuzzy match), so a
lab is recognized as "the same lab" across years using the same rules that
deduplicated it within a single year. That module is dynamically imported
since its filename starts with a digit and isn't a valid `import` target.

Migration matching (fast-follow, now more robust): the original version only
matched cross-year records when either ORCID/email was present, or
institution matched — which meant a PI who moved to a genuinely different
institution with no shared identifying token, and who has no ORCID/email on
file (the common case, per step 7's comments), was invisible as a migration
and silently showed up as a "new lab" instead. Two independent,
institution-agnostic identity anchors are now added ahead of the
name+institution fallback: an exact Google Scholar profile URL match, and an
exact lab/personal homepage URL match — both are stable across an
institutional move in a way "institution" itself isn't, and both are already
harvested into digital_footprint by steps 6/7. This still isn't a complete
fix (a PI with no ORCID, no Scholar profile, and no captured lab URL who
also changes their name's matching pattern is still missed), but it
meaningfully narrows the gap described above without requiring new data
collection.

Output:
    data/<year>/labs_report.csv
    data/<year>/annual_diff_summary.md (only if a prior year's data exists)

Usage:
    python 9_export_csv.py
"""

import csv
import importlib.util
import json
from pathlib import Path
from typing import Any

import config


def _load_sibling_module(filename: str):
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolve_entities = _load_sibling_module("7_resolve_entities.py")


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "pi_full_name", "aka_names", "orcid", "email", "institution", "department",
    "city", "country", "institutional_profile_url", "lab_url", "google_scholar",
    "contact", "translational_stage", "cell_gene_sources", "constructs_bioengineering",
    "functional_vectors", "experimental_models", "grant_funding_usd",
    "clinical_trial_ids", "patent_ids", "avi_status", "avi_score", "most_recent_activity_year",
    "electromechanical_risk_profile", "citation_impact", "industry_spinoff_affiliation",
    "member_count", "source_record_types",
]


def _join(values) -> str:
    return "; ".join(values) if values else ""


def flatten_lab(lab: dict[str, Any]) -> dict[str, str]:
    focus = lab.get("research_focus", {})
    footprint = lab.get("digital_footprint", {})
    metrics = lab.get("metrics", {})
    avi = lab.get("activity_verification_index", {})
    risk = lab.get("risk_flags", {})
    scale = lab.get("translation_scale_metrics", {})

    return {
        "pi_full_name": lab.get("pi_full_name", ""),
        "aka_names": _join(lab.get("aka_names", [])),
        "orcid": lab.get("orcid", ""),
        "email": lab.get("email", ""),
        "institution": lab.get("institution", ""),
        "department": lab.get("department", ""),
        "city": lab.get("city", ""),
        "country": lab.get("country", ""),
        "institutional_profile_url": lab.get("institutional_profile_url", ""),
        "lab_url": footprint.get("lab_url", ""),
        "google_scholar": footprint.get("google_scholar", ""),
        "contact": footprint.get("contact", ""),
        "translational_stage": lab.get("translational_stage", ""),
        "cell_gene_sources": _join(focus.get("cell_gene_sources", [])),
        "constructs_bioengineering": _join(focus.get("constructs_bioengineering", [])),
        "functional_vectors": _join(focus.get("functional_vectors", [])),
        "experimental_models": _join(lab.get("experimental_models", [])),
        "grant_funding_usd": metrics.get("grant_funding_usd", "") or "",
        "clinical_trial_ids": _join(metrics.get("clinical_trial_ids", [])),
        "patent_ids": _join(metrics.get("patent_ids", [])),
        "avi_status": avi.get("status", ""),
        "avi_score": avi.get("avi_score", "") if avi.get("avi_score") is not None else "",
        "most_recent_activity_year": avi.get("most_recent_activity_year", "") or "",
        "electromechanical_risk_profile": "Yes" if risk.get("electromechanical_risk_profile") else "No",
        "citation_impact": scale.get("citation_impact", "") if scale.get("citation_impact") is not None else "",
        "industry_spinoff_affiliation": "Yes" if scale.get("industry_spinoff_affiliation") else (
            "" if scale.get("industry_spinoff_affiliation") is None else "No"
        ),
        "member_count": lab.get("member_count", ""),
        "source_record_types": _join(lab.get("source_record_types", [])),
    }


def export_csv(labs: list[dict[str, Any]], out_path: Path):
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for lab in labs:
            writer.writerow(flatten_lab(lab))


# ---------------------------------------------------------------------------
# Year-over-year diff
# ---------------------------------------------------------------------------

def _labs_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """
    Identity check across two annual runs, in priority order of how stable
    each signal is across an institutional move:
      1. ORCID (rarely populated currently, but the strongest signal when present)
      2. Email
      3. Google Scholar profile URL (institution-agnostic — persists across a move)
      4. Lab/personal homepage URL (usually institution-agnostic in practice,
         though a PI who takes their old URL's exact path with them to a new
         host would still match; a URL that's reissued to someone else at the
         old institution is the main failure mode, and is rare in practice)
      5. Name + institution fuzzy match (the original v1 fallback — the only
         path that requires institution to stay the same, which is exactly
         the case a genuine migration violates)
    """
    orcid_a, orcid_b = a.get("orcid", ""), b.get("orcid", "")
    if orcid_a and orcid_b and orcid_a == orcid_b:
        return True

    email_a, email_b = a.get("email", "").strip().lower(), b.get("email", "").strip().lower()
    if email_a and email_b and email_a == email_b:
        return True

    scholar_a = a.get("digital_footprint", {}).get("google_scholar", "").strip().lower()
    scholar_b = b.get("digital_footprint", {}).get("google_scholar", "").strip().lower()
    if scholar_a and scholar_b and scholar_a == scholar_b:
        return True

    lab_url_a = a.get("digital_footprint", {}).get("lab_url", "").strip().lower()
    lab_url_b = b.get("digital_footprint", {}).get("lab_url", "").strip().lower()
    if lab_url_a and lab_url_b and lab_url_a == lab_url_b:
        return True

    return (
        resolve_entities.names_match(a.get("pi_full_name", ""), b.get("pi_full_name", ""))
        and resolve_entities.institutions_match(a.get("institution", ""), b.get("institution", ""))
    )


def _matched_via_institution_agnostic_signal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """
    True if two records matched via a signal that doesn't require the
    institution to stay the same (ORCID/email/Scholar/lab URL) — used to
    decide whether an institution change for this pair is real migration
    evidence, versus just two coincidentally-matched name+institution
    records where "migration" wouldn't even make sense to check.
    """
    orcid_a, orcid_b = a.get("orcid", ""), b.get("orcid", "")
    if orcid_a and orcid_b and orcid_a == orcid_b:
        return True
    email_a, email_b = a.get("email", "").strip().lower(), b.get("email", "").strip().lower()
    if email_a and email_b and email_a == email_b:
        return True
    scholar_a = a.get("digital_footprint", {}).get("google_scholar", "").strip().lower()
    scholar_b = b.get("digital_footprint", {}).get("google_scholar", "").strip().lower()
    if scholar_a and scholar_b and scholar_a == scholar_b:
        return True
    lab_url_a = a.get("digital_footprint", {}).get("lab_url", "").strip().lower()
    lab_url_b = b.get("digital_footprint", {}).get("lab_url", "").strip().lower()
    if lab_url_a and lab_url_b and lab_url_a == lab_url_b:
        return True
    return False


def match_against_prior(current_labs: list[dict], prior_labs: list[dict]) -> list[tuple[dict, dict | None]]:
    """Returns (current_lab, matched_prior_lab_or_None) pairs, one-to-one on the prior side."""
    used_prior_indices = set()
    pairs = []

    for cur in current_labs:
        match = None
        for i, prior in enumerate(prior_labs):
            if i in used_prior_indices:
                continue
            if _labs_match(cur, prior):
                match = prior
                used_prior_indices.add(i)
                break
        pairs.append((cur, match))

    return pairs


def build_diff_summary(current_labs: list[dict], prior_labs: list[dict], prior_year_label: str) -> str:
    pairs = match_against_prior(current_labs, prior_labs)

    new_labs = [cur for cur, match in pairs if match is None]

    funding_shifts = []
    newly_inactive = []
    migrations = []

    for cur, prior in pairs:
        if prior is None:
            continue

        cur_funding = cur.get("metrics", {}).get("grant_funding_usd")
        prior_funding = prior.get("metrics", {}).get("grant_funding_usd")
        if cur_funding and prior_funding and cur_funding != prior_funding:
            delta = cur_funding - prior_funding
            if abs(delta) >= 50000 or (prior_funding and abs(delta / prior_funding) >= 0.2):
                funding_shifts.append((cur, prior_funding, cur_funding, delta))

        cur_status = cur.get("activity_verification_index", {}).get("status", "")
        prior_status = prior.get("activity_verification_index", {}).get("status", "")
        if cur_status == "Inactive/Legacy" and prior_status != "Inactive/Legacy":
            newly_inactive.append(cur)

        cur_inst = cur.get("institution", "")
        prior_inst = prior.get("institution", "")
        if cur_inst and prior_inst and cur_inst != prior_inst and \
           not resolve_entities.institutions_match(cur_inst, prior_inst):
            # Only counted as migration evidence if the pair was matched via an
            # institution-agnostic signal — otherwise the pair wouldn't have
            # matched at all under a genuine institution change (see _labs_match).
            confidence = "high" if _matched_via_institution_agnostic_signal(cur, prior) else "name-match only"
            migrations.append((cur, prior_inst, cur_inst, confidence))

    lines = [
        f"# Annual Diff Summary — {config.CURRENT_YEAR} vs. {prior_year_label}",
        "",
        f"Current run: {len(current_labs)} labs. Prior run: {len(prior_labs)} labs.",
        "",
        f"## 🆕 Newly Discovered Labs ({len(new_labs)})",
        "",
    ]
    if new_labs:
        for lab in new_labs:
            lines.append(f"- **{lab.get('pi_full_name', 'Unknown')}** — {lab.get('institution', 'Unknown institution')}")
    else:
        lines.append("_None this year._")

    lines += ["", f"## 📈 Significant Funding Shifts ({len(funding_shifts)})", ""]
    if funding_shifts:
        for lab, prev, cur, delta in funding_shifts:
            direction = "up" if delta > 0 else "down"
            lines.append(
                f"- **{lab.get('pi_full_name', 'Unknown')}** ({lab.get('institution', '')}): "
                f"${prev:,.0f} → ${cur:,.0f} ({direction} ${abs(delta):,.0f})"
            )
    else:
        lines.append("_No significant funding changes detected._")

    lines += ["", f"## ⚠️ Newly Inactive Labs ({len(newly_inactive)})", ""]
    if newly_inactive:
        for lab in newly_inactive:
            year = lab.get("activity_verification_index", {}).get("most_recent_activity_year", "unknown")
            lines.append(f"- **{lab.get('pi_full_name', 'Unknown')}** ({lab.get('institution', '')}) — last activity: {year}")
    else:
        lines.append("_No labs newly flagged inactive this year._")

    lines += ["", f"## 🏛️ Possible Institutional Migrations ({len(migrations)})", ""]
    if migrations:
        for lab, prev_inst, cur_inst, confidence in migrations:
            lines.append(f"- **{lab.get('pi_full_name', 'Unknown')}**: {prev_inst} → {cur_inst} _(confidence: {confidence})_")
    else:
        lines.append("_None detected._")
    lines.append("")
    lines.append(
        "_Migrations are only detectable for PIs matched across years via ORCID, email, a Google Scholar "
        "profile URL, or a captured lab URL — 'name-match only' entries above matched on institution too, "
        "which shouldn't happen for a genuine institution change and likely indicates a coincidental "
        "name+institution collision rather than a real move. A PI with none of those stable identifiers on "
        "file who also moves institutions will still show up as a new lab instead — see README for the "
        "underlying data gap (no reliable identifier is harvested for every source in v1)._"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    config.ensure_run_dir()

    in_path = config.run_path("labs_final")
    if not in_path.exists():
        print(f"{in_path.name} not found — run step 8 first.")
        return

    with open(in_path) as f:
        current_labs = json.load(f)

    csv_path = config.run_path("labs_report_csv")
    export_csv(current_labs, csv_path)
    print(f"Wrote {len(current_labs)} labs to {csv_path}")

    prior_dir = config.get_previous_run_dir()
    if prior_dir is None:
        print("No prior year's data found — skipping diff summary (this appears to be the first run).")
        return

    prior_final_path = prior_dir / config.FILES["labs_final"]
    if not prior_final_path.exists():
        print(f"Prior year directory {prior_dir.name} exists but has no labs_final.json — skipping diff.")
        return

    with open(prior_final_path) as f:
        prior_labs = json.load(f)

    print(f"Diffing against {prior_dir.name} ({len(prior_labs)} prior labs)...")
    summary = build_diff_summary(current_labs, prior_labs, prior_dir.name)

    summary_path = config.CURRENT_RUN_DIR / config.FILES["annual_diff_summary"]
    with open(summary_path, "w") as f:
        f.write(summary)

    print(f"Wrote diff summary to {summary_path}")


if __name__ == "__main__":
    main()
