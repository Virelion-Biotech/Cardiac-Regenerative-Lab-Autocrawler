import unittest
from unittest import mock

import requests

from _helpers import FakeResponse, load_step

scoring = load_step("8_score_and_enrich.py")
config = scoring.config


def lab_with_years(*years):
    return {"source_references": [{"year": y} for y in years]}


class AviTest(unittest.TestCase):
    def test_unknown_without_dated_evidence(self):
        avi = scoring.compute_avi({"source_references": [{"year": "2020"}, {}]})
        self.assertEqual(avi["status"], "Unknown")
        self.assertIsNone(avi["avi_score"])

    def test_status_bands_follow_config_windows(self):
        now = scoring.CURRENT_YEAR
        self.assertEqual(scoring.compute_avi(lab_with_years(now - 1, now))["status"], "Active")
        self.assertEqual(scoring.compute_avi(lab_with_years(now))["avi_score"], 100)
        aging_years = config.AVI_INACTIVE_THRESHOLD_MONTHS // 12
        aging = scoring.compute_avi(lab_with_years(now - aging_years))
        self.assertEqual(aging["status"], "Aging" if aging_years * 12 > config.AVI_ACTIVE_WINDOW_MONTHS else "Active")
        old = scoring.compute_avi(lab_with_years(now - 10))
        self.assertEqual(old["status"], "Inactive/Legacy")
        self.assertEqual(old["avi_score"], 0)
        self.assertEqual(old["months_since_last_activity"], 120)


class RiskFlagTest(unittest.TestCase):
    def test_matches_only_risk_vectors(self):
        lab = {"research_focus": {"functional_vectors": ["arrhythmia mitigation", "vascularization"]}}
        self.assertEqual(scoring.compute_risk_flags(lab), {"electromechanical_risk_profile": True, "matched_risk_vectors": ["arrhythmia mitigation"]})
        self.assertFalse(scoring.compute_risk_flags({})["electromechanical_risk_profile"])


class CitationLookupTest(unittest.TestCase):
    def test_empty_input_makes_no_request(self):
        with mock.patch.object(scoring.requests, "post") as post:
            self.assertEqual(scoring.fetch_citation_counts([]), {})
        post.assert_not_called()

    def test_batches_and_skips_unknown_dois(self):
        responses = [FakeResponse([{"citationCount": 5}, None]), FakeResponse([{"citationCount": None}])]
        with mock.patch.object(config, "SEMANTIC_SCHOLAR_BATCH_SIZE", 2), \
             mock.patch.object(scoring.requests, "post", side_effect=responses) as post, \
             mock.patch.object(scoring.time, "sleep"):
            counts = scoring.fetch_citation_counts(["a", "b", "c"])
        self.assertEqual(counts, {"a": 5})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"], {"ids": ["DOI:a", "DOI:b"]})

    def test_request_failure_is_tolerated(self):
        with mock.patch.object(scoring.requests, "post", side_effect=requests.ConnectionError("x")), \
             mock.patch.object(scoring.time, "sleep"):
            self.assertEqual(scoring.fetch_citation_counts(["a"]), {})


class SpinoffHeuristicTest(unittest.TestCase):
    def test_not_computed_without_patents(self):
        self.assertIsNone(scoring.compute_industry_spinoff_flag({}, {})["flag"])

    def test_flags_distinct_non_academic_assignee(self):
        lab = {"institution": "Stanford University", "metrics": {"patent_ids": ["P1", "P2", "missing"]}}
        patents = {
            "P1": {"assignee_organizations": ["HeartWorks Inc", "Stanford University"]},
            "P2": {"assignee_organizations": ["Board of Trustees of the Leland Stanford Junior University", None]},
        }
        result = scoring.compute_industry_spinoff_flag(lab, patents)
        self.assertTrue(result["flag"])
        self.assertEqual(result["matched_organizations"], ["HeartWorks Inc"])

    def test_assignee_sharing_institution_tokens_is_not_flagged(self):
        lab = {"institution": "Acme Labs", "metrics": {"patent_ids": ["P1"]}}
        result = scoring.compute_industry_spinoff_flag(lab, {"P1": {"assignee_organizations": ["Acme Corp"]}})
        self.assertFalse(result["flag"])

    def test_orgs_distinct_handles_blanks(self):
        self.assertTrue(scoring._orgs_distinct("", "X"))
        self.assertFalse(scoring._orgs_distinct("Acme Corp", "acme labs"))


if __name__ == "__main__":
    unittest.main()
