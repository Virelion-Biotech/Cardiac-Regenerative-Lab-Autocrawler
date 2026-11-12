import csv
import tempfile
import unittest
from pathlib import Path

from _helpers import load_step

export = load_step("9_export_csv.py")
leaderboard = load_step("11_export_funding_leaderboard.py")
dashboard = load_step("12_export_trial_dashboard.py")


def lab(name, institution, funding=None, status="Active", **extra):
    base = {
        "pi_full_name": name,
        "institution": institution,
        "metrics": {"grant_funding_usd": funding, "clinical_trial_ids": [], "patent_ids": []},
        "activity_verification_index": {"status": status, "avi_score": 80, "most_recent_activity_year": 2025},
    }
    base.update(extra)
    return base


class FlattenAndCsvTest(unittest.TestCase):
    def test_flatten_lab_joins_lists_and_renders_flags(self):
        row = export.flatten_lab(lab(
            "Joseph Wu", "Stanford", 1000, aka_names=["J. Wu", "Joe Wu"],
            research_focus={"functional_vectors": ["a", "b"]},
            risk_flags={"electromechanical_risk_profile": True},
            translation_scale_metrics={"citation_impact": 0, "industry_spinoff_affiliation": False},
        ))
        self.assertEqual(set(row), set(export.CSV_COLUMNS))
        self.assertEqual(row["aka_names"], "J. Wu; Joe Wu")
        self.assertEqual(row["functional_vectors"], "a; b")
        self.assertEqual(row["electromechanical_risk_profile"], "Yes")
        self.assertEqual(row["citation_impact"], 0)
        self.assertEqual(row["industry_spinoff_affiliation"], "No")

    def test_unknown_spinoff_and_missing_values_are_blank(self):
        row = export.flatten_lab({})
        self.assertEqual(row["industry_spinoff_affiliation"], "")
        self.assertEqual(row["avi_score"], "")
        self.assertEqual(row["grant_funding_usd"], "")
        self.assertEqual(row["electromechanical_risk_profile"], "No")

    def test_export_csv_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "labs.csv"
            export.export_csv([lab("A B", "X", 5), lab("C D", "Y")], out)
            with open(out, newline="") as f:
                rows = list(csv.DictReader(f))
        self.assertEqual([r["pi_full_name"] for r in rows], ["A B", "C D"])
        self.assertEqual(list(rows[0]), export.CSV_COLUMNS)


class YearOverYearDiffTest(unittest.TestCase):
    def test_scholar_url_matches_across_institution_move(self):
        a = lab("Jane Doe", "Kyoto University", digital_footprint={"google_scholar": "https://scholar/x"})
        b = lab("Jane Doe", "Harvard Medical School", digital_footprint={"google_scholar": "HTTPS://SCHOLAR/X "})
        self.assertTrue(export._labs_match(a, b))
        self.assertTrue(export._matched_via_institution_agnostic_signal(a, b))

    def test_name_only_without_institution_does_not_match(self):
        a = lab("Jane Doe", "Kyoto University")
        b = lab("Jane Doe", "Harvard Medical School")
        self.assertFalse(export._labs_match(a, b))
        self.assertFalse(export._matched_via_institution_agnostic_signal(a, b))

    def test_matching_is_one_to_one_on_the_prior_side(self):
        prior = [lab("Jane Doe", "Stanford")]
        current = [lab("Jane Doe", "Stanford"), lab("J. Doe", "Stanford University")]
        pairs = export.match_against_prior(current, prior)
        self.assertIs(pairs[0][1], prior[0])
        self.assertIsNone(pairs[1][1])

    def test_diff_summary_sections(self):
        prior = [
            lab("Jane Doe", "Kyoto University", 100000, orcid="0000-1"),
            lab("Old Pi", "MIT", 1000, status="Active"),
            lab("Steady Pi", "Yale", 100000),
        ]
        current = [
            lab("Jane Doe", "Harvard Medical School", 300000, orcid="0000-1"),
            lab("Old Pi", "MIT", 1000, status="Inactive/Legacy"),
            lab("Steady Pi", "Yale", 101000),
            lab("New Pi", "Oxford"),
        ]
        text = export.build_diff_summary(current, prior, "2025")
        self.assertIn("vs. 2025", text)
        self.assertIn("Newly Discovered Labs (1)", text)
        self.assertIn("**New Pi** — Oxford", text)
        self.assertIn("Significant Funding Shifts (1)", text)
        self.assertIn("$100,000 → $300,000 (up $200,000)", text)
        self.assertIn("Newly Inactive Labs (1)", text)
        self.assertIn("Possible Institutional Migrations (1)", text)
        self.assertIn("Kyoto University → Harvard Medical School _(confidence: high)_", text)

    def test_diff_summary_empty_sections(self):
        text = export.build_diff_summary([], [], "2025")
        self.assertIn("_None this year._", text)
        self.assertIn("_None detected._", text)


