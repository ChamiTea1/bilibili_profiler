# -*- coding: utf-8 -*-
"""Web 报告审查修复：回复前缀遮蔽、页缓存时效、删除互斥、登录态粘性、
弹幕搜索、合成键口径、误报入参、高能点单飞、js_json / uid_linkable。

离线回归：隔离临时库，不联网、不读 Cookie、不消耗 LLM 额度。
"""
import json
import os
import sys
import time
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import tempfile
TMP = tempfile.mkdtemp(prefix="webrev_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db")
config.DB_PATH = storage.DB_PATH
storage.init_db()

import report
import web
from auth import REFRESH_DEAD, REFRESH_NONE, REFRESH_RETRY

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ✔ %s %s" % (name, extra))
    else:
        fail += 1
        print("  ✘ %s %s" % (name, extra))


NICK = "被回复者完整昵称甲"
REPLY = "回复 @%s :你在说什么" % NICK

print("=== 1. 回复前缀遮蔽 ===")
check("无前缀正文不改",
      report.mask_reply_prefix("普通评论", True, web._mask_name) == "普通评论")
check("mask 关闭不改前缀",
      report.mask_reply_prefix(REPLY, False, web._mask_name) == REPLY)
masked_reply = report.mask_reply_prefix(REPLY, True, web._mask_name)
check("mask 开启只替换前缀里的昵称",
      NICK not in masked_reply and masked_reply.startswith("回复 @（￣▽￣）甲")
      and masked_reply.endswith("你在说什么"),
      "(%s)" % masked_reply)

card = report.generate_user_card({
    "uid": 1, "name": "甲", "level": 1,
    "comments": [{"content": REPLY, "like": 2, "ctime": 0, "is_sub": 1}],
    "other_videos": {"items": [{
        "bvid": "BVother0001", "title": "另一视频", "danmaku_count": 0, "comment_count": 1,
        "danmaku_samples": [], "legacy": False,
        "comment_samples": [{"content": REPLY, "like": 0, "ctime": 0, "is_sub": 1}],
    }]},
}, mask=True, mask_name=web._mask_name)
check("用户卡评论区（本视频与其他视频）不出现完整昵称",
      NICK not in card and card.count("（￣▽￣）甲") >= 2)
plain_card = report.generate_user_card({
    "uid": 1, "name": "甲", "comments": [{"content": REPLY, "like": 1, "ctime": 0}],
})
check("未传 mask 时用户卡评论保持原文", NICK in plain_card)

subs = [{"rpid": 2, "uid": 9, "name": "回", "content": REPLY, "like": 1,
         "parent_rpid": 0, "problem": ""}]
with web.app.test_request_context("/", headers={"Cookie": "mask=1"}):
    tree = web._reply_tree_html(subs, 1, 0)
    board = web._problem_comment_board_html(
        [{"rpid": 3, "uid": 9, "name": "评", "content": REPLY, "ctime": 0, "like": 1,
          "reply_count": 0, "is_sub": 1, "problem": "引战阴阳", "heat": 1,
          "parent_content": "", "parent_name": ""}], [], None, 0)
check("高回复回复树在 mask 下不出现完整昵称", NICK not in tree and "（￣▽￣）甲" in tree)
check("问题评论榜在 mask 下不出现完整昵称", NICK not in board and "（￣▽￣）甲" in board)
with web.app.test_request_context("/"):
    tree_off = web._reply_tree_html(subs, 1, 0)
check("mask 关闭时回复树保留昵称", NICK in tree_off)


print("=== 2. js_json 与 uid_linkable ===")
blob = report.js_json({"html": "<!--<script>alert(1)</script>"})
check("js_json 后不再出现字面量 <", "<" not in blob, "(%s)" % blob[:48])
check("js_json 结果仍可被 JSON 解析还原",
      json.loads(blob)["html"] == "<!--<script>alert(1)</script>")

