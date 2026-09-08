"""
6_extract_structured.py — Cardiac-Regenerative-Lab-Autocrawler
Virelion Biotech

Converts filtered.json (step 5's output) into structured lab profile
candidates matching the extraction schema from the README.

Design: for papers/grants/trials, PI names, institution, and location are
already available as structured fields from steps 1-3's parsing — pulling
those out deterministically is more reliable than asking an LLM to re-derive
facts it doesn't need to guess at. Claude is used for the parts that
genuinely require judgment: categorizing research focus (cell/gene sources,
bioengineering constructs, functional vectors), translational stage, and
experimental model systems from free text — and, for crawled lab pages
(unstructured HTML text with no pre-parsed fields), extracting identity and
affiliation fields too.

Geocoding (fast-follow, now wired up): once city/country are known (either
pre-parsed or Claude-extracted), each candidate is geocoded via Nominatim
into geo_coordinates. Geocoding is cached per (city, country) pair within a
run and rate-limited to Nominatim's usage-policy ceiling of 1 request/sec,
since many candidates share the same institution's city.

Output: data/<year>/labs_extracted.json
    A list of lab profile candidates (not yet deduplicated — that's step 7).

Usage:
    python 6_extract_structured.py
"""

import json
import time
from typing import Any

import anthropic
import requests

import config

BATCH_SIZE = 5  # smaller than step 5's batch size — schema per item is larger
API_RETRY_DELAY = 2.0
MAX_RETRIES = 2
BATCH_POLL_INTERVAL_SECONDS = 20

TRANSLATIONAL_STAGES = [
    "Discovery/Basic Science",
    "Preclinical (Small Animal)",
    "Preclinical (Large Animal)",
    "Clinical Trial Phase I",
    "Clinical Trial Phase II",
    "Clinical Trial Phase III",
]

EXTRACTION_SCHEMA_INSTRUCTIONS = f"""
For each item, produce a lab profile object with exactly these fields:
- "id": the item id exactly as given
- "pi_full_name": string (use the known PI name if provided; otherwise extract from text; empty string if unknown)
- "department": string (empty if unknown)
- "institution": string (use known institution if provided; otherwise extract from text)
- "city": string (empty if unknown)
- "country": string (empty if unknown)
- "cell_gene_sources": array of strings, from: hiPSC-CM, ESC-CM, direct reprogramming, cardiac progenitor cells, mRNA therapy, other (only include ones actually evidenced in the text)
- "constructs_bioengineering": array of strings, from: engineered heart tissue, cardiac patch, 3D bioprinting, decellularized matrix, other
- "functional_vectors": array of strings, from: biological pacing, electromechanical integration, arrhythmia mitigation, vascularization, metabolic maturation, other
- "experimental_models": array of strings, from: in vitro, small animal, large animal
- "translational_stage": exactly one of {TRANSLATIONAL_STAGES}
- "lab_url": string, only if a URL is present in the source data (empty otherwise — do not guess a URL)
- "contact": string, only if an email or contact is present in the source data (empty otherwise)

Only include a category tag if the text actually provides evidence for it. Do not guess.
""".strip()


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Deterministic pre-extraction of known fields (papers / grants / trials)
# ---------------------------------------------------------------------------

def _known_fields_for_record(rec: dict[str, Any]) -> dict[str, Any]:
    """
    Pulls out PI name / institution / location fields that are already
    structured from steps 1-3, so Claude doesn't have to re-derive facts
    that are already known with certainty. Returns {} for lab_pages, which
    have no pre-parsed structured fields.
    """
    record_type = rec.get("record_type")

    if record_type == "papers":
        authors = rec.get("authors", [])
        pi_name = ""
        institution = ""
        if authors:
            # Use the last-listed author as a heuristic for senior/PI author
            # (common convention in biomedical papers); true PI resolution
            # happens properly in step 7 via entity resolution.
            last_author = authors[-1]
            pi_name = f"{last_author.get('fore_name', '')} {last_author.get('last_name', '')}".strip()
            institution = last_author.get("affiliation", "")
        return {"pi_full_name": pi_name, "institution": institution, "doi": rec.get("doi", "")}

    if record_type == "grants":
        pis = rec.get("principal_investigators", [])
        pi_name = pis[0].get("full_name", "") if pis else rec.get("contact_pi_name", "")
        return {
            "pi_full_name": pi_name,
            "institution": rec.get("org_name", ""),
            "city": rec.get("org_city", ""),
            "country": rec.get("org_country", ""),
            "award_amount": rec.get("award_amount"),
            "project_num": rec.get("project_num", ""),
        }

    if record_type == "trials":
        investigators = rec.get("investigators", [])
        pi_name = investigators[0].get("name", "") if investigators else ""
        institution = investigators[0].get("affiliation", "") if investigators else rec.get("lead_sponsor", "")
        locations = rec.get("locations", [])
        city = locations[0].get("city", "") if locations else ""
        country = locations[0].get("country", "") if locations else ""
        return {
            "pi_full_name": pi_name,
            "institution": institution,
            "city": city,
            "country": country,
            "nct_id": rec.get("nct_id", ""),
        }

    if record_type == "patents":
        inventors = rec.get("inventors", [])
        pi_name = ""
        if inventors:
            first = inventors[0]
            pi_name = f"{first.get('first_name', '')} {first.get('last_name', '')}".strip()
        orgs = rec.get("assignee_organizations", [])
        countries = rec.get("assignee_countries", [])
        return {
            "pi_full_name": pi_name,
            "institution": orgs[0] if orgs else "",
            "country": countries[0] if countries else "",
            "patent_id": rec.get("patent_id", ""),
        }

    return {}  # lab_pages: nothing pre-parsed, Claude extracts everything


