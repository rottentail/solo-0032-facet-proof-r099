#!/usr/bin/env python3
"""Facet Proof 曝光租约竞争演练:验证实际竞争中一个曝光只产生一张最终图版。

场景:
  takeover         工作器 A 短租约领取并完整计算,卡在提交前失去租约;
                   recover 回收后工作器 B(HTTP worker/step)接管并完成;
                   A 迟到的心跳/提交/失败上报必须全部被拒或无效。
  cancel-running   运行中取消:心跳与提交被拒(cancelled),失败上报落为 cancelled。
  cancel-recovered 取消后租约过期:recover 直接落 cancelled,迟到 fail 为 no-op。
  http-race        4 条 HTTP 连接同时 worker/step 抢同一曝光:恰好一个成功。
  recover-live     未过期租约不受 recover 影响,持有者可正常完成。

检查分级(用于区分调度时机变化与真实不变量破坏):
  invariant    任何调度下都必须成立,失败即真实破坏,退出码非零。
  expectation  受控编排下确定成立(尝试次数、事件因果骨架),失败意味实现行为改变。
  observation  调度/时序相关(谁赢得竞争、耗时、精确事件序列),只记录不断言。

运行: python3 drills/lease_contention.py [--runs N] [--report PATH] [--keep ROOT]
"""
import argparse
import http.client
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from facetproof.application import Application
from facetproof.errors import Fault
from facetproof.planner import build_plan
from facetproof.raster import rasterize
from facetproof.passes import serialize_passes
from facetproof.values import digest

PREFIX = "/facet/v1/"
RENDER = {"width": 96, "height": 96, "samples": 2}
T0 = time.monotonic()


class DrillWorker:
    """镜像 facetproof.worker.Worker.run_one 的计算与提交段,但租约时长可控、
    提交前可暂停。渲染期间不发心跳:心跳是存活工作器的责任,本角色模拟
    卡住(停顿/失联)而失去租约的工作器。所有领域操作仍走产品代码。"""

    def __init__(self, app, lease_seconds):
        self.app, self.lease_seconds = app, lease_seconds
        self.exposure = None
        self.payload = None

    def claim(self):
        self.exposure = self.app.exposures.claim(lease_seconds=self.lease_seconds)
        return self.exposure

    def compute(self):
        exposure = self.exposure
        with self.app.db.unit_of_work() as conn:
            seal = self.app.db.get(conn, exposure["seal_id"], "seal")["payload"]
        plan = build_plan(seal, exposure["request"])
        rendered = rasterize(plan, lambda: None)
        passes, statistics = serialize_passes(plan, rendered, exposure["request"])
        descriptors = {name: self.app.blobs.put(data) for name, data in passes.items()}
        self.payload = {"format": "facet-plate-1", "seal_id": exposure["seal_id"],
                        "seal_hash": seal["content_hash"], "request": exposure["request"],
                        "width": exposure["request"]["width"], "height": exposure["request"]["height"],
                        "lens_hash": plan["lens_hash"], "objects": plan["objects"],
                        "passes": descriptors, "statistics": statistics,
                        "visual_hash": digest(descriptors)}
        return self.payload

    def heartbeat(self):
        return self.app.exposures.heartbeat(self.exposure["id"], self.exposure["token"])

    def finish(self):
        return self.app.plates.finish(self.exposure, self.payload)

    def fail(self, error):
        return self.app.exposures.fail(self.exposure["id"], self.exposure["token"], error)


