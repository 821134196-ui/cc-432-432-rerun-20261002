"""纺织厂布卷疵点复检与退修系统 —— 领域层（SQLite）。

核心规则：
* 疵点按米数定位，位置必须落在布卷长度区间内；照片文件统一存于本地目录。
* 复检只能引用原检验段（source_segment_id），不能凭空新建疵点；退修完成后生成新版本。
* 已放行布卷不能直接改回待检（唯一的“回头路”是由新版本承接）。
* 复检任务先抢先得：两个产线同时接收同一布卷时，只允许一条任务成功（行锁 + 状态条件）。
* 复检人不得与初检人相同。
* 不同等级疵点放行条件不同；照片缺失的疵点必须在复检时补拍后才允许放行。
* 复检任务超过时限未提交即超时，由后台扫描器标记并产生提醒。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS loom (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,           -- 织机编号
    workshop    TEXT NOT NULL,                  -- 车间
    status      TEXT NOT NULL DEFAULT '运行'
);

CREATE TABLE IF NOT EXISTS color_batch (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,           -- 颜色批次号
    color       TEXT NOT NULL,                  -- 颜色名
    dye_lot     TEXT NOT NULL                   -- 染缸批号
);

CREATE TABLE IF NOT EXISTS fabric_roll (
    id              INTEGER PRIMARY KEY,
    roll_no         TEXT NOT NULL UNIQUE,       -- 布卷编号
    length_m        REAL NOT NULL,              -- 长度（米）
    width_m         REAL NOT NULL,              -- 幅宽（米）
    color_batch_id  INTEGER NOT NULL REFERENCES color_batch(id),
    loom_id         INTEGER NOT NULL REFERENCES loom(id),
    min_length_m    REAL NOT NULL DEFAULT 0,     -- 订单最小长度（长度不足判定）
    current_version INTEGER NOT NULL DEFAULT 0, -- 当前检验版本号（0 = 尚未检验）
    status          TEXT NOT NULL DEFAULT '待检',  -- 待检/检验中/待复检/退修中/待放行/已放行
    released        INTEGER NOT NULL DEFAULT 0, -- 是否放行（终态保护）
    created_at      TEXT NOT NULL
);

-- 检验版本：初检 v1；每次退修后由复检提交生成 v2、v3……
CREATE TABLE IF NOT EXISTS inspection_version (
    id              INTEGER PRIMARY KEY,
    roll_id         INTEGER NOT NULL REFERENCES fabric_roll(id),
    version_no      INTEGER NOT NULL,           -- 1 = 初检，>1 = 退修后新版本
    kind            TEXT NOT NULL,              -- initial / rework
    inspector       TEXT NOT NULL,              -- 初检人
    created_at      TEXT NOT NULL,
    UNIQUE(roll_id, version_no)
);

-- 检验段：复检只能引用初检段（source_version_no 恒为 1）
CREATE TABLE IF NOT EXISTS segment (
    id                  INTEGER PRIMARY KEY,
    version_id          INTEGER NOT NULL REFERENCES inspection_version(id),
    roll_id             INTEGER NOT NULL REFERENCES fabric_roll(id),
    start_m             REAL NOT NULL,
    end_m               REAL NOT NULL,
    inspector           TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

-- 疵点类型字典
CREATE TABLE IF NOT EXISTS defect_type (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,                  -- 断经/色差/污渍/长度不足/其他
    severity    TEXT NOT NULL DEFAULT 'B'       -- A 严重 / B 中等 / C 轻微
);

CREATE TABLE IF NOT EXISTS defect (
    id              INTEGER PRIMARY KEY,
    roll_id         INTEGER NOT NULL REFERENCES fabric_roll(id),
    version_no      INTEGER NOT NULL,           -- 快照所属版本（1 = 初检）
    source_segment_id INTEGER REFERENCES segment(id),  -- 复检时引用的原检验段；初检为 NULL
    type_id         INTEGER NOT NULL REFERENCES defect_type(id),
    pos_m           REAL NOT NULL,              -- 米数定位
    length_m        REAL NOT NULL DEFAULT 0,    -- 疵点长度
    grade           TEXT NOT NULL,              -- A / B / C
    status          TEXT NOT NULL DEFAULT '待复检', -- 待复检/复检确认/误判/已修复
    photo_file      TEXT,                       -- 本地照片文件名（相对照片目录）
    note            TEXT,
    created_at      TEXT NOT NULL
);

-- 复检任务（每个布卷同一时刻至多一条 open 任务，由部分唯一索引保证）
CREATE TABLE IF NOT EXISTS review_task (
    id              INTEGER PRIMARY KEY,
    roll_id         INTEGER NOT NULL REFERENCES fabric_roll(id),
    version_no      INTEGER NOT NULL,           -- 复检针对的版本
    line            TEXT,                       -- 接收产线（接收后写入）
    reviewer        TEXT,                       -- 复检人
    status          TEXT NOT NULL DEFAULT '待接收', -- 待接收/进行中/已提交/超时
    created_at      TEXT NOT NULL,
    claimed_at      TEXT,
    due_at          TEXT NOT NULL,              -- 复检时限
    submitted_at    TEXT,
    disposition     TEXT,                       -- 误判关闭 / 转退修
    note            TEXT NOT NULL DEFAULT ''
);
-- 同一布卷同一版本可留下多条历史任务（超时后重新下发），但任一时刻至多一条 open：
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_open
    ON review_task(roll_id) WHERE status IN ('待接收', '进行中');

-- 退修工单
CREATE TABLE IF NOT EXISTS rework_order (
    id              INTEGER PRIMARY KEY,
    roll_id         INTEGER NOT NULL REFERENCES fabric_roll(id),
    task_id         INTEGER NOT NULL REFERENCES review_task(id),
    from_version    INTEGER NOT NULL,
    to_version      INTEGER,                    -- 退修完成后生成的新版本号
    defects         TEXT NOT NULL,              -- JSON：退修疵点 id 列表
    status          TEXT NOT NULL DEFAULT '待退修', -- 待退修/退修中/已完成
    assignee        TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    completed_at    TEXT
);

-- 放行记录
CREATE TABLE IF NOT EXISTS release_record (
    id              INTEGER PRIMARY KEY,
    roll_id         INTEGER NOT NULL REFERENCES fabric_roll(id),
    version_no      INTEGER NOT NULL,
    reviewer        TEXT NOT NULL,
    decision        TEXT NOT NULL,              -- 放行
    basis           TEXT NOT NULL,              -- 放行依据（文字）
    created_at      TEXT NOT NULL
);

-- 复检提醒（进程内后台扫描器写入，API/前端轮询读取）
CREATE TABLE IF NOT EXISTS reminder (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                  -- review_overdue
    ref_id      INTEGER NOT NULL,               -- review_task.id
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    read        INTEGER NOT NULL DEFAULT 0
);
"""