now = time.time()
crc = {"uid": 11, "resolve_method": "CRC32破解",
       "_resolution": {"candidate_status": {"11": "exists"}, "checked_at": now}}
check("缺 candidate_checked_at 时不回退解析时刻、不可点击", not report.uid_linkable(crc))
fresh = {"uid": 11, "resolve_method": "CRC32破解",
         "_resolution": {"candidate_status": {"11": "exists"},
                         "candidate_checked_at": {"11": now}, "checked_at": 0}}
check("有新鲜核验时刻则可点击", report.uid_linkable(fresh))
plain = {"uid": 22, "resolve_method": "评论区验证",
         "_resolution": {"checked_at": 0}}
check("明文来源不依赖核验时刻、直接可点击", report.uid_linkable(plain))


print("=== 3. 页缓存按时间与修订号失效，mask 分版本 ===")
BV = "BVwebrev001"
storage.save_video_info(BV, {"bvid": BV, "title": "缓存", "cid": 1, "duration": 30})
cl = web.app.test_client()
first = cl.get("/video/" + BV)
check("空视频报告页可渲染", first.status_code == 200 and "缓存" in first.get_data(as_text=True))
key = (BV, False)
entry = web._PAGE_CACHE.get(key)
check("缓存记下渲染时刻且按 mask 分键",
      entry is not None and len(entry) >= 3 and isinstance(entry[2], float))
fp = entry[0]
web._PAGE_CACHE[key] = (fp, "FRESH_MARKER", time.time())
check("指纹相同且未过期时命中缓存",
      cl.get("/video/" + BV).get_data(as_text=True) == "FRESH_MARKER")
web._PAGE_CACHE[key] = (fp, "STALE_MARKER", time.time() - 10 ** 9)
stale = cl.get("/video/" + BV).get_data(as_text=True)
check("超过寿命后当未命中并重渲染", "STALE_MARKER" not in stale and "缓存" in stale)
web._PAGE_CACHE[key] = (("bogus",), "BAD_FP", time.time())
bad = cl.get("/video/" + BV).get_data(as_text=True)
check("数据修订号变化仍失效", "BAD_FP" not in bad)
fp2 = web._PAGE_CACHE[key][0]
web._PAGE_CACHE[(BV, True)] = (fp2, "MASK_ONLY", time.time())
check("未开 mask 不命中遮蔽版缓存", "MASK_ONLY" not in cl.get("/video/" + BV).get_data(as_text=True))
cl_mask = web.app.test_client()
cl_mask.set_cookie("mask", "1")
check("mask 开启命中自己的缓存版本",
      cl_mask.get("/video/" + BV).get_data(as_text=True) == "MASK_ONLY")
web._PAGE_CACHE.clear()


print("=== 4. 删除接口原子占位 ===")
BVD = "BVwebrevdel"
storage.save_video_info(BVD, {"bvid": BVD, "title": "删", "cid": 1, "duration": 10})
job_id, reject = web._try_register_job("analyze", BVD, total=1)
check("预先占位成功", job_id is not None and reject == "")
denied = cl.post("/api/video/%s/delete" % BVD)
check("已有任务时删除返回 409", denied.status_code == 409, "(%s)" % denied.status_code)
check("409 没有抢占原任务", web._has_running_job(BVD))
with web.JOBS_LOCK:
    web.JOBS[job_id]["finished"] = True
seen = {}
real_delete = web.delete_video_data
def _watch_delete(bvid):
    seen["running"] = web._has_running_job(bvid)
    return real_delete(bvid)
web.delete_video_data = _watch_delete
try:
    gone = cl.post("/api/video/%s/delete" % BVD)
finally:
    web.delete_video_data = real_delete
check("删除期间持有 delete 占位，结束后释放",
      gone.status_code == 200 and seen.get("running") is True and not web._has_running_job(BVD),
      "(status=%s running=%s)" % (gone.status_code, seen.get("running")))
