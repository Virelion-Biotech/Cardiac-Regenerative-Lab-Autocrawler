import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from _helpers import FakeResponse, load_step

crawl = load_step("4_crawl_lab_pages.py")
classify = load_step("5_classify_relevance.py")
extract = load_step("6_extract_structured.py")


class CrawlTest(unittest.TestCase):
    HTML = """
    <html><head><title> Cardio Lab </title><script>var x=1;</script></head>
    <body><nav>Menu</nav>
      <a href="/faculty/jane">Jane</a>
      <a href="/faculty/jane">Jane again</a>
      <a href="https://other.org/people/x">External</a>
      <a href="/news">Researcher spotlight</a>
      <a href="/about">About</a>
      <p>Heart   regeneration
      research.</p><footer>Footer</footer>
    </body></html>
    """

    def test_discover_profile_links_same_domain_deduplicated(self):
        links = crawl.discover_profile_links("https://uni.edu/dir", self.HTML, max_links=10)
        self.assertEqual(links, ["https://uni.edu/faculty/jane", "https://uni.edu/news"])

    def test_discover_profile_links_respects_limit(self):
        self.assertEqual(len(crawl.discover_profile_links("https://uni.edu/dir", self.HTML, max_links=1)), 1)

    def test_extract_page_text_strips_chrome(self):
        title, text = crawl.extract_page_text(self.HTML)
        self.assertEqual(title, "Cardio Lab")
        self.assertIn("Heart regeneration research.", text)
        for noise in ("Menu", "Footer", "var x"):
            self.assertNotIn(noise, text)

    def test_fetch_page_skips_disallowed_urls(self):
        with mock.patch.object(crawl, "_is_allowed", return_value=False), mock.patch.object(crawl.requests, "get") as get:
            self.assertIsNone(crawl.fetch_page("https://uni.edu/x"))
        get.assert_not_called()

    def test_fetch_page_returns_none_on_http_error(self):
        with mock.patch.object(crawl, "_is_allowed", return_value=True), \
             mock.patch.object(crawl, "_rate_limit"), \
             mock.patch.object(crawl.requests, "get", side_effect=requests.ConnectionError("x")):
            self.assertIsNone(crawl.fetch_page("https://uni.edu/x"))

    def test_robots_check_can_be_disabled(self):
        with mock.patch.object(crawl.config, "CRAWL_RESPECT_ROBOTS_TXT", False):
            self.assertTrue(crawl._is_allowed("https://uni.edu/private"))