def _record_year(rec: dict[str, Any]) -> int | None:
    """
    Extracts a single year of 'most recent activity' from a raw record, for
    step 8's recency scoring. Each source type stores dates differently:
    papers have pub_year directly; grants use fiscal_year; trials don't have
    a single clean year field, so the later of start/completion date is used
    (a trial still recruiting or completing soon is more 'active' evidence
    than one that started years ago). Crawled lab pages have no reliable
    date at all and are left undated (they still count as evidence of an
    active lab, just not for recency scoring).
    """
    record_type = rec.get("record_type")

    if record_type == "papers":
        year = rec.get("pub_year", "")
        return int(year) if str(year).strip().isdigit() else None

    if record_type == "grants":
        year = rec.get("fiscal_year", "")
        try:
            return int(year)
        except (TypeError, ValueError):
            return None

    if record_type == "trials":
        for date_field in ("completion_date", "start_date"):
            date_str = rec.get(date_field, "") or ""
            match = date_str[:4]
            if match.isdigit():
                return int(match)
        return None

    if record_type == "patents":
        date_str = rec.get("patent_date", "") or ""
        return int(date_str[:4]) if date_str[:4].isdigit() else None

    return None  # lab_pages: no reliable date


def _text_for_record(rec: dict[str, Any]) -> str:
    record_type = rec.get("record_type")
    if record_type in ("papers", "grants", "patents"):
        return f"{rec.get('title', '')}\n\n{rec.get('abstract', '')}"
    if record_type == "trials":
        return f"{rec.get('title', '')}\n\n{rec.get('brief_summary', '')}"
    if record_type == "lab_pages":
        return f"{rec.get('title', '')}\n\n{rec.get('text', '')}"
    return ""


# ---------------------------------------------------------------------------
# Geocoding (Nominatim)
# ---------------------------------------------------------------------------

# Cache of (normalized city, normalized country) -> {"lat": float, "lng": float} | None,
# scoped to a single run. Many candidates share the same institution's city, so this
# avoids redundant geocoding calls (and keeps us comfortably under Nominatim's 1 req/sec cap).
_geocode_cache: dict[tuple[str, str], dict[str, float] | None] = {}
_last_geocode_request_time = 0.0


def _geocode_rate_limit():
    global _last_geocode_request_time
    elapsed = time.time() - _last_geocode_request_time
    wait = config.GEOCODE_DELAY_SECONDS - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_geocode_request_time = time.time()


