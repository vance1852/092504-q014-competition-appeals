"""运行基础服务与申诉裁决服务的离线端到端验收。"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .appeal_service import AppealService
from .clock import FixedClock
from .service import DomainService
from .storage import Database


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run() -> dict[str, object]:
    """执行登记链与一条完整申诉裁决链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        appeals = AppealService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范训练机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="训练负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="institution_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="institution_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # ---- 申诉裁决完整链路 ----
        for rid, name in [("rv-001", "承办人"), ("rv-002", "复核人")]:
            service.register_actor(request_id="req-actor-" + rid, actor_id="admin-001",
                                   new_actor_id=rid, display_name=name, role="reviewer",
                                   organization_id="org-001")
        appeals.register_competition(request_id="req-comp", actor_id="admin-001",
                                     competition_id="comp-001", name="技能竞赛")
        appeals.register_competitor(request_id="req-player", actor_id="operator-001",
                                    competition_id="comp-001", competitor_id="player-001",
                                    person_name="某选手")
        appeals.publish_score_version(request_id="req-version", actor_id="operator-001",
                                      competition_id="comp-001", version_id="ver-001",
                                      version_label="初赛成绩",
                                      entries={"player-001": "store://score/player-001/v1"})
        appeals.register_adjudicator(request_id="req-adj-1", actor_id="admin-001",
                                     competition_id="comp-001", adjudicator_id="rv-001",
                                     display_name="承办人")
        appeals.register_adjudicator(request_id="req-adj-2", actor_id="admin-001",
                                     competition_id="comp-001", adjudicator_id="rv-002",
                                     display_name="复核人")
        filed = appeals.file_appeal(
            request_id="req-appeal", actor_id="operator-001", version_id="ver-001",
            competitor_id="player-001", grounds="计时设备异常导致成绩偏高",
            evidence=[{"kind": "statement", "content_digest": _sha("陈述书"),
                       "storage_ref": "store://evidence/1"}])
        case_id = filed.response["case_id"]
        # 补交材料形成不可变新版本，不覆盖首版。
        appeals.add_evidence(request_id="req-evidence-2", actor_id="operator-001", case_id=case_id,
                             kind="structured_log_summary", content_digest=_sha("结构化日志摘要"),
                             storage_ref="store://evidence/2")
        # 回避核对：承办人通过，完成核对。
        appeals.screen_adjudicator(request_id="req-screen-1", actor_id="operator-001",
                                   case_id=case_id, adjudicator_id="rv-001", result="clear")
        appeals.screen_adjudicator(request_id="req-screen-2", actor_id="operator-001",
                                   case_id=case_id, adjudicator_id="rv-002", result="clear")
        appeals.complete_screening(request_id="req-screen-done", actor_id="operator-001",
                                   case_id=case_id)
        appeals.assign_handler(request_id="req-assign", actor_id="operator-001", case_id=case_id)
        appeals.request_evidence(request_id="req-more", actor_id="rv-001", case_id=case_id,
                                note="请补充设备异常时段日志")
        appeals.add_evidence(request_id="req-evidence-3", actor_id="operator-001", case_id=case_id,
                             kind="supporting_document", content_digest=_sha("证明文件"),
                             storage_ref="store://evidence/3")
        appeals.submit_recommendation(request_id="req-recommend", actor_id="rv-001",
                                      case_id=case_id, recommendation="correct",
                                      basis_digest=_sha("承办依据"), basis_ref="store://basis/handler",
                                      proposed_score_ref="store://score/player-001/corrected")
        # 独立复核人作出终局裁决，成绩引用在同一事务内原子更新。
        appeals.decide_appeal(request_id="req-decide", actor_id="rv-002", case_id=case_id,
                              outcome="correct", basis_digest=_sha("复核决定依据"),
                              basis_ref="store://basis/reviewer",
                              corrected_score_ref="store://score/player-001/corrected")
        case = appeals.get_case(case_id)
        updates = appeals.list_score_updates(case_id)
        public = appeals.public_result(case.case_number)
        timeline = appeals.case_timeline(actor_id="admin-001", case_id=case_id)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "case_status": case.status, "case_outcome": case.final_outcome,
                  "evidence_versions": len(appeals.list_evidence(case_id)),
                  "score_corrected": updates[0].new_ref if updates else None,
                  "public_outcome": public.outcome, "public_has_identity": "competitor_id" in public.__dict__,
                  "timeline_events": len(timeline["events"])}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["case_status"] == "decided"
          and result["score_corrected"] == "store://score/player-001/corrected"
          and not result["public_has_identity"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
