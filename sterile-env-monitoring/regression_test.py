#!/usr/bin/env python3
"""环境监测回归测试：独立临时实例、可重复运行、结束自动清理。

覆盖场景：
  合格读数不生成事件 / 超限自动建事件并关联批次 / 未关闭事件拦截放行 /
  原因措施 + 复测合格才能关闭 / 复测仍超限不能关闭并继续拦截 /
  已放行批次不能补挂事件 / 重复放行拒绝 / 并发放行仅一次成功 /
  超限读数与放行并发（拦截或分离二选一，多轮）/ 已关闭事件拒绝复测。

用法：
  python3 regression_test.py              # 运行一轮
  python3 regression_test.py --repeat 3   # 连续运行 3 轮（验证可重复性）

每轮启动独立临时实例（随机空闲端口 + 临时数据库），结束后终止进程并删除数据，
不触碰项目目录下的 data.db，不影响演示数据。
"""
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")


# ---------------- 临时实例生命周期 ----------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def temp_server():
    """启动独立临时实例（随机端口 + 临时数据库），退出时终止并清理。"""
    port = free_port()
    tmp = tempfile.mkdtemp(prefix="emr-regression-")
    env = dict(os.environ, EMR_DB=os.path.join(tmp, "test.db"), PORT=str(port))
    proc = subprocess.Popen([sys.executable, SERVER], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("临时实例启动失败")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- API helper ----------------

def must(cond, msg):
    if not cond:
        raise AssertionError(msg)


class Api:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method)
        data = None
        if body is not None:
            req.add_header("Content-Type", "application/json")
            data = json.dumps(body).encode()
        try:
            with urllib.request.urlopen(req, data=data, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def make_batch(self, line_id, batch_no):
        s, r = self.call("POST", "/api/batches",
                         {"line_id": line_id, "batch_no": batch_no,
                          "product_name": "回归测试批次", "spec": "test"})
        must(s == 200, f"创建批次失败: {r}")
        return r["data"]["batch"]

    def point_id(self, code):
        s, r = self.call("GET", "/api/points")
        for p in r["data"]["points"]:
            if p["code"] == code:
                return p["id"]
        raise AssertionError(f"监测点 {code} 不存在")


# ---------------- 测试用例 ----------------

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@test
def t_normal_reading_no_event(api):
    """合格读数不生成事件"""
    pid = api.point_id("P-HUM-01")
    s, r = api.call("POST", "/api/readings", {"point_id": pid, "value": 50, "recorded_by": "回归"})
    must(s == 200, f"登记失败: {r}")
    must(r["data"]["reading"]["exceeded"] is False, "合格读数被误判超限")
    must(r["data"]["event"] is None, "合格读数不应生成事件")


@test
def t_overlimit_event_links_batch(api):
    """超限自动建事件并关联当前在产批次"""
    api.make_batch(1, "R-OVER-001")
    pid = api.point_id("P-TEMP-01")
    s, r = api.call("POST", "/api/readings", {"point_id": pid, "value": 30.5, "recorded_by": "回归"})
    ev = r["data"]["event"]
    must(s == 200 and ev is not None, "超限读数未生成事件")
    must(r["data"]["reading"]["exceeded"] is True, "超限读数判定错误")
    must(ev["status"] == "open", "新事件应为未关闭")
    must(ev["batch_no"] == "R-OVER-001", f"事件应关联当前在产批次，实际: {ev['batch_no']}")


@test
def t_open_event_blocks_release(api):
    """未关闭事件拦截批次放行"""
    b = api.make_batch(1, "R-BLOCK-001")
    pid = api.point_id("P-TEMP-01")
    api.call("POST", "/api/readings", {"point_id": pid, "value": 31.0, "recorded_by": "回归"})
    s, r = api.call("POST", f"/api/batches/{b['id']}/submit-release", {"operator": "王质量"})
    must(s == 409 and r["error"]["code"] == "BATCH_BLOCKED", f"未关闭事件未拦截放行: {s} {r}")
    evs = r["error"]["details"]["blocking_events"]
    must(len(evs) == 1 and evs[0]["batch_no"] == "R-BLOCK-001", "拦截响应应列出该批次的未关闭事件")


@test
def t_close_requires_disposition_and_passing_retest(api):
    """原因措施 + 复测合格才能关闭；复测仍超限不能关闭并继续拦截"""
    b = api.make_batch(1, "R-CLOSE-001")
    pid = api.point_id("P-TEMP-01")
    _, r = api.call("POST", "/api/readings", {"point_id": pid, "value": 29.5, "recorded_by": "回归"})
    eid = r["data"]["event"]["id"]

    # 无原因措施、无复测 → 不能关闭
    s, r = api.call("POST", f"/api/events/{eid}/close", {"operator": "王质量"})
    must(s == 409 and r["error"]["code"] == "EVENT_NOT_CLOSABLE", "缺少原因措施和复测时不应关闭")

    # 复测合格但未记原因措施 → 仍不能关闭
    s, r = api.call("POST", "/api/readings", {"point_id": pid, "value": 23.0, "recorded_by": "回归", "event_id": eid})
    must(s == 200 and r["data"]["retest_passed"] is True, "复测合格判定错误")
    s, r = api.call("POST", f"/api/events/{eid}/close", {"operator": "王质量"})
    must(s == 409, "未记录原因措施不应关闭")

    # 记录原因措施
    s, r = api.call("POST", f"/api/events/{eid}/disposition",
                    {"cause": "空调故障", "measures": "切换备用机组", "operator": "王质量"})
    must(s == 200, f"记录原因措施失败: {r}")

    # 复测仍超限 → 不能关闭，且继续拦截放行
    s, r = api.call("POST", "/api/readings", {"point_id": pid, "value": 28.0, "recorded_by": "回归", "event_id": eid})
    must(s == 200 and r["data"]["retest_passed"] is False and r["data"]["can_close"] is False,
         "复测仍超限时不应满足关闭条件")
    s, r = api.call("POST", f"/api/events/{eid}/close", {"operator": "王质量"})
    must(s == 409, "复测仍超限不应关闭")
    s, r = api.call("POST", f"/api/batches/{b['id']}/submit-release", {"operator": "王质量"})
    must(s == 409 and r["error"]["code"] == "BATCH_BLOCKED", "复测仍超限的事件应继续拦截放行")

    # 复测合格 → 可关闭 → 放行成功
    s, r = api.call("POST", "/api/readings", {"point_id": pid, "value": 22.5, "recorded_by": "回归", "event_id": eid})
    must(s == 200 and r["data"]["can_close"] is True, "复测合格后应满足关闭条件")
    s, r = api.call("POST", f"/api/events/{eid}/close", {"operator": "王质量"})
    must(s == 200 and r["data"]["event"]["status"] == "closed", "事件应关闭成功")
    s, r = api.call("POST", f"/api/batches/{b['id']}/submit-release", {"operator": "王质量"})
    must(s == 200 and r["data"]["batch"]["status"] == "released", "事件关闭后应放行成功")


@test
def t_released_batch_cannot_get_events(api):
    """已放行批次不能补挂事件"""
    b = api.make_batch(2, "R-REL-001")
    s, r = api.call("POST", f"/api/batches/{b['id']}/submit-release", {"operator": "王质量"})
    must(s == 200, f"放行失败: {r}")
    pid = api.point_id("P-TEMP-02")  # 同产线再来超限读数
    s, r = api.call("POST", "/api/readings", {"point_id": pid, "value": 30.0, "recorded_by": "回归"})
    ev = r["data"]["event"]
    must(ev is not None and ev["batch_no"] != "R-REL-001", "事件不得关联已放行批次")
    s, r = api.call("GET", f"/api/events?batch_id={b['id']}")
    must(r["data"]["events"] == [], "已放行批次不应挂任何事件")


@test
def t_duplicate_release_rejected(api):
    """重复放行被拒绝"""
    b = api.make_batch(2, "R-DUP-001")
    s, _ = api.call("POST", f"/api/batches/{b['id']}/submit-release", {"operator": "王质量"})
    must(s == 200, "首次放行应成功")
    s, r = api.call("POST", f"/api/batches/{b['id']}/submit-release", {"operator": "王质量"})
    must(s == 409 and r["error"]["code"] == "ALREADY_RELEASED", "重复放行应返回 409 ALREADY_RELEASED")


@test
def t_closed_event_rejects_retest(api):
    """已关闭事件拒绝复测读数"""
    api.make_batch(1, "R-CLSD-001")
    pid = api.point_id("P-TEMP-01")
    _, r = api.call("POST", "/api/readings", {"point_id": pid, "value": 28.0, "recorded_by": "回归"})
    eid = r["data"]["event"]["id"]
    api.call("POST", f"/api/events/{eid}/disposition", {"cause": "c", "measures": "m", "operator": "王质量"})
    api.call("POST", "/api/readings", {"point_id": pid, "value": 22.0, "recorded_by": "回归", "event_id": eid})
    s, r = api.call("POST", f"/api/events/{eid}/close", {"operator": "王质量"})
    must(s == 200, "满足条件应可关闭")
    s, r = api.call("POST", "/api/readings", {"point_id": pid, "value": 23.0, "recorded_by": "回归", "event_id": eid})
    must(s == 409 and r["error"]["code"] == "EVENT_CLOSED", "已关闭事件应拒绝复测（409 EVENT_CLOSED）")


@test
def t_concurrent_release_single_success(api):
    """并发放行同一批次仅一次成功"""
    b = api.make_batch(1, "R-CONC-001")
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(api.call("POST", f"/api/batches/{b['id']}/submit-release", {"operator": "王质量"}))

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    oks = [x for x in results if x[0] == 200]
    dup = [x for x in results if x[0] == 409 and x[1]["error"]["code"] == "ALREADY_RELEASED"]
    must(len(oks) == 1, f"并发放行应仅 1 次成功，实际 {len(oks)}")
    must(len(dup) == 7, f"其余 7 个请求应收到已放行提示，实际 {len(dup)}")


@test
def t_reading_release_race(api):
    """超限读数与放行并发：拦截或分离二选一（12 轮）"""
    pid = api.point_id("P-TEMP-01")
    for i in range(12):
        b = api.make_batch(1, f"R-RACE-{i:02d}")
        barrier = threading.Barrier(2)
        res = {}

        def do_reading():
            barrier.wait()
            res["r"] = api.call("POST", "/api/readings", {"point_id": pid, "value": 99.0, "recorded_by": "回归"})

        def do_release():
            barrier.wait()
            res["l"] = api.call("POST", f"/api/batches/{b['id']}/submit-release", {"operator": "王质量"})

        t1, t2 = threading.Thread(target=do_reading), threading.Thread(target=do_release)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        rs, reading = res["r"]
        ls, release = res["l"]
        ev = reading["data"]["event"]
        if ls == 409:
            must(release["error"]["code"] == "BATCH_BLOCKED" and ev["batch_no"] == b["batch_no"],
                 f"第{i+1}轮：拦截时事件应挂在该批次")
        elif ls == 200:
            s, evs = api.call("GET", f"/api/events?batch_id={b['id']}")
            must(ev["batch_no"] != b["batch_no"] and evs["data"]["events"] == [],
                 f"第{i+1}轮：放行成功后批次不得被补挂事件")
        else:
            raise AssertionError(f"第{i+1}轮：放行返回意外状态 {ls}")


# ---------------- 运行器 ----------------

def run_round():
    failures = []
    with temp_server() as base:
        api = Api(base)
        for fn in TESTS:
            name = (fn.__doc__ or fn.__name__).strip()
            try:
                fn(api)
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                failures.append(name)
                print(f"  FAIL  {name} -> {e}")
                traceback.print_exc(limit=2)
    return failures


def main():
    repeat = 1
    if "--repeat" in sys.argv:
        repeat = int(sys.argv[sys.argv.index("--repeat") + 1])
    all_ok = True
    for i in range(repeat):
        if repeat > 1:
            print(f"\n===== 第 {i + 1}/{repeat} 轮 =====")
        failures = run_round()
        all_ok = all_ok and not failures
        if repeat > 1:
            print(f"  第 {i + 1} 轮：{len(TESTS) - len(failures)}/{len(TESTS)} 通过")
    total = len(TESTS)
    print(f"\n{'全部通过' if all_ok else '存在失败'}：每轮 {total} 个用例 × {repeat} 轮")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
