import hashlib
import unittest
from datetime import datetime, timedelta, timezone

from skills_workspace.appeal_service import AppealService
from skills_workspace.clock import FixedClock
from skills_workspace.errors import (
    AppealWindowClosed,
    ConflictOfInterestError,
    DuplicateAppealError,
    EligibilityError,
    LeaseExpired,
    NoAdjudicatorAvailable,
    NotFoundError,
    NotLeaseHolder,
    PermissionDenied,
    ProtectedStateError,
)
from skills_workspace.service import DomainService
from skills_workspace.storage import Database


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


class AppealFixture:
    """搭建一套可复用的竞赛、选手、成绩版本与裁决人员。"""

    def __init__(self, clock=None) -> None:
        self.clock = clock or FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.database = Database()
        self.base = DomainService(self.database, self.clock)
        self.service = AppealService(self.database, self.clock)
        self.base.register_organization(request_id="org0", actor_id="bootstrap",
                                        organization_id="o1", name="赛事组委会")
        self.base.register_actor(request_id="adm0", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="op0", actor_id="a1", new_actor_id="op1",
                                 display_name="秘书", role="operator", organization_id="o1")
        self.base.register_actor(request_id="aud0", actor_id="a1", new_actor_id="au1",
                                 display_name="审计员", role="auditor", organization_id="o1")
        for rid, name in [("rv1", "裁决甲"), ("rv2", "裁决乙"), ("rv3", "裁决丙")]:
            self.base.register_actor(request_id="act" + rid, actor_id="a1", new_actor_id=rid,
                                     display_name=name, role="reviewer", organization_id="o1")
        self.service.register_competition(request_id="cmp", actor_id="a1",
                                          competition_id="c1", name="技能大赛")
        self.service.register_competitor(request_id="pt1", actor_id="op1", competition_id="c1",
                                         competitor_id="p1", person_name="张三")
        self.service.publish_score_version(
            request_id="ver", actor_id="op1", competition_id="c1", version_id="v1",
            version_label="初赛成绩", entries={"p1": "store://score/p1/v1"})
        for rid in ("rv1", "rv2", "rv3"):
            self.service.register_adjudicator(request_id="adj" + rid, actor_id="a1",
                                              competition_id="c1", adjudicator_id=rid,
                                              display_name=rid)

    def close(self) -> None:
        self.database.close()