web.JOBS.clear()


print("=== 5. 误报接口拒绝非对象 JSON ===")
bad_fp = cl.post("/api/video/%s/false_positive" % BV, json=["dm", "x"])
check("JSON 数组返回 400", bad_fp.status_code == 400, "(%s)" % bad_fp.get_json())
ok_fp = cl.post("/api/video/%s/false_positive" % BV,
                json={"kind": "dm", "target": "不会命中", "count_only": True})
check("JSON 对象仍可查询影响面", ok_fp.status_code == 200 and ok_fp.get_json().get("ok") is True)


print("=== 6. 登录态：网络抖动不粘失效，伪字段弹出 ===")
def _reset_client():
    web._client = None
    web._client_failed = False

def _cookie():
    return {"SESSDATA": "sess", "bili_jct": "jct",
            "_refresh_token": "rtok", "_refreshed_at": "1700000000"}

_reset_client()
held = {}
def _verify_ok(client):
    held["client"] = client
    return True
with patch.object(web, "load_cookie", side_effect=_cookie), \
     patch.object(web, "verify_cookie", side_effect=_verify_ok):
    got = web._get_client()
c = held["client"]
names = c.get_cookies_dict()
check("加载 cookie 弹出伪字段并记下刷新时间",
      got is c and c._refresh_token == "rtok" and c._refreshed_at == 1700000000.0
      and "_refresh_token" not in names and "_refreshed_at" not in names)

_reset_client()
with patch.object(web, "load_cookie", side_effect=_cookie), \
     patch.object(web, "verify_cookie", return_value=False), \
     patch.object(web, "_try_refresh_cookie", return_value=REFRESH_RETRY):
    try:
        web._get_client()
        raised = False
    except web.CookieInvalidError:
        raised = True
check("REFRESH_RETRY 不置粘性失效", raised and web._client_failed is False)

_reset_client()
with patch.object(web, "load_cookie", side_effect=_cookie), \
     patch.object(web, "verify_cookie", return_value=False), \
     patch.object(web, "_try_refresh_cookie", return_value=REFRESH_DEAD):
    try:
        web._warm_client()
    except Exception:
        pass
check("热身失败（含 REFRESH_DEAD）不置粘性标记", web._client_failed is False and web._client is None)

_reset_client()
with patch.object(web, "load_cookie", side_effect=_cookie), \
     patch.object(web, "verify_cookie", return_value=False), \
     patch.object(web, "_try_refresh_cookie", return_value=REFRESH_DEAD):
    try:
        web._get_client()
    except web.CookieInvalidError:
        pass
check("REFRESH_DEAD 才置粘性失效", web._client_failed is True)

_reset_client()
with patch.object(web, "load_cookie", side_effect=lambda: {"SESSDATA": "sess"}), \
     patch.object(web, "verify_cookie", return_value=False):
    try:
        web._get_client()
    except web.CookieInvalidError:
        pass
check("无刷新令牌且验证失败置粘性失效", web._client_failed is True)
_reset_client()


print("=== 7. 手动分析评论只加载一次并按 uid 分组 ===")
calls = {"n": 0}
def _count_comments(bvid, uids=None):
    calls["n"] += 1
    return [
        {"uid": 11, "content": REPLY, "like": 3, "location": "", "rpid": 1},
        {"uid": 22, "content": "普通", "like": 1, "location": "", "rpid": 2},
    ]
saved = {}
def _save_user(uid, name, level, user_data, profile, bvid=None):
    saved[uid] = [c.get("content") for c in profile.get("comments") or []]
def _analyze(user_data, dm, spam):
    return {"name": user_data.get("name", ""), "uid": user_data.get("uid"), "danmaku": dm}
