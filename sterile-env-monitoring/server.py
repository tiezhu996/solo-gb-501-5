#!/usr/bin/env python3
"""无菌包装环境监测与批次放行联动子系统 —— HTTP 服务与 REST API。

运行：
    python3 server.py            # 默认端口 8000（PORT 环境变量可覆盖）
    python3 server.py --reset    # 清空数据库并重新写入演示数据

业务规则：
1. 监测点关联产线与限值（温度/湿度/压差/粒子，支持单边限值）。
2. 登记常规读数超限时自动生成事件，事件关联该产线当前在产批次；
   已放行批次不会被补挂事件。
3. 批次存在未关闭事件（含复测仍超限）时禁止提交放行。
4. 事件关闭条件：已记录原因与纠正措施，且事件下最近一次复测读数合格。
"""
import json
import math
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from db import DB_PATH, connect, init_db, is_exceeded, now_str
from seed import seed_if_empty

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

PARAMETERS = {
    "temperature": {"label": "温度", "unit": "°C"},
    "humidity": {"label": "湿度", "unit": "%RH"},
    "pressure": {"label": "压差", "unit": "Pa"},
    "particle": {"label": "悬浮粒子", "unit": "个/m³"},
}


class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


# ---------------------------------------------------------------- 序列化 helpers

def fmt_num(v):
    return f"{v:g}"


def limit_text(p):
    lo, hi, unit = p["limit_min"], p["limit_max"], p["unit"]
    if lo is not None and hi is not None:
        return f"{fmt_num(lo)} ~ {fmt_num(hi)} {unit}"
    if lo is not None:
        return f"≥ {fmt_num(lo)} {unit}"
    return f"≤ {fmt_num(hi)} {unit}"


def point_dict(row):
    d = dict(row)
    d["parameter_label"] = PARAMETERS[d["parameter"]]["label"]
    d["limit_text"] = limit_text(d)
    return d


def reading_dict(row):
    d = dict(row)
    d["parameter_label"] = PARAMETERS[d["parameter"]]["label"]
    d["limit_text"] = limit_text(d)
    d["exceeded"] = bool(d["exceeded"])
    d["is_retest"] = bool(d["is_retest"])
    return d


def batch_dict(row):
    d = dict(row)
    d["status_label"] = "已放行" if d["status"] == "released" else "在产"
    d["can_release"] = d["status"] == "in_production" and d["open_event_count"] == 0
    return d


def close_blockers(event, retests):
    """事件关闭的阻塞原因列表（为空表示可关闭）。"""
    blockers = []
    if not (event["cause"] and event["measures"]):
        blockers.append("尚未记录原因和纠正措施")
    if not retests:
        blockers.append("尚无复测读数")
    elif retests[0]["exceeded"]:
        blockers.append(f"最近一次复测仍超限（{fmt_num(retests[0]['value'])} {event['unit']}）")
    return blockers


def event_dict(conn, row):
    d = dict(row)
    d["parameter_label"] = PARAMETERS[d["parameter"]]["label"]
    d["limit_text"] = limit_text(d)
    d["status_label"] = "已关闭" if d["status"] == "closed" else "未关闭"
    retests = conn.execute(
        "SELECT * FROM readings WHERE event_id=? AND is_retest=1 ORDER BY id DESC", (d["id"],)
    ).fetchall()
    d["retest_count"] = len(retests)
    d["last_retest"] = (
        {"value": retests[0]["value"], "exceeded": bool(retests[0]["exceeded"]), "recorded_at": retests[0]["recorded_at"]}
        if retests else None
    )
    blockers = close_blockers(d, retests) if d["status"] == "open" else []
    d["close_blockers"] = blockers
    d["can_close"] = d["status"] == "open" and not blockers
    return d


# ---------------------------------------------------------------- SQL 片段

POINT_SQL = """
SELECT p.*, l.name AS line_name, l.code AS line_code,
       (SELECT COUNT(*) FROM readings r WHERE r.point_id = p.id) AS reading_count
FROM monitoring_points p JOIN lines l ON l.id = p.line_id
"""

