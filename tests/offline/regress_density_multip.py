# -*- coding: utf-8 -*-
"""弹幕密度时间轴分P口径：多分P视频必须按各分P内部时间分别建桶（9 项）

复现并锁死的问题：danmaku.time 是「各分P内部时间」（0~该P时长），而 videos.duration 是各分P
时长之和；旧实现把两者混用，导致 4 分P 的 BV1mtTD6rEtQ（总时长 418s）报告里弹幕全挤在
时间轴前 11 桶（01:52 之前）、02:02 之后恒为 0——看起来像「2 分钟后没人发弹幕」，
实际是 P1 之外的弹幕被画到了错误的时间位置。

离线回归脚本：使用隔离临时库，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_density_multip.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="dmp_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()
import web as webmod

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

MULTI, SINGLE, NOMETA, EMPTY = "BVmulti00001", "BVsingle0001", "BVnometa0001", "BVempty0001"
# 4 分P：109 + 103 + 103 + 103 = 418s（与 BV1mtTD6rEtQ 同形）
PAGES = [{"page": i + 1, "cid": 100 + i, "part": "第%dP" % (i + 1), "duration": d}
         for i, d in enumerate((109, 103, 103, 103))]


def _dm(page, times):
    """构造该分P的弹幕行（time 为分P内部时间）"""
    return [{"mid_hash": "h%d%02d" % (page, i), "content": "c", "time": t,
             "timestamp": 1700000000 + i, "mode": 1, "color": "#ffffff", "pool": 0,
             "dmid": 0, "page": page} for i, t in enumerate(times)]


storage.save_video_info(MULTI, {"bvid": MULTI, "title": "多分P", "duration": 418,
                                "pages": PAGES, "stat": {}})
# P2 的 101s 是「分P内部时间」：按旧口径（轴长 418）会被画进 01:41 附近的桶——
# 与 P1 的 01:41 混在一起，而 P2 自己的 101s 实际发生在该分P末尾
storage.append_danmaku(MULTI, _dm(1, [5.0, 50.0, 100.0, 108.9]) + _dm(2, [10.0, 60.0, 101.0])
                       + _dm(3, [30.0]) + _dm(4, [90.0]), set())

d = webmod._danmaku_density(MULTI, 418, {"pages": PAGES}) or {}
pgs = d.get("pages") or []
check("多分P：每个分P单独一项", d.get("multi") is True and [p["page"] for p in pgs] == [1, 2, 3, 4],
      "(得到 %r)" % ([p["page"] for p in pgs],))
check("多分P：各分P轴长=该分P时长（而非全片 418s）",
      [p["duration"] for p in pgs] == [109, 103, 103, 103],
      "(得到 %r)" % ([p["duration"] for p in pgs],))
check("多分P：各分P桶数按其自身时长（109s→10 桶，与旧实现 41 桶不同）",
      [len(p["data"]) for p in pgs] == [10, 10, 10, 10],
      "(得到 %r)" % ([len(p["data"]) for p in pgs],))
check("多分P：各分P弹幕数不丢不串（4/3/1/1）",
      [sum(p["data"]) for p in pgs] == [4, 3, 1, 1],
      "(得到 %r)" % ([sum(p["data"]) for p in pgs],))
# P2 轴长 103s、10 桶（10.3s/桶）：101s → 第 9 桶（末尾），旧口径下会落进前半段
check("分P内部时间按该P自身分桶（P2 的 101s 落在本P末桶）",
      pgs[1]["data"][9] == 1 and sum(pgs[1]["data"][:9]) == 2,
      "(P2 桶=%r)" % (pgs[1]["data"],))
check("默认选中弹幕最多的分P（P1）", d.get("default") == 0, "(得到 %r)" % (d.get("default"),))

# 单分P视频：轴长仍用 videos.duration（旧口径不变，避免既有报告换图）
storage.save_video_info(SINGLE, {"bvid": SINGLE, "title": "单分P", "duration": 200,
                                 "pages": [{"page": 1, "cid": 9, "part": "", "duration": 200}],
                                 "stat": {}})
storage.append_danmaku(SINGLE, _dm(1, [10.0, 25.0, 199.0]), set())
ds = webmod._danmaku_density(SINGLE, 200, {"pages": [{"page": 1, "cid": 9, "duration": 200}]}) or {}
sp = (ds.get("pages") or [{}])[0]
check("单分P：桶数/轴长与旧口径一致（200s→20 桶，10s/桶）",
      ds.get("multi") is False and len(sp.get("data") or []) == 20
      and sp["data"][1] == 1 and sp["data"][2] == 1 and sp["data"][19] == 1,
      "(得到 %r)" % (sp.get("data"),))

# 多分P但分P时长元信息缺失（旧报告缺 video_info_json.pages）：降级单轴而不是整块不渲染
storage.save_video_info(NOMETA, {"bvid": NOMETA, "title": "缺元信息", "duration": 418, "stat": {}})
storage.append_danmaku(NOMETA, _dm(1, [30.0]) + _dm(2, [40.0]), set())
dn = webmod._danmaku_density(NOMETA, 418, {}) or {}
check("多分P缺分P时长：降级单轴（onepage_fallback）且不返回 None",
      dn.get("onepage_fallback") is True and dn.get("multi") is False
      and sum((dn.get("pages") or [{}])[0].get("data") or []) == 2, "(得到 %r)" % (dn.get("multi"),))

storage.save_video_info(EMPTY, {"bvid": EMPTY, "title": "无弹幕", "duration": 100, "stat": {}})
check("无弹幕数据：返回 None 不渲染", webmod._danmaku_density(EMPTY, 100, {}) is None)

print("")
print("==== 弹幕密度时间轴分P口径: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
