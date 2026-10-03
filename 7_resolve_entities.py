"""
7_resolve_entities.py — Cardiac-Regenerative-Lab-Autocrawler
Virelion Biotech

Merges the per-record lab profile candidates from step 6 (one candidate per
paper/grant/trial/crawled page) into one canonical profile per real PI/lab.
A single active lab typically appears many times across sources — e.g.
"J. Wu", "Joseph Wu", and "Joseph C. Wu" at Stanford should collapse into one
record, not three.

Matching strategy, in priority order:
    1. Exact ORCID match (when present — rare in our current sources, since
       none of steps 1-3 do an ORCID lookup yet; this is here so resolution
       improves automatically once/if that's added).
    2. Exact email match (same caveat — rarely populated currently).
    3. Normalized name + institution similarity (the main path in practice):
       same last name, matching first name or first initial, and a fuzzy
       institution match above a threshold.

Uses a union-find structure so matches are transitive: if A matches B and
B matches C, all three merge into one cluster even if A and C weren't
directly compared as similar enough.

Output: data/<year>/labs_deduped.json
    One merged profile per resolved entity, with all source references and
    name variants preserved for traceability.

Usage:
    python 7_resolve_entities.py
"""

import json
import re
from difflib import SequenceMatcher
from typing import Any

import config

INSTITUTION_SIMILARITY_THRESHOLD = 0.6

# Must match the order used in step 6 — kept as a local constant here since
# resolution ranks translational stage independently of extraction.
TRANSLATIONAL_STAGE_ORDER = [
    "Discovery/Basic Science",
    "Preclinical (Small Animal)",
    "Preclinical (Large Animal)",
    "Clinical Trial Phase I",
    "Clinical Trial Phase II",
    "Clinical Trial Phase III",
]

INSTITUTION_STOPWORDS = {
    "university", "univ", "institute", "inst", "the", "of", "college",
    "hospital", "medical", "center", "centre", "school",
}

NAME_TITLES = {"dr", "dr.", "prof", "prof.", "professor", "md", "phd", "m.d.", "ph.d."}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[.,]", "", name)
    tokens = [t for t in name.split() if t not in NAME_TITLES]
    return " ".join(tokens)


def name_parts(name: str) -> tuple[str, str]:
    """Returns (first_initial, last_name) from a normalized name, for matching."""
    tokens = normalize_name(name).split()
    if not tokens:
        return "", ""
    last_name = tokens[-1]
    first_initial = tokens[0][0] if tokens[0] else ""
    return first_initial, last_name


def normalize_institution(inst: str) -> str:
    inst = inst.lower().strip()
    inst = re.sub(r"[^a-z0-9\s]", "", inst)
    tokens = [t for t in inst.split() if t not in INSTITUTION_STOPWORDS]
    return " ".join(tokens)


