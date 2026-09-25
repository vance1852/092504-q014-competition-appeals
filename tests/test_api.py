import unittest

from skills_workspace.api import route
from skills_workspace.service import DomainService
from skills_workspace.storage import Database


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "组委会"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "ad", "new_actor_id": "ad1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"}, {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "sec", "new_actor_id": "sec1", "display_name": "秘书",
               "role": "operator", "organization_id": "o1"}, {"X-Actor-Id": "ad1"})
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "site1", "organization_id": "o1",
               "name": "赛场", "timezone_name": "Asia/Shanghai"}, {"X-Actor-Id": "sec1"})

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/organizations", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_appeal_routes_require_existing_score_version(self):
        self._bootstrap()
        status, payload = route(self.service, "POST", "/appeals",
                                {"request_id": "f1", "score_version_id": "missing",
                                 "appellant_id": "sec1", "grounds": "x"},
                                {"X-Actor-Id": "sec1"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_full_appeal_workflow_over_http(self):
        self._bootstrap()
        headers = {"X-Actor-Id": "ad1"}
        for rid, aid, role, name in [
            ("arb1", "arb1", "reviewer", "裁决甲"),
            ("arb2", "arb2", "reviewer", "裁决乙"),
            ("comp1", "comp1", "competitor", "选手一"),
        ]:
            status, _ = route(self.service, "POST", "/actors",
                              {"request_id": rid, "new_actor_id": aid, "display_name": name,
                               "role": role, "organization_id": "o1"}, headers)
            self.assertEqual(201, status)

        status, published = route(self.service, "POST", "/score-versions",
                                  {"request_id": "p1", "site_id": "site1", "event_id": "ev1",
                                   "competitor_id": "comp1", "appeal_window_seconds": 3600,
                                   "result": {"score": 90}}, {"X-Actor-Id": "sec1"})
        self.assertEqual(201, status)

        status, filed = route(self.service, "POST", "/appeals",
                              {"request_id": "f1", "score_version_id": published["score_version_id"],
                               "appellant_id": "comp1", "grounds": "计时有误"},
                              {"X-Actor-Id": "comp1"})
        self.assertEqual(201, status)
        case_id = filed["case_id"]

        status, check = route(self.service, "POST", "/conflict-checks",
                              {"request_id": "cc1", "case_id": case_id, "arbitrator_id": "arb1"},
                              {"X-Actor-Id": "sec1"})
        self.assertEqual(200, status)
        self.assertFalse(check["conflicted"])

        status, assignment = route(self.service, "POST", "/appeal-assignments",
                                   {"request_id": "as1", "case_id": case_id},
                                   {"X-Actor-Id": "sec1"})
        self.assertEqual(201, status)
        self.assertEqual("arb1", assignment["arbitrator_id"])

        status, material = route(self.service, "POST", "/materials",
                                 {"request_id": "m1", "case_id": case_id, "material_id": "stmt",
                                  "material_kind": "statement",
                                  "content_sha256": "a" * 64, "storage_ref": "oss://s",
                                  "summary_text": "陈述", "content_type": "text/plain",
                                  "byte_length": 10}, {"X-Actor-Id": "comp1"})
        self.assertEqual(201, status)
        self.assertEqual(1, material["version"])

        status, recommendation = route(self.service, "POST", "/recommendations",
                                       {"request_id": "rc1", "case_id": case_id,
                                        "lease_token": assignment["lease_token"],
                                        "recommendation": "uphold", "rationale": "维持"},
                                       {"X-Actor-Id": "arb1"})
        self.assertEqual(201, status)
        self.assertEqual("under_review", recommendation["status"])

        status, _ = route(self.service, "POST", "/conflict-checks",
                          {"request_id": "cc2", "case_id": case_id, "arbitrator_id": "arb2"},
                          {"X-Actor-Id": "sec1"})
        self.assertEqual(200, status)

        status, reviewer = route(self.service, "POST", "/reviewer-assignments",
                                 {"request_id": "rv1", "case_id": case_id},
                                 {"X-Actor-Id": "sec1"})
        self.assertEqual(201, status)
        self.assertEqual("arb2", reviewer["reviewer_id"])

        status, decided = route(self.service, "POST", "/reviews",
                                {"request_id": "rvf1", "case_id": case_id, "approved": True,
                                 "rationale": "复核维持"},
                                {"X-Actor-Id": reviewer["reviewer_id"]})
        self.assertEqual(200, status)
        self.assertEqual("decided", decided["status"])

        status, public = route(self.service, "GET",
                               f"/public-result?public_token={decided['public_token']}", None)
        self.assertEqual(200, status)
        self.assertEqual("uphold", public["final_decision"])
        self.assertNotIn("comp1", str(public))

        status, timeline = route(self.service, "GET",
                                 f"/case-timeline?case_id={case_id}", None,
                                 {"X-Actor-Id": "ad1"})
        self.assertEqual(200, status)
        self.assertEqual(1, len(timeline["materials"]))

    def test_public_result_unknown_token_returns_404(self):
        status, payload = route(self.service, "GET",
                                "/public-result?public_token=nope", None)
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
