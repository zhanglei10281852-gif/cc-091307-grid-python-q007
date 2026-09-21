"""HTTP API 层（Python 标准库实现，无外部依赖）。

认证约定（生产环境由网关注入，此处以请求头模拟）：
    X-Operator-Id     操作人标识，如 grid-east-01
    X-Operator-Role   grid（网格员）/ admin（管理端）
    X-Operator-Zones  网格员负责片区，逗号分隔，如 "城东片区,城西片区"
    Idempotency-Key   幂等键（POST 类接口），重复提交返回首次结果
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .errors import ServiceError
from .service import HousingSafetyService, Operator


def _without(body: dict, *keys) -> dict:
    return {k: v for k, v in (body or {}).items() if k not in keys}


class ApiApp:
    """路由分发：HTTP 语义 ↔ 领域服务调用，统一错误映射。"""

    def __init__(self, service: HousingSafetyService):
        self.service = service

    def dispatch(self, method, path, query, body, operator, idem_key):
        svc = self.service
        routes = (
            ("GET", r"/api/health",
             lambda g: {"status": "ok", "service": "housing-safety-registry"}),
            ("POST", r"/api/properties",
             lambda g: svc.register_property(body, operator, idem_key)),
            ("GET", r"/api/properties",
             lambda g: {"items": svc.list_properties(operator, zone=query.get("zone"))}),
            ("GET", r"/api/properties/([^/]+)",
             lambda g: svc.get_property(g[0], operator)),
            ("PUT", r"/api/properties/([^/]+)",
             lambda g: svc.update_property(g[0], _without(body, "expected_version"),
                                           body.get("expected_version"), operator)),
            ("POST", r"/api/properties/([^/]+)/units",
             lambda g: svc.register_unit(g[0], body, operator, idem_key)),
            ("POST", r"/api/properties/([^/]+)/parties",
             lambda g: svc.register_party(g[0], body, operator, idem_key)),
            ("GET", r"/api/properties/([^/]+)/history",
             lambda g: svc.property_history(g[0], operator)),
            ("PUT", r"/api/units/([^/]+)",
             lambda g: svc.update_unit(g[0], _without(body, "expected_version"),
                                       body.get("expected_version"), operator)),
            ("PUT", r"/api/parties/([^/]+)",
             lambda g: svc.update_party(g[0], _without(body, "expected_version"),
                                        body.get("expected_version"), operator)),
            ("POST", r"/api/inspections",
             lambda g: svc.create_inspection(body, operator, idem_key)),
            ("GET", r"/api/inspections",
             lambda g: {"items": svc.list_inspections(
                 operator, property_id=query.get("property_id"), status=query.get("status"))}),
            ("GET", r"/api/inspections/([^/]+)",
             lambda g: svc.get_inspection(g[0], operator)),
            ("POST", r"/api/inspections/([^/]+)/rectify",
             lambda g: svc.rectify_inspection(g[0], body.get("deadline"),
                                              body.get("expected_version"), operator)),
            ("POST", r"/api/inspections/([^/]+)/close",
             lambda g: svc.close_inspection(g[0], body.get("review_note"),
                                            body.get("expected_version"), operator)),
            ("GET", r"/api/admin/risk-overview",
             lambda g: svc.risk_overview(operator)),
        )
        for route_method, pattern, handler in routes:
            if route_method != method:
                continue
            match = re.fullmatch(pattern, path)
            if match is None:
                continue
            try:
                return 200, handler(match.groups())
            except ServiceError as exc:
                return exc.status, exc.to_dict()
            except Exception as exc:  # noqa: BLE001 —— 兜底，避免连接被异常截断
                return 500, {"error": {"code": "INTERNAL_ERROR", "message": str(exc)}}
        return 404, {"error": {"code": "NOT_FOUND", "message": f"接口不存在: {method} {path}"}}


class RequestHandler(BaseHTTPRequestHandler):
    app: ApiApp = None  # 由 make_server 注入
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def log_message(self, *args):  # 保持测试输出干净
        pass

    def _handle(self, method):
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body = {}
        if method in ("POST", "PUT"):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send_json(400, {"error": {"code": "BAD_JSON",
                                                    "message": "请求体不是合法 JSON"}})
                    return
                if not isinstance(body, dict):
                    self._send_json(400, {"error": {"code": "BAD_JSON",
                                                    "message": "请求体须为 JSON 对象"}})
                    return
        operator = Operator(
            self.headers.get("X-Operator-Id") or "anonymous",
            self.headers.get("X-Operator-Role") or "grid",
            # 片区名含中文，按 URL 编码传输（HTTP 头仅支持 latin-1）
            [unquote(z.strip()) for z in (self.headers.get("X-Operator-Zones") or "").split(",")
             if z.strip()],
        )
        idem_key = self.headers.get("Idempotency-Key")
        status, payload = self.app.dispatch(method, parsed.path, query, body, operator, idem_key)
        self._send_json(status, payload)

    def _send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(service: HousingSafetyService, host="127.0.0.1", port=8000) -> ThreadingHTTPServer:
    app = ApiApp(service)
    handler_cls = type("BoundRequestHandler", (RequestHandler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler_cls)


def run(host="0.0.0.0", port=8000, db_path=None):
    import os

    db_path = db_path or os.environ.get("HOUSING_DB_PATH", "data/housing.db")
    service = HousingSafetyService(db_path)
    server = make_server(service, host, port)
    print(f"房屋安全登记服务已启动: http://{host}:{port}  (db={db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
