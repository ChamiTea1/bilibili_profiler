# -*- coding: utf-8 -*-
"""分享长图导出：payload 装配 / 渲染护栏 / 路由与遮蔽（9 项）

背景：src/long_image.py 曾只被 web.py import 而没有任何路由与按钮调用——模块写完却
没接线，且因为 pyflakes 过滤了 "imported but unused" 而无人发现。本用例同时锁住
「引擎行为」与「确实接上了路由且跟随隐藏信息开关」。

离线回归脚本：隔离临时库 + Flask test_client，**不联网、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_long_image.py
"""
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="limg_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()

import long_image as li
import web
from PIL import Image

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))


# ---------- 1) payload 装配（纯字典，不碰库） ----------
def _prof(uid, name, spam, dm, cringe_n=0, tags=None, ai=""):
    p = {"uid": uid, "name": name, "level": 6,
         "danmaku": {"count": dm, "spam_level": spam},
         "tags": tags or ["硬核用户"]}
    if cringe_n:
        p["cringe"] = {"count": cringe_n, "max_severity": 2, "categories": ["引战阴阳"],
                       "examples": [{"content": "你懂什么", "category": "引战阴阳", "reason": "嘲讽"}]}
    if ai:
        p["ai_deep"] = ai
    return p

profs = [_prof(1, "甲", "高", 30, cringe_n=2, ai="深掘文本"),
         _prof(2, "乙", "中", 5)] + \
        [_prof(100 + i, "丙%d" % i, "低", 1) for i in range(20)]
stats = {"total": len(profs)}
chart = {"cat_counts": {"引战阴阳": 2, "广告引流": 1}}
attack = {"attackers": [{"name": "甲", "categories": ["引战阴阳"], "count": 3,
                         "examples": [{"content": "攻击原文"}]}],
          "victims": [{"name": "乙", "count": 2, "categories": [],
                       "comments": {"1": {"parent": "受害原评", "attacks": [{"attack": "攻击原文"}]}}}]}
comments = [{"name": "丙", "like": 9, "reply_count": 2, "problem": "引战阴阳", "content": "问题评论"},
            {"name": "丁", "like": 1, "reply_count": 0, "problem": "广告引流", "content": "广告"}]
events = [{"content": "接龙", "sender_count": 7, "total": 12},
          {"content": "队列", "sender_count": 5, "total": 9}]
video_info = {"title": "测试视频", "duration": 100, "pubdate": 1700000000,
              "owner": {"name": "UP"}, "stat": {"view": 10, "danmaku": 20, "reply": 3}}

pl = li.build_payload("BVlimg00001", video_info, profs, stats, chart, profs[:2], attack,
                      comments, events, low_conf=4)
check("payload 计数与头部字段正确",
      pl["title"] == "测试视频" and pl["user_count"] == len(profs)
      and pl["spam_high"] == 1 and pl["cringe_count"] == 1
      and pl["comment_problem"] == 2 and pl["repeat_events"] == 2 and pl["low_conf"] == 4
      and pl["categories"][0] == ("引战阴阳", 2),
      "(user_count=%s spam_high=%s cringe=%s)" % (pl["user_count"], pl["spam_high"], pl["cringe_count"]))

check("用户卡截断到 LONG_IMAGE_USER_TOP_N 且保持传入顺序",
      len(pl["users"]) == min(len(profs), li.LONG_IMAGE_USER_TOP_N)
      and pl["users"][0]["name"] == "甲" and pl["users"][0]["ai"] == "深掘文本"
      and pl["users"][0]["cringe"] == 2,
      "(卡片 %d 张)" % len(pl["users"]))

check("问题弹幕榜 rank 连续且原文带类别与原因",
      [e["rank"] for e in pl["cringe_entries"]] == [1, 2]
      and "引战阴阳" in pl["cringe_entries"][0]["quote"]
      and pl["cringe_entries"][0]["right"] == "2 条",
      "(%r)" % (pl["cringe_entries"][0]["quote"][:30],))

check("争执焦点双榜与评论榜摘要装配",
      pl["attackers"][0]["right"] == "3 次" and "受害原评" in pl["victims"][0]["quote"]
      and pl["problem_comments"][0]["right"] == "❤9 ↩2",
      "(%s / %s)" % (pl["attackers"][0]["quote"], pl["victims"][0]["quote"][:24]))


# ---------- 2) 渲染与护栏 ----------
# 无中文字体环境（如未装 fonts-noto-cjk 的 CI runner）：必须给出可读错误而不是崩溃。
# 用缓存注入模拟，任何机器都能验证这条降级契约。
_orig_font = li._FONT_CACHE.get("font")
li._FONT_CACHE["font"] = (None, 0)
no_font_err = None
try:
    li.render_long_image(pl)
