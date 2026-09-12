"""演示数据种子：覆盖「正常读数、超限事件、事件关闭、放行拦截」四类场景。

仅在数据库为空时写入（幂等），已有业务数据不会被覆盖。
"""
from db import is_exceeded


def seed_if_empty(conn):
    if conn.execute("SELECT COUNT(*) AS n FROM lines").fetchone()["n"]:
        return False
    _seed(conn)
    conn.commit()
    return True


def _seed(conn):
    # ---- 产线 ----
    for code, name, area in [
        ("L-A1", "无菌灌装一线", "洁净区 A（B 级背景 + A 级层流）"),
        ("L-A2", "无菌灌装二线", "洁净区 B（C 级背景 + A 级层流）"),
    ]:
        conn.execute(
            "INSERT INTO lines(code, name, area, created_at) VALUES (?,?,?,?)",
            (code, name, area, "2026-09-10 08:00:00"),
        )

    # ---- 监测点（关联产线 + 限值）----
    for line_id, code, name, param, unit, lo, hi in [
        (1, "P-TEMP-01", "灌装间温度", "temperature", "°C", 18.0, 26.0),
        (1, "P-HUM-01", "灌装间相对湿度", "humidity", "%RH", 45.0, 65.0),
        (1, "P-DIF-01", "灌装间对走廊压差", "pressure", "Pa", 10.0, None),
        (1, "P-PAR-01", "悬浮粒子（≥0.5μm）", "particle", "个/m³", None, 3520.0),
        (2, "P-TEMP-02", "灌装间温度", "temperature", "°C", 18.0, 26.0),
        (2, "P-DIF-02", "灌装间对走廊压差", "pressure", "Pa", 10.0, None),
    ]:
        conn.execute(
            "INSERT INTO monitoring_points(line_id, code, name, parameter, unit, limit_min, limit_max, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (line_id, code, name, param, unit, lo, hi, "2026-09-10 08:00:00"),
        )

    # ---- 批次：3 个在产 + 1 个已放行 ----
    for line_id, no, product, spec, status, created, rel_at, rel_by in [
        (1, "B2026-0901", "预灌封注射器无菌包装", "1ml × 100000 支", "in_production", "2026-09-11 08:00:00", None, None),
        (1, "B2026-0902", "西林瓶无菌包装", "2ml × 80000 支", "in_production", "2026-09-11 08:10:00", None, None),
        (2, "B2026-0903", "预灌封注射器无菌包装", "1ml × 120000 支", "in_production", "2026-09-12 08:00:00", None, None),
        (2, "B2026-0831", "安瓿瓶无菌包装", "5ml × 60000 支", "released", "2026-08-31 08:00:00", "2026-09-01 16:00:00", "王质量"),
    ]:
        conn.execute(
            "INSERT INTO batches(line_id, batch_no, product_name, spec, status, created_at, released_at, released_by)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (line_id, no, product, spec, status, created, rel_at, rel_by),
        )

    def reading(point_id, value, by, at, event_id=None, is_retest=0):
        point = conn.execute("SELECT * FROM monitoring_points WHERE id=?", (point_id,)).fetchone()
        exceeded = 1 if is_exceeded(point, value) else 0
        cur = conn.execute(
            "INSERT INTO readings(point_id, event_id, value, exceeded, is_retest, recorded_by, recorded_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (point_id, event_id, value, exceeded, is_retest, by, at, at),
        )
        return cur.lastrowid

    def event(point_id, batch_id, trigger_reading_id, at):
        cur = conn.execute(
            "INSERT INTO events(point_id, batch_id, trigger_reading_id, status, created_at) VALUES (?,?,?,'open',?)",
            (point_id, batch_id, trigger_reading_id, at),
        )
        eid = cur.lastrowid
        conn.execute("UPDATE events SET event_no=? WHERE id=?", (f"EV-{eid:04d}", eid))
        conn.execute("UPDATE readings SET event_id=? WHERE id=?", (eid, trigger_reading_id))
        return eid

    # ---- 正常读数（09-11 早班，全部合格）----
    reading(1, 22.5, "张监测", "2026-09-11 08:30:00")
    reading(2, 52.0, "张监测", "2026-09-11 08:30:00")
    reading(3, 12.5, "张监测", "2026-09-11 08:30:00")
    reading(4, 2100, "李监测", "2026-09-11 08:35:00")
    reading(5, 23.1, "李监测", "2026-09-11 08:40:00")
    reading(6, 11.8, "李监测", "2026-09-11 08:40:00")

    # ---- 超限：粒子 3860 > 3520 → EV-0001（关联 B2026-0902，复测合格后关闭）----
    rid = reading(4, 3860, "李监测", "2026-09-11 09:20:00")
    ev1 = event(4, 2, rid, "2026-09-11 09:20:00")
    conn.execute(
        "UPDATE events SET cause=?, measures=?, disposition_by=?, disposition_at=? WHERE id=?",
        (
            "高效过滤器边框密封条老化泄漏，导致灌装间局部粒子计数升高",
            "更换密封条并完成 PAO 检漏；泄漏排查期间暂停灌装，恢复生产后加密监测频次",
            "王质量",
            "2026-09-11 10:00:00",
            ev1,
        ),
    )
    reading(4, 2900, "李监测", "2026-09-11 11:00:00", event_id=ev1, is_retest=1)  # 复测合格
    conn.execute(
        "UPDATE events SET status='closed', closed_at=?, closed_by=? WHERE id=?",
        ("2026-09-11 11:30:00", "王质量", ev1),
    )

    # ---- 超限：温度 28.4 > 26 → EV-0002（关联 B2026-0901，复测仍超限，保持未关闭并拦截放行）----
    rid = reading(1, 28.4, "张监测", "2026-09-11 14:05:00")
    ev2 = event(1, 1, rid, "2026-09-11 14:05:00")
    conn.execute(
        "UPDATE events SET cause=?, measures=?, disposition_by=?, disposition_at=? WHERE id=?",
        (
            "洁净空调表冷器电磁阀故障，制冷量不足导致灌装间温度缓慢上升",
            "已切换备用空调机组并维修故障电磁阀；每 30 分钟加密监测一次，待温度稳定后复测",
            "王质量",
            "2026-09-11 15:00:00",
            ev2,
        ),
    )
    reading(1, 27.1, "张监测", "2026-09-11 16:30:00", event_id=ev2, is_retest=1)  # 复测仍超限 → 继续拦截

    # ---- 次日正常读数（09-12 早班，全部合格）----
    reading(1, 23.2, "张监测", "2026-09-12 08:30:00")
    reading(2, 55.0, "张监测", "2026-09-12 08:30:00")
    reading(3, 11.9, "张监测", "2026-09-12 08:30:00")
    reading(4, 2350, "李监测", "2026-09-12 08:35:00")
    reading(5, 22.8, "李监测", "2026-09-12 08:40:00")
    reading(6, 12.2, "李监测", "2026-09-12 08:40:00")
