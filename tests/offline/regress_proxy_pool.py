"""账号出口隔离回归：假控制器/假 HTTP，不联网、不读 Cookie、不消耗 LLM。

可加 --mihomo，使用本机已安装核心与临时本地代理验证真实路由及 HTTPS 连接切换。
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import Mock, patch
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import requests
import api_client as ac
import clash_ctl as cc
import combo_pool as cp
import config
import proxy_core as pc
import proxy_pool as pp

CASES = []


def case(name):
    def register(fn):
        CASES.append((name, fn))
        return fn
    return register


def _assert_secret_mode(path: Path) -> None:
    """含凭证配置的权限断言：POSIX 必须 0o600，Windows 只校验"未被设为只读"。

    Windows 的 os.chmod 只能表达只读位（S_IWRITE），0o600 根本落不了地——实测
    os.open(..., 0o600)+os.chmod(..., 0o600) 后 st_mode 仍是 0o666。原断言写死
    0o600，导致 windows-latest 上必挂（ubuntu 正常），CI 因此变红。
    """
    mode = path.stat().st_mode & 0o777
    if os.name == "nt":
        assert mode & 0o200, "Windows 上含凭证配置不应被设为只读（实际 %o）" % mode
    else:
        assert mode == 0o600, "含凭证配置应为 0o600，实际 %o" % mode


def response(value, code=200):
    r = requests.Response()
    r.status_code = code
    r._content = (value if isinstance(value, str) else json.dumps(value)).encode()
    r.encoding = "utf-8"
    return r


class FakeCtl:
    api_url = "http://127.0.0.1:19090"
    secret = "测试密钥"

    def __init__(self, group="账号A", nodes=None):
        self.group = group
        self.nodes = nodes or ["香港A", "日本B", "美国C"]
        self.now = self.nodes[0]
        self.dead = []
        self.switched = []

    def current_node(self):
        return self.now

    def mark_dead(self, node):
        self.dead.append(node)

    def rotate_node(self, exclude=()):
        eligible = [n for n in self.nodes if n != self.now and n not in self.dead and n not in exclude]
        if eligible:
            self.now = eligible[0]
            self.switched.append(self.now)
            return self.now


class Client(ac.BiliAPIClient):
    def __init__(self, ctl=None, ips=None):
        super().__init__()
        self.ctl = ctl
        self.ips = ips or {"香港A": "203.0.113.1", "日本B": "203.0.113.2", "美国C": "203.0.113.3"}
        self.probes = 0
        self.nav_error = None

    def get_raw(self, url, **kw):
        self.probes += 1
        return response(self.ips.get(self.ctl.now if self.ctl else "", "203.0.113.99"))

    def get(self, url, **kw):
        if self.nav_error:
            raise self.nav_error
        return {"code": 0}


class Controller:
    def __init__(self):
        self.groups = {"甲": "香港A", "乙": "香港A"}
        self.history = {n: [{"delay": 100}] for n in ["香港A", "日本B", "美国C"]}

    def get(self, *args, **kw):
        proxies = {g: {"type": "Selector", "all": [*self.history, "DIRECT"], "now": n}
                   for g, n in self.groups.items()}
        proxies.update({n: {"history": hist} for n, hist in self.history.items()})
        return response({"proxies": proxies})

    def put(self, url, json=None, **kw):
        self.groups[unquote(url.rsplit("/", 1)[-1])] = json["name"]
        return response("", 204)


@contextmanager
def controllers():
    backend = Controller()
    with patch.object(cc, "_SESSION", backend):
        yield backend, cc.ClashCtl("http://127.0.0.1:19090", "", "甲"), cc.ClashCtl("http://127.0.0.1:19090", "", "乙")


def route(ctl=None, name="甲", managed=True):
    ctl = ctl or FakeCtl(name)
    r = pp.get_route(name, "http://127.0.0.1:18080", ctl, managed)
    c = Client(ctl)
    r.attach(c)
    r.node = ctl.now
    r.ready = True
    return r, c, ctl


@case("各账号配置独立入口、端口和选择组，并只监听回环")
def _():
    core = pc.ProxyCore(["http://127.0.0.1/sub"], account_names=["主号", "alt1"])
    bindings = core.account_routes()
    assert len({url for _, url in bindings.values()}) == 2
    assert len({ctl.group for ctl, _ in bindings.values()}) == 2
    yaml = core._gen_config()
    assert yaml.count("type: mixed") == 2 and yaml.count("listen: 127.0.0.1") == 2
    for ctl, url in bindings.values():
        assert f'proxy: "{ctl.group}"' in yaml and f'port: {url.rsplit(":", 1)[-1]}' in yaml


@case("重复建入口和新增账号不会改变既有账号端口")
def _():
    core = pc.ProxyCore(["http://127.0.0.1/sub"], account_names=["主号", "alt1"])
    old = {n: u for n, (_, u) in core.account_routes().items()}
    assert core.ensure_accounts(["alt1", "主号", "alt2"])
    assert all(core.account_routes()[n][1] == u for n, u in old.items())


@case("热加载失败回滚入口和凭证配置")
def _():
    with tempfile.TemporaryDirectory() as tmp, patch.object(pc, "_RUNTIME_DIR", tmp):
        core = pc.ProxyCore(["http://127.0.0.1/sub"], account_names=["主号"])
        Path(core._runtime_dir).mkdir()
        path = Path(core._runtime_dir) / "config.yaml"
        core._write_config(str(path))
        old = path.read_text(encoding="utf-8")
        core._proc = Mock()
        core._proc.poll.return_value = None
        with patch.object(cc.ClashCtl, "current_node", return_value="香港A"), patch.object(cc.ClashCtl, "switch_node", return_value=True), patch.object(cc.ClashCtl, "reload_config", return_value=False):
            assert not core.ensure_accounts(["主号", "alt1"])
        assert set(core.account_routes()) == {"主号"} and path.read_text(encoding="utf-8") == old
        _assert_secret_mode(path)
        assert not Path(str(path) + ".tmp").exists()


@case("凭证权限断言：POSIX 严格 0600，Windows 只要求可写（两分支自检）")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "cfg.yaml"
        p.write_text("凭证: x", encoding="utf-8")     # Linux 下默认 0o644
        with patch.object(os, "name", "nt"):          # 模拟 windows runner 的语义
            _assert_secret_mode(p)                    # 不得因拿不到 0o600 而判失败
        if os.name != "nt":                           # 真实 POSIX：分支必须严格
            os.chmod(p, 0o600)
            _assert_secret_mode(p)
            os.chmod(p, 0o644)
            raised = False
            try:
                _assert_secret_mode(p)
            except AssertionError:
                raised = True
            assert raised, "POSIX 上权限过宽必须报错"


@case("停核心清理本实例 provider 凭证，同时保留其他实例目录")
def _():
    with tempfile.TemporaryDirectory() as tmp, patch.object(pc, "_RUNTIME_DIR", tmp):
        core = pc.ProxyCore(["http://127.0.0.1/sub"])
        own = Path(core._runtime_dir)
        own.mkdir()
        (own / "节点缓存.yaml").write_text("测试凭证", encoding="utf-8")
        peer = Path(tmp) / "其他核心"
        peer.mkdir()
        core.stop()
        assert not own.exists() and peer.exists()


@case("手动判死后缓存节点不会再次入选")
def _():
    with controllers() as (_, a, b):
        a.refresh_nodes()
        a.mark_dead("日本B")
        assert a.pick_next_node() == "美国C"
        assert "日本B" not in b.list_nodes()


@case("健康检查成功不会提前复活手动判死节点")
def _():
    with controllers() as (_, a, _b):
        a.mark_dead("日本B")
        assert "日本B" not in a.list_nodes()


@case("死节点 TTL 到期后可以重新探测")
def _():
    with controllers() as (_, a, _b):
        a.mark_dead("日本B", ttl=-1)
        assert "日本B" in a.list_nodes()


@case("全灭节点不会立即绕过死名单继续请求")
def _():
    with controllers() as (_, a, _b):
        for n in ["香港A", "日本B", "美国C"]:
            a.mark_dead(n)
        assert a.pick_next_node() is None and a.list_nodes() == []


@case("并发选节点和提交选择保持原子推进")
def _():
    with controllers() as (_, a, _b), ThreadPoolExecutor(2) as ex:
        values = list(ex.map(lambda _: a.rotate_node(), range(2)))
        assert len(set(values)) == 2 and all(values)


@case("切一个账号的组不改变其他账号组")
def _():
    with controllers() as (backend, a, _b):
        assert a.rotate_node() == "日本B"
        assert backend.groups == {"甲": "日本B", "乙": "香港A"}


@case("实际 IP 相同的节点共用请求锁和冷却")
def _():
    a, ca, _ = route(name="甲")
    b, cb, _ = route(name="乙")
    with patch.dict(os.environ, {"PROXY_IP_CHECK_URL": "http://127.0.0.1/ip"}):
        a._probe_ip(ca)
        b._probe_ip(cb)
    assert a.ip == b.ip and a.gate is b.gate
    a.gate.cooldown(10)
    assert b.snapshot()["cooldown_seconds"] > 9


@case("未知出口统一限速，IP 探测失败不误杀节点")
def _():
    a, ca, ctl = route(name="甲")
    b, _cb, _ = route(name="乙")
    ca.get_raw = Mock(side_effect=pp.ProxyConnError("探测服务暂不可达"))
    with patch.dict(os.environ, {"PROXY_IP_CHECK_URL": "http://127.0.0.1/ip"}):
        a._probe_ip(ca)
    assert a.gate is b.gate and not ctl.dead and a.active_proxy


@case("IP 探测遵守缓存间隔，并支持 IPv6")
def _():
    a, ca, _ = route()
    ca.get_raw = Mock(return_value=response("2001:db8::1"))
    with patch.dict(os.environ, {"PROXY_IP_CHECK_URL": "http://127.0.0.1/ip"}):
        a._probe_ip(ca)
        a._probe_ip(ca)
    assert ca.get_raw.call_count == 1 and a.ip == "2001:db8::1"


@case("首次分配按实际 IP 去重，而非只比较节点名")
def _():
    a, ca, _ = route(name="甲")
    ctl = FakeCtl("乙")
    b, cb, _ = route(ctl, "乙")
    b.ready = False
    cb.ips = {"香港A": "203.0.113.1", "日本B": "203.0.113.1", "美国C": "203.0.113.3"}
    with patch.dict(os.environ, {"PROXY_IP_CHECK_URL": "http://127.0.0.1/ip"}):
        a._probe_ip(ca)
        b.prepare(cb)
    assert b.ip != a.ip and ctl.now == "美国C"


@case("相同出口的并发请求在物理发送层串行化")
def _():
    gate = pp.EgressGate()
    first = threading.Event()
    release = threading.Event()
    entered = []
    def send(*args, **kw):
        entered.append(threading.get_ident())
        first.set()
        assert release.wait(2)
        return response("{}")
    clients = [ac.BiliAPIClient(), ac.BiliAPIClient()]
    for c in clients:
        c.set_egress_gate(gate)
        c.session.request = send
    with patch.object(ac, "REQUEST_DELAY", (0, 0)), ThreadPoolExecutor(2) as ex:
        f1 = ex.submit(clients[0]._request_immediate, "GET", "http://127.0.0.1/")
        assert first.wait(2)
        f2 = ex.submit(clients[1]._request_immediate, "GET", "http://127.0.0.1/")
        try:
            time.sleep(0.02)
            assert len(entered) == 1
        finally:
            release.set()
        f1.result(2)
        f2.result(2)


@case("不同实际出口的请求可以同时在途")
def _():
    barrier = threading.Barrier(2)
    def send(*args, **kw):
        barrier.wait(timeout=2)
        return response("{}")
    clients = [ac.BiliAPIClient(), ac.BiliAPIClient()]
    for c in clients:
        c.set_egress_gate(pp.EgressGate())
        c.session.request = send
    with patch.object(ac, "REQUEST_DELAY", (0, 0)), ThreadPoolExecutor(2) as ex:
        futures = [ex.submit(c._request_immediate, "GET", "http://127.0.0.1/") for c in clients]
        for f in futures:
            f.result(3)


@case("高风险接口与交互请求仍遵守出口限速间隔")
def _():
    c = ac.BiliAPIClient()
    gate = pp.EgressGate()
    c.set_egress_gate(gate)
    times = []
    def send(*args, **kw):
        times.append(time.monotonic())
        return response("{}")
    c.session.request = send
    with patch.object(ac, "REQUEST_DELAY_LONG", (0.03, 0.03)):
        for _ in range(2):
            c._request_immediate("GET", "http://127.0.0.1/space/wbi/acc/info")
    assert times[1] - times[0] >= 0.025


@case("出口冷却在实际发请求前等待，并可即时读取统计")
def _():
    gate = pp.EgressGate()
    gate.cooldown(0.03)
    before = time.monotonic()
    assert gate.snapshot()["cooldown_seconds"] > 0
    with gate.request(0):
        assert time.monotonic() - before >= 0.025


@case("风控保留原节点，不污染死名单，并计入出口统计")
def _():
    r, c, ctl = route()
    def risk(_):
        raise ac.RiskControlError("测试风控")
    try:
        r.execute(c, risk, 10)
        raise AssertionError("应抛风控信号")
    except ac.RiskControlError:
        pass
    assert not ctl.switched and not ctl.dead
    assert r.snapshot()["risk"] == 1 and r.snapshot()["cooldown_seconds"] > 9


@case("故障归因使用请求开始时节点，不查询失败后的全局选择")
def _():
    r, c, ctl = route()
    def fail(_):
        ctl.now = "日本B"
        raise ac.ProxyConnError("测试故障")
    try:
        r.execute(c, fail, 0)
    except pp.RetryProxyError:
        pass
    assert ctl.dead == ["香港A"]


@case("切换与摘代理清理连接池，保留 Cookie 和请求头")
def _():
    r, c, _ctl = route()
    c.session.cookies.set("测试", "保留")
    c.session.headers["X-测试"] = "保留"
    adapters = [Mock(), Mock()]
    c.session.adapters = dict(zip(["http://", "https://"], adapters))
    assert r._rotate()
    assert all(a.close.call_count == 1 for a in adapters)
    r._strip()
    assert all(a.close.call_count == 2 for a in adapters)
    assert c.session.cookies.get("测试") == "保留" and c.session.headers["X-测试"] == "保留"


@case("主池、分片和重复建池共享同一账号的降级与恢复状态")
def _():
    r, c, _ctl = route()
    pool = cp.ComboPool([("主号", c)], routes={"主号": r})
    child = pool.shard_pools()[0]
    r._strip()
    assert child._routes["主号"] is r and pool._routes["主号"] is r
    assert not c._proxy_active and r.stripped_at is not None
    assert pp.get_route("甲") is r


@case("某账号代理故障降级不会清除另一个账号代理")
def _():
    a, ca, _ = route(name="甲")
    b, cb, _ = route(name="乙")
    a._strip()
    assert not ca._proxy_active and cb._proxy_active and b.active_proxy


@case("连续代理故障有界降级，直连代理异常不会无限循环")
def _():
    r, c, _ = route()
    pool = cp.ComboPool([("主号", c)], routes={"主号": r})
    calls = []
    def fail(client):
        calls.append(client._proxy_active)
        raise ac.ProxyConnError("测试故障")
    try:
        pool.run(fail)
        raise AssertionError("直连异常应上抛")
    except ac.ProxyConnError:
        pass
    assert calls == [True, True, True, False]


@case("恢复重探由共享出口执行，成功后主池和分片同时恢复")
def _():
    r, c, _ = route()
    pool = cp.ComboPool([("主号", c)], routes={"主号": r})
    child = pool.shard_pools()[0]
    r._strip()
    with patch.object(config, "PROXY_RETRY_AFTER", 0):
        assert child.run(lambda c: {"code": 0}) == {"code": 0}
    assert r.active_proxy and c._proxy_active and pool._routes["主号"].stripped_at is None


@case("外部共享入口故障不会自动切外部全局节点")
def _():
    r, c, ctl = route(managed=False)
    try:
        r.execute(c, lambda _: (_ for _ in ()).throw(ac.ProxyConnError("测试")), 0)
    except pp.RetryProxyError:
        pass
    assert not ctl.switched and not ctl.dead


@case("自检目标接口异常不误杀节点，风控信号保留冷却")
def _():
    r, c, ctl = route()
    c.nav_error = RuntimeError("接口故障")
    assert not r._selfcheck(c, 3) and not ctl.dead
    c.nav_error = ac.RiskControlError("风控")
    assert r._selfcheck(c, 3) and r.snapshot()["cooldown_seconds"] > 0 and not ctl.dead


@case("核心重载后连接池与出口测量失效，既有绑定仍复用")
def _():
    r, c, _ = route()
    r.sync_generation(1)
    r.ip = "203.0.113.1"
    c.reset_connections = Mock()
    r.sync_generation(2)
    assert not r.ready and r.ip is None and c.reset_connections.call_count == 1
    r.sync_generation(2)
    assert c.reset_connections.call_count == 1


@case("出口诊断返回调用结果与 P95，不包含控制器密钥和代理凭证")
def _():
    r, c, ctl = route()
    r.execute(c, lambda _: {"code": -1}, 0)
    r.execute(c, lambda _: {"code": 0}, 0)
    status = r.snapshot()
    assert status["ok"] == 1 and status["api_error"] == 1 and status["p95_seconds"] >= 0
    assert ctl.secret not in json.dumps(status, ensure_ascii=False) and "proxy_url" not in status


@case("配置了订阅时内置独立入口优先于自动发现的外部核心")
def _():
    ctl = FakeCtl()
    core = Mock()
    core.generation = 1
    core.start.return_value = ctl
    core.account_routes.return_value = {"主号": (ctl, "http://127.0.0.1:18080")}
    with patch.object(config, "CLASH_ENABLED", False), patch.object(config, "SUB_URLS", ["测试订阅"]), patch.object(pc, "get_core", return_value=core), patch.object(cp, "_external_routes") as ext:
        routes = cp._discover_ip_pool([("主号", Client())])
    assert routes["主号"].managed and not ext.called


@case("外部独立入口配置检查重复端口和重复选择组，不打印凭证")
def _():
    mapping = {"主号": {"proxy_url": "http://测试账号:测试密码@127.0.0.1:18080", "group": "甲"},
               "alt1": {"proxy_url": "http://测试账号:测试密码@127.0.0.1:18080", "group": "乙"}}
    out = io.StringIO()
    from contextlib import redirect_stdout
    with controllers(), patch.dict(os.environ, {"CLASH_ACCOUNT_ROUTES": json.dumps(mapping)}), redirect_stdout(out):
        routes = cp._external_routes([("主号", Client()), ("alt1", Client())], config.CLASH_API_URL)
    assert not routes["主号"].managed and routes["主号"] is routes["alt1"]
    assert "测试密码" not in out.getvalue()


@case("其他请求成功不会重置当前失败请求的风控预算")
def _():
    r, c, _ctl = route()
    pool = cp.ComboPool([("主号", c)], routes={"主号": r})
    calls = []
    def outer(_):
        pass
    def execute(client, fn, cooldown):
        if fn is outer:
            calls.append(1)
            pool.run(lambda _: {"code": 0})
            raise ac.RiskControlError("测试风控")
        return fn(client)
    r.execute = execute
    try:
        pool.run(outer)
        raise AssertionError("应在预算耗尽后停止")
    except ac.RiskControlError:
        pass
    assert len(calls) == config.MAX_RISK_ROUNDS


@case("辅助 IP 探测可限定为一次请求，不继承主 API 的多轮退避")
def _():
    c = ac.BiliAPIClient()
    c.session.request = Mock(side_effect=requests.Timeout("测试超时"))
    c._sleep_if_needed = lambda _: None
    try:
        c.get_raw("http://127.0.0.1/ip", max_attempts=1)
        raise AssertionError("应抛超时")
    except requests.Timeout:
        pass
    assert c.session.request.call_count == 1
    assert "max_attempts" not in c.session.request.call_args.kwargs


@case("普通直连账号无需访问辅助 IP 服务")
def _():
    r = pp.get_route("direct-test")
    c = Client()
    r.attach(c)
    with patch.dict(os.environ, {"PROXY_IP_CHECK_URL": "http://127.0.0.1/ip"}):
        r.execute(c, lambda _: {"code": 0}, 0)
    assert c.probes == 0


@case("热加载等待已有请求结束，再恢复选择和失效连接")
def _():
    with tempfile.TemporaryDirectory() as tmp, patch.object(pc, "_RUNTIME_DIR", tmp):
        core = pc.ProxyCore(["http://127.0.0.1/sub"])
        Path(core._runtime_dir).mkdir()
        core._proc = Mock()
        core._proc.poll.return_value = None
        r, c, ctl = route()
        ctl.api_url = f"http://127.0.0.1:{core.api_port}"
        ctl.secret = core.secret
        r.sync_generation(0)
        c.reset_connections = Mock()
        entered, release, reloaded = threading.Event(), threading.Event(), threading.Event()
        def pending(_):
            entered.set()
            assert release.wait(2)
            return {"code": 0}
        def reload(*args):
            reloaded.set()
            return True
        with patch.object(cc.ClashCtl, "current_node", return_value="香港A"), patch.object(cc.ClashCtl, "switch_node", return_value=True), patch.object(cc.ClashCtl, "reload_config", side_effect=reload), ThreadPoolExecutor(2) as ex:
            f1 = ex.submit(r.execute, c, pending, 0)
            assert entered.wait(2)
            f2 = ex.submit(core.ensure_accounts, ["主号", "alt1"])
            try:
                assert not reloaded.wait(0.03)
            finally:
                release.set()
            f1.result(2)
            assert f2.result(2)
        assert reloaded.is_set() and not r.ready and c.reset_connections.call_count == 1


@case("出口不足时故障账号可借用健康节点，并共享其实际 IP 限速")
def _():
    ctl_a = FakeCtl("甲", ["香港A", "日本B"])
    ctl_b = FakeCtl("乙", ["香港A", "日本B"])
    ctl_b.now = "日本B"
    a, ca, _ = route(ctl_a, "甲")
    b, cb, _ = route(ctl_b, "乙")
    try:
        a.execute(ca, lambda _: (_ for _ in ()).throw(ac.ProxyConnError("故障")), 0)
    except pp.RetryProxyError:
        pass
    assert a.node == b.node == "日本B" and ca._proxy_active
    with patch.dict(os.environ, {"PROXY_IP_CHECK_URL": "http://127.0.0.1/ip"}):
        a._probe_ip(ca)
        b._probe_ip(cb)
    assert a.gate is b.gate


def main():
    passed = failed = 0
    for name, fn in CASES:
        pp._GATES.clear()
        pp._ROUTES.clear()
        pp._NODE_IPS.clear()
        cc._STATES.clear()
        try:
            with patch.dict(os.environ, {"PROXY_IP_CHECK_URL": "", "CLASH_ACCOUNT_ROUTES": ""}):
                fn()
            passed += 1
            print(f"  ✔ {name}")
        except Exception:
            failed += 1
            print(f"  ✘ {name}")
            traceback.print_exc()
    if "--mihomo" in sys.argv:
        from regress_proxy_pool_mihomo import run
        p, f = run()
        passed += p
        failed += f
    print(f"==== 账号出口隔离: {passed} 项通过, {failed} 项失败 ====")
    return bool(failed)


if __name__ == "__main__":
    sys.exit(main())