class Http:
    """每次请求一条新连接(服务器响应 Connection: close),多线程各自独立使用。"""

    def __init__(self, port):
        self.port = port

    def call(self, method, route, data=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        body = json.dumps(data).encode() if data is not None else None
        try:
            conn.request(method, PREFIX + route, body=body,
                         headers={"Content-Type": "application/json"} if body else {})
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
        finally:
            conn.close()

    def must(self, method, route, data=None):
        status, payload = self.call(method, route, data)
        if status != 200:
            raise AssertionError(f"{method} {route} -> {status}: {payload}")
        return payload


class Report:
    def __init__(self, name):
        self.scenario = name
        self.timeline = []
        self.checks = []
        self.extra = {}

    def log(self, actor, action, detail=None):
        self.timeline.append({"t": round(time.monotonic() - T0, 3),
                              "actor": actor, "action": action, "detail": detail})

    def _check(self, kind, cid, ok, detail):
        self.checks.append({"id": cid, "kind": kind,
                            "status": "pass" if ok else "fail", "detail": detail})
        return ok

    def invariant(self, cid, ok, detail):
        return self._check("invariant", cid, bool(ok), detail)

    def expectation(self, cid, ok, detail):
        return self._check("expectation", cid, bool(ok), detail)

    def observation(self, cid, detail):
        self.checks.append({"id": cid, "kind": "observation", "status": "info", "detail": detail})

    def as_dict(self):
        return {"scenario": self.scenario, "timeline": self.timeline,
                "checks": self.checks, **self.extra}


def skeleton(actions, pattern):
    """事件动作序列是否包含给定因果骨架(子序列),容忍将来新增的事件类型。"""
    it = iter(actions)
    return all(any(a == want for a in it) for want in pattern)


def plate_rows(root, exposure_id):
    """只读直查 SQLite:plates 表没有 HTTP 列表路由,唯一性必须落到存储层验证。"""
    path = Path(root) / "facetproof.sqlite3"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id, state FROM plates WHERE exposure_id=?",
                            (exposure_id,)).fetchall()
        return [{"id": r[0], "state": r[1]} for r in rows]
    finally:
        conn.close()


def wait_lease_expired(http, exposure_id, timeout=20.0):
    """按公开状态里的 lease_until 判定过期,而非固定睡眠(时序鲁棒)。"""
    deadline = time.monotonic() + timeout
    while True:
        view = http.must("GET", f"exposures/{exposure_id}")
        if view["state"] == "running" and time.time() > view["lease_until"] + 0.02:
            return view
        if time.monotonic() > deadline:
            raise TimeoutError(f"lease for {exposure_id} never expired: {view}")
        time.sleep(0.05)


def late_call(worker, name, *args):
    """执行工作器的迟到操作,把 Fault 拒绝码记录为可断言的事实。"""
    try:
        result = getattr(worker, name)(*args)
        return {"accepted": True, "state": result.get("state") if isinstance(result, dict) else None}
    except Fault as exc:
        return {"accepted": False, "code": exc.code, "status": exc.status}