READING_SQL = """
SELECT r.*, p.code AS point_code, p.name AS point_name, p.parameter, p.unit,
       p.limit_min, p.limit_max, e.event_no
FROM readings r
JOIN monitoring_points p ON p.id = r.point_id
LEFT JOIN events e ON e.id = r.event_id
"""

EVENT_SQL = """
SELECT e.*, p.code AS point_code, p.name AS point_name, p.parameter, p.unit,
       p.limit_min, p.limit_max, p.line_id,
       l.name AS line_name, l.code AS line_code,
       b.batch_no AS batch_no, b.status AS batch_status,
       tr.value AS trigger_value, tr.recorded_by AS trigger_by, tr.recorded_at AS trigger_at
FROM events e
JOIN monitoring_points p ON p.id = e.point_id
JOIN lines l ON l.id = p.line_id
LEFT JOIN batches b ON b.id = e.batch_id
JOIN readings tr ON tr.id = e.trigger_reading_id
"""

BATCH_SQL = """
SELECT b.*, l.name AS line_name, l.code AS line_code,
       (SELECT COUNT(*) FROM events e WHERE e.batch_id = b.id AND e.status = 'open') AS open_event_count
FROM batches b JOIN lines l ON l.id = b.line_id
"""


def load_point(conn, pid):
    row = conn.execute(POINT_SQL + " WHERE p.id=?", (pid,)).fetchone()
    return point_dict(row) if row else None


def load_reading(conn, rid):
    row = conn.execute(READING_SQL + " WHERE r.id=?", (rid,)).fetchone()
    return reading_dict(row) if row else None


def load_event(conn, eid):
    row = conn.execute(EVENT_SQL + " WHERE e.id=?", (eid,)).fetchone()
    return event_dict(conn, row) if row else None


def load_batch(conn, bid):
    row = conn.execute(BATCH_SQL + " WHERE b.id=?", (bid,)).fetchone()
    return batch_dict(row) if row else None


# ---------------------------------------------------------------- 输入校验

def require(body, key, label):
    v = body.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ApiError(400, "VALIDATION", f"{label}不能为空")
    return v


def to_int(v, label):
    try:
        if isinstance(v, bool):
            raise ValueError
        return int(v)
    except (TypeError, ValueError):
        raise ApiError(400, "VALIDATION", f"{label}必须是整数")


def to_float(v, label):
    try:
        if isinstance(v, bool):
            raise ValueError
        f = float(v)
    except (TypeError, ValueError):
        raise ApiError(400, "VALIDATION", f"{label}必须是数字")
    if not math.isfinite(f):
        raise ApiError(400, "VALIDATION", f"{label}必须是有限数字")
    return f


def opt_float(v, label):
    if v is None or v == "":
        return None
    return to_float(v, label)


def opt_str(body, key, default="未署名"):
    return str(body.get(key) or "").strip() or default


def q1(query, key, default=None):
    vals = query.get(key)
    return vals[0] if vals else default


# ---------------------------------------------------------------- 路由

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(pattern), fn))
        return fn
    return deco


@route("GET", r"/api/health")
def health(ctx):
    return {"status": "ok", "time": now_str()}


@route("GET", r"/api/overview")
def overview(ctx):
    c = ctx.conn
    counts = {
        "lines": c.execute("SELECT COUNT(*) n FROM lines").fetchone()["n"],
        "points": c.execute("SELECT COUNT(*) n FROM monitoring_points").fetchone()["n"],
        "readings": c.execute("SELECT COUNT(*) n FROM readings").fetchone()["n"],
        "open_events": c.execute("SELECT COUNT(*) n FROM events WHERE status='open'").fetchone()["n"],
        "closed_events": c.execute("SELECT COUNT(*) n FROM events WHERE status='closed'").fetchone()["n"],
        "batches_in_production": c.execute("SELECT COUNT(*) n FROM batches WHERE status='in_production'").fetchone()["n"],
        "batches_released": c.execute("SELECT COUNT(*) n FROM batches WHERE status='released'").fetchone()["n"],
    }
    batches = [batch_dict(r) for r in c.execute(BATCH_SQL + " ORDER BY b.id DESC")]
    open_events = [event_dict(c, r) for r in c.execute(EVENT_SQL + " WHERE e.status='open' ORDER BY e.id DESC")]
    recent = [event_dict(c, r) for r in c.execute(EVENT_SQL + " ORDER BY e.id DESC LIMIT 6")]
    return {"counts": counts, "batches": batches, "open_events": open_events, "recent_events": recent}