except RuntimeError as e:
    no_font_err = str(e)
finally:
    if _orig_font is None:
        li._FONT_CACHE.pop("font", None)
    else:
        li._FONT_CACHE["font"] = _orig_font
check("无中文字体时给出可读错误（含安装提示）而非崩溃",
      bool(no_font_err) and "字体" in no_font_err and "noto" in no_font_err.lower(),
      "(%s)" % (no_font_err or "未报错")[:44])

# 渲染类检查需要真实字体；无字体环境下明确跳过（CI 的 ubuntu 任务会装 fonts-noto-cjk）
HAS_FONT = bool(li._find_font()[0])
if not HAS_FONT:
    print("  （跳过渲染类检查：本机没有可用的中文字体；"
          "图形输出验证在装有 fonts-noto-cjk 的环境执行）")


if HAS_FONT:   # 无字体环境在上面已单独验证降级契约，这里跳过渲染本身
    img = li.render_long_image(pl)
    buf = io.BytesIO(); img.save(buf, format="PNG")
    check("渲染输出为竖版图且宽度符合配置",
          img.width == li.LONG_IMAGE_WIDTH and img.height > img.width,
          "(%dx%d)" % (img.width, img.height))
    check("导出字节是合法 PNG",
          buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n" and len(buf.getvalue()) > 10_000,
          "(%d KB)" % (len(buf.getvalue()) // 1024))

try:
    li.render_long_image(pl, width=li.LONG_IMAGE_MIN_WIDTH - 1)
    narrow = None
except ValueError as e:
    narrow = str(e)
check("宽度低于下限时拒绝出图（不产出畸形排版）", narrow is not None, "(%s)" % (narrow or "未报错")[:40])

if HAS_FONT:
    _orig_max = li.LONG_IMAGE_MAX_PIXELS
    li.LONG_IMAGE_MAX_PIXELS = 1000     # 人为压低上限，验证护栏生效
    try:
        li.render_long_image(pl)
        too_big = None
    except ValueError as e:
        too_big = str(e)
    finally:
        li.LONG_IMAGE_MAX_PIXELS = _orig_max
    check("超过像素上限时拒绝出图（防超大图打爆内存）",
          too_big is not None, "(%s)" % (too_big or "未报错")[:40])


# ---------- 3) 路由确实接上了 + 跟随隐藏信息开关 ----------
rules = {str(r) for r in web.app.url_map.iter_rules()}
check("路由 /api/video/<bvid>/longimage 已注册",
      "/api/video/<bvid>/longimage" in rules, "(%d 条路由)" % len(rules))

BV = "BVlimg00001"
storage.save_video_info(BV, {"bvid": BV, "title": "长图测试", "cid": 1, "duration": 60})
storage.save_sender(BV, "h1", 201, "高", "评论区验证", 6, ["滚"], "高", 8.0)
storage.save_user_data(201, "很长的一个昵称", 6, {"uid": 201},
                       {"uid": 201, "name": "很长的一个昵称", "level": 6,
                        "danmaku": {"count": 6, "spam_level": "高"},
                        "tags": ["硬核用户"]})
cl = web.app.test_client()
r = cl.get("/api/video/%s/longimage" % BV)
body = r.data
if HAS_FONT:
    ok_png = r.status_code == 200 and body[:8] == b"\x89PNG\r\n\x1a\n"
    check("已分析视频可导出长图（PNG 响应）", ok_png,
          "status=%s, %d KB" % (r.status_code, len(body) // 1024))
else:
    # 无字体环境：路由必须降级为可读 JSON 错误（不把异常栈甩给浏览器）。
    # 注意 jsonify 默认把中文转义成 \uXXXX，断言要解析后再看文案。
    try:
        err_msg = json.loads(body.decode("utf-8")).get("error", "")
    except Exception:
        err_msg = ""
    check("无中文字体时路由返回可读 JSON 错误（不崩页面）",
          r.status_code == 500 and "字体" in err_msg,
          "(status=%s, error=%r)" % (r.status_code, err_msg[:40]))
check("未分析视频返回 404", cl.get("/api/video/BVnotexist9/longimage").status_code == 404)

if HAS_FONT:
    cl_mask = web.app.test_client()
    cl_mask.set_cookie("mask", "1")
    masked = cl_mask.get("/api/video/%s/longimage" % BV).data
    check("开启「隐藏信息」后出图为遮蔽版（方案 A：分享不泄露昵称）",
          masked[:8] == b"\x89PNG\r\n\x1a\n" and masked != body,
          "(%d KB vs %d KB)" % (len(masked) // 1024, len(body) // 1024))

print("")
print("==== 分享长图导出: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
