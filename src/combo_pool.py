"""账号×IP 组合池：风控时换"新号+新IP"重试，长冷却为最后手段

ComboPool 鸭子类型模拟 BiliAPIClient（get/post/get_raw/get_cookies_dict/update_cookies），
各采集阶段/函数零签名改动：原来的 client 实参直接换成 pool 即可。
每个请求经 run() 包装——风控时 rotate()（下一账号+切节点）后原请求重试，
翻页游标保留在采集函数内部，断点天然不丢。

降级链（任何一层坏了退到下一层，流水线不中断）：
  账号×IP 组合轮换 → IP 池故障摘代理 → 账号轮转+直连 → 整圈风控 600s 长冷却
  → 连续 MAX_RISK_ROUNDS 圈仍失败抛 RiskControlError（阶段层按失败降级跳过本单元）

注意（架构约束）：内置核心为单 mixed-port 单 select 组，IP 维度全局单点——
所有账号共享同一出口 IP，切节点全局生效（shard 分片间同样如此）。
"""
import random
import threading
import time

from config import (CLASH_API_URL, CLASH_ENABLED, CLASH_GROUP, CLASH_PROXY_URL,
                    CLASH_SECRET, MAX_RISK_ROUNDS, NAV_URL, RISK_COOLDOWN, SUB_URLS,
                    PROXY_RETRY_AFTER, SINGLE_ACCOUNT_RISK_COOLDOWN)
from api_client import ProxyConnError, RiskControlError

_PROXY_FAIL_STRIP_THRESHOLD = 3   # 代理连续连接失败多少次后判定 IP 池不可用、摘代理转直连
# 摘代理恢复重探间隔与单账号冷却基准已迁移至
# config.PROXY_RETRY_AFTER / config.SINGLE_ACCOUNT_RISK_COOLDOWN


