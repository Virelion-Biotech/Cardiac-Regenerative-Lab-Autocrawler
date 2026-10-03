"""
11_export_funding_leaderboard.py — Cardiac-Regenerative-Lab-Autocrawler
Virelion Biotech

Reads labs_final.json (already scored by step 8) and publishes two
standalone leaderboards: top-funded individual labs, and a funding rollup
by institution (since several PIs at the same institution should show up
as that institution's combined footprint, not just as separate rows).

No new harvesting needed — all data already exists in labs_final.json from
NIH RePORTER via step 2.

Output:
    data/<year>/funding_leaderboard.csv               (lab-level, sorted by funding desc)
    data/<year>/funding_leaderboard_institutions.csv   (institution-level rollup)

Usage:
    python 11_export_funding_leaderboard.py
"""

import csv
import json

import config


def build_lab_leaderboard(labs: list[dict]) -> list[dict]:
    funded = [lab for lab in labs if lab.get("metrics", {}).get("grant_funding_usd")]
    funded.sort(key=lambda lab: lab["metrics"]["grant_funding_usd"], reverse=True)

    rows = []
    for rank, lab in enumerate(funded, start=1):
        rows.append({
            "rank": rank,
            "pi_full_name": lab.get("pi_full_name", ""),
            "institution": lab.get("institution", ""),
            "country": lab.get("country", ""),
            "grant_funding_usd": lab["metrics"]["grant_funding_usd"],
            "translational_stage": lab.get("translational_stage", ""),
            "avi_status": lab.get("activity_verification_index", {}).get("status", ""),
        })
    return rows


def build_institution_rollup(labs: list[dict]) -> list[dict]:
    totals: dict[str, dict] = {}
    for lab in labs:
        inst = lab.get("institution", "").strip()
        funding = lab.get("metrics", {}).get("grant_funding_usd")
        if not inst:
            continue
        entry = totals.setdefault(inst, {"institution": inst, "total_funding_usd": 0, "funded_lab_count": 0, "total_lab_count": 0})
        entry["total_lab_count"] += 1
        if funding:
            entry["total_funding_usd"] += funding
            entry["funded_lab_count"] += 1

    rollup = sorted(totals.values(), key=lambda e: e["total_funding_usd"], reverse=True)
    for rank, entry in enumerate(rollup, start=1):
        entry["rank"] = rank
    return rollup


def write_csv(rows: list[dict], fieldnames: list[str], out_path):
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    config.ensure_run_dir()

    in_path = config.run_path("labs_final")
    if not in_path.exists():
        print(f"{in_path.name} not found — run step 8 first.")
        return

    with open(in_path) as f:
        labs = json.load(f)

    lab_rows = build_lab_leaderboard(labs)
    lab_path = config.run_path("funding_leaderboard_csv")
    write_csv(lab_rows, ["rank", "pi_full_name", "institution", "country", "grant_funding_usd",
                          "translational_stage", "avi_status"], lab_path)
    print(f"Wrote {len(lab_rows)} funded labs to {lab_path}")

    inst_rows = build_institution_rollup(labs)
    inst_path = config.run_path("funding_leaderboard_institutions_csv")
    write_csv(inst_rows, ["rank", "institution", "total_funding_usd", "funded_lab_count", "total_lab_count"], inst_path)
    print(f"Wrote {len(inst_rows)} institution rollups to {inst_path}")

    if lab_rows:
        print(f"\nTop-funded lab: {lab_rows[0]['pi_full_name']} ({lab_rows[0]['institution']}) — "
              f"${lab_rows[0]['grant_funding_usd']:,.0f}")
    if inst_rows:
        print(f"Top-funded institution: {inst_rows[0]['institution']} — ${inst_rows[0]['total_funding_usd']:,.0f}")


if __name__ == "__main__":
    main()
