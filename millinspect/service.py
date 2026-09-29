"""领域服务：布卷、检验版本、复检任务、退修工单与放行的全部业务规则。

时间统一使用 UTC ISO 字符串；所有写方法支持注入 ``now`` 便于测试。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from . import photos as photos_mod
from .db import connect, init_db, tx
from .domain import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
    DEFECT_TYPES,
    PHOTO_GRANT_TTL_SECONDS,
    SHORT_LENGTH_RATIO,
    Conflict,
    DomainError,
    NotFound,
    PermissionDenied,
    ReleaseDecision,
    worst_grade,
)

STATUS_NAMES = {
    "pending": "待检",
    "inspecting": "初检中",
    "reinspecting": "复检中",
    "repairing": "退修中",
    "released": "已放行",
    "rejected": "已拒收",
}

TASK_STATUS_NAMES = {
    "queued": "待接收",
    "claimed": "复检中",
    "completed": "已完成",
    "timeout": "已超时",
    "cancelled": "已取消",
}

REPAIR_STATUS_NAMES = {
    "open": "待开工",
    "in_progress": "退修中",
    "done": "已完成",
    "cancelled": "已取消",
}

_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "bin": "application/octet-stream"}
_MIME_EXT = {v: k for k, v in _EXT_MIME.items() if k != "bin"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class MillService:
    def __init__(self, db_path: str, data_dir: str):
        self.db_path = db_path
        self.data_dir = data_dir
        self.conn = connect(db_path)
        init_db(self.conn)
        self._seed_defect_types()
        self._sweeper_stop: threading.Event | None = None
        self._sweeper_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 基础

    def _seed_defect_types(self) -> None:
        for code, (name, sev) in DEFECT_TYPES.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO defect_types(code, name, severity) VALUES (?,?,?)",
                (code, name, sev),
            )
        self.conn.commit()

    def add_loom(self, code: str, name: str, workshop: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO looms(code, name, workshop) VALUES (?,?,?)",
            (code, name, workshop),
        )
        self.conn.commit()

    def add_color_batch(self, code: str, color_name: str, dye_lot: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO color_batches(code, color_name, dye_lot) VALUES (?,?,?)",
            (code, color_name, dye_lot),
        )
        self.conn.commit()

    def list_looms(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM looms ORDER BY code"))

    def list_color_batches(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM color_batches ORDER BY code"))

    def list_defect_types(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM defect_types ORDER BY severity, code"))

    # ------------------------------------------------------------------ 布卷

    def create_bolt(
        self,
        code: str,
        length_m: float,
        width_m: float,
        color_batch: str,
        loom_code: str,
        nominal_length_m: float | None = None,
        now: datetime | None = None,
    ) -> int:
        now = now or utcnow()
        try:
            cur = self.conn.execute(
                """INSERT INTO bolts(code, length_m, width_m, color_batch, loom_code,
                                     received_at, status, nominal_length_m)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (code, length_m, width_m, color_batch, loom_code, iso(now), "pending", nominal_length_m),
            )
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise DomainError(f"布卷编号重复或基础数据不存在: {code}") from exc

    def _bolt_row(self, code: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM bolts WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFound(f"布卷不存在: {code}")
        return row

    def list_bolts(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT b.*, cb.color_name, cb.code AS color_batch_code,
                      l.name AS loom_name, r.grade AS released_grade, r.id AS release_id
               FROM bolts b
               JOIN color_batches cb ON cb.code = b.color_batch
               JOIN looms l ON l.code = b.loom_code
               LEFT JOIN releases r ON r.id = (
                   SELECT id FROM releases WHERE bolt_code=b.code ORDER BY id DESC LIMIT 1
               )
               ORDER BY b.received_at"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["status_name"] = STATUS_NAMES[r["status"]]
            d["defect_count"] = self.conn.execute(
                """SELECT COUNT(*) FROM defects df
                   JOIN segments s ON s.id=df.segment_id
                   JOIN inspection_versions v ON v.id=s.version_id
                   WHERE v.bolt_code=? AND v.id=?""",
                (r["code"], r["current_version"]),
            ).fetchone()[0] if r["current_version"] else 0
            out.append(d)
        return out

    def bolt_detail(self, code: str) -> dict:
        bolt = dict(self._bolt_row(code))
        bolt["status_name"] = STATUS_NAMES[bolt["status"]]
        versions = []
        for v in self.conn.execute(
            "SELECT * FROM inspection_versions WHERE bolt_code=? ORDER BY version_no", (code,)
        ):
            vd = dict(v)
            vd["segments"] = self._version_segments(v["id"])
            versions.append(vd)
        bolt["versions"] = versions
        bolt["tasks"] = [dict(r) for r in self.conn.execute(
            """SELECT t.*, s.start_m, s.end_m, dt.name AS trigger_name
               FROM reinspection_tasks t
               JOIN segments s ON s.id=t.segment_id
               JOIN defect_types dt ON dt.code=t.trigger_code
               WHERE t.bolt_code=? ORDER BY t.id""",
            (code,),
        )]
        bolt["repairs"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM repair_orders WHERE bolt_code=? ORDER BY id", (code,)
        )]
        bolt["releases"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM releases WHERE bolt_code=? ORDER BY id", (code,)
        )]
        return bolt

    def _version_segments(self, version_id: int) -> list[dict]:
        segs = []
        for s in self.conn.execute(
            "SELECT * FROM segments WHERE version_id=? ORDER BY seq", (version_id,)
        ):
            sd = dict(s)
            sd["defects"] = [dict(d) for d in self.conn.execute(
                """SELECT d.*, dt.name AS type_name, dt.severity
                   FROM defects d JOIN defect_types dt ON dt.code=d.type_code
                   WHERE d.segment_id=? ORDER BY d.position_m""",
                (s["id"],),
            )]
            segs.append(sd)
        return segs

    # ------------------------------------------------------------- 初检/版本

    def start_inspection(self, bolt_code: str, inspector: str, now: datetime | None = None) -> int:
        """开始初检，创建 V1。只有待检布卷可以开始。"""
        now = now or utcnow()
        bolt = self._bolt_row(bolt_code)
        if bolt["status"] != "pending":
            raise Conflict(f"布卷 {bolt_code} 当前为{STATUS_NAMES[bolt['status']]}，不能开始初检")
        with tx(self.conn):
            cur = self.conn.execute(
                """INSERT INTO inspection_versions(bolt_code, version_no, kind, inspector, created_at)
                   VALUES (?,1,'initial',?,?)""",
                (bolt_code, inspector, iso(now)),
            )
            vid = cur.lastrowid
            self.conn.execute(
                "UPDATE bolts SET status='inspecting', current_version=? WHERE code=?",
                (vid, bolt_code),
            )
        return vid

    def _version_row(self, version_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM inspection_versions WHERE id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"检验版本不存在: {version_id}")
        return row

    def add_segment(
        self,
        version_id: int,
        seq: int,
        start_m: float,
        end_m: float,
        inspector: str,
        now: datetime | None = None,
    ) -> int:
        """登记检验段。校验：区间正向、不越出布卷长度、同版本内不重叠。"""
        now = now or utcnow()
        v = self._version_row(version_id)
        bolt = self._bolt_row(v["bolt_code"])
        if start_m < 0:
            raise DomainError("检验段起点不能为负")
        if end_m <= start_m:
            raise DomainError(f"检验段区间无效: [{start_m}, {end_m}]，结束米数必须大于起点")
        if end_m > bolt["length_m"] + 1e-9:
            raise DomainError(
                f"检验段结束米数 {end_m} 超出布卷长度 {bolt['length_m']}"
            )
        for s in self.conn.execute(
            "SELECT start_m, end_m FROM segments WHERE version_id=?", (version_id,)
        ):
            if start_m < s["end_m"] - 1e-9 and end_m > s["start_m"] + 1e-9:
                raise DomainError(
                    f"检验段 [{start_m},{end_m}] 与已有区间 [{s['start_m']},{s['end_m']}] 重叠"
                )
        try:
            cur = self.conn.execute(
                """INSERT INTO segments(version_id, seq, start_m, end_m, inspector, checked_at)
                   VALUES (?,?,?,?,?,?)""",
                (version_id, seq, start_m, end_m, inspector, iso(now)),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DomainError(f"段序号 {seq} 已存在") from exc
        return cur.lastrowid

    def add_defect(
        self,
        segment_id: int,
        type_code: str,
        position_m: float,
        description: str = "",
        photo_bytes: bytes | None = None,
        photo_mime: str = "image/png",
        now: datetime | None = None,
    ) -> int:
        """在检验段上按米数登记疵点并附照片。

        - 位置必须落在检验段区间内；
        - 普通疵点必须附照片；“照片缺失”类型不允许带照片，须事后补照；
        - 布卷已放行后不得再向当前版本追加疵点。
        """
        now = now or utcnow()
        seg = self.conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
        if seg is None:
            raise NotFound(f"检验段不存在: {segment_id}")
        v = self._version_row(seg["version_id"])
        bolt = self._bolt_row(v["bolt_code"])
        if bolt["status"] == "released":
            raise Conflict("布卷已放行，不能再登记疵点")
        if bolt["status"] == "repairing":
            raise Conflict("布卷退修中，旧版本冻结，不能再登记疵点")
        if type_code not in DEFECT_TYPES:
            raise DomainError(f"未知疵点类型: {type_code}")
        if not (seg["start_m"] - 1e-9 <= position_m <= seg["end_m"] + 1e-9):
            raise DomainError(
                f"疵点位置 {position_m}m 不在检验段 [{seg['start_m']},{seg['end_m']}] 内"
            )
        if type_code == "missing_photo" and photo_bytes is not None:
            raise DomainError("“照片缺失”登记时不能携带照片，请登记后补照")
        if type_code != "missing_photo" and not photo_bytes:
            raise DomainError("疵点必须附照片；如确无照片，请登记“照片缺失”")

        cur = self.conn.execute(
            """INSERT INTO defects(segment_id, type_code, position_m, description, recorded_at)
               VALUES (?,?,?,?,?)""",
            (segment_id, type_code, position_m, description, iso(now)),
        )
        defect_id = cur.lastrowid
        rel_path = None
        if photo_bytes:
            ext = _MIME_EXT.get(photo_mime, "bin")
            rel_path = photos_mod.save_photo(self.data_dir, defect_id, photo_bytes, ext)
            self.conn.execute("UPDATE defects SET photo_path=? WHERE id=?", (rel_path, defect_id))
        self.conn.commit()
        return defect_id

    def attach_photo(
        self,
        defect_id: int,
        photo_bytes: bytes,
        photo_mime: str = "image/png",
        now: datetime | None = None,
    ) -> None:
        """为“照片缺失”疵点补照；补照后该 C 级问题视为已解决。"""
        now = now or utcnow()
        d = self.conn.execute("SELECT * FROM defects WHERE id=?", (defect_id,)).fetchone()
        if d is None:
            raise NotFound(f"疵点不存在: {defect_id}")
        seg = self.conn.execute("SELECT * FROM segments WHERE id=?", (d["segment_id"],)).fetchone()
        v = self._version_row(seg["version_id"])
        bolt = self._bolt_row(v["bolt_code"])
        if bolt["status"] == "released":
            raise Conflict("布卷已放行，不能补照")
        if bolt["status"] == "repairing":
            raise Conflict("布卷退修中，旧版本冻结，不能补照")
        if d["type_code"] != "missing_photo":
            raise DomainError("仅“照片缺失”疵点需要补照")
        if d["resolved_at"]:
            raise Conflict("该疵点已补照")
        ext = _MIME_EXT.get(photo_mime, "bin")
        rel_path = photos_mod.save_photo(self.data_dir, defect_id, photo_bytes, ext)
        self.conn.execute(
            "UPDATE defects SET photo_path=?, resolved_at=? WHERE id=?",
            (rel_path, iso(now), defect_id),
        )
        self.conn.commit()

    # ----------------------------------------------------------------- 复检

    def enqueue_reinspection(
        self,
        bolt_code: str,
        segment_id: int,
        trigger_code: str,
        timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
        now: datetime | None = None,
    ) -> int:
        """派发复检任务。复检只能引用原检验段（当前版本上的段）。"""
        now = now or utcnow()
        bolt = self._bolt_row(bolt_code)
        if bolt["status"] == "released":
            raise Conflict("布卷已放行，不能派发复检")
        if bolt["status"] == "repairing":
            raise Conflict("布卷退修中，旧版本已冻结，待退修完成生成新版本后再派发复检")
        if bolt["status"] not in ("inspecting", "reinspecting"):
            raise Conflict(f"布卷当前为{STATUS_NAMES[bolt['status']]}，不能派发复检")
        seg = self.conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
        if seg is None:
            raise NotFound(f"检验段不存在: {segment_id}")
        v = self._version_row(seg["version_id"])
        if v["bolt_code"] != bolt_code:
            raise DomainError("复检段不属于该布卷")
        if bolt["current_version"] != v["id"]:
            raise Conflict("复检只能引用当前版本的原检验段（旧版本已被退修新版本取代）")
        if trigger_code not in DEFECT_TYPES:
            raise DomainError(f"未知触发类型: {trigger_code}")
        # 同一段未结束的任务不重复派发
        dup = self.conn.execute(
            """SELECT id FROM reinspection_tasks
               WHERE segment_id=? AND status IN ('queued','claimed')""",
            (segment_id,),
        ).fetchone()
        if dup:
            raise Conflict("该检验段已有进行中的复检任务")
        due = now + timedelta(seconds=timeout_seconds)
        with tx(self.conn):
            cur = self.conn.execute(
                """INSERT INTO reinspection_tasks
                       (bolt_code, version_id, segment_id, trigger_code, status, due_at)
                   VALUES (?,?,?,?,'queued',?)""",
                (bolt_code, v["id"], segment_id, trigger_code, iso(due)),
            )
            task_id = cur.lastrowid
            if bolt["status"] == "inspecting":
                self.conn.execute(
                    "UPDATE bolts SET status='reinspecting' WHERE code=?", (bolt_code,)
                )
        return task_id

    def claim_task(self, task_id: int, assignee: str, now: datetime | None = None) -> None:
        """接收复检任务。

        - 复检人不能与该段初检人相同；
        - 同一布卷同一时刻只允许一条任务被接收（原子更新，线程竞争只有一条成功）。
        """
        now = now or utcnow()
        with tx(self.conn):
            task = self.conn.execute(
                "SELECT * FROM reinspection_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"复检任务不存在: {task_id}")
            if task["status"] != "queued":
                raise Conflict(
                    f"任务已被接收或已结束（{TASK_STATUS_NAMES[task['status']]}）"
                )
            seg = self.conn.execute(
                "SELECT * FROM segments WHERE id=?", (task["segment_id"],)
            ).fetchone()
            if seg["inspector"] == assignee:
                raise DomainError("复检人与初检人不能相同")
            active = self.conn.execute(
                "SELECT id FROM reinspection_tasks WHERE bolt_code=? AND status='claimed'",
                (task["bolt_code"],),
            ).fetchone()
            if active:
                raise Conflict(f"布卷 {task['bolt_code']} 已有一条复检任务被接收")
            cur = self.conn.execute(
                """UPDATE reinspection_tasks
                       SET status='claimed', assignee=?, claimed_at=?
                   WHERE id=? AND status='queued'""",
                (assignee, iso(now), task_id),
            )
            if cur.rowcount == 0:  # 并发下被另一条线抢先
                raise Conflict("复检任务已被另一条复检线接收")
            self.conn.execute(
                "UPDATE bolts SET status='reinspecting' WHERE code=?", (task["bolt_code"],)
            )

    def complete_task(
        self,
        task_id: int,
        assignee: str,
        result: str,
        note: str = "",
        now: datetime | None = None,
    ) -> None:
        """完成复检：confirmed=问题确认（需退修/折价处理），cleared=不影响使用。"""
        now = now or utcnow()
        if result not in ("confirmed", "cleared"):
            raise DomainError("复检结果必须是 confirmed 或 cleared")
        with tx(self.conn):
            cur = self.conn.execute(
                """UPDATE reinspection_tasks
                       SET status='completed', completed_at=?, result=?, note=?
                   WHERE id=? AND status='claimed' AND assignee=?""",
                (iso(now), result, note, task_id, assignee),
            )
            if cur.rowcount == 0:
                task = self.conn.execute(
                    "SELECT * FROM reinspection_tasks WHERE id=?", (task_id,)
                ).fetchone()
                if task is None:
                    raise NotFound(f"复检任务不存在: {task_id}")
                if task["status"] == "timeout":
                    raise Conflict("复检任务已超时，请重新派发")
                if task["assignee"] != assignee:
                    raise PermissionDenied("只有接收人可以完成该任务")
                raise Conflict(f"任务当前状态为{TASK_STATUS_NAMES[task['status']]}，无法完成")

    def queue(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT t.*, s.start_m, s.end_m, s.inspector AS initial_inspector,
                      dt.name AS trigger_name, dt.severity,
                      iv.version_no, iv.kind AS version_kind
               FROM reinspection_tasks t
               JOIN segments s ON s.id=t.segment_id
               JOIN inspection_versions iv ON iv.id=t.version_id
               JOIN defect_types dt ON dt.code=t.trigger_code
               ORDER BY (t.status='queued') DESC, t.due_at""",
        ).fetchall()
        nowdt = utcnow()
        out = []
        for r in rows:
            d = dict(r)
            d["status_name"] = TASK_STATUS_NAMES[d["status"]]
            d["overdue"] = d["status"] in ("queued", "claimed") and parse_iso(d["due_at"]) < nowdt
            out.append(d)
        return out

    def _sweep_timeouts(self, now: datetime | None = None) -> int:
        """把超过 due_at 的 queued/claimed 任务标记为 timeout。"""
        now = now or utcnow()
        count = 0
        with tx(self.conn):
            rows = self.conn.execute(
                "SELECT id FROM reinspection_tasks WHERE status IN ('queued','claimed') AND due_at < ?",
                (iso(now),),
            ).fetchall()
            for r in rows:
                self.conn.execute(
                    "UPDATE reinspection_tasks SET status='timeout' WHERE id=? AND status IN ('queued','claimed')",
                    (r["id"],),
                )
                count += 1
        return count

    def start_sweeper(self, interval_seconds: float = 5.0) -> None:
        """启动进程内超时巡检线程。"""
        if self._sweeper_thread and self._sweeper_thread.is_alive():
            return
        stop = threading.Event()
        self._sweeper_stop = stop

        def loop() -> None:
            while not stop.wait(interval_seconds):
                try:
                    self._sweep_timeouts()
                except sqlite3.Error:
                    pass

        t = threading.Thread(target=loop, name="reinspect-timeout-sweeper", daemon=True)
        self._sweeper_thread = t
        t.start()

    def stop_sweeper(self) -> None:
        if self._sweeper_stop:
            self._sweeper_stop.set()
            self._sweeper_thread = None

    # ----------------------------------------------------------------- 退修

    def open_repair(
        self, bolt_code: str, reason: str, now: datetime | None = None
    ) -> int:
        """对当前版本开具退修工单（A 级断经、B 级色差/污渍确认后）。"""
        now = now or utcnow()
        bolt = self._bolt_row(bolt_code)
        if bolt["status"] == "released":
            raise Conflict("已放行布卷不能直接改为退修/待检")
        if not bolt["current_version"]:
            raise Conflict("布卷尚未初检，不能退修")
        active = self.conn.execute(
            "SELECT id FROM repair_orders WHERE bolt_code=? AND status IN ('open','in_progress')",
            (bolt_code,),
        ).fetchone()
        if active:
            raise Conflict("该布卷已有进行中的退修工单")
        cur = self.conn.execute(
            """INSERT INTO repair_orders(bolt_code, from_version_id, reason, status, created_at)
               VALUES (?,?,?,'open',?)""",
            (bolt_code, bolt["current_version"], reason, iso(now)),
        )
        self.conn.execute("UPDATE bolts SET status='repairing' WHERE code=?", (bolt_code,))
        self.conn.commit()
        return cur.lastrowid

    def start_repair(self, order_id: int, now: datetime | None = None) -> None:
        now = now or utcnow()
        order = self._repair_row(order_id)
        if order["status"] != "open":
            raise Conflict("只有待开工工单可以开工")
        self.conn.execute(
            "UPDATE repair_orders SET status='in_progress', started_at=? WHERE id=?",
            (iso(now), order_id),
        )
        self.conn.commit()

    def finish_repair(
        self, order_id: int, inspector: str, now: datetime | None = None
    ) -> int:
        """退修完成：生成新的 post_repair 检验版本并成为当前版本。"""
        now = now or utcnow()
        order = self._repair_row(order_id)
        if order["status"] not in ("open", "in_progress"):
            raise Conflict("退修工单已结束")
        bolt = self._bolt_row(order["bolt_code"])
        if bolt["status"] == "released":
            raise Conflict("已放行布卷不能直接改为待检")
        with tx(self.conn):
            last_no = self.conn.execute(
                "SELECT COALESCE(MAX(version_no),0) FROM inspection_versions WHERE bolt_code=?",
                (order["bolt_code"],),
            ).fetchone()[0]
            cur = self.conn.execute(
                """INSERT INTO inspection_versions
                       (bolt_code, version_no, kind, inspector, created_at, supersedes, repair_order_id)
                   VALUES (?,?,'post_repair',?,?,?,?)""",
                (order["bolt_code"], last_no + 1, inspector, iso(now),
                 order["from_version_id"], order_id),
            )
            new_vid = cur.lastrowid
            self.conn.execute(
                "UPDATE repair_orders SET status='done', finished_at=?, new_version_id=? WHERE id=?",
                (iso(now), new_vid, order_id),
            )
            self.conn.execute(
                "UPDATE bolts SET current_version=?, status='reinspecting' WHERE code=?",
                (new_vid, order["bolt_code"]),
            )
        return new_vid

    def _repair_row(self, order_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM repair_orders WHERE id=?", (order_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"退修工单不存在: {order_id}")
        return row

    def repairs(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT ro.*, b.length_m, cb.color_name
               FROM repair_orders ro
               JOIN bolts b ON b.code=ro.bolt_code
               JOIN color_batches cb ON cb.code=b.color_batch
               ORDER BY ro.id DESC"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["status_name"] = REPAIR_STATUS_NAMES[d["status"]]
            nv = self.conn.execute(
                "SELECT version_no FROM inspection_versions WHERE id=?", (r["new_version_id"],)
            ).fetchone() if r["new_version_id"] else None
            fv = self.conn.execute(
                "SELECT version_no FROM inspection_versions WHERE id=?", (r["from_version_id"],)
            ).fetchone()
            d["from_version_no"] = fv["version_no"] if fv else None
            d["new_version_no"] = nv["version_no"] if nv else None
            out.append(d)
        return out

    # ----------------------------------------------------------------- 放行

    def _segment_cleared(self, segment_id: int) -> bool:
        row = self.conn.execute(
            """SELECT result FROM reinspection_tasks
               WHERE segment_id=? AND status='completed'
               ORDER BY id DESC LIMIT 1""",
            (segment_id,),
        ).fetchone()
        return bool(row and row["result"] == "cleared")

    def evaluate_release(self, bolt_code: str) -> ReleaseDecision:
        """按当前版本上的疵点等级计算放行条件是否满足。"""
        bolt = self._bolt_row(bolt_code)
        reasons: list[str] = []
        if bolt["status"] == "released":
            return ReleaseDecision(False, "A", ["布卷已放行，不能重复放行"])
        if not bolt["current_version"]:
            return ReleaseDecision(False, "A", ["布卷尚未初检"])
        version = self._version_row(bolt["current_version"])

        active = self.conn.execute(
            "SELECT id, segment_id, status FROM reinspection_tasks WHERE bolt_code=? AND status IN ('queued','claimed','timeout')",
            (bolt_code,),
        ).fetchall()
        stale_timeout = [
            r for r in active
            if r["status"] == "timeout"
            and not self.conn.execute(
                "SELECT 1 FROM reinspection_tasks WHERE segment_id=? AND id>? AND status='completed' LIMIT 1",
                (r["segment_id"], r["id"]),
            ).fetchone()
        ]
        if stale_timeout:
            reasons.append("存在超时复检任务，须重新派发并完成后才能放行")
        if any(r["status"] in ("queued", "claimed") for r in active):
            reasons.append("尚有未完成的复检任务")

        defects = self.conn.execute(
            """SELECT d.*, dt.severity, s.id AS seg_id
               FROM defects d
               JOIN defect_types dt ON dt.code=d.type_code
               JOIN segments s ON s.id=d.segment_id
               WHERE s.version_id=?""",
            (version["id"],),
        ).fetchall()

        grades = [d["severity"] for d in defects]
        # 放行定级：A=优等（全新无疵点）；B=退修修复品或 B 级疵点让步放行；
        # C=仅存 C 级轻微疵点确认放行。退修品最高只能定 B 级。
        if grades:
            grade = worst_grade(grades)
        elif version["kind"] == "post_repair":
            grade = "B"
        else:
            grade = "A"
        repaired = version["kind"] == "post_repair"

        for d in defects:
            pos = f"{d['position_m']}m 处"
            if d["severity"] == "A":  # 断经：必须退修 + 新版本复检确认清除
                if not repaired:
                    reasons.append(f"{pos} A 级疵点（断经）必须先退修")
                elif not self._segment_cleared(d["seg_id"]):
                    reasons.append(f"{pos}断经退修后须经复检确认清除")
            elif d["type_code"] in ("color_diff", "stain"):
                if not repaired:
                    label = "色差" if d["type_code"] == "color_diff" else "污渍"
                    reasons.append(f"{pos} B 级疵点（{label}）须退修处理")
                elif not self._segment_cleared(d["seg_id"]):
                    reasons.append(f"{pos}退修后须经复检确认不影响使用")
            elif d["type_code"] == "short_length":
                if not self._segment_cleared(d["seg_id"]):
                    reasons.append(f"{pos}长度不足须经复检确认后折价放行")
            elif d["type_code"] == "missing_photo":
                if not d["resolved_at"]:
                    reasons.append(f"{pos}照片缺失，必须补齐照片")
            else:  # C 级其他：书面确认
                if not self._segment_cleared(d["seg_id"]):
                    reasons.append(f"{pos} C 级疵点须经复检员书面确认")
        return ReleaseDecision(not reasons, grade, reasons)

    def release(
        self, bolt_code: str, released_by: str, note: str = "", now: datetime | None = None
    ) -> int:
        now = now or utcnow()
        decision = self.evaluate_release(bolt_code)
        if not decision.allowed:
            raise Conflict("；".join(decision.reasons))
        bolt = self._bolt_row(bolt_code)
        # 长度不足折价说明自动附注
        defects = self.conn.execute(
            """SELECT d.* FROM defects d JOIN segments s ON s.id=d.segment_id
               WHERE s.version_id=? AND d.type_code='short_length'""",
            (bolt["current_version"],),
        ).fetchall()
        full_note = note
        if defects and bolt["nominal_length_m"]:
            ratio = bolt["length_m"] / bolt["nominal_length_m"]
            if ratio < SHORT_LENGTH_RATIO and f"{ratio:.1%}" not in full_note:
                full_note = (full_note + " " if full_note else "") + (
                    f"实际 {bolt['length_m']}m / 标称 {bolt['nominal_length_m']}m，"
                    f"按 {ratio:.1%} 折价放行"
                )
        cur = self.conn.execute(
            """INSERT INTO releases(bolt_code, version_id, released_by, released_at, grade, note)
               VALUES (?,?,?,?,?,?)""",
            (bolt_code, bolt["current_version"], released_by, iso(now),
             decision.grade, full_note),
        )
        self.conn.execute("UPDATE bolts SET status='released' WHERE code=?", (bolt_code,))
        self.conn.commit()
        return cur.lastrowid

    def releases(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT r.*, b.length_m, b.width_m, cb.color_name, l.code AS loom_code,
                      iv.version_no
               FROM releases r
               JOIN bolts b ON b.code=r.bolt_code
               JOIN color_batches cb ON cb.code=b.color_batch
               JOIN looms l ON l.code=b.loom_code
               JOIN inspection_versions iv ON iv.id=r.version_id
               ORDER BY r.id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- 照片权限

    def grant_photo(
        self,
        defect_id: int,
        task_id: int,
        assignee: str,
        now: datetime | None = None,
    ) -> str:
        """复检人为其已接收任务下的疵点申请照片访问令牌。"""
        now = now or utcnow()
        import secrets

        task = self.conn.execute(
            "SELECT * FROM reinspection_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if task is None:
            raise NotFound(f"复检任务不存在: {task_id}")
        if task["status"] != "claimed":
            raise Conflict("任务不在复检中，不能查看照片")
        if task["assignee"] != assignee:
            raise PermissionDenied("只能为自己接收的复检任务申请照片")
        d = self.conn.execute("SELECT * FROM defects WHERE id=?", (defect_id,)).fetchone()
        if d is None:
            raise NotFound(f"疵点不存在: {defect_id}")
        seg = self.conn.execute(
            "SELECT version_id FROM segments WHERE id=?", (d["segment_id"],)
        ).fetchone()
        if seg["version_id"] != task["version_id"] or not d["photo_path"]:
            raise PermissionDenied("该照片不属于此复检任务或照片缺失")
        token = secrets.token_urlsafe(24)
        self.conn.execute(
            """INSERT INTO photo_grants(token, defect_id, task_id, assignee, created_at, expires_at)
               VALUES (?,?,?,?,?,?)""",
            (token, defect_id, task_id, assignee, iso(now),
             iso(now + timedelta(seconds=PHOTO_GRANT_TTL_SECONDS))),
        )
        self.conn.commit()
        return token

    def read_photo(self, token: str, now: datetime | None = None) -> tuple[bytes, str]:
        """凭令牌读取照片；任务完成、令牌过期或无效一律拒绝。"""
        now = now or utcnow()
        g = self.conn.execute(
            "SELECT * FROM photo_grants WHERE token=?", (token,)
        ).fetchone()
        if g is None:
            raise PermissionDenied("无效的照片访问令牌")
        if parse_iso(g["expires_at"]) < now:
            raise PermissionDenied("照片访问令牌已过期")
        task = self.conn.execute(
            "SELECT status, assignee FROM reinspection_tasks WHERE id=?", (g["task_id"],)
        ).fetchone()
        if task is None or task["status"] != "claimed":
            raise PermissionDenied("复检任务已结束，照片访问权限收回")
        d = self.conn.execute(
            "SELECT photo_path FROM defects WHERE id=?", (g["defect_id"],)
        ).fetchone()
        if not d or not d["photo_path"]:
            raise NotFound("照片缺失")
        path = photos_mod.abs_path(self.data_dir, d["photo_path"])
        ext = path.rsplit(".", 1)[-1].lower()
        with open(path, "rb") as fh:
            return fh.read(), _EXT_MIME.get(ext, "application/octet-stream")

    def close(self) -> None:
        self.stop_sweeper()
        self.conn.close()
