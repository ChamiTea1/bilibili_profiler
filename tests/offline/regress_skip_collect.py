# -*- coding: utf-8 -*-
"""--skip-collect：阶段5 只读库内已采数据、零网络请求（4 项）

复现并锁死的行为：--skip-collect（phase_collect_users(cache_only=True)）必须
完全不触碰采集池/网络，只把 users 表里已有的数据带回阶段6；未采用户不返回。

离线回归脚本：使用隔离临时库 + 哨兵采集池，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_skip_collect.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="skc_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()
import main

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

CACHED_UID, FRESH_UID = 1001, 2002
storage.save_user_data(CACHED_UID, "已采用户", 5,
                       {"uid": CACHED_UID, "name": "已采用户", "level": 5}, {})

def _resolved(*uids):
    return {"h%d" % u: {"uid": u, "confidence": "高", "method": main.METHOD_COMMENT_VERIFY,
                        "danmaku_count": 3, "contents": ["x"], "spam_level": "低",
                        "spam_score": 0.0, "collision_risk": False} for u in uids}

class SentinelPool:
    """任何属性访问都视为"碰了网络"：--skip-collect 下不应发生"""
    touched = []

    def __getattr__(self, name):
        SentinelPool.touched.append(name)
        raise AssertionError("阶段5 在 --skip-collect 下不应触碰采集池（访问了 %r）" % name)

pool = SentinelPool()
err = None
try:
    got = main.phase_collect_users(_resolved(CACHED_UID, FRESH_UID), pool, cache_only=True)
except Exception as e:
    got, err = None, e

check("库内已采用户被带回阶段6", isinstance(got, dict) and CACHED_UID in got,
      "(得到 %r, 异常 %r)" % (got if got is None else sorted(got), err))
check("未采集用户不出现在结果中", isinstance(got, dict) and FRESH_UID not in got,
      "" if not isinstance(got, dict) else "(键 %s)" % sorted(got))
check("全程零网络：采集池一次都没被触碰", not SentinelPool.touched and err is None,
      "(触碰记录 %r)" % (SentinelPool.touched,))

# 全新视频（库内无任何已采数据）：返回空 map 且不报错、不触碰池
SentinelPool.touched.clear()
got2, err2 = None, None
try:
    got2 = main.phase_collect_users(_resolved(FRESH_UID), SentinelPool(), cache_only=True)
except Exception as e:
    err2 = e
check("库内无已采数据时返回空量表且不报错", got2 == {} and err2 is None,
      "(得到 %r, 异常 %r)" % (got2, err2))

print("")
print("==== --skip-collect 只读缓存: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
