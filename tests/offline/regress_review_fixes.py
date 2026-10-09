# -*- coding: utf-8 -*-
"""本轮 code review 修复的定点验证（批量 SystemExit、批次缓存保序、CancelledError、
天数上限滚动补采、api_danmaku 非法 sort、跨批幻觉下标过滤，共 10 项）

离线回归脚本：使用隔离临时库 + 假 HTTP/假 OpenAI，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_review_fixes.py
"""
import os
import sys
import io
import contextlib
import datetime as _dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="revfix_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))


print("=== 1. run_batch 不被 SystemExit 击穿、登录失败中止批量 ===")
import main as mainmod
from auth import LoginRequiredError
calls = []
def fake_run_analysis(bvid, **kw):
    calls.append(bvid)
    if bvid == "BVfail00001":
        raise SystemExit(1)                      # 视频不存在/风控的阶段退出路径
    if bvid == "BVlogin0001":
        raise LoginRequiredError("二维码过期，需重新扫码登录")
    return None
mainmod.run_analysis = fake_run_analysis
batch_file = os.path.join(TMP, "batch.txt")
Path(batch_file).write_text("\n".join(["BVfail00001", "BVok0000002", "BVlogin0001", "BVskip0004"]) + "\n",
                            encoding="utf-8")
with contextlib.redirect_stdout(io.StringIO()):
    mainmod.run_batch(batch_file)
check("SystemExit 只跳过当前视频，批量继续", calls[:2] == ["BVfail00001", "BVok0000002"],
      "(调用序 %s)" % calls)
check("登录失败中止批量（后续视频不再空转）", calls == ["BVfail00001", "BVok0000002", "BVlogin0001"],
      "(未执行 %s)" % ("BVskip0004" if "BVskip0004" not in calls else "无"))

print("=== 2. 批次缓存指纹保序（防下标张冠李戴）===")
import cringe_detector as cd
CNT = {"n": 0}
class _Comp:
    def create(self, **kw):
        CNT["n"] += 1
        raw = '[{"i": 0, "category": "批评吐槽", "severity": 1, "reason": "r"}]'
        return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": raw})()})()]})()
class FakeOpenAI:
    def __init__(self, **kw):
        self.chat = type("Chat", (), {"completions": _Comp()})()
cd.OpenAI = FakeOpenAI
cd.LLM_FALLBACK = ("", "", "", "")
cd.LLM_CONCURRENCY = 1
PROMPT = lambda b, s, v: "p"
vi = {"title": "t", "bvid": "BVord000001"}
cd._judge_batches([{"content": "甲"}, {"content": "乙"}], 2, vi, PROMPT, "测试",
                  cache_prefix="cringe:BVord:v4")
n1 = CNT["n"]
cd._judge_batches([{"content": "乙"}, {"content": "甲"}], 2, vi, PROMPT, "测试",
                  cache_prefix="cringe:BVord:v4")
n2 = CNT["n"]
check("同集合不同顺序 → 批次缓存不命中（顺序敏感）", n2 == n1 + 1,
      "(调用 %d → %d)" % (n1, n2))
cd._judge_batches([{"content": "甲"}, {"content": "乙"}], 2, vi, PROMPT, "测试",
                  cache_prefix="cringe:BVord:v4")
check("同序重跑 → 批次缓存命中（零调用）", CNT["n"] == n2, "(调用 %d)" % CNT["n"])

print("=== 3. 跨批幻觉下标被掐掉 ===")
CNT["n"] = 0
class _CompHallu:
    def create(self, **kw):
        CNT["n"] += 1
        raw = '[{"i": 5, "category": "人身攻击", "severity": 3, "reason": "x"}, {"i": 1, "category": "批评吐槽", "severity": 1, "reason": "r"}]'
        return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": raw})()})()]})()
class FakeOpenAI2:
    def __init__(self, **kw):
        self.chat = type("Chat", (), {"completions": _CompHallu()})()
cd.OpenAI = FakeOpenAI2
# 单批覆盖全部 3 条（合法区间 [0,3)）：i=5 属跨批幻觉下标，应被过滤；i=1 保留
verdicts, failed, total = cd._judge_batches(
    [{"content": "x"}, {"content": "y"}, {"content": "z"}], 3, vi, PROMPT, "测试")
check("越界下标(i=5)被过滤、批内合法下标(i=1)保留",
      [v["i"] for v in verdicts] == [1] and failed == 0,
      "(verdicts %s)" % [v.get("i") for v in verdicts])