class ClassifyStepTest(unittest.TestCase):
    def test_parse_decisions_plain_and_fenced(self):
        raw = '[{"id": "papers_0", "include": true}, {"no_id": 1}]'
        self.assertEqual(classify._parse_decisions(raw), {"papers_0": {"id": "papers_0", "include": True}})
        fenced = '```json\n[{"id": "a", "include": false}]\n```'
        self.assertEqual(list(classify._parse_decisions(fenced)), ["a"])
        self.assertIsNone(classify._parse_decisions("not json"))

    def test_build_items_block(self):
        block = classify.build_items_block([{"id": "a", "text": "x"}, {"id": "b", "text": "y"}])
        self.assertEqual(block, "--- ITEM a ---\nx\n\n--- ITEM b ---\ny")

    def test_normalize_candidates_reads_every_source(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(classify.config, "CURRENT_RUN_DIR", Path(tmp)):
            files = {
                "raw_papers.json": [{"title": "P", "abstract": "A"}, {"title": "", "abstract": ""}],
                "raw_grants.json": [{"title": "G"}],
                "raw_trials.json": [{"title": "T", "conditions": ["MI"], "interventions": ["cells"]}],
                "raw_patents.json": [{"title": "Pat" * 2000}],
            }
            for name, rows in files.items():
                (Path(tmp) / name).write_text(json.dumps(rows))
            candidates = classify.normalize_candidates()  # raw_lab_pages.json missing on purpose
        self.assertEqual([c["id"] for c in candidates], ["papers_0", "grants_0", "trials_0", "patents_0"])
        self.assertIn("Conditions: MI", candidates[2]["text"])
        self.assertEqual(len(candidates[3]["text"]), 3000)


class ExtractStepTest(unittest.TestCase):
    def test_known_fields_per_record_type(self):
        paper = {"record_type": "papers", "doi": "d", "authors": [{"fore_name": "A", "last_name": "First"},
                                                                    {"fore_name": "Jane", "last_name": "Senior", "affiliation": "Kyoto"}]}
        self.assertEqual(extract._known_fields_for_record(paper), {"pi_full_name": "Jane Senior", "institution": "Kyoto", "doi": "d"})
        grant = {"record_type": "grants", "contact_pi_name": "Contact", "org_name": "MIT", "award_amount": 5}
        self.assertEqual(extract._known_fields_for_record(grant)["pi_full_name"], "Contact")
        trial = {"record_type": "trials", "lead_sponsor": "Sponsor", "locations": [{"city": "Osaka", "country": "Japan"}]}
        fields = extract._known_fields_for_record(trial)
        self.assertEqual((fields["institution"], fields["city"]), ("Sponsor", "Osaka"))
        patent = {"record_type": "patents", "inventors": [{"first_name": "I", "last_name": "N"}], "assignee_organizations": ["Co"]}
        self.assertEqual(extract._known_fields_for_record(patent)["pi_full_name"], "I N")
        self.assertEqual(extract._known_fields_for_record({"record_type": "lab_pages"}), {})

    def test_record_year(self):
        self.assertEqual(extract._record_year({"record_type": "papers", "pub_year": "2024"}), 2024)
        self.assertIsNone(extract._record_year({"record_type": "papers", "pub_year": "n/a"}))
        self.assertEqual(extract._record_year({"record_type": "grants", "fiscal_year": "2023"}), 2023)
        self.assertIsNone(extract._record_year({"record_type": "grants", "fiscal_year": None}))
        self.assertEqual(extract._record_year({"record_type": "trials", "start_date": "2020-01", "completion_date": "2027-06"}), 2027)
        self.assertEqual(extract._record_year({"record_type": "trials", "start_date": "2020-01", "completion_date": ""}), 2020)
        self.assertEqual(extract._record_year({"record_type": "patents", "patent_date": "2019-03-01"}), 2019)
        self.assertIsNone(extract._record_year({"record_type": "lab_pages"}))

    def test_text_for_record(self):
        self.assertEqual(extract._text_for_record({"record_type": "trials", "title": "T", "brief_summary": "S"}), "T\n\nS")
        self.assertEqual(extract._text_for_record({"record_type": "unknown"}), "")

    def test_build_items_block_lists_known_fields(self):
        block = extract.build_items_block([{"id": "x", "known_fields": {"a": "1", "b": ""}, "text": "body"},
                                           {"id": "y", "known_fields": {}, "text": "t"}])
        self.assertIn("Known fields: a=1\nText:\nbody", block)
        self.assertIn("(none pre-known)", block)

    def test_parse_extractions(self):
        self.assertEqual(list(extract._parse_extractions('```\n[{"id": "z"}]\n```')), ["z"])
        self.assertIsNone(extract._parse_extractions("{"))

    def test_geocode_caches_and_tolerates_failures(self):
        extract._geocode_cache.clear()
        with mock.patch.object(extract, "_geocode_rate_limit"), \
             mock.patch.object(extract.requests, "get", return_value=FakeResponse([{"lat": "1.5", "lon": "2.5"}])) as get:
            self.assertEqual(extract.geocode_city_country("Kyoto", "Japan"), {"lat": 1.5, "lng": 2.5})
            self.assertEqual(extract.geocode_city_country(" kyoto ", "JAPAN"), {"lat": 1.5, "lng": 2.5})
        self.assertEqual(get.call_count, 1)
        self.assertIsNone(extract.geocode_city_country("", ""))
        with mock.patch.object(extract, "_geocode_rate_limit"), \
             mock.patch.object(extract.requests, "get", side_effect=requests.Timeout("slow")):
            self.assertIsNone(extract.geocode_city_country("Nowhere", "Land"))
        extract._geocode_cache.clear()


if __name__ == "__main__":
    unittest.main()
