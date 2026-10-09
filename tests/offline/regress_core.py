# -*- coding: utf-8 -*-
"""主回归：本轮修复的定点验证（历史弹幕续采、线程池收尾、post 重试、限速预算等 20 项）

离线回归脚本：使用隔离临时库 + 假 HTTP/假 OpenAI，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_core.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile, time, threading

# 隔离库：绝不碰真实 data/profiler.db
TMP = tempfile.mkdtemp(prefix="regress_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

print("=== 1. P0-1 历史弹幕续采不丢日期 ===")
import danmaku_history as dh
DATES = ["2026-08-%02d" % d for d in range(1, 21)]
PUB = int(__import__("datetime").datetime(2026, 7, 15, tzinfo=__import__("datetime").timezone.utc).timestamp())
def varint(n):
    o = b""
    while True:
        x = n & 0x7F; n >>= 7; o += bytes([x | (0x80 if n else 0)])
        if not n: return o
class Resp:
    def __init__(s, b): s.content = b
class FC:
    def __init__(s, interrupt_after=None, fail=()): s.n=0; s.ia=interrupt_after; s.fail=set(fail); s.served=[]
    def get(s, u, params=None, **k):
        m = (params or {}).get("month", ""); return {"code": 0, "data": [d for d in DATES if d.startswith(m)]}
    def get_raw(s, u, params=None, **k):
        d = (params or {}).get("date"); s.n += 1
        if s.ia is not None and s.n > s.ia: raise KeyboardInterrupt()
        s.served.append(d)
        if d in s.fail: return Resp(b"<html>x</html>")
        e = bytes([8]) + varint(int(d[-2:])) + bytes([0x3A, 1, 0x78]); return Resp(bytes([10]) + varint(len(e)) + e)
def st(b):
    rows = storage.get_db().execute("SELECT key,value FROM phase_state WHERE bvid=? AND phase='danmaku'", (b,)).fetchall()
    kv = {r["key"]: r["value"] for r in rows}
    return kv, set((kv.get("fetched_dates") or "").split(",")) - {""}, set((kv.get("failed_dates") or "").split(",")) - {""}
try: dh.fetch_history_danmaku(1, FC(interrupt_after=5), PUB, bvid="BVA")
except KeyboardInterrupt: pass
c = FC(); dh.fetch_history_danmaku(1, c, PUB, bvid="BVA")
kv, fds, _ = st("BVA")
check("中断后续采补齐全部日期", len(fds) == 20 and len(c.served) == 15, "(请求 %d 天, 已采 %d)" % (len(c.served), len(fds)))
check("补齐后才写 done", kv.get("done") == "1")
try: dh.fetch_history_danmaku(1, FC(interrupt_after=5), PUB, bvid="BVB")
except KeyboardInterrupt: pass
dh.fetch_history_danmaku(1, FC(fail={"2026-08-05"}), PUB, bvid="BVB")
kv, fds, fls = st("BVB")
check("失败日挂账且不写 done", fls == {"2026-08-05"} and kv.get("done") is None, "(failed=%s done=%s)" % (sorted(fls), kv.get("done")))

print("=== 1b. 月份索引失败（code!=0，非异常路径）不得误标完成 ===")
import io, contextlib
class FCBadMonth(FC):
    """2026-08 的月份索引返回 code!=0 —— _fetch_month_dates 返回 None（不抛异常）。

    修复前该 None 被当成"本月无弹幕"的空列表：window_complete 保持 True，
    于是 0 条弹幕也照写 done=1，该月数据永久静默丢失且不再重试。"""
    def get(s, u, params=None, **k):
        m = (params or {}).get("month", "")
        if m == "2026-08":
            return {"code": -101, "message": "账号未登录"}
        return {"code": 0, "data": [d for d in DATES if d.startswith(m)]}
with contextlib.redirect_stdout(io.StringIO()):
    dh.fetch_history_danmaku(1, FCBadMonth(), PUB, bvid="BVD")
kv, fds, _ = st("BVD")
check("月份索引失败不写 done（保留续采入口）", kv.get("done") is None and len(fds) == 0,
      "(已采 %d 天, done=%s)" % (len(fds), kv.get("done")))
# 重跑（索引恢复正常）必须能补齐该月日期并正常标完成 —— 证明续采入口真的留住了
with contextlib.redirect_stdout(io.StringIO()):
    dh.fetch_history_danmaku(1, FC(), PUB, bvid="BVD")
kv2, fds2, _ = st("BVD")
check("重跑补齐失败月份后才写 done", len(fds2) == 20 and kv2.get("done") == "1",
      "(已采 %d 天, done=%s)" % (len(fds2), kv2.get("done")))

print("=== 2. P0-2 线程池异常路径收尾 ===")
real = storage.append_danmaku; cnt = {"n": 0}
def flaky(b, dms, seen):
    cnt["n"] += 1
    if cnt["n"] == 2: raise RuntimeError("模拟落库失败")
    return real(b, dms, seen)
storage.append_danmaku = flaky
class FC2(FC):
    def shard_pools(s): return [s, s]
tb = None
try: dh.fetch_history_danmaku(1, FC2(), PUB, bvid="BVC")
except RuntimeError: tb = sys.exc_info()[2]
time.sleep(0.3)
check("异常后无线程残留（保留 traceback）", not [t for t in threading.enumerate() if t.name.startswith("ThreadPoolExecutor")])

print("=== 3. P1-3 post 重试不再把上次响应当表单 ===")
import api_client
sent = []
class FakeResp:
    def __init__(s, code): s._c = code; s.status_code = 200
    def raise_for_status(s): pass
    def json(s): return {"code": s._c}
class FCli(api_client.BiliAPIClient):
    def __init__(s): super().__init__(); s._risk_cooldown_until = 0
    def _request_locked(s, method, url, **kw):
        sent.append(dict(kw.get("data") or {}))
        return FakeResp(-412 if len(sent) == 1 else 0)
    def _sleep_if_needed(s, url): pass
import types
api_client.random = types.SimpleNamespace(uniform=lambda a, b: 0)
api_client.RISK_COOLDOWN = 0
api_client.RETRY_BACKOFF = 0
cli = FCli()
r = cli.post("http://x/y", data={"refresh_token": "T1", "csrf": "C1"})
check("两次请求体一致（非上次响应）", len(sent) == 2 and sent[0] == sent[1] == {"refresh_token": "T1", "csrf": "C1"}, str(sent))

print("=== 4. 其余定点修复 ===")
import web as webmod
check("_norm_color 白名单", webmod._norm_color("#a1b2c3") == "#a1b2c3" and webmod._norm_color("red;background:url(x)") == "" and webmod._norm_color(None) == "")
import comment
check("_rpid_of 回退 id", comment._rpid_of({"id": 7}) == 7 and comment._rpid_of({}) is None and comment._rpid_of({"rpid": 0, "id": 9}) == 9)
import danmaku
xml = ('<i><d p="1,1,25,16777215,1700000000,0,zzzz,111">bad</d>'
       '<d p="1,1,25,16777215,1700000000,0,,222">empty</d>'
       '<d p="1,1,25,16777215,1700000000,0,0a1b2c,333">ok</d>'
       '<d p="1,1,25,16777215,1700000000,0,abc,444">short</d></i>').encode()
dms = danmaku.parse_danmaku_xml(xml)
check("非法 mid_hash 丢弃、合法短 hash 补零", [d["mid_hash"] for d in dms] == ["000a1b2c", "00000abc"], str([d["mid_hash"] for d in dms]))
class FCli2:
    def get_raw(s, u, params=None, **k):
        if (params or {}).get("oid") == 5:
            return type("R", (), {"content": b"<i></i>"})()   # 含 cid 的分P返回空 XML
        raise AssertionError("不应发起请求")
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    out = danmaku.fetch_all_danmaku({"pages": [{"page": 1}, {"cid": 5, "page": 2}]}, FCli2())
check("缺 cid 的分P被跳过、含 cid 的分P照常请求", out == [] and "缺少 cid" in buf.getvalue())
import profile_analyzer as pa
tg = pa.tag_activity_pattern({"activity_type": "深夜党", "peak_hour": 3})
check("时段标签不重复", tg.count("深夜党") == 1, str(tg))
import report
html = report.generate_user_card({"uid": 1, "name": "n", "follower": None, "following": None,
                                  "like_num": None, "danmaku": {}, "tags": [],
                                  "all_followings_raw": [{"sign": "s"}], "all_following_names": [""],
                                  "following_summary": {"up_details": [{"name": "", "word_freq": []}]}})
check("画像卡片渲染容错（None/缺键）", "user-card" in html)
import spam_detector as sd
_cap = sd.SPAM_PAIRWISE_UNIQUE_CAP
A, B, C = "a" * 10, "b" * 10, "c" * 10      # 三组两两相似度均为 0（便于精确比对）
contents = [A] * 100 + [B] * 100 + [C] * 100
sd.SPAM_PAIRWISE_UNIQUE_CAP = 1000          # 关闭抽样 → 全量精确值
f = sd.analyze_content_repeat(contents); avg_full = f[1] / f[2]
sd.SPAM_PAIRWISE_UNIQUE_CAP = 2             # 强制极小抽样：放大口径不一致的偏差
s = sd.analyze_content_repeat(contents); avg_samp = s[1] / s[2]
sd.SPAM_PAIRWISE_UNIQUE_CAP = _cap
# 旧实现把抽样的跨内容对与**全量**的相同内容对（sim=1）直接相加，分母少算未抽到的
# 跨内容对 → 均值被系统性推向 1，规则3（变种刷屏）因此误命中。
check("抽样不虚增相似度（跨内容对须还原到全量口径）",
      abs(avg_samp - avg_full) < 0.02, "(全量 %.3f vs 抽样 %.3f)" % (avg_full, avg_samp))

import web_autostart
os.environ["PROFILER_PORT"] = "abc"
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    web_autostart.maybe_launch_web("BV1xx411c7mD")     # 不应抛异常
check("PROFILER_PORT 非法不崩主流程", "不是合法端口" in buf2.getvalue())
del os.environ["PROFILER_PORT"]

print("=== 5. P1-10 单批退避受预算约束 ===")
import cringe_detector as cd, httpx, openai
cd.LLM_RETRY_BUDGET_SECONDS = 3
class FakeClient:
    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                raise openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
    def __init__(s, **kw): pass
cd.OpenAI = FakeClient
cd.LLM_FALLBACK = ("", "", "", "")
t0 = time.monotonic()
verdicts, failed, total = cd._judge_batches([{"content": "hi"}], 1, {"title": "t", "bvid": "BV1"}, lambda b, s, v: "p", "测试")
el = time.monotonic() - t0
check("超预算即熔断（不再长时间重试）", failed == 1 and el < 20, "(耗时 %.1fs, failed=%d)" % (el, failed))

print("=== 6. P1-3 组合池风控账本按使用方隔离（多 job 并发共享单例池）===")
# 场景：web.py 的组合池是模块级单例，两个不同 bvid 的 job 并发共用。
# A job 刚撞了一次风控（2 账号中的 1 个已标记），此时 B job 成功一次——
# B 的成功能量只能清 B 自己的账本；若清了 A 的，A 的「整圈风控」进度就被
# 并发的 B 反复抹掉（永远凑不满 MAX_RISK_ROUNDS → 反复长冷却却不放弃）。
import combo_pool as cp

class _PoolClient:
    """池成员替身：只需可被置 raise_on_risk（无 IP 池时 set_proxy 不会被调）"""
    def __init__(s, name): s.name = name; s.raise_on_risk = False
    def set_proxy(s, url): pass

_pool = cp.ComboPool([("号1", _PoolClient("号1")), ("号2", _PoolClient("号2"))],
                     clash=None, proxy_url=None)
ev_in, ev_go = threading.Event(), threading.Event()
_calls = []
def _a_fn(c):
    _calls.append(1)
    if len(_calls) == 1:
        raise cp.RiskControlError("-412 风控")
    ev_in.set()               # A 已挂在第二次调用上，此时 A.marks == [True, False]
    ev_go.wait(10)
    return "ok"

def _a_body():
    with cp.pool_owner("jobA"):
        _pool.run(_a_fn, desc="A")

def _a_marks():
    """取 A 的账本快照；退化成共享账本（修复前实现）时回落到 None 账本，便于干净断言"""
    led = _pool._ledgers.get("jobA") or _pool._ledgers.get(None)
    return list(led.marks) if led else []

_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    th = threading.Thread(target=_a_body, daemon=True)
    th.start()
    reached = ev_in.wait(10)
    marks_before = _a_marks()
    if reached:
        with cp.pool_owner("jobB"):
            _pool.run(lambda c: "ok", desc="B")     # B 全程成功
    marks_after = _a_marks()
    ev_go.set()
    th.join(10)
_led_b = _pool._ledgers.get("jobB")
rounds_b = _led_b.rounds if _led_b else 0
check("并发 job 风控账本隔离（B 成功不清空 A 的整圈进度）",
      reached and marks_before == [True, False] and marks_after == marks_before
      and rounds_b == 0,
      "(A %s → B 成功后 %s)" % (marks_before, marks_after))

print("=== 7. 规则3 语义：完全相同属「大量重复」，不得再算「变种刷屏」===")
_ts = [1700000000 + 10 * i for i in range(10)]
_same = sd.analyze_spam(["哈哈哈"] * 10, _ts)
check("完全相同的弹幕不判变种刷屏", "变种刷屏" not in _same["reason"],
      "(理由: %s)" % _same["reason"])
_base = "这是一条足够长的测试弹幕内容"
_var = sd.analyze_spam([_base + str(i) for i in range(10)], _ts)
check("真正的变体仍判变种刷屏", "变种刷屏" in _var["reason"],
      "(理由: %s)" % _var["reason"])

print("")
print("==== 结果: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)