@route("GET", r"/api/lines")
def list_lines(ctx):
    rows = ctx.conn.execute(
        """
        SELECT l.*,
               (SELECT COUNT(*) FROM monitoring_points p WHERE p.line_id = l.id) AS point_count,
               (SELECT COUNT(*) FROM batches b WHERE b.line_id = l.id AND b.status = 'in_production') AS active_batches
        FROM lines l ORDER BY l.id
        """
    ).fetchall()
    return {"lines": [dict(r) for r in rows]}


@route("GET", r"/api/points")
def list_points(ctx):
    sql, args = POINT_SQL, []
    line_id = q1(ctx.query, "line_id")
    if line_id:
        sql += " WHERE p.line_id=?"
        args.append(to_int(line_id, "line_id"))
    rows = ctx.conn.execute(sql + " ORDER BY p.id", args).fetchall()
    return {"points": [point_dict(r) for r in rows]}


@route("POST", r"/api/points")
def create_point(ctx):
    body = ctx.body
    line_id = to_int(require(body, "line_id", "所属产线"), "line_id")
    code = str(require(body, "code", "点位编码")).strip()
    name = str(require(body, "name", "点位名称")).strip()
    parameter = require(body, "parameter", "监测参数")
    if parameter not in PARAMETERS:
        raise ApiError(400, "VALIDATION", "监测参数必须是：" + "、".join(PARAMETERS))
    unit = str(body.get("unit") or "").strip() or PARAMETERS[parameter]["unit"]
    limit_min = opt_float(body.get("limit_min"), "下限")
    limit_max = opt_float(body.get("limit_max"), "上限")
    if limit_min is None and limit_max is None:
        raise ApiError(400, "VALIDATION", "限值至少设置一项（下限或上限）")
    if limit_min is not None and limit_max is not None and limit_min > limit_max:
        raise ApiError(400, "VALIDATION", "下限不能大于上限")
    conn = ctx.conn
    if not conn.execute("SELECT 1 FROM lines WHERE id=?", (line_id,)).fetchone():
        raise ApiError(404, "LINE_NOT_FOUND", "产线不存在")
    if conn.execute("SELECT 1 FROM monitoring_points WHERE code=?", (code,)).fetchone():
        raise ApiError(409, "CODE_EXISTS", f"点位编码 {code} 已存在")
    cur = conn.execute(
        "INSERT INTO monitoring_points(line_id, code, name, parameter, unit, limit_min, limit_max, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (line_id, code, name, parameter, unit, limit_min, limit_max, now_str()),
    )
    return {"point": load_point(conn, cur.lastrowid)}


@route("GET", r"/api/readings")
def list_readings(ctx):
    sql, args, where = READING_SQL, [], []
    point_id = q1(ctx.query, "point_id")
    event_id = q1(ctx.query, "event_id")
    if point_id:
        where.append("r.point_id=?")
        args.append(to_int(point_id, "point_id"))
    if event_id:
        where.append("r.event_id=?")
        args.append(to_int(event_id, "event_id"))
    if where:
        sql += " WHERE " + " AND ".join(where)
    limit = min(to_int(q1(ctx.query, "limit", "50"), "limit"), 200)
    rows = ctx.conn.execute(sql + " ORDER BY r.id DESC LIMIT ?", args + [limit]).fetchall()
    return {"readings": [reading_dict(r) for r in rows]}


