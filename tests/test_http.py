"""HTTP 端到端测试：JSON API、照片 403/200、双线程 HTTP 接收竞争、超时提醒线程。"""
from __future__ import annotations

import base64
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

from millinspect.app import make_handler, App
from millinspect.service import MillService
from tests.helpers import PNG_1


def _png_data_uri() -> str:
    return base64.b64encode(PNG_1).decode()


class HttpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mill-http-")
        self.svc = MillService(f"{self.tmp}/mill.db", self.tmp)
        self.svc.add_loom("L-01", "1 号织机", "甲班车间")
        self.svc.add_color_batch("CB-01", "亚麻原色", "D-1")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(self.svc)))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.svc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def request(self, path: str, body=None, method: str | None = None, raw: bool = False):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method or ("POST" if body is not None else "GET"))
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if raw:
                    return resp.status, payload, ctype
                return resp.status, json.loads(payload)
        except urllib.error.HTTPError as e:
            payload = e.read()
            if raw:
                return e.code, payload, e.headers.get("Content-Type", "")
            try:
                return e.code, json.loads(payload)
            except json.JSONDecodeError:
                return e.code, {"error": payload.decode()}

    def test_meta_and_static(self):
        status, meta = self.request("/api/meta")
        self.assertEqual(status, 200)
        self.assertIn("defect_types", meta)
        status, html, ctype = self.request("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("布卷疵点复检", html.decode())

    def test_full_flow_api(self):
        # 入检
        s, r = self.request("/api/bolts", {
            "code": "B-100", "length_m": 120, "nominal_length_m": 120,
            "width_m": 1.5, "color_batch": "CB-01", "loom_code": "L-01"})
        self.assertEqual(s, 201)
        # 开始初检
        s, r = self.request("/api/bolts/B-100/inspection", {"inspector": "王检"})
        self.assertEqual(s, 201)
        vid = r["version_id"]
        # 检验段越界 -> 400
        s, r = self.request(f"/api/versions/{vid}/segments", {
            "seq": 1, "start_m": 0, "end_m": 130, "inspector": "王检"})
        self.assertEqual(s, 400)
        # 正常段
        s, r = self.request(f"/api/versions/{vid}/segments", {
            "seq": 1, "start_m": 0, "end_m": 40, "inspector": "王检"})
        sid = r["segment_id"]
        # 无照片疵点 -> 400
        s, r = self.request(f"/api/segments/{sid}/defects", {
            "type_code": "stain", "position_m": 10})
        self.assertEqual(s, 400)
        # 带照片
        s, r = self.request(f"/api/segments/{sid}/defects", {
            "type_code": "stain", "position_m": 10, "description": "边部油污",
            "photo_data": _png_data_uri(), "photo_mime": "image/png"})
        self.assertEqual(s, 201)
        did = r["defect_id"]
        return vid, sid, did

    def test_photo_403_then_200_then_403_after_complete(self):
        _, sid, did = self.test_full_flow_api()
        # 派发 -> 初检人自己接收 -> 400（复检人不能相同）
        s, r = self.request("/api/queue", {
            "bolt_code": "B-100", "segment_id": sid, "trigger_code": "stain"})
        self.assertEqual(s, 201)
        task_id = r["task_id"]
        s, r = self.request(f"/api/tasks/{task_id}/claim", {"assignee": "王检"})
        self.assertEqual(s, 400)
        # 另一名复检员接收
        s, _ = self.request(f"/api/tasks/{task_id}/claim", {"assignee": "李复"})
        self.assertEqual(s, 200)
        # 无令牌访问照片 -> 403
        s, _, _ = self.request("/api/photo?token=junk", raw=True)
        self.assertEqual(s, 403)
        # 非接收人申请令牌 -> 403
        s, _ = self.request(f"/api/tasks/{task_id}/photo-grant",
                            {"defect_id": did, "assignee": "孙复"})
        self.assertEqual(s, 403)
        # 接收人申请 -> 200，带令牌读图 -> 200
        s, r = self.request(f"/api/tasks/{task_id}/photo-grant",
                            {"defect_id": did, "assignee": "李复"})
        self.assertEqual(s, 200)
        url = r["url"]
        s, payload, ctype = self.request(url, raw=True)
        self.assertEqual(s, 200)
        self.assertEqual(ctype, "image/png")
        self.assertEqual(payload, PNG_1)
        # 完成复检后再次读图 -> 403（权限收回）
        s, _ = self.request(f"/api/tasks/{task_id}/complete",
                            {"assignee": "李复", "result": "cleared", "note": "无碍"})
        self.assertEqual(s, 200)
        s, _, _ = self.request(url, raw=True)
        self.assertEqual(s, 403)

    def test_concurrent_http_claims_one_wins(self):
        _, sid = self._bolt_two_segments("B-200")
        statuses = []
        t1 = self.request("/api/queue", {
            "bolt_code": "B-200", "segment_id": sid[0], "trigger_code": "stain"})[1]["task_id"]
        t2 = self.request("/api/queue", {
            "bolt_code": "B-200", "segment_id": sid[1], "trigger_code": "color_diff"})[1]["task_id"]
        barrier = threading.Barrier(2)

        def claim(task_id, who):
            barrier.wait()
            status, _ = self.request(f"/api/tasks/{task_id}/claim", {"assignee": who})
            statuses.append(status)

        th1 = threading.Thread(target=claim, args=(t1, "李复"))
        th2 = threading.Thread(target=claim, args=(t2, "孙复"))
        th1.start(); th2.start(); th1.join(10); th2.join(10)
        self.assertEqual(sorted(statuses), [200, 409], statuses)
        _, tasks = self.request("/api/queue")
        claimed = [t for t in tasks if t["bolt_code"] == "B-200" and t["status"] == "claimed"]
        self.assertEqual(len(claimed), 1)

    def _bolt_two_segments(self, code: str):
        self.request("/api/bolts", {
            "code": code, "length_m": 120, "nominal_length_m": 120,
            "width_m": 1.5, "color_batch": "CB-01", "loom_code": "L-01"})
        _, r = self.request(f"/api/bolts/{code}/inspection", {"inspector": "王检"})
        vid = r["version_id"]
        sids = []
        for i, (a, b) in enumerate([(0, 60), (60, 120)], start=1):
            _, r = self.request(f"/api/versions/{vid}/segments",
                                {"seq": i, "start_m": a, "end_m": b, "inspector": "王检"})
            sids.append(r["segment_id"])
        for sid, dtype, pos in [(sids[0], "stain", 10), (sids[1], "color_diff", 70)]:
            self.request(f"/api/segments/{sid}/defects", {
                "type_code": dtype, "position_m": pos,
                "photo_data": _png_data_uri(), "photo_mime": "image/png"})
        return vid, sids

    def test_released_bolt_rejects_rewrite(self):
        self.make_clean_released()
        # 放行后尝试直接再做初检 -> 409
        s, _ = self.request("/api/bolts/B-300/inspection", {"inspector": "王检"})
        self.assertEqual(s, 409)

    def make_clean_released(self):
        self.request("/api/bolts", {
            "code": "B-300", "length_m": 120, "nominal_length_m": 120,
            "width_m": 1.5, "color_batch": "CB-01", "loom_code": "L-01"})
        _, r = self.request("/api/bolts/B-300/inspection", {"inspector": "王检"})
        vid = r["version_id"]
        self.request(f"/api/versions/{vid}/segments",
                     {"seq": 1, "start_m": 0, "end_m": 120, "inspector": "王检"})
        s, r = self.request("/api/releases", {"bolt_code": "B-300", "released_by": "钱班"})
        self.assertEqual(s, 201)

    def test_sweeper_background_marks_timeout(self):
        # 启动真实后台巡检线程（间隔很短）
        self.svc.start_sweeper(interval_seconds=0.2)
        _, sid = self._bolt_two_segments("B-400")
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)  # 截止时间必然早于真实当前时间
        _, r = self.request("/api/queue", {
            "bolt_code": "B-400", "segment_id": sid[0], "trigger_code": "stain",
            "timeout_seconds": 1})
        task_id = r["task_id"]
        # 直接把 due_at 调回过去以模拟超时
        self.svc.conn.execute(
            "UPDATE reinspection_tasks SET due_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", task_id))
        self.svc.conn.commit()
        deadline = datetime.now(timezone.utc).timestamp() + 5
        status = "queued"
        while datetime.now(timezone.utc).timestamp() < deadline:
            _, tasks = self.request("/api/queue")
            task = next(t for t in tasks if t["id"] == task_id)
            status = task["status"]
            if status == "timeout":
                break
            threading.Event().wait(0.25)
        self.assertEqual(status, "timeout")
