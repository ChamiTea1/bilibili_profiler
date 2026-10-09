# -*- coding: utf-8 -*-
"""弹幕高能点 AI 标注：峰值检测/样本采集/LLM 标注/缓存命中/降级（7 项）

离线回归脚本：使用隔离临时库 + 假 OpenAI，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_density_peaks.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="peak_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))


import cringe_detector as cd
import web as webmod

BV = "BVpeak000001"
DUR = 600   # 60 桶 × 10s

def _dm(t, content, dmid):
    return {"mid_hash": "h%08d" % dmid, "content": content, "time": t,
            "timestamp": 1700000000 + dmid, "mode": 1, "color": "#ffffff",
            "pool": 0, "dmid": dmid, "page": 1}

rows = []
# 桶 5（50-60s）：15 条吵架弹幕（>P90 且 > 最小条数 → 峰）
rows += [_dm(50 + i * 0.5, "你就是错的" if i % 2 else "你才错了", 1000 + i) for i in range(15)]
# 桶 20（200-210s）：12 条玩梗刷屏（峰）
rows += [_dm(200 + i * 0.5, "哈哈哈哈哈", 2000 + i) for i in range(12)]
# 其余桶零星 1 条（基线，不构成峰）
rows += [_dm(i * 10 + 1, "路过", 3000 + i) for i in range(0, 60, 3)]
storage.save_video_info(BV, {"bvid": BV, "title": "t", "cid": 1, "duration": DUR})
storage.append_danmaku(BV, rows, set())

# 假 OpenAI：第 1 次调用返回标注（含一个幻觉编号 99 应被过滤），此后走缓存
CNT = {"n": 0}
class _Comp:
    def create(self, **kw):
        CNT["n"] += 1
        raw = ('[{"i": 0, "label": "观众对线互喷", "kind": "吵架对线"},'
               '{"i": 1, "label": "集体笑抽", "kind": "刷屏玩梗"},'
               '{"i": 99, "label": "幻觉", "kind": "名场面"}]')
        return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": raw})()})()]})()
class FakeOpenAI:
    def __init__(self, **kw):
        self.chat = type("Chat", (), {"completions": _Comp()})()

cd.LLM_API_KEY = "offline-test-key"
cd.LLM_FALLBACK = ("", "", "", "")
cd.OpenAI = FakeOpenAI
webmod.LLM_API_KEY = "offline-test-key"   # web 侧开关（_annotate_density_peaks 的零成本路径判定）

print("=== 1. 峰值检测 + LLM 标注 + 挂载 ===")
density = webmod._danmaku_density(BV, DUR, {})
density = webmod._annotate_density_peaks(BV, density, {})
page = density["pages"][0]
peaks = {p["i"]: p for p in page.get("peaks", [])}
check("两个峰值桶被标注（桶5=吵架/桶20=玩梗）",
      set(peaks) == {5, 20}, "(peaks %s)" % sorted(peaks))
check("标注内容与类别正确、幻觉编号 99 被过滤",
      peaks.get(5, {}).get("kind") == "吵架对线" and "互喷" in peaks.get(5, {}).get("label", "")
      and peaks.get(20, {}).get("kind") == "刷屏玩梗" and 99 not in peaks,
      "(%s)" % peaks)
check("基线桶（1 条弹幕）不标注", 0 not in peaks and 30 not in peaks)

print("=== 2. 缓存命中（重跑零调用）与指纹失效 ===")
n1 = CNT["n"]
density2 = webmod._danmaku_density(BV, DUR, {})
density2 = webmod._annotate_density_peaks(BV, density2, {})
check("同数据重跑命中 llm_cache（零 LLM 调用）",
      CNT["n"] == n1 and len(density2["pages"][0].get("peaks", [])) == 2,
      "(调用 %d 次)" % (CNT["n"] - n1))
# 新增弹幕改变峰值样本 → 指纹变 → 缓存失效重判（再耗一次调用）
storage.append_danmaku(BV, [_dm(52.0, "新弹幕改指纹", 99999)], set())
density3 = webmod._danmaku_density(BV, DUR, {})
density3 = webmod._annotate_density_peaks(BV, density3, {})
check("弹幕量变化 → 指纹失效重新标注", CNT["n"] == n1 + 1,
      "(调用 %d 次)" % (CNT["n"] - n1))

print("=== 3. 降级路径 ===")
saved = cd.OpenAI

class FakeBoom:
    def __init__(self, **kw):
        self.chat = type("Chat", (), {"completions": type("C", (), {
            "create": staticmethod(lambda **kw: (_ for _ in ()).throw(RuntimeError("模拟故障")))})()})()

cd.OpenAI = FakeBoom
BV2 = "BVpeak000002"
storage.save_video_info(BV2, {"bvid": BV2, "title": "t2", "cid": 2, "duration": DUR})
storage.append_danmaku(BV2, [_dm(50 + i * 0.5, "吵架%d" % i, 5000 + i) for i in range(15)], set())
d_fail = webmod._danmaku_density(BV2, DUR, {})
d_fail = webmod._annotate_density_peaks(BV2, d_fail, {})
check("LLM 失败静默降级（无标注、不抛异常）",
      not d_fail["pages"][0].get("peaks"), "(peaks %s)" % d_fail["pages"][0].get("peaks"))
cd.OpenAI = saved
saved_key = webmod.LLM_API_KEY
webmod.LLM_API_KEY = ""
d_nokey = webmod._danmaku_density(BV, DUR, {})
d_nokey = webmod._annotate_density_peaks(BV, d_nokey, {})
check("未配置 Key 零成本路径（不发请求）",
      CNT["n"] == n1 + 1 and isinstance(d_nokey, dict), "(调用 %d)" % CNT["n"])
webmod.LLM_API_KEY = saved_key

print("")
print("==== 高能点标注: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
