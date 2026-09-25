import hashlib
import unittest
from datetime import datetime, timedelta, timezone

from skills_workspace.appeals import AppealService
from skills_workspace.errors import (
    ConflictError,
    EligibilityError,
    LeaseError,
    NotFoundError,
    PermissionDenied,
    ProtectedStateError,
    ValidationError,
)
from skills_workspace.service import DomainService
from skills_workspace.storage import Database

BASE = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, **delta):
        self.value += timedelta(**delta)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AppealFixture(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(BASE)
        self.service = DomainService(self.database, self.clock)
        self.appeals = AppealService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="赛事组委会")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="ad1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="sec", actor_id="ad1", new_actor_id="sec1",
                                    display_name="秘书", role="operator", organization_id="o1")
        for rid, aid, name in [
            ("arb-a", "arb1", "裁决甲"), ("arb-b", "arb2", "裁决乙"),
            ("arb-c", "arb3", "裁决丙"),
        ]:
            self.service.register_actor(request_id=rid, actor_id="ad1", new_actor_id=aid,
                                        display_name=name, role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="comp", actor_id="ad1", new_actor_id="comp1",
                                    display_name="选手一", role="competitor", organization_id="o1")
        self.service.register_actor(request_id="aud", actor_id="ad1", new_actor_id="aud1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="sec1", site_id="site1",
                                   organization_id="o1", name="赛场",
                                   timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def publish(self, *, event_id="ev1", window=86400, result=None, request_id="pub1"):
        return self.appeals.publish_score_version(
            request_id=request_id, actor_id="sec1", site_id="site1", event_id=event_id,
            competitor_id="comp1", appeal_window_seconds=window,
            result=result if result is not None else {"score": 90},
        )

    def file(self, score_version_id, *, request_id="file1", actor_id="comp1", grounds="计时有误"):
        return self.appeals.file_appeal(
            request_id=request_id, actor_id=actor_id, score_version_id=score_version_id,
            appellant_id="comp1", grounds=grounds,
        )

    def clear(self, case_id, *arbitrators):
        for index, arbitrator in enumerate(arbitrators):
            self.appeals.record_conflict_check(
                request_id=f"cc-{case_id[:6]}-{arbitrator}", actor_id="sec1", case_id=case_id,
                arbitrator_id=arbitrator,
            )

    def assign(self, case_id, *, request_id="assign1"):
        return self.appeals.assign_case(request_id=request_id, actor_id="sec1", case_id=case_id)


class PublishAndEligibilityTest(AppealFixture):
    def test_publish_versions_increment_and_replay(self):
        first = self.publish(request_id="p1")
        second = self.publish(request_id="p2")
        self.assertEqual((first["version"], second["version"]), (1, 2))
        replay = self.publish(request_id="p1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["score_version_id"], first["score_version_id"])

    def test_appeal_after_deadline_is_rejected(self):
        published = self.publish(window=3600)
        self.clock.advance(hours=2)
        with self.assertRaises(EligibilityError):
            self.file(published["score_version_id"])

    def test_duplicate_appeal_rejected(self):
        published = self.publish()
        self.file(published["score_version_id"])
        with self.assertRaises(EligibilityError):
            self.file(published["score_version_id"], request_id="file2")

    def test_appeal_against_superseded_version_rejected(self):
        first = self.publish(request_id="p1")
        case_id = self.file(first["score_version_id"])["case_id"]
        self.clear(case_id, "arb1", "arb2")
        assignment = self.assign(case_id)
        self.appeals.recommend_decision(
            request_id="rec1", actor_id=assignment["arbitrator_id"], case_id=case_id,
            lease_token=assignment["lease_token"], recommendation="correct",
            rationale="计时偏差", corrected_result={"score": 93},
        )
        reviewer = self.appeals.assign_reviewer(request_id="rev-assign", actor_id="sec1",
                                                case_id=case_id)["reviewer_id"]
        self.appeals.review_decision(request_id="rev1", actor_id=reviewer, case_id=case_id,
                                     approved=True, rationale="确认")
        self.clock.advance(hours=1)
        with self.assertRaises(EligibilityError):
            self.file(first["score_version_id"], request_id="file-old")

    def test_competitor_cannot_appeal_for_other(self):
        self.service.register_actor(request_id="comp2", actor_id="ad1", new_actor_id="comp2",
                                    display_name="选手二", role="competitor", organization_id="o1")
        published = self.publish()
        with self.assertRaises(PermissionDenied):
            self.appeals.file_appeal(request_id="f-x", actor_id="comp2",
                                     score_version_id=published["score_version_id"],
                                     appellant_id="comp1", grounds="代申诉")

    def test_invalid_result_shape(self):
        with self.assertRaises(ValidationError):
            self.appeals.publish_score_version(request_id="bad", actor_id="sec1", site_id="site1",
                                               event_id="ev1", competitor_id="comp1",
                                               appeal_window_seconds=60, result={})


class ConflictAndAssignmentTest(AppealFixture):
    def test_disclosed_relationship_conflicts_and_blocks_assignment(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        self.appeals.register_disclosure(request_id="disc1", actor_id="arb1",
                                         arbitrator_id="arb1", scope_type="event",
                                         scope_value="ev1", note="裁判长")
        result = self.appeals.record_conflict_check(request_id="cc1", actor_id="sec1",
                                                    case_id=case_id, arbitrator_id="arb1")
        self.assertTrue(result["conflicted"])
        self.assertIn("披露与赛项存在关联", result["objective_reasons"])
        with self.assertRaises(EligibilityError):
            self.assign(case_id)

    def test_self_party_is_objective_conflict(self):
        # 选手本人不可能同时是裁决人员；正常候选不会命中本人冲突
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        result = self.appeals.record_conflict_check(request_id="cc-self", actor_id="sec1",
                                                    case_id=case_id, arbitrator_id="arb1")
        self.assertFalse(result["conflicted"])

    def test_assign_requires_filed_state(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        self.clear(case_id, "arb1")
        self.assign(case_id)
        with self.assertRaises(ConflictError):
            self.assign(case_id, request_id="assign2")

    def test_assign_picks_cleared_candidate(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        self.appeals.register_disclosure(request_id="disc2", actor_id="arb1",
                                         arbitrator_id="arb1", scope_type="competitor",
                                         scope_value="comp1")
        self.clear(case_id, "arb1", "arb2")
        assignment = self.assign(case_id)
        self.assertEqual(assignment["arbitrator_id"], "arb2")

    def test_assign_is_idempotent_with_same_token(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        self.clear(case_id, "arb1")
        first = self.assign(case_id)
        replay = self.assign(case_id)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["lease_token"], first["lease_token"])


class LeaseTest(AppealFixture):
    def _assigned_case(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        self.clear(case_id, "arb1")
        assignment = self.assign(case_id)
        return case_id, assignment

    def test_expired_token_cannot_submit(self):
        case_id, assignment = self._assigned_case()
        self.clock.advance(minutes=31)
        with self.assertRaises(LeaseError):
            self.appeals.request_supplement(
                request_id="sup1", actor_id="arb1", case_id=case_id,
                lease_token=assignment["lease_token"], note="请补录像",
            )

    def test_reclaim_returns_case_to_filed_and_old_holder_blocked(self):
        case_id, assignment = self._assigned_case()
        self.clock.advance(minutes=31)
        result = self.appeals.reclaim_expired_leases(actor_id="sec1")
        self.assertEqual(result["reclaimed"], [case_id])
        with self.assertRaises(PermissionDenied):
            self.appeals.request_supplement(
                request_id="sup2", actor_id="arb1", case_id=case_id,
                lease_token=assignment["lease_token"], note="迟交",
            )
        view = self.appeals.get_case_view(actor_id="sec1", case_id=case_id)
        self.assertEqual(view["status"], "filed")
        self.assertIsNone(view["assigned_arbitrator_id"])

    def test_renew_rotates_token_and_old_token_fails(self):
        case_id, assignment = self._assigned_case()
        renewed = self.appeals.renew_lease(actor_id="arb1", case_id=case_id,
                                           lease_token=assignment["lease_token"])
        self.assertNotEqual(renewed["lease_token"], assignment["lease_token"])
        with self.assertRaises(LeaseError):
            self.appeals.request_supplement(
                request_id="sup3", actor_id="arb1", case_id=case_id,
                lease_token=assignment["lease_token"], note="旧代号",
            )

    def test_other_arbitrator_cannot_use_lease(self):
        case_id, assignment = self._assigned_case()
        with self.assertRaises(PermissionDenied):
            self.appeals.request_supplement(
                request_id="sup4", actor_id="arb2", case_id=case_id,
                lease_token=assignment["lease_token"], note="越权",
            )


class MaterialSealingTest(AppealFixture):
    def test_supplements_form_immutable_versions(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        first = self.appeals.submit_material(
            request_id="m1", actor_id="comp1", case_id=case_id, material_id="stmt",
            material_kind="statement", content_sha256=sha("一"), storage_ref="oss://a",
            summary_text="陈述一", content_type="text/plain", byte_length=100,
        )
        second = self.appeals.submit_material(
            request_id="m2", actor_id="comp1", case_id=case_id, material_id="stmt",
            material_kind="statement", content_sha256=sha("二"), storage_ref="oss://b",
            summary_text="陈述二", content_type="text/plain", byte_length=120,
        )
        self.assertEqual((first["version"], second["version"]), (1, 2))
        materials = self.appeals.list_materials(actor_id="comp1", case_id=case_id)
        self.assertEqual(len(materials), 2)
        self.assertEqual(materials[0]["content_sha256"], sha("一"))
        self.assertEqual(materials[1]["content_sha256"], sha("二"))

    def test_replay_does_not_create_new_version(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        args = dict(actor_id="comp1", case_id=case_id, material_id="stmt",
                    material_kind="statement", content_sha256=sha("一"),
                    storage_ref="oss://a", summary_text="陈述一",
                    content_type="text/plain", byte_length=100)
        first = self.appeals.submit_material(request_id="m1", **args)
        replay = self.appeals.submit_material(request_id="m1", **args)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["version"], first["version"])
        self.assertEqual(len(self.appeals.list_materials(actor_id="comp1", case_id=case_id)), 1)

    def test_kind_change_on_same_material_id_rejected(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        common = dict(actor_id="comp1", case_id=case_id, material_id="doc",
                      content_sha256=sha("x"), storage_ref="oss://a",
                      summary_text="x", content_type="text/plain", byte_length=10)
        self.appeals.submit_material(request_id="m1", material_kind="statement", **common)
        with self.assertRaises(ConflictError):
            self.appeals.submit_material(request_id="m2", material_kind="log_summary", **common)

    def test_bad_hash_rejected(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        with self.assertRaises(ValidationError):
            self.appeals.submit_material(
                request_id="m-bad", actor_id="comp1", case_id=case_id, material_id="doc",
                material_kind="statement", content_sha256="not-a-hash",
                storage_ref="oss://a", summary_text="x", content_type="text/plain", byte_length=1,
            )

    def test_arbitrator_needs_valid_lease_to_seal(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        self.clear(case_id, "arb1")
        assignment = self.assign(case_id)
        args = dict(actor_id="arb1", case_id=case_id, material_id="log",
                    material_kind="log_summary", content_sha256=sha("l"),
                    storage_ref="oss://log", summary_text="日志摘要",
                    content_type="application/json", byte_length=10)
        with self.assertRaises(LeaseError):
            self.appeals.submit_material(request_id="m-no-token", lease_token="wrong", **args)
        sealed = self.appeals.submit_material(request_id="m-ok",
                                              lease_token=assignment["lease_token"], **args)
        self.assertEqual(sealed["version"], 1)

    def test_material_sealed_after_review_starts(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        self.clear(case_id, "arb1", "arb2")
        assignment = self.assign(case_id)
        self.appeals.recommend_decision(
            request_id="rec1", actor_id=assignment["arbitrator_id"], case_id=case_id,
            lease_token=assignment["lease_token"], recommendation="uphold", rationale="维持",
        )
        with self.assertRaises(ConflictError):
            self.appeals.submit_material(
                request_id="m-late", actor_id="comp1", case_id=case_id, material_id="late",
                material_kind="statement", content_sha256=sha("late"), storage_ref="oss://l",
                summary_text="迟交", content_type="text/plain", byte_length=1,
            )


class DecisionWorkflowTest(AppealFixture):
    def _recommended(self, recommendation="uphold", corrected=None, *, cleared=("arb2",)):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        # arb1 对该赛项有披露，构成回避；默认承办人为 arb2
        self.appeals.register_disclosure(request_id="disc-ev1", actor_id="arb1",
                                         arbitrator_id="arb1", scope_type="event",
                                         scope_value="ev1", note="裁判长")
        self.clear(case_id, *cleared)
        assignment = self.assign(case_id)
        self.assertEqual(assignment["arbitrator_id"], "arb2")
        kwargs = dict(request_id="rec1", actor_id=assignment["arbitrator_id"], case_id=case_id,
                      lease_token=assignment["lease_token"], recommendation=recommendation,
                      rationale="承办意见")
        if recommendation == "correct":
            kwargs["corrected_result"] = corrected or {"score": 93}
        self.appeals.recommend_decision(**kwargs)
        return case_id, assignment["arbitrator_id"]

    def _independent_reviewer(self, case_id, reviewer="arb3"):
        self.clear(case_id, reviewer)
        assigned = self.appeals.assign_reviewer(request_id="rva", actor_id="sec1",
                                                case_id=case_id)
        self.assertEqual(assigned["reviewer_id"], reviewer)
        return reviewer

    def test_correct_requires_corrected_result(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        self.clear(case_id, "arb1")
        assignment = self.assign(case_id)
        with self.assertRaises(ValidationError):
            self.appeals.recommend_decision(
                request_id="rec-bad", actor_id="arb1", case_id=case_id,
                lease_token=assignment["lease_token"], recommendation="correct", rationale="缺结果",
            )

    def test_reviewer_must_be_independent(self):
        case_id, arbitrator = self._recommended()
        # 承办人即便被登记为合格也不能复核本人
        with self.assertRaises(PermissionDenied):
            self.appeals.review_decision(request_id="rev-self", actor_id=arbitrator,
                                         case_id=case_id, approved=True, rationale="自核")

    def test_reviewer_assignment_excludes_arbitrator(self):
        case_id, arbitrator = self._recommended()
        # 除承办人外没有其他合格复核人时不能指定
        with self.assertRaises(EligibilityError):
            self.appeals.assign_reviewer(request_id="rva", actor_id="sec1", case_id=case_id)
        reviewer = self._independent_reviewer(case_id)
        self.assertNotEqual(reviewer, arbitrator)

    def test_approval_atomically_creates_corrected_score_version(self):
        case_id, _ = self._recommended("correct")
        self._independent_reviewer(case_id)
        result = self.appeals.review_decision(request_id="rev1", actor_id="arb3", case_id=case_id,
                                              approved=True, rationale="复核确认")
        self.assertEqual(result["status"], "decided")
        timeline = self.appeals.case_timeline(actor_id="aud1", case_id=case_id)
        chain = {item["version"]: item for item in timeline["score_chain"]}
        self.assertIsNotNone(chain[1]["superseded_by"])
        self.assertEqual(chain[1]["superseded_by"], chain[2]["score_version_id"])
        self.assertIsNone(chain[2]["superseded_by"])
        self.assertEqual(timeline["decision"]["final_decision"], "correct")

    def test_uphold_decided_with_public_token(self):
        case_id, _ = self._recommended("uphold")
        self._independent_reviewer(case_id)
        result = self.appeals.review_decision(request_id="rev1", actor_id="arb3", case_id=case_id,
                                              approved=True, rationale="维持")
        self.assertEqual(result["status"], "decided")
        self.assertIn("public_token", result)

    def test_reject_is_terminal_without_public_token(self):
        case_id, _ = self._recommended("reject")
        self._independent_reviewer(case_id)
        result = self.appeals.review_decision(request_id="rev1", actor_id="arb3", case_id=case_id,
                                              approved=True, rationale="驳回")
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("public_token", result)
        with self.assertRaises(NotFoundError):
            self.appeals.public_result("anything")

    def test_return_sends_back_to_arbitrator_with_new_lease(self):
        case_id, arbitrator = self._recommended("uphold")
        self._independent_reviewer(case_id)
        returned = self.appeals.review_decision(request_id="rev1", actor_id="arb3",
                                                case_id=case_id, approved=False,
                                                rationale="理由不充分")
        self.assertEqual(returned["status"], "assigned")
        self.assertEqual(returned["arbitrator_id"], arbitrator)
        self.assertTrue(returned["new_lease_token"])
        # 原承办人可用新租约重新提交
        self.appeals.recommend_decision(
            request_id="rec2", actor_id=arbitrator, case_id=case_id,
            lease_token=returned["new_lease_token"], recommendation="uphold", rationale="补充后维持",
        )

    def test_terminal_states_are_protected(self):
        case_id, _ = self._recommended("uphold")
        self._independent_reviewer(case_id)
        self.appeals.review_decision(request_id="rev1", actor_id="arb3", case_id=case_id,
                                     approved=True, rationale="维持")
        with self.assertRaises(ProtectedStateError):
            self.appeals.withdraw_appeal(request_id="w1", actor_id="comp1", case_id=case_id,
                                         reason="终局后撤诉")
        with self.assertRaises(ProtectedStateError):
            self.appeals.record_conflict_check(request_id="cc-late", actor_id="sec1",
                                               case_id=case_id, arbitrator_id="arb2")

    def test_withdraw_is_protected_terminal(self):
        published = self.publish()
        case_id = self.file(published["score_version_id"])["case_id"]
        result = self.appeals.withdraw_appeal(request_id="w1", actor_id="comp1", case_id=case_id,
                                              reason="和解")
        self.assertEqual(result["status"], "withdrawn")
        replay = self.appeals.withdraw_appeal(request_id="w1", actor_id="comp1", case_id=case_id,
                                              reason="和解")
        self.assertTrue(replay["replayed"])
        with self.assertRaises(ProtectedStateError):
            self.appeals.withdraw_appeal(request_id="w2", actor_id="comp1", case_id=case_id,
                                         reason="再次撤诉")


class ViewTest(AppealFixture):
    def test_public_result_hides_personal_information(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        self.clear(case_id, "arb1", "arb3")
        assignment = self.assign(case_id)
        self.appeals.recommend_decision(
            request_id="rec1", actor_id=assignment["arbitrator_id"], case_id=case_id,
            lease_token=assignment["lease_token"], recommendation="correct",
            rationale="更正", corrected_result={"score": 93},
        )
        self.appeals.assign_reviewer(request_id="rva", actor_id="sec1", case_id=case_id)
        decided = self.appeals.review_decision(request_id="rev1", actor_id="arb3", case_id=case_id,
                                               approved=True, rationale="确认")
        public = self.appeals.public_result(decided["public_token"])
        serialized = str(public)
        for secret in ("comp1", "arb1", "arb3", "sec1", "o1"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(public["final_decision"], "correct")
        self.assertTrue(public["competitor_ref"].startswith("competitor-"))

    def test_timeline_reconstructs_full_history(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        self.clear(case_id, "arb1", "arb2")
        assignment = self.assign(case_id)
        self.appeals.submit_material(
            request_id="m1", actor_id="comp1", case_id=case_id, material_id="stmt",
            material_kind="statement", content_sha256=sha("s"), storage_ref="oss://s",
            summary_text="陈述", content_type="text/plain", byte_length=10,
        )
        self.appeals.recommend_decision(
            request_id="rec1", actor_id=assignment["arbitrator_id"], case_id=case_id,
            lease_token=assignment["lease_token"], recommendation="uphold", rationale="维持",
        )
        self.clear(case_id, "arb3")
        reviewer = self.appeals.assign_reviewer(request_id="rva", actor_id="sec1",
                                                case_id=case_id)["reviewer_id"]
        self.appeals.review_decision(request_id="rev1", actor_id=reviewer, case_id=case_id,
                                     approved=True, rationale="确认")
        timeline = self.appeals.case_timeline(actor_id="aud1", case_id=case_id)
        kinds = [event["event_type"] for event in timeline["events"]]
        self.assertIn("case.filed", kinds)
        self.assertIn("conflict.checked", kinds)
        self.assertIn("material.sealed", kinds)
        self.assertIn("decision.recommended", kinds)
        self.assertIn("case.decided", kinds)
        self.assertEqual(len(timeline["materials"]), 1)
        self.assertEqual(len(timeline["conflict_checks"]), 3)
        self.assertEqual(timeline["decision"]["reviewer_id"], reviewer)

    def test_competitor_cannot_read_others_materials(self):
        self.service.register_actor(request_id="comp2", actor_id="ad1", new_actor_id="comp2",
                                    display_name="选手二", role="competitor", organization_id="o1")
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        with self.assertRaises(PermissionDenied):
            self.appeals.list_materials(actor_id="comp2", case_id=case_id)

    def test_auditor_cannot_assign(self):
        case_id = self.file(self.publish()["score_version_id"])["case_id"]
        with self.assertRaises(PermissionDenied):
            self.appeals.assign_case(request_id="a", actor_id="aud1", case_id=case_id)


class AuditChainTest(AppealFixture):
    def test_full_flow_keeps_audit_chain_valid(self):
        published = self.publish()
        case_id = self.file(published["score_version_id"])["case_id"]
        self.clear(case_id, "arb1", "arb3")
        assignment = self.assign(case_id)
        self.appeals.submit_material(
            request_id="m1", actor_id="comp1", case_id=case_id, material_id="stmt",
            material_kind="statement", content_sha256=sha("s"), storage_ref="oss://s",
            summary_text="陈述", content_type="text/plain", byte_length=10,
        )
        self.appeals.recommend_decision(
            request_id="rec1", actor_id=assignment["arbitrator_id"], case_id=case_id,
            lease_token=assignment["lease_token"], recommendation="uphold", rationale="维持",
        )
        self.appeals.assign_reviewer(request_id="rva", actor_id="sec1", case_id=case_id)
        self.appeals.review_decision(request_id="rev1", actor_id="arb3", case_id=case_id,
                                     approved=True, rationale="确认")
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 5)


if __name__ == "__main__":
    unittest.main()
