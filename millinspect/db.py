"""SQLite 连接管理与建表脚本。

所有版本化检验数据都保存在本库；疵点照片文件保存在本地目录
（见 :mod:`millinspect.photos`），库中只记录相对路径。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 织机（来源机台）
CREATE TABLE IF NOT EXISTS looms (
    code        TEXT PRIMARY KEY,          -- 机台号，如 L-12
    name        TEXT NOT NULL,
    workshop    TEXT NOT NULL
);

-- 颜色批次
CREATE TABLE IF NOT EXISTS color_batches (
    code        TEXT PRIMARY KEY,          -- 颜色批次号，如 CB-2401
    color_name  TEXT NOT NULL,             -- 颜色名称
    dye_lot     TEXT NOT NULL              -- 染缸批次
);

-- 布卷台账
CREATE TABLE IF NOT EXISTS bolts (
    code            TEXT PRIMARY KEY,      -- 布卷编号
    length_m        REAL NOT NULL CHECK (length_m > 0),  -- 长度（米）
    width_m         REAL NOT NULL CHECK (width_m > 0),   -- 幅宽（米）
    color_batch     TEXT NOT NULL REFERENCES color_batches(code),
    loom_code       TEXT NOT NULL REFERENCES looms(code),
    received_at     TEXT NOT NULL,         -- 入检时间 ISO
    nominal_length_m REAL,                 -- 标称长度（用于长度不足折价）
    status          TEXT NOT NULL          -- pending/inspecting/reinspecting/repairing/released/rejected
                    CHECK (status IN ('pending','inspecting','reinspecting',
                                      'repairing','released','rejected')),
    current_version INTEGER REFERENCES inspection_versions(id)
);

-- 检验版本：初检 V1，退修后生成 V2、V3……
CREATE TABLE IF NOT EXISTS inspection_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bolt_code       TEXT NOT NULL REFERENCES bolts(code),
    version_no      INTEGER NOT NULL,      -- 1=初检，2+=退修后复检版本
    kind            TEXT NOT NULL CHECK (kind IN ('initial','post_repair')),
    inspector       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    supersedes      INTEGER REFERENCES inspection_versions(id),  -- 退修前的旧版本
    repair_order_id INTEGER,
    UNIQUE (bolt_code, version_no)
);

-- 检验段：沿布卷长度切分的检验区间
CREATE TABLE IF NOT EXISTS segments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id      INTEGER NOT NULL REFERENCES inspection_versions(id),
    seq             INTEGER NOT NULL,      -- 段序号
    start_m         REAL NOT NULL,         -- 起始米数
    end_m           REAL NOT NULL,         -- 结束米数
    inspector       TEXT NOT NULL,
    checked_at      TEXT NOT NULL,
    CHECK (end_m > start_m),
    UNIQUE (version_id, seq)
);

-- 疵点类型字典
CREATE TABLE IF NOT EXISTS defect_types (
    code        TEXT PRIMARY KEY,          -- broken_warp/color_diff/stain/short_length/missing_photo/other
    name        TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('A','B','C'))  -- A 最严重
);

-- 疵点：按米数定位，必须落在所属检验段区间内
CREATE TABLE IF NOT EXISTS defects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id      INTEGER NOT NULL REFERENCES segments(id),
    type_code       TEXT NOT NULL REFERENCES defect_types(code),
    position_m      REAL NOT NULL,         -- 距布卷起点的米数
    description     TEXT NOT NULL DEFAULT '',
    photo_path      TEXT,                  -- 本地照片相对路径；NULL=照片缺失
    resolved_at     TEXT,                  -- 照片缺失补照时间
    recorded_at     TEXT NOT NULL
);

-- 复检任务（复检只能引用原检验段）
CREATE TABLE IF NOT EXISTS reinspection_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bolt_code       TEXT NOT NULL REFERENCES bolts(code),
    version_id      INTEGER NOT NULL REFERENCES inspection_versions(id),  -- 被复检的原版本
    segment_id      INTEGER NOT NULL REFERENCES segments(id),             -- 被引用的原检验段
    trigger_code    TEXT NOT NULL,         -- 触发原因 defect 类型码
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','claimed','completed','timeout','cancelled')),
    assignee        TEXT,                  -- 接收（claim）的复检人
    claimed_at      TEXT,
    due_at          TEXT NOT NULL,         -- 超时时刻
    completed_at    TEXT,
    result          TEXT CHECK (result IS NULL OR result IN ('confirmed','cleared')),
    note            TEXT NOT NULL DEFAULT ''
);

-- 退修工单：退修完成后生成新版本
CREATE TABLE IF NOT EXISTS repair_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bolt_code       TEXT NOT NULL REFERENCES bolts(code),
    from_version_id INTEGER NOT NULL REFERENCES inspection_versions(id),
    new_version_id  INTEGER REFERENCES inspection_versions(id),
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','in_progress','done','cancelled')),
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);

-- 放行记录
CREATE TABLE IF NOT EXISTS releases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bolt_code       TEXT NOT NULL REFERENCES bolts(code),
    version_id      INTEGER NOT NULL REFERENCES inspection_versions(id),
    released_by     TEXT NOT NULL,
    released_at     TEXT NOT NULL,
    grade           TEXT NOT NULL CHECK (grade IN ('A','B','C')),  -- 按最严重疵点定级
    note            TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_segments_version ON segments(version_id);
CREATE INDEX IF NOT EXISTS idx_defects_segment ON defects(segment_id);
CREATE INDEX IF NOT EXISTS idx_tasks_bolt ON reinspection_tasks(bolt_code);
CREATE INDEX IF NOT EXISTS idx_repairs_bolt ON repair_orders(bolt_code);

-- 照片访问令牌：复检人只有在任务已接收且未完成时才能查看对应疵点照片
CREATE TABLE IF NOT EXISTS photo_grants (
    token           TEXT PRIMARY KEY,
    defect_id       INTEGER NOT NULL REFERENCES defects(id),
    task_id         INTEGER NOT NULL REFERENCES reinspection_tasks(id),
    assignee        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);
"""


class _LockingConnection(sqlite3.Connection):
    """自带写事务锁的连接，供多线程共享时串行化 ``tx()``。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tx_lock = threading.Lock()


def connect(db_path: str) -> sqlite3.Connection:
    """打开 SQLite 连接并启用外键。"""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False,
                           factory=_LockingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表（幂等）。"""
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection):
    """串行化事务：用于接收竞争等需要原子性的操作。

    连接可能被多线程共享（``check_same_thread=False``），因此通过挂在连接上的
    进程内锁把写事务串行化；锁内再用 ``BEGIN IMMEDIATE`` 抢占 SQLite 写锁，
    保证“检查状态 + 条件更新”整体原子。
    """
    lock = getattr(conn, "_tx_lock", None)
    if lock is not None:
        lock.acquire()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if lock is not None:
            lock.release()
