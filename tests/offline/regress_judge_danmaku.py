# -*- coding: utf-8 -*-
"""问题弹幕判定聚合：批次判定必须被采纳并归到正确发送者（4 项）

离线回归脚本：使用隔离临时库 + 假 HTTP/假 OpenAI，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_judge_danmaku.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile, json
TMP = tempfile.mkdtemp(prefix="crg_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()
import cringe_detector as cd

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

RESP = ['[{"i": 4, "category": "广告引流", "severity": 2, "reason": "群号引流"}]',
        '[]\n\n注：经逐条审核，未发现符合八类问题弹幕定义的内容。']
CNT = {"n": 0}
class _Comp:
    def create(self, **kw):
        i = CNT["n"]; CNT["n"] += 1
        raw = RESP[i % len(RESP)]
        return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": raw})()})()]})()
class FakeOpenAI:
    def __init__(self, **kw):
        self.chat = type("Chat", (), {"completions": _Comp()})()

cd.LLM_API_KEY = "offline-test-key"   # 判定入口在 Key 为空时会直接跳过，离线用例注入占位值
cd.LLM_FALLBACK = ("", "", "", "")     # 只走主用厂商（假客户端）
cd.OpenAI = FakeOpenAI
cd.CRINGE_BATCH_SIZE = 3        # 6 条 → 2 批，第 2 批走"空数组 + 说明文字"
cd.LLM_CONCURRENCY = 1          # 串行，保证批次与响应一一对应

dms = [{"content": "c%d" % i, "mid_hash": "h%03d" % i, "time": i, "timestamp": 1700000000 + i,
        "mode": 1, "color": "#ffffff", "pool": 0, "dmid": i, "page": 1} for i in range(6)]
groups = {d["mid_hash"]: {"mid_hash": d["mid_hash"], "count": 1, "contents": [d["content"]],
                          "timestamps": [d["timestamp"]], "video_times": [d["time"]],
                          "colors": [d["color"]], "pages": [1]} for d in dms}
info = {"bvid": "BVcrg000001", "title": "t", "desc": "", "owner": {"name": "up"}}

res = cd.detect_cringe_danmaku(dms, groups, info)
hit = res.get("h004")
check("判定被采纳并归到正确发送者", bool(hit) and hit["count"] == 1,
      "(结果 %s)" % json.dumps(res, ensure_ascii=False)[:120])
check("类别/严重度正确", bool(hit) and hit["categories"] == ["广告引流"] and hit["max_severity"] == 2,
      "" if not hit else str(hit["categories"]))
check("空数组批次不误判、不产生条目", len(res) == 1, "(涉及发送者 %d)" % len(res))

calls_after_first = CNT["n"]
res2 = cd.detect_cringe_danmaku(dms, groups, info)
check("整段缓存命中（重跑零 LLM 调用）", CNT["n"] == calls_after_first and res2.get("h004", {}).get("count") == 1,
      "(额外调用 %d 次)" % (CNT["n"] - calls_after_first))

print("")
print("==== 判定聚合: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)