@route("POST", r"/api/readings")
def create_reading(ctx):
    """登记读数。

    - 常规读数：超限时自动生成事件并关联该产线当前在产批次（已放行批次不补挂）。
    - 携带 event_id：作为该事件的复测读数，不生成新事件；复测仍超限则事件继续拦截。
    """
    body = ctx.body
    point_id = to_int(require(body, "point_id", "监测点"), "point_id")
    value = to_float(require(body, "value", "读数值"), "读数值")
    recorded_by = opt_str(body, "recorded_by")
    conn = ctx.conn
    point = conn.execute("SELECT * FROM monitoring_points WHERE id=?", (point_id,)).fetchone()
    if not point:
        raise ApiError(404, "POINT_NOT_FOUND", "监测点不存在")
    exceeded = 1 if is_exceeded(point, value) else 0
    now = now_str()

    event_id = body.get("event_id")
    if event_id is not None:
        # —— 复测读数 ——
        event_id = to_int(event_id, "event_id")
        ev = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not ev:
            raise ApiError(404, "EVENT_NOT_FOUND", "事件不存在")
        if ev["status"] != "open":
            raise ApiError(409, "EVENT_CLOSED", f"事件 {ev['event_no']} 已关闭，不能再登记复测")
        if ev["point_id"] != point_id:
            raise ApiError(400, "RETEST_POINT_MISMATCH", "复测读数必须在事件所属监测点登记")
        cur = conn.execute(
            "INSERT INTO readings(point_id, event_id, value, exceeded, is_retest, recorded_by, recorded_at, created_at)"
            " VALUES (?,?,?,?,1,?,?,?)",
            (point_id, event_id, value, exceeded, recorded_by, now, now),
        )
        event = load_event(conn, event_id)
        return {
            "reading": load_reading(conn, cur.lastrowid),
            "event": event,
            "retest_passed": not exceeded,
            "can_close": event["can_close"],
        }

    # —— 常规读数 ——
    cur = conn.execute(
        "INSERT INTO readings(point_id, event_id, value, exceeded, is_retest, recorded_by, recorded_at, created_at)"
        " VALUES (?,NULL,?,?,0,?,?,?)",
        (point_id, value, exceeded, recorded_by, now, now),
    )
    reading_id = cur.lastrowid
    event = None
    if exceeded:
        # 只关联该产线“在产”批次；已放行批次绝不补挂事件
        batch = conn.execute(
            "SELECT * FROM batches WHERE line_id=? AND status='in_production' ORDER BY id DESC LIMIT 1",
            (point["line_id"],),
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO events(point_id, batch_id, trigger_reading_id, status, created_at) VALUES (?,?,?,'open',?)",
            (point_id, batch["id"] if batch else None, reading_id, now),
        )
        event_id = cur.lastrowid
        conn.execute("UPDATE events SET event_no=? WHERE id=?", (f"EV-{event_id:04d}", event_id))
        conn.execute("UPDATE readings SET event_id=? WHERE id=?", (event_id, reading_id))
        event = load_event(conn, event_id)
    return {"reading": load_reading(conn, reading_id), "event": event}


@route("GET", r"/api/events")
def list_events(ctx):
    sql, args, where = EVENT_SQL, [], []
    status = q1(ctx.query, "status")
    if status:
        if status not in ("open", "closed"):
            raise ApiError(400, "VALIDATION", "status 只能是 open 或 closed")
        where.append("e.status=?")
        args.append(status)
    batch_id = q1(ctx.query, "batch_id")
    if batch_id:
        where.append("e.batch_id=?")
        args.append(to_int(batch_id, "batch_id"))
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = ctx.conn.execute(sql + " ORDER BY e.id DESC", args).fetchall()
    return {"events": [event_dict(ctx.conn, r) for r in rows]}