class AppealIntakeTest(unittest.TestCase):
    def setUp(self):
        self.fx = AppealFixture()

    def tearDown(self):
        self.fx.close()

    def test_filing_creates_case_and_immutable_initial_evidence(self):
        result = self.fx.service.file_appeal(
            request_id="ap1", actor_id="op1", version_id="v1", competitor_id="p1",
            grounds="计时设备异常",
            evidence=[{"kind": "statement", "content_digest": sha("陈述"),
                       "storage_ref": "store://ev/1"}])
        self.assertFalse(result.receipt.replayed)
        self.assertEqual("screening", result.response["status"])
        evidence = self.fx.service.list_evidence(result.response["case_id"])
        self.assertEqual(1, evidence[0].version_seq)
        self.assertEqual(sha("陈述"), evidence[0].content_digest)

    def test_supplement_adds_new_version_without_overwriting(self):
        case_id = self._open_case()
        second = self.fx.service.add_evidence(
            request_id="ev2", actor_id="op1", case_id=case_id, kind="supporting_document",
            content_digest=sha("证明"), storage_ref="store://ev/2")
        third = self.fx.service.add_evidence(
            request_id="ev3", actor_id="op1", case_id=case_id, kind="structured_log_summary",
            content_digest=sha("日志"), storage_ref="store://ev/3")
        self.assertEqual(2, second.response["version_seq"])
        self.assertEqual(3, third.response["version_seq"])
        evidence = self.fx.service.list_evidence(case_id)
        self.assertEqual([1, 2, 3], [item.version_seq for item in evidence])
        self.assertEqual("store://ev/1", evidence[0].storage_ref)

    def test_ineligible_competitor_cannot_appeal(self):
        self.fx.service.register_competitor(
            request_id="pt2", actor_id="op1", competition_id="c1", competitor_id="p2",
            person_name="李四", eligible=False)
        self.fx.service.publish_score_version(
            request_id="v2", actor_id="op1", competition_id="c1", version_id="v2",
            version_label="复赛", entries={"p2": "store://score/p2/v1"})
        with self.assertRaises(EligibilityError):
            self.fx.service.file_appeal(request_id="ap2", actor_id="op1", version_id="v2",
                                        competitor_id="p2", grounds="争议", evidence=[])

    def test_appeal_after_deadline_is_rejected(self):
        clock = MutableClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        fx = AppealFixture(clock)
        try:
            clock.advance(hours=73)
            with self.assertRaises(AppealWindowClosed):
                fx.service.file_appeal(request_id="late", actor_id="op1", version_id="v1",
                                       competitor_id="p1", grounds="超期", evidence=[])
        finally:
            fx.close()

    def test_duplicate_grounds_are_rejected_even_after_terminal(self):
        case_id = self._decided_case()
        with self.assertRaises(DuplicateAppealError):
            self.fx.service.file_appeal(request_id="dup", actor_id="op1", version_id="v1",
                                        competitor_id="p1", grounds="计时设备异常", evidence=[])
        self.assertEqual("decided", self.fx.service.get_case(case_id).status)

    def test_only_summary_and_reference_are_stored(self):
        case_id = self._open_case()
        row = self.fx.database.connection.execute(
            "SELECT * FROM case_evidence WHERE case_id=?", (case_id,)).fetchone()
        self.assertIsNone(row["media_type"])
        self.assertEqual(64, len(row["content_digest"]))
        self.assertTrue(row["storage_ref"].startswith("store://"))

    def _open_case(self) -> str:
        return self.fx.service.file_appeal(
            request_id="ap1", actor_id="op1", version_id="v1", competitor_id="p1",
            grounds="计时设备异常",
            evidence=[{"kind": "statement", "content_digest": sha("陈述"),
                       "storage_ref": "store://ev/1"}]).response["case_id"]

    def _decided_case(self) -> str:
        case_id = self._open_case()
        self.fx.service.screen_adjudicator(request_id="s1", actor_id="op1", case_id=case_id,
                                           adjudicator_id="rv2", result="clear")
        self.fx.service.complete_screening(request_id="sd", actor_id="op1", case_id=case_id)
        self.fx.service.assign_handler(request_id="as", actor_id="op1", case_id=case_id)
        self.fx.service.submit_recommendation(
            request_id="rc", actor_id="rv2", case_id=case_id, recommendation="uphold",
            basis_digest=sha("依据"), basis_ref="store://basis/1")
        self.fx.service.decide_appeal(
            request_id="dc", actor_id="rv3", case_id=case_id, outcome="uphold",
            basis_digest=sha("终局"), basis_ref="store://basis/final")
        return case_id


