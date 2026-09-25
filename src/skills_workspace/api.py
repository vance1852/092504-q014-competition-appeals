"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .appeals import AppealService
from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    appeals = AppealService(service.database, service.clock)
    query = parse_qs(parsed.query)

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, q("category"))]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(q("after_sequence", "0"))
            return 200, {"items": service.audit_events(after)}

        # 申诉证据封存与裁决
        if method == "POST" and parsed.path == "/score-versions":
            result = appeals.publish_score_version(actor_id=actor_id, **body)
            return 200 if result["replayed"] else 201, result
        if method == "POST" and parsed.path == "/disclosures":
            result = appeals.register_disclosure(actor_id=actor_id, **body)
            return 200 if result["replayed"] else 201, result
        if method == "POST" and parsed.path == "/conflict-checks":
            result = appeals.record_conflict_check(actor_id=actor_id, **body)
            return 200, result
        if method == "POST" and parsed.path == "/appeals":
            result = appeals.file_appeal(actor_id=actor_id, **body)
            return 200 if result["replayed"] else 201, result
        if method == "POST" and parsed.path == "/appeal-assignments":
            result = appeals.assign_case(actor_id=actor_id, **body)
            return 200 if result["replayed"] else 201, result
        if method == "POST" and parsed.path == "/lease-renewals":
            return 200, appeals.renew_lease(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/lease-reclaims":
            return 200, appeals.reclaim_expired_leases(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/materials":
            result = appeals.submit_material(actor_id=actor_id, **body)
            return 200 if result["replayed"] else 201, result
        if method == "GET" and parsed.path == "/materials":
            case_id = q("case_id", "")
            if not case_id:
                raise ValidationError("case_id 不能为空")
            return 200, {"items": appeals.list_materials(actor_id=actor_id, case_id=case_id)}
        if method == "POST" and parsed.path == "/supplement-requests":
            result = appeals.request_supplement(actor_id=actor_id, **body)
            return 200 if result["replayed"] else 201, result
        if method == "POST" and parsed.path == "/recommendations":
            result = appeals.recommend_decision(actor_id=actor_id, **body)
            return 200 if result["replayed"] else 201, result
        if method == "POST" and parsed.path == "/reviewer-assignments":
            result = appeals.assign_reviewer(actor_id=actor_id, **body)
            return 200 if result["replayed"] else 201, result
        if method == "POST" and parsed.path == "/reviews":
            result = appeals.review_decision(actor_id=actor_id, **body)
            return 200, result
        if method == "POST" and parsed.path == "/withdrawals":
            result = appeals.withdraw_appeal(actor_id=actor_id, **body)
            return 200 if result["replayed"] else 201, result
        if method == "GET" and parsed.path == "/cases":
            return 200, {"items": appeals.list_cases(actor_id=actor_id, status=q("status"),
                                                    site_id=q("site_id"))}
        if method == "GET" and parsed.path == "/case":
            case_id = q("case_id", "")
            if not case_id:
                raise ValidationError("case_id 不能为空")
            return 200, appeals.get_case_view(actor_id=actor_id, case_id=case_id)
        if method == "GET" and parsed.path == "/case-timeline":
            case_id = q("case_id", "")
            if not case_id:
                raise ValidationError("case_id 不能为空")
            return 200, appeals.case_timeline(actor_id=actor_id, case_id=case_id)
        if method == "GET" and parsed.path == "/public-result":
            token = q("public_token", "")
            if not token:
                raise ValidationError("public_token 不能为空")
            return 200, appeals.public_result(token)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
