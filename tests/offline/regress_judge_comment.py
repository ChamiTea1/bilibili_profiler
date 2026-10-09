# -*- coding: utf-8 -*-
"""问题评论判定聚合：verdict 必须回映到 rpid（3 项）

离线回归脚本：使用隔离临时库 + 假 HTTP/假 OpenAI，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_judge_comment.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="cmtc_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()
import cringe_detector as cd

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

# 批次0 为内容字典序的前两条（c0,c1，全局下标 0~1）——下标区间校验会掐掉
# 跨批幻觉下标，假响应用批内合法下标 i:1（c1 → rpid101）
RESP = ['[{"i": 1, "category": "引战阴阳", "severity": 2, "reason": "拉踩"}]',
        '[]\n\n注：未发现符合八类问题评论的内容。']
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
cd.COMMENT_CRINGE_BATCH_SIZE = 2
cd.LLM_CONCURRENCY = 1

comments = [{"rpid": 100 + i, "uid": 1000 + i, "content": "c%d" % i, "like": i, "is_sub": 0}
            for i in range(4)]
res = cd.detect_problem_comments(comments, {"bvid": "BVcmt0001", "title": "t"})
check("verdict 回映到 rpid", 101 in res and res[101]["category"] == "引战阴阳",
      "(命中 %s；内容字典序批次0 内 i:1 → c1 → rpid101)" % list(res.keys()))
check("空数组批次不产生条目", len(res) == 1)

# 真实验证回写：先落库评论行，再回写判定，断言 problem 列确被更新
# （原先 `check(..., True)` 恒真——空库 UPDATE 必然命中 0 行，关键路径零有效验证）
storage.save_comments("BVcmt0001", [
    {"rpid": r, "uid": u, "content": "c%d" % i, "like": i, "is_sub": 0}
    for i, (r, u) in enumerate([(100, 1000), (101, 1001), (102, 1002), (103, 1003)])
])
storage.update_comment_problems("BVcmt0001", {r: v["category"] for r, v in res.items()})
rows = storage.get_db().execute(
    "SELECT rpid, problem FROM comments WHERE bvid='BVcmt0001' ORDER BY rpid").fetchall()
check("判定回写 comments.problem 列", rows and rows[1]["problem"] == "引战阴阳"
      and all(r["problem"] == "" for r in rows if r["rpid"] != 101),
      "(回写后 %s)" % [(r["rpid"], r["problem"]) for r in rows])
print("")
print("==== 问题评论聚合: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)