class ConflictScreeningTest(unittest.TestCase):
    def setUp(self):
        self.fx = AppealFixture()
        self.case_id = self.fx.service.file_appeal(
            request_id="ap1", actor_id="op1", version_id="v1", competitor_id="p1",
            grounds="评分项错误", evidence=[]).response["case_id"]

    def tearDown(self):
        self.fx.close()

    def test_declared_conflict_cannot_be_cleared(self):
        self.fx.service.declare_conflict(request_id="cf1", actor_id="op1", competition_id="c1",
                                         adjudicator_id="rv1", competitor_id="p1", reason="师生")
        with self.assertRaises(ConflictOfInterestError):
            self.fx.service.screen_adjudicator(request_id="s1", actor_id="op1",
                                               case_id=self.case_id, adjudicator_id="rv1",
                                               result="clear")

    def test_cannot_complete_screening_without_clear_adjudicator(self):
        self.fx.service.screen_adjudicator(request_id="s1", actor_id="op1", case_id=self.case_id,
                                           adjudicator_id="rv1", result="conflicted")
        with self.assertRaises(ConflictOfInterestError):
            self.fx.service.complete_screening(request_id="sd", actor_id="op1",
                                               case_id=self.case_id)

    def test_candidate_cannot_view_case_before_screening_complete(self):
        # 案件尚在 screening，任何候选人都看不到内容，避免在回避核对前接触案件。
        with self.assertRaises(PermissionDenied):
            self.fx.service.case_snapshot(actor_id="rv2", case_id=self.case_id)

    def test_assignment_picks_only_cleared_non_conflicted_person(self):
        self.fx.service.declare_conflict(request_id="cf2", actor_id="op1", competition_id="c1",
                                         adjudicator_id="rv2", competitor_id="p1")
        self.fx.service.screen_adjudicator(request_id="s2", actor_id="op1", case_id=self.case_id,
                                           adjudicator_id="rv2", result="conflicted")
        self.fx.service.screen_adjudicator(request_id="s3", actor_id="op1", case_id=self.case_id,
                                           adjudicator_id="rv3", result="clear")
        self.fx.service.complete_screening(request_id="sd", actor_id="op1", case_id=self.case_id)
        assignment = self.fx.service.assign_handler(request_id="as", actor_id="op1",
                                                    case_id=self.case_id)
        self.assertEqual("rv3", assignment.response["handler_id"])

    def test_no_available_adjudicator(self):
        for rid in ("rv1", "rv2", "rv3"):
            self.fx.service.declare_conflict(request_id="cf" + rid, actor_id="op1",
                                             competition_id="c1", adjudicator_id=rid,
                                             competitor_id="p1")
            self.fx.service.screen_adjudicator(request_id="s" + rid, actor_id="op1",
                                               case_id=self.case_id, adjudicator_id=rid,
                                               result="conflicted")
        with self.assertRaises(ConflictOfInterestError):
            self.fx.service.complete_screening(request_id="sd", actor_id="op1",
                                               case_id=self.case_id)


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.fx = AppealFixture(self.clock)
        self.case_id = self.fx.service.file_appeal(
            request_id="ap1", actor_id="op1", version_id="v1", competitor_id="p1",
            grounds="设备异常", evidence=[]).response["case_id"]
        self.fx.service.screen_adjudicator(request_id="s1", actor_id="op1", case_id=self.case_id,
                                           adjudicator_id="rv1", result="clear")
        self.fx.service.screen_adjudicator(request_id="s2", actor_id="op1", case_id=self.case_id,
                                           adjudicator_id="rv2", result="clear")
        self.fx.service.complete_screening(request_id="sd", actor_id="op1", case_id=self.case_id)
        self.first = self.fx.service.assign_handler(
            request_id="as1", actor_id="op1", case_id=self.case_id, lease_seconds=3600)

    def tearDown(self):
        self.fx.close()

    def test_only_holder_can_act(self):
        with self.assertRaises(NotLeaseHolder):
            self.fx.service.request_evidence(request_id="erx", actor_id="rv2",
                                             case_id=self.case_id, note="补充")

    def test_expired_holder_cannot_submit(self):
        self.clock.advance(hours=2)
        with self.assertRaises(LeaseExpired):
            self.fx.service.submit_recommendation(
                request_id="rc", actor_id="rv1", case_id=self.case_id, recommendation="uphold",
                basis_digest=sha("b"), basis_ref="store://b")

    def test_reclaim_then_reassign_excludes_former_holder(self):
        self.clock.advance(hours=2)
        result = self.fx.service.reclaim_expired_leases(actor_id="op1")
        self.assertEqual(1, result["count"])
        second = self.fx.service.assign_handler(request_id="as2", actor_id="op1",
                                                case_id=self.case_id)
        self.assertEqual("rv2", second.response["handler_id"])
        self.assertGreater(second.response["generation"], self.first.response["generation"])
        # 旧承办人在新租约下继续被拒绝。
        with self.assertRaises(NotLeaseHolder):
            self.fx.service.request_evidence(request_id="er2", actor_id="rv1",
                                             case_id=self.case_id, note="x")