print("=== 4. 致命错误路径不再被 CancelledError 击穿 ===")
import httpx, openai
class _CompFatal:
    def create(self, **kw):
        raise openai.AuthenticationError(
            "bad key", response=httpx.Response(401, request=httpx.Request("POST", "http://x")), body=None)
class FakeOpenAI3:
    def __init__(self, **kw):
        self.chat = type("Chat", (), {"completions": _CompFatal()})()
cd.OpenAI = FakeOpenAI3
raised = None
try:
    cd._judge_batches([{"content": "a"}, {"content": "b"}], 1, vi, PROMPT, "测试")
except openai.AuthenticationError as e:
    raised = e
except BaseException as e:                        # CancelledError 会走这里（修复前）
    raised = e
check("致命错误按设计上抛（非 CancelledError 击穿）", isinstance(raised, openai.AuthenticationError),
      "(抛出 %s)" % type(raised).__name__)

print("=== 5. 达天数上限的 done 视频跨天重跑仍滚动补采 ===")
import danmaku_history as dh
def varint(n):
    o = b""
    while True:
        x = n & 0x7F; n >>= 7; o += bytes([x | (0x80 if n else 0)])
        if not n: return o
TODAY = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date()
DATES = [(TODAY - _dt.timedelta(days=k)).isoformat() for k in range(5, -1, -1)]  # today-5 .. today
PUB = int(_dt.datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=_dt.timezone(_dt.timedelta(hours=8))).timestamp()) - 5 * 86400
class Resp:
    def __init__(s, b): s.content = b
class FC:
    def __init__(s): s.served = []
    def get(s, u, params=None, **k):
        m = (params or {}).get("month", ""); return {"code": 0, "data": [d for d in DATES if d.startswith(m)]}
    def get_raw(s, u, params=None, **k):
        d = (params or {}).get("date"); s.served.append(d)
        e = bytes([8]) + varint(int(d[-2:]) + 500) + bytes([0x3A, 1, 0x78])
        return Resp(bytes([10]) + varint(len(e)) + e)
dh.HISTORY_MAX_DAYS = 5
dh.HISTORY_RECENT_REFRESH_DAYS = 3
BV = "BVrefresh001"
storage.set_phase_state(BV, "danmaku", "done", "1")
storage.set_phase_state(BV, "danmaku", "last_date", DATES[0])       # today-5（跨天重跑：停在昨天以前）
dh._save_date_set(BV, "fetched_dates", set(DATES[:5]), 1)            # 已采 5 天（恰达上限）
c = FC()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    dh.fetch_history_danmaku(1, c, PUB, bvid=BV)
kv = {r["key"]: r["value"] for r in storage.get_db().execute(
    "SELECT key,value FROM phase_state WHERE bvid=? AND phase='danmaku'", (BV,)).fetchall()}
fds = set((kv.get("fetched_dates") or "").split(",")) - {""}
check("滚动补采未被天数上限冻结（补采 3 天）",
      len(c.served) == 3 and set(c.served) == set(DATES[3:]),
      "(请求 %s)" % sorted(c.served))
check("补采后 done 保持 1 且新日期入集", kv.get("done") == "1" and DATES[5] in fds and DATES[4] in fds,
      "(fetched %d 天)" % len(fds))

print("=== 6. api_danmaku 非法 sort 不再 500 ===")
import web as webmod
storage.save_video_info("BVapi0000001", {"bvid": "BVapi0000001", "title": "t", "cid": 1})
storage.append_danmaku("BVapi0000001", [
    {"mid_hash": "a1b2c3d4", "content": "测试弹幕", "time": 1.0, "timestamp": 1700000000,
     "mode": 1, "color": "#ffffff", "pool": 0, "dmid": 1, "page": 1}], set())
webmod.app.config["TESTING"] = True
tc = webmod.app.test_client()
r_bad = tc.get("/api/video/BVapi0000001/danmaku?sort=abc")
r_ok = tc.get("/api/video/BVapi0000001/danmaku?sort=video_time")
check("非法 sort 回退默认列（200 + rows）", r_bad.status_code == 200 and "rows" in r_bad.get_json(),
      "(status %d)" % r_bad.status_code)
check("合法 sort 照常", r_ok.status_code == 200 and r_ok.get_json().get("total") == 1,
      "(total %s)" % r_ok.get_json().get("total"))

print("")
print("==== review 修复回归: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