class ComboPool:
    """账号×IP 组合池。

    线程模型：池状态迁移（_idx/风控标记/圈计数/代理故障计数/冷却截止时刻）由
    self._lock 保护；fn(client) 的实际请求执行不放在锁内——请求本身不序列化，
    成员 client 自身已带 RLock（BiliAPIClient 限速锁）。整圈风控的长冷却只记录
    冷却截止时刻（锁内读写 _cooldown_until），实际等待移出锁外统一执行，
    避免冷却期其他线程在同一把锁上串行阻塞并继续撞风控。
    web.py 多后台 job 各建一池、共享主号 client 的场景下，池内状态不会竞态。
    """

    def __init__(self, accounts: list, clash=None, proxy_url: str | None = None):
        # 副作用：池成员的 raise_on_risk 被永久置 True（风控改抛信号由本池接管），
        # 入池后的 client 不应再被裸用走 api_client 旧冷却路径。
        self._accounts = list(accounts)     # [(名字, BiliAPIClient)]，[0] 恒为主号
        self._clash = clash                 # ClashCtl 或 None（无 IP 池）
        self._proxy_url = proxy_url
        self._idx = 0
        self._risk_marks = [False] * len(self._accounts)
        self._rounds = 0
        self._proxy_fail_streak = 0
        self._cooldown_until = 0.0          # 全局冷却截止时刻（时间戳），0 表示无冷却
        self._proxy_backup = None           # 摘代理降级时备份 (proxy_url, clash) 供恢复
        self._proxy_strip_ts = 0.0          # 摘代理时刻（距上次恢复尝试的计时起点）
        self._lock = threading.Lock()
        self._restore_lock = threading.Lock()   # 代理恢复自检串行化（_maybe_restore_proxy）
        for _, c in self._accounts:
            c.raise_on_risk = True          # 池成员风控改抛信号，由本池接管
            if proxy_url:
                c.set_proxy(proxy_url)

    # ---- 鸭子类型：模拟 BiliAPIClient ----
    def get(self, url, params=None, headers=None, **kw):
        return self.run(lambda c: c.get(url, params=params, headers=headers, **kw), desc=url[-40:])

    def post(self, url, data=None, params=None, **kw):
        return self.run(lambda c: c.post(url, data=data, params=params, **kw), desc=url[-40:])

    def get_raw(self, url, params=None, **kw):
        return self.run(lambda c: c.get_raw(url, params=params, **kw), desc=url[-40:])

    def get_cookies_dict(self) -> dict:
        # user_collector.get_user_space_info 等会直接取当前账号 Cookie；锁内读 _idx
        with self._lock:
            client = self._accounts[self._idx][1]
        return client.get_cookies_dict()

    def update_cookies(self, cookies):
        # 登录路径不进池；仅为鸭子类型完整性防御；锁内读 _idx
        with self._lock:
            client = self._accounts[self._idx][1]
        client.update_cookies(cookies)

    @property
    def current(self):
        with self._lock:
            return self._accounts[self._idx]

    def rotate(self, reason: str = ""):
        """换下一账号 + 切下一节点（切节点失败仅换号并记日志）。

        仅在 run() 持锁路径调用，自身不再取锁。
        """
        self._idx = (self._idx + 1) % len(self._accounts)
        name = self._accounts[self._idx][0]
        if self._clash:
            nxt = self._clash.pick_next_node()
            if nxt and self._clash.switch_node(nxt):
                print(f"[Pool] {reason} → 换号[{name}] + 切节点[{nxt}]")
                return
            print(f"[Pool] {reason} → 换号[{name}]（切节点失败，保持当前 IP）")
        else:
            print(f"[Pool] {reason} → 换号[{name}]")

    def _cooldown_seconds(self) -> float:
        """风控长冷却基准时长：单账号子池换号无意义（冷却只是等风控消退），
        缩短至 SINGLE_ACCOUNT_RISK_COOLDOWN；多账号池保持 RISK_COOLDOWN"""
        return SINGLE_ACCOUNT_RISK_COOLDOWN if len(self._accounts) == 1 else RISK_COOLDOWN

    def _wait_cooldown(self):
        """全局冷却等待（锁外执行）：锁内只读写截止时刻，实际 sleep 不持锁，
        冷却期其他线程不会被锁串行阻塞着继续撞风控"""
        while True:
            with self._lock:
                remaining = self._cooldown_until - time.time()
                if remaining <= 0:
                    self._cooldown_until = 0.0
                    return
            print(f"[Pool] 全局冷却中，等待 {remaining:.0f} 秒...")
            time.sleep(remaining)

    def _maybe_restore_proxy(self):
        """摘代理降级满 PROXY_RETRY_AFTER 秒后，下次请求前重跑代理自检，
        成功则恢复代理与 clash 控制器（自检走网络，挂回/摘掉在锁内，自检本身锁外）。

        _restore_lock 非阻塞串行化：多线程同时到期时只允许一个线程跑自检，
        其余直接跳过本次（等下一次调用再判），避免并发重复自检与状态挂回竞态。"""
        if not self._restore_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                if self._proxy_backup is None or self._proxy_url is not None:
                    return
                if time.time() - self._proxy_strip_ts < PROXY_RETRY_AFTER:
                    return
                # 本次尝试无论成败都重新计时（_strip_proxy 会刷新 _proxy_strip_ts），
                # 避免每次请求都打自检
                proxy_url, clash = self._proxy_backup
                self._proxy_url = proxy_url
                self._clash = clash
                self._proxy_fail_streak = 0
                for _, c in self._accounts:
                    c.set_proxy(proxy_url)
            print("[Pool] 摘代理已满重试间隔，重跑代理自检尝试恢复...")
            if _proxy_selfcheck(self, max_tries=3):
                with self._lock:
                    self._proxy_backup = None
                    self._proxy_strip_ts = 0.0
                print("[Pool] 代理自检通过，已恢复代理出口与节点控制器")
                return
            print("[Pool] 代理自检仍未通过，维持直连降级")
            with self._lock:
                self._strip_proxy()
        finally:
            self._restore_lock.release()

    def run(self, fn, desc: str = ""):
        """执行 fn(client)；风控→rotate 重试；整圈风控→长冷却；冷却 MAX_RISK_ROUNDS 圈仍败→抛"""
        while True:
            self._wait_cooldown()       # 锁外统一等待：未到冷却截止时刻先等
            self._maybe_restore_proxy() # 摘代理降级到期则尝试恢复代理
            with self._lock:
                idx = self._idx         # 锁内快照账号，锁外执行 fn
                name, client = self._accounts[idx]
            try:
                result = fn(client)     # 实际请求不持锁：请求本身不序列化
            except ProxyConnError as e:
                with self._lock:
                    if self._proxy_url is None:
                        raise   # 已转直连仍报代理错误属异常，直接上抛
                    self._on_proxy_fault(e)
                # 代理故障不消耗风控圈：原地重试（已切节点或已转直连）
                continue
            except RiskControlError as e:
                with self._lock:
                    self._risk_marks[idx] = True    # 风控标记打在快照账号上
                    print(f"[Pool] 账号[{name}] 触发风控（{str(e)[:60]}）")
                    if all(self._risk_marks):
                        self._rounds += 1
                        if self._rounds >= MAX_RISK_ROUNDS:
                            raise RiskControlError(
                                f"{desc} 连续 {self._rounds} 圈全账号风控，放弃本单元") from e
                        wait = self._cooldown_seconds() + random.uniform(0, 60)
                        print(f"[Pool] 全部账号均触发风控，长冷却 {wait:.0f} 秒"
                              f"（最后手段，第 {self._rounds}/{MAX_RISK_ROUNDS} 圈）...")
                        # 锁内只记录冷却截止时刻，实际等待移出锁外（见 _wait_cooldown）
                        self._cooldown_until = time.time() + wait
                        self._risk_marks = [False] * len(self._accounts)
                    self.rotate(f"风控({desc})")
                continue
            # 业务成功：清零风控圈与代理故障计数（一次霉运不污染后续单元）
            with self._lock:
                self._rounds = 0
                self._risk_marks = [False] * len(self._accounts)
                self._proxy_fail_streak = 0
            return result

    def _on_proxy_fault(self, e: Exception):
        """代理连接失败：先切节点重试；连续失败达阈值判定 IP 池不可用，摘代理转直连。

        仅在 run() 持锁路径调用，自身不再取锁。
        """
        self._proxy_fail_streak += 1
        if self._proxy_fail_streak >= _PROXY_FAIL_STRIP_THRESHOLD:
            print(f"[Pool] 代理连续 {self._proxy_fail_streak} 次连接失败，判定 IP 池不可用，摘代理转直连: {e}")
            self._strip_proxy()
            return
        if self._clash:
            # 故障即把当前节点记入死名单（即时反应，不等 600s 一轮的健康检查），再切下一个
            cur = self._clash.current_node()
            if cur:
                self._clash.mark_dead(cur)
            nxt = self._clash.pick_next_node()
            if nxt and self._clash.switch_node(nxt):
                print(f"[Pool] 代理连接失败，已切换节点 → {nxt}（第 {self._proxy_fail_streak} 次）")
                return
        print(f"[Pool] 代理连接失败（第 {self._proxy_fail_streak} 次，无法切节点）: {e}")

    def _strip_proxy(self):
        """摘掉池内全部账号的代理，转直连（IP 池故障降级）。

        由 run() 持锁路径或 build_pool 建池期（尚未共享）调用，自身不再取锁。
        降级时备份 (proxy_url, clash) 并记录时刻：PROXY_RETRY_AFTER 秒后由
        _maybe_restore_proxy 重跑自检，通过则恢复代理与节点控制器。
        """
        if self._proxy_url is not None:
            self._proxy_backup = (self._proxy_url, self._clash)
        self._proxy_url = None
        self._clash = None
        self._proxy_strip_ts = time.time()
        for _, c in self._accounts:
            c.set_proxy(None)

    def shard_pools(self) -> list["ComboPool"]:
        """按账号分片：每账号一个独立子池（共享 clash/IP 池），供多线程并行采集。

        限速是 per-client 实例的，N 个账号并行 ≈ N 倍吞吐。子池风控轮换只切节点
        不换号（其它号正被别的分片占用），整圈风控的长冷却兜底逻辑照旧。
        共享 clash 的切节点对所有分片同时生效（mixed 端口全局路由），属预期。
        子池构造走 __new__ 跳过 __init__：raise_on_risk/set_proxy 副作用已由主池施加。
        """
        pools = []
        for name, client in self._accounts:
            p = ComboPool.__new__(ComboPool)
            p._accounts = [(name, client)]
            p._clash = self._clash
            p._proxy_url = self._proxy_url
            p._idx = 0
            p._risk_marks = [False]
            p._rounds = 0
            p._proxy_fail_streak = 0
            p._cooldown_until = 0.0
            p._proxy_backup = None
            p._proxy_strip_ts = 0.0
            p._lock = threading.Lock()
            p._restore_lock = threading.Lock()
            pools.append(p)
        return pools


