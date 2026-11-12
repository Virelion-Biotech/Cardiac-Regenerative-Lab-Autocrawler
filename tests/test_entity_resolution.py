import unittest

from _helpers import load_step

er = load_step("7_resolve_entities.py")


def candidate(name, institution, **extra):
    base = {"pi_full_name": name, "institution": institution}
    base.update(extra)
    return base


class NormalizationTest(unittest.TestCase):
    def test_normalize_name_strips_titles_and_punctuation(self):
        self.assertEqual(er.normalize_name("Prof. Dr. Joseph C. Wu, MD"), "joseph c wu")

    def test_name_parts(self):
        self.assertEqual(er.name_parts("Joseph C. Wu"), ("j", "wu"))
        self.assertEqual(er.name_parts("Dr."), ("", ""))

    def test_normalize_institution_drops_stopwords(self):
        self.assertEqual(er.normalize_institution("The University of Washington School of Medicine"), "washington medicine")


class MatchingTest(unittest.TestCase):
    def test_names_match_on_last_name_and_initial(self):
        self.assertTrue(er.names_match("J. Wu", "Joseph Wu"))
        self.assertFalse(er.names_match("Joseph Wu", "Sean Wu"))
        self.assertFalse(er.names_match("Joseph Wu", "Joseph Wong"))
        self.assertFalse(er.names_match("", "Joseph Wu"))

    def test_institutions_match_variants(self):
        self.assertTrue(er.institutions_match("Stanford University", "Stanford Cardiovascular Institute"))
        self.assertTrue(er.institutions_match("Univ. of Washington", "University of Washington"))
        self.assertFalse(er.institutions_match("Kyoto University", "Harvard Medical School"))
        self.assertFalse(er.institutions_match("", "Stanford"))
        self.assertFalse(er.institutions_match("The University", "Stanford"))


class UnionFindTest(unittest.TestCase):
    def test_transitive_union(self):
        uf = er.UnionFind(4)
        uf.union(0, 1)
        uf.union(1, 2)
        self.assertEqual(uf.find(0), uf.find(2))
        self.assertNotEqual(uf.find(0), uf.find(3))


class ClusteringTest(unittest.TestCase):
    def test_clusters_by_orcid_email_and_name_institution(self):
        candidates = [
            candidate("Joseph Wu", "Stanford University"),
            candidate("J. Wu", "Stanford Cardiovascular Institute"),
            candidate("Someone Else", "MIT", orcid="0000-1"),
            candidate("S. Else", "Elsewhere", orcid="0000-1"),
            candidate("A Person", "Kyoto University", email="A@x.org"),
            candidate("Another Name", "Osaka", email="a@X.org"),
            candidate("Unrelated Pi", "Harvard"),
        ]
        clusters = sorted(sorted(c) for c in er.cluster_candidates(candidates))
        self.assertEqual(clusters, [[0, 1], [2, 3], [4, 5], [6]])

    def test_empty_input(self):
        self.assertEqual(er.cluster_candidates([]), [])


class MergeTest(unittest.TestCase):
    def test_merge_cluster_combines_evidence(self):
        members = [
            candidate("J. Wu", "Stanford", source_record_type="papers",
                      metrics={"grant_funding_usd": None, "clinical_trial_ids": ["NCT1"], "patent_ids": []},
                      research_focus={"functional_vectors": ["arrhythmia mitigation"]},
                      translational_stage="Discovery/Basic Science", source_reference={"id": "p1"}),
            candidate("Joseph C. Wu", "Stanford Cardiovascular Institute", source_record_type="grants",
                      email="jwu@stanford.edu", geo_coordinates={"lat": 37.4, "lng": -122.2},
                      metrics={"grant_funding_usd": 1000, "clinical_trial_ids": ["NCT1", "NCT2"], "patent_ids": ["P1"]},
                      research_focus={"functional_vectors": ["vascularization", "arrhythmia mitigation"]},
                      translational_stage="Clinical Trial Phase I", source_reference={"id": "g1"}),
            candidate("Joseph Wu", "", source_record_type="grants",
                      metrics={"grant_funding_usd": 500}, translational_stage="Preclinical (Large Animal)"),
        ]
        merged = er.merge_cluster([0, 1, 2], members)
        self.assertEqual(merged["pi_full_name"], "Joseph C. Wu")
        self.assertEqual(merged["aka_names"], ["J. Wu", "Joseph Wu"])
        self.assertEqual(merged["institution"], "Stanford Cardiovascular Institute")
        self.assertEqual(merged["email"], "jwu@stanford.edu")
        self.assertEqual(merged["geo_coordinates"], {"lat": 37.4, "lng": -122.2})
        self.assertEqual(merged["metrics"]["grant_funding_usd"], 1500)
        self.assertEqual(merged["metrics"]["clinical_trial_ids"], ["NCT1", "NCT2"])
        self.assertEqual(merged["metrics"]["patent_ids"], ["P1"])
        self.assertEqual(merged["research_focus"]["functional_vectors"], ["arrhythmia mitigation", "vascularization"])
        self.assertEqual(merged["translational_stage"], "Clinical Trial Phase I")
        self.assertEqual(merged["source_record_count"], 3)
        self.assertEqual(merged["source_record_types"], ["grants", "papers"])

    def test_funding_is_none_when_no_member_reports_it(self):
        merged = er.merge_cluster([0], [candidate("A B", "X")])
        self.assertIsNone(merged["metrics"]["grant_funding_usd"])
        self.assertIsNone(merged["geo_coordinates"])

    def test_best_stage_falls_back_to_first_unranked_value(self):
        self.assertEqual(er._best_translational_stage(["", "Other"]), "Other")
        self.assertEqual(er._best_translational_stage([]), "")


if __name__ == "__main__":
    unittest.main()
