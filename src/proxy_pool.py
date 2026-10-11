"""账号出口与实际 IP 的共享调度：入口隔离、连接更新、限速、冷却及健康统计。

所有探测均走 BiliAPIClient，探测失败不把节点判死。未知出口保守地共享限速，
已测得相同公网 IP 的不同节点也共享同一把请求锁和冷却截止时刻。
"""
from collections import deque
from contextlib import ExitStack, contextmanager
import ipaddress
import os
import random
import threading
import time
import weakref

import config
from api_client import ProxyConnError, RiskControlError


class EgressGate:
    """同一实际出口的请求串行化；不同出口之间可以并行。"""

    def __init__(self):
        self.lock = threading.RLock()
        self._state_lock = threading.Lock()
        self.last_request = 0.0
        self.cooldown_until = 0.0
        self.counts = {"ok": 0, "risk": 0, "proxy_error": 0, "api_error": 0}
        self.latencies = deque(maxlen=getattr(config, "PROXY_METRICS_WINDOW", 200))

    @contextmanager
    def request(self, delay):
        with self.lock:
            while True:
                with self._state_lock:
                    now = time.monotonic()
                    wait = max(self.cooldown_until - now,
                               self.last_request + delay - now)
                if wait <= 0:
                    break
                time.sleep(min(wait, 30))
            with self._state_lock:
                self.last_request = time.monotonic()
            yield

    def cooldown(self, seconds):
        with self._state_lock:
            self.cooldown_until = max(self.cooldown_until, time.monotonic() + seconds)

    def record(self, kind, elapsed):
        with self._state_lock:
            self.counts[kind] += 1
            self.latencies.append(elapsed)

    def snapshot(self):
        with self._state_lock:
            values = sorted(self.latencies)
            return {**self.counts,
                    "p95_seconds": values[max(0, (len(values) * 95 + 99) // 100 - 1)]
                    if values else 0.0,
                    "cooldown_seconds": max(0, self.cooldown_until - time.monotonic())}


_REGISTRY_LOCK = threading.RLock()
_GATES = {}
_ROUTES = {}
_NODE_IPS = {}


class RetryProxyError(ProxyConnError):
    """出口层已完成切换或降级，允许组合池重试一次当前请求。"""


def get_gate(key):
    with _REGISTRY_LOCK:
        return _GATES.setdefault(key, EgressGate())


def _occupied(route):
    """只在注册表锁内取快照，不再拿其他出口的锁，避免锁序反转。"""
    with _REGISTRY_LOCK:
        peers = [r for r in _ROUTES.values() if r is not route and r.clients]
        nodes = {r.node for r in peers if r.clash and route.clash
                 and r.clash.api_url == route.clash.api_url and r.node}
        ips = {r.ip for r in peers if r.ip}
        now = time.monotonic()
        ttl = getattr(config, "PROXY_IP_CHECK_INTERVAL", 1800)
        nodes.update(node for (api, node), (ip, ts) in _NODE_IPS.items()
                     if route.clash and api == route.clash.api_url and ip in ips
                     and now - ts < ttl)
        return nodes, ips


class ProxyRoute:
    """一个账号入口的共享状态，主池、分片和重复建池均复用此对象。

    route.lock 覆盖请求与节点切换，故障只归因于该请求开始时的节点。
    外部共享入口 managed=False，不自动修改其全局选择组。
    """

    def __init__(self, proxy_url=None, clash=None, managed=False):
        self.proxy_url = proxy_url
        self.active_proxy = proxy_url
        self.clash = clash
        self.managed = managed
        self.lock = threading.RLock()
        self.clients = weakref.WeakSet()
        self.node = None
        self.ip = None
        self.gate = get_gate("unknown")
        self.probe_at = None
        self.stripped_at = None
        self.fail_streak = 0
        self.ready = not bool(proxy_url)
        self.generation = None

    def sync_generation(self, generation):
        """内置核心重启/热加载后重新确认选择与出口，旧账号连接池同时失效。"""
        with self.lock:
            if self.generation is None:
                self.generation = generation
            elif generation > self.generation:
                self.generation = generation
                self.ready = False
                self.node = self.ip = None
                self.probe_at = None
                self._reset_connections()
                self._set_gate("unknown")

    def attach(self, client):
        with self.lock:
            self.clients.add(client)
            client.raise_on_risk = True
            client.set_proxy(self.active_proxy)
            client.set_egress_gate(self.gate)

    def _set_gate(self, key):
        self.gate = get_gate(key)
        for client in list(self.clients):
            client.set_egress_gate(self.gate)

    def _reset_connections(self):
        for client in list(self.clients):
            client.reset_connections()

    def _probe_ip(self, client, force=False):
        url = os.environ.get("PROXY_IP_CHECK_URL",
                             getattr(config, "PROXY_IP_CHECK_URL", "https://api.ipify.org")).strip()
        if not url or self.proxy_url is None:
            return
        now = time.monotonic()
        if not force and self.probe_at is not None and (
                now - self.probe_at < getattr(config, "PROXY_IP_CHECK_INTERVAL", 1800)):
            return
        self.probe_at = now
        try:
            response = client.get_raw(url, timeout=getattr(config, "PROXY_IP_CHECK_TIMEOUT", 8),
                                      max_attempts=getattr(config, "PROXY_IP_CHECK_ATTEMPTS", 1))
            response.raise_for_status()
            ip = str(ipaddress.ip_address(response.text.strip()))
        except Exception:
            # 公网 IP 服务故障不等于代理故障；未知出口降低并发而不是误杀节点。
            self.ip = None
            self._set_gate("unknown")
            return
        self.ip = ip
        self._set_gate(("ip", ip))
        if self.clash and self.node:
            with _REGISTRY_LOCK:
                _NODE_IPS[(self.clash.api_url, self.node)] = (ip, now)

    def _rotate(self, visited=(), allow_shared=True):
        if not self.managed or not self.clash:
            return False
        occupied, _ = _occupied(self)
        nxt = self.clash.rotate_node(exclude=occupied | set(visited))
        if not nxt and allow_shared:
            # 出口不足时可借用健康节点，实测相同 IP 后共用限速，避免无谓转直连。
            nxt = self.clash.rotate_node(exclude=set(visited))
        if not nxt:
            return False
        self.node = nxt
        self.ip = None
        self.probe_at = None
        self._reset_connections()
        self._set_gate("unknown")
        return True

    def _strip(self):
        self.active_proxy = None
        self.ip = None
        self.stripped_at = time.monotonic()
        self.probe_at = None
        for client in list(self.clients):
            client.set_proxy(None)
        self._set_gate("unknown")
        print("[Pool] 当前账号入口故障，降级直连，其他独立入口继续使用")

    def _selfcheck(self, client, max_tries):
        for _ in range(max_tries):
            failed_node = self.node
            try:
                data = client.get(config.NAV_URL, timeout=8)
                return isinstance(data, dict) and "code" in data
            except RiskControlError:
                # 链路可达，但冷却信号仍须保留，不把风控误判成死节点。
                self.gate.cooldown(config.SINGLE_ACCOUNT_RISK_COOLDOWN)
                return True
            except ProxyConnError:
                if self.managed and self.clash and failed_node:
                    self.clash.mark_dead(failed_node)
                if not self._rotate():
                    return False
            except Exception:
                return False
        return False

    def prepare(self, client):
        """首次分配尽量避开已占用节点/IP，出口不足时由共享 IP 限速兜底。"""
        with self.lock:
            if self.ready or not self.active_proxy:
                return
            self.node = self.clash.current_node() if self.clash else None
            occupied, _ = _occupied(self)
            if self.managed and self.node in occupied:
                self._rotate(allow_shared=False)
            visited = set()
            tries = getattr(config, "PROXY_STARTUP_MAX_TRIES", 3)
            for _ in range(tries):
                self._probe_ip(client, force=True)
                _, ips = _occupied(self)
                if not self.ip or self.ip not in ips:
                    break
                visited.add(self.node)
                if not self._rotate(visited, allow_shared=False):
                    break
            if not self._selfcheck(client, tries):
                self._strip()
            elif self.probe_at is None:
                self._probe_ip(client)
            self.ready = True

    def _restore(self, client):
        if self.active_proxy or not self.proxy_url or self.stripped_at is None:
            return
        if time.monotonic() - self.stripped_at < config.PROXY_RETRY_AFTER:
            return
        self.active_proxy = self.proxy_url
        for member in list(self.clients):
            member.set_proxy(self.proxy_url)
        self._set_gate("unknown")
        self.node = self.clash.current_node() if self.clash else None
        if self._selfcheck(client, getattr(config, "PROXY_STARTUP_MAX_TRIES", 3)):
            self.fail_streak = 0
            self.stripped_at = None
            self._probe_ip(client, force=True)
            print("[Pool] 当前账号独立入口恢复")
        else:
            self._strip()

    def execute(self, client, fn, cooldown):
        with self.lock:
            self.prepare(client)
            self._restore(client)
            self._probe_ip(client)
            failed_node, gate = self.node, self.gate
            started = time.monotonic()
            try:
                result = fn(client)
            except ProxyConnError as e:
                gate.record("proxy_error", time.monotonic() - started)
                if not self.active_proxy:
                    raise
                self.fail_streak += 1
                if self.managed and self.clash and failed_node:
                    self.clash.mark_dead(failed_node)
                if self.fail_streak >= getattr(config, "PROXY_FAIL_STRIP_THRESHOLD", 3):
                    self._strip()
                else:
                    self._rotate()
                raise RetryProxyError("代理入口已处理，重试当前账号") from e
            except RiskControlError:
                gate.record("risk", time.monotonic() - started)
                gate.cooldown(cooldown)
                # 风控只降速/冷却，不以跨地区跳转替代等待，也不进入节点死名单。
                raise
            except Exception:
                gate.record("api_error", time.monotonic() - started)
                raise
            self.fail_streak = 0
            kind = "api_error" if isinstance(result, dict) and result.get("code", 0) else "ok"
            gate.record(kind, time.monotonic() - started)
            return result

    def snapshot(self):
        with self.lock:
            return {"proxy_active": bool(self.active_proxy), "independent": self.managed,
                    "node": self.node, "ip": self.ip, **self.gate.snapshot()}


def get_route(key, proxy_url=None, clash=None, managed=False):
    with _REGISTRY_LOCK:
        route = _ROUTES.get(key)
        if route is None:
            route = _ROUTES[key] = ProxyRoute(proxy_url, clash, managed)
        return route


@contextmanager
def pause_core_routes(api_url, secret):
    """热加载时等待本核心账号请求结束，恢复选择与失效连接后再放行。"""
    with _REGISTRY_LOCK:
        routes = sorted({r for r in _ROUTES.values() if r.managed and r.clash
                         and r.clash.api_url == api_url and r.clash.secret == secret}, key=id)
    with ExitStack() as stack:
        for route in routes:
            stack.enter_context(route.lock)
        yield routes


def risk_cooldown(single_account):
    base = config.SINGLE_ACCOUNT_RISK_COOLDOWN if single_account else config.RISK_COOLDOWN
    return base + random.uniform(0, 60)