def scenario_takeover(http, app, root, seal_id):
    """核心场景:A 领取后失去租约,B 接管;两次计算都发生,只允许一个成功。"""
    rep = Report("takeover")
    exp = http.must("POST", f"seals/{seal_id}/exposures", {"key": "takeover", **RENDER})
    eid = exp["id"]
    rep.extra["exposure_id"] = eid
    worker_a = DrillWorker(app, lease_seconds=0.6)
    gate, claimed, computed, done = (threading.Event() for _ in range(4))
    late = {}

    def actor_a():
        try:
            worker_a.claim()
            claimed.set()
            rep.log("A", "claimed", {"attempt": worker_a.exposure["attempt"],
                                     "lease_until": worker_a.exposure["lease_until"]})
            payload = worker_a.compute()
            rep.log("A", "computed", {"visual_hash": payload["visual_hash"]})
            computed.set()
            if not gate.wait(30):
                raise TimeoutError("gate never released")
            late["heartbeat"] = late_call(worker_a, "heartbeat")
            late["finish"] = late_call(worker_a, "finish")
            late["fail"] = late_call(worker_a, "fail", "late_failure")
            rep.log("A", "late-ops", late)
        except BaseException as exc:  # noqa: BLE001 - 演练记录一切
            late["actor_error"] = repr(exc)
        finally:
            done.set()

    threading.Thread(target=actor_a, daemon=True).start()
    if not claimed.wait(15) or not computed.wait(90):
        raise TimeoutError("worker A stalled before gate")

    wait_lease_expired(http, eid)
    rec = http.must("POST", "worker/recover")
    rep.log("drill", "recover", rec)
    mid = http.must("GET", f"exposures/{eid}")
    rep.invariant("takeover.recovered-public-state",
                  mid["state"] == "queued" and mid["token"] is None and mid["lease_until"] is None,
                  f"recover 后公开状态: state={mid['state']} token={mid['token']}")

    step = http.must("POST", "worker/step")
    rep.log("B", "worker/step", {"state": step["state"]})
    rep.invariant("takeover.takeover-succeeded", step["state"] == "succeeded", f"B 结果: {step['state']}")
    plate = step["plate"]
    pid = plate["id"]
    rep.extra["plate_id"] = pid
    gate.set()
    if not done.wait(15):
        raise TimeoutError("worker A stalled after gate")

    final = http.must("GET", f"exposures/{eid}")
    again = http.must("GET", f"exposures/{eid}")
    events = http.must("GET", f"events/{eid}/history")["items"]
    actions = [e["action"] for e in events]
    rows = plate_rows(root, eid)

    rep.invariant("takeover.single-plate-row", len(rows) == 1 and rows[0]["id"] == pid,
                  f"plates 表中该曝光的行: {rows}")
    rep.invariant("takeover.exposure-terminal",
                  final["state"] == "succeeded" and final["plate_id"] == pid,
                  f"终态: state={final['state']} plate_id={final['plate_id']}")
    rep.invariant("takeover.plate-backref",
                  http.must("GET", f"plates/{pid}")["exposure_id"] == eid,
                  "图版反向引用曝光")
    rep.invariant("takeover.single-succeeded-event", actions.count("succeeded") == 1,
                  f"succeeded 事件数: {actions.count('succeeded')} 序列={actions}")
    rep.invariant("takeover.late-heartbeat-rejected",
                  late.get("heartbeat", {}).get("code") == "lease_lost"
                  and late["heartbeat"]["status"] == 409,
                  f"迟到心跳: {late.get('heartbeat')}")
    rep.invariant("takeover.late-finish-rejected",
                  late.get("finish", {}).get("code") == "lease_lost"
                  and late["finish"]["status"] == 409,
                  f"迟到提交: {late.get('finish')}")
    rep.invariant("takeover.late-fail-noop",
                  late.get("fail", {}).get("state") == "succeeded",
                  f"迟到失败上报不改变他人成功: {late.get('fail')}")
    rep.invariant("takeover.public-state-stable", again == final,
                  "迟到操作后公开状态逐字节稳定")
    rep.invariant("takeover.loser-computed",
                  all(app.blobs.read(d["sha256"]) for d in worker_a.payload["passes"].values()),
                  "A 的六个通道字节真实写入内容寻址存储(两次计算都发生)")
    rep.expectation("takeover.render-deterministic",
                    worker_a.payload["visual_hash"] == plate["payload"]["visual_hash"],
                    f"A 计算摘要 == B 承诺摘要: {worker_a.payload['visual_hash'][:16]}...")
    rep.expectation("takeover.attempt-count", final["attempt"] == 2,
                    f"attempt={final['attempt']}")
    rep.expectation("takeover.event-skeleton",
                    skeleton(actions, ["queued", "claimed", "recovered", "claimed", "succeeded"]),
                    f"事件序列: {actions}")
    tokens = [e["payload"]["token"] for e in events if e["action"] == "claimed"]
    rep.expectation("takeover.distinct-lease-tokens", len(set(tokens)) == 2,
                    "两次领取的租约令牌不同")
    rep.observation("takeover.event-sequence", actions)
    rep.observation("takeover.winner", {"winner": "B(http worker/step)", "plate": pid})

    status, body = http.call("POST", f"exposures/{eid}/cancel")
    rep.invariant("takeover.cancel-after-success-rejected",
                  status == 409 and body.get("error") == "state",
                  f"完成后取消: {status} {body}")
    status, body = http.call("POST", f"exposures/{eid}/retry")
    rep.invariant("takeover.retry-after-success-rejected",
                  status == 409 and body.get("error") == "state",
                  f"完成后重试: {status} {body}")
    ver = http.must("POST", f"plates/{pid}/verify")
    rep.invariant("takeover.plate-verifies", ver["valid"] is True, "图版校验通过")
    rep.extra["events"] = events
    return rep