def geocode_city_country(city: str, country: str) -> dict[str, float] | None:
    """
    Looks up (city, country) via Nominatim and returns {"lat": ..., "lng": ...},
    or None if either field is empty, the lookup returns no results, or the
    request fails. Failures are logged but never raise — geocoding is a
    nice-to-have enrichment, not a blocker for the rest of the pipeline.
    """
    city, country = (city or "").strip(), (country or "").strip()
    if not city and not country:
        return None

    key = (city.lower(), country.lower())
    if key in _geocode_cache:
        return _geocode_cache[key]

    query = ", ".join(part for part in (city, country) if part)
    params = {"q": query, "format": "json", "limit": 1}
    headers = {"User-Agent": config.GEOCODE_USER_AGENT}

    result = None
    try:
        _geocode_rate_limit()
        resp = requests.get(config.NOMINATIM_BASE, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        matches = resp.json()
        if matches:
            result = {"lat": float(matches[0]["lat"]), "lng": float(matches[0]["lon"])}
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        print(f"    [warn] geocoding failed for '{query}': {e}")

    _geocode_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------

def build_items_block(batch: list[dict[str, Any]]) -> str:
    items_block = ""
    for c in batch:
        known = c["known_fields"]
        known_str = ", ".join(f"{k}={v}" for k, v in known.items() if v) or "(none pre-known)"
        items_block += f"\n\n--- ITEM {c['id']} ---\nKnown fields: {known_str}\nText:\n{c['text']}"
    return items_block.strip()


# System prompt is identical across every batch call, so it's cached
# (cache_control) rather than re-billed on every one of potentially
# hundreds of batch calls — Anthropic charges ~10% of base input price on
# cache hits after the first call.
SYSTEM_PROMPT = (
    "You are extracting structured lab profile data for a cardiac regeneration research database.\n\n"
    f"{EXTRACTION_SCHEMA_INSTRUCTIONS}\n\n"
    'Where "Known fields" are given for an item, use them for pi_full_name/institution/city/country '
    "rather than re-deriving them, unless the text clearly contradicts them.\n\n"
    "Respond with ONLY a JSON array, no other text, no markdown fences."
)


def _system_block() -> list[dict[str, Any]]:
    return [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]


def _parse_extractions(raw_text: str) -> dict[str, dict[str, Any]] | None:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        extracted = json.loads(text)
        return {e["id"]: e for e in extracted if "id" in e}
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def extract_batch_sync(client: anthropic.Anthropic, batch: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Synchronous fallback for batches that fail via the Batch API (rare) — full price, not the 50% batch rate."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=config.CLAUDE_MAX_TOKENS,
                system=_system_block(),
                messages=[{"role": "user", "content": build_items_block(batch)}],
            )
            raw_text = "".join(b.text for b in response.content if b.type == "text")
            extractions = _parse_extractions(raw_text)
            if extractions is not None:
                return extractions
            print(f"    [warn] sync retry attempt {attempt + 1}: unparseable response")
        except anthropic.APIError as e:
            print(f"    [warn] sync retry attempt {attempt + 1} failed: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(API_RETRY_DELAY)

    print(f"    [warn] giving up on batch after {MAX_RETRIES + 1} sync attempts — "
          f"{len(batch)} items will be flagged for manual review")
    return {c["id"]: {"id": c["id"], "_extraction_failed": True} for c in batch}


def submit_batch_job(client: anthropic.Anthropic, batches: list[list[dict[str, Any]]]) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    requests_ = []
    custom_id_map = {}
    for i, batch in enumerate(batches):
        custom_id = f"batch_{i}"
        custom_id_map[custom_id] = batch
        requests_.append({
            "custom_id": custom_id,
            "params": {
                "model": config.CLAUDE_MODEL,
                "max_tokens": config.CLAUDE_MAX_TOKENS,
                "system": _system_block(),
                "messages": [{"role": "user", "content": build_items_block(batch)}],
            },
        })
    batch_job = client.messages.batches.create(requests=requests_)
    return batch_job.id, custom_id_map


def wait_for_batch_job(client: anthropic.Anthropic, batch_id: str):
    print(f"  Submitted Batch API job {batch_id}. Waiting for completion "
          f"(Anthropic's Batch API can take anywhere from minutes to ~24h)...")
    while True:
        batch_job = client.messages.batches.retrieve(batch_id)
        counts = batch_job.request_counts
        print(f"    status={batch_job.processing_status} | "
              f"succeeded={counts.succeeded} errored={counts.errored} "
              f"processing={counts.processing} canceled={counts.canceled} expired={counts.expired}")
        if batch_job.processing_status == "ended":
            return batch_job
        time.sleep(BATCH_POLL_INTERVAL_SECONDS)


def fetch_batch_results(client: anthropic.Anthropic, batch_id: str) -> dict[str, str | None]:
    results = {}
    for entry in client.messages.batches.results(batch_id):
        if entry.result.type == "succeeded":
            raw_text = "".join(b.text for b in entry.result.message.content if b.type == "text")
            results[entry.custom_id] = raw_text
        else:
            results[entry.custom_id] = None
    return results


def extract_all(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    client = _client()
    batches = [candidates[i:i + BATCH_SIZE] for i in range(0, len(candidates), BATCH_SIZE)]

    print(f"Submitting {len(batches)} batches ({len(candidates)} items) as one Batch API job "
          f"(50% cheaper than synchronous calls)...")
    batch_id, custom_id_map = submit_batch_job(client, batches)
    wait_for_batch_job(client, batch_id)
    raw_results = fetch_batch_results(client, batch_id)

    all_extractions = {}
    failed_batches = []
    for custom_id, batch in custom_id_map.items():
        raw_text = raw_results.get(custom_id)
        extractions = _parse_extractions(raw_text) if raw_text else None
        if extractions is None:
            failed_batches.append(batch)
        else:
            all_extractions.update(extractions)

    if failed_batches:
        print(f"  {len(failed_batches)} batch(es) didn't come back parseable via the Batch API — "
              f"retrying those synchronously...")
        for batch in failed_batches:
            all_extractions.update(extract_batch_sync(client, batch))

    return all_extractions


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    config.check_required_env_vars()
    config.ensure_run_dir()

    filtered_path = config.run_path("filtered")
    if not filtered_path.exists():
        print(f"{filtered_path.name} not found — run step 5 first.")
        return

    with open(filtered_path) as f:
        filtered_records = json.load(f)

    print(f"Preparing {len(filtered_records)} filtered records for extraction...")
    candidates = []
    for i, rec in enumerate(filtered_records):
        candidates.append({
            "id": f"item_{i}",
            "source_index": i,
            "known_fields": _known_fields_for_record(rec),
            "text": _text_for_record(rec)[:4000],
        })

    all_extractions = extract_all(candidates)

    print("Geocoding city/country pairs via Nominatim (rate-limited to "
          f"{1 / config.GEOCODE_DELAY_SECONDS:.0f} req/sec)...")

    labs_extracted = []
    needs_review = []
    geocoded_count = 0

    for c in candidates:
        extraction = all_extractions.get(c["id"])
        original = filtered_records[c["source_index"]]

        if extraction is None or extraction.get("_extraction_failed"):
            needs_review.append(original)
            continue

        geo_coordinates = geocode_city_country(extraction.get("city", ""), extraction.get("country", ""))
        if geo_coordinates is not None:
            geocoded_count += 1

        labs_extracted.append({
            "pi_full_name": extraction.get("pi_full_name", ""),
            "pi_canonical_name": extraction.get("pi_full_name", ""),  # true canonicalization happens in step 7
            "orcid": "",
            "email": extraction.get("contact", ""),
            "institutional_profile_url": extraction.get("lab_url", ""),
            "department": extraction.get("department", ""),
            "institution": extraction.get("institution", ""),
            "city": extraction.get("city", ""),
            "country": extraction.get("country", ""),
            "geo_coordinates": geo_coordinates,
            "research_focus": {
                "cell_gene_sources": extraction.get("cell_gene_sources", []),
                "constructs_bioengineering": extraction.get("constructs_bioengineering", []),
                "functional_vectors": extraction.get("functional_vectors", []),
            },
            "experimental_models": extraction.get("experimental_models", []),
            "translational_stage": extraction.get("translational_stage", ""),
            "digital_footprint": {
                "lab_url": extraction.get("lab_url", ""),
                "google_scholar": "",
                "latest_pub_doi": original.get("doi", ""),
                "contact": extraction.get("contact", ""),
            },
            "metrics": {
                "grant_funding_usd": original.get("award_amount"),
                "clinical_trial_ids": [original["nct_id"]] if original.get("nct_id") else [],
                "patent_ids": [original["patent_id"]] if original.get("patent_id") else [],
            },
            "source_record_type": original.get("record_type", ""),
            "source_reference": {
                "title": original.get("title", ""),
                "doi": original.get("doi", ""),
                "project_num": original.get("project_num", ""),
                "nct_id": original.get("nct_id", ""),
                "patent_id": original.get("patent_id", ""),
                "url": original.get("url", ""),
                "year": _record_year(original),
            },
        })

    out_path = config.run_path("labs_extracted")
    with open(out_path, "w") as f:
        json.dump(labs_extracted, f, indent=2)

    if needs_review:
        review_path = config.CURRENT_RUN_DIR / "extraction_needs_review.json"
        with open(review_path, "w") as f:
            json.dump(needs_review, f, indent=2)
        print(f"\n[!] {len(needs_review)} items failed extraction and were written to {review_path}")

    print(f"  Geocoded {geocoded_count}/{len(labs_extracted)} candidates "
          f"({len(_geocode_cache)} distinct city/country pairs looked up)")
    print(f"\nDone. Extracted {len(labs_extracted)} lab profile candidates to {out_path}")


if __name__ == "__main__":
    main()
