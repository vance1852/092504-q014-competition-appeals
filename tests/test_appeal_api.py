import hashlib
import unittest
from datetime import datetime, timezone

from skills_workspace.api import route
from skills_workspace.appeal_service import AppealService
from skills_workspace.clock import FixedClock
from skills_workspace.service import DomainService
from skills_workspace.storage import Database


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AppealApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        self.appeals = AppealService(self.database, clock)
        self.service.register_organization(request_id="org0", actor_id="bootstrap",
                                           organization_id="o1", name="组委会")
        self.service.register_actor(request_id="adm0", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op0", actor_id="a1", new_actor_id="op1",
                                    display_name="秘书", role="operator", organization_id="o1")
        self.service.register_actor(request_id="actrv", actor_id="a1", new_actor_id="rv1",
                                    display_name="裁决", role="reviewer", organization_id="o1")
        self.appeals.register_competition(request_id="cmp", actor_id="a1",
                                          competition_id="c1", name="大赛")
        self.appeals.register_competitor(request_id="pt1", actor_id="op1", competition_id="c1",
                                         competitor_id="p1", person_name="张三")
        self.appeals.publish_score_version(request_id="ver", actor_id="op1", competition_id="c1",
                                           version_id="v1", version_label="初赛",
                                           entries={"p1": "store://v1"})
        self.appeals.register_adjudicator(request_id="adj", actor_id="a1", competition_id="c1",
                                          adjudicator_id="rv1", display_name="裁决")

    def tearDown(self):
        self.database.close()

    def _file(self):
        status, payload = route(self.service, "POST", "/appeals", {
            "request_id": "ap1", "version_id": "v1", "competitor_id": "p1",
            "grounds": "计时异常", "evidence": []}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        return payload["response"]["case_id"]

    def test_full_appeal_flow_over_http(self):
        case_id = self._file()
        status, payload = route(self.service, "POST",
                                f"/appeals/{case_id}/screenings/check",
                                {"request_id": "s1", "adjudicator_id": "rv1", "result": "clear"},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST",
                                f"/appeals/{case_id}/screening/complete",
                                {"request_id": "sd"}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST",
                                f"/appeals/{case_id}/assignment/assign",
                                {"request_id": "as1"}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        self.assertEqual("rv1", payload["response"]["handler_id"])

    def test_appeal_window_closed_returns_conflict_status(self):
        # 越过 72 小时申诉期限（用一个已截止的版本）。
        self.appeals.publish_score_version(
            request_id="vold", actor_id="op1", competition_id="c1", version_id="v0",
            version_label="旧成绩", entries={"p1": "store://v0"},
            published_at="2026-09-20T00:00:00Z")
        status, payload = route(self.service, "POST", "/appeals", {
            "request_id": "old", "version_id": "v0", "competitor_id": "p1",
            "grounds": "超期", "evidence": []}, {"X-Actor-Id": "op1"})
        self.assertEqual(409, status)
        self.assertEqual("appeal_window_closed", payload["error"])

    def test_replayed_appeal_returns_200(self):
        self._file()
        status, payload = route(self.service, "POST", "/appeals", {
            "request_id": "ap1", "version_id": "v1", "competitor_id": "p1",
            "grounds": "计时异常", "evidence": []}, {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_evidence_versions_listed(self):
        case_id = self._file()
        status, payload = route(self.service, "GET", f"/appeals/{case_id}/evidence", None,
                                {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertEqual([], payload["items"])

    def test_health_still_works(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])


if __name__ == "__main__":
    unittest.main()
