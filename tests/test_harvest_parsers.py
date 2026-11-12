import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from _helpers import FakeResponse, load_step

papers = load_step("1_harvest_papers.py")
grants = load_step("2_harvest_grants.py")
trials = load_step("3_harvest_trials.py")
patents = load_step("10_harvest_patents.py")


class PubmedParserTest(unittest.TestCase):
    def test_full_article_fields(self):
        article = ET.fromstring(
            "<PubmedArticle><PMID>42</PMID><ArticleTitle>Heart repair</ArticleTitle>"
            "<Abstract><AbstractText>Part one.</AbstractText><AbstractText>Part two.</AbstractText></Abstract>"
            "<Journal><Title>Circ Res</Title></Journal><PubDate><Year>2024</Year></PubDate>"
            "<ArticleIdList><ArticleId IdType='pubmed'>42</ArticleId><ArticleId IdType='doi'>10.1/xyz</ArticleId></ArticleIdList>"
            "<AuthorList><Author><LastName>Wu</LastName><ForeName>Joseph</ForeName>"
            "<AffiliationInfo><Affiliation>Stanford</Affiliation></AffiliationInfo></Author>"
            "<Author><CollectiveName>Consortium</CollectiveName></Author></AuthorList></PubmedArticle>"
        )
        row = papers._parse_pubmed_article(article)
        self.assertEqual(row["pmid"], "42")
        self.assertEqual(row["doi"], "10.1/xyz")
        self.assertEqual(row["abstract"], "Part one. Part two.")
        self.assertEqual(row["journal"], "Circ Res")
        self.assertEqual(row["pub_year"], "2024")
        self.assertEqual(row["authors"], [{"last_name": "Wu", "fore_name": "Joseph", "affiliation": "Stanford"}])

    def test_medline_date_fallback_and_missing_nodes(self):
        article = ET.fromstring("<PubmedArticle><PMID>7</PMID><PubDate><MedlineDate>2023 Jan-Feb</MedlineDate></PubDate></PubmedArticle>")
        row = papers._parse_pubmed_article(article)
        self.assertEqual(row["pub_year"], "2023")
        self.assertEqual(row["title"], "")
        self.assertEqual(row["abstract"], "")
        self.assertEqual(row["authors"], [])

    def test_pubmed_params_include_identity_and_optional_key(self):
        with mock.patch.object(papers.config, "NCBI_EMAIL", "a@b.org"), mock.patch.object(papers.config, "NCBI_API_KEY", ""):
            params = papers._pubmed_params({"term": "x"})
        self.assertEqual(params["email"], "a@b.org")
        self.assertNotIn("api_key", params)
        self.assertEqual(params["term"], "x")
        with mock.patch.object(papers.config, "NCBI_API_KEY", "k"):
            self.assertEqual(papers._pubmed_params({})["api_key"], "k")

    def test_pubmed_search_returns_id_list(self):
        payload = {"esearchresult": {"idlist": ["1", "2"]}}
        with mock.patch.object(papers.requests, "get", return_value=FakeResponse(payload)) as get:
            self.assertEqual(papers.pubmed_search("cardiac"), ["1", "2"])
        self.assertEqual(get.call_args.kwargs["params"]["term"], "cardiac")


class EuropePmcParserTest(unittest.TestCase):
    def test_preprint_detection(self):
        item = {"source": "PPR", "title": "T", "pubYear": "2025", "authorList": {"author": [{"lastName": "Li", "firstName": "A"}]}}
        row = papers._parse_europepmc_item(item)
        self.assertEqual(row["source"], "europepmc_preprint")
        self.assertEqual(row["authors"][0]["last_name"], "Li")

    def test_published_article(self):
        item = {"source": "MED", "pmid": "9", "journalInfo": {"journal": {"title": "Nature"}}}
        row = papers._parse_europepmc_item(item)
        self.assertEqual(row["source"], "europepmc")
        self.assertEqual(row["journal"], "Nature")
        self.assertEqual(row["authors"], [])

    def test_search_restricts_to_medline_when_preprints_excluded(self):
        with mock.patch.object(papers.requests, "get", return_value=FakeResponse({"resultList": {"result": [{"source": "MED"}]}})) as get:
            rows = papers.europepmc_search("heart", include_preprints=False)
        self.assertEqual(len(rows), 1)
        self.assertIn("SRC:MED", get.call_args.kwargs["params"]["query"])

    def test_harvest_term_survives_source_failures(self):
        import requests

        with mock.patch.object(papers, "pubmed_search", side_effect=requests.ConnectionError("down")), \
             mock.patch.object(papers, "europepmc_search", return_value=[{"source": "europepmc"}]), \
             mock.patch.object(papers.time, "sleep"):
            self.assertEqual(papers.harvest_term("x"), [{"source": "europepmc"}])