# ---- 领域常量 ---------------------------------------------------------------

ROLL_STATUSES = ['待检', '检验中', '待复检', '退修中', '待放行', '已放行']
SEVERITY = {'A': '严重', 'B': '中等', 'C': '轻微'}

# 不同等级疵点的放行条件
RELEASE_RULES = {
    'A': 'A 级疵点必须经退修修复并在新版本复检确认后方可放行',
    'B': 'B 级疵点须复检确认；可接受的小疵点须有复检记录与照片',
    'C': 'C 级疵点复检确认或判误判即可放行',
}

# 疵点类型的专项处理方式
DEFECT_HANDLING = {
    '断经': '必须退修（接头/换经），修复后复检确认',
    '色差': '与颜色批次标样比对；确认后让步接收或退修回染',
    '污渍': '可清洗的先清洗复检；无法清除按 A 级退修',
    '长度不足': '核减结算长度；低于订单最小长度不得放行',
    '照片缺失': '不允许直接放行，复检时必须补拍照片',
    '复检超时': '任务自动置为超时，提醒质检班长改派，他人可重新接收',
}

REVIEW_TIMEOUT_SEC = 30 * 60  # 复检时限 30 分钟（演示用；可在种子数据中按秒偏移构造超时）


class DomainError(Exception):
    """业务规则冲突，HTTP 层映射为 409/400。"""


def now_ts() -> float:
    return time.time()


def iso(ts: Optional[float] = None) -> str:
    ts = now_ts() if ts is None else ts
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')


# ---- 存储 -------------------------------------------------------------------