def institutions_match(a: str, b: str) -> bool:
    """
    True if two institution strings likely refer to the same organization.
    Full-string similarity alone misses a very common case: a university and
    one of its named sub-institutes (e.g. "Stanford University" vs "Stanford
    Cardiovascular Institute") — these share a distinctive core token but are
    otherwise different strings. So this also matches on any shared token of
    4+ characters after stopword removal, which catches that case at the
    cost of some risk of over-merging labs that happen to share a city name
    (e.g. "Osaka University" / "Osaka City Hospital"). Given this pipeline
    prioritizes exhaustive recall over precision (a human reviews the
    output), that tradeoff is acceptable here but is worth knowing about.
    """
    if not a or not b:
        return False
    na, nb = normalize_institution(a), normalize_institution(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if SequenceMatcher(None, na, nb).ratio() >= INSTITUTION_SIMILARITY_THRESHOLD:
        return True

    tokens_a, tokens_b = set(na.split()), set(nb.split())
    shared_distinctive_tokens = {t for t in (tokens_a & tokens_b) if len(t) >= 4}
    return bool(shared_distinctive_tokens)


def names_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    fi_a, last_a = name_parts(a)
    fi_b, last_b = name_parts(b)
    if not last_a or not last_b or last_a != last_b:
        return False
    # Same last name; match if first initials agree (covers "J. Wu" vs "Joseph Wu")
    return fi_a == fi_b


# ---------------------------------------------------------------------------
# Union-Find for transitive clustering
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def cluster_candidates(candidates: list[dict[str, Any]]) -> list[list[int]]:
    n = len(candidates)
    uf = UnionFind(n)

    for i in range(n):
        for j in range(i + 1, n):
            a, b = candidates[i], candidates[j]

            if a.get("orcid") and b.get("orcid") and a["orcid"] == b["orcid"]:
                uf.union(i, j)
                continue

            if a.get("email") and b.get("email") and a["email"].lower() == b["email"].lower():
                uf.union(i, j)
                continue

            if names_match(a.get("pi_full_name", ""), b.get("pi_full_name", "")) and \
               institutions_match(a.get("institution", ""), b.get("institution", "")):
                uf.union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        root = uf.find(i)
        clusters.setdefault(root, []).append(i)

    return list(clusters.values())


# ---------------------------------------------------------------------------
# Merging a cluster into one canonical profile
# ---------------------------------------------------------------------------

def _pick_longest(values: list[str]) -> str:
    non_empty = [v for v in values if v]
    return max(non_empty, key=len) if non_empty else ""

def _pick_first_nonempty(values: list[str]) -> str:
    for v in values:
        if v:
            return v
    return ""

def _pick_first_truthy(values: list[Any]) -> Any:
    """Like _pick_first_nonempty, but for non-string values (e.g. the
    geo_coordinates dict) where '' isn't the right falsy sentinel to check."""
    for v in values:
        if v:
            return v
    return None

def _union_lists(lists: list[list[str]]) -> list[str]:
    seen = []
    for lst in lists:
        for item in lst or []:
            if item not in seen:
                seen.append(item)
    return seen

def _best_translational_stage(stages: list[str]) -> str:
    ranked = [s for s in stages if s in TRANSLATIONAL_STAGE_ORDER]
    if not ranked:
        return _pick_first_nonempty(stages)
    return max(ranked, key=TRANSLATIONAL_STAGE_ORDER.index)


def merge_cluster(indices: list[int], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    members = [candidates[i] for i in indices]

    all_names = [m.get("pi_full_name", "") for m in members]
    canonical_name = _pick_longest(all_names)
    aka_names = sorted(set(n for n in all_names if n and n != canonical_name))

    total_funding = sum(
        m["metrics"]["grant_funding_usd"]
        for m in members
        if m.get("metrics", {}).get("grant_funding_usd") is not None
    ) or None

    all_trial_ids = _union_lists([m.get("metrics", {}).get("clinical_trial_ids", []) for m in members])
    all_patent_ids = _union_lists([m.get("metrics", {}).get("patent_ids", []) for m in members])

    return {
        "pi_full_name": canonical_name,
        "pi_canonical_name": canonical_name,
        "aka_names": aka_names,
        "orcid": _pick_first_nonempty([m.get("orcid", "") for m in members]),
        "email": _pick_first_nonempty([m.get("email", "") for m in members]),
        "institutional_profile_url": _pick_first_nonempty([m.get("institutional_profile_url", "") for m in members]),
        "department": _pick_first_nonempty([m.get("department", "") for m in members]),
        "institution": _pick_longest([m.get("institution", "") for m in members]),
        "city": _pick_first_nonempty([m.get("city", "") for m in members]),
        "country": _pick_first_nonempty([m.get("country", "") for m in members]),
        # geo_coordinates is a {"lat": float, "lng": float} dict (or None) from step 6's
        # Nominatim lookup — pick the first member that actually has one rather than
        # coercing to a string, so downstream consumers get real numeric coordinates.
        "geo_coordinates": _pick_first_truthy([m.get("geo_coordinates") for m in members]),
        "research_focus": {
            "cell_gene_sources": _union_lists([m.get("research_focus", {}).get("cell_gene_sources", []) for m in members]),
            "constructs_bioengineering": _union_lists([m.get("research_focus", {}).get("constructs_bioengineering", []) for m in members]),
            "functional_vectors": _union_lists([m.get("research_focus", {}).get("functional_vectors", []) for m in members]),
        },
        "experimental_models": _union_lists([m.get("experimental_models", []) for m in members]),
        "translational_stage": _best_translational_stage([m.get("translational_stage", "") for m in members]),
        "digital_footprint": {
            "lab_url": _pick_first_nonempty([m.get("digital_footprint", {}).get("lab_url", "") for m in members]),
            "google_scholar": _pick_first_nonempty([m.get("digital_footprint", {}).get("google_scholar", "") for m in members]),
            "latest_pub_doi": _pick_first_nonempty([m.get("digital_footprint", {}).get("latest_pub_doi", "") for m in members]),
            "contact": _pick_first_nonempty([m.get("digital_footprint", {}).get("contact", "") for m in members]),
        },
        "metrics": {
            "grant_funding_usd": total_funding,
            "clinical_trial_ids": all_trial_ids,
            "patent_ids": all_patent_ids,
        },
        "source_record_count": len(members),
        "source_record_types": sorted(set(m.get("source_record_type", "") for m in members if m.get("source_record_type"))),
        "source_references": [m.get("source_reference", {}) for m in members],
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    config.ensure_run_dir()

    in_path = config.run_path("labs_extracted")
    if not in_path.exists():
        print(f"{in_path.name} not found — run step 6 first.")
        return

    with open(in_path) as f:
        candidates = json.load(f)

    print(f"Resolving entities across {len(candidates)} extracted candidates...")
    clusters = cluster_candidates(candidates)
    print(f"  -> resolved into {len(clusters)} distinct labs")

    merged = [merge_cluster(indices, candidates) for indices in clusters]

    # Sort by number of contributing source records (proxy for how well-evidenced a lab is)
    merged.sort(key=lambda lab: lab["source_record_count"], reverse=True)

    out_path = config.run_path("labs_deduped")
    with open(out_path, "w") as f:
        json.dump(merged, f, indent=2)

    multi_source = sum(1 for lab in merged if lab["source_record_count"] > 1)
    geocoded = sum(1 for lab in merged if lab.get("geo_coordinates"))
    print(f"  -> {multi_source} labs were confirmed by more than one source record")
    print(f"  -> {geocoded} labs carry geo_coordinates from step 6's geocoding")
    print(f"\nDone. Wrote {len(merged)} deduplicated lab profiles to {out_path}")


if __name__ == "__main__":
    main()
