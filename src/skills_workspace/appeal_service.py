"""提供竞赛申诉的证据封存、回避核对、租约承办与独立复核裁决能力。"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .domain import (
    ACTIVE_CASE_STATUSES,
    EVIDENCE_KINDS,
    FINAL_OUTCOMES,
    RECOMMENDATIONS,
    TERMINAL_CASE_STATUSES,
)
from .errors import (
    AppealWindowClosed,
    ConflictError,
    ConflictOfInterestError,
    DuplicateAppealError,
    EligibilityError,
    LeaseExpired,
    NoAdjudicatorAvailable,
    NotFoundError,
    NotLeaseHolder,
    PermissionDenied,
    ProtectedStateError,
    ValidationError,
)
from .models import (
    AppealCase,
    CaseAssignment,
    CaseScreening,
    ConflictDeclaration,
    EvidenceVersion,
    IdempotentWrite,
    Lease,
    PublicCaseResult,
    ScoreReferenceUpdate,
    WriteReceipt,
)
from .storage import Database


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
DIGEST64 = re.compile(r"^[a-f0-9]{64}$")
DEFAULT_LEASE_SECONDS = 24 * 60 * 60
DEFAULT_APPEAL_WINDOW_HOURS = 72

CASE_COLUMNS = (
    "case_id,case_number,version_id,competitor_id,grounds,grounds_digest,status,"
    "handler_id,lease_generation,leased_until,reviewer_id,recommendation,"
    "recommendation_basis_digest,recommendation_basis_ref,proposed_score_ref,recommended_at,"
    "final_outcome,final_basis_digest,final_basis_ref,corrected_score_ref,decided_at,created_at"
)


class AppealService:
    """协调申诉受理、回避核对、承办租约与终局裁决规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now_iso(self) -> str:
        return self._iso(self._now())

    def _iso(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _parse_iso(self, value: str, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO-8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc)

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _digest64(self, value: str, field: str) -> str:
        value = str(value).strip().lower()
        if not DIGEST64.fullmatch(value):
            raise ValidationError(f"{field} 必须是 64 位十六进制 SHA-256 摘要")
        return value

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> IdempotentWrite:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            receipt = WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
            return IdempotentWrite(receipt, {})
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now_iso()),
        )
        return IdempotentWrite(WriteReceipt(request_id, resource_type, resource_id, False), response)

    def _early_replay(self, *, request_id: str, action: str,
                      payload: dict[str, Any]) -> IdempotentWrite | None:
        """在任何业务规则之前识别幂等重放，避免被状态/时限规则误拦。

        回执一旦提交即不可变，因此在事务外读取已提交的回执是安全的。
        """

        request_id = self._identifier(request_id, "request_id")
        row = self.database.connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return IdempotentWrite(
            WriteReceipt(request_id, row["resource_type"], row["resource_id"], True), {})

    def _case_from_row(self, row) -> AppealCase:
        return AppealCase(
            case_id=row["case_id"], case_number=row["case_number"], version_id=row["version_id"],
            competitor_id=row["competitor_id"], grounds=row["grounds"], status=row["status"],
            handler_id=row["handler_id"], lease_generation=row["lease_generation"],
            leased_until=row["leased_until"], reviewer_id=row["reviewer_id"],
            recommendation=row["recommendation"],
            recommendation_basis_digest=row["recommendation_basis_digest"],
            recommendation_basis_ref=row["recommendation_basis_ref"],
            proposed_score_ref=row["proposed_score_ref"], recommended_at=row["recommended_at"],
            final_outcome=row["final_outcome"], final_basis_digest=row["final_basis_digest"],
            final_basis_ref=row["final_basis_ref"], corrected_score_ref=row["corrected_score_ref"],
            decided_at=row["decided_at"], created_at=row["created_at"],
        )

    def _load_case(self, connection, case_id: str) -> AppealCase:
        row = connection.execute(
            f"SELECT {CASE_COLUMNS} FROM appeal_cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("申诉案件不存在")
        return self._case_from_row(row)

    def _load_case_by_number(self, connection, case_number: str) -> AppealCase:
        row = connection.execute(
            f"SELECT {CASE_COLUMNS} FROM appeal_cases WHERE case_number=?", (case_number,)
        ).fetchone()
        if row is None:
            raise NotFoundError("申诉案件不存在")
        return self._case_from_row(row)

    def _adjudicator(self, connection, actor_id: str, competition_id: str):
        row = connection.execute(
            "SELECT * FROM adjudicators WHERE adjudicator_id=? AND competition_id=?",
            (actor_id, competition_id),
        ).fetchone()
        if row is None or not row["active"]:
            raise PermissionDenied("不是本场竞赛的在册裁决人员")
        return row

    def _guard_status(self, case: AppealCase, allowed: frozenset[str]) -> None:
        if case.status in TERMINAL_CASE_STATUSES:
            raise ProtectedStateError(f"案件已进入受保护终态（{case.status}），不能再变更")
        if case.status not in allowed:
            raise ConflictError(f"案件当前状态 {case.status} 不允许该操作")

    def _close_assignment(self, connection, case: AppealCase, reason: str, when: str) -> None:
        connection.execute(
            "UPDATE case_assignments SET released_at=?, release_reason=? "
            "WHERE case_id=? AND generation=? AND released_at IS NULL",
            (when, reason, case.case_id, case.lease_generation),
        )

    # ------------------------------------------------------------------ 赛事建档

    def register_competition(self, *, request_id: str, actor_id: str,
                             competition_id: str, name: str) -> IdempotentWrite:
        payload = {"actor_id": actor_id, "competition_id": competition_id, "name": name}
        early = self._early_replay(request_id=request_id, action="register_competition",
                                   payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            competition_id = self._identifier(competition_id, "competition_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO competitions(competition_id,name,created_at) VALUES(?,?,?)",
                        (competition_id, name, self._now_iso()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("竞赛编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="competition.registered",
                             resource_type="competition", resource_id=competition_id,
                             detail={"name": name}, occurred_at=self._now_iso())
                return "competition", competition_id, {"competition_id": competition_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_competition", payload=payload, create=create)

    def register_competitor(self, *, request_id: str, actor_id: str, competition_id: str,
                            competitor_id: str, person_name: str,
                            eligible: bool = True) -> IdempotentWrite:
        payload = {"actor_id": actor_id, "competition_id": competition_id,
                   "competitor_id": competitor_id, "person_name": person_name,
                   "eligible": bool(eligible)}
        early = self._early_replay(request_id=request_id, action="register_competitor", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            competition_id = self._identifier(competition_id, "competition_id")
            competitor_id = self._identifier(competitor_id, "competitor_id")
            person_name = self._text(person_name, "person_name")
            if connection.execute("SELECT 1 FROM competitions WHERE competition_id=?",
                                  (competition_id,)).fetchone() is None:
                raise NotFoundError("竞赛不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO competitors(competitor_id,competition_id,person_name,eligible,created_at)"
                        " VALUES(?,?,?,?,?)",
                        (competitor_id, competition_id, person_name,
                         1 if eligible else 0, self._now_iso()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("选手编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="competitor.registered",
                             resource_type="competitor", resource_id=competitor_id,
                             detail={"competition_id": competition_id, "eligible": bool(eligible)},
                             occurred_at=self._now_iso())
                return "competitor", competitor_id, {"competitor_id": competitor_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_competitor", payload=payload, create=create)

    def publish_score_version(self, *, request_id: str, actor_id: str, competition_id: str,
                              version_id: str, version_label: str,
                              entries: dict[str, str],
                              published_at: str | None = None,
                              appeal_window_hours: int = DEFAULT_APPEAL_WINDOW_HOURS) -> IdempotentWrite:
        if not isinstance(entries, dict) or not entries:
            raise ValidationError("entries 必须是非空的 选手编号->成绩存储引用 映射")
        if not isinstance(appeal_window_hours, int) or appeal_window_hours <= 0:
            raise ValidationError("appeal_window_hours 必须是正整数")
        payload = {"actor_id": actor_id, "competition_id": competition_id, "version_id": version_id,
                   "version_label": version_label, "entries": entries,
                   "published_at": published_at, "appeal_window_hours": appeal_window_hours}
        early = self._early_replay(request_id=request_id, action="publish_score_version", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            competition_id = self._identifier(competition_id, "competition_id")
            version_id = self._identifier(version_id, "version_id")
            version_label = self._text(version_label, "version_label", 120)
            if connection.execute("SELECT 1 FROM competitions WHERE competition_id=?",
                                  (competition_id,)).fetchone() is None:
                raise NotFoundError("竞赛不存在")
            published_dt = (self._parse_iso(published_at, "published_at")
                            if published_at else self._now())
            deadline_dt = published_dt + timedelta(hours=appeal_window_hours)
            published_iso = self._iso(published_dt)
            deadline_iso = self._iso(deadline_dt)
            clean_entries = {self._identifier(k, "competitor_id"): self._text(v, "score_ref", 500)
                             for k, v in entries.items()}
            for competitor_id in clean_entries:
                if connection.execute(
                    "SELECT 1 FROM competitors WHERE competitor_id=? AND competition_id=?",
                    (competitor_id, competition_id),
                ).fetchone() is None:
                    raise NotFoundError(f"选手 {competitor_id} 不属于该竞赛")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO score_versions(version_id,competition_id,version_label,"
                        "published_at,appeal_deadline,created_at) VALUES(?,?,?,?,?,?)",
                        (version_id, competition_id, version_label,
                         published_iso, deadline_iso, self._now_iso()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("成绩版本编号已经存在") from exc
                for competitor_id, score_ref in clean_entries.items():
                    connection.execute(
                        "INSERT INTO score_entries(entry_id,version_id,competitor_id,score_ref,created_at)"
                        " VALUES(?,?,?,?,?)",
                        (uuid.uuid4().hex, version_id, competitor_id, score_ref, self._now_iso()),
                    )
                append_event(connection, actor_id=actor_id, action="score_version.published",
                             resource_type="score_version", resource_id=version_id,
                             detail={"competition_id": competition_id,
                                     "version_label": version_label,
                                     "published_at": published_iso,
                                     "appeal_deadline": deadline_iso,
                                     "entry_count": len(clean_entries)},
                             occurred_at=self._now_iso())
                return "score_version", version_id, {
                    "version_id": version_id, "appeal_deadline": deadline_iso,
                    "entry_count": len(clean_entries),
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_score_version", payload=payload, create=create)

    def register_adjudicator(self, *, request_id: str, actor_id: str, competition_id: str,
                             adjudicator_id: str, display_name: str) -> IdempotentWrite:
        payload = {"actor_id": actor_id, "competition_id": competition_id,
                   "adjudicator_id": adjudicator_id, "display_name": display_name}
        early = self._early_replay(request_id=request_id, action="register_adjudicator", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            competition_id = self._identifier(competition_id, "competition_id")
            adjudicator_id = self._identifier(adjudicator_id, "adjudicator_id")
            display_name = self._text(display_name, "display_name")
            subject = self._actor(connection, adjudicator_id)
            if subject["role"] != "reviewer":
                raise ValidationError("裁决人员对应的操作者角色必须是 reviewer")
            if connection.execute("SELECT 1 FROM competitions WHERE competition_id=?",
                                  (competition_id,)).fetchone() is None:
                raise NotFoundError("竞赛不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO adjudicators(adjudicator_id,competition_id,display_name,active,created_at)"
                        " VALUES(?,?,?,1,?)",
                        (adjudicator_id, competition_id, display_name, self._now_iso()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("裁决人员已经登记") from exc
                append_event(connection, actor_id=actor_id, action="adjudicator.registered",
                             resource_type="adjudicator", resource_id=adjudicator_id,
                             detail={"competition_id": competition_id, "display_name": display_name},
                             occurred_at=self._now_iso())
                return "adjudicator", adjudicator_id, {"adjudicator_id": adjudicator_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_adjudicator", payload=payload, create=create)

    def declare_conflict(self, *, request_id: str, actor_id: str, competition_id: str,
                         adjudicator_id: str, competitor_id: str,
                         reason: str | None = None) -> IdempotentWrite:
        payload = {"actor_id": actor_id, "competition_id": competition_id,
                   "adjudicator_id": adjudicator_id, "competitor_id": competitor_id,
                   "reason": reason}
        early = self._early_replay(request_id=request_id, action="declare_conflict", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            competition_id = self._identifier(competition_id, "competition_id")
            adjudicator_id = self._identifier(adjudicator_id, "adjudicator_id")
            competitor_id = self._identifier(competitor_id, "competitor_id")
            reason = self._text(reason, "reason", 500) if reason is not None else None
            self._adjudicator(connection, adjudicator_id, competition_id)
            if connection.execute(
                "SELECT 1 FROM competitors WHERE competitor_id=? AND competition_id=?",
                (competitor_id, competition_id),
            ).fetchone() is None:
                raise NotFoundError("选手不属于该竞赛")

            def create() -> tuple[str, str, dict[str, Any]]:
                conflict_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO conflict_declarations(conflict_id,competition_id,"
                        "adjudicator_id,competitor_id,reason,created_at) VALUES(?,?,?,?,?,?)",
                        (conflict_id, competition_id, adjudicator_id, competitor_id,
                         reason, self._now_iso()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("该回避关系已经申报") from exc
                append_event(connection, actor_id=actor_id, action="conflict.declared",
                             resource_type="conflict_declaration", resource_id=conflict_id,
                             detail={"competition_id": competition_id,
                                     "adjudicator_id": adjudicator_id,
                                     "competitor_id": competitor_id},
                             occurred_at=self._now_iso())
                return "conflict_declaration", conflict_id, {"conflict_id": conflict_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="declare_conflict", payload=payload, create=create)

    # ------------------------------------------------------------------ 申诉受理

    def file_appeal(self, *, request_id: str, actor_id: str, version_id: str,
                    competitor_id: str, grounds: str,
                    evidence: list[dict[str, Any]] | None = None) -> IdempotentWrite:
        evidence = evidence or []
        if not isinstance(evidence, list):
            raise ValidationError("evidence 必须是数组")
        grounds = self._text(grounds, "grounds", 2000)
        payload = {"actor_id": actor_id, "version_id": version_id,
                   "competitor_id": competitor_id, "grounds": grounds,
                   "evidence": [self._normalize_evidence_item(item) for item in evidence]}
        early = self._early_replay(request_id=request_id, action="file_appeal", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            version_id = self._identifier(version_id, "version_id")
            competitor_id = self._identifier(competitor_id, "competitor_id")
            version = connection.execute(
                "SELECT * FROM score_versions WHERE version_id=?", (version_id,)
            ).fetchone()
            if version is None:
                raise NotFoundError("成绩版本不存在")
            competitor = connection.execute(
                "SELECT * FROM competitors WHERE competitor_id=? AND competition_id=?",
                (competitor_id, version["competition_id"]),
            ).fetchone()
            if competitor is None:
                raise NotFoundError("选手不属于该竞赛")
            # 1) 资格检查。
            if not competitor["eligible"]:
                raise EligibilityError("选手当前不具备申诉资格")
            # 2) 申诉期限检查（按成绩版本公布时间与期限）。
            now = self._now()
            deadline = self._parse_iso(version["appeal_deadline"], "appeal_deadline")
            if now > deadline:
                raise AppealWindowClosed(
                    f"申诉期限已于 {version['appeal_deadline']} 截止"
                )
            # 3) 重复申请检查：同一事项永远不可重复，在办案件覆盖同版本同选手。
            grounds_hash = digest(grounds)
            if connection.execute(
                "SELECT 1 FROM appeal_cases WHERE version_id=? AND competitor_id=? "
                "AND grounds_digest=?",
                (version_id, competitor_id, grounds_hash),
            ).fetchone():
                raise DuplicateAppealError("同一成绩版本与申诉事项的案件已经存在")
            if connection.execute(
                "SELECT 1 FROM appeal_cases WHERE version_id=? AND competitor_id=? "
                "AND status IN ('screening','awaiting_assignment','assigned','in_review')",
                (version_id, competitor_id),
            ).fetchone():
                raise DuplicateAppealError("该成绩版本已有在办申诉案件")

            def create() -> tuple[str, str, dict[str, Any]]:
                sequence_row = connection.execute(
                    "SELECT COUNT(*) AS count FROM appeal_cases WHERE version_id=?", (version_id,)
                ).fetchone()
                case_number = f"AP-{version_id}-{sequence_row['count'] + 1:04d}"
                case_id = uuid.uuid4().hex
                now_iso = self._now_iso()
                connection.execute(
                    "INSERT INTO appeal_cases(case_id,case_number,version_id,competitor_id,"
                    "grounds,grounds_digest,status,lease_generation,created_at)"
                    " VALUES(?,?,?,?,?,?,?,0,?)",
                    (case_id, case_number, version_id, competitor_id,
                     grounds, grounds_hash, "screening", now_iso),
                )
                evidence_ids = []
                for index, item in enumerate(payload["evidence"], start=1):
                    evidence_id = self._insert_evidence(
                        connection, case_id=case_id, version_seq=index,
                        submitted_by=actor_id, now_iso=now_iso, item=item,
                    )
                    evidence_ids.append(evidence_id)
                append_event(connection, actor_id=actor_id, action="appeal.filed",
                             resource_type="appeal_case", resource_id=case_id,
                             detail={"case_number": case_number, "version_id": version_id,
                                     "competitor_id": competitor_id,
                                     "grounds_digest": grounds_hash,
                                     "initial_evidence": evidence_ids},
                             occurred_at=now_iso)
                return "appeal_case", case_id, {
                    "case_id": case_id, "case_number": case_number,
                    "status": "screening", "evidence_versions": len(payload["evidence"]),
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="file_appeal", payload=payload, create=create)

    def _normalize_evidence_item(self, item: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValidationError("evidence 条目必须是对象")
        kind = str(item.get("kind", "")).strip()
        if kind not in EVIDENCE_KINDS:
            raise ValidationError("evidence.kind 必须是 statement/structured_log_summary/supporting_document")
        content_digest = self._digest64(item.get("content_digest", ""), "content_digest")
        storage_ref = self._text(item.get("storage_ref", ""), "storage_ref", 500)
        media_type = item.get("media_type")
        if media_type is not None:
            media_type = self._text(media_type, "media_type", 120)
        byte_size = item.get("byte_size")
        if byte_size is not None:
            if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size <= 0:
                raise ValidationError("byte_size 必须是正整数")
        note = item.get("note")
        if note is not None:
            note = self._text(note, "note", 1000)
        return {"kind": kind, "content_digest": content_digest, "storage_ref": storage_ref,
                "media_type": media_type, "byte_size": byte_size, "note": note}

    def _insert_evidence(self, connection, *, case_id: str, version_seq: int,
                         submitted_by: str, now_iso: str, item: dict[str, Any]) -> str:
        evidence_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO case_evidence(evidence_id,case_id,version_seq,kind,content_digest,"
            "storage_ref,media_type,byte_size,submitted_by,note,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (evidence_id, case_id, version_seq, item["kind"], item["content_digest"],
             item["storage_ref"], item["media_type"], item["byte_size"],
             submitted_by, item["note"], now_iso),
        )
        append_event(connection, actor_id=submitted_by, action="evidence.added",
                     resource_type="case_evidence", resource_id=evidence_id,
                     detail={"case_id": case_id, "version_seq": version_seq,
                             "kind": item["kind"], "content_digest": item["content_digest"],
                             "storage_ref": item["storage_ref"]},
                     occurred_at=now_iso)
        return evidence_id

    def add_evidence(self, *, request_id: str, actor_id: str, case_id: str,
                     kind: str, content_digest: str, storage_ref: str,
                     media_type: str | None = None, byte_size: int | None = None,
                     note: str | None = None) -> IdempotentWrite:
        """补交材料：只保存摘要与引用，并生成新的不可变版本（不覆盖旧版本）。"""

        raw_item = {"kind": kind, "content_digest": content_digest, "storage_ref": storage_ref,
                    "media_type": media_type, "byte_size": byte_size, "note": note}
        item = self._normalize_evidence_item(raw_item)
        payload = {"actor_id": actor_id, "case_id": case_id, **item}
        early = self._early_replay(request_id=request_id, action="add_evidence", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            case_id = self._identifier(case_id, "case_id")
            case = self._load_case(connection, case_id)
            self._guard_status(case, frozenset({"screening", "awaiting_assignment", "assigned"}))

            def create() -> tuple[str, str, dict[str, Any]]:
                count_row = connection.execute(
                    "SELECT COUNT(*) AS count FROM case_evidence WHERE case_id=?", (case_id,)
                ).fetchone()
                version_seq = count_row["count"] + 1
                now_iso = self._now_iso()
                evidence_id = self._insert_evidence(
                    connection, case_id=case_id, version_seq=version_seq,
                    submitted_by=actor_id, now_iso=now_iso, item=item,
                )
                return "case_evidence", evidence_id, {
                    "evidence_id": evidence_id, "case_id": case_id,
                    "version_seq": version_seq, "kind": item["kind"],
                    "content_digest": item["content_digest"],
                    "storage_ref": item["storage_ref"],
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="add_evidence", payload=payload, create=create)

    # ------------------------------------------------------------------ 回避核对

    def screen_adjudicator(self, *, request_id: str, actor_id: str, case_id: str,
                           adjudicator_id: str, result: str,
                           reason: str | None = None) -> IdempotentWrite:
        payload = {"actor_id": actor_id, "case_id": case_id,
                   "adjudicator_id": adjudicator_id, "result": result, "reason": reason}
        early = self._early_replay(request_id=request_id, action="screen_adjudicator", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            case_id = self._identifier(case_id, "case_id")
            adjudicator_id = self._identifier(adjudicator_id, "adjudicator_id")
            if result not in ("clear", "conflicted"):
                raise ValidationError("result 必须是 clear 或 conflicted")
            reason = self._text(reason, "reason", 500) if reason is not None else None
            case = self._load_case(connection, case_id)
            self._guard_status(case, frozenset({"screening"}))
            version = connection.execute(
                "SELECT * FROM score_versions WHERE version_id=?", (case.version_id,)
            ).fetchone()
            self._adjudicator(connection, adjudicator_id, version["competition_id"])
            declared = connection.execute(
                "SELECT 1 FROM conflict_declarations WHERE adjudicator_id=? AND competitor_id=?",
                (adjudicator_id, case.competitor_id),
            ).fetchone()
            if declared and result == "clear":
                raise ConflictOfInterestError("已申报利益冲突，不能作出无冲突结论")

            def create() -> tuple[str, str, dict[str, Any]]:
                screening_id = uuid.uuid4().hex
                now_iso = self._now_iso()
                try:
                    connection.execute(
                        "INSERT INTO case_screenings(screening_id,case_id,adjudicator_id,status,"
                        "reason,checked_by,created_at,resolved_at) VALUES(?,?,?,?,?,?,?,?)",
                        (screening_id, case_id, adjudicator_id, result, reason,
                         actor_id, now_iso, now_iso),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("该裁决人员对本案的回避结论已登记") from exc
                append_event(connection, actor_id=actor_id, action="adjudicator.screened",
                             resource_type="case_screening", resource_id=screening_id,
                             detail={"case_id": case_id, "adjudicator_id": adjudicator_id,
                                     "result": result},
                             occurred_at=now_iso)
                return "case_screening", screening_id, {
                    "screening_id": screening_id, "case_id": case_id,
                    "adjudicator_id": adjudicator_id, "status": result,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="screen_adjudicator", payload=payload, create=create)

    def complete_screening(self, *, request_id: str, actor_id: str,
                           case_id: str) -> IdempotentWrite:
        """回避核对完成且至少有一名无冲突合格人员后，案件进入待分派。"""

        payload = {"actor_id": actor_id, "case_id": case_id}
        early = self._early_replay(request_id=request_id, action="complete_screening", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            case_id = self._identifier(case_id, "case_id")
            case = self._load_case(connection, case_id)
            self._guard_status(case, frozenset({"screening"}))
            clear_count = connection.execute(
                "SELECT COUNT(*) AS count FROM case_screenings WHERE case_id=? AND status='clear'",
                (case_id,),
            ).fetchone()["count"]
            if not clear_count:
                raise ConflictOfInterestError("尚无通过回避核对的裁决人员，不能进入分派")

            def create() -> tuple[str, str, dict[str, Any]]:
                now_iso = self._now_iso()
                connection.execute(
                    "UPDATE appeal_cases SET status='awaiting_assignment' WHERE case_id=?",
                    (case_id,),
                )
                append_event(connection, actor_id=actor_id, action="case.screening_completed",
                             resource_type="appeal_case", resource_id=case_id,
                             detail={"clear_count": clear_count}, occurred_at=now_iso)
                return "appeal_case", case_id, {"case_id": case_id,
                                                "status": "awaiting_assignment"}

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_screening", payload=payload, create=create)

    # ------------------------------------------------------------------ 承办租约

    def assign_handler(self, *, request_id: str, actor_id: str, case_id: str,
                       lease_seconds: int = DEFAULT_LEASE_SECONDS) -> IdempotentWrite:
        if not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValidationError("lease_seconds 必须是正整数")
        payload = {"actor_id": actor_id, "case_id": case_id, "lease_seconds": lease_seconds}
        early = self._early_replay(request_id=request_id, action="assign_handler", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            case_id = self._identifier(case_id, "case_id")
            case = self._load_case(connection, case_id)
            self._guard_status(case, frozenset({"awaiting_assignment"}))
            version = connection.execute(
                "SELECT * FROM score_versions WHERE version_id=?", (case.version_id,)
            ).fetchone()
            candidate = connection.execute(
                "SELECT a.adjudicator_id FROM adjudicators a "
                "WHERE a.competition_id=? AND a.active=1 "
                "AND EXISTS (SELECT 1 FROM case_screenings s WHERE s.case_id=? "
                "            AND s.adjudicator_id=a.adjudicator_id AND s.status='clear') "
                "AND NOT EXISTS (SELECT 1 FROM conflict_declarations c "
                "                WHERE c.adjudicator_id=a.adjudicator_id AND c.competitor_id=? "
                "                AND c.competitor_id=?) "
                "AND a.adjudicator_id NOT IN "
                "    (SELECT adjudicator_id FROM case_assignments WHERE case_id=?) "
                "ORDER BY (SELECT COUNT(*) FROM appeal_cases ac WHERE ac.handler_id=a.adjudicator_id "
                "         AND ac.status='assigned') ASC, a.adjudicator_id ASC LIMIT 1",
                (version["competition_id"], case_id, version["competition_id"],
                 case.competitor_id, case_id),
            ).fetchone()
            if candidate is None:
                raise NoAdjudicatorAvailable("合格人员中暂无可分派的承办人")
            handler_id = candidate["adjudicator_id"]
            generation = case.lease_generation + 1
            leased_until = self._iso(self._now() + timedelta(seconds=lease_seconds))

            def create() -> tuple[str, str, dict[str, Any]]:
                now_iso = self._now_iso()
                connection.execute(
                    "INSERT INTO case_assignments(assignment_id,case_id,adjudicator_id,generation,"
                    "started_at) VALUES(?,?,?,?,?)",
                    (uuid.uuid4().hex, case_id, handler_id, generation, now_iso),
                )
                connection.execute(
                    "UPDATE appeal_cases SET status='assigned', handler_id=?, lease_generation=?, "
                    "leased_until=? WHERE case_id=?",
                    (handler_id, generation, leased_until, case_id),
                )
                append_event(connection, actor_id=actor_id, action="case.assigned",
                             resource_type="appeal_case", resource_id=case_id,
                             detail={"handler_id": handler_id, "generation": generation,
                                     "leased_until": leased_until},
                             occurred_at=now_iso)
                return "appeal_case", case_id, {"case_id": case_id, "status": "assigned",
                                                "handler_id": handler_id,
                                                "generation": generation,
                                                "leased_until": leased_until}

            return self._idempotent(connection, request_id=request_id,
                                    action="assign_handler", payload=payload, create=create)

    def _require_active_lease(self, connection, case: AppealCase, actor) -> AppealCase:
        """校验操作者持有当前有效租约；过期租约立即判定失效。"""

        self._require(actor, "reviewer")
        self._guard_status(case, frozenset({"assigned"}))
        if case.handler_id != actor["actor_id"]:
            raise NotLeaseHolder("只有当前承办人可以执行该动作")
        if case.leased_until and self._now_iso() > case.leased_until:
            raise LeaseExpired("承办租约已超时，请等待回收后重新分派")
        return case

    def current_lease(self, case_id: str) -> Lease | None:
        with self.database.transaction() as connection:
            case = self._load_case(connection, case_id)
            if case.status != "assigned" or not case.handler_id or not case.leased_until:
                return None
            active = self._now_iso() <= case.leased_until
            return Lease(case.case_id, case.handler_id, case.lease_generation,
                         case.leased_until, "active" if active else "expired")

    def reclaim_expired_leases(self, *, actor_id: str) -> dict[str, Any]:
        """回收全部超时租约；旧承办人随即失去提交资格，案件回到待分派。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            now_iso = self._now_iso()
            expired = connection.execute(
                f"SELECT {CASE_COLUMNS} FROM appeal_cases "
                "WHERE status='assigned' AND leased_until IS NOT NULL AND leased_until<?",
                (now_iso,),
            ).fetchall()
            reclaimed = []
            for row in expired:
                case = self._case_from_row(row)
                self._close_assignment(connection, case, "expired", now_iso)
                connection.execute(
                    "UPDATE appeal_cases SET status='awaiting_assignment', handler_id=NULL, "
                    "leased_until=NULL, lease_generation=lease_generation+1 WHERE case_id=?",
                    (case.case_id,),
                )
                append_event(connection, actor_id=actor_id, action="lease.reclaimed",
                             resource_type="appeal_case", resource_id=case.case_id,
                             detail={"previous_handler": case.handler_id,
                                     "generation": case.lease_generation},
                             occurred_at=now_iso)
                reclaimed.append({"case_id": case.case_id, "previous_handler": case.handler_id})
            return {"reclaimed": reclaimed, "count": len(reclaimed)}

    def request_evidence(self, *, request_id: str, actor_id: str, case_id: str,
                         note: str) -> IdempotentWrite:
        note = self._text(note, "note", 1000)
        payload = {"actor_id": actor_id, "case_id": case_id, "note": note}
        early = self._early_replay(request_id=request_id, action="request_evidence", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            case = self._load_case(connection, self._identifier(case_id, "case_id"))
            self._require_active_lease(connection, case, actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                now_iso = self._now_iso()
                cursor = connection.execute(
                    "INSERT INTO evidence_requests(case_id,note,requested_by,created_at)"
                    " VALUES(?,?,?,?)",
                    (case_id, note, actor_id, now_iso),
                )
                request_seq = cursor.lastrowid
                append_event(connection, actor_id=actor_id, action="evidence.requested",
                             resource_type="evidence_request", resource_id=str(request_seq),
                             detail={"case_id": case_id, "note": note,
                                     "generation": case.lease_generation},
                             occurred_at=now_iso)
                return "evidence_request", str(request_seq), {
                    "request_seq": request_seq, "case_id": case_id, "note": note,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="request_evidence", payload=payload, create=create)

    def submit_recommendation(self, *, request_id: str, actor_id: str, case_id: str,
                              recommendation: str, basis_digest: str, basis_ref: str,
                              proposed_score_ref: str | None = None) -> IdempotentWrite:
        if recommendation not in RECOMMENDATIONS:
            raise ValidationError("recommendation 必须是 uphold 或 correct")
        basis_digest = self._digest64(basis_digest, "basis_digest")
        basis_ref = self._text(basis_ref, "basis_ref", 500)
        if recommendation == "correct":
            proposed_score_ref = self._text(proposed_score_ref or "", "proposed_score_ref", 500)
        elif proposed_score_ref is not None:
            proposed_score_ref = self._text(proposed_score_ref, "proposed_score_ref", 500)
        payload = {"actor_id": actor_id, "case_id": case_id, "recommendation": recommendation,
                   "basis_digest": basis_digest, "basis_ref": basis_ref,
                   "proposed_score_ref": proposed_score_ref}
        early = self._early_replay(request_id=request_id, action="submit_recommendation", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            case = self._load_case(connection, self._identifier(case_id, "case_id"))
            self._require_active_lease(connection, case, actor)
            if recommendation == "correct":
                entry = connection.execute(
                    "SELECT 1 FROM score_entries WHERE version_id=? AND competitor_id=?",
                    (case.version_id, case.competitor_id),
                ).fetchone()
                if entry is None:
                    raise NotFoundError("成绩版本中不存在该选手的成绩条目")

            def create() -> tuple[str, str, dict[str, Any]]:
                now_iso = self._now_iso()
                connection.execute(
                    "UPDATE appeal_cases SET status='in_review', recommendation=?, "
                    "recommendation_basis_digest=?, recommendation_basis_ref=?, "
                    "proposed_score_ref=?, recommended_at=? WHERE case_id=?",
                    (recommendation, basis_digest, basis_ref,
                     proposed_score_ref, now_iso, case_id),
                )
                append_event(connection, actor_id=actor_id, action="recommendation.submitted",
                             resource_type="appeal_case", resource_id=case_id,
                             detail={"generation": case.lease_generation,
                                     "recommendation": recommendation,
                                     "basis_digest": basis_digest, "basis_ref": basis_ref,
                                     "proposed_score_ref": proposed_score_ref},
                             occurred_at=now_iso)
                return "appeal_case", case_id, {"case_id": case_id, "status": "in_review",
                                                "recommendation": recommendation}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_recommendation", payload=payload, create=create)

    # ------------------------------------------------------------------ 终局决定

    def withdraw_appeal(self, *, request_id: str, actor_id: str,
                        case_id: str) -> IdempotentWrite:
        payload = {"actor_id": actor_id, "case_id": case_id}
        early = self._early_replay(request_id=request_id, action="withdraw_appeal", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            case = self._load_case(connection, self._identifier(case_id, "case_id"))
            self._guard_status(case, ACTIVE_CASE_STATUSES)

            def create() -> tuple[str, str, dict[str, Any]]:
                now_iso = self._now_iso()
                if case.status == "assigned":
                    self._close_assignment(connection, case, "completed", now_iso)
                connection.execute(
                    "UPDATE appeal_cases SET status='withdrawn', handler_id=NULL, "
                    "leased_until=NULL, decided_at=? WHERE case_id=?",
                    (now_iso, case_id),
                )
                append_event(connection, actor_id=actor_id, action="appeal.withdrawn",
                             resource_type="appeal_case", resource_id=case_id,
                             detail={"previous_status": case.status}, occurred_at=now_iso)
                return "appeal_case", case_id, {"case_id": case_id, "status": "withdrawn"}

            return self._idempotent(connection, request_id=request_id,
                                    action="withdraw_appeal", payload=payload, create=create)

    def _independent_reviewer(self, connection, actor, case: AppealCase):
        """复核人必须是在册、无回避、且从未承办本案的第三方。"""

        self._require(actor, "reviewer")
        version = connection.execute(
            "SELECT * FROM score_versions WHERE version_id=?", (case.version_id,)
        ).fetchone()
        self._adjudicator(connection, actor["actor_id"], version["competition_id"])
        if case.handler_id == actor["actor_id"]:
            raise PermissionDenied("独立复核不能由本案承办人执行")
        if connection.execute(
            "SELECT 1 FROM case_assignments WHERE case_id=? AND adjudicator_id=?",
            (case.case_id, actor["actor_id"]),
        ).fetchone():
            raise PermissionDenied("曾承办本案的人员不能担任独立复核人")
        if connection.execute(
            "SELECT 1 FROM conflict_declarations WHERE adjudicator_id=? AND competitor_id=?",
            (actor["actor_id"], case.competitor_id),
        ).fetchone():
            raise ConflictOfInterestError("复核人与案件存在利益冲突")
        return version

    def reject_appeal(self, *, request_id: str, actor_id: str, case_id: str,
                      basis_digest: str, basis_ref: str) -> IdempotentWrite:
        basis_digest = self._digest64(basis_digest, "basis_digest")
        basis_ref = self._text(basis_ref, "basis_ref", 500)
        payload = {"actor_id": actor_id, "case_id": case_id,
                   "basis_digest": basis_digest, "basis_ref": basis_ref}
        early = self._early_replay(request_id=request_id, action="reject_appeal", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            case = self._load_case(connection, self._identifier(case_id, "case_id"))
            self._guard_status(case, frozenset({"in_review"}))
            self._independent_reviewer(connection, actor, case)

            def create() -> tuple[str, str, dict[str, Any]]:
                now_iso = self._now_iso()
                self._close_assignment(connection, case, "completed", now_iso)
                connection.execute(
                    "UPDATE appeal_cases SET status='rejected', reviewer_id=?, "
                    "final_basis_digest=?, final_basis_ref=?, handler_id=NULL, "
                    "leased_until=NULL, decided_at=? WHERE case_id=?",
                    (actor_id, basis_digest, basis_ref, now_iso, case_id),
                )
                append_event(connection, actor_id=actor_id, action="appeal.rejected",
                             resource_type="appeal_case", resource_id=case_id,
                             detail={"reviewer_id": actor_id, "handler_id": case.handler_id,
                                     "basis_digest": basis_digest, "basis_ref": basis_ref},
                             occurred_at=now_iso)
                return "appeal_case", case_id, {"case_id": case_id, "status": "rejected"}

            return self._idempotent(connection, request_id=request_id,
                                    action="reject_appeal", payload=payload, create=create)

    def decide_appeal(self, *, request_id: str, actor_id: str, case_id: str,
                      outcome: str, basis_digest: str, basis_ref: str,
                      corrected_score_ref: str | None = None) -> IdempotentWrite:
        if outcome not in FINAL_OUTCOMES:
            raise ValidationError("outcome 必须是 uphold 或 correct")
        basis_digest = self._digest64(basis_digest, "basis_digest")
        basis_ref = self._text(basis_ref, "basis_ref", 500)
        if outcome == "correct":
            corrected_score_ref = self._text(corrected_score_ref or "",
                                             "corrected_score_ref", 500)
        payload = {"actor_id": actor_id, "case_id": case_id, "outcome": outcome,
                   "basis_digest": basis_digest, "basis_ref": basis_ref,
                   "corrected_score_ref": corrected_score_ref}
        early = self._early_replay(request_id=request_id, action="decide_appeal", payload=payload)
        if early is not None:
            return early
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            case = self._load_case(connection, self._identifier(case_id, "case_id"))
            self._guard_status(case, frozenset({"in_review"}))
            self._independent_reviewer(connection, actor, case)
            if outcome == "correct":
                # 复核更正必须建立在承办人的更正建议与拟更正引用之上。
                if case.recommendation != "correct" or not case.proposed_score_ref:
                    raise ConflictError("没有承办人的更正建议与拟更正引用，不能作出更正裁决")
                if corrected_score_ref != case.proposed_score_ref:
                    raise ConflictError("裁决更正引用必须与承办建议的拟更正引用一致")
            entry_row = connection.execute(
                "SELECT * FROM score_entries WHERE version_id=? AND competitor_id=?",
                (case.version_id, case.competitor_id),
            ).fetchone()
            if entry_row is None:
                raise NotFoundError("成绩版本中不存在该选手的成绩条目")

            def create() -> tuple[str, str, dict[str, Any]]:
                now_iso = self._now_iso()
                self._close_assignment(connection, case, "completed", now_iso)
                # 终局决定与成绩引用更新在同一事务内原子完成。
                connection.execute(
                    "UPDATE appeal_cases SET status='decided', reviewer_id=?, final_outcome=?, "
                    "final_basis_digest=?, final_basis_ref=?, corrected_score_ref=?, "
                    "handler_id=NULL, leased_until=NULL, decided_at=? WHERE case_id=?",
                    (actor_id, outcome, basis_digest, basis_ref, corrected_score_ref,
                     now_iso, case_id),
                )
                if outcome == "correct":
                    connection.execute(
                        "UPDATE score_entries SET score_ref=? WHERE entry_id=?",
                        (corrected_score_ref, entry_row["entry_id"]),
                    )
                    update_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO score_reference_updates(update_id,case_id,version_id,"
                        "competitor_id,previous_ref,new_ref,created_at) VALUES(?,?,?,?,?,?,?)",
                        (update_id, case_id, case.version_id, case.competitor_id,
                         entry_row["score_ref"], corrected_score_ref, now_iso),
                    )
                append_event(connection, actor_id=actor_id, action="appeal.decided",
                             resource_type="appeal_case", resource_id=case_id,
                             detail={"reviewer_id": actor_id, "handler_id": case.handler_id,
                                     "outcome": outcome, "basis_digest": basis_digest,
                                     "basis_ref": basis_ref,
                                     "corrected_score_ref": corrected_score_ref},
                             occurred_at=now_iso)
                if outcome == "correct":
                    append_event(connection, actor_id=actor_id,
                                 action="score_reference.updated",
                                 resource_type="score_reference_update", resource_id=update_id,
                                 detail={"case_id": case_id, "version_id": case.version_id,
                                         "competitor_id": case.competitor_id,
                                         "previous_ref": entry_row["score_ref"],
                                         "new_ref": corrected_score_ref},
                                 occurred_at=now_iso)
                return "appeal_case", case_id, {"case_id": case_id, "status": "decided",
                                                "outcome": outcome,
                                                "corrected_score_ref": corrected_score_ref}

            return self._idempotent(connection, request_id=request_id,
                                    action="decide_appeal", payload=payload, create=create)

    # ------------------------------------------------------------------ 查询视图

    def get_case(self, case_id: str) -> AppealCase:
        row = self.database.connection.execute(
            f"SELECT {CASE_COLUMNS} FROM appeal_cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("申诉案件不存在")
        return self._case_from_row(row)

    def list_evidence(self, case_id: str) -> list[EvidenceVersion]:
        rows = self.database.connection.execute(
            "SELECT * FROM case_evidence WHERE case_id=? ORDER BY version_seq", (case_id,)
        ).fetchall()
        return [EvidenceVersion(row["evidence_id"], row["case_id"], row["version_seq"],
                                row["kind"], row["content_digest"], row["storage_ref"],
                                row["media_type"], row["byte_size"], row["submitted_by"],
                                row["note"], row["created_at"]) for row in rows]

    def list_screenings(self, case_id: str) -> list[CaseScreening]:
        rows = self.database.connection.execute(
            "SELECT * FROM case_screenings WHERE case_id=? ORDER BY created_at", (case_id,)
        ).fetchall()
        return [CaseScreening(row["screening_id"], row["case_id"], row["adjudicator_id"],
                              row["status"], row["reason"], row["checked_by"],
                              row["created_at"], row["resolved_at"]) for row in rows]

    def list_assignments(self, case_id: str) -> list[CaseAssignment]:
        rows = self.database.connection.execute(
            "SELECT * FROM case_assignments WHERE case_id=? ORDER BY generation", (case_id,)
        ).fetchall()
        return [CaseAssignment(row["assignment_id"], row["case_id"], row["adjudicator_id"],
                               row["generation"], row["started_at"], row["released_at"],
                               row["release_reason"]) for row in rows]

    def list_conflicts(self, competition_id: str, competitor_id: str) -> list[ConflictDeclaration]:
        rows = self.database.connection.execute(
            "SELECT * FROM conflict_declarations WHERE competition_id=? AND competitor_id=?",
            (competition_id, competitor_id),
        ).fetchall()
        return [ConflictDeclaration(row["conflict_id"], row["competition_id"],
                                    row["adjudicator_id"], row["competitor_id"],
                                    row["reason"], row["created_at"]) for row in rows]

    def list_score_updates(self, case_id: str) -> list[ScoreReferenceUpdate]:
        rows = self.database.connection.execute(
            "SELECT * FROM score_reference_updates WHERE case_id=? ORDER BY created_at", (case_id,)
        ).fetchall()
        return [ScoreReferenceUpdate(row["update_id"], row["case_id"], row["version_id"],
                                    row["competitor_id"], row["previous_ref"], row["new_ref"],
                                    row["created_at"]) for row in rows]

    def public_result(self, case_number: str) -> PublicCaseResult:
        """公开结果：仅给出案号、事由、结论与更正标记，隐藏一切个人信息。"""

        with self.database.transaction() as connection:
            case = self._load_case_by_number(connection, case_number)
        if case.status not in TERMINAL_CASE_STATUSES:
            raise NotFoundError("案件尚未作出终局决定")
        if case.status == "decided":
            outcome = case.final_outcome or "decided"
        else:
            outcome = case.status
        return PublicCaseResult(
            case_number=case.case_number, grounds=case.grounds, outcome=outcome,
            corrected=bool(case.corrected_score_ref), decided_at=case.decided_at or "",
        )

    def _reviewer_may_view(self, connection, actor, case: AppealCase) -> bool:
        """判断裁决人员是否有权查看案件内容。"""

        if actor["actor_id"] == case.handler_id or actor["actor_id"] == case.reviewer_id:
            return True
        # 复核阶段：任何满足独立复核资格（在册、无回避、未承办过本案）的人可阅卷。
        if case.status != "in_review":
            return False
        version = connection.execute(
            "SELECT competition_id FROM score_versions WHERE version_id=?", (case.version_id,)
        ).fetchone()
        if version is None:
            return False
        if connection.execute(
            "SELECT 1 FROM adjudicators WHERE adjudicator_id=? AND competition_id=? AND active=1",
            (actor["actor_id"], version["competition_id"]),
        ).fetchone() is None:
            return False
        if connection.execute(
            "SELECT 1 FROM case_assignments WHERE case_id=? AND adjudicator_id=?",
            (case.case_id, actor["actor_id"]),
        ).fetchone():
            return False
        if connection.execute(
            "SELECT 1 FROM conflict_declarations WHERE adjudicator_id=? AND competitor_id=?",
            (actor["actor_id"], case.competitor_id),
        ).fetchone():
            return False
        return True

    def case_snapshot(self, *, actor_id: str, case_id: str) -> dict[str, Any]:
        """办案视图：回避核对完成前承办候选人不得查看，办案/复核人员按资格阅卷。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            case = self._load_case(connection, case_id)
            if actor["role"] in {"admin", "operator", "auditor"}:
                pass
            elif not self._reviewer_may_view(connection, actor, case):
                if case.status in ("screening", "awaiting_assignment"):
                    raise PermissionDenied("案件仍在回避核对阶段，承办候选人不得查看")
                raise PermissionDenied("无权查看该案件")
            return self._snapshot_payload(connection, case)

    def _snapshot_payload(self, connection, case: AppealCase) -> dict[str, Any]:
        return {
            "case": case.__dict__,
            "evidence": [item.__dict__ for item in self.list_evidence(case.case_id)],
            "screenings": [item.__dict__ for item in self.list_screenings(case.case_id)],
            "assignments": [item.__dict__ for item in self.list_assignments(case.case_id)],
            "score_updates": [item.__dict__ for item in self.list_score_updates(case.case_id)],
        }

    def case_timeline(self, *, actor_id: str, case_id: str) -> dict[str, Any]:
        """审计视图：还原材料时间线、回避过程与决定依据。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "auditor")
            case = self._load_case(connection, case_id)
            version = connection.execute(
                "SELECT * FROM score_versions WHERE version_id=?", (case.version_id,)
            ).fetchone()
            events = []
            for row in connection.execute("SELECT * FROM audit_events ORDER BY sequence"):
                detail = json.loads(row["detail_json"])
                if row["resource_id"] == case_id or detail.get("case_id") == case_id:
                    events.append({"sequence": row["sequence"], "actor_id": row["actor_id"],
                                   "action": row["action"], "resource_type": row["resource_type"],
                                   "resource_id": row["resource_id"], "detail": detail,
                                   "occurred_at": row["occurred_at"],
                                   "event_hash": row["event_hash"]})
            conflicts = [item.__dict__ for item in
                         self.list_conflicts(version["competition_id"], case.competitor_id)]
            return {
                "case": case.__dict__,
                "version": {"version_id": version["version_id"],
                            "version_label": version["version_label"],
                            "published_at": version["published_at"],
                            "appeal_deadline": version["appeal_deadline"]},
                "events": events,
                "evidence": [item.__dict__ for item in self.list_evidence(case_id)],
                "screenings": [item.__dict__ for item in self.list_screenings(case_id)],
                "assignments": [item.__dict__ for item in self.list_assignments(case_id)],
                "conflict_declarations": conflicts,
                "score_updates": [item.__dict__ for item in self.list_score_updates(case_id)],
                "evidence_requests": [
                    {"request_seq": row["request_seq"], "note": row["note"],
                     "requested_by": row["requested_by"], "created_at": row["created_at"]}
                    for row in connection.execute(
                        "SELECT * FROM evidence_requests WHERE case_id=? ORDER BY request_seq",
                        (case_id,),
                    ).fetchall()
                ],
            }