def _discover_ip_pool():
    """IP 池来源自动发现：外部控制器（显式配置优先，再探测本机常见端口）→ SUB_URLS 内置核心 → 无"""
    from clash_ctl import ClashCtl
    candidates = [CLASH_API_URL] if CLASH_ENABLED else []
    candidates += ["http://127.0.0.1:9090", "http://127.0.0.1:9999", "http://127.0.0.1:9097"]
    seen = set()
    for api in candidates:
        if api in seen:
            continue
        seen.add(api)
        ctl = ClashCtl(api, CLASH_SECRET if api == CLASH_API_URL else "", CLASH_GROUP)
        # 先 available 再 refresh：控制器不通就不必刷新节点列表
        if ctl.available() and (nodes := ctl.refresh_nodes()):
            print(f"[Pool] 发现外部 Clash 控制器 {api}（组 {ctl.group}，{len(nodes)} 节点）")
            return ctl, CLASH_PROXY_URL
    if SUB_URLS:
        from proxy_core import get_core
        # 单例：web.py 重新生成 job 反复建池时复用同一内置核心，不累积拉起子进程
        core = get_core(SUB_URLS, group=CLASH_GROUP or "profiler")
        ctl = core.start()
        if ctl:
            return ctl, f"http://127.0.0.1:{core.mix_port}"
        print("[Pool] 内置核心启动失败，禁用 IP 池")
    return None, None