class Database:
    def __init__(self, path: str = 'textile_qc.db', photo_dir: str = 'photos'):
        self.path = path
        self.photo_dir = os.path.abspath(photo_dir)
        os.makedirs(self.photo_dir, exist_ok=True)
        # check_same_thread=False + 自身锁：HTTP 线程与后台扫描器共用
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA foreign_keys = ON')
        self._conn.execute('PRAGMA journal_mode = WAL')
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # --- 基础工具 ---
    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def tx(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """在事务 + 行级锁内执行写操作；fn 抛 DomainError 时回滚。"""
        with self._lock:
            c = self._conn
            try:
                c.execute('BEGIN IMMEDIATE')
                result = fn(c)
                c.commit()
                return result
            except Exception:
                c.rollback()
                raise

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args).fetchall())

    def one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def require_roll(self, conn: sqlite3.Connection, roll_id: int) -> sqlite3.Row:
        r = conn.execute('SELECT * FROM fabric_roll WHERE id=?', (roll_id,)).fetchone()
        if not r:
            raise DomainError(f'布卷 {roll_id} 不存在')
        return r

    @staticmethod
    def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
        return [dict(r) for r in rows]

    # --- 布卷 / 台账 ---
    def list_rolls(self) -> list[dict]:
        rows = self.query("""
            SELECT r.*, c.code AS color_batch_code, c.color, c.dye_lot,
                   l.code AS loom_code, l.workshop
            FROM fabric_roll r
            JOIN color_batch c ON c.id = r.color_batch_id
            JOIN loom l ON l.id = r.loom_id
            ORDER BY r.roll_no
        """)
        out = []
        for r in rows:
            d = dict(r)
            d['open_defects'] = self._open_defect_count(r['id'])
            out.append(d)
        return out

    def _open_defect_count(self, roll_id: int) -> int:
        r = self.one(
            "SELECT COUNT(*) n FROM defect WHERE roll_id=? AND status='待复检'",
            (roll_id,))
        return r['n'] if r else 0

    def create_roll(self, roll_no: str, length_m: float, width_m: float,
                    color_batch_code: str, loom_code: str,
                    min_length_m: float = 0) -> int:
        def work(c: sqlite3.Connection) -> int:
            cb = c.execute('SELECT id FROM color_batch WHERE code=?', (color_batch_code,)).fetchone()
            lm = c.execute('SELECT id FROM loom WHERE code=?', (loom_code,)).fetchone()
            if not cb:
                raise DomainError(f'颜色批次 {color_batch_code} 不存在')
            if not lm:
                raise DomainError(f'织机 {loom_code} 不存在')
            if length_m <= 0 or width_m <= 0:
                raise DomainError('长度与幅宽必须为正数')
            cur = c.execute("""
                INSERT INTO fabric_roll(roll_no, length_m, width_m, color_batch_id, loom_id,
                                        min_length_m, created_at)
                VALUES (?,?,?,?,?,?,?)
            """, (roll_no, length_m, width_m, cb['id'], lm['id'], min_length_m, iso()))
            return int(cur.lastrowid)
        return self.tx(work)

    def _assert_not_released(self, r: sqlite3.Row, action: str) -> None:
        if r['released']:
            raise DomainError(
                f"布卷 {r['roll_no']} 已放行（终态），不能直接{action}；"
                f"如需处理须由退修产生新版本承接")

    # --- 初检 ---
    def begin_inspection(self, roll_id: int, inspector: str) -> int:
        def work(c: sqlite3.Connection) -> int:
            r = self.require_roll(c, roll_id)
            self._assert_not_released(r, '改为待检/检验')
            if r['current_version'] != 0 or r['status'] not in ('待检',):
                raise DomainError(f"布卷 {r['roll_no']} 当前状态为 {r['status']}，不能开始初检")
            if not inspector:
                raise DomainError('初检人不能为空')
            c.execute("UPDATE fabric_roll SET status='检验中', current_version=1 WHERE id=?",
                      (roll_id,))
            cur = c.execute(
                "INSERT INTO inspection_version(roll_id, version_no, kind, inspector, created_at)"
                " VALUES (?,1,'initial',?,?)",
                (roll_id, inspector, iso()))
            return int(cur.lastrowid)
        return self.tx(work)

    def add_segment(self, version_id: int, start_m: float, end_m: float,
                    inspector: str) -> int:
        def work(c: sqlite3.Connection) -> int:
            v = c.execute('SELECT * FROM inspection_version WHERE id=?', (version_id,)).fetchone()
            if not v:
                raise DomainError('检验版本不存在')
            if v['version_no'] != 1:
                raise DomainError('检验段只允许在初检版本中创建')
            r = self.require_roll(c, v['roll_id'])
            self._assert_not_released(r, '录入检验段')
            # 位置区间校验
            if start_m < 0 or end_m <= start_m:
                raise DomainError(f'检验段区间非法：[{start_m}, {end_m})')
            if end_m > r['length_m'] + 1e-9:
                raise DomainError(
                    f'检验段 {start_m}-{end_m}m 超出布卷长度 {r["length_m"]}m')
            # 段不重叠
            old = c.execute(
                'SELECT start_m, end_m FROM segment WHERE version_id=?', (version_id,)).fetchall()
            for s in old:
                if start_m < s['end_m'] - 1e-9 and s['start_m'] < end_m - 1e-9:
                    raise DomainError(
                        f'检验段与已有区间 [{s["start_m"]}, {s["end_m"]}) 重叠')
            cur = c.execute("""
                INSERT INTO segment(version_id, roll_id, start_m, end_m, inspector, created_at)
                VALUES (?,?,?,?,?,?)
            """, (version_id, r['id'], start_m, end_m, inspector, iso()))
            return int(cur.lastrowid)
        return self.tx(work)

    def add_defect(self, segment_id: int, type_code: str, pos_m: float,
                   length_m: float, grade: str, inspector: str,
                   photo_file: Optional[str], note: Optional[str]) -> int:
        def work(c: sqlite3.Connection) -> int:
            seg = c.execute('SELECT * FROM segment WHERE id=?', (segment_id,)).fetchone()
            if not seg:
                raise DomainError('检验段不存在')
            r = self.require_roll(c, seg['roll_id'])
            self._assert_not_released(r, '登记疵点')
            if pos_m < seg['start_m'] - 1e-9 or pos_m > seg['end_m'] + 1e-9:
                raise DomainError(
                    f'疵点位置 {pos_m}m 不在检验段 [{seg["start_m"]}, {seg["end_m"]}] 内')
            if pos_m > r['length_m'] + 1e-9:
                raise DomainError(f'疵点位置 {pos_m}m 超出布卷长度 {r["length_m"]}m')
            if length_m < 0:
                raise DomainError('疵点长度不能为负')
            if grade not in SEVERITY:
                raise DomainError('疵点等级必须为 A/B/C')
            t = c.execute('SELECT * FROM defect_type WHERE code=?', (type_code,)).fetchone()
            if not t:
                raise DomainError(f'疵点类型 {type_code} 不存在')
            cur = c.execute("""
                INSERT INTO defect(roll_id, version_no, source_segment_id, type_id,
                                   pos_m, length_m, grade, status, photo_file, note, created_at)
                VALUES (?,1,?,?,?,?,?,'待复检',?,?,?)
            """, (r['id'], seg['id'], t['id'], pos_m, length_m, grade,
                  photo_file, note, iso()))
            return int(cur.lastrowid)
        return self.tx(work)

    def complete_initial(self, roll_id: int) -> None:
        def work(c: sqlite3.Connection) -> int:
            r = self.require_roll(c, roll_id)
            self._assert_not_released(r, '结束初检')
            if r['status'] != '检验中':
                raise DomainError(f"布卷 {r['roll_no']} 不是检验中状态")
            v = c.execute(
                "SELECT * FROM inspection_version WHERE roll_id=? AND version_no=1",
                (roll_id,)).fetchone()
            if not v:
                raise DomainError('不存在初检版本')
            n = c.execute(
                "SELECT COUNT(*) n FROM defect WHERE roll_id=? AND version_no=1",
                (roll_id,)).fetchone()['n']
            new_status = '待复检' if n else '待放行'
            c.execute('UPDATE fabric_roll SET status=? WHERE id=?', (new_status, roll_id))
            if n:
                due = now_ts() + REVIEW_TIMEOUT_SEC
                c.execute("""
                    INSERT INTO review_task(roll_id, version_no, status, created_at, due_at)
                    VALUES (?,1,'待接收',?,?)
                """, (roll_id, iso(), iso(due)))
            return 0
        return self.tx(work)

    # --- 复检任务：接收竞争 ---
    def claim_task(self, task_id: int, line: str, reviewer: str) -> dict:
        """两个产线同时接收时只允许一条成功：UPDATE ... WHERE 状态条件 + 部分唯一索引。"""
        if not line or not reviewer:
            raise DomainError('接收产线与复检人必填')

        def work(c: sqlite3.Connection) -> dict:
            t = c.execute('SELECT * FROM review_task WHERE id=?', (task_id,)).fetchone()
            if not t:
                raise DomainError('复检任务不存在')
            r = self.require_roll(c, t['roll_id'])
            # 初检人隔离：取初检版本 inspector
            v1 = c.execute(
                'SELECT inspector FROM inspection_version WHERE roll_id=? AND version_no=1',
                (t['roll_id'],)).fetchone()
            if v1 and v1['inspector'] == reviewer:
                raise DomainError(
                    f'复检人 {reviewer} 与初检人相同，按隔离规则不能接收本任务')
            cur = c.execute("""
                UPDATE review_task
                SET status='进行中', line=?, reviewer=?, claimed_at=?
                WHERE id=? AND status='待接收'
            """, (line, reviewer, iso(), task_id))
            if cur.rowcount == 0:
                cur2 = c.execute(
                    "SELECT line FROM review_task WHERE id=?", (task_id,)).fetchone()
                who = cur2['line'] if cur2 else '其他产线'
                raise DomainError(f'接收失败：任务已被 {who} 抢先接收（同一布卷仅允许一条复检任务）')
            c.execute("UPDATE fabric_roll SET status='待复检' WHERE id=?", (r['id'],))
            return {'task_id': task_id, 'line': line, 'reviewer': reviewer}
        return self.tx(work)

    def submit_review(self, task_id: int, decisions: list[dict],
                      disposition: str) -> dict:
        """提交复检。decisions: [{defect_id, action, photo_file?, note?}]
        action ∈ 确认 / 误判 / 转退修。转退修的 A 级（或断经）疵点进入退修工单。
        """
        def work(c: sqlite3.Connection) -> dict:
            t = c.execute('SELECT * FROM review_task WHERE id=?', (task_id,)).fetchone()
            if not t:
                raise DomainError('复检任务不存在')
            if t['status'] != '进行中':
                raise DomainError(f"任务状态为 {t['status']}，不能提交（仅进行中可提交）")
            r = self.require_roll(c, t['roll_id'])
            to_rework: list[int] = []
            resolved = 0
            for d in decisions:
                df = c.execute(
                    'SELECT * FROM defect WHERE id=? AND roll_id=?',
                    (d['defect_id'], r['id'])).fetchone()
                if not df:
                    raise DomainError(f"疵点 {d['defect_id']} 不属于本布卷")
                # 复检只能引用原检验段：复检动作必须挂在初检疵点（版本 1）上
                if df['version_no'] != 1:
                    raise DomainError('复检只能引用初检（版本 1）产生的疵点')
                action = d.get('action')
                photo = d.get('photo_file') or df['photo_file']
                if action == '误判':
                    c.execute(
                        "UPDATE defect SET status='误判', note=COALESCE(?,note) WHERE id=?",
                        (d.get('note'), df['id']))
                    resolved += 1
                elif action == '确认':
                    # 照片缺失专项处理：必须补拍
                    if not photo:
                        raise DomainError(
                            f"疵点 #{df['id']} 照片缺失，须补拍照片后方可确认")
                    c.execute(
                        "UPDATE defect SET status='复检确认', photo_file=? WHERE id=?",
                        (photo, df['id']))
                    resolved += 1
                elif action == '转退修':
                    dt = c.execute('SELECT category FROM defect_type WHERE id=?',
                                   (df['type_id'],)).fetchone()
                    if df['grade'] != 'A' and dt['category'] != '断经':
                        # B/C 级也允许退修，但 A/断经是强制项
                        pass
                    c.execute("UPDATE defect SET status='待复检' WHERE id=?", (df['id'],))
                    to_rework.append(df['id'])
                else:
                    raise DomainError(f"未知复检动作：{action}")
            if disposition not in ('误判关闭', '转退修', '直接放行'):
                raise DomainError('处置方式必须为 误判关闭/转退修/直接放行')
            if disposition == '转退修' and not to_rework:
                raise DomainError('处置为转退修时至少要选择一个疵点退修')
            c.execute("""
                UPDATE review_task SET status='已提交', submitted_at=?, disposition=?
                WHERE id=?
            """, (iso(), disposition, task_id))
            order_id = None
            if to_rework:
                cur = c.execute("""
                    INSERT INTO rework_order(roll_id, task_id, from_version, defects,
                                             status, created_at)
                    VALUES (?,?,?,?,'待退修',?)
                """, (r['id'], task_id, t['version_no'], _json(to_rework), iso()))
                order_id = int(cur.lastrowid)
                c.execute("UPDATE fabric_roll SET status='退修中' WHERE id=?", (r['id'],))
            else:
                c.execute("UPDATE fabric_roll SET status='待放行' WHERE id=?", (r['id'],))
            return {'task_id': task_id, 'resolved': resolved,
                    'rework_order_id': order_id, 'disposition': disposition}
        return self.tx(work)

    # --- 退修：新版本 ---
    def start_rework(self, order_id: int, assignee: str) -> None:
        def work(c: sqlite3.Connection) -> None:
            o = c.execute('SELECT * FROM rework_order WHERE id=?', (order_id,)).fetchone()
            if not o:
                raise DomainError('退修工单不存在')
            if o['status'] != '待退修':
                raise DomainError(f"工单状态为 {o['status']}，不能开工")
            c.execute(
                "UPDATE rework_order SET status='退修中', assignee=?, started_at=? WHERE id=?",
                (assignee, iso(), order_id))
        self.tx(work)

    def complete_rework(self, order_id: int, results: list[dict],
                        inspector: str) -> int:
        """退修完成 → 生成新版本（v+1），新版本中的修复疵点由 source_segment_id
        引用原检验段，证明其来源于初检。results: [{defect_id, action:修复/核减长度,
        photo_file, note, new_length_m?}]
        返回新版本号。"""
        def work(c: sqlite3.Connection) -> int:
            o = c.execute('SELECT * FROM rework_order WHERE id=?', (order_id,)).fetchone()
            if not o:
                raise DomainError('退修工单不存在')
            if o['status'] != '退修中':
                raise DomainError(f"工单状态为 {o['status']}，不能完工")
            r = self.require_roll(c, o['roll_id'])
            new_len = r['length_m']
            new_no = r['current_version'] + 1 if r['current_version'] else 2
            # 初检已存在，故新版本号至少为 2
            new_no = max(new_no, r['current_version'] + 1)
            cur = c.execute("""
                INSERT INTO inspection_version(roll_id, version_no, kind, inspector, created_at)
                VALUES (?,?, 'rework',?,?)
            """, (r['id'], new_no, inspector, iso()))
            version_id = int(cur.lastrowid)
            for item in results:
                df = c.execute('SELECT * FROM defect WHERE id=?', (item['defect_id'],)).fetchone()
                if not df:
                    raise DomainError(f"疵点 {item['defect_id']} 不存在")
                action = item.get('action')
                if action == '核减长度':
                    nl = item.get('new_length_m')
                    if nl is None or nl <= 0 or nl >= r['length_m']:
                        raise DomainError('核减后的长度必须为正且小于当前长度')
                    new_len = nl
                elif action != '修复':
                    raise DomainError('退修结果必须为 修复/核减长度')
                if not item.get('photo_file'):
                    raise DomainError(f"疵点 #{df['id']} 修复后须附照片（照片缺失不得放行）")
                # 修复疵点引用原检验段（source_segment_id），落在新版本快照中
                c.execute("""
                    INSERT INTO defect(roll_id, version_no, source_segment_id, type_id,
                                       pos_m, length_m, grade, status, photo_file, note, created_at)
                    VALUES (?,?,?,?,?,?,?, '已修复', ?,?,?)
                """, (r['id'], new_no, df['source_segment_id'], df['type_id'],
                      df['pos_m'], df['length_m'], df['grade'],
                      item['photo_file'], item.get('note'), iso()))
                c.execute("UPDATE defect SET status='已修复' WHERE id=?", (df['id'],))
            c.execute("""
                UPDATE rework_order SET status='已完成', to_version=?, completed_at=?
                WHERE id=?
            """, (new_no, iso(), order_id))
            c.execute("""
                UPDATE fabric_roll SET current_version=?, length_m=?, status='待放行'
                WHERE id=?
            """, (new_no, new_len, r['id']))
            return new_no
        return self.tx(work)

    # --- 放行：按等级条件 ---
    def release(self, roll_id: int, reviewer: str) -> dict:
        def work(c: sqlite3.Connection) -> dict:
            r = self.require_roll(c, roll_id)
            if r['released']:
                raise DomainError(f"布卷 {r['roll_no']} 已放行，不能重复放行")
            if not reviewer:
                raise DomainError('放行人（复检人）必填')
            v1 = c.execute(
                'SELECT inspector FROM inspection_version WHERE roll_id=? AND version_no=1',
                (roll_id,)).fetchone()
            if v1 and v1['inspector'] == reviewer:
                raise DomainError('放行人不得与初检人相同')
            if r['status'] != '待放行':
                raise DomainError(f"布卷状态为 {r['status']}，不满足放行前提")
            version = r['current_version']
            # 放行条件评估（针对当前未关闭疵点）
            blockers: list[str] = []
            if r['min_length_m'] and r['length_m'] < r['min_length_m']:
                blockers.append(
                    f"布卷长度 {r['length_m']}m 低于订单最小长度 {r['min_length_m']}m，不得放行")
            defects = c.execute(
                'SELECT d.*, t.category FROM defect d JOIN defect_type t ON t.id=d.type_id '
                'WHERE d.roll_id=? AND d.version_no<=?',
                (roll_id, max(version, 1))).fetchall()
            # 只看初检疵点的最终状态（新版本中为修复记录）
            initial = [d for d in defects if d['version_no'] == 1]
            for d in initial:
                if d['status'] == '误判':
                    continue
                if d['status'] == '复检确认':
                    if d['grade'] == 'A':
                        blockers.append(f"#{d['id']} A 级疵点未退修修复，不得放行")
                    if not d['photo_file']:
                        blockers.append(f"#{d['id']} 照片缺失，不得放行")
                elif d['status'] == '已修复':
                    # 新版本必须有复检确认记录（修复照片）
                    fixed = c.execute(
                        "SELECT * FROM defect WHERE roll_id=? AND version_no=? "
                        "AND source_segment_id=? AND status='已修复'",
                        (roll_id, version, d['source_segment_id'])).fetchall()
                    if not fixed:
                        blockers.append(f"#{d['id']} 修复缺少新版本复检记录")
                    for f in fixed:
                        if not f['photo_file']:
                            blockers.append(f"#{d['id']} 修复照片缺失，不得放行")
                else:
                    blockers.append(f"#{d['id']} 疵点状态为 {d['status']}，尚未闭环")
                # 断经强制退修
                if d['category'] == '断经' and d['status'] != '已修复':
                    blockers.append(f"#{d['id']} 断经必须退修修复后方可放行")
            if blockers:
                raise DomainError('放行条件不满足：' + '；'.join(blockers))
            basis = '；'.join(
                f"{d['id']}号{d['grade']}级:{d['status']}" for d in initial) or '无疵点，初检合格'
            cur = c.execute("""
                INSERT INTO release_record(roll_id, version_no, reviewer, decision, basis, created_at)
                VALUES (?,?,?, '放行',?,?)
            """, (roll_id, version, reviewer, basis, iso()))
            c.execute(
                "UPDATE fabric_roll SET status='已放行', released=1 WHERE id=?",
                (roll_id,))
            return {'release_id': int(cur.lastrowid), 'basis': basis}
        return self.tx(work)

    # --- 超时扫描（进程内提醒）---
    def scan_overdue(self) -> list[dict]:
        """把超过 due_at 仍 open 的任务置超时并写提醒。返回新产生的提醒。"""
        produced: list[dict] = []

        def work(c: sqlite3.Connection) -> list[dict]:
            rows = c.execute("""
                SELECT * FROM review_task
                WHERE status IN ('待接收','进行中') AND due_at < ?
            """, (iso(),)).fetchall()
            for t in rows:
                c.execute("UPDATE review_task SET status='超时' WHERE id=?", (t['id'],))
                c.execute(
                    "UPDATE fabric_roll SET status='待复检' WHERE id=?", (t['roll_id'],))
                msg = f"布卷 #{t['roll_id']} 复检任务 #{t['id']} 已超时（时限 {t['due_at']}），请改派"
                c.execute("""
                    INSERT INTO reminder(kind, ref_id, message, created_at)
                    VALUES ('review_overdue', ?,?,?)
                """, (t['id'], msg, iso()))
                produced.append({'task_id': t['id'], 'roll_id': t['roll_id'], 'message': msg})
            return produced
        return self.tx(work)

    def list_reminders(self, only_unread: bool = True) -> list[dict]:
        sql = ('SELECT * FROM reminder WHERE read=0 ORDER BY id DESC LIMIT 50'
               if only_unread else 'SELECT * FROM reminder ORDER BY id DESC LIMIT 50')
        return self.rows_to_dicts(self.query(sql))

    def recreate_task_after_timeout(self, roll_id: int, timeout_sec: Optional[int] = None) -> int:
        """超时任务已终态；允许重新下发一条待接收任务（演示改派流程）。"""
        def work(c: sqlite3.Connection) -> int:
            r = self.require_roll(c, roll_id)
            last = c.execute(
                'SELECT * FROM review_task WHERE roll_id=? ORDER BY id DESC LIMIT 1',
                (roll_id,)).fetchone()
            if not last:
                raise DomainError('该布卷没有复检任务')
            if last['status'] != '超时':
                raise DomainError(f"上一任务状态为 {last['status']}，无需重新下发")
            due = now_ts() + (timeout_sec or REVIEW_TIMEOUT_SEC)
            cur = c.execute("""
                INSERT INTO review_task(roll_id, version_no, status, created_at, due_at)
                VALUES (?,1,'待接收',?,?)
            """, (roll_id, iso(), iso(due)))
            return int(cur.lastrowid)
        return self.tx(work)

    # --- 查询视图 ---
    def roll_detail(self, roll_id: int) -> dict:
        r = self.one("""
            SELECT r.*, c.code color_batch_code, c.color, c.dye_lot,
                   l.code loom_code, l.workshop
            FROM fabric_roll r
            JOIN color_batch c ON c.id=r.color_batch_id
            JOIN loom l ON l.id=r.loom_id
            WHERE r.id=?
        """, (roll_id,))
        if not r:
            raise DomainError('布卷不存在')
        d = dict(r)
        d['versions'] = self.rows_to_dicts(self.query(
            'SELECT * FROM inspection_version WHERE roll_id=? ORDER BY version_no', (roll_id,)))
        d['segments'] = self.rows_to_dicts(self.query(
            'SELECT * FROM segment WHERE roll_id=? ORDER BY start_m', (roll_id,)))
        d['defects'] = self.rows_to_dicts(self.query("""
            SELECT d.*, t.name type_name, t.code type_code, t.category
            FROM defect d JOIN defect_type t ON t.id=d.type_id
            WHERE d.roll_id=? ORDER BY d.pos_m
        """, (roll_id,)))
        d['tasks'] = self.rows_to_dicts(self.query(
            'SELECT * FROM review_task WHERE roll_id=? ORDER BY id', (roll_id,)))
        d['orders'] = self.rows_to_dicts(self.query(
            'SELECT * FROM rework_order WHERE roll_id=? ORDER BY id', (roll_id,)))
        return d

    def review_queue(self) -> list[dict]:
        return self.rows_to_dicts(self.query("""
            SELECT t.*, r.roll_no, r.length_m, r.width_m,
                   (SELECT inspector FROM inspection_version v
                    WHERE v.roll_id=r.id AND v.version_no=1) initial_inspector,
                   (SELECT COUNT(*) FROM defect d WHERE d.roll_id=r.id
                    AND d.version_no=1 AND d.status='待复检') pending
            FROM review_task t JOIN fabric_roll r ON r.id=t.roll_id
            ORDER BY CASE t.status WHEN '进行中' THEN 0 WHEN '待接收' THEN 1 ELSE 2 END, t.id
        """))

    def rework_progress(self) -> list[dict]:
        return self.rows_to_dicts(self.query("""
            SELECT o.*, r.roll_no FROM rework_order o
            JOIN fabric_roll r ON r.id=o.roll_id ORDER BY o.id
        """))

    def releases(self) -> list[dict]:
        return self.rows_to_dicts(self.query("""
            SELECT x.*, r.roll_no FROM release_record x
            JOIN fabric_roll r ON r.id=x.roll_id ORDER BY x.id DESC
        """))

    def defects_map(self) -> list[dict]:
        return self.rows_to_dicts(self.query("""
            SELECT d.*, r.roll_no, r.length_m roll_length, t.name type_name,
                   t.code type_code, t.category
            FROM defect d
            JOIN fabric_roll r ON r.id=d.roll_id
            JOIN defect_type t ON t.id=d.type_id
            WHERE d.version_no=1
            ORDER BY r.roll_no, d.pos_m
        """))

    # --- 照片权限 ---
    def photo_path_for(self, photo_file: str, reviewer: Optional[str],
                       roll_id: Optional[int]) -> str:
        """照片只允许质检/复检人员访问；且只能访问本布卷的照片。
        演示环境以 X-User 头标识身份；无身份拒绝。路径穿越一律拒绝。"""
        if not reviewer:
            raise DomainError('未提供检验人员身份，无权查看疵点照片')
        if not photo_file or '/' in photo_file or '\\' in photo_file or '..' in photo_file:
            raise DomainError('非法照片文件名')
        if roll_id is not None:
            ok = self.one(
                'SELECT 1 FROM defect WHERE roll_id=? AND photo_file=?',
                (roll_id, photo_file))
            if not ok:
                raise DomainError('该照片不属于此布卷，禁止访问')
        full = os.path.join(self.photo_dir, photo_file)
        if not os.path.isfile(full):
            raise DomainError('照片文件缺失')
        return full

    # --- 种子数据 ---
    def seed(self, timed_out_task: bool = True) -> dict:
        return seed_demo(self, timed_out_task=timed_out_task)


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


