"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示训练或赛事组织下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class IdempotentWrite:
    """在幂等回执之外附带首次写入时的业务响应。"""

    receipt: WriteReceipt
    response: dict[str, Any]


@dataclass(frozen=True)
class Competition:
    """表示一场可受理申诉的竞赛活动。"""

    competition_id: str
    name: str
    created_at: str


@dataclass(frozen=True)
class Competitor:
    """表示选手的内部登记信息，姓名等个人信息不进入公开视图。"""

    competitor_id: str
    competition_id: str
    person_name: str
    eligible: bool
    created_at: str


@dataclass(frozen=True)
class ScoreVersion:
    """表示一次成绩公布版本及其申诉期限。"""

    version_id: str
    competition_id: str
    version_label: str
    published_at: str
    appeal_deadline: str
    created_at: str


@dataclass(frozen=True)
class ScoreEntry:
    """表示选手在某个成绩版本中的当前成绩存储引用。"""

    entry_id: str
    version_id: str
    competitor_id: str
    score_ref: str
    created_at: str


@dataclass(frozen=True)
class ConflictDeclaration:
    """表示已申报的承办回避关系。"""

    conflict_id: str
    competition_id: str
    adjudicator_id: str
    competitor_id: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class EvidenceVersion:
    """表示一份不可变的申诉材料版本，仅含内容摘要与存储引用。"""

    evidence_id: str
    case_id: str
    version_seq: int
    kind: str
    content_digest: str
    storage_ref: str
    media_type: str | None
    byte_size: int | None
    submitted_by: str
    note: str | None
    created_at: str


@dataclass(frozen=True)
class CaseScreening:
    """表示一名裁决人员对一起案件的回避核对结论。"""

    screening_id: str
    case_id: str
    adjudicator_id: str
    status: str
    reason: str | None
    checked_by: str
    created_at: str
    resolved_at: str


@dataclass(frozen=True)
class CaseAssignment:
    """表示一次承办租约的历史记录，旧承办人据此被排除。"""

    assignment_id: str
    case_id: str
    adjudicator_id: str
    generation: int
    started_at: str
    released_at: str | None
    release_reason: str | None


@dataclass(frozen=True)
class AppealCase:
    """表示一起申诉案件及其承办、复核与终态信息。"""

    case_id: str
    case_number: str
    version_id: str
    competitor_id: str
    grounds: str
    status: str
    handler_id: str | None
    lease_generation: int
    leased_until: str | None
    reviewer_id: str | None
    recommendation: str | None
    recommendation_basis_digest: str | None
    recommendation_basis_ref: str | None
    proposed_score_ref: str | None
    recommended_at: str | None
    final_outcome: str | None
    final_basis_digest: str | None
    final_basis_ref: str | None
    corrected_score_ref: str | None
    decided_at: str | None
    created_at: str


@dataclass(frozen=True)
class Lease:
    """描述一次承办租约的持有时效。"""

    case_id: str
    handler_id: str
    generation: int
    leased_until: str
    status: str


@dataclass(frozen=True)
class ScoreReferenceUpdate:
    """表示成绩引用随终局裁决发生的一次原子更新。"""

    update_id: str
    case_id: str
    version_id: str
    competitor_id: str
    previous_ref: str
    new_ref: str
    created_at: str


@dataclass(frozen=True)
class PublicCaseResult:
    """公开结果视图，不包含任何可识别个人的信息。"""

    case_number: str
    grounds: str
    outcome: str
    corrected: bool
    decided_at: str
