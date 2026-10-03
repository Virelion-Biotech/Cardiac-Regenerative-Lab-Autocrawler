"""
1_harvest_papers.py — Cardiac-Regenerative-Lab-Autocrawler
Virelion Biotech

Queries PubMed (via NCBI E-utilities) and Europe PMC for papers matching the
domain search terms in config.py. Europe PMC's index includes bioRxiv and
medRxiv preprints (via its `source` filter), so it covers all four literature
sources named in the README without needing bioRxiv's more limited native
search API.

Output: data/<year>/raw_papers.json
    A list of paper records, each tagged with its source and search term hit.
    No filtering or deduplication happens here — that's steps 5 and 7.

Usage:
    python 1_harvest_papers.py
"""

import json
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

import config

# ---------------------------------------------------------------------------
# PubMed (NCBI E-utilities)
# ---------------------------------------------------------------------------

# NCBI usage policy: max 3 req/sec without an API key, 10 req/sec with one.
PUBMED_DELAY = 0.1 if config.NCBI_API_KEY else 0.34


def _pubmed_params(extra: dict) -> dict:
    params = {"db": "pubmed", "email": config.NCBI_EMAIL, "tool": "virelion_cardiac_lab_finder"}
    if config.NCBI_API_KEY:
        params["api_key"] = config.NCBI_API_KEY
    params.update(extra)
    return params


def pubmed_search(term: str, retmax: int = 200) -> list[str]:
    """Returns a list of PubMed IDs matching the search term."""
    url = f"{config.PUBMED_EUTILS_BASE}/esearch.fcgi"
    params = _pubmed_params({"term": term, "retmax": retmax, "retmode": "json", "sort": "date"})
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("esearchresult", {}).get("idlist", [])


def pubmed_fetch_details(pmids: list[str]) -> list[dict[str, Any]]:
    """Fetches full record details (title, abstract, authors, affiliations, DOI) for a batch of PMIDs."""
    if not pmids:
        return []
    url = f"{config.PUBMED_EUTILS_BASE}/efetch.fcgi"
    params = _pubmed_params({"id": ",".join(pmids), "retmode": "xml", "rettype": "abstract"})
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()

    records = []
    root = ET.fromstring(resp.content)
    for article in root.findall(".//PubmedArticle"):
        try:
            records.append(_parse_pubmed_article(article))
        except Exception as e:
            # Skip malformed individual records rather than failing the whole batch
            print(f"  [warn] failed to parse a PubMed record: {e}")
    return records


def _parse_pubmed_article(article: ET.Element) -> dict[str, Any]:
    pmid = article.findtext(".//PMID", default="")
    title_node = article.find(".//ArticleTitle")
    title = "".join(title_node.itertext()) if title_node is not None else ""

    abstract_parts = ["".join(el.itertext()) for el in article.findall(".//AbstractText")]
    abstract = " ".join(abstract_parts).strip()

    journal = article.findtext(".//Journal/Title", default="")
    pub_year = article.findtext(".//PubDate/Year", default="")
    if not pub_year:
        # Some records only have MedlineDate (e.g. "2023 Jan-Feb")
        medline_date = article.findtext(".//PubDate/MedlineDate", default="")
        pub_year = medline_date[:4] if medline_date else ""

    doi = ""
    for id_el in article.findall(".//ArticleId"):
        if id_el.get("IdType") == "doi":
            doi = id_el.text or ""

    authors = []
    for author_el in article.findall(".//AuthorList/Author"):
        last = author_el.findtext("LastName", default="")
        fore = author_el.findtext("ForeName", default="")
        affil = author_el.findtext(".//AffiliationInfo/Affiliation", default="")
        if last:
            authors.append({"last_name": last, "fore_name": fore, "affiliation": affil})

    return {
        "source": "pubmed",
        "pmid": pmid,
        "doi": doi,
        "title": title,
        "abstract": abstract,
        "journal": journal,
        "pub_year": pub_year,
        "authors": authors,
    }


# ---------------------------------------------------------------------------
# Europe PMC (covers published articles + bioRxiv/medRxiv preprints)
# ---------------------------------------------------------------------------

def europepmc_search(term: str, page_size: int = 100, include_preprints: bool = True) -> list[dict[str, Any]]:
    """
    Searches Europe PMC. When include_preprints is True, does not restrict by
    source, so results include bioRxiv/medRxiv preprints alongside published
    articles (Europe PMC tags each result's source in the response).
    """
    url = f"{config.EUROPE_PMC_BASE}/search"
    query = term if include_preprints else f"({term}) AND (SRC:MED)"
    params = {
        "query": query,
        "format": "json",
        "pageSize": page_size,
        "resultType": "core",
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    records = []
    for item in data.get("resultList", {}).get("result", []):
        records.append(_parse_europepmc_item(item))
    return records


def _parse_europepmc_item(item: dict) -> dict[str, Any]:
    authors = []
    for a in item.get("authorList", {}).get("author", []):
        authors.append({
            "last_name": a.get("lastName", ""),
            "fore_name": a.get("firstName", ""),
            "affiliation": a.get("affiliation", ""),
        })

    src = item.get("source", "")
    is_preprint = src in ("PPR",) or "bioRxiv" in item.get("bookOrReportDetails", {}).get("publisher", "")

    return {
        "source": "europepmc_preprint" if is_preprint else "europepmc",
        "pmid": item.get("pmid", ""),
        "doi": item.get("doi", ""),
        "title": item.get("title", ""),
        "abstract": item.get("abstractText", ""),
        "journal": item.get("journalInfo", {}).get("journal", {}).get("title", ""),
        "pub_year": item.get("pubYear", ""),
        "authors": authors,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def harvest_term(term: str) -> list[dict[str, Any]]:
    """Runs both PubMed and Europe PMC harvesting for a single search term."""
    results = []

    print(f"  PubMed: searching '{term}'")
    try:
        pmids = pubmed_search(term)
        time.sleep(PUBMED_DELAY)
        # efetch in batches of 100 (efetch's practical batch limit)
        for i in range(0, len(pmids), 100):
            batch = pmids[i:i + 100]
            results.extend(pubmed_fetch_details(batch))
            time.sleep(PUBMED_DELAY)
        print(f"    -> {len(pmids)} PubMed records")
    except requests.RequestException as e:
        print(f"    [warn] PubMed search failed for '{term}': {e}")

    print(f"  Europe PMC (incl. preprints): searching '{term}'")
    try:
        epmc_results = europepmc_search(term)
        results.extend(epmc_results)
        print(f"    -> {len(epmc_results)} Europe PMC records")
    except requests.RequestException as e:
        print(f"    [warn] Europe PMC search failed for '{term}': {e}")

    return results


def main():
    config.check_required_env_vars()
    config.ensure_run_dir()

    print(f"Harvesting papers for {len(config.ALL_SEARCH_TERMS)} search terms...")
    all_records = []
    for term in config.ALL_SEARCH_TERMS:
        print(f"\n[{term}]")
        term_records = harvest_term(term)
        for rec in term_records:
            rec["matched_term"] = term
        all_records.extend(term_records)

    out_path = config.run_path("raw_papers")
    with open(out_path, "w") as f:
        json.dump(all_records, f, indent=2)

    print(f"\nDone. Wrote {len(all_records)} raw records (pre-dedup) to {out_path}")


if __name__ == "__main__":
    main()
