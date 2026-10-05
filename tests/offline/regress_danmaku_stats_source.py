# -*- coding: utf-8 -*-
"""弹幕统计同源与报告补齐：缓存命中分支不得泄漏旧快照 contents（5 项）

复现并锁死的问题：阶段4 缓存命中分支曾直接用 senders 行里的旧 count/contents，
而阶段6 的 video_times 却现取本轮 sender_groups → 两者长度不一致时 report.py 的
zip 静默截断丢样本、甚至错配 mm:ss。

离线回归脚本：使用隔离临时库 + 假客户端，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_danmaku_stats_source.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="dss_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()
import main
import report as rp

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

BVID = "BVoffline001"
MH = "hA"
# 缓存行：只承载"解析结论"，弹幕统计是旧快照（2 条）
storage.save_sender(bvid=BVID, mid_hash=MH, uid=111,
                    confidence="高", method=main.METHOD_COMMENT_VERIFY,
                    danmaku_count=2, contents=["旧1", "旧2"],
                    spam_level="低", spam_score=0.0)
# 本轮 sender_groups：同一发送者已增长到 3 条（模拟续采/滚动补采新弹幕）
cur_contents = ["新1", "新2", "新3"]
cur_times = [10.0, 20.0, 30.0]
sender_groups = {MH: {"mid_hash": MH, "count": 3, "contents": cur_contents,
                      "timestamps": [1, 2, 3], "video_times": cur_times,
                      "colors": ["#ffffff"] * 3, "pages": [1]}}

resolved = main.phase_resolve(BVID, sender_groups, {}, client=None)
info = resolved.get(MH, {})

check("缓存命中分支用当前 sender_groups 刷新 danmaku_count",
      info.get("danmaku_count") == 3, "(得到 %r)" % info.get("danmaku_count"))
check("缓存命中分支用当前 sender_groups 刷新 contents（旧快照不泄漏）",
      info.get("contents") == cur_contents,
      "(得到 %r)" % info.get("contents"))
check("阶段6 组合的 contents 与 video_times 同源等长",
      len(info.get("contents", [])) == len(sender_groups[MH]["video_times"]),
      "(contents=%d, video_times=%d)" % (len(info.get("contents", [])),
                                         len(sender_groups[MH]["video_times"])))

def _dm_li_count(profile):
    html = rp.generate_user_card(profile)
    # 弹幕列表是卡片里唯一的 <ol class="dm-list">；本用例 profile 无评论小节
    return html.count("<li>")

# 历史残留画像形态一：contents 多于 video_times（曾被 zip 截断丢样本）
card_short = {"uid": 1, "name": "测试", "danmaku": {"count": 3, "contents": ["a", "b", "c"],
               "video_times": [10.0], "spam_level": "低", "spam_score": 0.0}}
check("报告渲染：video_times 短于 contents 时补齐不丢样本", _dm_li_count(card_short) == 3,
      "(渲染 %d 条)" % _dm_li_count(card_short))

# 历史残留画像形态二：video_times 完全缺失（回退 00:00，样本仍全渲染）
card_empty = {"uid": 2, "name": "测试2", "danmaku": {"count": 2, "contents": ["x", "y"],
              "video_times": [], "spam_level": "低", "spam_score": 0.0}}
check("报告渲染：video_times 为空时仍渲染全部样本", _dm_li_count(card_empty) == 2,
      "(渲染 %d 条)" % _dm_li_count(card_empty))

print("")
print("==== 弹幕统计同源与报告补齐: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
