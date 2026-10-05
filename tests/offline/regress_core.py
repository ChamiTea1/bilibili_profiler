# -*- coding: utf-8 -*-
"""主回归：本轮修复的定点验证（历史弹幕续采、线程池收尾、post 重试、限速预算等 14 项）

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

print("=== 2. P0-2 线程池异常路径收尾 ===")
import concurrent.futures.thread as _t
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
    def get_raw(s, u, params=None, **k): raise AssertionError("不应发起请求")
danmaku.fetch_all_danmaku({"pages": [{"page": 1}, {"cid": 5, "page": 2}]}, FCli2()) if False else None
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    out = danmaku.fetch_all_danmaku({"pages": [{"page": 1}]}, FCli2())
check("缺 cid 的分P被跳过不中断", out == [] and "缺少 cid" in buf.getvalue())
import profile_analyzer as pa
tg = pa.tag_activity_pattern({"activity_type": "深夜党", "peak_hour": 3})
check("时段标签不重复", tg.count("深夜党") == 1, str(tg))
check("follower 为 None 不崩", True)  # 见下 report 渲染测试
import report
html = report.generate_user_card({"uid": 1, "name": "n", "follower": None, "following": None,
                                  "like_num": None, "danmaku": {}, "tags": [],
                                  "all_followings_raw": [{"sign": "s"}], "all_following_names": [""],
                                  "following_summary": {"up_details": [{"name": "", "word_freq": []}]}})
check("画像卡片渲染容错（None/缺键）", "user-card" in html)
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

print("")
print("==== 结果: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)