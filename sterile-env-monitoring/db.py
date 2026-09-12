"""SQLite 持久化层：连接、schema 与领域小工具。"""
import os
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("EMR_DB", os.path.join(BASE_DIR, "data.db"))

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 产线
CREATE TABLE IF NOT EXISTS lines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    area       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 环境监测点：关联产线 + 限值（温度/湿度/压差/粒子，支持单边限值）
CREATE TABLE IF NOT EXISTS monitoring_points (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id    INTEGER NOT NULL REFERENCES lines(id),
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    parameter  TEXT NOT NULL CHECK (parameter IN ('temperature', 'humidity', 'pressure', 'particle')),
    unit       TEXT NOT NULL,
    limit_min  REAL,
    limit_max  REAL,
    created_at TEXT NOT NULL,
    CHECK (limit_min IS NOT NULL OR limit_max IS NOT NULL)
);

-- 生产批次：in_production（在产）/ released（已放行）
CREATE TABLE IF NOT EXISTS batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id      INTEGER NOT NULL REFERENCES lines(id),
    batch_no     TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    spec         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'in_production' CHECK (status IN ('in_production', 'released')),
    created_at   TEXT NOT NULL,
    released_at  TEXT,
    released_by  TEXT
);

-- 超限事件：由超限读数自动生成，关联监测点与（在产）批次
CREATE TABLE IF NOT EXISTS events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_no           TEXT UNIQUE,
    point_id           INTEGER NOT NULL REFERENCES monitoring_points(id),
    batch_id           INTEGER REFERENCES batches(id),
    trigger_reading_id INTEGER,
    status             TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    cause              TEXT,
    measures           TEXT,
    disposition_by     TEXT,
    disposition_at     TEXT,
    closed_at          TEXT,
    closed_by          TEXT,
    created_at         TEXT NOT NULL
);

-- 监测读数：常规读数；is_retest=1 时表示针对某事件的复测读数
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id    INTEGER NOT NULL REFERENCES monitoring_points(id),
    event_id    INTEGER REFERENCES events(id),
    value       REAL NOT NULL,
    exceeded    INTEGER NOT NULL DEFAULT 0,
    is_retest   INTEGER NOT NULL DEFAULT 0,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_readings_point ON readings(point_id);
CREATE INDEX IF NOT EXISTS idx_readings_event ON readings(event_id);
CREATE INDEX IF NOT EXISTS idx_events_batch ON events(batch_id, status);
"""


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def is_exceeded(point, value):
    """按监测点限值判定读数是否超限（支持单边限值）。"""
    lo, hi = point["limit_min"], point["limit_max"]
    if lo is not None and value < lo:
        return True
    if hi is not None and value > hi:
        return True
    return False