def scenario_cancel_running(http, app, root, seal_id):
    """运行中取消:公开状态仍 running 但带取消标记;心跳/提交被拒;fail 落 cancelled。"""
    rep = Report("cancel-running")
    exp = http.must("POST", f"seals/{seal_id}/exposures", {"key": "cancel-running", **RENDER})
    eid = exp["id"]
    rep.extra["exposure_id"] = eid
    worker_a = DrillWorker(app, lease_seconds=120)
    gate, claimed, computed, done = (threading.Event() for _ in range(4))
    late = {}

    def actor_a():
        try:
            worker_a.claim()
            claimed.set()
            worker_a.compute()
            computed.set()
            if not gate.wait(30):
                raise TimeoutError("gate never released")
            late["heartbeat"] = late_call(worker_a, "heartbeat")
            late["finish"] = late_call(worker_a, "finish")
            late["fail"] = late_call(worker_a, "fail", "giving_up")
            rep.log("A", "late-ops", late)
        except BaseException as exc:  # noqa: BLE001
            late["actor_error"] = repr(exc)
        finally:
            done.set()

    threading.Thread(target=actor_a, daemon=True).start()
    if not claimed.wait(15) or not computed.wait(90):
        raise TimeoutError("worker A stalled")

    http.must("POST", f"exposures/{eid}/cancel")
    mid = http.must("GET", f"exposures/{eid}")
    rep.invariant("cancel.running-public-state",
                  mid["state"] == "running" and mid["cancel_requested"] == 1,
                  f"取消后公开状态: state={mid['state']} cancel_requested={mid['cancel_requested']}")
    idle = http.must("POST", "worker/step")
    rep.invariant("cancel.running-not-claimable", idle["state"] == "idle",
                  f"运行中的曝光不会被重复领取: {idle}")
    gate.set()
    if not done.wait(15):
        raise TimeoutError("worker A stalled after gate")

    rep.invariant("cancel.heartbeat-rejected",
                  late.get("heartbeat", {}).get("code") == "cancelled"
                  and late["heartbeat"]["status"] == 409,
                  f"取消后心跳: {late.get('heartbeat')}")
    rep.invariant("cancel.finish-rejected",
                  late.get("finish", {}).get("code") == "cancelled"
                  and late["finish"]["status"] == 409,
                  f"取消后提交: {late.get('finish')}")
    rep.invariant("cancel.fail-applies-cancelled",
                  late.get("fail", {}).get("state") == "cancelled",
                  f"持有者失败上报落为 cancelled: {late.get('fail')}")

    final = http.must("GET", f"exposures/{eid}")
    events = http.must("GET", f"events/{eid}/history")["items"]
    actions = [e["action"] for e in events]
    rep.invariant("cancel.terminal-state",
                  final["state"] == "cancelled" and final["plate_id"] is None,
                  f"终态: state={final['state']} plate_id={final['plate_id']}")
    rep.invariant("cancel.no-plate-row", plate_rows(root, eid) == [],
                  "取消的曝光没有图版行")
    rep.invariant("cancel.no-succeeded-event", actions.count("succeeded") == 0,
                  f"事件序列: {actions}")
    rep.expectation("cancel.event-skeleton",
                    skeleton(actions, ["queued", "claimed", "cancel_requested", "cancelled"]),
                    f"事件序列: {actions}")
    rep.observation("cancel.event-sequence", actions)

    status, body = http.call("POST", f"exposures/{eid}/retry")
    rep.invariant("cancel.retry-rejected", status == 409 and body.get("error") == "state",
                  f"取消后重试: {status} {body}")
    status, _ = http.call("POST", f"exposures/{eid}/cancel")
    rep.invariant("cancel.cancel-idempotent", status == 200, f"重复取消幂等: {status}")
    idle = http.must("POST", "worker/step")
    rep.invariant("cancel.cancelled-not-claimable", idle["state"] == "idle",
                  "已取消曝光不会被领取")
    rep.extra["events"] = events
    return rep