# ---- 演示数据 ---------------------------------------------------------------

def seed_demo(db: Database, timed_out_task: bool = True) -> dict:
    """创建演示主数据、布卷与照片，并返回对象 id 映射，供一键样例使用。"""
    import json

    def work(c: sqlite3.Connection) -> dict:
        ids: dict[str, Any] = {}
        if c.execute('SELECT COUNT(*) n FROM loom').fetchone()['n']:
            return {'skipped': True}
        for code, ws in [('LM-101', '一车间'), ('LM-102', '一车间'), ('LM-207', '二车间')]:
            c.execute("INSERT INTO loom(code, workshop) VALUES (?,?)", (code, ws))
        for code, color, lot in [('CB-A21', '亚麻原色', 'DYE-77'),
                                 ('CB-A22', '雾灰', 'DYE-78'),
                                 ('CB-B09', '锈红', 'DYE-80')]:
            c.execute("INSERT INTO color_batch(code, color, dye_lot) VALUES (?,?,?)",
                      (code, color, lot))
        types = [
            ('END_DOWN', '断经', '断经', 'A'),
            ('COLOR_DIFF', '色差', '色差', 'B'),
            ('STAIN', '污渍', '污渍', 'B'),
            ('SHORT_LEN', '长度不足', '长度不足', 'B'),
            ('MISS_END', '缺经', '断经', 'B'),
            ('SLUB', '竹节纱', '其他', 'C'),
        ]
        type_ids = {}
        for code, name, cat, sev in types:
            cur = c.execute(
                "INSERT INTO defect_type(code,name,category,severity) VALUES (?,?,?,?)",
                (code, name, cat, sev))
            type_ids[code] = int(cur.lastrowid)
        ids['type_ids'] = type_ids

        def new_roll(no, length, width, cb, lm, min_len=0):
            cur = c.execute("""
                INSERT INTO fabric_roll(roll_no,length_m,width_m,color_batch_id,loom_id,
                                        min_length_m,created_at)
                VALUES (?,?,?,
                    (SELECT id FROM color_batch WHERE code=?),
                    (SELECT id FROM loom WHERE code=?),?,?)
            """, (no, length, width, cb, lm, min_len, iso()))
            return int(cur.lastrowid)

        r1 = new_roll('BR-20261001-01', 120.0, 1.50, 'CB-A21', 'LM-101', 110)
        r2 = new_roll('BR-20261001-02', 96.5, 1.48, 'CB-A22', 'LM-102', 98)
        r3 = new_roll('BR-20261001-03', 130.0, 1.52, 'CB-B09', 'LM-207', 120)
        ids.update(roll1=r1, roll2=r2, roll3=r3)

        def begin(rid, inspector):
            cur = c.execute("""
                INSERT INTO inspection_version(roll_id,version_no,kind,inspector,created_at)
                VALUES (?,1,'initial',?,?)""", (rid, inspector, iso()))
            c.execute("UPDATE fabric_roll SET status='检验中', current_version=1 WHERE id=?", (rid,))
            return int(cur.lastrowid)

        def seg(vid, rid, a, b, insp):
            cur = c.execute("""
                INSERT INTO segment(version_id,roll_id,start_m,end_m,inspector,created_at)
                VALUES (?,?,?,?,?,?)""", (vid, rid, a, b, insp, iso()))
            return int(cur.lastrowid)

        def defect(rid, sid, tid, pos, length, grade, photo, note):
            cur = c.execute("""
                INSERT INTO defect(roll_id,version_no,source_segment_id,type_id,pos_m,length_m,
                                   grade,status,photo_file,note,created_at)
                VALUES (?,1,?,?,?,?,?, '待复检',?,?,?)
            """, (rid, sid, tid, pos, length, grade, photo, note, iso()))
            return int(cur.lastrowid)

        # 卷1：初检完成，有断经(A) + 污渍(B)，待复检
        v1 = begin(r1, '王检')
        s1 = seg(v1, r1, 0, 60, '王检')
        s2 = seg(v1, r1, 60, 120, '王检')
        d1 = defect(r1, s1, type_ids['END_DOWN'], 18.2, 0.6, 'A',
                    f'r{r1}_d1.svg', '整幅断经，约3根')
        d2 = defect(r1, s2, type_ids['STAIN'], 88.0, 0.3, 'B',
                    f'r{r1}_d2.svg', '边部油污')
        c.execute("UPDATE fabric_roll SET status='待复检' WHERE id=?", (r1,))
        due = now_ts() + REVIEW_TIMEOUT_SEC
        cur = c.execute("""
            INSERT INTO review_task(roll_id,version_no,status,created_at,due_at)
            VALUES (?,1,'待接收',?,?)""", (r1, iso(), iso(due)))
        ids['task1'] = int(cur.lastrowid)
        ids.update(seg1=s1, seg2=s2, defect_enddown=d1, defect_stain=d2)

        # 卷2：色差(B,无照片→照片缺失场景) + 长度不足(B)，任务已超时
        v2 = begin(r2, '李检')
        s3 = seg(v2, r2, 0, 96.5, '李检')
        d3 = defect(r2, s3, type_ids['COLOR_DIFF'], 40.0, 1.2, 'B', None,
                    '与标样有色光差异，待补拍')
        d4 = defect(r2, s3, type_ids['SHORT_LEN'], 95.0, 1.5, 'B',
                    f'r{r2}_d4.svg', '实测 95.0m，订单要求≥98m')
        c.execute("UPDATE fabric_roll SET status='待复检' WHERE id=?", (r2,))
        past = iso(now_ts() - 60)  # 已过期 1 分钟
        cur = c.execute("""
            INSERT INTO review_task(roll_id,version_no,status,created_at,due_at)
            VALUES (?,1,'待接收',?,?)""", (r2, iso(), past))
        ids['task2_overdue'] = int(cur.lastrowid)
        ids.update(defect_color=d3, defect_short=d4)
        if timed_out_task:
            c.execute("UPDATE review_task SET status='超时' WHERE id=?",
                      (ids['task2_overdue'],))
            c.execute("""INSERT INTO reminder(kind,ref_id,message,created_at)
                         VALUES ('review_overdue',?,?,?)""",
                      (ids['task2_overdue'],
                       f'布卷 #{r2} 复检任务 #{ids["task2_overdue"]} 已超时（种子数据），请改派',
                       iso()))

        # 卷3：仅 C 级竹节纱，已走完 复检→放行
        v3 = begin(r3, '王检')
        s4 = seg(v3, r3, 0, 130, '王检')
        d5 = defect(r3, s4, type_ids['SLUB'], 55.0, 0.1, 'C',
                    f'r{r3}_d5.svg', '轻微竹节')
        c.execute("UPDATE defect SET status='复检确认' WHERE id=?", (d5,))
        cur = c.execute("""
            INSERT INTO review_task(roll_id,version_no,status,reviewer,line,
                                    created_at,claimed_at,due_at,submitted_at,disposition)
            VALUES (?,1,'已提交', '周复检','复检二线',?,?,?,?,'误判关闭')
        """, (r3, iso(), iso(), iso(now_ts()+REVIEW_TIMEOUT_SEC), iso()))
        t3 = int(cur.lastrowid)
        c.execute("""
            INSERT INTO release_record(roll_id,version_no,reviewer,decision,basis,created_at)
            VALUES (?,1,'周复检','放行', '5号C级:复检确认',?)""", (r3, iso()))
        c.execute("UPDATE fabric_roll SET status='已放行', released=1 WHERE id=?", (r3,))
        ids.update(defect_slub=d5, task3=t3, roll3_released=True)

        # 卷4：完整“初检(A级断经) → 复检转退修 → v2 修复复检 → 放行”样例
        r4 = new_roll('BR-20261001-04', 110.0, 1.50, 'CB-A21', 'LM-101', 100)
        v4 = begin(r4, '李检')
        s5 = seg(v4, r4, 0, 110, '李检')
        d6 = defect(r4, s5, type_ids['END_DOWN'], 33.0, 0.8, 'A',
                    f'r{r4}_d6.svg', '密路断经')
        t4 = int(c.execute("""
            INSERT INTO review_task(roll_id,version_no,status,reviewer,line,
                                    created_at,claimed_at,due_at,submitted_at,disposition)
            VALUES (?,1,'已提交','赵复检','复检一线',?,?,?,?,'转退修')
        """, (r4, iso(), iso(), iso(now_ts()+REVIEW_TIMEOUT_SEC), iso())).lastrowid)
        o1 = int(c.execute("""
            INSERT INTO rework_order(roll_id,task_id,from_version,to_version,defects,
                                     status,assignee,created_at,started_at,completed_at)
            VALUES (?, ?,1,2,?, '已完成','钱修布',?,?,?)
        """, (r4, t4, json.dumps([d6]), iso(), iso(), iso())).lastrowid)
        v4b = int(c.execute("""
            INSERT INTO inspection_version(roll_id,version_no,kind,inspector,created_at)
            VALUES (?,2,'rework','钱修布',?)""", (r4, iso())).lastrowid)
        # v2 修复记录：source_segment_id 引用初检段 s5
        c.execute("""
            INSERT INTO defect(roll_id,version_no,source_segment_id,type_id,pos_m,length_m,
                               grade,status,photo_file,note,created_at)
            VALUES (?,2,?,?,33.0,0.8,'A','已修复',?, '接头修复并复检',?)
        """, (r4, s5, type_ids['END_DOWN'], f'r{r4}_d6_fixed.svg', iso()))
        c.execute("UPDATE defect SET status='已修复' WHERE id=?", (d6,))
        c.execute("UPDATE fabric_roll SET status='已放行', released=1, current_version=2 WHERE id=?",
                  (r4,))
        c.execute("""
            INSERT INTO release_record(roll_id,version_no,reviewer,decision,basis,created_at)
            VALUES (?,2,'赵复检','放行', '6号A级:已修复（v2 复检确认）',?)""", (r4, iso()))
        ids.update(roll4=r4, defect_enddown2=d6, task4=t4, order1=o1, version4b=v4b)

        # 卷5：初检进行中（检验录入样例：已录一段、一个疵点，尚未结束初检）
        r5 = new_roll('BR-20261001-05', 88.0, 1.46, 'CB-A22', 'LM-102', 80)
        v5 = begin(r5, '王检')
        s6 = seg(v5, r5, 0, 40, '王检')
        d7 = defect(r5, s6, type_ids['MISS_END'], 12.5, 0.2, 'B',
                    f'r{r5}_d7.svg', '边部缺经 1 根')
        ids.update(roll5=r5, seg5=s6, defect_miss=d7, version5=v5)
        return ids
    ids = db.tx(work)
    if not ids.get('skipped'):
        _write_demo_photos(db.photo_dir, ids)
    return ids


