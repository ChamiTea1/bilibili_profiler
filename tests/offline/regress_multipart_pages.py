# -*- coding: utf-8 -*-
"""多分P 口径：历史弹幕按分P 采集 + 样本时间标分P（14 项）

复现并锁死的问题：B站历史弹幕接口按 cid 维度（每个分P 一个弹幕池），旧实现只采
分P 1 的 cid，导致多分P 视频的 P2+ 永久只有实时池数据（密度图严重偏低）；同时
danmaku.time 是「所在分P 内的相对秒数」，画像样本/时间线/弹幕浏览器里的 mm:ss
不标分P 就无法解释（同一 00:30 既可能是 P1 也可能是 P4）。

离线回归脚本：使用隔离临时库 + 假 HTTP 客户端，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_multipart_pages.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="mpp_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()
import main
import report as rp
import web as webmod

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

PAGES4 = [{"page": 1, "cid": 101, "part": "中", "duration": 109},
          {"page": 2, "cid": 202, "part": "日", "duration": 103},
          {"page": 3, "cid": 303, "part": "英", "duration": 103},
          {"page": 4, "cid": 404, "part": "韩", "duration": 103}]

print("=== 1. 待采分P 清单（历史接口按 cid 维度） ===")
check("多分P：每个分P 各采一份（cid 对应）",
      main._history_page_targets({"pages": PAGES4}) == [(1, 101), (2, 202), (3, 303), (4, 404)])
check("单分P：只采分P 1",
      main._history_page_targets({"pages": PAGES4[:1]}) == [(1, 101)])
check("无 pages 元信息：退回主 cid（page=1，旧数据行为不变）",
      main._history_page_targets({"cid": 999}) == [(1, 999)])
_old_max = main.HISTORY_MULTIPAGE_MAX_PAGES
main.HISTORY_MULTIPAGE_MAX_PAGES = 2
check("分P 数超上限：只采前 N 个分P",
      main._history_page_targets({"pages": PAGES4}) == [(1, 101), (2, 202)])
main.HISTORY_MULTIPAGE_MAX_PAGES = _old_max
_old_en = main.HISTORY_MULTIPAGE_ENABLED
main.HISTORY_MULTIPAGE_ENABLED = False
check("开关关闭：退回只采分P 1", main._history_page_targets({"pages": PAGES4}) == [(1, 101)])
main.HISTORY_MULTIPAGE_ENABLED = _old_en

print("=== 2. 历史弹幕按分P 落库与检查点隔离 ===")
def varint(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n: return out

def _fb(no, data: bytes):     # length-delimited 字段
    return varint(no << 3 | 2) + varint(len(data)) + data

def _fv(no, val):             # varint 字段
    return varint(no << 3) + varint(val)

def _elem(dmid: int, content: str) -> bytes:
    """构造一个 DanmakuElem（progress=2/midHash=6/content=7/ctime=8/idStr=12）"""
    e = (_fv(2, 5000) + _fb(6, b"abc") + _fb(7, content.encode()) +
         _fv(8, 1700000000) + _fb(12, str(dmid).encode()))
    return _fb(1, e)          # repeated elems = 1

TODAY = datetime.now(timezone(timedelta(hours=8)))
DAYS = [(TODAY - timedelta(days=i)).strftime("%Y-%m-%d") for i in (1, 2, 3)]
PUB = int((TODAY - timedelta(days=40)).timestamp())

class Resp:
    def __init__(s, b): s.content = b

class FHist:
    """只实现历史采集用到的两个接口的假客户端（不联网）"""
    def __init__(s): s.dates = []
    def get(s, url, params=None, **kw):
        m = (params or {}).get("month", "")
        return {"code": 0, "data": [d for d in DAYS if d.startswith(m)]}
    def get_raw(s, url, params=None, **kw):
        d = (params or {}).get("date"); s.dates.append(d)
        return Resp(_elem(int(d.replace("-", "")) * 10, "历史" + d))

import danmaku_history as dh
BVM = "BVmultipage1"
dh.fetch_history_danmaku(202, FHist(), PUB, bvid=BVM, page=2)
rows = storage.load_danmaku(BVM)
check("分P 2 的历史弹幕落库时带上 page=2",
      bool(rows) and all(int(r["page"]) == 2 for r in rows), "(共 %d 条)" % len(rows))
check("分P 2 的检查点键带 :p2 后缀",
      storage.get_phase_state(BVM, "danmaku", "done:p2") == "1"
      and storage.get_phase_state(BVM, "danmaku", "last_date:p2") is not None)
check("分P 1 的检查点键仍是不带后缀的原键（既有报告不触发重采）",
      storage.get_phase_state(BVM, "danmaku", "done") is None)

print("=== 3. 画像样本标分P（报告卡片） ===")
card = {"uid": 1, "name": "n", "tags": [], "danmaku": {
    "count": 3, "contents": ["c30", "c5", "c90"], "video_times": [30.0, 5.0, 90.0],
    "video_pages": [2, 1, 2], "multi_page": True, "spam_level": "低", "spam_score": 0.0}}
html = rp.generate_user_card(card)
i5, i30, i90 = html.find("P1 00:05"), html.find("P2 00:30"), html.find("P2 01:30")
check("多分P：样本渲染为「P{n} mm:ss」", i5 > 0 and i30 > 0 and i90 > 0,
      "(P1 00:05=%d, P2 00:30=%d, P2 01:30=%d)" % (i5, i30, i90))
check("多分P：先按分P 再按分P 内时间排序（跨分P 比 time 无意义）", 0 < i5 < i30 < i90)
card1 = {"uid": 2, "name": "n2", "tags": [], "danmaku": {
    "count": 2, "contents": ["a", "b"], "video_times": [30.0, 5.0],
    "spam_level": "低", "spam_score": 0.0}}
html1 = rp.generate_user_card(card1)
check("单分P：不带 P{n} 前缀（口径与旧版一致）",
      "00:05" in html1 and "00:30" in html1 and "P1 " not in html1)

print("=== 4. 旧画像渲染期回填分P（不落库、无需重跑） ===")
BVF = "BVfillpages1"
storage.append_danmaku(BVF, [
    {"mid_hash": "h1", "content": "x", "time": 10.0, "timestamp": 1, "page": 2, "dmid": 1},
    {"mid_hash": "h1", "content": "y", "time": 20.0, "timestamp": 2, "page": 1, "dmid": 2},
    {"mid_hash": "h2", "content": "z", "time": 7.0, "timestamp": 3, "page": 1, "dmid": 3},
    {"mid_hash": "h2", "content": "z", "time": 7.0, "timestamp": 4, "page": 2, "dmid": 4},
], set())
p_old = {"uid": 1, "name": "n", "_mid_hash": "h1",
         "danmaku": {"count": 2, "contents": ["x", "y"], "video_times": [10.0, 20.0]}}
p_amb = {"uid": 2, "name": "m", "_mid_hash": "h2",
         "danmaku": {"count": 1, "contents": ["z"], "video_times": [7.0]}}
webmod._fill_sample_pages(BVF, [p_old, p_amb])
check("旧画像按 (mid_hash, content, time) 回填分P 且置 multi_page",
      p_old["danmaku"]["video_pages"] == [2, 1] and p_old["danmaku"]["multi_page"] is True,
      "(得到 %r)" % (p_old["danmaku"].get("video_pages"),))
check("回填歧义（同一内容+时间出现在多个分P）：记 0 不标，宁可不标不标错",
      p_amb["danmaku"]["video_pages"] == [0], "(得到 %r)" % (p_amb["danmaku"]["video_pages"],))

print("=== 5. 弹幕浏览器「首次出现」按 (分P, 分P内时间) ===")
BVA = "BVapidm00001"
storage.save_video_info(BVA, {"bvid": BVA, "title": "多分P", "duration": 212,
                              "pages": PAGES4[:2], "stat": {}})
storage.append_danmaku(BVA, [
    {"mid_hash": "hA", "content": "早", "time": 50.0, "timestamp": 1, "page": 1, "dmid": 11},
    {"mid_hash": "hA", "content": "早", "time": 50.0, "timestamp": 2, "page": 2, "dmid": 12},
    {"mid_hash": "hB", "content": "晚", "time": 10.0, "timestamp": 3, "page": 2, "dmid": 13},
], set())
c = webmod.app.test_client()
data = c.get("/api/video/%s/danmaku?sort=video_time&order=asc" % BVA).get_json()
by_content = {r["content"]: r for r in data["rows"]}
# 旧口径（MIN(time)）会把 P2 的 10.0s 排在 P1 的 50.0s 之前；新口径 P2 整体在 P1 之后
check("API 返回 multi_page 与逐行 first_page（不再跨分P 比 time）",
      data.get("multi_page") is True and by_content["早"]["first_page"] == 1
      and by_content["晚"]["first_page"] == 2
      and [r["content"] for r in data["rows"]] == ["早", "晚"],
      "(顺序 %r)" % ([r["content"] for r in data["rows"]],))

print("")
print("==== 多分P 口径（历史采集 + 样本标注）: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