senders = [
    {"mid_hash": "aaaaaaa1", "uid": 11, "spam_level": "低", "spam_score": 0, "resolution": {}},
    {"mid_hash": "aaaaaaa2", "uid": 22, "spam_level": "低", "spam_score": 0, "resolution": {}},
    {"mid_hash": "aaaaaaa3", "uid": 33, "spam_level": "低", "spam_score": 0, "resolution": {}},
]
web.JOBS["n1job"] = {"errors": [], "results": [], "finished": False, "current": "", "done": 0,
                    "bvid": "BVn1comments"}
with patch.object(web, "load_comments", side_effect=_count_comments), \
     patch.object(web, "load_senders", return_value=senders), \
     patch.object(web, "resolution_is_current", return_value=True), \
     patch.object(web, "load_video_profile", return_value=None), \
     patch.object(web, "load_user_data", side_effect=lambda uid: ({"name": "n", "level": 1, "uid": uid}, {})), \
     patch.object(web, "analyze_profile", side_effect=_analyze), \
     patch.object(web, "save_user_data", side_effect=_save_user), \
     patch.object(web, "_get_client", return_value=object()), \
     patch.object(web, "_get_pool", return_value=object()), \
     patch.object(web, "LLM_API_KEY", ""):
    web._run_analysis_job("n1job", "BVn1comments", ["aaaaaaa1", "aaaaaaa2", "aaaaaaa3"])
check("三个发送者只 load_comments 一次", calls["n"] == 1, "(calls=%s)" % calls["n"])
check("评论按 uid 分组，不串到别人头上",
      saved.get(11) == [REPLY] and saved.get(22) == ["普通"] and saved.get(33) == [],
      "(%s)" % saved)
web.JOBS.pop("n1job", None)


print("=== 8. 弹幕搜索用原始昵称，响应不泄露 ===")
BVS = "BVwebrevsrch"
storage.save_video_info(BVS, {"bvid": BVS, "title": "搜", "cid": 1, "duration": 20})
REAL = "搜索专用昵称甲乙"
storage.save_sender(BVS, "abc12345", 880011, "高", "评论区验证", 1, ["哈哈"], "低", 0.0)
storage.save_user_data(880011, REAL, 2, {"uid": 880011, "name": REAL},
                       {"uid": 880011, "name": REAL}, bvid=BVS)
storage.append_danmaku(BVS, [{"mid_hash": "abc12345", "content": "哈哈", "time": 1.0,
                              "timestamp": 1700000001, "page": 1, "dmid": 1}], set())
cl_s = web.app.test_client()
cl_s.set_cookie("mask", "1")
found = cl_s.get("/api/video/%s/danmaku" % BVS, query_string={"sender": REAL})
body = found.get_data(as_text=True)
rows = (found.get_json() or {}).get("rows") or []
check("mask 下按原始昵称能搜到，响应不含完整真名",
      found.status_code == 200 and len(rows) == 1 and REAL not in body
      and rows[0]["name"].endswith("乙") and "raw_name" not in rows[0],
      "(name=%s)" % (rows[0]["name"] if rows else None))


print("=== 9. 已解析发送者排除 cmt: 合成键 ===")
storage.save_sender(BVS, "cmt:880011", 880011, "高", "问题评论", 0, [], "低", 0.0)
panel = web._danmaku_panel_stats(BVS)
rq = web._resolve_quality(BVS)
check("弹幕面板与解析质量都不把 cmt: 算进已解析发送者",
      panel.get("resolved") == 1 and rq.get("resolved") == 1 and rq.get("comment_authors") == 1,
      "(panel=%s rq=%s authors=%s)" % (panel.get("resolved"), rq.get("resolved"), rq.get("comment_authors")))


print("=== 10. 密度峰值同一视频单飞，失败不标注 ===")
BVP = "BVwebrevpeak"
storage.append_danmaku(BVP, [{"mid_hash": "abc12345", "content": "峰值弹幕", "time": 95.0,
                              "timestamp": 1700000002, "page": 1, "dmid": 9}], set())
