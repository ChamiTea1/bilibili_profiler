# -*- coding: utf-8 -*-
"""B站屏蔽列表导出：选人标准/误报扣除/低置信度排除/排序截断/路由下载（8 项）

离线回归脚本：使用隔离临时库，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_blocklist.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="blk_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))


# ---------- 造数据：senders + 画像（cringe/comment_problem）+ 问题评论 + 误报 ----------
BV = "BVblk000001"
storage.save_video_info(BV, {"bvid": BV, "title": "t", "cid": 1})

def _profile(uid, cringe=None, comment_problem=None):
    p = {"uid": uid, "name": "u%d" % uid}
    if cringe is not None:
        p["cringe"] = cringe
    if comment_problem is not None:
        p["comment_problem"] = comment_problem
    return p

def _cringe(*items):
    return {"count": len(items), "max_severity": max((i["severity"] for i in items), default=1),
            "categories": list(dict.fromkeys(i["category"] for i in items)),
            "items": list(items), "examples": list(items)[:5]}

def _item(c, cat, sev):
    return {"content": c, "category": cat, "severity": sev, "reason": "r"}

def _save_user(uid, name, profile):
    """建 users 行 + 落 video_profiles 画像。

    画像按 (bvid, uid) 存 video_profiles——同一 UID 在不同视频的问题弹幕/评论不可共用，
    users.profile_json 仅为旧库兼容列（save_user_data 不再落画像）。
    """
    storage.save_user_data(uid, name, 3, {}, None)
    storage.save_video_profile(BV, uid, profile)

# 101：问题弹幕（严重度3，高置信度）→ cringe 标准入选
storage.save_sender(BV, "h101", 101, "高", "评论区验证", 10, ["滚", "垃圾"],
                    "低", 0.0)
_save_user(101, "u101", _profile(
    101, cringe=_cringe(_item("滚", "人身攻击", 3), _item("垃圾", "人身攻击", 2))))
# 102：问题弹幕但全部内容被标记误报 → 不入选；部分标记仍入选
storage.save_sender(BV, "h102", 102, "高", "评论区验证", 5, ["x1", "x2"], "低", 0.0)
_save_user(102, "u102", _profile(
    102, cringe=_cringe(_item("x1", "引战阴阳", 2), _item("x2", "引战阴阳", 2))))
# 103：高/中风险刷屏（spam 标准）
storage.save_sender(BV, "h103", 103, "高", "评论区验证", 30, ["刷"] * 30, "高", 9.5)
# 104：刷屏但被标记误报（kind=spam）→ 展示层降级口径，不入选
storage.save_sender(BV, "h104", 104, "高", "评论区验证", 30, ["刷2"] * 30, "高", 9.0)
storage.toggle_false_positive(BV, "spam", "h104")
# 105：未解析（uid=None）+ 高风险刷屏 → 无 UID 可导出，计入 skipped_unresolved
storage.save_sender(BV, "h105", None, "", "", 20, ["?"], "高", 8.0)
# 106：低置信度（CRC32 反查）+ 问题弹幕 → 默认排除，include_lowconf 才纳入
storage.save_sender(BV, "h106", 106, "低", "CRC32破解", 8, ["骂"], "低", 0.0)
_save_user(106, "u106", _profile(
    106, cringe=_cringe(_item("骂", "人身攻击", 3))))
# 107/108：问题评论作者（评论区明文 UID，无 senders 行）；108 的评论被标记误报
storage.save_comments(BV, [
    {"rpid": 900, "uid": 107, "content": "拉踩", "like": 5, "is_sub": 0},
    {"rpid": 901, "uid": 108, "content": "引战", "like": 5, "is_sub": 0},
])
storage.update_comment_problems(BV, {900: "引战阴阳", 901: "引战阴阳"})
storage.toggle_false_positive(BV, "cmt", "901")
# 102 的 x1 标记弹幕误报（部分扣除：剩 x2 仍命中）
storage.toggle_false_positive(BV, "dm", "x1")

import web as webmod

print("=== 1. 选人标准与误报扣除 ===")
r = webmod._build_blocklist(BV, {"cringe", "spam"}, 200)
uids = [int(e["filter"]) for e in r["entries"]]
check("cringe+spam：问题弹幕/刷屏入选，误报全扣者排除、cmt 维度未选不纳入",
      101 in uids and 102 in uids and 103 in uids and 104 not in uids
      and 107 not in uids and 108 not in uids,
      "(uid %s)" % uids)
check("未解析发送者计入 skipped_unresolved", r["skipped_unresolved"] == 1,
      "(%d)" % r["skipped_unresolved"])

print("=== 2. 低置信度排除（防误屏蔽）===")
check("低置信度默认排除", 106 not in uids, "(uid %s)" % uids)
r_low = webmod._build_blocklist(BV, {"cringe"}, 200, include_lowconf=True)
uids_low = [int(e["filter"]) for e in r_low["entries"]]
check("include_lowconf=True 纳入", 106 in uids_low, "(uid %s)" % uids_low)

print("=== 3. 问题评论作者维度（cmt）===")
r_cmt = webmod._build_blocklist(BV, {"cringe", "spam", "cmt"}, 200)
uids_cmt = [int(e["filter"]) for e in r_cmt["entries"]]
check("cmt 维度纳入明文评论作者、cmt 误报排除",
      107 in uids_cmt and 108 not in uids_cmt, "(uid %s)" % uids_cmt)
check("排序按 严重度>刷屏分>弹幕数（101:3 → 102:2 → 107:1 → 103:0分9.5）",
      uids_cmt[:4] == [101, 102, 107, 103], "(前4 %s)" % uids_cmt[:4])

print("=== 4. 截断与条目格式 ===")
r_cap = webmod._build_blocklist(BV, {"cringe"}, 1)
check("上限截断保留最高严重度", [int(e["filter"]) for e in r_cap["entries"]] == [101],
      "(%s)" % [e["filter"] for e in r_cap["entries"]])
e0 = r_cmt["entries"][0]
check("条目为B站屏蔽列表口径（type=2 用户屏蔽）",
      isinstance(e0["type"], int) and e0["type"] == 2 and e0["filter"] == str(101)
      and e0["opened"] is True and e0["id"] == 1 and "问题弹幕" in e0["comment"],
      "(%s)" % e0)

print("=== 5. 路由：下载与错误口径 ===")
webmod.app.config["TESTING"] = True
tc = webmod.app.test_client()
resp = tc.get("/api/video/%s/blocklist?crit=cringe,spam&max=200" % BV)
check("合法请求 200 + 附件下载头",
      resp.status_code == 200 and "attachment" in resp.headers.get("Content-Disposition", "")
      and b'"type": 2' in resp.data,
      "(status %d)" % resp.status_code)
check("未知视频 404", tc.get("/api/video/BVnope/blocklist").status_code == 404)
check("非法 crit 400", tc.get("/api/video/%s/blocklist?crit=bogus" % BV).status_code == 400)

print("=== 6. 跨视频合并屏蔽榜（惯犯优先）===")
# 第二个视频：101 再命中问题弹幕（跨视频惯犯）；109 仅在此视频刷屏（单视频命中）
BV2 = "BVblk000002"
storage.save_video_info(BV2, {"bvid": BV2, "title": "t2", "cid": 2})
storage.save_sender(BV2, "g101", 101, "高", "评论区验证", 7, ["骂人"], "低", 0.0)
# 本地架构：画像按 (bvid,uid) 存 video_profiles，save_user_data 不再落 profile_json
storage.save_video_profile(BV2, 101, _profile(
    101, cringe=_cringe(_item("骂人", "人身攻击", 2))))
storage.save_sender(BV2, "g109", 109, "高", "评论区验证", 25, ["冲"] * 25, "高", 8.0)
rc = webmod._build_cross_blocklist({"cringe", "spam"}, 2, 200)
cross_uids = [int(e["filter"]) for e in rc["entries"]]
check("双视频命中者入选、单视频命中者按 min_videos 排除",
      cross_uids == [101] and 109 not in cross_uids and 103 not in cross_uids,
      "(uid %s)" % cross_uids)
check("comment 标注命中视频数", "2个视频命中" in rc["entries"][0]["comment"],
      "(%s)" % rc["entries"][0]["comment"])
rc1 = webmod._build_cross_blocklist({"cringe", "spam"}, 1, 200)
check("min_videos=1 时单视频命中者也入选（按命中视频数排序）",
      101 in [int(e["filter"]) for e in rc1["entries"]]
      and int(rc1["entries"][0]["filter"]) == 101,
      "(前2 %s)" % [e["filter"] for e in rc1["entries"][:2]])
resp_c = tc.get("/api/blocklist/cross?crit=cringe,spam&min_videos=2")
check("跨视频路由 200 + 附件下载头 + type=2 条目",
      resp_c.status_code == 200 and "attachment" in resp_c.headers.get("Content-Disposition", "")
      and b'"type": 2' in resp_c.data,
      "(status %d)" % resp_c.status_code)
check("跨视频无命中 404", tc.get("/api/blocklist/cross?crit=cmt&min_videos=2").status_code == 404)
check("跨视频非法 crit 400", tc.get("/api/blocklist/cross?crit=bogus").status_code == 400)

print("")
print("==== 屏蔽列表导出: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
