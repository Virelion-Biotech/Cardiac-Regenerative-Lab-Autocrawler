"""Pagination and retry behaviour of the network-facing steps, with all I/O mocked."""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from _helpers import FakeResponse, load_step

grants = load_step("2_harvest_grants.py")
trials = load_step("3_harvest_trials.py")
patents = load_step("10_harvest_patents.py")
classify = load_step("5_classify_relevance.py")


class PaginationTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nih_reporter_pages_until_total(self):
        pages = [
            FakeResponse({"results": [{"project_num": "1"}, {"project_num": "2"}], "meta": {"total": 3}}),
            FakeResponse({"results": [{"project_num": "3"}], "meta": {"total": 3}}),
        ]
        with mock.patch.object(grants.requests, "post", side_effect=pages) as post:
            rows = grants.nih_reporter_search("heart", page_size=2)
        self.assertEqual([r["project_num"] for r in rows], ["1", "2", "3"])
        self.assertEqual([c.kwargs["json"]["offset"] for c in post.call_args_list], [0, 2])

    def test_gtr_accepts_single_object_pages(self):
        pages = [FakeResponse({"project": [{"id": "a"}], "totalPages": 2}), FakeResponse({"project": {"id": "b"}, "totalPages": 2})]
        with mock.patch.object(grants.requests, "get", side_effect=pages):
            rows = grants.gtr_search("heart")
        self.assertEqual([r["project_num"] for r in rows], ["a", "b"])

    def test_horizon_europe_stops_on_empty_page(self):
        with mock.patch.object(grants.requests, "post", return_value=FakeResponse({"results": [], "totalResults": 50})) as post:
            self.assertEqual(grants.horizon_europe_search("heart"), [])
        self.assertEqual(post.call_count, 1)

    def test_ctgov_follows_page_tokens(self):
        pages = [
            FakeResponse({"studies": [{"protocolSection": {"identificationModule": {"nctId": "N1"}}}], "nextPageToken": "t2"}),
            FakeResponse({"studies": [{"protocolSection": {"identificationModule": {"nctId": "N2"}}}]}),
        ]
        with mock.patch.object(trials.requests, "get", side_effect=pages) as get:
            rows = trials.ctgov_search("heart")
        self.assertEqual([r["nct_id"] for r in rows], ["N1", "N2"])
        self.assertNotIn("pageToken", get.call_args_list[0].kwargs["params"])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["pageToken"], "t2")

    def test_patentsview_uses_last_id_as_cursor(self):
        pages = [
            FakeResponse({"patents": [{"patent_id": "1"}, {"patent_id": "2"}]}),
            FakeResponse({"patents": [{"patent_id": "3"}]}),
        ]
        with mock.patch.object(patents.requests, "get", side_effect=pages) as get:
            rows = patents.patentsview_search("patch", page_size=2)
        self.assertEqual([r["patent_id"] for r in rows], ["1", "2", "3"])
        second = json.loads(get.call_args_list[1].kwargs["params"]["o"])
        self.assertEqual(second, {"size": 2, "after": "2"})


class ClassifyRetryTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(classify.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _client(*texts):
        responses = [SimpleNamespace(content=[SimpleNamespace(type="text", text=t)]) for t in texts]
        return SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(side_effect=responses)))

    def test_retries_until_parseable(self):
        client = self._client("garbage", '[{"id": "a", "include": true}]')
        result = classify.classify_batch_sync(client, [{"id": "a", "text": "x"}])
        self.assertTrue(result["a"]["include"])
        self.assertEqual(client.messages.create.call_count, 2)

    def test_gives_up_and_flags_for_review(self):
        client = self._client(*["nope"] * (classify.MAX_RETRIES + 1))
        result = classify.classify_batch_sync(client, [{"id": "a", "text": "x"}])
        self.assertEqual(result["a"]["reason"], "CLASSIFICATION_FAILED_NEEDS_REVIEW")
        self.assertFalse(result["a"]["include"])


if __name__ == "__main__":
    unittest.main()