n = 10
density = {"pages": [{
    "page": 1, "data": [1] * 9 + [50], "starts": [i * 10 for i in range(n)],
    "labels": ["t%d" % i for i in range(n)], "duration": 100,
}]}
peak_calls = {"n": 0}
def _slow_peaks(*_a, **_k):
    peak_calls["n"] += 1
    time.sleep(0.25)
    return {0: {"label": "名场面", "kind": "名场面"}}
errs = []
def _run():
    try:
        _run.out.append(web._annotate_density_peaks(BVP, json.loads(json.dumps(density)), {}))
    except Exception as e:
        errs.append(e)
_run.out = []
with patch.object(web, "LLM_API_KEY", "offline"), \
     patch.object(web, "detect_density_peaks", side_effect=_slow_peaks):
    t1 = threading.Thread(target=_run)
    t2 = threading.Thread(target=_run)
    t1.start(); t2.start(); t1.join(); t2.join()
check("并发标注只调用一次 LLM", peak_calls["n"] == 1 and not errs, "(calls=%s errs=%s)" % (peak_calls["n"], errs))
with patch.object(web, "LLM_API_KEY", "offline"), \
     patch.object(web, "detect_density_peaks", side_effect=RuntimeError("llm down")):
    failed = web._annotate_density_peaks(BVP, json.loads(json.dumps(density)), {})
check("标注失败静默降级、不挂峰值",
      failed is not None and not any(p.get("peaks") for p in failed["pages"]))


# 长条卡片只展示未生成画像用户，并保留本视频发言和安全链接。
light_senders = [{"uid": 778899, "mid_hash": "abcd", "method": "评论区验证", "danmaku_count": 2,
                  "contents": ["<script>弹幕</script>", "第二条"], "resolution": {}, "spam_level": "中"},
                 {"uid": 998877, "mid_hash": "efgh", "method": "CRC32破解", "danmaku_count": 1,
                  "contents": ["待核验"], "resolution": {}}]
with patch.object(web, "load_senders", return_value=light_senders), patch.object(web, "load_comments", return_value=[
    {"uid": 778899, "uname": "轻量用户甲", "content": REPLY}]):
    light = web._light_user_cards("BVlight", [], False, set())
    hidden = web._light_user_cards("BVlight", [], True, set())
    covered = web._light_user_cards("BVlight", [{"uid": 778899}], False, set())
check("长条展示弹幕评论并转义脚本", "第二条" in light and REPLY in light and "&lt;script&gt;" in light)
check("明文账号卡片可直达空间，未知CRC不给空间链接", 'href="https://space.bilibili.com/778899"' in light and 'href="https://space.bilibili.com/998877"' not in light)
check("长条遮蔽昵称UID及回复前缀", "轻量用户甲" not in hidden and NICK not in hidden and "UID:778899" not in hidden)
check("已有完整画像不重复展示长条", "第二条" not in covered and "待核验" in covered)

# 深采名额不截断长条名单；超过100人的名单完整进入前端分页集合。
large = [{"uid": 800000 + i, "mid_hash": "h" + str(i), "method": "问题评论", "danmaku_count": 0,
          "contents": [], "resolution": {}, "confidence": "高"} for i in range(350)]
with patch.object(web, "load_senders", return_value=large), patch.object(web, "load_comments", return_value=[]):
    remaining = web._light_user_cards("BVlarge", [{"uid": 800000 + i} for i in range(100)], False, set())
check("原名单350人，100份画像以外250名长条全部保留", remaining.count('class="user-card light-user-card"') == 250 and 'id="uid-800349"' in remaining)
with web.app.test_client() as client:
    info = client.get("/api/report-service").get_json()
check("报告服务返回导入时冻结的指纹和任务数", info["source_revision"] == web._LOADED_SOURCE_REVISION and isinstance(info["active_jobs"], int))

print("")
print("==== Web 报告审查: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
