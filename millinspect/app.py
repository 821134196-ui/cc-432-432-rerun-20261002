"""HTTP 服务：JSON API + 静态前端，纯标准库实现。

启动::

    python -m millinspect.app            # 监听 127.0.0.1:8000
    python -m millinspect.app --seed     # 播种演示数据
    python -m millinspect.app --port 8080 --reset

单条本地启动命令见仓库根目录 ``run.sh``（自动播种并启动）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import demo
from .domain import RELEASE_RULES
from .service import (
    REPAIR_STATUS_NAMES,
    STATUS_NAMES,
    TASK_STATUS_NAMES,
    Conflict,
    DomainError,
    MillService,
    NotFound,
    PermissionDenied,
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

STAFF = {
    "initial_inspectors": ["王检", "赵检"],
    "reinspectors": ["李复", "孙复"],
    "repairers": ["周修"],
    "releasers": ["钱班"],
}

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class App:
    def __init__(self, svc: MillService):
        self.svc = svc


def _b64_photo(body: dict) -> tuple[bytes | None, str]:
    if not body.get("photo_data"):
        return None, "image/png"
    mime = body.get("photo_mime", "image/png")
    return base64.b64decode(body["photo_data"]), mime


def make_handler(app: App):
    svc = app.svc

    class Handler(BaseHTTPRequestHandler):
        server_version = "MillInspect/1.0"

        def log_message(self, fmt, *args):  # 简洁日志
            sys.stderr.write("[http] %s\n" % (fmt % args,))

        # ------------------------------------------------------------ 工具

        def _send_json(self, obj, status: int = 200) -> None:
            data = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DomainError(f"请求体不是合法 JSON: {exc}") from exc

        def _error(self, exc: Exception) -> None:
            status = HTTPStatus.BAD_REQUEST
            if isinstance(exc, NotFound):
                status = HTTPStatus.NOT_FOUND
            elif isinstance(exc, Conflict):
                status = HTTPStatus.CONFLICT
            elif isinstance(exc, PermissionDenied):
                status = HTTPStatus.FORBIDDEN
            elif isinstance(exc, DomainError):
                status = HTTPStatus.BAD_REQUEST
            self._send_json({"error": str(exc)}, status)

        # ------------------------------------------------------------ GET

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                path, qs = parsed.path, parse_qs(parsed.query)
                if path.startswith("/api/"):
                    self._api_get(path, qs)
                else:
                    self._static(path)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, DomainError):
                    self._error(exc)
                else:
                    traceback.print_exc()
                    self._send_json({"error": f"服务器内部错误: {exc}"}, 500)

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._api_post(urlparse(self.path).path)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, DomainError):
                    self._error(exc)
                else:
                    traceback.print_exc()
                    self._send_json({"error": f"服务器内部错误: {exc}"}, 500)

        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = os.path.realpath(os.path.join(STATIC_DIR, rel))
            root = os.path.realpath(STATIC_DIR)
            if os.path.commonpath([root, target]) != root or not os.path.isfile(target):
                self.send_error(404)
                return
            ext = os.path.splitext(target)[1]
            with open(target, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", _MIME.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _api_get(self, path: str, qs: dict) -> None:
            if path == "/api/meta":
                self._send_json({
                    "staff": STAFF,
                    "looms": [dict(r) for r in svc.list_looms()],
                    "color_batches": [dict(r) for r in svc.list_color_batches()],
                    "defect_types": [dict(r) for r in svc.list_defect_types()],
                    "release_rules": RELEASE_RULES,
                    "status_names": STATUS_NAMES,
                    "task_status_names": TASK_STATUS_NAMES,
                    "repair_status_names": REPAIR_STATUS_NAMES,
                })
            elif path == "/api/bolts":
                self._send_json(svc.list_bolts())
            elif path.startswith("/api/bolts/"):
                self._send_json(svc.bolt_detail(path.rsplit("/", 1)[-1]))
            elif path == "/api/queue":
                self._send_json(svc.queue())
            elif path == "/api/repairs":
                self._send_json(svc.repairs())
            elif path == "/api/releases":
                self._send_json(svc.releases())
            elif path == "/api/release-check":
                code = qs.get("bolt", [""])[0]
                d = svc.evaluate_release(code)
                self._send_json({"allowed": d.allowed, "grade": d.grade, "reasons": d.reasons})
            elif path == "/api/photo":
                token = qs.get("token", [""])[0]
                data, mime = svc.read_photo(token)
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send_json({"error": "未知接口"}, 404)

        # ----------------------------------------------------------- POST

        def _api_post(self, path: str) -> None:
            body = self._read_json()
            parts = [p for p in path.split("/") if p]

            if path == "/api/bolts":
                bid = svc.create_bolt(
                    body["code"], float(body["length_m"]), float(body["width_m"]),
                    body["color_batch"], body["loom_code"],
                    float(body["nominal_length_m"]) if body.get("nominal_length_m") else None,
                )
                self._send_json({"id": bid}, 201)
            elif len(parts) == 4 and parts[:2] == ["api", "bolts"] and parts[3] == "inspection":
                vid = svc.start_inspection(parts[2], body["inspector"])
                self._send_json({"version_id": vid}, 201)
            elif len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "segments":
                sid = svc.add_segment(
                    int(parts[2]), int(body["seq"]),
                    float(body["start_m"]), float(body["end_m"]), body["inspector"],
                )
                self._send_json({"segment_id": sid}, 201)
            elif len(parts) == 4 and parts[:2] == ["api", "segments"] and parts[3] == "defects":
                photo, mime = _b64_photo(body)
                did = svc.add_defect(
                    int(parts[2]), body["type_code"], float(body["position_m"]),
                    body.get("description", ""), photo, mime,
                )
                self._send_json({"defect_id": did}, 201)
            elif len(parts) == 4 and parts[:2] == ["api", "defects"] and parts[3] == "photo":
                photo, mime = _b64_photo(body)
                if not photo:
                    raise DomainError("缺少照片数据")
                svc.attach_photo(int(parts[2]), photo, mime)
                self._send_json({"ok": True})
            elif path == "/api/queue":
                tid = svc.enqueue_reinspection(
                    body["bolt_code"], int(body["segment_id"]), body["trigger_code"],
                    float(body.get("timeout_seconds", 24 * 3600)),
                )
                self._send_json({"task_id": tid}, 201)
            elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "claim":
                svc.claim_task(int(parts[2]), body["assignee"])
                self._send_json({"ok": True})
            elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "complete":
                svc.complete_task(
                    int(parts[2]), body["assignee"], body["result"], body.get("note", "")
                )
                self._send_json({"ok": True})
            elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "photo-grant":
                token = svc.grant_photo(int(body["defect_id"]), int(parts[2]), body["assignee"])
                self._send_json({"token": token, "url": f"/api/photo?token={token}"})
            elif path == "/api/repairs":
                oid = svc.open_repair(body["bolt_code"], body["reason"])
                self._send_json({"order_id": oid}, 201)
            elif len(parts) == 4 and parts[:2] == ["api", "repairs"] and parts[3] == "start":
                svc.start_repair(int(parts[2]))
                self._send_json({"ok": True})
            elif len(parts) == 4 and parts[:2] == ["api", "repairs"] and parts[3] == "finish":
                vid = svc.finish_repair(int(parts[2]), body["inspector"])
                self._send_json({"new_version_id": vid})
            elif path == "/api/releases":
                rid = svc.release(body["bolt_code"], body["released_by"], body.get("note", ""))
                self._send_json({"release_id": rid}, 201)
            elif path == "/api/sweep-timeouts":
                self._send_json({"timed_out": svc._sweep_timeouts()})
            else:
                self._send_json({"error": "未知接口"}, 404)

    return Handler


def build(data_dir: str, reset: bool = False, seed: bool = False) -> tuple[MillService, App]:
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "millinspect.db")
    if reset and os.path.exists(db_path):
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
    svc = MillService(db_path, data_dir)
    if seed:
        demo.seed(svc)
    svc.start_sweeper(interval_seconds=3.0)
    return svc, App(svc)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="纺织厂布卷疵点复检与退修系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data-dir", default=os.path.join(os.getcwd(), "data"))
    ap.add_argument("--seed", action="store_true", help="播种演示数据（空库时）")
    ap.add_argument("--reset", action="store_true", help="启动前删除数据库")
    args = ap.parse_args(argv)

    svc, app = build(args.data_dir, reset=args.reset, seed=args.seed)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"布卷疵点复检与退修系统已启动: http://{args.host}:{args.port}")
    print(f"数据目录: {args.data_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        svc.close()


if __name__ == "__main__":
    main()
