"""实现竞赛申诉证据封存与裁决工作流。"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    EligibilityError,
    LeaseError,
    NotFoundError,
    PermissionDenied,
    ProtectedStateError,
    ValidationError,
)
from .models import Actor, ScoreVersion
from .requests import IDENTIFIER, idempotent_write
from .storage import Database

# 案件状态机
STATUS_FILED = "filed"                            # 已受理、等待回避核对与分派
STATUS_ASSIGNED = "assigned"                      # 承办人持租约办理
STATUS_AWAITING = "awaiting_supplement"           # 已要求补证
STATUS_UNDER_REVIEW = "under_review"              # 承办建议待独立复核
STATUS_WITHDRAWN = "withdrawn"                    # 撤诉（受保护终态）
STATUS_REJECTED = "rejected"                      # 驳回（受保护终态）
STATUS_DECIDED = "decided"                        # 裁决：维持或更正（受保护终态）

TERMINAL_STATUSES = frozenset({STATUS_WITHDRAWN, STATUS_REJECTED, STATUS_DECIDED})
MATERIAL_KINDS = frozenset({"statement", "log_summary", "evidence_document"})
RECOMMENDATIONS = frozenset({"uphold", "correct", "reject"})
DISCLOSURE_SCOPES = frozenset({"competitor", "event", "organization"})

DEFAULT_LEASE_SECONDS = 1800
DEFAULT_CORRECTION_WINDOW_SECONDS = 3 * 24 * 3600
ARBITRATOR_ROLE = "reviewer"


class AppealService:
    """编排成绩版本、证据封存、回避核对、租约分派与独立复核。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _after(self, seconds: int) -> str:
        return (self.clock.now() + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 2000) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require_roles(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _case_row(self, connection, case_id: str):
        row = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError("申诉案件不存在")
        return row

    def _guard_terminal(self, row) -> None:
        if row["status"] in TERMINAL_STATUSES:
            raise ProtectedStateError(f"案件已处于受保护终态：{row['status']}")

    def _timeline(self, connection, *, case_id: str, event_type: str, actor_id: str,
                  detail: dict[str, Any], occurred_at: str) -> None:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM case_events WHERE case_id=?", (case_id,)
        ).fetchone()["next"]
        connection.execute(
            "INSERT INTO case_events(case_id,seq,event_type,actor_id,detail_json,occurred_at) "
            "VALUES(?,?,?,?,?,?)",
            (case_id, seq, event_type, actor_id, canonical_json(detail), occurred_at),
        )

    def _audit(self, connection, *, actor_id: str, action: str, case_id: str,
               detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type="appeal_case",
                     resource_id=case_id, detail=detail, occurred_at=self._now())

    def _case_dict(self, row) -> dict[str, Any]:        return {
            "case_id": row["case_id"],
            "site_id": row["site_id"],
            "score_version_id": row["score_version_id"],
            "event_id": row["event_id"],
            "competitor_id": row["competitor_id"],
            "appellant_id": row["appellant_id"],
            "grounds": row["grounds"],
            "status": row["status"],
            "assigned_arbitrator_id": row["assigned_arbitrator_id"],
            "lease_version": row["lease_version"],
            "lease_expires_at": row["lease_expires_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------ 成绩版本

    def publish_score_version(self, *, request_id: str, actor_id: str, site_id: str,
                              event_id: str, competitor_id: str, appeal_window_seconds: int,
                              result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict) or not result:
            raise ValidationError("result 必须是非空对象")
        if not isinstance(appeal_window_seconds, int) or appeal_window_seconds <= 0:
            raise ValidationError("appeal_window_seconds 必须是正整数")
        payload = {"actor_id": actor_id, "site_id": site_id, "event_id": event_id,
                   "competitor_id": competitor_id, "appeal_window_seconds": appeal_window_seconds,
                   "result": result}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            site_id = self._identifier(site_id, "site_id")
            event_id = self._identifier(event_id, "event_id")
            competitor_id = self._identifier(competitor_id, "competitor_id")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的场地上公布成绩")

            def create() -> tuple[str, str, dict[str, Any]]:
                version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM score_versions "
                    "WHERE site_id=? AND event_id=? AND competitor_id=?",
                    (site_id, event_id, competitor_id),
                ).fetchone()["next"]
                score_version_id = uuid.uuid4().hex
                now = self._now()
                deadline = self._after(appeal_window_seconds)
                result_hash = digest(result)
                connection.execute(
                    "INSERT INTO score_versions(score_version_id,site_id,event_id,competitor_id,version,"
                    "published_at,appeal_deadline,payload_json,payload_hash,superseded_by,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (score_version_id, site_id, event_id, competitor_id, version, now, deadline,
                     canonical_json(result), result_hash, None, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="score_version.published",
                             resource_type="score_version", resource_id=score_version_id,
                             detail={"site_id": site_id, "event_id": event_id,
                                     "competitor_id": competitor_id, "version": version,
                                     "appeal_deadline": deadline, "payload_hash": result_hash},
                             occurred_at=now)
                response = {"score_version_id": score_version_id, "version": version,
                            "appeal_deadline": deadline}
                return "score_version", score_version_id, response

            receipt = idempotent_write(connection, now=self._now(), request_id=request_id,
                                       action="publish_score_version", payload=payload, create=create)
            row = connection.execute(
                "SELECT * FROM score_versions WHERE score_version_id=?", (receipt.resource_id,)
            ).fetchone()
            return {"receipt": receipt.__dict__,
                    "score_version_id": receipt.resource_id, "version": row["version"],
                    "replayed": receipt.replayed, "appeal_deadline": row["appeal_deadline"]}

    def get_score_version(self, score_version_id: str) -> ScoreVersion:
        row = self.database.connection.execute(
            "SELECT * FROM score_versions WHERE score_version_id=?", (score_version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("成绩版本不存在")
        return ScoreVersion(row["score_version_id"], row["site_id"], row["event_id"],
                            row["competitor_id"], row["version"], row["published_at"],
                            row["appeal_deadline"], row["payload_hash"])

    # ------------------------------------------------------------------ 利益冲突

    def _disclosure_conflicts(self, connection, arbitrator: Actor,
                              event_id: str, competitor_id: str,
                              site_org: str) -> list[str]:
        reasons: list[str] = []
        if arbitrator.actor_id == competitor_id:
            reasons.append("本人为申诉当事人")
        rows = connection.execute(
            "SELECT scope_type, scope_value FROM arbitrator_disclosures WHERE arbitrator_id=?",
            (arbitrator.actor_id,),
        ).fetchall()
        for item in rows:
            if item["scope_type"] == "competitor" and item["scope_value"] == competitor_id:
                reasons.append("披露与选手存在关联")
            if item["scope_type"] == "event" and item["scope_value"] == event_id:
                reasons.append("披露与赛项存在关联")
            if item["scope_type"] == "organization" and item["scope_value"] == site_org:
                reasons.append("披露与主办组织存在关联")
        return reasons

    def register_disclosure(self, *, request_id: str, actor_id: str, arbitrator_id: str,
                            scope_type: str, scope_value: str, note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "arbitrator_id": arbitrator_id,
                   "scope_type": scope_type, "scope_value": scope_value, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", ARBITRATOR_ROLE)
            if actor.role == ARBITRATOR_ROLE and actor.actor_id != arbitrator_id:
                raise PermissionDenied("裁决人员只能登记本人的回避披露")
            arbitrator = self._actor(connection, arbitrator_id)
            if arbitrator.role != ARBITRATOR_ROLE:
                raise ValidationError("回避对象必须是裁决人员")
            if scope_type not in DISCLOSURE_SCOPES:
                raise ValidationError("scope_type 不在允许范围内")
            scope_value = self._identifier(scope_value, "scope_value")
            note = str(note or "")[:500]

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT OR IGNORE INTO arbitrator_disclosures"
                    "(arbitrator_id,scope_type,scope_value,note,created_at) VALUES(?,?,?,?,?)",
                    (arbitrator_id, scope_type, scope_value, note, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="disclosure.registered",
                            case_id=arbitrator_id,
                            detail={"arbitrator_id": arbitrator_id, "scope_type": scope_type,
                                    "scope_value": scope_value})
                response = {"arbitrator_id": arbitrator_id, "scope_type": scope_type,
                            "scope_value": scope_value}
                return "arbitrator_disclosure", f"{arbitrator_id}:{scope_type}:{scope_value}", response

            receipt = idempotent_write(connection, now=self._now(), request_id=request_id,
                                       action="register_disclosure", payload=payload, create=create)
            return {"receipt": receipt.__dict__, "replayed": receipt.replayed}

    def record_conflict_check(self, *, request_id: str, actor_id: str, case_id: str,
                              arbitrator_id: str, declared_conflicted: bool = False,
                              note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "case_id": case_id, "arbitrator_id": arbitrator_id,
                   "declared_conflicted": bool(declared_conflicted), "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            arbitrator = self._actor(connection, arbitrator_id)
            if arbitrator.role != ARBITRATOR_ROLE:
                raise ValidationError("核对对象必须是裁决人员")
            declared = bool(declared_conflicted)
            note_text = str(note or "")[:500]

            def create() -> tuple[str, str, dict[str, Any]]:
                case_row = self._case_row(connection, case_id)
                self._guard_terminal(case_row)
                site = connection.execute(
                    "SELECT organization_id FROM sites WHERE site_id=?", (case_row["site_id"],)
                ).fetchone()
                objective = self._disclosure_conflicts(
                    connection, arbitrator, case_row["event_id"], case_row["competitor_id"],
                    site["organization_id"],
                )
                conflicted = declared or bool(objective)
                basis = {"declared_conflicted": declared,
                         "objective_reasons": objective,
                         "effective_conflicted": conflicted, "note": note_text}
                connection.execute(
                    "INSERT INTO conflict_checks(case_id,arbitrator_id,conflicted,basis_json,"
                    "checked_by,checked_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(case_id,arbitrator_id) DO UPDATE SET "
                    "conflicted=excluded.conflicted,basis_json=excluded.basis_json,"
                    "checked_by=excluded.checked_by,checked_at=excluded.checked_at",
                    (case_id, arbitrator_id, 1 if conflicted else 0, canonical_json(basis),
                     actor_id, self._now()),
                )
                self._timeline(connection, case_id=case_id, event_type="conflict.checked",
                               actor_id=actor_id, detail={"arbitrator_id": arbitrator_id, **basis},
                               occurred_at=self._now())
                self._audit(connection, actor_id=actor_id, action="conflict.checked",
                            case_id=case_id, detail={"arbitrator_id": arbitrator_id, **basis})
                response = {"case_id": case_id, "arbitrator_id": arbitrator_id,
                            "conflicted": conflicted}
                return "conflict_check", f"{case_id}:{arbitrator_id}", response

            receipt = idempotent_write(connection, now=self._now(), request_id=request_id,
                                       action="record_conflict_check", payload=payload, create=create)
            stored = connection.execute(
                "SELECT conflicted, basis_json FROM conflict_checks WHERE case_id=? AND arbitrator_id=?",
                (case_id, arbitrator_id),
            ).fetchone()
            basis = json.loads(stored["basis_json"])
            return {"receipt": receipt.__dict__, "replayed": receipt.replayed,
                    "case_id": case_id, "arbitrator_id": arbitrator_id,
                    "conflicted": bool(stored["conflicted"]),
                    "objective_reasons": basis["objective_reasons"]}

    # ------------------------------------------------------------------ 受理立案

    def file_appeal(self, *, request_id: str, actor_id: str, score_version_id: str,
                    appellant_id: str, grounds: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "score_version_id": score_version_id,
                   "appellant_id": appellant_id, "grounds": grounds}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "competitor")
            score = connection.execute(
                "SELECT * FROM score_versions WHERE score_version_id=?", (score_version_id,)
            ).fetchone()
            if score is None:
                raise NotFoundError("成绩版本不存在")
            appellant = self._actor(connection, appellant_id)
            if actor.role == "competitor" and (actor.actor_id != appellant_id
                                               or appellant_id != score["competitor_id"]):
                raise PermissionDenied("选手只能本人就本人成绩提出申诉")
            if appellant_id != score["competitor_id"] and actor.role not in {"admin", "operator"}:
                raise PermissionDenied("仅秘书组可代选手登记申诉")
            grounds = self._text(grounds, "grounds", 2000)
            score_id = score["score_version_id"]
            score_version = score["version"]
            score_site = score["site_id"]
            score_event = score["event_id"]
            score_competitor = score["competitor_id"]

            def create() -> tuple[str, str, dict[str, Any]]:
                # 资格校验放在 create 内：请求重放时应返回原回执而非重复校验
                current = connection.execute(
                    "SELECT * FROM score_versions WHERE score_version_id=?", (score_id,)
                ).fetchone()
                if self._now() > current["appeal_deadline"]:
                    raise EligibilityError("已超过该成绩版本的申诉期限")
                if current["superseded_by"]:
                    raise EligibilityError("该成绩版本已被新版本替代，应针对新版本申诉")
                duplicate = connection.execute(
                    "SELECT case_id, status FROM cases WHERE score_version_id=? AND competitor_id=?",
                    (score_id, score_competitor),
                ).fetchone()
                if duplicate is not None:
                    if duplicate["status"] in TERMINAL_STATUSES:
                        raise EligibilityError("该成绩版本的申诉已有终态结论，不能重复申请")
                    raise EligibilityError("该成绩版本已存在进行中的申诉案件")
                case_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO cases(case_id,site_id,score_version_id,event_id,competitor_id,"
                    "appellant_id,grounds,status,lease_version,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,0,?,?,?)",
                    (case_id, score_site, score_id, score_event, score_competitor,
                     appellant_id, grounds, STATUS_FILED, actor_id, now, now),
                )
                self._timeline(connection, case_id=case_id, event_type="case.filed",
                               actor_id=actor_id,
                               detail={"score_version_id": score_id,
                                       "version": score_version,
                                       "appellant_id": appellant_id,
                                       "appeal_deadline": current["appeal_deadline"]},
                               occurred_at=now)
                append_event(connection, actor_id=actor_id, action="appeal.filed",
                             resource_type="appeal_case", resource_id=case_id,
                             detail={"site_id": score_site, "event_id": score_event,
                                     "competitor_id": score_competitor,
                                     "score_version_id": score_id},
                             occurred_at=now)
                response = {"case_id": case_id, "status": STATUS_FILED}
                return "appeal_case", case_id, response

            receipt = idempotent_write(connection, now=self._now(), request_id=request_id,
                                       action="file_appeal", payload=payload, create=create)
            return {"receipt": receipt.__dict__, "case_id": receipt.resource_id,
                    "status": STATUS_FILED, "replayed": receipt.replayed}

    # ------------------------------------------------------------------ 分派租约

    def _cleared_candidates(self, connection, case_row) -> list[Actor]:
        rows = connection.execute(
            "SELECT a.* FROM conflict_checks c JOIN actors a ON a.actor_id = c.arbitrator_id "
            "WHERE c.case_id=? AND c.conflicted=0 AND a.active=1 AND a.role=?",
            (case_row["case_id"], ARBITRATOR_ROLE),
        ).fetchall()
        return [Actor(r["actor_id"], r["display_name"], r["role"], r["organization_id"], True)
                for r in rows]

    def assign_case(self, *, request_id: str, actor_id: str, case_id: str,
                    lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any]:
        if not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValidationError("lease_seconds 必须是正整数")
        payload = {"actor_id": actor_id, "case_id": case_id, "lease_seconds": lease_seconds}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")

            def create() -> tuple[str, str, dict[str, Any]]:
                case_row = self._case_row(connection, case_id)
                self._guard_terminal(case_row)
                if case_row["status"] != STATUS_FILED:
                    raise ConflictError("案件已有承办人或已进入办理阶段")
                candidates = self._cleared_candidates(connection, case_row)
                if not candidates:
                    raise EligibilityError("尚无通过回避核对的合格承办人，不能分派")
                now = self._now()
                workload = dict(connection.execute(
                    "SELECT assigned_arbitrator_id, COUNT(*) AS count FROM cases "
                    "WHERE status IN (?, ?) AND lease_expires_at > ? GROUP BY assigned_arbitrator_id",
                    (STATUS_ASSIGNED, STATUS_AWAITING, now),
                ).fetchall())
                chosen = sorted(candidates,
                                key=lambda c: (workload.get(c.actor_id, 0), c.actor_id))[0]
                lease_token = uuid.uuid4().hex
                expires_at = (self.clock.now() + timedelta(seconds=lease_seconds)
                              ).isoformat().replace("+00:00", "Z")
                connection.execute(
                    "UPDATE cases SET status=?, assigned_arbitrator_id=?, lease_version=lease_version+1,"
                    "lease_token=?, lease_expires_at=?, updated_at=? WHERE case_id=?",
                    (STATUS_ASSIGNED, chosen.actor_id, lease_token, expires_at, now, case_id),
                )
                self._timeline(connection, case_id=case_id, event_type="case.assigned",
                               actor_id=actor_id,
                               detail={"arbitrator_id": chosen.actor_id,
                                       "lease_expires_at": expires_at},
                               occurred_at=now)
                self._audit(connection, actor_id=actor_id, action="case.assigned", case_id=case_id,
                            detail={"arbitrator_id": chosen.actor_id, "lease_expires_at": expires_at})
                response = {"case_id": case_id, "arbitrator_id": chosen.actor_id,
                            "lease_token": lease_token, "lease_expires_at": expires_at}
                return "case_assignment", case_id, response

            receipt = idempotent_write(connection, now=self._now(), request_id=request_id,
                                       action="assign_case", payload=payload, create=create)
            stored = self._case_row(connection, case_id)
            return {"case_id": case_id, "arbitrator_id": stored["assigned_arbitrator_id"],
                    "lease_token": stored["lease_token"],
                    "lease_expires_at": stored["lease_expires_at"],
                    "replayed": receipt.replayed}

    def _hold_lease(self, case_row, arbitrator_id: str, lease_token: str) -> None:
        """校验当前持有者与未超时租约；旧代号与超时代号一律拒绝。"""

        self._guard_terminal(case_row)
        if case_row["assigned_arbitrator_id"] != arbitrator_id:
            raise PermissionDenied("不是本案当前承办人")
        if not lease_token or case_row["lease_token"] != lease_token:
            raise LeaseError("租约代号无效，可能已被回收或轮换")
        if self._now() > (case_row["lease_expires_at"] or ""):
            raise LeaseError("承办租约已超时，不能继续提交")

    def renew_lease(self, *, actor_id: str, case_id: str, lease_token: str,
                    lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any]:
        if not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValidationError("lease_seconds 必须是正整数")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, ARBITRATOR_ROLE)
            case_row = self._case_row(connection, case_id)
            self._hold_lease(case_row, actor_id, lease_token)
            expires_at = self._after(lease_seconds)
            new_token = uuid.uuid4().hex
            connection.execute(
                "UPDATE cases SET lease_token=?, lease_expires_at=?, updated_at=? WHERE case_id=?",
                (new_token, expires_at, self._now(), case_id),
            )
            self._timeline(connection, case_id=case_id, event_type="lease.renewed",
                           actor_id=actor_id, detail={"lease_expires_at": expires_at},
                           occurred_at=self._now())
            return {"case_id": case_id, "lease_token": new_token,
                    "lease_expires_at": expires_at}

    def reclaim_expired_leases(self, *, actor_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            now = self._now()
            rows = connection.execute(
                "SELECT case_id, assigned_arbitrator_id, lease_version FROM cases "
                "WHERE status IN (?, ?) AND lease_expires_at <= ?",
                (STATUS_ASSIGNED, STATUS_AWAITING, now),
            ).fetchall()
            reclaimed: list[str] = []
            for row in rows:
                connection.execute(
                    "UPDATE cases SET status=?, assigned_arbitrator_id=NULL, lease_version=lease_version+1,"
                    "lease_token=NULL, lease_expires_at=NULL, updated_at=? WHERE case_id=?",
                    (STATUS_FILED, now, row["case_id"]),
                )
                self._timeline(connection, case_id=row["case_id"], event_type="lease.reclaimed",
                               actor_id=actor_id,
                               detail={"previous_arbitrator_id": row["assigned_arbitrator_id"],
                                       "previous_lease_version": row["lease_version"]},
                               occurred_at=now)
                self._audit(connection, actor_id=actor_id, action="lease.reclaimed",
                            case_id=row["case_id"],
                            detail={"previous_arbitrator_id": row["assigned_arbitrator_id"]})
                reclaimed.append(row["case_id"])
            return {"reclaimed": reclaimed, "count": len(reclaimed)}

    # ------------------------------------------------------------------ 证据封存

    def submit_material(self, *, request_id: str, actor_id: str, case_id: str, material_id: str,
                        material_kind: str, content_sha256: str, storage_ref: str,
                        summary_text: str, content_type: str, byte_length: int,
                        lease_token: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "case_id": case_id, "material_id": material_id,
                   "material_kind": material_kind, "content_sha256": content_sha256,
                   "storage_ref": storage_ref, "summary_text": summary_text,
                   "content_type": content_type, "byte_length": byte_length,
                   "lease_token": lease_token}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "competitor", ARBITRATOR_ROLE)
            material_id = self._identifier(material_id, "material_id")
            if material_kind not in MATERIAL_KINDS:
                raise ValidationError("material_kind 不在允许范围内")
            content_sha256 = str(content_sha256 or "").strip().lower()
            if len(content_sha256) != 64 or any(c not in "0123456789abcdef" for c in content_sha256):
                raise ValidationError("content_sha256 必须是 64 位十六进制摘要")
            storage_ref = self._text(storage_ref, "storage_ref", 500)
            summary_text = self._text(summary_text, "summary_text", 4000)
            content_type = self._text(content_type, "content_type", 120)
            if not isinstance(byte_length, int) or byte_length < 0:
                raise ValidationError("byte_length 必须是非负整数；系统不保存原文")
            token = lease_token or None

            def create() -> tuple[str, str, dict[str, Any]]:
                case_row = self._case_row(connection, case_id)
                self._guard_terminal(case_row)
                if case_row["status"] == STATUS_UNDER_REVIEW:
                    raise ConflictError("案件已进入复核，证据卷宗已封存，不能再补交材料")
                if actor.role == "competitor" and (actor.actor_id != case_row["appellant_id"]
                                                   or case_row["competitor_id"] != actor.actor_id):
                    raise PermissionDenied("只能为本人案件补交材料")
                if actor.role == ARBITRATOR_ROLE:
                    # 承办人必须持有有效租约，旧代号或超时代号都不能封存材料
                    if case_row["status"] not in {STATUS_ASSIGNED, STATUS_AWAITING}:
                        raise ConflictError("案件当前不处于承办阶段")
                    self._hold_lease(case_row, actor_id, token or "")
                prior = connection.execute(
                    "SELECT material_kind FROM material_versions WHERE case_id=? AND material_id=? "
                    "ORDER BY version LIMIT 1", (case_id, material_id),
                ).fetchone()
                if prior is not None and prior["material_kind"] != material_kind:
                    raise ConflictError("同一材料编号的补充版本不能更改材料类别")
                version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM material_versions "
                    "WHERE case_id=? AND material_id=?", (case_id, material_id),
                ).fetchone()["next"]
                now = self._now()
                connection.execute(
                    "INSERT INTO material_versions(material_id,case_id,material_kind,version,"
                    "content_sha256,storage_ref,summary_text,content_type,byte_length,"
                    "submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (material_id, case_id, material_kind, version, content_sha256,
                     storage_ref, summary_text, content_type, byte_length, actor_id, now),
                )
                self._timeline(connection, case_id=case_id, event_type="material.sealed",
                               actor_id=actor_id,
                               detail={"material_id": material_id, "version": version,
                                       "material_kind": material_kind,
                                       "content_sha256": content_sha256,
                                       "storage_ref": storage_ref, "byte_length": byte_length},
                               occurred_at=now)
                self._audit(connection, actor_id=actor_id, action="material.sealed",
                            case_id=case_id,
                            detail={"material_id": material_id, "version": version,
                                    "material_kind": material_kind,
                                    "content_sha256": content_sha256})
                response = {"case_id": case_id, "material_id": material_id, "version": version,
                            "content_sha256": content_sha256}
                return "material_version", f"{case_id}:{material_id}:{version}", response

            receipt = idempotent_write(connection, now=self._now(), request_id=request_id,
                                       action="submit_material", payload=payload, create=create)
            return {"receipt": receipt.__dict__, "replayed": receipt.replayed,
                    "case_id": case_id, "material_id": material_id,
                    "version": int(receipt.resource_id.rsplit(":", 1)[1]),
                    "content_sha256": content_sha256, "storage_ref": storage_ref}

    def list_materials(self, *, actor_id: str, case_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        case_row = self._case_row(connection, case_id)
        if actor.role not in {"admin", "operator", "auditor"} and actor.actor_id not in (
            case_row["appellant_id"], case_row["assigned_arbitrator_id"],
        ):
            decision = connection.execute(
                "SELECT reviewer_id FROM decisions WHERE case_id=?", (case_id,)
            ).fetchone()
            if decision is None or decision["reviewer_id"] != actor.actor_id:
                raise PermissionDenied("无权查看本案材料")
        rows = connection.execute(
            "SELECT * FROM material_versions WHERE case_id=? "
            "ORDER BY submitted_at, material_id, version", (case_id,)
        ).fetchall()
        return [{"material_id": r["material_id"], "case_id": r["case_id"],
                 "material_kind": r["material_kind"], "version": r["version"],
                 "content_sha256": r["content_sha256"], "storage_ref": r["storage_ref"],
                 "summary_text": r["summary_text"], "content_type": r["content_type"],
                 "byte_length": r["byte_length"], "submitted_by": r["submitted_by"],
                 "submitted_at": r["submitted_at"]} for r in rows]

    # ------------------------------------------------------------------ 承办动作

    def request_supplement(self, *, request_id: str, actor_id: str, case_id: str,
                           lease_token: str, note: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "case_id": case_id, "lease_token": lease_token, "note": note}
        note = self._text(note, "note", 2000)
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, ARBITRATOR_ROLE)

            def create() -> tuple[str, str, dict[str, Any]]:
                case_row = self._case_row(connection, case_id)
                self._hold_lease(case_row, actor_id, lease_token)
                if case_row["status"] not in {STATUS_ASSIGNED, STATUS_AWAITING}:
                    raise ConflictError("当前状态不能要求补证")
                now = self._now()
                connection.execute(
                    "UPDATE cases SET status=?, updated_at=? WHERE case_id=?",
                    (STATUS_AWAITING, now, case_id),
                )
                self._timeline(connection, case_id=case_id, event_type="supplement.requested",
                               actor_id=actor_id, detail={"note": note}, occurred_at=now)
                self._audit(connection, actor_id=actor_id, action="supplement.requested",
                            case_id=case_id, detail={"note": note})
                return "supplement_request", case_id, {"case_id": case_id, "status": STATUS_AWAITING}

            receipt = idempotent_write(connection, now=self._now(), request_id=request_id,
                                       action="request_supplement", payload=payload, create=create)
            return {"case_id": case_id, "status": STATUS_AWAITING, "replayed": receipt.replayed}

    def recommend_decision(self, *, request_id: str, actor_id: str, case_id: str,
                           lease_token: str, recommendation: str, rationale: str,
                           corrected_result: dict[str, Any] | None = None) -> dict[str, Any]:
        """承办人建议维持、更正或驳回；提交后进入独立复核。"""

        payload = {"actor_id": actor_id, "case_id": case_id, "lease_token": lease_token,
                   "recommendation": recommendation, "rationale": rationale,
                   "corrected_result": corrected_result}
        rationale = self._text(rationale, "rationale", 4000)
        if recommendation not in RECOMMENDATIONS:
            raise ValidationError("recommendation 必须是 uphold、correct 或 reject")
        if recommendation == "correct":
            if not isinstance(corrected_result, dict) or not corrected_result:
                raise ValidationError("建议更正时必须提供 corrected_result")
        else:
            corrected_result = None
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, ARBITRATOR_ROLE)

            def create() -> tuple[str, str, dict[str, Any]]:
                case_row = self._case_row(connection, case_id)
                self._hold_lease(case_row, actor_id, lease_token)
                if case_row["status"] not in {STATUS_ASSIGNED, STATUS_AWAITING}:
                    raise ConflictError("当前状态不能提交承办建议")
                now = self._now()
                corrected_json = (canonical_json(corrected_result)
                                  if corrected_result is not None else None)
                existing = connection.execute(
                    "SELECT case_id FROM decisions WHERE case_id=?", (case_id,)
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO decisions(case_id,recommendation,rationale,arbitrator_id,"
                        "recommended_at,review_status,correction_payload_json) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (case_id, recommendation, rationale, actor_id, now, "pending",
                         corrected_json),
                    )
                else:
                    connection.execute(
                        "UPDATE decisions SET recommendation=?, rationale=?, arbitrator_id=?,"
                        "recommended_at=?, review_status='pending', reviewer_id=NULL,"
                        "review_rationale=NULL, reviewed_at=NULL, final_decision=NULL,"
                        "score_version_id=NULL, correction_ref=NULL, correction_payload_json=? "
                        "WHERE case_id=?",
                        (recommendation, rationale, actor_id, now, corrected_json, case_id),
                    )
                connection.execute(
                    "UPDATE cases SET status=?, lease_token=NULL, lease_expires_at=NULL, updated_at=? "
                    "WHERE case_id=?",
                    (STATUS_UNDER_REVIEW, now, case_id),
                )
                self._timeline(connection, case_id=case_id, event_type="decision.recommended",
                               actor_id=actor_id,
                               detail={"recommendation": recommendation, "rationale": rationale},
                               occurred_at=now)
                self._audit(connection, actor_id=actor_id, action="decision.recommended",
                            case_id=case_id, detail={"recommendation": recommendation})
                return ("decision_recommendation", case_id,
                        {"case_id": case_id, "status": STATUS_UNDER_REVIEW})

            receipt = idempotent_write(connection, now=self._now(), request_id=request_id,
                                       action="recommend_decision", payload=payload, create=create)
            return {"case_id": case_id, "status": STATUS_UNDER_REVIEW, "replayed": receipt.replayed}

    # ------------------------------------------------------------------ 独立复核

    def assign_reviewer(self, *, request_id: str, actor_id: str, case_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "case_id": case_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")

            def create() -> tuple[str, str, dict[str, Any]]:
                case_row = self._case_row(connection, case_id)
                if case_row["status"] != STATUS_UNDER_REVIEW:
                    raise ConflictError("只有待复核案件才能指定复核人")
                decision = connection.execute(
                    "SELECT * FROM decisions WHERE case_id=?", (case_id,)
                ).fetchone()
                if decision is None:
                    raise NotFoundError("案件缺少承办建议")
                candidates = [c for c in self._cleared_candidates(connection, case_row)
                              if c.actor_id != decision["arbitrator_id"]]
                if not candidates:
                    raise EligibilityError("没有独立于承办人的合格复核人")
                chosen = sorted(candidates, key=lambda c: c.actor_id)[0]
                connection.execute(
                    "UPDATE decisions SET reviewer_id=? WHERE case_id=?",
                    (chosen.actor_id, case_id),
                )
                self._timeline(connection, case_id=case_id, event_type="reviewer.assigned",
                               actor_id=actor_id, detail={"reviewer_id": chosen.actor_id},
                               occurred_at=self._now())
                self._audit(connection, actor_id=actor_id, action="reviewer.assigned",
                            case_id=case_id, detail={"reviewer_id": chosen.actor_id})
                return ("reviewer_assignment", case_id,
                        {"case_id": case_id, "reviewer_id": chosen.actor_id})

            receipt = idempotent_write(connection, now=self._now(), request_id=request_id,
                                       action="assign_reviewer", payload=payload, create=create)
            decision_row = connection.execute(
                "SELECT reviewer_id FROM decisions WHERE case_id=?", (case_id,)
            ).fetchone()
            return {"case_id": case_id, "reviewer_id": decision_row["reviewer_id"],
                    "replayed": receipt.replayed}

    def _seal_terminal(self, connection, *, case_row, actor_id: str, status: str,
                       event_type: str, audit_action: str, outcome: dict[str, Any],
                       final_decision: str, new_score_version_id: str | None,
                       public_token: str | None, now: str) -> tuple[str, str, dict[str, Any]]:
        connection.execute(
            "UPDATE cases SET status=?, outcome_json=?, public_token=?, updated_at=? WHERE case_id=?",
            (status, canonical_json(outcome), public_token, now, case_row["case_id"]),
        )
        self._timeline(connection, case_id=case_row["case_id"], event_type=event_type,
                       actor_id=actor_id, detail=outcome, occurred_at=now)
        audit_detail = {"final_decision": final_decision,
                        "new_score_version_id": new_score_version_id}
        self._audit(connection, actor_id=actor_id, action=audit_action,
                    case_id=case_row["case_id"], detail=audit_detail)
        response = {"case_id": case_row["case_id"], "status": status}
        if public_token:
            response["public_token"] = public_token
        return "appeal_case", case_row["case_id"], response

    def review_decision(self, *, request_id: str, actor_id: str, case_id: str,
                        approved: bool, rationale: str,
                        correction_appeal_seconds: int = DEFAULT_CORRECTION_WINDOW_SECONDS) -> dict[str, Any]:
        """独立复核人批准终局决定，或退回承办人重新办理。"""

        payload = {"actor_id": actor_id, "case_id": case_id, "approved": bool(approved),
                   "rationale": rationale}
        rationale = self._text(rationale, "rationale", 4000)
        if not isinstance(correction_appeal_seconds, int) or correction_appeal_seconds <= 0:
            raise ValidationError("correction_appeal_seconds 必须是正整数")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, ARBITRATOR_ROLE)

            def _load_reviewable():
                case_row = self._case_row(connection, case_id)
                self._guard_terminal(case_row)
                if case_row["status"] != STATUS_UNDER_REVIEW:
                    raise ConflictError("案件当前不处于待复核状态")
                decision = connection.execute(
                    "SELECT * FROM decisions WHERE case_id=?", (case_id,)
                ).fetchone()
                if decision is None:
                    raise NotFoundError("案件缺少承办建议")
                if decision["reviewer_id"] != actor_id:
                    raise PermissionDenied("只有指定的独立复核人可以复核")
                # 独立性：复核人必须已通过本案回避核对，且不能是承办人本人
                if decision["arbitrator_id"] == actor_id:
                    raise PermissionDenied("承办人不能复核自己的建议")
                check = connection.execute(
                    "SELECT conflicted FROM conflict_checks WHERE case_id=? AND arbitrator_id=?",
                    (case_id, actor_id),
                ).fetchone()
                if check is None or check["conflicted"] != 0:
                    raise PermissionDenied("复核人未通过回避核对")
                return case_row, decision

            if not approved:
                def create_returned() -> tuple[str, str, dict[str, Any]]:
                    case_row, decision = _load_reviewable()
                    now = self._now()
                    connection.execute(
                        "UPDATE decisions SET review_status='returned', review_rationale=?,"
                        "reviewed_at=? WHERE case_id=?",
                        (rationale, now, case_id),
                    )
                    new_token = uuid.uuid4().hex
                    expires_at = self._after(DEFAULT_LEASE_SECONDS)
                    connection.execute(
                        "UPDATE cases SET status=?, lease_version=lease_version+1, lease_token=?,"
                        "lease_expires_at=?, updated_at=? WHERE case_id=?",
                        (STATUS_ASSIGNED, new_token, expires_at, now, case_id),
                    )
                    self._timeline(connection, case_id=case_id, event_type="review.returned",
                                   actor_id=actor_id, detail={"rationale": rationale},
                                   occurred_at=now)
                    self._audit(connection, actor_id=actor_id, action="review.returned",
                                case_id=case_id, detail={})
                    return ("decision_review", case_id,
                            {"case_id": case_id, "status": STATUS_ASSIGNED,
                             "new_lease_token": new_token})

                receipt = idempotent_write(connection, now=self._now(), request_id=request_id,
                                           action="review_decision_return", payload=payload,
                                           create=create_returned)
                stored = self._case_row(connection, case_id)
                return {"case_id": case_id, "status": STATUS_ASSIGNED, "approved": False,
                        "replayed": receipt.replayed,
                        "arbitrator_id": stored["assigned_arbitrator_id"],
                        "new_lease_token": stored["lease_token"],
                        "lease_expires_at": stored["lease_expires_at"]}

            def create_approved() -> tuple[str, str, dict[str, Any]]:
                case_row, decision = _load_reviewable()
                now = self._now()
                new_score_version_id: str | None = None
                correction_ref: str | None = None
                outcome: dict[str, Any] = {
                    "recommendation": decision["recommendation"],
                    "arbitrator_rationale": decision["rationale"],
                    "review_rationale": rationale,
                    "reviewer_id": actor_id,
                }
                if decision["recommendation"] == "correct":
                    corrected = json.loads(decision["correction_payload_json"])
                    current = connection.execute(
                        "SELECT * FROM score_versions WHERE score_version_id=?",
                        (case_row["score_version_id"],),
                    ).fetchone()
                    new_version = current["version"] + 1
                    new_score_version_id = uuid.uuid4().hex
                    deadline = self._after(correction_appeal_seconds)
                    corrected_hash = digest(corrected)
                    connection.execute(
                        "INSERT INTO score_versions(score_version_id,site_id,event_id,competitor_id,"
                        "version,published_at,appeal_deadline,payload_json,payload_hash,"
                        "superseded_by,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (new_score_version_id, current["site_id"], current["event_id"],
                         current["competitor_id"], new_version, now, deadline,
                         canonical_json(corrected), corrected_hash, None, actor_id, now),
                    )
                    # 原子切换成绩引用：同一事务内旧版本指向新版本
                    connection.execute(
                        "UPDATE score_versions SET superseded_by=? WHERE score_version_id=?",
                        (new_score_version_id, current["score_version_id"]),
                    )
                    correction_ref = f"score_version:{new_score_version_id}"
                    outcome.update({"new_score_version_id": new_score_version_id,
                                    "new_version": new_version, "correction_ref": correction_ref,
                                    "corrected_result_hash": corrected_hash,
                                    "correction_appeal_deadline": deadline})
                    final_status = STATUS_DECIDED
                    public_token = uuid.uuid4().hex
                elif decision["recommendation"] == "uphold":
                    final_status = STATUS_DECIDED
                    public_token = uuid.uuid4().hex
                else:
                    final_status = STATUS_REJECTED
                    public_token = None
                connection.execute(
                    "UPDATE decisions SET review_status='approved', review_rationale=?, reviewed_at=?,"
                    "final_decision=?, score_version_id=?, correction_ref=? WHERE case_id=?",
                    (rationale, now, decision["recommendation"], new_score_version_id,
                     correction_ref, case_id),
                )
                sealed = self._seal_terminal(
                    connection, case_row=case_row, actor_id=actor_id, status=final_status,
                    event_type="case.decided" if final_status == STATUS_DECIDED else "case.rejected",
                    audit_action=("appeal.decided" if final_status == STATUS_DECIDED
                                  else "appeal.rejected"),
                    outcome=outcome, final_decision=decision["recommendation"],
                    new_score_version_id=new_score_version_id, public_token=public_token, now=now,
                )
                if decision["recommendation"] == "correct":
                    self._timeline(connection, case_id=case_id, event_type="score.corrected",
                                   actor_id=actor_id,
                                   detail={"previous_score_version_id": case_row["score_version_id"],
                                           "new_score_version_id": new_score_version_id},
                                   occurred_at=now)
                return sealed

            receipt = idempotent_write(connection, now=self._now(), request_id=request_id,
                                       action="review_decision_approve", payload=payload,
                                       create=create_approved)
            row = self._case_row(connection, case_id)
            response = {"case_id": case_id, "status": row["status"], "approved": True,
                        "replayed": receipt.replayed}
            if row["public_token"]:
                response["public_token"] = row["public_token"]
            return response

    # ------------------------------------------------------------------ 撤诉

    def withdraw_appeal(self, *, request_id: str, actor_id: str, case_id: str,
                        reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "case_id": case_id, "reason": reason}
        reason = self._text(reason, "reason", 2000)
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "competitor")

            def create() -> tuple[str, str, dict[str, Any]]:
                case_row = self._case_row(connection, case_id)
                self._guard_terminal(case_row)
                if actor.role == "competitor" and actor.actor_id != case_row["appellant_id"]:
                    raise PermissionDenied("只能撤回本人的申诉")
                return self._seal_terminal(
                    connection, case_row=case_row, actor_id=actor_id, status=STATUS_WITHDRAWN,
                    event_type="case.withdrawn", audit_action="appeal.withdrawn",
                    outcome={"reason": reason}, final_decision="withdrawn",
                    new_score_version_id=None, public_token=None, now=self._now(),
                )

            receipt = idempotent_write(connection, now=self._now(), request_id=request_id,
                                       action="withdraw_appeal", payload=payload, create=create)
            return {"case_id": case_id, "status": STATUS_WITHDRAWN, "replayed": receipt.replayed}

    # ------------------------------------------------------------------ 查询视图

    def get_case_view(self, *, actor_id: str, case_id: str) -> dict[str, Any]:
        """面向秘书组、当事人和当前承办/复核人的案件视图。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        case_row = self._case_row(connection, case_id)
        decision = connection.execute(
            "SELECT reviewer_id FROM decisions WHERE case_id=?", (case_id,)
        ).fetchone()
        party = actor.actor_id in {case_row["appellant_id"],
                                   case_row["assigned_arbitrator_id"]} or (
            decision is not None and decision["reviewer_id"] == actor.actor_id)
        if actor.role not in {"admin", "operator", "auditor"} and not party:
            raise PermissionDenied("无权查看本案")
        view = self._case_dict(case_row)
        view["outcome"] = json.loads(case_row["outcome_json"]) if case_row["outcome_json"] else None
        view["public_token"] = case_row["public_token"]
        return view

    def list_cases(self, *, actor_id: str, status: str | None = None,
                   site_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require_roles(actor, "admin", "operator", "auditor")
        query = "SELECT * FROM cases WHERE 1=1"
        parameters: list[Any] = []
        if status:
            if status not in {STATUS_FILED, STATUS_ASSIGNED, STATUS_AWAITING,
                              STATUS_UNDER_REVIEW, *TERMINAL_STATUSES}:
                raise ValidationError("status 不在允许范围内")
            query += " AND status=?"
            parameters.append(status)
        if site_id:
            query += " AND site_id=?"
            parameters.append(site_id)
        query += " ORDER BY created_at, case_id"
        return [self._case_dict(r) for r in connection.execute(query, parameters).fetchall()]

    def public_result(self, public_token: str) -> dict[str, Any]:
        """公开裁决结果；隐藏任何可识别个人的信息。"""

        row = self.database.connection.execute(
            "SELECT * FROM cases WHERE public_token=?", (public_token,)
        ).fetchone()
        if row is None or row["status"] != STATUS_DECIDED:
            raise NotFoundError("公开结果不存在或尚未公布")
        outcome = json.loads(row["outcome_json"])
        masked = digest([row["case_id"], row["competitor_id"]])[:10]
        result: dict[str, Any] = {
            "public_token": public_token,
            "event_id": row["event_id"],
            "competitor_ref": f"competitor-{masked}",
            "final_decision": outcome.get("recommendation"),
            "decided_at": row["updated_at"],
        }
        if outcome.get("correction_ref"):
            result["correction_ref"] = outcome["correction_ref"]
            result["corrected_score_version_id"] = outcome["new_score_version_id"]
            result["new_appeal_deadline"] = outcome.get("correction_appeal_deadline")
        return result

    def case_timeline(self, *, actor_id: str, case_id: str) -> dict[str, Any]:
        """审计视图：还原材料时间线、回避过程和决定依据。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require_roles(actor, "admin", "auditor")
        case_row = self._case_row(connection, case_id)
        materials = [
            {"material_id": r["material_id"], "material_kind": r["material_kind"],
             "version": r["version"], "content_sha256": r["content_sha256"],
             "storage_ref": r["storage_ref"], "summary_text": r["summary_text"],
             "content_type": r["content_type"], "byte_length": r["byte_length"],
             "submitted_by": r["submitted_by"], "submitted_at": r["submitted_at"]}
            for r in connection.execute(
                "SELECT * FROM material_versions WHERE case_id=? "
                "ORDER BY submitted_at, material_id, version", (case_id,)).fetchall()
        ]
        checks = [
            {"arbitrator_id": r["arbitrator_id"], "conflicted": bool(r["conflicted"]),
             "basis": json.loads(r["basis_json"]), "checked_by": r["checked_by"],
             "checked_at": r["checked_at"]}
            for r in connection.execute(
                "SELECT * FROM conflict_checks WHERE case_id=? ORDER BY checked_at, arbitrator_id",
                (case_id,)).fetchall()
        ]
        events = [
            {"seq": r["seq"], "event_type": r["event_type"], "actor_id": r["actor_id"],
             "detail": json.loads(r["detail_json"]), "occurred_at": r["occurred_at"]}
            for r in connection.execute(
                "SELECT * FROM case_events WHERE case_id=? ORDER BY seq", (case_id,)).fetchall()
        ]
        decision_row = connection.execute(
            "SELECT * FROM decisions WHERE case_id=?", (case_id,)
        ).fetchone()
        decision = None
        if decision_row is not None:
            decision = {
                "recommendation": decision_row["recommendation"],
                "rationale": decision_row["rationale"],
                "arbitrator_id": decision_row["arbitrator_id"],
                "recommended_at": decision_row["recommended_at"],
                "review_status": decision_row["review_status"],
                "reviewer_id": decision_row["reviewer_id"],
                "review_rationale": decision_row["review_rationale"],
                "reviewed_at": decision_row["reviewed_at"],
                "final_decision": decision_row["final_decision"],
                "new_score_version_id": decision_row["score_version_id"],
                "correction_ref": decision_row["correction_ref"],
            }
        score_chain = [
            {"score_version_id": r["score_version_id"], "version": r["version"],
             "published_at": r["published_at"], "appeal_deadline": r["appeal_deadline"],
             "payload_hash": r["payload_hash"], "superseded_by": r["superseded_by"]}
            for r in connection.execute(
                "SELECT * FROM score_versions WHERE site_id=? AND event_id=? AND competitor_id=? "
                "ORDER BY version",
                (case_row["site_id"], case_row["event_id"], case_row["competitor_id"])).fetchall()
        ]
        return {"case": self._case_dict(case_row),
                "outcome": json.loads(case_row["outcome_json"]) if case_row["outcome_json"] else None,
                "materials": materials, "conflict_checks": checks,
                "events": events, "decision": decision, "score_chain": score_chain}
