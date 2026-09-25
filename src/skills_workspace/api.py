"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .appeal_service import AppealService
from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


def _write_result(result) -> tuple[int, dict[str, Any]]:
    """统一转换幂等写回结果。"""

    receipt = result.receipt if hasattr(result, "receipt") else result
    payload = dict(receipt.__dict__)
    response = getattr(result, "response", None)
    if response:
        payload["response"] = response
    return 200 if receipt.replayed else 201, payload


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    appeals = AppealService(service.database, service.clock)
    segments = [segment for segment in parsed.path.split("/") if segment]
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
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        status, payload = _appeal_route(appeals, method, segments, parse_qs(parsed.query),
                                        body, actor_id)
        if status is not None:
            return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _appeal_route(appeals: AppealService, method: str, segments: list[str],
                  query: dict[str, list[str]], body: dict[str, Any],
                  actor_id: str) -> tuple[int | None, dict[str, Any]]:
    """处理竞赛申诉与裁决相关路由。"""

    def q(name: str, default: str = "") -> str:
        return query.get(name, [default])[0]

    if method == "POST" and segments == ["competitions"]:
        return _write_result(appeals.register_competition(actor_id=actor_id, **body))
    if method == "POST" and segments == ["competitors"]:
        return _write_result(appeals.register_competitor(actor_id=actor_id, **body))
    if method == "POST" and segments == ["score-versions"]:
        return _write_result(appeals.publish_score_version(actor_id=actor_id, **body))
    if method == "POST" and segments == ["adjudicators"]:
        return _write_result(appeals.register_adjudicator(actor_id=actor_id, **body))
    if method == "POST" and segments == ["conflict-declarations"]:
        return _write_result(appeals.declare_conflict(actor_id=actor_id, **body))
    if method == "POST" and segments == ["appeals"]:
        return _write_result(appeals.file_appeal(actor_id=actor_id, **body))
    if len(segments) == 3 and segments[0] == "appeals" and segments[2] == "evidence":
        if method == "POST":
            return _write_result(appeals.add_evidence(actor_id=actor_id, case_id=segments[1], **body))
        if method == "GET":
            items = [item.__dict__ for item in appeals.list_evidence(segments[1])]
            return 200, {"items": items}
    if len(segments) == 4 and segments[0] == "appeals" and segments[2] == "screenings":
        if method == "POST" and segments[3] == "check":
            return _write_result(appeals.screen_adjudicator(actor_id=actor_id, case_id=segments[1], **body))
    if method == "POST" and len(segments) == 4 and segments[0] == "appeals" \
            and segments[2] == "screening" and segments[3] == "complete":
        return _write_result(appeals.complete_screening(actor_id=actor_id, case_id=segments[1],
                                                        **body))
    if method == "POST" and len(segments) == 4 and segments[0] == "appeals" \
            and segments[2] == "assignment" and segments[3] == "assign":
        return _write_result(appeals.assign_handler(actor_id=actor_id, case_id=segments[1], **body))
    if method == "POST" and segments == ["leases", "reclaim"]:
        return 200, appeals.reclaim_expired_leases(actor_id=actor_id)
    if method == "POST" and len(segments) == 4 and segments[0] == "appeals" \
            and segments[2] == "evidence" and segments[3] == "requests":
        return _write_result(appeals.request_evidence(actor_id=actor_id, case_id=segments[1], **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "appeals" \
            and segments[2] == "recommendation":
        return _write_result(appeals.submit_recommendation(actor_id=actor_id, case_id=segments[1], **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "appeals" \
            and segments[2] == "withdraw":
        return _write_result(appeals.withdraw_appeal(actor_id=actor_id, case_id=segments[1],
                                                     **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "appeals" \
            and segments[2] == "reject":
        return _write_result(appeals.reject_appeal(actor_id=actor_id, case_id=segments[1], **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "appeals" \
            and segments[2] == "decision":
        return _write_result(appeals.decide_appeal(actor_id=actor_id, case_id=segments[1], **body))
    if method == "GET" and len(segments) == 2 and segments[0] == "appeals":
        case = appeals.get_case(segments[1])
        return 200, case.__dict__
    if method == "GET" and len(segments) == 3 and segments[0] == "appeals" and segments[2] == "snapshot":
        return 200, appeals.case_snapshot(actor_id=actor_id, case_id=segments[1])
    if method == "GET" and len(segments) == 3 and segments[0] == "appeals" and segments[2] == "timeline":
        return 200, appeals.case_timeline(actor_id=actor_id, case_id=segments[1])
    if method == "GET" and segments[:1] == ["public"] and len(segments) == 3 \
            and segments[1] == "appeals":
        result = appeals.public_result(segments[2])
        return 200, result.__dict__
    return None, {}


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
