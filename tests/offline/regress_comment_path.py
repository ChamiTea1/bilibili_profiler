# -*- coding: utf-8 -*-
"""评论采集路径：wbi/legacy 翻页、异常降级、真重复页、脏 rpid、刷新计数（10 项）

离线回归脚本：使用隔离临时库 + 假 HTTP/假 OpenAI，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_comment_path.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="cmt_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()
import comment as C

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

def reply(rpid, mid=1001, text="hi"):
    return {"rpid": rpid, "member": {"mid": str(mid), "uname": "u%d" % mid, "sign": "",
                                     "level_info": {"current_level": 3}, "avatar": ""},
            "content": {"message": text}, "like": 1, "rcount": 0, "ctime": 1700000000,
            "reply_control": {"location": "IP属地：江苏"}, "replies": []}

class WbiClient:
    """第 fail_at 次 wbi 请求抛异常（None=不抛）；is_end 在最后一页"""
    def __init__(self, pages=3, fail_at=None): self.i = 0; self.fail_at = fail_at; self.pages = pages
    def get(self, url, params=None, **kw):
        self.i += 1
        if self.fail_at is not None and self.i == self.fail_at:
            raise RuntimeError("模拟网络异常")
        if "wbi" in url:
            p = self.i
            if p > self.pages:
                return {"code": 0, "data": {"replies": [], "cursor": {"is_end": True}}}
            return {"code": 0, "data": {"replies": [reply(p * 10)], "cursor": {
                "pagination_reply": {"next_offset": "off%d" % p}, "is_end": p >= self.pages}}}
        raise AssertionError("不应走旧接口")

print("=== 1. wbi 正常翻页 ===")
c = WbiClient(pages=3)
out = C._fetch_comments_wbi(1, c, 100, bvid="BVC1")
kv = {r["key"]: r["value"] for r in storage.get_db().execute(
    "SELECT key, value FROM phase_state WHERE bvid='BVC1' AND phase='comment'")}
check("三页全部采集", len(out) == 3 and len(c.__dict__) > 0, "(得到 %d 条主评论)" % len(out))
check("自然结束写 done=1", kv.get("done") == "1" and kv.get("mode") == "wbi")

print("=== 2. 首页请求异常 → 返回 None（调用方降级旧接口）===")
c2 = WbiClient(pages=3, fail_at=1)
res = C._fetch_comments_wbi(1, c2, 100, bvid="BVC2")
check("首页异常返回 None", res is None, repr(res))

print("=== 3. 中途请求异常 → 保留已采、不写 done、不崩 ===")
c3 = WbiClient(pages=3, fail_at=2)
out3 = C._fetch_comments_wbi(1, c3, 100, bvid="BVC3")
kv3 = {r["key"]: r["value"] for r in storage.get_db().execute(
    "SELECT key, value FROM phase_state WHERE bvid='BVC3' AND phase='comment'")}
check("中途异常保留第 1 页", len(out3) == 1, "(得到 %d 条)" % len(out3))
check("未写 done（可续采）", kv3.get("done") is None, "done=%s" % kv3.get("done"))

print("=== 4. fetch_comments 端到端：wbi 首页失败自动降级 legacy ===")
class FallbackClient:
    def __init__(self): self.legacy_hits = 0
    def get(self, url, params=None, **kw):
        if "wbi/main" in url:
            raise RuntimeError("模拟 wbi 不可用")
        self.legacy_hits += 1
        p = params.get("next") or 1
        if p > 2:
            return {"code": 0, "data": {"replies": [], "cursor": {"is_end": True}}}
        return {"code": 0, "data": {"replies": [reply(p * 100, mid=2000 + p)],
                                    "cursor": {"next": p + 1, "is_end": p >= 2}}}
fc = FallbackClient()
out4 = C.fetch_comments(1, fc, 100, bvid="BVC4")
kv4 = {r["key"]: r["value"] for r in storage.get_db().execute(
    "SELECT key, value FROM phase_state WHERE bvid='BVC4' AND phase='comment'")}
check("降级后旧接口采到数据", len(out4) == 2, "(得到 %d 条, legacy 请求 %d 次)" % (len(out4), fc.legacy_hits))
check("检查点记为 legacy 且 done=1", kv4.get("mode") == "legacy" and kv4.get("done") == "1")

print("=== 5. 真重复页检测（整页 rpid 已见过即终止）===")
class DupClient:
    def __init__(self): self.n = 0
    def get(self, url, params=None, **kw):
        self.n += 1
        if self.n == 1:
            return {"code": 0, "data": {"replies": [reply(7)], "cursor": {
                "pagination_reply": {"next_offset": "o1"}, "is_end": False}}}
        return {"code": 0, "data": {"replies": [reply(7)], "cursor": {
            "pagination_reply": {"next_offset": "o2"}, "is_end": False}}}   # 同一 rpid → 重复页
dc = DupClient()
out5 = C._fetch_comments_wbi(1, dc, 100, bvid="BVC5")
check("重复页终止且只采 1 条", len(out5) == 1 and dc.n == 2, "(条数 %d, 请求 %d 次)" % (len(out5), dc.n))

print("=== 6. 缺 rpid 的脏行不再塌缩成同一个 None ===")
class DirtyClient:
    def __init__(self): self.n = 0
    def get(self, url, params=None, **kw):
        self.n += 1
        rows = [{"member": {"mid": "9", "uname": "x"}, "content": {"message": "a"}, "like": 0,
                 "ctime": 1, "reply_control": {}} for _ in range(3)]        # 三条都缺 rpid/id
        return {"code": 0, "data": {"replies": rows, "cursor": {"is_end": True}}}
dd = DirtyClient()
out6 = C._fetch_comments_wbi(1, dd, 100, bvid="BVC6")
check("缺 rpid 行仍被采集（未误判重复页）", len(out6) == 3 and dd.n == 1, "(条数 %d)" % len(out6))

print("=== 7. refresh_comments 计数不重复累加 ===")
class RefreshClient:
    def __init__(self): self.n = 0
    def get(self, url, params=None, **kw):
        self.n += 1
        if self.n == 1:
            return {"code": 0, "data": {"replies": [reply(501, mid=3001), reply(502, mid=3002)],
                                        "cursor": {"pagination_reply": {"next_offset": "o"}, "is_end": False}}}
        return {"code": 0, "data": {"replies": [], "cursor": {"is_end": True}}}
rc = RefreshClient()
n_new = C.refresh_comments(1, rc, bvid="BVC7")
check("新增计数等于实际新增条数", n_new == 2, "(返回 %s)" % n_new)

print("")
print("==== 评论路径: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)