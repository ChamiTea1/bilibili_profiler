"""Clash/mihomo 外部控制器封装：列节点/切节点（换出口 IP）

所有失败静默降级（返回 False/[]/None），不抛给上层——IP 池不可用时由 ComboPool 走降级链。
注意：直连控制器（requests 裸调用），不走 BiliAPIClient（无 B 站限速语义）。
线程安全：多号并行分片采集时多个池共享同一控制器，节点轮换由锁串行化。

**必须禁用环境代理**：控制器永远在本机（127.0.0.1），而很多代理客户端会把
http_proxy/https_proxy/all_proxy 写进 shell。此时 requests 默认会把这些本地调用
也送进代理 → 收到 502/超时 → 表现为「内置核心就绪轮询 60 次全失败、IP 池建不起来、
启动阶段像卡死」。注意 Python 的 no_proxy 只支持后缀匹配，用户写 127.* 并不生效，
所以这里不能指望环境变量，只能硬关 trust_env。
"""
import re
import threading
import time
from urllib.parse import urlparse

import requests

from config import PROXY_NODE_DEAD_TTL

# 控制器请求一律不走环境代理（见模块 docstring；本地回环调用被代理劫持会让建池卡死）
_SESSION = requests.Session()
_SESSION.trust_env = False

# 同一核心的不同账号选择组共享控制锁和死名单，故障信息不会在分片间丢失。
_STATES = {}
_STATES_LOCK = threading.Lock()

# 组内伪节点/内置策略名，不进入轮换
_PSEUDO = {"DIRECT", "REJECT", "PASS", "GLOBAL", "COMPATIBLE"}

# 控制器地址只允许回环：external-controller 无强认证，监听非回环地址会被局域网滥用
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# 地区识别：旗帜 emoji（区域指示符对）优先，其次节点名中的地区关键字
_REGION_FLAG_RE = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
_REGION_WORDS = ["香港", "台湾", "澳门", "日本", "韩国", "新加坡", "美国", "加拿大",
                 "英国", "德国", "法国", "荷兰", "俄罗斯", "澳大利亚", "印度", "土耳其",
                 "泰国", "越南", "马来西亚", "菲律宾", "印尼", "巴西", "阿根廷"]


def _region_of(name: str) -> str:
    """从节点名提取地区标识（旗帜 emoji 或地区关键字）；识别不出返回空串"""
    m = _REGION_FLAG_RE.search(name or "")
    if m:
        return m.group(0)
    for w in _REGION_WORDS:
        if w in (name or ""):
            return w
    return ""