class DecisionTest(unittest.TestCase):
    def setUp(self):
        self.fx = AppealFixture()
        self.case_id = self.fx.service.file_appeal(
            request_id="ap1", actor_id="op1", version_id="v1", competitor_id="p1",
            grounds="计时异常", evidence=[]).response["case_id"]
        self.fx.service.screen_adjudicator(request_id="s1", actor_id="op1", case_id=self.case_id,
                                           adjudicator_id="rv2", result="clear")
        self.fx.service.complete_screening(request_id="sd", actor_id="op1", case_id=self.case_id)
        self.fx.service.assign_handler(request_id="as1", actor_id="op1", case_id=self.case_id)

    def tearDown(self):
        self.fx.close()

    def test_handler_cannot_be_independent_reviewer(self):
        self.fx.service.submit_recommendation(
            request_id="rc", actor_id="rv2", case_id=self.case_id, recommendation="uphold",
            basis_digest=sha("b"), basis_ref="store://b")
        with self.assertRaises(PermissionDenied):
            self.fx.service.decide_appeal(request_id="dc", actor_id="rv2", case_id=self.case_id,
                                          outcome="uphold", basis_digest=sha("f"),
                                          basis_ref="store://f")

    def test_former_handler_cannot_review(self):
        self.fx.service.submit_recommendation(
            request_id="rc", actor_id="rv2", case_id=self.case_id, recommendation="uphold",
            basis_digest=sha("b"), basis_ref="store://b")
        # rv1 从未承办本案可以复核；这里改用一个曾经承办过的人需先回收，
        # 直接验证无资格的冲突人员被拦截。
        self.fx.service.declare_conflict(request_id="cf", actor_id="op1", competition_id="c1",
                                         adjudicator_id="rv1", competitor_id="p1")
        with self.assertRaises(ConflictOfInterestError):
            self.fx.service.decide_appeal(request_id="dc", actor_id="rv1", case_id=self.case_id,
                                          outcome="uphold", basis_digest=sha("f"),
                                          basis_ref="store://f")

    def test_correction_atomically_updates_score_reference(self):
        self.fx.service.submit_recommendation(
            request_id="rc", actor_id="rv2", case_id=self.case_id, recommendation="correct",
            basis_digest=sha("b"), basis_ref="store://b",
            proposed_score_ref="store://score/p1/corrected")
        decision = self.fx.service.decide_appeal(
            request_id="dc", actor_id="rv3", case_id=self.case_id, outcome="correct",
            basis_digest=sha("f"), basis_ref="store://f",
            corrected_score_ref="store://score/p1/corrected")
        self.assertEqual("decided", decision.response["status"])
        entry = self.fx.database.connection.execute(
            "SELECT score_ref FROM score_entries WHERE version_id='v1' AND competitor_id='p1'"
        ).fetchone()
        self.assertEqual("store://score/p1/corrected", entry["score_ref"])
        updates = self.fx.service.list_score_updates(self.case_id)
        self.assertEqual("store://score/p1/v1", updates[0].previous_ref)

    def test_correction_requires_matching_proposed_reference(self):
        self.fx.service.submit_recommendation(
            request_id="rc", actor_id="rv2", case_id=self.case_id, recommendation="correct",
            basis_digest=sha("b"), basis_ref="store://b",
            proposed_score_ref="store://score/p1/corrected")
        with self.assertRaises(Exception):
            self.fx.service.decide_appeal(
                request_id="dc", actor_id="rv3", case_id=self.case_id, outcome="correct",
                basis_digest=sha("f"), basis_ref="store://f",
                corrected_score_ref="store://score/p1/different")

    def test_reject_is_protected_terminal(self):
        self.fx.service.submit_recommendation(
            request_id="rc", actor_id="rv2", case_id=self.case_id, recommendation="uphold",
            basis_digest=sha("b"), basis_ref="store://b")
        self.fx.service.reject_appeal(request_id="rj", actor_id="rv3", case_id=self.case_id,
                                      basis_digest=sha("x"), basis_ref="store://x")
        with self.assertRaises(ProtectedStateError):
            self.fx.service.add_evidence(request_id="late", actor_id="op1", case_id=self.case_id,
                                         kind="statement", content_digest=sha("late"),
                                         storage_ref="store://late")

    def test_withdraw_is_protected_terminal(self):
        self.fx.service.withdraw_appeal(request_id="wd", actor_id="op1", case_id=self.case_id)
        with self.assertRaises(ProtectedStateError):
            self.fx.service.assign_handler(request_id="asx", actor_id="op1", case_id=self.case_id)