@route("GET", r"/api/events/(?P<id>\d+)")
def get_event(ctx):
    eid = int(ctx.params["id"])
    event = load_event(ctx.conn, eid)
    if not event:
        raise ApiError(404, "EVENT_NOT_FOUND", "事件不存在")
    event["retests"] = [
        reading_dict(r)
        for r in ctx.conn.execute(READING_SQL + " WHERE r.event_id=? AND r.is_retest=1 ORDER BY r.id DESC", (eid,))
    ]
    event["trigger_reading"] = load_reading(ctx.conn, event["trigger_reading_id"])
    return {"event": event}


@route("POST", r"/api/events/(?P<id>\d+)/disposition")
def disposition(ctx):
    """质量人员记录原因与纠正措施。"""
    body = ctx.body
    cause = str(require(body, "cause", "原因分析")).strip()
    measures = str(require(body, "measures", "纠正措施")).strip()
    operator = opt_str(body, "operator")
    conn = ctx.conn
    ev = conn.execute("SELECT * FROM events WHERE id=?", (int(ctx.params["id"]),)).fetchone()
    if not ev:
        raise ApiError(404, "EVENT_NOT_FOUND", "事件不存在")
    if ev["status"] != "open":
        raise ApiError(409, "EVENT_CLOSED", f"事件 {ev['event_no']} 已关闭，不能再修改处置信息")
    conn.execute(
        "UPDATE events SET cause=?, measures=?, disposition_by=?, disposition_at=? WHERE id=?",
        (cause, measures, operator, now_str(), ev["id"]),
    )
    return {"event": load_event(conn, ev["id"])}


@route("POST", r"/api/events/(?P<id>\d+)/close")
def close_event(ctx):
    """关闭事件：必须已记录原因措施，且最近一次复测合格。"""
    conn = ctx.conn
    ev = conn.execute("SELECT * FROM events WHERE id=?", (int(ctx.params["id"]),)).fetchone()
    if not ev:
        raise ApiError(404, "EVENT_NOT_FOUND", "事件不存在")
    if ev["status"] == "closed":
        raise ApiError(409, "EVENT_CLOSED", f"事件 {ev['event_no']} 已关闭，请勿重复操作")
    full = conn.execute(EVENT_SQL + " WHERE e.id=?", (ev["id"],)).fetchone()
    retests = conn.execute(
        "SELECT * FROM readings WHERE event_id=? AND is_retest=1 ORDER BY id DESC", (ev["id"],)
    ).fetchall()
    blockers = close_blockers(full, retests)
    if blockers:
        raise ApiError(409, "EVENT_NOT_CLOSABLE", "事件不满足关闭条件：" + "；".join(blockers), {"blockers": blockers})
    operator = opt_str(ctx.body, "operator")
    cur = conn.execute(
        "UPDATE events SET status='closed', closed_at=?, closed_by=? WHERE id=? AND status='open'",
        (now_str(), operator, ev["id"]),
    )
    if cur.rowcount == 0:
        # 并发下已被其他请求关闭
        raise ApiError(409, "EVENT_CLOSED", f"事件 {ev['event_no']} 已关闭，请勿重复操作")
    return {"event": load_event(conn, ev["id"])}


@route("GET", r"/api/batches")
def list_batches(ctx):
    rows = ctx.conn.execute(BATCH_SQL + " ORDER BY b.id DESC").fetchall()
    return {"batches": [batch_dict(r) for r in rows]}


@route("POST", r"/api/batches")
def create_batch(ctx):
    body = ctx.body
    line_id = to_int(require(body, "line_id", "所属产线"), "line_id")
    batch_no = str(require(body, "batch_no", "批次号")).strip()
    product_name = str(require(body, "product_name", "产品名称")).strip()
    spec = str(body.get("spec") or "").strip()
    conn = ctx.conn
    if not conn.execute("SELECT 1 FROM lines WHERE id=?", (line_id,)).fetchone():
        raise ApiError(404, "LINE_NOT_FOUND", "产线不存在")
    if conn.execute("SELECT 1 FROM batches WHERE batch_no=?", (batch_no,)).fetchone():
        raise ApiError(409, "BATCH_NO_EXISTS", f"批次号 {batch_no} 已存在")
    cur = conn.execute(
        "INSERT INTO batches(line_id, batch_no, product_name, spec, status, created_at) VALUES (?,?,?,?,'in_production',?)",
        (line_id, batch_no, product_name, spec, now_str()),
    )
    return {"batch": load_batch(conn, cur.lastrowid)}