def scenario_cancel_recovered(http, app, root, seal_id):
    """取消后租约过期:recover 直接落 cancelled;迟到的 fail 是 no-op。"""
    rep = Report("cancel-recovered")
    exp = http.must("POST", f"seals/{seal_id}/exposures", {"key": "cancel-recovered", **RENDER})
    eid = exp["id"]
    rep.extra["exposure_id"] = eid
    worker_a = DrillWorker(app, lease_seconds=0.5)
    gate, claimed, done = threading.Event(), threading.Event(), threading.Event()
    late = {}

    def actor_a():
        try:
            worker_a.claim()
            claimed.set()
            worker_a.compute()
            if not gate.wait(30):
                raise TimeoutError("gate never released")
            late["fail"] = late_call(worker_a, "fail", "too_late")
            late["finish"] = late_call(worker_a, "finish")
            rep.log("A", "late-ops", late)
        except BaseException as exc:  # noqa: BLE001
            late["actor_error"] = repr(exc)
        finally:
            done.set()

    threading.Thread(target=actor_a, daemon=True).start()
    if not claimed.wait(15):
        raise TimeoutError("worker A stalled")
    http.must("POST", f"exposures/{eid}/cancel")
    wait_lease_expired(http, eid)
    rec = http.must("POST", "worker/recover")
    rep.log("drill", "recover", rec)
    rep.invariant("cancelrec.recovered-this-exposure", eid in rec["recovered"],
                  f"recover 回收: {rec}")
    gate.set()
    if not done.wait(15):
        raise TimeoutError("worker A stalled after gate")

    final = http.must("GET", f"exposures/{eid}")
    events = http.must("GET", f"events/{eid}/history")["items"]
    actions = [e["action"] for e in events]
    recovered = [e for e in events if e["action"] == "recovered"]
    rep.invariant("cancelrec.terminal-cancelled",
                  final["state"] == "cancelled" and final["token"] is None
                  and final["lease_until"] is None and final["plate_id"] is None,
                  f"恢复后公开状态: state={final['state']}")
    rep.invariant("cancelrec.recover-event-cancelled",
                  len(recovered) == 1 and recovered[0]["payload"]["state"] == "cancelled",
                  f"recover 事件落 cancelled: {recovered}")
    rep.invariant("cancelrec.no-plate-row", plate_rows(root, eid) == [],
                  "无图版行")
    rep.invariant("cancelrec.late-fail-noop",
                  late.get("fail", {}).get("state") == "cancelled",
                  f"过期后 fail 为 no-op: {late.get('fail')}")
    rep.invariant("cancelrec.late-finish-rejected",
                  late.get("finish", {}).get("code") == "lease_lost",
                  f"过期后提交: {late.get('finish')}")
    rep.expectation("cancelrec.event-skeleton",
                    skeleton(actions, ["queued", "claimed", "cancel_requested", "recovered"]),
                    f"事件序列: {actions}")
    rep.observation("cancelrec.event-sequence", actions)
    rep.extra["events"] = events
    return rep


