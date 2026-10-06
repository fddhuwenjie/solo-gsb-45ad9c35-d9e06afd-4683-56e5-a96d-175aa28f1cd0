"""SQLite 连接与建表。"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "correlation.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS borehole (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    x           REAL    NOT NULL DEFAULT 0,
    collar_elev REAL    NOT NULL,
    note        TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS layer (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    borehole_id INTEGER NOT NULL REFERENCES borehole(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    top        REAL    NOT NULL,          -- 层顶深度（自孔口向下，米）
    bottom     REAL    NOT NULL,          -- 层底深度（自孔口向下，米）
    lithology  TEXT    NOT NULL,
    grain      TEXT    NOT NULL DEFAULT '未知',
    water      TEXT    NOT NULL DEFAULT '未知',
    confidence REAL    NOT NULL DEFAULT 1.0,
    UNIQUE (borehole_id, seq)
);

CREATE TABLE IF NOT EXISTS marker (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT    NOT NULL UNIQUE,
    description TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS marker_layer (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    marker_id  INTEGER NOT NULL REFERENCES marker(id) ON DELETE CASCADE,
    borehole_id INTEGER NOT NULL REFERENCES borehole(id) ON DELETE CASCADE,
    layer_id   INTEGER NOT NULL REFERENCES layer(id) ON DELETE CASCADE,
    UNIQUE (marker_id, borehole_id)
);

CREATE TABLE IF NOT EXISTS settings (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS run_result (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT,
    generated_at TEXT,
    run_id  TEXT
);

-- 人工定案：只从属于某次计算（run_id）；重新计算 / 层段 / 标志层 / 参数
-- 变化后 run_result 换新 run_id 并清空本表，旧定案不再挂到任何结果上。
CREATE TABLE IF NOT EXISTS decision (
    run_id         TEXT    NOT NULL,
    hole_id        INTEGER NOT NULL,
    solution_index INTEGER NOT NULL,          -- 选定 tie_solutions 的下标
    payload        TEXT    NOT NULL,          -- 定案展示体（匹配对/未匹配层全量明细）
    decided_at     TEXT,
    PRIMARY KEY (run_id, hole_id)
);
"""

DEFAULT_SETTINGS = {
    "weights": {
        "lithology": 0.45,
        "grain": 0.15,
        "water": 0.10,
        "elev": 0.20,
        "confidence": 0.10,
    },
    "elev_scale": 5.0,      # 高程差特征尺度（米），差 5m 时高程相似度≈0.37
    "gap_open": 2.0,        # 开间隙惩罚（相似度单位）
    "gap_extend": 1.0,      # 续间隙惩罚
}


def get_db():
    # isolation_level=None：关闭 sqlite3 模块的隐式事务，事务边界由路由
    # 显式控制（否则 SELECT 与后续写入分属不同事务，读-校验-写之间会被
    # 其他连接的提交插入）。timeout=30：写锁被其他连接/进程持有时最多
    # 等待 30s（busy_timeout），多 gunicorn worker 下写请求彼此排队，
    # 而不是立即报 database is locked。
    conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def begin_write(conn):
    """开启写事务并立即取得数据库级写锁（BEGIN IMMEDIATE）。

    写锁跨连接、跨进程生效：本事务提交或回滚之前，其他连接的写事务只能
    排队等待（busy_timeout 内），因此事务内的“读-校验-写”不会被其他
    写者（编辑 / 重算 / 定案）的提交插入；首个读语句看到的就是最新已
    提交状态，且提交前不会有其他写者再改变它。
    """
    conn.execute("BEGIN IMMEDIATE")


def begin_read(conn):
    """开启只读快照事务：首个 SELECT 起持有共享锁，此后多语句读到同一
    快照；不阻塞其他读者，也不阻塞写者开始（仅在其提交瞬间短暂互斥）。"""
    conn.execute("BEGIN")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        # 旧库迁移：run_result 增加 run_id（历史结果没有定案代次标识）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(run_result)")}
        if "run_id" not in cols:
            conn.execute("ALTER TABLE run_result ADD COLUMN run_id TEXT")
        import json
        row = conn.execute("SELECT payload FROM settings WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO settings (id, payload) VALUES (1, ?)",
                (json.dumps(DEFAULT_SETTINGS, ensure_ascii=False),),
            )
        conn.commit()
    finally:
        conn.close()