@route("POST", r"/api/batches/(?P<id>\d+)/submit-release")
def submit_release(ctx):
    """提交放行：存在未关闭（含复测仍超限）事件的批次一律拦截。

    状态迁移为原子条件更新（WHERE status='in_production'）：并发提交同一批次时
    仅一个请求生效，其余请求影响行数为 0，返回“已放行”提示。
    """
    conn = ctx.conn
    bid = int(ctx.params["id"])
    batch = conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    if not batch:
        raise ApiError(404, "BATCH_NOT_FOUND", "批次不存在")
    if batch["status"] == "released":
        raise ApiError(409, "ALREADY_RELEASED", f"批次 {batch['batch_no']} 已放行，请勿重复提交")
    blocking = conn.execute(
        EVENT_SQL + " WHERE e.batch_id=? AND e.status='open' ORDER BY e.id", (bid,)
    ).fetchall()
    if blocking:
        events = [event_dict(conn, r) for r in blocking]
        raise ApiError(
            409,
            "BATCH_BLOCKED",
            f"批次 {batch['batch_no']} 存在 {len(events)} 个未关闭的超限事件，禁止提交放行",
            {"blocking_events": events},
        )
    operator = opt_str(ctx.body, "operator")
    cur = conn.execute(
        "UPDATE batches SET status='released', released_at=?, released_by=? WHERE id=? AND status='in_production'",
        (now_str(), operator, bid),
    )
    if cur.rowcount == 0:
        # 并发下已被其他请求放行
        raise ApiError(409, "ALREADY_RELEASED", f"批次 {batch['batch_no']} 已放行，请勿重复提交")
    return {"batch": load_batch(conn, bid)}


# ---------------------------------------------------------------- HTTP 骨架

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "SterileEMR/1.0"

    def log_message(self, *args):  # 保持控制台安静
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api"):
                self._handle_api(method, parsed.path, parse_qs(parsed.query))
            else:
                self._serve_static(parsed.path)
        except ApiError as e:
            self._send_json(e.status, {"ok": False, "error": {"code": e.code, "message": e.message, "details": e.details}})
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send_json(500, {"ok": False, "error": {"code": "INTERNAL", "message": f"服务器内部错误: {e}"}})

    def _handle_api(self, method, path, query):
        body = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    raise ApiError(400, "BAD_JSON", "请求体不是合法 JSON")
                if not isinstance(body, dict):
                    raise ApiError(400, "BAD_JSON", "请求体必须是 JSON 对象")
        conn = connect()
        try:
            for m, pattern, fn in ROUTES:
                if m != method:
                    continue
                match = pattern.fullmatch(path)
                if match:
                    ctx = SimpleNamespace(conn=conn, body=body, query=query, params=match.groupdict())
                    data = fn(ctx)
                    conn.commit()
                    self._send_json(200, {"ok": True, "data": data})
                    return
            raise ApiError(404, "NOT_FOUND", f"接口不存在: {method} {path}")
        except ApiError:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _serve_static(self, path):
        if path in ("/", "/index.html"):
            rel = "index.html"
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
        else:
            self._send_json(404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "资源不存在"}})
            return
        rel = os.path.normpath(rel).lstrip(os.sep)
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.abspath(full).startswith(os.path.abspath(STATIC_DIR)) or not os.path.isfile(full):
            self._send_json(404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "资源不存在"}})
            return
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def main():
    if "--reset" in sys.argv and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = connect()
    init_db(conn)
    seeded = seed_if_empty(conn)
    conn.close()
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("✔ 无菌包装环境监测与批次放行联动子系统")
    print(f"  数据库: {DB_PATH}{'（已写入演示数据）' if seeded else ''}")
    print(f"  访问:   http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