def scenario_http_race(http, root, seal_id):
    """4 条 HTTP 连接同时 worker/step 抢同一曝光:恰好一个成功,其余 idle。"""
    rep = Report("http-race")
    exp = http.must("POST", f"seals/{seal_id}/exposures", {"key": "http-race", **RENDER})
    eid = exp["id"]
    rep.extra["exposure_id"] = eid
    n = 4
    results, barrier = [None] * n, threading.Barrier(n)

    def racer(i):
        try:
            barrier.wait(10)
            results[i] = http.must("POST", "worker/step")
        except BaseException as exc:  # noqa: BLE001
            results[i] = {"state": "error", "error": repr(exc)}

    threads = [threading.Thread(target=racer, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(90)

    wins = [r for r in results if r and r["state"] == "succeeded"]
    idles = [r for r in results if r and r["state"] == "idle"]
    if wins:
        rep.extra["plate_id"] = wins[0]["plate"]["id"]
    rep.log("drill", "race-results", [r["state"] if r else "stalled" for r in results])
    rep.invariant("race.all-answered", all(r is not None for r in results),
                  f"全部应答: {[r and r['state'] for r in results]}")
    rep.invariant("race.single-winner", len(wins) == 1 and len(idles) == n - 1,
                  f"成功 {len(wins)} / 空闲 {len(idles)} / 共 {n}")
    rows = plate_rows(root, eid)
    rep.invariant("race.single-plate-row", len(rows) == 1 and rows[0]["id"] == wins[0]["plate"]["id"],
                  f"plates 行: {rows}")
    final = http.must("GET", f"exposures/{eid}")
    rep.invariant("race.terminal",
                  final["state"] == "succeeded" and final["plate_id"] == rows[0]["id"],
                  f"终态: {final['state']}")
    events = http.must("GET", f"events/{eid}/history")["items"]
    actions = [e["action"] for e in events]
    rep.invariant("race.single-claim-and-success",
                  actions.count("claimed") == 1 and actions.count("succeeded") == 1,
                  f"事件序列: {actions}")
    rep.expectation("race.attempt-count", final["attempt"] == 1, f"attempt={final['attempt']}")
    rep.observation("race.winner-plate", wins[0]["plate"]["id"] if wins else None)
    rep.extra["events"] = events
    return rep


def scenario_recover_live(http, app, root, seal_id):
    """未过期租约不受 recover 影响,持有者正常完成。"""
    rep = Report("recover-live")
    exp = http.must("POST", f"seals/{seal_id}/exposures", {"key": "recover-live", **RENDER})
    eid = exp["id"]
    rep.extra["exposure_id"] = eid
    worker_a = DrillWorker(app, lease_seconds=120)
    worker_a.claim()
    worker_a.compute()
    rec = http.must("POST", "worker/recover")
    rep.log("drill", "recover-during-live-lease", rec)
    rep.invariant("live.recover-ignores-valid-lease", eid not in rec["recovered"],
                  f"recover 结果: {rec}")
    rep.expectation("live.recover-empty", rec["recovered"] == [],
                    "此时没有其他过期租约")
    mid = http.must("GET", f"exposures/{eid}")
    rep.invariant("live.still-running", mid["state"] == "running",
                  f"recover 后仍 running: {mid['state']}")
    plate = worker_a.finish()
    rep.extra["plate_id"] = plate["id"]
    rep.invariant("live.finish-succeeds", plate["state"] == "ready",
                  f"持有者提交成功: {plate['id']}")
    final = http.must("GET", f"exposures/{eid}")
    rep.invariant("live.terminal",
                  final["state"] == "succeeded" and final["plate_id"] == plate["id"],
                  f"终态: {final['state']}")
    events = http.must("GET", f"events/{eid}/history")["items"]
    rep.expectation("live.event-skeleton",
                    skeleton([e["action"] for e in events], ["queued", "claimed", "succeeded"]),
                    f"事件序列: {[e['action'] for e in events]}")
    rep.extra["events"] = events
    return rep


def setup_scene(http):
    """与 demo 相同的斜翼几何,冻结一张快照供全部场景共享。"""
    mesh = http.must("POST", "meshes", {"name": "斜翼", "vertices": [[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0], [0, 0, 1.2]],
                                        "faces": [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4], [0, 3, 2], [0, 2, 1]]})
    pigment = http.must("POST", "pigments", {"name": "釉青", "color": [.12, .62, .61], "two_sided": True})
    assembly = http.must("POST", "assemblies", {"name": "租约演练", "lens": {"eye": [3, 2, 4]}})
    http.must("PUT", f"assemblies/{assembly['id']}",
              {"revision": 1, "placements": [{"key": "wing", "mesh_id": mesh["id"],
                                              "pigment_id": pigment["id"],
                                              "transform": {"euler": [0, 20, 0]}}]})
    seal = http.must("POST", f"assemblies/{assembly['id']}/seal", {"revision": 2})
    return seal["id"]


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def run_once(run_index, keep_root=None):
    root = keep_root or tempfile.mkdtemp(prefix=f"facetproof-drill-{run_index}-")
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "facetproof", "--root", root,
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    reports = []
    try:
        deadline = time.monotonic() + 20
        http = Http(port)
        while True:
            try:
                http.must("GET", "health")
                break
            except Exception:
                if server.poll() is not None:
                    raise RuntimeError(f"server exited: {server.stdout.read()}")
                if time.monotonic() > deadline:
                    raise TimeoutError("server never became healthy")
                time.sleep(0.1)
        app = Application(root)  # 演练进程的独立 SQLite 连接,与服务进程共享存储
        seal_id = setup_scene(http)
        for fn in (lambda: scenario_takeover(http, app, root, seal_id),
                   lambda: scenario_cancel_running(http, app, root, seal_id),
                   lambda: scenario_cancel_recovered(http, app, root, seal_id),
                   lambda: scenario_http_race(http, root, seal_id),
                   lambda: scenario_recover_live(http, app, root, seal_id)):
            try:
                reports.append(fn())
            except Exception as exc:  # 场景编排自身失败视为不变量破坏
                rep = Report(getattr(fn, "__name__", "scenario"))
                rep.invariant("scenario-completed", False, f"{type(exc).__name__}: {exc}")
                reports.append(rep)
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(5)
    relations = []
    for rep in reports:
        events = rep.extra.get("events", [])
        relations.append({
            "scenario": rep.scenario,
            "exposure": rep.extra.get("exposure_id"),
            "plate": rep.extra.get("plate_id"),
            "events": [e["action"] for e in events],
            "plates_for_exposure": plate_rows(root, rep.extra["exposure_id"]) if rep.extra.get("exposure_id") else [],
        })
    return {"run": run_index, "root": root, "scenarios": [r.as_dict() for r in reports],
            "relations": relations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1, help="重复轮数(每轮全新存储与端口)")
    parser.add_argument("--report", help="报告输出路径(默认写到临时文件并打印路径)")
    parser.add_argument("--keep", help="保留本轮数据根目录(默认临时目录)")
    args = parser.parse_args()

    runs, failed = [], 0
    for i in range(args.runs):
        result = run_once(i, keep_root=args.keep if args.runs == 1 else None)
        runs.append(result)
        for scenario in result["scenarios"]:
            for check in scenario["checks"]:
                mark = {"pass": "✓", "fail": "✗", "info": "·"}[check["status"]]
                print(f"{mark} [{scenario['scenario']}/{check['kind']}] {check['id']}: {check['detail']}")
                if check["status"] == "fail":
                    failed += 1
    report = {"drill": "facetproof-lease-contention", "runs": runs,
              "failed_checks": failed, "verdict": "pass" if failed == 0 else "fail"}
    if args.report:
        path = Path(args.report)
    else:
        fd, name = tempfile.mkstemp(prefix="lease-contention-", suffix=".json")
        Path(name).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        path = Path(name)
    if args.report:
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告: {path}")
    print(f"结论: {report['verdict']} (失败检查 {failed} 项)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