class ClashCtl:
    def __init__(self, api_url: str, secret: str = "", group: str = ""):
        self.api_url = api_url.rstrip("/")
        host = urlparse(self.api_url).hostname or ""
        if host not in _LOOPBACK_HOSTS:
            print(f"[Clash] 安全警告：控制器地址 {self.api_url} 非回环地址，"
                  f"控制器接口可能被网络内其他主机滥用，建议改用 127.0.0.1")
        self.secret = secret
        self.group = group          # 为空时首次读组自动挑选第一个非 GLOBAL 的 Selector 组
        self._nodes: list[str] = []
        self._region_cursor: dict[str, int] = {}   # 各地区内部轮换位置
        with _STATES_LOCK:
            state = _STATES.setdefault((self.api_url, secret),
                                       (threading.RLock(), {}))
        self._lock, self._dead = state

    def mark_dead(self, name: str, ttl: float = PROXY_NODE_DEAD_TTL):
        """手动标记节点为死（代理故障时由组合池即时上报，不等下一轮健康检查）。
        手动标记优先级高于健康检查历史：历史最多滞后 600s，刚发生的故障更新鲜，
        健康检查通道不得提前复活手动标记的节点（只能等 TTL 到期）"""
        if name:
            with self._lock:
                self._dead[name] = (time.time() + ttl, True)

    def _update_dead_from_history(self, proxies: dict):
        """读取各节点的健康检查历史（proxy_core 为每个订阅 provider 开启了
        health-check，interval 600s）：最近一次探测 delay<=0 判死（进 TTL 名单），
        探测恢复成功则移出死名单——但只复活健康检查自己标记的，不动手动标记"""
        now = time.time()
        for name, p in proxies.items():
            if not isinstance(p, dict):
                continue
            history = p.get("history") or []
            if not history:
                continue                        # 未探测过：不判死，给首次机会
            last = history[-1].get("delay", 0) if isinstance(history[-1], dict) else 0
            if last <= 0:
                self._dead.setdefault(name, (now + PROXY_NODE_DEAD_TTL, False))
            elif not (self._dead.get(name) or (0, False))[1]:
                self._dead.pop(name, None)      # 仅复活非手动标记的条目

    def _is_alive(self, name: str) -> bool:
        ent = self._dead.get(name)
        if ent is None:
            return True
        if time.time() >= ent[0]:
            # pop 而非 del：与健康检查通道并发时重复删除不抛 KeyError
            self._dead.pop(name, None)          # TTL 到期，允许复活重试
            return True
        return False

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.secret}"} if self.secret else {}

    def available(self) -> bool:
        """控制器连通性自检"""
        try:
            r = _SESSION.get(f"{self.api_url}/proxies", headers=self._headers(), timeout=3)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _fetch_group(self) -> dict:
        """读节点组信息；group 为空时自动挑选第一个非 GLOBAL、含节点的 Selector 组。
        返回 {"name", "all", "now"}；请求异常向上抛（由调用方静默降级）。"""
        with self._lock:
            return self._fetch_group_locked()

    def _fetch_group_locked(self) -> dict:
        r = _SESSION.get(f"{self.api_url}/proxies", headers=self._headers(), timeout=5)
        r.raise_for_status()
        body = r.json()
        # 防御非预期 JSON 结构（如返回 list/str）：不做 isinstance 检查会让
        # .get 抛 AttributeError 逃逸出 list_nodes 的 requests.RequestException 兜底
        proxies = body.get("proxies") if isinstance(body, dict) else None
        if not isinstance(proxies, dict):
            return {"name": "", "all": [], "now": ""}
        # 同核心共享 RLock，健康检查和手动故障上报不会相互覆盖。
        self._update_dead_from_history(proxies)
        if self.group:
            g = proxies.get(self.group)
            if not isinstance(g, dict):
                g = {}
            return {"name": self.group, "all": g.get("all") or [], "now": g.get("now", "")}
        for name, g in proxies.items():
            if name == "GLOBAL" or not isinstance(g, dict):
                continue
            if g.get("type") == "Selector" and (g.get("all") or []):
                self.group = name
                return {"name": name, "all": g["all"], "now": g.get("now", "")}
        return {"name": "", "all": [], "now": ""}

    def list_nodes(self) -> list[str]:
        """组内可轮换节点（过滤伪节点 + 死节点名单）；失败返回 []。

        死节点全灭时返回空列表，待 TTL 到期后再探测，避免重复撞同一批坏出口。"""
        with self._lock:
            return self._list_nodes_locked()

    def _list_nodes_locked(self) -> list[str]:
        try:
            g = self._fetch_group()
        except requests.RequestException:
            return []
        alive = [n for n in g["all"] if n and n.upper() not in _PSEUDO]
        filtered = [n for n in alive if self._is_alive(n)]
        if 0 < len(filtered) < len(alive):
            print(f"[Clash] 节点池 {len(alive)} 个，剔除当前不通 {len(alive) - len(filtered)} 个"
                  f"（健康检查/故障上报，TTL {PROXY_NODE_DEAD_TTL}s 后允许复活）")
        return filtered

    def refresh_nodes(self) -> list[str]:
        """刷新并缓存节点列表"""
        self._nodes = self.list_nodes()
        return self._nodes

    def current_node(self) -> str | None:
        """当前选中节点；请求失败或组内无选中（空串）均归一为 None"""
        try:
            return self._fetch_group()["now"] or None
        except requests.RequestException:
            return None

    def pick_next_node(self, exclude=()) -> str | None:
        """组内轮换推进，**跨地区跳跃**：同机场同地区节点常共享出口 IP，
        顺序轮换等于白换，故优先切到不同地区的节点（地区内再轮询）。
        节点为空、或单节点组（唯一节点即当前节点）无节点可换时返回 None"""
        with self._lock:
            # 每次重新消费健康检查和手动死名单；缓存仅用于展示，不决定存活状态。
            self.refresh_nodes()
            if not self._nodes:
                return None
            cur = self.current_node()
            if len(self._nodes) == 1 and self._nodes[0] == cur:
                return None                              # 单节点组：无节点可换，让消费方感知
            cur_region = _region_of(cur) if cur else None
            # 地区循环顺序（按节点列表首次出现排序，去重），从当前地区的下一个开始
            regions = list(dict.fromkeys(_region_of(n) for n in self._nodes))
            if cur_region in regions:
                i = regions.index(cur_region)
                ordered = regions[i + 1:] + regions[:i + 1]
            else:
                ordered = regions
            for region in ordered:
                candidates = [n for n in self._nodes
                              if _region_of(n) == region and n != cur and n not in exclude]
                if not candidates:
                    continue
                pos = self._region_cursor.get(region, 0) % len(candidates)
                self._region_cursor[region] = pos + 1
                return candidates[pos]
            return None

    def rotate_node(self, exclude=()) -> str | None:
        """选节点与提交选择放在同一控制锁内，失败不报告已切换。"""
        with self._lock:
            nxt = self.pick_next_node(exclude=exclude)
            return nxt if nxt and self.switch_node(nxt) else None

    def reload_config(self, path: str) -> bool:
        """仅供本项目内置核心添加账号入口；外部核心不调用此接口。"""
        try:
            with self._lock:
                r = _SESSION.put(f"{self.api_url}/configs", headers=self._headers(),
                                 json={"path": path, "force": True}, timeout=10)
                return r.status_code in (200, 204)
        except requests.RequestException:
            return False

    def switch_node(self, name: str) -> bool:
        """切换节点组选择（换出口 IP）；失败返回 False"""
        if not name:
            return False
        try:
            with self._lock:
                g = self._fetch_group()
                if not g["name"]:
                    return False
                r = _SESSION.put(
                    f"{self.api_url}/proxies/{requests.utils.quote(g['name'], safe='')}",
                    headers=self._headers(), json={"name": name}, timeout=5)
                if r.status_code in (200, 204):
                    return True
                return False
        except requests.RequestException:
            return False
