"""账号组合池：账号入口稳定绑定，风控冷却，代理连接故障才切节点。

主池与分片共享 ProxyRoute；同一实际 IP 共用限速和冷却，不把账号数当成
有效出口数。内置核心每账号一端口/一选择组，外部共享入口不自动切全局节点。
"""
import json
import os
import threading
from urllib.parse import urlparse

import config
from api_client import RiskControlError
from proxy_pool import RetryProxyError, get_route, risk_cooldown


class ComboPool:
    """鸭子类型模拟 BiliAPIClient，保留各采集模块的调用签名。"""

    def __init__(self, accounts, clash=None, proxy_url=None, routes=None):
        self._accounts = list(accounts)
        if not self._accounts:
            raise ValueError("账号池不能为空")
        self._idx = 0
        self._lock = threading.Lock()
        if routes is None:
            # 兼容外部调用构造单入口池；共享入口不修改外部应用的选择组。
            key = ("external-shared", proxy_url) if proxy_url else ("direct",)
            shared = get_route(key, proxy_url, clash, managed=False)
            routes = {name: shared for name, _ in self._accounts}
        self._routes = routes
        for name, client in self._accounts:
            self._routes[name].attach(client)

    def get(self, url, params=None, headers=None, **kw):
        return self.run(lambda c: c.get(url, params=params, headers=headers, **kw), desc=url[-40:])

    def post(self, url, data=None, params=None, **kw):
        return self.run(lambda c: c.post(url, data=data, params=params, **kw), desc=url[-40:])

    def get_raw(self, url, params=None, **kw):
        return self.run(lambda c: c.get_raw(url, params=params, **kw), desc=url[-40:])

    @property
    def current(self):
        with self._lock:
            return self._accounts[self._idx]

    def get_cookies_dict(self):
        return self.current[1].get_cookies_dict()

    def update_cookies(self, cookies):
        self.current[1].update_cookies(cookies)

    def run(self, fn, desc=""):
        # 重试预算属于本次请求，其他线程的成功不能重置它。
        risk_marks, rounds = set(), 0
        while True:
            with self._lock:
                idx = self._idx
                name, client = self._accounts[idx]
            route = self._routes[name]
            try:
                result = route.execute(client, fn, risk_cooldown(len(self._accounts) == 1))
            except RetryProxyError:
                continue
            except RiskControlError as e:
                with self._lock:
                    risk_marks.add(idx)
                    if len(risk_marks) == len(self._accounts):
                        rounds += 1
                        risk_marks.clear()
                        if rounds >= config.MAX_RISK_ROUNDS:
                            raise RiskControlError(
                                f"{desc} 连续 {rounds} 圈账号风控，放弃本单元") from e
                    self._idx = (idx + 1) % len(self._accounts)
                print(f"[Pool] 账号[{name}] 触发风控，出口已进入冷却，切换下一账号尝试")
                continue
            return result

    def shard_pools(self):
        """分片只分账号，出口状态与主池共用，降级/重探不会在分片间失联。"""
        return [ComboPool([(name, client)], routes={name: self._routes[name]})
                for name, client in self._accounts]

    def proxy_status(self):
        """出口诊断不包含订阅、控制器密钥或带认证的代理 URL。"""
        return {name: self._routes[name].snapshot() for name, _ in self._accounts}


def _external_routes(accounts, api):
    from clash_ctl import ClashCtl
    secret = config.CLASH_SECRET if api == config.CLASH_API_URL else ""
    ctl = ClashCtl(api, secret, config.CLASH_GROUP)
    if not ctl.available():
        return None
    raw = os.environ.get("CLASH_ACCOUNT_ROUTES", getattr(config, "CLASH_ACCOUNT_ROUTES", ""))
    if raw:
        try:
            mapping = json.loads(raw) if isinstance(raw, str) else raw
            entries = []
            for name, _ in accounts:
                entry = mapping[name]
                url, group = entry["proxy_url"], entry["group"]
                if not isinstance(url, str) or not isinstance(group, str) or not group:
                    raise ValueError("账号入口配置格式错误")
                parsed = urlparse(url)
                if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                        or not parsed.port or not isinstance(group, str) or not group):
                    raise ValueError("入口必须为本机 HTTP 代理和非空选择组")
                entries.append((name, url, group))
            if len({urlparse(url).port for _, url, _ in entries}) != len(entries) or len({g for _, _, g in entries}) != len(entries):
                raise ValueError("账号入口和选择组必须各自独立")
            routes = {}
            for name, url, group in entries:
                account_ctl = ClashCtl(api, secret, group)
                if not account_ctl.refresh_nodes():
                    raise ValueError("账号选择组无可用节点")
                routes[name] = get_route((api, secret, group, url), url, account_ctl, managed=True)
            print(f"[Pool] 外部核心独立账号入口: {len(routes)} 个")
            return routes
        except (ValueError, TypeError, KeyError):
            print("[Pool] 外部账号入口配置无效，回退共享静态入口；不会修改外部全局选择组")
    if not ctl.refresh_nodes():
        return None
    shared = get_route(("external-shared", config.CLASH_PROXY_URL),
                       config.CLASH_PROXY_URL, ctl, managed=False)
    print("[Pool] 使用外部共享静态入口；同一出口统一限速，不自动切换全局节点")
    return {name: shared for name, _ in accounts}


def _discover_ip_pool(accounts):
    """显式外部配置优先；订阅存在时优先内置隔离核心，再探测外部共享入口。"""
    if config.CLASH_ENABLED:
        routes = _external_routes(accounts, config.CLASH_API_URL)
        if routes:
            return routes
    if config.SUB_URLS:
        from proxy_core import get_core
        core = get_core(config.SUB_URLS, group=config.CLASH_GROUP or "profiler",
                        account_names=[name for name, _ in accounts])
        if core.start():
            bindings = core.account_routes()
            routes = {}
            for name, _ in accounts:
                if name not in bindings:
                    routes[name] = get_route(("direct",))
                    continue
                ctl, url = bindings[name]
                routes[name] = get_route((ctl.api_url, ctl.secret, ctl.group, url),
                                         url, ctl, managed=True)
                routes[name].sync_generation(core.generation)
            return routes
        print("[Pool] 内置核心不可用，尝试外部共享入口或直连")
    for api in dict.fromkeys(["http://127.0.0.1:9090", "http://127.0.0.1:9999", "http://127.0.0.1:9097"]):
        if config.CLASH_ENABLED and api == config.CLASH_API_URL:
            continue
        routes = _external_routes(accounts, api)
        if routes:
            return routes
    direct = get_route(("direct",))
    return {name: direct for name, _ in accounts}


def build_pool(main_client):
    """主号与小号绑定稳定入口，首次按真实 IP 去重，自检失败按入口降级。"""
    from auth import load_extra_clients
    accounts = [("主号", main_client)] + load_extra_clients()
    routes = _discover_ip_pool(accounts)
    pool = ComboPool(accounts, routes=routes)
    for name, client in accounts:
        routes[name].prepare(client)
    status = pool.proxy_status()
    measured = {s["ip"] for s in status.values() if s["proxy_active"] and s["ip"]}
    active = sum(s["proxy_active"] for s in status.values())
    print(f"[Pool] {len(accounts)} 个账号，{active} 个代理绑定，实测 {len(measured)} 个不同出口 IP"
          "（相同或未知出口共享限速；风控遵守冷却）")
    return pool