def _write_demo_photos(directory: str, ids: dict) -> None:
    """生成演示照片（SVG 占位图：织物纹理 + 疵点标记）。"""
    specs = {
        ids['roll1']: [
            ('d1', ids['defect_enddown'], '断经 18.2m', '#9a4b32'),
            ('d2', ids['defect_stain'], '污渍 88.0m', '#5b5147'),
        ],
        ids['roll2']: [
            ('d4', ids['defect_short'], '长度不足 95.0m', '#9a4b32'),
        ],
        ids['roll3']: [
            ('d5', ids['defect_slub'], '竹节纱 55.0m', '#8a7a5f'),
        ],
        ids['roll4']: [
            ('d6', ids['defect_enddown2'], '断经 33.0m（初检）', '#9a4b32'),
            ('d6_fixed', ids['defect_enddown2'], '断经 33.0m 修复后（v2）', '#4e6b4a'),
        ],
        ids['roll5']: [
            ('d7', ids['defect_miss'], '缺经 12.5m', '#8a7a5f'),
        ],
    }
    for roll_id, items in specs.items():
        for tag, defect_id, label, mark in items:
            name = f'r{roll_id}_{tag}.svg'
            svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='320' height='240'>
<rect width='320' height='240' fill='#e8ddc7'/>
<g stroke='#c9bda2' stroke-width='1'>
{''.join(f"<line x1='{x}' y1='0' x2='{x}' y2='240'/>" for x in range(0, 321, 12))}
{''.join(f"<line x1='0' y1='{y}' x2='320' y2='{y}'/>" for y in range(0, 241, 12))}
</g>
<rect x='70' y='80' width='180' height='80' rx='6' fill='none' stroke='{mark}' stroke-width='4' stroke-dasharray='8 6'/>
<text x='160' y='128' font-size='20' text-anchor='middle' fill='#3f4a52' font-family='sans-serif'>{label}</text>
<text x='160' y='210' font-size='13' text-anchor='middle' fill='#6b6256' font-family='sans-serif'>演示照片 defect #{defect_id}</text>
</svg>"""
            with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                f.write(svg)
