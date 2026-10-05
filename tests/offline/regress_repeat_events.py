# -*- coding: utf-8 -*-
"""群体复读事件的检测口径：视频内时间轴 + 写法变体合并（17 项）

复现并锁死的问题：旧实现只在**真实发送时间戳**上开 60s 窗口，而 B站 的接龙/+1 复读
发生在**同一个视频时间点**——观众可能相隔几个月才看到这里，却都在同一画面刷同一句
话。结果就是报告显示「未检出群体复读事件」，而视频里明明满屏复读：
BV1mtTD6rEtQ 用视频内时间轴命中 18 起（「？」78 人/90 条挤在 P1 34~93s），
用发送时间轴 0 起；全库 30 个视频是 331 : 18。

离线回归脚本：使用隔离临时库，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_repeat_events.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="rpe_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()
import spam_detector as sd
import web as webmod

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

NOW = int(time.time())
MONTH = 30 * 24 * 3600

def row(content, mh, vt, ts, page=1):
    return {"content": content, "mid_hash": mh, "time": vt, "timestamp": ts, "page": page}

print("=== 1. 视频内时间轴（接龙复读的主形态） ===")
# 5 个不同发送者、相隔数月的发送时间，但都集中在视频 P1 的 30~34s（同一画面）
video_burst = [row("许愿不歪", "h%d" % i, 30.0 + i, NOW - i * MONTH) for i in range(6)]
video_burst += [row("许愿不歪", "h0", 31.5, NOW - MONTH), row("许愿不歪", "h1", 32.0, NOW - 2 * MONTH)]
ev = sd.detect_repeat_events(video_burst)
check("跨月发送但同一视频时间 → 命中（旧实现漏检）",
      len(ev) == 1 and ev[0]["sender_count"] == 6 and ev[0]["total"] == 8,
      "(得到 %r)" % ([(e["sender_count"], e["total"]) for e in ev],))
check("事件带 video 窗口（分P + 秒数）且 send 轴不达标时不显示",
      ev and ev[0]["video"] and ev[0]["video"]["page"] == 1
      and ev[0]["video"]["start"] <= 30.0 and ev[0]["video"]["end"] >= 34.0
      and ev[0]["send"] is None,
      "(得到 %r)" % (ev[0]["video"] if ev else None,))

print("=== 2. 发送时间轴（集中刷屏形态，保留） ===")
# 6 人 8 条集中在 60s 内发出，但视频时间分散在整片（不是同一画面）
send_burst = [row("懂的都懂", "s%d" % i, 10.0 + i * 40, NOW - 30 + i * 5) for i in range(6)]
send_burst += [row("懂的都懂", "s0", 300.0, NOW - 28), row("懂的都懂", "s1", 500.0, NOW - 25)]
ev2 = sd.detect_repeat_events(send_burst)
check("同内容短时间集中刷出（视频时间分散）→ 命中 send 轴",
      len(ev2) == 1 and ev2[0]["send"] is not None and ev2[0]["video"] is None,
      "(得到 %r)" % (ev2[0] if ev2 else None,))

print("=== 3. 双轴都命中 ===")
both = [row("原神牛逼", "b%d" % i, 408.0 + i, NOW - 600 + i) for i in range(6)]
both += [row("原神牛逼", "b0", 409.0, NOW - 590), row("原神牛逼", "b1", 409.5, NOW - 580)]
ev3 = sd.detect_repeat_events(both)
check("两轴都达标时都带出（前端同时展示视频内+发送时间）",
      len(ev3) == 1 and ev3[0]["video"] and ev3[0]["send"],
      "(得到 %r)" % ((ev3[0]["video"], ev3[0]["send"]) if ev3 else None,))

print("=== 4. 分P 隔离（time 是分P 内相对秒数，跨分P 不可比） ===")
# P1 与 P2 各有 3 人在各自 30s 处刷同一句：跨分P 合并会误判成 6 人，必须分开算
split = [row("秧秧", "p1_%d" % i, 30.0 + i, NOW - i * MONTH, page=1) for i in range(3)]
split += [row("秧秧", "p2_%d" % i, 30.0 + i, NOW - (i + 3) * MONTH, page=2) for i in range(3)]
split += [row("秧秧", "p1_9", 31.0, NOW - 9 * MONTH, page=1),
          row("秧秧", "p2_9", 31.0, NOW - 10 * MONTH, page=2)]
check("跨分P 的同秒数不得合并成一个事件（分P 各自未达标 → 不命中）",
      sd.detect_repeat_events(split) == [], "(得到 %r)" % (sd.detect_repeat_events(split),))
# 但同一分P 内集中到 5 人即命中，且 video.page 正确
split2 = [row("秧秧", "p2_%d" % i, 30.0 + i, NOW - i * MONTH, page=2) for i in range(6)]
split2 += [row("秧秧", "p2_0", 31.0, NOW - 20 * MONTH, page=2),
           row("秧秧", "p2_1", 31.5, NOW - 21 * MONTH, page=2)]
ev4 = sd.detect_repeat_events(split2)
check("同一分P 内达标 → 命中且 page 指向该分P",
      len(ev4) == 1 and ev4[0]["video"]["page"] == 2 and ev4[0]["sender_count"] == 6,
      "(得到 %r)" % (ev4[0] if ev4 else None,))

print("=== 5. 阈值与边界 ===")
check("恰好 5 人 / 8 条即命中（含边界）",
      len(sd.detect_repeat_events([row("a", "t%d" % i, 10.0 + i * 0.5, NOW - i) for i in range(5)]
                                  + [row("a", "t0", 11.0, NOW), row("a", "t1", 12.0, NOW),
                                     row("a", "t2", 13.0, NOW)])) == 1)
check("4 人 / 8 条不命中（发送者数不足）",
      sd.detect_repeat_events([row("b", "u%d" % (i % 4), 10.0 + i, NOW - i) for i in range(8)]) == [])
check("5 人 / 7 条不命中（条数不足）",
      sd.detect_repeat_events([row("c", "v%d" % i, 10.0 + i, NOW - i) for i in range(5)]
                              + [row("c", "v0", 11.0, NOW), row("c", "v1", 12.0, NOW)]) == [])

print("=== 6. 报告区块渲染（双轴时间 + 跳转核验链接） ===")
BV = "BVrepeat0001"
storage.save_video_info(BV, {"bvid": BV, "title": "复读", "duration": 120,
                             "pages": [{"page": 1, "cid": 1, "duration": 120}], "stat": {}})
storage.append_danmaku(BV, [{"mid_hash": "r%d" % i, "content": "许愿不歪", "time": 33.0 + i * 0.2,
                             "timestamp": NOW - i * MONTH, "page": 1, "dmid": 1000 + i}
                            for i in range(8 - 2)] +
                           [{"mid_hash": "r%d" % (i + 6), "content": "许愿不歪", "time": 34.0 + i,
                             "timestamp": NOW - (i + 6) * MONTH, "page": 1, "dmid": 1000 + i + 6}
                            for i in range(2)], set())
html = webmod._repeat_events_block(BV)
check("区块渲染出视频内时间与跳转链接（单分P 不带 p 参数）",
      "视频 00:33" in html and "?t=33" in html and "p=1" not in html,
      "(片段 %r)" % (html[html.find("视频"):html.find("视频") + 60] if "视频" in html else html[:80],))
check("渲染不再出现旧的「时间段」表头（改为双轴时间列）",
      "时间段" not in html and "时间（视频内可点跳转核验" in html)

print("=== 7. 写法变体合并（同一波复读的不同写法算一句） ===")
# 「原神牛逼」+「原神牛逼！」+「原神牛逼！！！」本来是三行，人数还被拆散
va = [row("原神牛逼", "m%d" % i, 400.0 + i, NOW - i) for i in range(6)]
va += [row("原神牛逼！", "m%d" % i, 401.0 + i * 0.1, NOW - i) for i in range(6)]
va += [row("原神牛逼！！！", "m%d" % i, 402.0, NOW - i) for i in range(2)]
va += [row("原神牛逼！", "extra1", 403.0, NOW), row("原神牛逼！！", "extra2", 404.0, NOW)]
check("标点/重复字符变体合并成一个事件（人数取并集、条数求和）",
      len(sd.detect_repeat_events(va)) == 1
      and sd.detect_repeat_events(va)[0]["sender_count"] == 8
      and sd.detect_repeat_events(va)[0]["total"] == 16,
      "(得到 %r)" % ([(e["sender_count"], e["total"]) for e in sd.detect_repeat_events(va)],))
# 条数：原神牛逼×6 + 原神牛逼！×7 + 原神牛逼！！！×2 + 原神牛逼！！×1 = 16 条 / 8 人
check("主写法取该键下最多的写法，variant_count 统计不同写法数",
      sd.detect_repeat_events(va)[0]["content"] == "原神牛逼！"
      and sd.detect_repeat_events(va)[0]["variant_count"] == 4,
      "(得到 %r)" % (sd.detect_repeat_events(va)[0]["variants"],))
# 纯标点：？/？？？/？（半角）算一句（8 条 / 6 人），但不能与「……」并成一句
# ——若把纯标点归一化成空串，「？」就会和「……」并成一个事件
vp = [row("？", "q%d" % i, 30.0 + i * 0.1, NOW - i) for i in range(5)]
vp += [row("？？？", "q%d" % i, 30.5, NOW - i) for i in range(2)]
vp += [row("?", "q7", 31.0, NOW)]
vp += [row("……", "d%d" % i, 30.2 + i * 0.05, NOW - i) for i in range(8)]
evp = sd.detect_repeat_events(vp)
check("纯标点变体合并、且不与「……」误并（纯标点不退化空串）",
      len(evp) == 2 and {e["content"] for e in evp} == {"？", "……"}
      and [e for e in evp if e["content"] == "？"][0]["sender_count"] == 6
      and [e for e in evp if e["content"] == "？"][0]["variant_count"] == 3,
      "(得到 %r)" % ([(e["content"], e["sender_count"], e["variant_count"]) for e in evp],))
# 单个写法各自不达标、合并后才达标 → 说明阈值作用在归一化后的事件上
vt = [row("来了", "k%d" % i, 10.0 + i * 0.1, NOW - i) for i in range(3)]
vt += [row("来了！", "k%d" % (i + 3), 10.5 + i * 0.1, NOW - i) for i in range(3)]
vt += [row("来了", "k6", 11.0, NOW), row("来了！", "k7", 11.1, NOW)]
check("各自不达标、变体合并后达标即命中（阈值作用在归一化事件上）",
      len(sd.detect_repeat_events(vt)) == 1, "(得到 %r)" % (sd.detect_repeat_events(vt),))
# 英文单词不得因"折叠重复字母"被误并：good / god 是两句
vg = [row("good", "g%d" % i, 5.0 + i * 0.1, NOW - i) for i in range(5)]
vg += [row("good", "g0", 5.5, NOW), row("good", "g1", 5.6, NOW), row("good", "gx", 6.0, NOW)]
vg += [row("god", "h%d" % i, 5.2 + i * 0.1, NOW - i) for i in range(5)]
vg += [row("god", "h0", 5.7, NOW), row("god", "h1", 5.8, NOW), row("god", "gy", 6.1, NOW)]
check("英文不因连续字母折叠被误并（good 与 god 保持两句）",
      len(sd.detect_repeat_events(vg)) == 2,
      "(得到 %r)" % ([e["content"] for e in sd.detect_repeat_events(vg)],))
# 报告区块渲染：变体徽标
BV2 = "BVrepeat0002"
storage.save_video_info(BV2, {"bvid": BV2, "title": "变体复读", "duration": 120,
                              "pages": [{"page": 1, "cid": 2, "duration": 120}], "stat": {}})
storage.append_danmaku(BV2, [{"mid_hash": "w%d" % i, "content": "原神牛逼" if i % 2 else "原神牛逼！",
                              "time": 40.0 + i * 0.2, "timestamp": NOW - i, "page": 1,
                              "dmid": 2000 + i} for i in range(9)], set())
html2 = webmod._repeat_events_block(BV2)
check("区块渲染出「＋N 变体」徽标与变体 tooltip",
      "＋1 变体" in html2 and "原神牛逼！（" in html2, "(片段 %r)" % (html2[html2.find("原神牛逼"):html2.find("原神牛逼") + 120],))

print("")
print("==== 群体复读事件检测口径: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