class GrantParserTest(unittest.TestCase):
    def test_reporter_item(self):
        item = {
            "project_num": "R01HL1", "project_title": "T", "fiscal_year": 2024, "award_amount": 500000,
            "org_name": "Stanford University", "principal_investigators": [{"full_name": "Joseph Wu", "first_name": "Joseph", "last_name": "Wu"}],
            "agency_ic_admin": {"name": "NHLBI"},
        }
        row = grants._parse_reporter_item(item)
        self.assertEqual(row["source"], "nih_reporter")
        self.assertEqual(row["award_amount"], 500000)
        self.assertEqual(row["funding_agency"], "NHLBI")
        self.assertEqual(row["principal_investigators"][0]["last_name"], "Wu")
        self.assertEqual(grants._parse_reporter_item({})["funding_agency"], "")

    def test_gtr_project_converts_gbp_and_resolves_links(self):
        item = {
            "id": "EP/1", "title": "T", "leadFunder": "BHF",
            "fund": {"start": "2023-04-01", "end": "2026-03-31", "valuePounds": {"amount": 1000}},
            "links": {"link": [{"rel": "PI_PER", "title": "Jane Doe"}, {"rel": "LEAD_ORG", "name": "Imperial College"}, {"rel": "PI_PER"}]},
        }
        row = grants._parse_gtr_project(item)
        self.assertEqual(row["award_amount"], 1270)
        self.assertEqual(row["award_amount_original_value"], 1000)
        self.assertEqual(row["fiscal_year"], "2023")
        self.assertEqual(row["contact_pi_name"], "Jane Doe")
        self.assertEqual(row["org_name"], "Imperial College")
        self.assertEqual(row["principal_investigators"][0]["full_name"], "Jane Doe")

    def test_gtr_without_fund_or_links(self):
        row = grants._parse_gtr_project({"id": "X"})
        self.assertIsNone(row["award_amount"])
        self.assertEqual(row["principal_investigators"], [])

    def test_horizon_europe_item(self):
        item = {"reference": "REF", "metadata": {"title": ["Regen"], "summary": ["S"], "startDate": ["2024-01-01"],
                                                  "ecMaxContribution": ["2500000.5"], "coordinatorName": ["KU Leuven"], "coordinatorCountry": ["BE"]}}
        row = grants._parse_horizon_europe_item(item)
        self.assertEqual(row["project_num"], "REF")
        self.assertEqual(row["abstract"], "S")
        self.assertEqual(row["award_amount"], 2500000.5)
        self.assertEqual(row["fiscal_year"], "2024")
        self.assertEqual(row["org_country"], "BE")

    def test_to_float(self):
        self.assertEqual(grants._to_float("3"), 3.0)
        self.assertIsNone(grants._to_float(""))
        self.assertIsNone(grants._to_float(None))

    def test_recent_fiscal_years_are_descending_and_contiguous(self):
        years = grants._recent_fiscal_years(3)
        self.assertEqual(len(years), 3)
        self.assertEqual(sorted(years), list(range(min(years), max(years) + 1)))


class TrialParserTest(unittest.TestCase):
    def test_ctgov_study(self):
        study = {"protocolSection": {
            "identificationModule": {"nctId": "NCT1", "briefTitle": "B"},
            "statusModule": {"overallStatus": "RECRUITING", "startDateStruct": {"date": "2022-05"}},
            "designModule": {"phases": ["PHASE1", "PHASE2"]},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Sponsor"}},
            "armsInterventionsModule": {"interventions": [{"name": "Cells"}]},
            "contactsLocationsModule": {"locations": [{"city": "Kyoto", "country": "Japan"}],
                                         "overallOfficials": [{"name": "Dr X", "affiliation": "Kyoto U"}]},
        }}
        row = trials._parse_ctgov_study(study)
        self.assertEqual(row["nct_id"], "NCT1")
        self.assertEqual(row["phase"], "PHASE1")
        self.assertEqual(row["interventions"], ["Cells"])
        self.assertEqual(row["locations"][0]["country"], "Japan")
        self.assertEqual(row["investigators"][0]["affiliation"], "Kyoto U")
        self.assertEqual(trials._parse_ctgov_study({})["phase"], "")

    def test_ctis_application_accepts_string_or_dict_member_states(self):
        app = {"ctNumber": "2024-1", "title": "T", "memberStatesConcerned": ["DE", {"country": "FR"}, None],
               "sponsor": {"name": "S"}, "investigationalMedicinalProducts": [{"name": "IMP"}]}
        row = trials._parse_ctis_application(app)
        self.assertEqual(row["nct_id"], "2024-1")
        self.assertEqual([loc["country"] for loc in row["locations"]], ["DE", "FR"])
        self.assertEqual(row["official_title"], "T")
        self.assertEqual(row["interventions"], ["IMP"])


class PatentParserTest(unittest.TestCase):
    def test_parse_patent_skips_blank_entries(self):
        item = {"patent_id": "123", "patent_title": "Patch", "patent_date": "2021-02-03",
                "assignees": [{"assignee_organization": "HeartCo", "assignee_country": "US"}, {}],
                "inventors": [{"inventor_name_first": "A", "inventor_name_last": "B"}, {"inventor_name_first": "NoLast"}]}
        row = patents._parse_patent(item)
        self.assertEqual(row["assignee_organizations"], ["HeartCo"])
        self.assertEqual(row["assignee_countries"], ["US"])
        self.assertEqual(row["inventors"], [{"first_name": "A", "last_name": "B"}])
        self.assertEqual(patents._parse_patent({"assignees": None, "inventors": None})["inventors"], [])


if __name__ == "__main__":
    unittest.main()
