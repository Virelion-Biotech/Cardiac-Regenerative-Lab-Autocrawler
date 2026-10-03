"""
5_classify_relevance.py — Cardiac-Regenerative-Lab-Autocrawler
Virelion Biotech

Loads everything harvested in steps 1-4 (papers, grants, trials, crawled lab
pages), normalizes each into a lightweight text candidate, and uses Claude to
classify each as in- or out-of-domain per the inclusion/exclusion taxonomy in
config.py (e.g. excluding general interventional cardiology or non-cardiac
stem cell work that isn't actually about cardiac regeneration).

Classification happens in batches (default 10 candidates/call) to control
API cost while keeping the model's context small enough for reliable JSON
output.

Output: data/<year>/filtered.json
    A list of the original records that passed classification, each with
    classification metadata attached (confidence, reason) and a
    "record_type" field (papers/grants/trials/lab_pages) so step 6 knows
    how to interpret it.

Usage:
    python 5_classify_relevance.py
"""

import json
import time
from typing import Any

import anthropic

import config

BATCH_SIZE = 10
API_RETRY_DELAY = 2.0
MAX_RETRIES = 2
BATCH_POLL_INTERVAL_SECONDS = 20

INCLUSION_CRITERIA = """
INCLUDE if the item describes active research, funding, or a clinical trial on:
- Myocardial repair or regeneration via cell transplantation
- Direct cardiac cell lineage reprogramming
- Cardiac tissue engineering (engineered heart tissue, cardiac patches, decellularized matrices, 3D bioprinting)
- Biological pacing / bioartificial pacemakers
- Stem cell-derived cardiomyocytes (hiPSC-CM, ESC-CM) for cardiac repair
- Electromechanical integration or arrhythmia risk specifically in the context of regenerative cardiac grafts

EXCLUDE if the item is primarily about:
- Standard interventional cardiology (stenting, catheterization) with no regenerative component
- General electrophysiology/ablation unrelated to regenerative grafts
- General stem cell biology with no cardiac application
- Cardiology clinical care/diagnostics with no regenerative research angle
""".strip()

RESPONSE_FORMAT_INSTRUCTIONS = """
Respond with ONLY a JSON array, no other text, no markdown fences.
Each element must have exactly these fields:
- "id": the item id exactly as given
- "include": true or false
- "confidence": a number from 0.0 to 1.0
- "reason": a brief (one sentence) justification
""".strip()

# The system prompt is identical across every batch call, so it's marked for
# prompt caching (cache_control) — Anthropic caches this block after the
# first call and charges ~10% of base input price on subsequent hits,
# instead of re-billing the full instructions on every one of potentially
# hundreds of batch calls.
SYSTEM_PROMPT = (
    "You are classifying research items for a cardiac regeneration lab discovery database.\n\n"
    f"{INCLUSION_CRITERIA}\n\n{RESPONSE_FORMAT_INSTRUCTIONS}"
)


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _system_block() -> list[dict[str, Any]]:
    return [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]


# ---------------------------------------------------------------------------
# Loading & normalizing candidates from all four sources
# ---------------------------------------------------------------------------

def _load_json(key: str) -> list[dict[str, Any]]:
    path = config.run_path(key)
    if not path.exists():
        print(f"  [warn] {path.name} not found — skipping this source (did you run the harvest step for it?)")
        return []
    with open(path) as f:
        return json.load(f)


def normalize_candidates() -> list[dict[str, Any]]:
    """
    Builds a flat list of {id, record_type, source_index, text} candidates
    from all four raw data files, keeping a pointer back to the original
    full record so filtered.json can carry the complete original data.
    """
    candidates = []

    papers = _load_json("raw_papers")
    for i, rec in enumerate(papers):
        text = f"{rec.get('title', '')}\n\n{rec.get('abstract', '')}".strip()
        if text:
            candidates.append({"id": f"papers_{i}", "record_type": "papers", "source_index": i, "text": text[:3000]})

    grants = _load_json("raw_grants")
    for i, rec in enumerate(grants):
        text = f"{rec.get('title', '')}\n\n{rec.get('abstract', '')}".strip()
        if text:
            candidates.append({"id": f"grants_{i}", "record_type": "grants", "source_index": i, "text": text[:3000]})

    trials = _load_json("raw_trials")
    for i, rec in enumerate(trials):
        conditions = ", ".join(rec.get("conditions", []))
        interventions = ", ".join(rec.get("interventions", []))
        text = (
            f"{rec.get('title', '')}\n\n"
            f"Conditions: {conditions}\nInterventions: {interventions}\n\n"
            f"{rec.get('brief_summary', '')}"
        ).strip()
        if text:
            candidates.append({"id": f"trials_{i}", "record_type": "trials", "source_index": i, "text": text[:3000]})

    patents = _load_json("raw_patents")
    for i, rec in enumerate(patents):
        text = f"{rec.get('title', '')}\n\n{rec.get('abstract', '')}".strip()
        if text:
            candidates.append({"id": f"patents_{i}", "record_type": "patents", "source_index": i, "text": text[:3000]})

    lab_pages = _load_json("raw_lab_pages")
    for i, rec in enumerate(lab_pages):
        text = f"{rec.get('title', '')}\n\n{rec.get('text', '')}".strip()
        if text:
            candidates.append({"id": f"lab_pages_{i}", "record_type": "lab_pages", "source_index": i, "text": text[:3000]})

    return candidates