class LeaderboardTest(unittest.TestCase):
    def test_lab_leaderboard_ranks_funded_labs_only(self):
        rows = leaderboard.build_lab_leaderboard([lab("A", "X", 10), lab("B", "Y", 30), lab("C", "Z")])
        self.assertEqual([(r["rank"], r["pi_full_name"]) for r in rows], [(1, "B"), (2, "A")])
        self.assertEqual(rows[0]["avi_status"], "Active")

    def test_institution_rollup(self):
        rows = leaderboard.build_institution_rollup([
            lab("A", "Stanford", 10), lab("B", " Stanford ", 20), lab("C", "Yale"), lab("D", "", 99),
        ])
        self.assertEqual(rows[0], {"institution": "Stanford", "total_funding_usd": 30, "funded_lab_count": 2, "total_lab_count": 2, "rank": 1})
        self.assertEqual(rows[1]["institution"], "Yale")
        self.assertEqual(len(rows), 2)

    def test_write_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "x.csv"
            leaderboard.write_csv([{"a": 1, "b": 2}], ["a", "b"], out)
            self.assertEqual(out.read_text().splitlines(), ["a,b", "1,2"])


class TrialDashboardTest(unittest.TestCase):
    def test_year_from_date(self):
        self.assertEqual(dashboard._year_from_date("2023-04"), "2023")
        self.assertEqual(dashboard._year_from_date(""), "Unknown")
        self.assertEqual(dashboard._year_from_date("n/a"), "Unknown")

    def test_dashboard_aggregates(self):
        trials = [
            {"phase": "PHASE1", "lead_sponsor": "S1", "status": "RECRUITING", "start_date": "2021-01",
             "locations": [{"country": "Japan"}, {"country": "Japan"}, {"country": "US"}]},
            {"phase": "", "lead_sponsor": "S1", "start_date": "2019-05", "locations": []},
            {"phase": "PHASE1", "start_date": ""},
        ]
        data = dashboard.build_dashboard_data(trials)
        self.assertEqual(data["total_trials"], 3)
        self.assertEqual(dict(zip(data["phase_labels"], data["phase_values"])), {"PHASE1": 2, "Not specified": 1})
        self.assertEqual(dict(zip(data["sponsor_labels"], data["sponsor_values"])), {"S1": 2, "Unknown": 1})
        self.assertEqual(dict(zip(data["country_labels"], data["country_values"])), {"Japan": 1, "US": 1})
        self.assertEqual(data["year_labels"], ["2019", "2021"])
        self.assertEqual(data["status_values"], [1, 2])

    def test_main_renders_html_from_raw_trials(self):
        import json
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(dashboard.config, "CURRENT_RUN_DIR", Path(tmp)):
            (Path(tmp) / "raw_trials.json").write_text(json.dumps([{"phase": "PHASE2", "start_date": "2024-01"}]))
            dashboard.main()
            html = (Path(tmp) / "trial_dashboard.html").read_text()
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn('"phase_labels": ["PHASE2"]', html)
        self.assertNotIn("{data_json}", html)


class PipelineMainTest(unittest.TestCase):
    """End-to-end runs of the offline export steps against a temporary run directory."""

    def test_leaderboard_main_writes_both_csvs(self):
        import json
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(leaderboard.config, "CURRENT_RUN_DIR", Path(tmp)):
            (Path(tmp) / "labs_final.json").write_text(json.dumps([lab("A B", "Stanford", 10)]))
            leaderboard.main()
            labs_csv = (Path(tmp) / "funding_leaderboard.csv").read_text()
            inst_csv = (Path(tmp) / "funding_leaderboard_institutions.csv").read_text()
        self.assertIn("A B", labs_csv)
        self.assertIn("Stanford", inst_csv)

    def test_export_main_writes_report_csv(self):
        import json
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "2099"
            with mock.patch.object(export.config, "CURRENT_RUN_DIR", run), \
                 mock.patch.object(export.config, "DATA_DIR", Path(tmp)), \
                 mock.patch.object(export.config, "CURRENT_YEAR", "2099"):
                run.mkdir()
                (run / "labs_final.json").write_text(json.dumps([lab("A B", "Stanford", 10)]))
                prior = Path(tmp) / "2098"
                prior.mkdir()
                (prior / "labs_final.json").write_text(json.dumps([]))
                export.main()
                self.assertTrue((run / "labs_report.csv").exists())
                summary = (run / "annual_diff_summary.md").read_text()
        self.assertIn("2099 vs. 2098", summary)
        self.assertIn("Newly Discovered Labs (1)", summary)


if __name__ == "__main__":
    unittest.main()
