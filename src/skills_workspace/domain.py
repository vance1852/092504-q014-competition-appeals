"""定义基础服务允许登记的资料类别与申诉裁决领域常量。"""

ALLOWED_CATEGORIES = frozenset({
    "institution_profile",
    "venue_registry",
    "resource_registry",
    "participant_assignment",
})


def is_allowed_category(value: str) -> bool:
    return value in ALLOWED_CATEGORIES


# 申诉材料类型：陈述书、结构化日志摘要、证明文件。
EVIDENCE_KINDS = frozenset({
    "statement",
    "structured_log_summary",
    "supporting_document",
})

# 案件生命周期状态。前三个为受理后流转状态，后三个为受保护终态。
CASE_STATUSES = frozenset({
    "screening",            # 资格/重复/利益冲突核对中
    "awaiting_assignment",
    "assigned",
    "in_review",            # 承办建议已提交，等待独立复核
    "withdrawn",            # 受保护终态：撤诉
    "rejected",             # 受保护终态：驳回
    "decided",              # 受保护终态：裁决
})

ACTIVE_CASE_STATUSES = frozenset({
    "screening",
    "awaiting_assignment",
    "assigned",
    "in_review",
})

TERMINAL_CASE_STATUSES = frozenset({
    "withdrawn",
    "rejected",
    "decided",
})

# 承办人的处理建议。
RECOMMENDATIONS = frozenset({
    "uphold",   # 建议维持原成绩
    "correct",  # 建议更正成绩
})

# 终局裁决结果（须经独立复核）。
FINAL_OUTCOMES = frozenset({
    "uphold",
    "correct",
})

# 回避核对结论。
SCREENING_STATUSES = frozenset({
    "pending",
    "clear",
    "conflicted",
})

# 承办租约分配/回收原因。
LEASE_RELEASE_REASONS = frozenset({
    "completed",
    "expired",
    "revoked",
})
