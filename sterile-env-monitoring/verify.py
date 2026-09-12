#!/usr/bin/env python3
"""端到端业务规则校验：以临时数据库启动独立实例，逐项断言核心规则。

用法：python3 verify.py
覆盖：正常读数、超限自动事件、放行拦截、原因措施、复测仍超限继续拦截、
      复测合格后关闭、关闭后放行成功、已放行批次不能补挂事件、输入校验。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
HERE = os.path.dirname(os.path.abspath(__file__))

CHECKS = []


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def check(name, cond, extra=""):
    CHECKS.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  -> {extra}"))


def run_checks():
    # ---- 页面与静态资源 ----
    s, _ = call("GET", "/api/health")
    check("健康检查 /api/health", s == 200)
    with urllib.request.urlopen(BASE + "/", timeout=5) as resp:
        html = resp.read().decode()
    check("前端首页可访问", resp.status == 200 and "超限事件" in html)
    with urllib.request.urlopen(BASE + "/static/app.js", timeout=5) as resp:
        check("前端脚本可访问", resp.status == 200)

    # ---- 演示数据（正常 / 超限 / 关闭 / 拦截 四类场景）----
    s, ov = call("GET", "/api/overview")
    d = ov["data"]
    check("演示数据：6 个监测点", d["counts"]["points"] == 6, d["counts"])
    check("演示数据：1 起未关闭事件", d["counts"]["open_events"] == 1, d["counts"])
    check("演示数据：1 起已关闭事件", d["counts"]["closed_events"] == 1, d["counts"])

    s, batches = call("GET", "/api/batches")
    bl = {b["batch_no"]: b for b in batches["data"]["batches"]}
    b1, b2, b3, b4 = bl["B2026-0901"], bl["B2026-0902"], bl["B2026-0903"], bl["B2026-0831"]
    check("B2026-0901 因未关闭事件被拦截", b1["can_release"] is False and b1["open_event_count"] == 1, b1)
    check("B2026-0902 事件已关闭、可放行", b2["can_release"] is True, b2)
    check("B2026-0903 读数正常、可放行", b3["can_release"] is True, b3)
    check("B2026-0831 为已放行批次", b4["status"] == "released", b4)

    # ---- 放行拦截：未关闭事件阻止提交放行 ----
    s, r = call("POST", f"/api/batches/{b1['id']}/submit-release", {"operator": "王质量"})
    check("未关闭事件拦截批次提交放行（409 BATCH_BLOCKED）",
          s == 409 and r["error"]["code"] == "BATCH_BLOCKED", r)
    blocking = r["error"]["details"]["blocking_events"]
    check("拦截响应列出未关闭事件", len(blocking) == 1 and blocking[0]["status"] == "open", blocking)

    # ---- 正常读数：合格、不生成事件 ----
    s, points = call("GET", "/api/points")
    pts = {p["code"]: p for p in points["data"]["points"]}
    s, r = call("POST", "/api/readings", {"point_id": pts["P-HUM-01"]["id"], "value": 50, "recorded_by": "张监测"})
    check("正常读数合格且不生成事件",
          s == 200 and r["data"]["reading"]["exceeded"] is False and r["data"]["event"] is None, r)

    # ---- 超限读数：自动生成事件，关联在产批次而非已放行批次 ----
    s, r = call("POST", "/api/readings", {"point_id": pts["P-TEMP-02"]["id"], "value": 29.0, "recorded_by": "李监测"})
    ev = r["data"]["event"]
    check("超限读数自动生成未关闭事件", s == 200 and ev is not None and ev["status"] == "open", r)
    check("事件关联该产线当前在产批次 B2026-0903", ev and ev["batch_no"] == "B2026-0903", ev)
    s, r = call("GET", f"/api/events?batch_id={b4['id']}")
    check("已放行批次 B2026-0831 未被补挂任何事件", s == 200 and r["data"]["events"] == [], r)
    s, r = call("POST", f"/api/batches/{b3['id']}/submit-release", {"operator": "王质量"})
    check("新事件立即拦截其关联批次 B2026-0903", s == 409 and r["error"]["code"] == "BATCH_BLOCKED", r)

    # ---- 事件关闭规则：复测仍超限 → 禁止关闭、继续拦截 ----
    s, events = call("GET", "/api/events?status=open")
    temp_ev = [e for e in events["data"]["events"] if e["point_code"] == "P-TEMP-01"][0]
    s, r = call("POST", f"/api/events/{temp_ev['id']}/close", {"operator": "王质量"})
    check("复测仍超限的事件禁止关闭（409 EVENT_NOT_CLOSABLE）",
          s == 409 and r["error"]["code"] == "EVENT_NOT_CLOSABLE", r)

    s, r = call("POST", f"/api/events/{temp_ev['id']}/disposition",
                {"cause": "表冷器电磁阀故障", "measures": "切换备用机组并维修", "operator": "王质量"})
    check("质量人员可记录原因和措施", s == 200 and r["data"]["event"]["cause"] == "表冷器电磁阀故障", r)

    s, r = call("POST", "/api/readings",
                {"point_id": pts["P-TEMP-01"]["id"], "value": 27.3, "recorded_by": "张监测", "event_id": temp_ev["id"]})
    check("复测仍超限：事件保持未关闭且不可关闭",
          s == 200 and r["data"]["retest_passed"] is False and r["data"]["can_close"] is False, r)
    s, r = call("POST", f"/api/batches/{b1['id']}/submit-release", {"operator": "王质量"})
    check("复测仍超限的事件继续拦截批次", s == 409, r)

    # ---- 复测合格 → 允许关闭 → 放行成功 ----
    s, r = call("POST", "/api/readings",
                {"point_id": pts["P-TEMP-01"]["id"], "value": 23.4, "recorded_by": "张监测", "event_id": temp_ev["id"]})
    check("复测合格后事件满足关闭条件",
          s == 200 and r["data"]["retest_passed"] is True and r["data"]["can_close"] is True, r)
    s, r = call("POST", f"/api/events/{temp_ev['id']}/close", {"operator": "王质量"})
    check("事件关闭成功", s == 200 and r["data"]["event"]["status"] == "closed", r)
    s, r = call("POST", f"/api/batches/{b1['id']}/submit-release", {"operator": "王质量"})
    check("事件关闭后批次放行成功", s == 200 and r["data"]["batch"]["status"] == "released", r)
    s, r = call("POST", f"/api/batches/{b1['id']}/submit-release", {"operator": "王质量"})
    check("重复放行被拒绝（409 ALREADY_RELEASED）", s == 409 and r["error"]["code"] == "ALREADY_RELEASED", r)
    s, r = call("POST", "/api/readings",
                {"point_id": pts["P-TEMP-01"]["id"], "value": 23.0, "recorded_by": "张监测", "event_id": temp_ev["id"]})
    check("已关闭事件拒绝复测读数（409 EVENT_CLOSED）", s == 409 and r["error"]["code"] == "EVENT_CLOSED", r)

    # ---- 未记录原因措施禁止关闭 ----
    s, events = call("GET", "/api/events?status=open")
    ev2 = [e for e in events["data"]["events"] if e["point_code"] == "P-TEMP-02"][0]
    s, r = call("POST", "/api/readings",
                {"point_id": pts["P-TEMP-02"]["id"], "value": 22.0, "recorded_by": "李监测", "event_id": ev2["id"]})
    check("复测读数登记成功", s == 200 and r["data"]["retest_passed"] is True, r)
    s, r = call("POST", f"/api/events/{ev2['id']}/close", {"operator": "王质量"})
    check("未记录原因措施禁止关闭（409）", s == 409 and "原因" in r["error"]["message"], r)

    # ---- 输入校验 ----
    s, r = call("POST", "/api/readings", {"point_id": 9999, "value": 1})
    check("不存在的监测点返回 404", s == 404, r)
    s, r = call("POST", "/api/points", {"line_id": 1, "code": "X-1", "name": "x", "parameter": "temperature"})
    check("未设限值的监测点被拒绝（400）", s == 400, r)
    s, r = call("POST", "/api/readings", {"point_id": pts["P-HUM-01"]["id"], "value": "abc"})
    check("非数字读数被拒绝（400）", s == 400, r)


def main():
    tmp = tempfile.mkdtemp(prefix="emr-verify-")
    env = dict(os.environ, EMR_DB=os.path.join(tmp, "test.db"), PORT=str(PORT))
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "server.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            try:
                s, _ = call("GET", "/api/health")
                if s == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            print("服务启动失败")
            return 1
        run_checks()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