def _proxy_selfcheck(pool: ComboPool, max_tries: int = 5) -> bool:
    """启动自检：经代理发一次轻量请求（NAV）；当前节点死亡则切下一个重试，
    连续 max_tries 次仍不通才由调用方摘代理转直连（订阅里常混有死节点）"""
    _, client = pool.current
    for attempt in range(max_tries):
        try:
            data = client.get(NAV_URL, timeout=8)
            if isinstance(data, dict) and "code" in data:
                return True
        except RiskControlError:
            return True     # 账号风控说明代理链路是通的，不应摘代理
        except Exception:
            pass
        if pool._clash and attempt < max_tries - 1:
            cur = pool._clash.current_node()
            if cur:
                pool._clash.mark_dead(cur)      # 自检不通的节点直接进死名单，轮换不再选中
            nxt = pool._clash.pick_next_node()
            if nxt and pool._clash.switch_node(nxt):
                print(f"[Pool] 自检节点不通，切换到 [{nxt}] 重试")
            else:
                break       # 无节点可切，提前结束
    return False


def build_pool(main_client) -> ComboPool:
    """登录完成后构建组合池：主号+小号池 + IP 池自动发现；代理自检失败自动摘代理降级。

    副作用：main_client.raise_on_risk 被永久置 True（风控改抛信号由池接管），
    建池后该 client 不应再被裸用走 api_client 旧冷却路径。
    """
    from auth import load_extra_clients   # 延迟导入避免循环依赖
    accounts = [("主号", main_client)] + load_extra_clients()
    if len(accounts) > 1:
        names = "、".join(n for n, _ in accounts[1:])
        print(f"[Pool] 小号池: {len(accounts) - 1} 个可用（{names}），采集阶段多号并行分片，风控时换号+切节点")

    clash, proxy_url = _discover_ip_pool()
    pool = ComboPool(accounts, clash=clash, proxy_url=proxy_url)
    if proxy_url and not _proxy_selfcheck(pool):
        print("[Pool] 代理自检失败，摘代理转直连（仅账号轮转）")
        pool._strip_proxy()
    return pool
