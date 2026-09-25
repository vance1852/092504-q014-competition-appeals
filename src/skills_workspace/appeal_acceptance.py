"""运行竞赛申诉证据封存与裁决服务的离线端到端验收。"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .appeals import AppealService
from .clock import FixedClock
from .service import DomainService
from .storage import Database

START = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run() -> dict[str, object]:
    """执行一条完整的申诉裁决链并核对封存、租约、回避与终态。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "appeals_acceptance.sqlite3")
        clock = FixedClock(START)
        base = DomainService(database, clock)
        appeals = AppealService(database, clock)

        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="org-001", name="竞赛组委会")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-001",
                            display_name="系统管理员", role="admin", organization_id="org-001")
        base.register_actor(request_id="secretary", actor_id="admin-001", new_actor_id="sec-001",
                            display_name="秘书组长", role="operator", organization_id="org-001")
        for rid, aid, name, conflict in [
            ("arb-a", "arb-001", "裁决甲", True),
            ("arb-b", "arb-002", "裁决乙", False),
            ("arb-c", "arb-003", "裁决丙", False),
        ]:
            base.register_actor(request_id=rid, actor_id="admin-001", new_actor_id=aid,
                                display_name=name, role="reviewer", organization_id="org-001")
        base.register_actor(request_id="competitor", actor_id="admin-001", new_actor_id="comp-001",
                            display_name="参赛选手", role="competitor", organization_id="org-001")
        base.register_actor(request_id="auditor", actor_id="admin-001", new_actor_id="aud-001",
                            display_name="审计员", role="auditor", organization_id="org-001")
        base.register_site(request_id="site", actor_id="sec-001", site_id="site-001",
                           organization_id="org-001", name="决赛赛场",
                           timezone_name="Asia/Shanghai")

        published = appeals.publish_score_version(
            request_id="score-v1", actor_id="sec-001", site_id="site-001", event_id="event-weld",
            competitor_id="comp-001", appeal_window_seconds=86400, result={"score": 88.0},
        )
        filed = appeals.file_appeal(request_id="appeal-1", actor_id="comp-001",
                                    score_version_id=published["score_version_id"],
                                    appellant_id="comp-001", grounds="计时设备疑似异常")
        case_id = filed["case_id"]

        # 裁决甲披露与赛项存在关联，回避核对必须判定冲突
        appeals.register_disclosure(request_id="disc-1", actor_id="arb-001",
                                    arbitrator_id="arb-001", scope_type="event",
                                    scope_value="event-weld", note="本人担任裁判长")
        check_a = appeals.record_conflict_check(request_id="check-a", actor_id="sec-001",
                                                case_id=case_id, arbitrator_id="arb-001")
        check_b = appeals.record_conflict_check(request_id="check-b", actor_id="sec-001",
                                                case_id=case_id, arbitrator_id="arb-002")
        assignment = appeals.assign_case(request_id="assign-1", actor_id="sec-001",
                                         case_id=case_id, lease_seconds=1800)

        # 证据两次补充形成不可变版本；系统只留摘要与存储引用
        material_1 = appeals.submit_material(
            request_id="mat-1", actor_id="comp-001", case_id=case_id, material_id="statement",
            material_kind="statement", content_sha256=_sha("陈述一"), storage_ref="oss://ev/stat-1",
            summary_text="关于计时偏差的陈述", content_type="text/plain", byte_length=2048,
        )
        material_2 = appeals.submit_material(
            request_id="mat-2", actor_id="comp-001", case_id=case_id, material_id="statement",
            material_kind="statement", content_sha256=_sha("陈述二"), storage_ref="oss://ev/stat-2",
            summary_text="补充现场情况", content_type="text/plain", byte_length=2304,
        )
        appeals.submit_material(
            request_id="mat-3", actor_id=assignment["arbitrator_id"], case_id=case_id,
            material_id="device-log", material_kind="log_summary", content_sha256=_sha("日志"),
            storage_ref="oss://ev/log-1", summary_text="设备结构化日志摘要",
            content_type="application/json", byte_length=4096,
            lease_token=assignment["lease_token"],
        )

        # 租约超时后被回收，旧代号失效；重新分派给同一合格承办人
        clock._value = START + timedelta(minutes=31)
        reclaimed = appeals.reclaim_expired_leases(actor_id="sec-001")
        assignment_2 = appeals.assign_case(request_id="assign-2", actor_id="sec-001",
                                           case_id=case_id, lease_seconds=3600)

        appeals.recommend_decision(
            request_id="recommend-1", actor_id=assignment_2["arbitrator_id"], case_id=case_id,
            lease_token=assignment_2["lease_token"], recommendation="correct",
            rationale="日志显示计时少计 3 秒", corrected_result={"score": 91.0},
        )
        check_c = appeals.record_conflict_check(request_id="check-c", actor_id="sec-001",
                                                case_id=case_id, arbitrator_id="arb-003")
        reviewer = appeals.assign_reviewer(request_id="reviewer-1", actor_id="sec-001",
                                           case_id=case_id)
        decided = appeals.review_decision(request_id="review-1", actor_id=reviewer["reviewer_id"],
                                          case_id=case_id, approved=True,
                                          rationale="独立复核确认计时偏差")

        public = appeals.public_result(decided["public_token"])
        timeline = appeals.case_timeline(actor_id="aud-001", case_id=case_id)
        audit_valid, audit_events = base.verify_audit()

        result = {
            "status": "ok",
            "conflict_detected": check_a["conflicted"],
            "cleared_assignee": check_b["conflicted"] is False
            and assignment["arbitrator_id"] == "arb-002",
            "material_versions": [material_1["version"], material_2["version"]],
            "lease_reclaimed": reclaimed["count"] == 1,
            "final_status": decided["status"],
            "independent_reviewer": reviewer["reviewer_id"] == "arb-003",
            "score_corrected": timeline["score_chain"][-1]["version"] == 2
            and timeline["score_chain"][0]["superseded_by"]
            == timeline["score_chain"][-1]["score_version_id"],
            "public_anonymized": "comp-001" not in json.dumps(public, ensure_ascii=False),
            "timeline_events": len(timeline["events"]),
            "audit_valid": audit_valid,
            "audit_events": audit_events,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = {
        "status": "ok", "conflict_detected": True, "cleared_assignee": True,
        "material_versions": [1, 2], "lease_reclaimed": True, "final_status": "decided",
        "independent_reviewer": True, "score_corrected": True, "public_anonymized": True,
        "audit_valid": True,
    }
    ok = all(result[key] == value for key, value in expected.items())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
