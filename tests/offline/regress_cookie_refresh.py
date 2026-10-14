# -*- coding: utf-8 -*-
"""登录态续期与回写回归（9 项）

覆盖：续期节流记账、本地伪字段绝不进 session、刷新成功落盘 token+时间戳、
主动续期不重复触发、续期失败不改判账号可用性、无令牌零尝试、三态分类、
回写不覆盖其它进程的新登录态、跨进程锁真实互斥。

离线回归脚本：全部走临时目录与假客户端，**不联网、不读真实 Cookie、不消耗 LLM**。
从仓库任意目录均可运行：  python tests/offline/regress_cookie_refresh.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from Crypto.PublicKey import RSA

import auth

TMP = tempfile.mkdtemp(prefix="cookie_refresh_")
auth.COOKIES_DIR = os.path.join(TMP, "cookies")
os.makedirs(auth.COOKIES_DIR, exist_ok=True)
MAIN_PATH = os.path.join(TMP, "cookie.json")
auth.COOKIE_PATH = MAIN_PATH

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))


def write_cookie(path, token=None, stamp=None, sessdata="S"):
    d = {"SESSDATA": sessdata, "bili_jct": "J", "DedeUserID": "1"}
    if token is not None:
        d["_refresh_token"] = token
    if stamp is not None:
        d["_refreshed_at"] = str(stamp)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f)
    return d


def read_cookie(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------- 0) 刷新公钥必须可用 ----------
# c41020b 曾在插入 LoginRequiredError 时误改公钥最后一行（216→218 字符），导致
# _get_correspond_path() 抛 Incorrect padding、刷新路径长期全失效且无任何报错提示。
_key = _key_err = None
_correspond = ""
try:
    _key = RSA.import_key(auth.REFRESH_PUBKEY)
    _correspond = auth._get_correspond_path()
except Exception as e:
    _key_err = "%s: %s" % (type(e).__name__, e)
check("内置刷新公钥可解析且 correspondPath 可生成（坏掉则自动刷新全线失效）",
      _key is not None and len(_correspond) > 0,
      "(%s bits, %s)" % (_key.size_in_bits() if _key else "-", _key_err or "ok"))


# ---------- 1) 续期节流：记账式判定 ----------
now = time.time()
p1 = os.path.join(TMP, "due.json")
write_cookie(p1, token="t")                                  # 无时间戳
due_none = auth._due_for_refresh(p1)
write_cookie(p1, token="t", stamp=now - 86400)               # 1 天前
due_1d = auth._due_for_refresh(p1)
write_cookie(p1, token="t", stamp=now - 8 * 86400)           # 8 天前
due_8d = auth._due_for_refresh(p1)
write_cookie(p1, token="t", stamp="垃圾值")                   # 非法时间戳
due_bad = auth._due_for_refresh(p1)
check("续期节流：无记录→需要、1 天前→不需要、8 天前→需要、非法值→需要",
      due_none and not due_1d and due_8d and due_bad,
      "(%s/%s/%s/%s)" % (due_none, due_1d, due_8d, due_bad))


# ---------- 2) 假客户端：可配置的刷新响应 ----------
class FakeResp:
    def __init__(self, text): self.text = text


class FakeClient:
    """够用的 BiliAPIClient 替身：记录调用、可注入失败模式"""
    def __init__(self, refresh_code=0, fail_get_raw=False, new_token="new_tok"):
        self.cookies = {"SESSDATA": "S", "bili_jct": "J"}
        self.refresh_code = refresh_code
        self.fail_get_raw = fail_get_raw
        self.new_token = new_token
        self.posts = []
        self.got_raw = 0

    # --- 被测代码用到的接口 ---
    def update_cookies(self, d): self.cookies.update(d)
    def get_cookies_dict(self): return dict(self.cookies)
    def get_raw(self, url, **kw):
        self.got_raw += 1
        if self.fail_get_raw:
            raise RuntimeError("模拟网络故障")
        return FakeResp('<html><div id="1-name">CSRF-TOKEN</div></html>')
    def post(self, url, data=None, **kw):
        self.posts.append((url, dict(data or {})))
        if "cookie/refresh" in url:
            if self.refresh_code == 0:
                self.cookies["SESSDATA"] = "S-new"      # 模拟 Set-Cookie
            return {"code": self.refresh_code, "message": "模拟失败",
                    "data": {"refresh_token": self.new_token}}
        return {"code": 0}


# ---------- 3) 刷新成功：token 与时间戳都落盘 ----------
path = os.path.join(TMP, "alt1.json")
write_cookie(path, token="old_tok")
c = FakeClient()
c._refresh_token = "old_tok"
before = time.time()
result = auth._try_refresh_cookie(c, path)
saved = read_cookie(path)
check("刷新成功返回 REFRESH_OK 且文件同时更新 token 与续期时间戳",
      bool(result) and str(result) == "ok" and saved.get("_refresh_token") == "new_tok"
      and float(saved.get("_refreshed_at", 0)) >= before,
      "(result=%s token=%s)" % (result, saved.get("_refresh_token")))
check("刷新后节流即刻生效（同一天不会再刷）", not auth._due_for_refresh(path))


# ---------- 4) 本地伪字段绝不进 session ----------
client = FakeClient()
auth.BiliAPIClient = lambda: client
write_cookie(os.path.join(auth.COOKIES_DIR, "ath1.json"), token="tok1", stamp=now)
auth.verify_cookie = lambda cl: True
auth._try_refresh_cookie = lambda cl, p=None: auth.REFRESH_OK     # 不真刷，只验注入
loaded = auth.load_extra_clients()
leaked = [k for k in client.get_cookies_dict() if k in auth._LOCAL_FIELDS]
check("读文件→注入 session 时不泄漏本地伪字段（_refresh_token/_refreshed_at）",
      loaded and not leaked, "(泄漏 %s)" % (leaked or "无"))


# ---------- 5) 主动续期：到期才刷，且失败不改判可用性 ----------
calls = []
auth._try_refresh_cookie = lambda cl, p=None: (calls.append(p), auth.REFRESH_OK)[1]
write_cookie(os.path.join(auth.COOKIES_DIR, "ath1.json"), token="tok1", stamp=now)   # 刚刷过
auth.load_extra_clients()
check("未到续期窗口时不刷新（零额外请求）", calls == [], "(调用 %d 次)" % len(calls))

write_cookie(os.path.join(auth.COOKIES_DIR, "ath1.json"), token="tok1", stamp=now - 30 * 86400)
auth.load_extra_clients()
check("超过续期窗口时主动刷新一次", len(calls) == 1, "(调用 %d 次)" % len(calls))

calls.clear()
auth._try_refresh_cookie = lambda cl, p=None: (calls.append(p), auth.REFRESH_RETRY)[1]
kept = auth.load_extra_clients()
check("续期失败（网络/风控）不清除账号，继续用现有登录态",
      len(kept) == 1 and len(calls) == 1, "(保留 %d 个账号，尝试 %d 次)" % (len(kept), len(calls)))


# ---------- 6) 无令牌：零尝试 ----------
auth._try_refresh_cookie = lambda cl, p=None: (_ for _ in ()).throw(
    AssertionError("无 refresh_token 时不应尝试刷新"))
write_cookie(os.path.join(auth.COOKIES_DIR, "ath1.json"))       # 没有 _refresh_token
kept = auth.load_extra_clients()
check("没有 refresh_token 时完全不尝试刷新（哨兵）", len(kept) == 1)


# ---------- 7) 三态分类 ----------
auth._try_refresh_cookie = auth_orig = None      # 还原真实实现
import importlib
importlib.reload(auth)                            # 重新加载，拿回未被替换的真实函数
auth.COOKIES_DIR = os.path.join(TMP, "cookies")
auth.COOKIE_PATH = MAIN_PATH
dead = FakeClient(refresh_code=-101)
dead._refresh_token = "tok"
res_dead = auth._try_refresh_cookie(dead, os.path.join(TMP, "dead.json"))
netfail = FakeClient(fail_get_raw=True)
netfail._refresh_token = "tok"
res_net = auth._try_refresh_cookie(netfail, os.path.join(TMP, "net.json"))
notoken = FakeClient()
res_none = auth._try_refresh_cookie(notoken, os.path.join(TMP, "none.json"))
check("三态分类：-101→DEAD、网络异常→RETRY、无令牌→NONE",
      str(res_dead) == "dead" and str(res_net) == "retry" and str(res_none) == "none",
      "(%s/%s/%s)" % (res_dead, res_net, res_none))
check("三态真值：仅 OK 为真，其余为假（兼容既有 if 用法）",
      bool(auth.REFRESH_OK) and not auth.REFRESH_RETRY and not auth.REFRESH_DEAD
      and not auth.REFRESH_NONE)


# ---------- 8) 回写不得覆盖其它进程刷新的新登录态 ----------
path = os.path.join(TMP, "writeback.json")
write_cookie(path, token="disk_new")             # 磁盘上已是别进程刷出的新令牌
stale = FakeClient()
stale._refresh_token = "memory_old"              # 本进程手里还是旧令牌
wrote = auth.save_cookie_if_current(stale, path)
after = read_cookie(path)
check("磁盘令牌比本进程新时拒绝回写（不把账号写死）",
      wrote is False and after.get("_refresh_token") == "disk_new")

same = FakeClient()
same._refresh_token = "disk_new"
wrote2 = auth.save_cookie_if_current(same, path)
check("令牌一致时正常回写", wrote2 is True and read_cookie(path).get("SESSDATA") == "S")

write_cookie(path, token="disk_kept", sessdata="KEEP")
empty = FakeClient()
wrote3 = auth.save_cookie_if_current(empty, path)
after3 = read_cookie(path)
check("本进程没有令牌但磁盘有时不整份覆盖",
      wrote3 is False and after3.get("_refresh_token") == "disk_kept"
      and after3.get("SESSDATA") == "KEEP")

# 刷新后的 jar：空域旧值和统一域新值不能并存，刷新令牌不进 session
from api_client import BiliAPIClient
jar_client = BiliAPIClient()
jar_client.session.cookies.set("SESSDATA", "old", domain="")
jar_client.session.cookies.set("SESSDATA", "new", domain=".bilibili.com")
auth._replace_cookie_jar(jar_client, {
    "SESSDATA": "new", "bili_jct": "J", "_refresh_token": "不得入库", "_refreshed_at": "1"})
kept = [(c.name, c.value, c.domain) for c in jar_client.session.cookies]
check("刷新后重建 jar：只留统一域，刷新令牌不进 session",
      ("_refresh_token", "不得入库") not in [(n, v) for n, v, _d in kept]
      and [item for item in kept if item[0] == "SESSDATA"] == [("SESSDATA", "new", ".bilibili.com")]
      and ("bili_jct", "J", ".bilibili.com") in kept)


# ---------- 9) 跨进程锁：真实互斥 ----------
order = []
lock_path = os.path.join(TMP, "lock.json")
def worker(tag):
    with auth._refresh_lock(lock_path):
        order.append("+" + tag)
        time.sleep(0.15)
        order.append("-" + tag)
# daemon + join 超时：万一某平台的锁实现有问题，用例要"失败"而不是把 CI 挂死
t1 = threading.Thread(target=worker, args=("A",), daemon=True)
t2 = threading.Thread(target=worker, args=("B",), daemon=True)
t1.start(); time.sleep(0.02); t2.start()
t1.join(10); t2.join(10)
finished = not t1.is_alive() and not t2.is_alive()
check("刷新锁真实互斥（临界区不交叠，10 秒内完成）",
      finished and order in (["+A", "-A", "+B", "-B"], ["+B", "-B", "+A", "-A"]),
      "(完成=%s, %s)" % (finished, order))

print("")
print("==== 登录态续期与回写: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
