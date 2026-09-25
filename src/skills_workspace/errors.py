"""领域服务使用的业务异常。"""


class DomainError(Exception):
    """所有可预期业务异常的基类。"""

    code = "domain_error"
    status = 400


class ValidationError(DomainError):
    """输入字段不符合业务约束。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """请求引用的业务对象不存在。"""

    code = "not_found"
    status = 404


class PermissionDenied(DomainError):
    """操作者没有执行当前动作的权限。"""

    code = "permission_denied"
    status = 403


class ConflictError(DomainError):
    """请求编号或业务唯一键与既有内容冲突。"""

    code = "conflict"
    status = 409


class EligibilityError(ConflictError):
    """选手不具备申诉资格（未参赛、已取消资格等）。"""

    code = "competitor_ineligible"


class AppealWindowClosed(ConflictError):
    """申诉不在成绩版本允许的申诉期限内。"""

    code = "appeal_window_closed"


class DuplicateAppealError(ConflictError):
    """同一成绩版本与申诉事项已有在办或终局案件。"""

    code = "duplicate_appeal"


class ConflictOfInterestError(ConflictError):
    """候选承办人与案件存在未解除的利益冲突。"""

    code = "conflict_of_interest"


class NoAdjudicatorAvailable(ConflictError):
    """合格人员中暂无可分派的承办人。"""

    code = "no_adjudicator_available"


class LeaseExpired(ConflictError):
    """承办租约已到期并被回收，旧承办人不能继续提交。"""

    code = "lease_expired"


class NotLeaseHolder(ConflictError):
    """操作者不是案件当前生效租约的承办人。"""

    code = "not_lease_holder"


class ProtectedStateError(ConflictError):
    """案件已进入撤诉、驳回或裁决等受保护终态。"""

    code = "protected_state"