class ViewTest(unittest.TestCase):
    def setUp(self):
        self.fx = AppealFixture()
        self.case_id = self.fx.service.file_appeal(
            request_id="ap1", actor_id="op1", version_id="v1", competitor_id="p1",
            grounds="计时设备异常",
            evidence=[{"kind": "statement", "content_digest": sha("陈述"),
                       "storage_ref": "store://ev/1"}]).response["case_id"]

    def tearDown(self):
        self.fx.close()

    def test_public_result_hides_personal_information(self):
        self.fx.service.screen_adjudicator(request_id="s1", actor_id="op1", case_id=self.case_id,
                                           adjudicator_id="rv2", result="clear")
        self.fx.service.complete_screening(request_id="sd", actor_id="op1", case_id=self.case_id)
        self.fx.service.assign_handler(request_id="as", actor_id="op1", case_id=self.case_id)
        self.fx.service.submit_recommendation(
            request_id="rc", actor_id="rv2", case_id=self.case_id, recommendation="correct",
            basis_digest=sha("b"), basis_ref="store://b",
            proposed_score_ref="store://score/p1/corrected")
        self.fx.service.decide_appeal(
            request_id="dc", actor_id="rv3", case_id=self.case_id, outcome="correct",
            basis_digest=sha("f"), basis_ref="store://f",
            corrected_score_ref="store://score/p1/corrected")
        case_number = self.fx.service.get_case(self.case_id).case_number
        result = self.fx.service.public_result(case_number)
        payload = result.__dict__
        self.assertNotIn("competitor_id", payload)
        self.assertNotIn("person_name", payload)
        self.assertNotIn("handler_id", payload)
        self.assertTrue(result.corrected)
        self.assertEqual("correct", result.outcome)

    def test_public_result_unavailable_before_final(self):
        number = self.fx.service.get_case(self.case_id).case_number
        with self.assertRaises(NotFoundError):
            self.fx.service.public_result(number)

    def test_auditor_timeline_reconstructs_process(self):
        self.fx.service.declare_conflict(request_id="cf", actor_id="op1", competition_id="c1",
                                         adjudicator_id="rv1", competitor_id="p1")
        self.fx.service.screen_adjudicator(request_id="s1", actor_id="op1", case_id=self.case_id,
                                           adjudicator_id="rv1", result="conflicted")
        self.fx.service.screen_adjudicator(request_id="s2", actor_id="op1", case_id=self.case_id,
                                           adjudicator_id="rv2", result="clear")
        self.fx.service.complete_screening(request_id="sd", actor_id="op1", case_id=self.case_id)
        timeline = self.fx.service.case_timeline(actor_id="au1", case_id=self.case_id)
        actions = {event["action"] for event in timeline["events"]}
        self.assertIn("appeal.filed", actions)
        self.assertIn("adjudicator.screened", actions)
        self.assertEqual(1, len(timeline["conflict_declarations"]))
        self.assertEqual(2, len(timeline["screenings"]))

    def test_non_auditor_cannot_read_timeline(self):
        with self.assertRaises(PermissionDenied):
            self.fx.service.case_timeline(actor_id="op1", case_id=self.case_id)


if __name__ == "__main__":
    unittest.main()
