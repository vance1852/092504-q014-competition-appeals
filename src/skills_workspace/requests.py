"""提供跨服务复用的幂等请求回执逻辑。"""

from __future__ import annotations

import re
from typing import Any, Callable

from .audit import canonical_json, digest
from .errors import ConflictError, ValidationError
from .models import WriteReceipt

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")


def normalize_request_id(request_id: str) -> str:
    """校验幂等请求编号。"""

    value = str(request_id or "").strip()
    if not IDENTIFIER.fullmatch(value):
        raise ValidationError("request_id 格式无效")
    return value


def idempotent_write(connection, *, now: str, request_id: str, action: str,
                     payload: dict[str, Any],
                     create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
    """在同一事务中执行一次带幂等回执的写入。"""

    request_id = normalize_request_id(request_id)
    payload_hash = digest(payload)
    row = connection.execute(
        "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
    ).fetchone()
    if row:
        if row["action"] != action or row["payload_hash"] != payload_hash:
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
    resource_type, resource_id, response = create()
    connection.execute(
        "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
        "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (request_id, action, payload_hash, resource_type, resource_id,
         canonical_json(response), now),
    )
    return WriteReceipt(request_id, resource_type, resource_id, False)