# ---------------------------------------------------------------------------
# Claude classification
# ---------------------------------------------------------------------------

def build_items_block(batch: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"--- ITEM {c['id']} ---\n{c['text']}" for c in batch)


def _parse_decisions(raw_text: str) -> dict[str, dict[str, Any]] | None:
    """Returns a {id: decision} dict, or None if the response wasn't parseable JSON."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        decisions = json.loads(text)
        return {d["id"]: d for d in decisions if "id" in d}
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def classify_batch_sync(client: anthropic.Anthropic, batch: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    Synchronous fallback path — used only to retry batches that failed via
    the Batch API (rare). Costs full price rather than the Batch API's 50%
    discount, but reliability matters more than cost for the small number
    of batches that land here.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=config.CLAUDE_MAX_TOKENS,
                system=_system_block(),
                messages=[{"role": "user", "content": build_items_block(batch)}],
            )
            raw_text = "".join(b.text for b in response.content if b.type == "text")
            decisions = _parse_decisions(raw_text)
            if decisions is not None:
                return decisions
            print(f"    [warn] sync retry attempt {attempt + 1}: unparseable response")
        except anthropic.APIError as e:
            print(f"    [warn] sync retry attempt {attempt + 1} failed: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(API_RETRY_DELAY)

    print(f"    [warn] giving up on batch after {MAX_RETRIES + 1} sync attempts — "
          f"{len(batch)} items will be excluded by default and flagged for manual review")
    return {
        c["id"]: {"id": c["id"], "include": False, "confidence": 0.0, "reason": "CLASSIFICATION_FAILED_NEEDS_REVIEW"}
        for c in batch
    }


def submit_batch_job(client: anthropic.Anthropic, batches: list[list[dict[str, Any]]]) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    requests = []
    custom_id_map = {}
    for i, batch in enumerate(batches):
        custom_id = f"batch_{i}"
        custom_id_map[custom_id] = batch
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": config.CLAUDE_MODEL,
                "max_tokens": config.CLAUDE_MAX_TOKENS,
                "system": _system_block(),
                "messages": [{"role": "user", "content": build_items_block(batch)}],
            },
        })
    batch_job = client.messages.batches.create(requests=requests)
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
            results[entry.custom_id] = None  # errored, expired, or canceled
    return results


def classify_all(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    client = _client()
    batches = [candidates[i:i + BATCH_SIZE] for i in range(0, len(candidates), BATCH_SIZE)]

    print(f"Submitting {len(batches)} batches ({len(candidates)} items) as one Batch API job "
          f"(50% cheaper than synchronous calls)...")
    batch_id, custom_id_map = submit_batch_job(client, batches)
    wait_for_batch_job(client, batch_id)
    raw_results = fetch_batch_results(client, batch_id)

    all_decisions = {}
    failed_batches = []
    for custom_id, batch in custom_id_map.items():
        raw_text = raw_results.get(custom_id)
        decisions = _parse_decisions(raw_text) if raw_text else None
        if decisions is None:
            failed_batches.append(batch)
        else:
            all_decisions.update(decisions)

    if failed_batches:
        print(f"  {len(failed_batches)} batch(es) didn't come back parseable via the Batch API — "
              f"retrying those synchronously...")
        for batch in failed_batches:
            all_decisions.update(classify_batch_sync(client, batch))

    return all_decisions


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    config.check_required_env_vars()
    config.ensure_run_dir()

    print("Loading and normalizing candidates from all four sources...")
    candidates = normalize_candidates()
    print(f"  -> {len(candidates)} total candidates to classify\n")

    if not candidates:
        print("No candidates found. Did steps 1-4 run successfully?")
        with open(config.run_path("filtered"), "w") as f:
            json.dump([], f, indent=2)
        return

    decisions = classify_all(candidates)

    # Reload raw source files once (not per-candidate) to attach full original records
    raw_by_type = {
        "papers": _load_json("raw_papers"),
        "grants": _load_json("raw_grants"),
        "trials": _load_json("raw_trials"),
        "patents": _load_json("raw_patents"),
        "lab_pages": _load_json("raw_lab_pages"),
    }

    filtered = []
    needs_review = []
    excluded_count = 0

    for c in candidates:
        decision = decisions.get(c["id"])
        if decision is None:
            continue  # model dropped this id from its response; treat as excluded

        original_record = raw_by_type[c["record_type"]][c["source_index"]]

        if decision["reason"] == "CLASSIFICATION_FAILED_NEEDS_REVIEW":
            needs_review.append({**original_record, "record_type": c["record_type"]})
            continue

        if decision.get("include"):
            filtered.append({
                **original_record,
                "record_type": c["record_type"],
                "classification_confidence": decision.get("confidence"),
                "classification_reason": decision.get("reason"),
            })
        else:
            excluded_count += 1

    out_path = config.run_path("filtered")
    with open(out_path, "w") as f:
        json.dump(filtered, f, indent=2)

    if needs_review:
        review_path = config.CURRENT_RUN_DIR / "needs_manual_review.json"
        with open(review_path, "w") as f:
            json.dump(needs_review, f, indent=2)
        print(f"\n[!] {len(needs_review)} items failed classification and were written to {review_path} for manual review.")

    print(f"\nDone. {len(filtered)} included, {excluded_count} excluded, {len(needs_review)} needs review.")
    print(f"Wrote filtered candidates to {out_path}")


if __name__ == "__main__":
    main()
