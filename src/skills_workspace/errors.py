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


class EligibilityError(DomainError):
    """申诉未通过资格、期限或重复申请校验。"""

    code = "eligibility_failed"
    status = 422


class LeaseError(ConflictError):
    """承办租约已超时或代号已失效。"""

    code = "lease_stale"


class ProtectedStateError(ConflictError):
    """案件处于受保护终态，不允许再变更。"""

    code = "protected_state"
