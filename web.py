#!/usr/bin/env python3
"""
交互式 Web 数据报告 —— Flask 本地服务

静态单文件 HTML 报告已被本服务完全替换：页面骨架/用户卡片服务端渲染
（复用 report.py 渲染函数），弹幕浏览器走 JSON API（/api/video/<bvid>/danmaku）。

用法:
    python web.py                      # 监听 127.0.0.1:8001
    python web.py --port 9000          # 指定端口（跨平台，推荐）
    python web.py --stop --port 9000   # 停止该端口的后台实例

端口来源优先级：--port > PROFILER_PORT 环境变量 > 8001。
注意：`PROFILER_PORT=9000 python web.py` 是 **bash 语法**，Windows 的 cmd/PowerShell
不支持这种前置换值写法。Windows 下请用 --port，或按各自 shell 语法先设置再启动：
    cmd:        set PROFILER_PORT=9000   然后  python web.py
    PowerShell: $env:PROFILER_PORT="9000"; python web.py
"""
import sys
import os
import json
import glob
import io
import re
import argparse
import atexit
import functools
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from contextlib import closing

if sys.version_info < (3, 12):
    sys.exit(f"本项目需要 Python 3.12 或更高版本（当前 {sys.version.split()[0]}）："
             "代码使用了 Python 3.12 的嵌套 f-string 语法。")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from flask import Flask, Response, abort, jsonify, request, send_from_directory, redirect

from config import (REPORT_DIR, DATA_DIR, LLM_API_KEY, HISTORY_MAX_MONTHS, HISTORY_MAX_DAYS,
                     MAX_FOOTPRINT_VIDEOS, MAX_FOOTPRINT_DANMAKU_SAMPLES,
                     MAX_FOOTPRINT_COMMENT_SAMPLES,
                     HOT_COMMENT_MIN_REPLIES, HOT_COMMENT_MAX_SHOW,
                     USER_TIMELINE_MAX_VIDEOS, USER_TIMELINE_SAMPLES,
                     CROSS_VIDEO_MIN_VIDEOS, CROSS_VIDEO_MAX_USERS, DENSITY_BUCKETS,
                     COMMENT_HEAT_REPLY_WEIGHT, PROBLEM_COMMENT_TOP_N,
                     ATTACK_FOCUS_TOP_N, ATTACK_FOCUS_MAX_N, USER_CARD_URL, NAV_URL,
                     REPLY_TREE_MAX_DEPTH, WEB_JOB_MAX_KEPT, ANALYZE_MAX_TARGETS,
                     REPEAT_EVENT_TOP_N, BLOCKLIST_MAX_UIDS,
                     DENSITY_PEAK_MAX, DENSITY_PEAK_MIN_COUNT, DENSITY_PEAK_SAMPLE)
from auth import (load_cookie, verify_cookie, _try_refresh_cookie, _pop_local_fields,
                  REFRESH_OK, REFRESH_DEAD, REFRESH_NONE)
from api_client import BiliAPIClient
import config as _config
from storage import get_db, init_db, load_video_profile, ensure_video_profiles, load_comments, load_user_data
from storage import (load_senders, load_global_uid_map,
                     save_sender, save_user_data, load_video_info,
                     delete_video_data, toggle_false_positive, load_false_positives,
                     load_faces, load_face_cached_uids, save_face,
                     load_llm_cache, save_llm_cache)
from storage import (load_target_analysis, list_target_analyses,
                     has_normal_report, NORMAL_REPORT_PREDICATE, load_danmaku,
                     load_danmaku_problems, load_uid_verify_verdicts,
                     load_uid_candidates, load_uid_verify_times)
from target_report import render_target_report, render_target_list_page, page_number, render_space_assessment
from candidate_space import candidate_uids, cached_assessments
from crc_candidates import visible_candidate_uids, is_pure_crc, build_crc_groups, render_crc_groups, render_crc_group
from main import run_analysis
from uid_resolver import (resolve_sender, METHOD_CRC32_CRACK, build_evidence_maps,
                          resolution_is_current, resolution_record, VerificationBudget)
from comment import build_comment_uid_map
from combo_pool import build_pool
from user_collector import collect_user_data
from profile_analyzer import analyze_profile
from up_analyzer import fetch_up_wordcloud
from spam_detector import batch_detect_spam, detect_repeat_events, pool_distribution_from_rows
from cringe_detector import detect_density_peaks, load_cached_danmaku_problems
from llm_analyzer import LLMAnalyzer
from up_analyzer import _tokenize
from report import (REPORT_CSS, esc, js_json, generate_user_card, generate_summary_stats, uid_linkable,
                    generate_chart_data, generate_cringe_board, sort_profiles_by_risk,
                    up_wordcloud_data, PROBLEM_CATEGORY_COLORS, _category_chips,
                    mask_reply_prefix)
from long_image import build_payload, render_long_image

app = Flask(__name__)
PAGE_SIZE = 100  # 弹幕 API 默认/回退每页条数（可选 50/100/200，spec 3）
def _resolve_port(cli_port: int | None = None, env: dict | None = None) -> int:
    """解析监听端口：`--port` > `PROFILER_PORT` 环境变量 > 8001。

    Windows 的 cmd/PowerShell 不支持 `PROFILER_PORT=9000 python web.py` 这类 bash
    前置换值写法，故提供跨平台的 `--port`；环境变量保留兼容。
    非法/越界值一律告警并回退 8001（端口配错不该让服务起不来）。"""
    src = env if env is not None else os.environ
    raw = cli_port if cli_port is not None else src.get("PROFILER_PORT")
    try:
        port = int(raw) if raw not in (None, "") else 8001
    except (TypeError, ValueError):
        print(f"[Web] 警告: 端口 {raw!r} 不是合法整数，回退 8001")
        return 8001
    if not 1 <= port <= 65535:
        print(f"[Web] 警告: 端口 {port} 超出 1-65535，回退 8001")
        return 8001
    return port


# 监听端口（模块级默认：环境变量；__main__ 里会被 --port 覆盖）。
# _loopback_guard 的 Origin 白名单与 pidfile 路径都读这个全局，必须同源
_PORT = _resolve_port()
# 评论树递归深度上限 / job 表淘汰上限 / 单次分析目标上限已迁移至
# config.REPLY_TREE_MAX_DEPTH / config.WEB_JOB_MAX_KEPT / config.ANALYZE_MAX_TARGETS


@app.before_request
def _loopback_guard():
    """本机回环校验：写接口无防护时任意网页可借浏览器跨站调用本服务的删除/重跑等
    破坏性接口（CSRF），DNS rebinding（恶意域名解析到 127.0.0.1）还可整站读取画像数据。
    所有请求：Host 头必须是 127.0.0.1/localhost 系（封堵 DNS rebinding 读取面）；
    非 GET/HEAD/OPTIONS 请求：Origin 头存在时必须等于本服务回环源（封堵跨站写）。"""
    host = (request.host or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        abort(403)
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin")
    if origin is not None and origin not in (f"http://127.0.0.1:{_PORT}",
                                             f"http://localhost:{_PORT}"):
        abort(403)
    return None


def _db_error_page() -> str:
    """数据库锁定/异常时的 503 友好页（对齐 api_danmaku 的降级口径：提示而非白屏）"""
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="referrer" content="no-referrer">
<title>503 - B站弹幕用户画像分析</title>
<style>{REPORT_CSS}</style>
<link rel="stylesheet" href="/static/report.css">
</head>
<body>
<div class="container">
    <div class="header"><h1>503</h1><div class="meta">数据库暂不可用</div></div>
    <div class="nf-card">
        <p>数据库正被占用或查询失败（可能有分析任务正在写入），请稍后刷新重试。</p>
        <p><a class="filter-btn" href="/">返回首页</a></p>
    </div>
</div>
</body>
</html>'''


def _db_guard(view):
    """sqlite3.Error → 503 友好页（数据库锁定多发生在分析任务写入期，提示刷新重试）"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except sqlite3.Error as e:
            print(f"[Web] 数据库查询失败（{view.__name__}）: {e}")
            return _db_error_page(), 503
    return wrapper

# 报告页整页 HTML 内存缓存：(bvid, 遮蔽开关) → (数据指纹, HTML, 渲染时刻)。
# 避免每次刷新重跑 _load_profiles/_attach_other_videos 逐用户查询串；
# job 完成/删除/误报标记时主动失效，指纹比对兜底外部进程写入。
# 渲染时刻超过 1 小时（或更短的 UID 存在性核验 TTL）也当未命中，避免过期 UID 仍可点击。
_PAGE_CACHE: dict[tuple[str, bool], tuple] = {}
_PAGE_CACHE_LOCK = threading.Lock()


def _invalidate_page_cache(bvid: str):
    """使指定视频的报告页缓存失效（job 完成/删除时调用）；
    key 为 (bvid, 遮蔽开关) 元组，开/关两个形态一并清除"""
    with _PAGE_CACHE_LOCK:
        _PAGE_CACHE.pop((bvid, True), None)
        _PAGE_CACHE.pop((bvid, False), None)


def _page_fingerprint(bvid: str) -> tuple:
    """用数据库修订号检测外部写入，涵盖画像、UID、热度及同条数更新。"""
    with closing(get_db()) as conn:
        return tuple(conn.execute("SELECT scope, revision FROM data_revision WHERE scope IN (?, '*') "
                                  "ORDER BY scope", (bvid,)).fetchall())


def _page_cache_max_age() -> float:
    """整页缓存最长寿命：1 小时，且不超过 UID 存在性核验 TTL（更短则提前重渲染）。"""
    try:
        verify_ttl = float(getattr(_config, "UID_VERIFY_EXISTS_TTL", 3600))
    except (TypeError, ValueError):
        verify_ttl = 3600.0
    return min(3600.0, max(0.0, verify_ttl))


def _page_cache_lookup(bvid: str, mask: bool, page_fp: tuple) -> str | None:
    """命中条件：同一遮蔽版本、数据修订号未变、且未超过缓存寿命。否则当未命中。"""
    with _PAGE_CACHE_LOCK:
        cached = _PAGE_CACHE.get((bvid, mask))
    if not cached or cached[0] != page_fp or len(cached) < 3:
        return None
    try:
        rendered_at = float(cached[2])
    except (TypeError, ValueError):
        return None
    if time.time() - rendered_at >= _page_cache_max_age():
        return None
    return cached[1]


# ========== 手动勾选分析 job（spec B；状态存内存 dict，服务重启即失效——spec 已接受） ==========

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
_CLIENT_LOCK = threading.Lock()
_client = None          # 懒加载的 BiliAPIClient（其内部线程安全，限速全局共享）
_client_failed = False  # Cookie 校验失败过一次即记住，后续 POST 直接 503，不重复打验证请求


class CookieInvalidError(Exception):
    """Cookie 缺失/失效：POST 返回 503，job 启动即整体终止"""


def _get_client(*, mark_failed: bool = True) -> BiliAPIClient:
    """懒加载创建带登录态的 BiliAPIClient（auth.load_cookie 读 data/cookie.json +
    verify_cookie 联网校验）；失败抛 CookieInvalidError（携带 503 文案），由路由按 503 处理。

    verify_cookie 在网络异常时也返回 False，_try_refresh_cookie 的 REFRESH_RETRY
    在布尔上下文里同样为假。这两类都不得置粘性 _client_failed，下次请求再试。
    只有 REFRESH_DEAD，或确实没有可用登录态（无 SESSDATA，或 REFRESH_NONE 且验证失败）
    才置粘性标记。mark_failed=False 供启动热身使用：热身失败一律不置粘性标记。"""
    global _client, _client_failed
    with _CLIENT_LOCK:
        if _client is not None:
            return _client
        if _client_failed:
            raise CookieInvalidError("Cookie 失效，请先运行 python login.py")
        cookie_dict = load_cookie()
        if not cookie_dict or not cookie_dict.get("SESSDATA"):
            if mark_failed:
                _client_failed = True
            raise CookieInvalidError("Cookie 失效，请先运行 python login.py")
        # 本地伪字段必须弹出，不能进 session；时间戳留给续期节流
        refresh_token, stamp = _pop_local_fields(cookie_dict)
        client = BiliAPIClient()
        client.update_cookies(cookie_dict)
        if refresh_token:
            client._refresh_token = refresh_token
        if stamp:
            client._refreshed_at = stamp
        if verify_cookie(client):
            _client = client
            return _client
        # 验证失败：有令牌才尝试刷新。网络失败是 REFRESH_RETRY，不得当成令牌作废。
        result = _try_refresh_cookie(client) if refresh_token else REFRESH_NONE
        if result == REFRESH_OK and verify_cookie(client):
            _client = client
            return _client
        sticky = result == REFRESH_DEAD or result == REFRESH_NONE
        if sticky and mark_failed:
            _client_failed = True
        if result == REFRESH_DEAD:
            raise CookieInvalidError("Cookie 失效且刷新失败，请先运行 python login.py")
        if result == REFRESH_NONE:
            raise CookieInvalidError("Cookie 失效，请先运行 python login.py")
        raise CookieInvalidError("登录态暂时无法确认（网络异常），请稍后重试")


def _warm_client():
    """启动热身：提前拉 buvid3/bili_ticket/WBI。失败（含网络抖动）不得置粘性失效标记。"""
    try:
        _get_client(mark_failed=False).get(NAV_URL, immediate=True)
    except Exception:
        pass


# 账号×IP 组合池懒加载单例：小号池发现/（配 SUB_URLS 时）IP 池探测与 mihomo 核心
# 拉起只做一次，避免每个手动分析 job 重复探测；池内组合故障时本身会自动降级
_POOL = None
_POOL_LOCK = threading.Lock()


def _get_pool(client):
    """模块级懒加载组合池单例（锁内双重检查后建池缓存）"""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = build_pool(client)
    return _POOL


def _sender_danmaku_stats(bvid: str, mid_hash: str) -> dict:
    """从 danmaku 表重建该发送者的弹幕统计（web 端无内存态 sender_groups）。

    video_pages（与 contents/video_times 同下标）与 multi_page 一并带出：多分P 视频
    手动分析出的画像同样要能标注「P{n} mm:ss」，口径与阶段6 一致。"""
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT content, time, timestamp, page FROM danmaku "
            "WHERE bvid = ? AND mid_hash = ? ORDER BY page, time",
            (bvid, mid_hash)).fetchall()
    return {
        "count": len(rows),
        "contents": [r["content"] for r in rows],
        "timestamps": [r["timestamp"] for r in rows],
        "video_times": [r["time"] for r in rows],
        "video_pages": [int(r["page"] or 1) for r in rows],
        "multi_page": len(_load_page_meta(bvid)) > 1,
    }


def _run_analysis_job(job_id: str, bvid: str, mid_hashes: list[str]):
    """后台 job：每个 mid_hash 串行 解析→强制采集→规则画像→LLM深掘→落库（串行即天然限流）。

    错误处理（spec 5）：单发送者失败记 errors（{mid_hash, error} 结构，供前端透出与重试）继续；Cookie 失效 job 整体终止并标记。
    写库全部走 storage 的 save_*（各自短事务），不持长连接。
    """
    def update(**kw):
        with JOBS_LOCK:
            JOBS[job_id].update(kw)

    def add_error(msg, mid_hash=None):
        # 失败明细结构化：mid_hash 供前端"重试失败项"按钮重新提交，msg 为人类可读摘要
        with JOBS_LOCK:
            JOBS[job_id]["errors"].append({"mid_hash": mid_hash, "error": msg})
        print(f"[Job {job_id}] 失败: {mid_hash or '-'} {msg}")

    def add_result(uid):
        with JOBS_LOCK:
            JOBS[job_id]["results"].append(uid)

    try:
        client = _get_client()
    except CookieInvalidError as e:
        add_error(f"{e}（job 已终止）")
        update(finished=True, current="")
        return
    except Exception as e:
        add_error(f"创建 API 客户端失败: {e}（job 已终止）")
        update(finished=True, current="")
        return

    # 账号×IP 组合池（同主流程：风控换号+切节点，故障自动降级）；建池失败同 Cookie 失效语义终止 job
    try:
        pool = _get_pool(client)
    except Exception as e:
        add_error(f"构建组合池失败: {e}（job 已终止）")
        update(finished=True, current="")
        return

    cached = {r["mid_hash"]: r for r in load_senders(bvid)}
    video_info = load_video_info(bvid) or {}
    # 评论只读一次，按 uid 分组供循环内取「TA 的评论」；不要在每个 mid_hash 里再 load_comments
    all_comments = load_comments(bvid)
    plain_uid_map, method_map = build_evidence_maps(bvid,
        [("评论区验证", build_comment_uid_map(all_comments))], load_global_uid_map())
    comments_by_uid: dict[int, list] = {}
    for c in all_comments:
        comments_by_uid.setdefault(c["uid"], []).append(c)
    # LLM 深掘器：未配置 LLM_API_KEY 时跳过（spec 3.3）；初始化失败也只跳过深掘
    analyzer = None
    if LLM_API_KEY:
        try:
            analyzer = LLMAnalyzer()
        except Exception as e:
            print(f"[Job {job_id}] 警告: LLM 初始化失败（{e}），本次 job 跳过深掘")

    verification_budget = VerificationBudget()
    for i, mid_hash in enumerate(mid_hashes, 1):
        update(current=f"[{i}/{len(mid_hashes)}] {mid_hash}")
        try:
            row = cached.get(mid_hash)
            uid = row["uid"] if row and resolution_is_current(row, mid_hash, plain_uid_map, method_map) else None
            # 只有本视频已有画像才跳过；其它视频的基础资料可复用但不能代替本视频画像。
            if uid is not None and load_video_profile(bvid, uid):
                add_result(uid)
                update(done=i)
                continue

            stats = _sender_danmaku_stats(bvid, mid_hash)
            if uid is None:
                # 1. UID 解析：全局库明文命中优先，CRC32 彩虹表破解兜底；失败记 errors 继续
                verdict = resolve_sender(mid_hash, stats["contents"], plain_uid_map, pool, method_map=method_map,
                                         verification_budget=verification_budget)
                uid, confidence, method, _info, collision_risk, _candidates = verdict
                record = resolution_record(mid_hash, verdict, plain_uid_map, method_map)
                save_sender(bvid, mid_hash, uid, confidence, method, stats["count"], stats["contents"],
                            (row["spam_level"] if row else None) or "低",
                            (row["spam_score"] if row else None) or 0.0, resolution=record)
                if uid is None:
                    add_error(f"UID 解析失败（{method}）", mid_hash)
                    update(done=i)
                    continue
            else:
                collision_risk = row["resolution"].get("collision_risk", True)

            # 2. 强制采集（无视置信度，含"低"/碰撞）
            cached_user = load_user_data(uid)
            user_data = cached_user[0] if cached_user else collect_user_data(uid, pool)
            if "error" in user_data:
                add_error(f"UID:{uid} 采集失败 {user_data['error']}", mid_hash)
                update(done=i)
                continue
            user_data = dict(user_data)
            user_data.pop("ip_location", None)
            mine_comments = comments_by_uid.get(uid, [])
            location = next((c["location"] for c in mine_comments if c.get("location")), "")
            if location:
                user_data["ip_location"] = location

            # 3. 规则画像：刷屏统计从 danmaku 表现算（web 端无内存态 spam_results）
            spam = batch_detect_spam({mid_hash: stats}).get(mid_hash, {})
            profile = analyze_profile(user_data,
                                      {"count": stats["count"], "contents": stats["contents"],
                                       "video_times": stats["video_times"],
                                       "video_pages": stats["video_pages"], "multi_page": stats["multi_page"]},
                                      spam)
            profile["collision_risk"] = collision_risk
            profile["comments"] = sorted(mine_comments,
                                         key=lambda c: -c["like"])[:10]
            profile["cringe"] = {}     # 手动分析不重跑问题弹幕 LLM 检测

            # 4. LLM 深掘（top_k=1 单人单调用，llm_cache 命中零 token；失败只影响深掘）
            if analyzer is not None:
                try:
                    deep = analyzer.analyze_deep([profile], video_info, top_k=1)
                    if uid in deep:
                        profile["ai_deep"] = deep[uid]
                except Exception as e:
                    print(f"[Job {job_id}] 警告: UID:{uid} LLM 深掘失败（{e}），仅跳过深掘")

            save_user_data(uid, user_data.get("name", ""), user_data.get("level", 0),
                           user_data, profile, bvid=bvid)
            add_result(uid)
            update(done=i)
            print(f"[Job {job_id}] [{i}/{len(mid_hashes)}] {mid_hash} → UID:{uid} 完成")
        except Exception as e:
            add_error(str(e), mid_hash)
            update(done=i)

    update(finished=True, current="")
    _invalidate_page_cache(bvid)   # 该视频画像已变化，报告页缓存失效
    print(f"[Job {job_id}] 完成: 成功 {len(JOBS[job_id]['results'])}/{len(mid_hashes)}")


def _run_analysis_job_safe(job_id: str, bvid: str, mid_hashes: list[str]):
    """_run_analysis_job 的线程入口包装：兜底接住循环外异常（load_senders 等
    库读取不在 per-item try 内，外部 run.py --force 删表重建/库损坏时会抛
    sqlite3.Error；阶段函数还会 raise SystemExit——继承 BaseException，常规
    except Exception 接不住）。不兜底的话 job 线程崩溃后 finished 永为
    False，该视频的删除/重新分析因 _has_running_job 恒真而永久 409，只能重启解锁。"""
    try:
        _run_analysis_job(job_id, bvid, mid_hashes)
    except SystemExit as e:
        with JOBS_LOCK:
            JOBS[job_id]["errors"].append({"mid_hash": None, "error": f"分析被终止: {e}"})
        print(f"[Job {job_id}] 分析被终止（SystemExit: {e}）")
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["errors"].append({"mid_hash": None, "error": f"job 异常终止: {type(e).__name__}: {e}"})
        print(f"[Job {job_id}] 异常终止: {type(e).__name__}: {e}")
    finally:
        with JOBS_LOCK:
            JOBS[job_id]["finished"] = True
            JOBS[job_id]["current"] = ""
        _invalidate_page_cache(bvid)   # 异常路径的部分结果也已落库，页缓存必须失效


# ========== 数据加载辅助 ==========

def _load_video_row(bvid: str):
    """videos 表整行；不存在返回 None"""
    with closing(get_db()) as conn:
        return conn.execute("SELECT * FROM videos WHERE bvid = ?", (bvid,)).fetchone()


def _load_profiles(bvid: str) -> list[dict]:
    """该视频已解析发送者的画像（video_profiles 按 bvid+uid 隔离，同 UID 去重）。

    附带注入渲染期键（不落库）：resolve_method/resolve_confidence 来自 senders 表
    （卡片解析徽标 tooltip），collected_at 来自 users 表（基础信息采集时间），
    _mid_hash 来自 senders 表（刷屏判定误报按钮的 target），
    school 在旧缓存画像缺省时从 data_json 回退提取（毕业院校徽标）。
    GROUP BY u.uid 下 method/confidence/mid_hash 取该 uid 任一行（同 uid 多 mid_hash 极少见，可接受）。"""
    ensure_video_profiles(bvid)
    with closing(get_db()) as conn:
        rows = conn.execute('''
            SELECT p.profile_json, u.data_json, s.method, s.confidence, u.collected_at, s.mid_hash, s.resolution_json
            FROM senders s JOIN users u ON u.uid = s.uid
            JOIN video_profiles p ON p.bvid=s.bvid AND p.uid=s.uid
            WHERE s.bvid = ? AND s.uid IS NOT NULL
            GROUP BY u.uid
        ''', (bvid,)).fetchall()
    profiles = []
    for r in rows:
        try:
            p = json.loads(r["profile_json"])
        except Exception:
            continue
        p["resolve_method"] = r["method"] or ""
        p["resolve_confidence"] = r["confidence"] or ""
        resolution = json.loads(r["resolution_json"] or "{}")
        p["_resolution"] = resolution      # 供 report.uid_linkable 判定该 UID 能否给跳转
        p["collision_risk"] = resolution.get("collision_risk", r["method"] == METHOD_CRC32_CRACK)
        p["resolve_reason"] = resolution.get("reason", "")
        p["collected_at"] = r["collected_at"] or ""
        p["_mid_hash"] = r["mid_hash"] or ""
        # 隐藏信息（mask Cookie）：昵称只留末字、另注入遮蔽 UID 显示串
        # （uid 原值保留：链接/锚点/逻辑比较都靠它，遮蔽只影响可见文本）
        if _mask_on():
            p["name"] = _mask_name(p.get("name"))
            p["_uid_disp"] = _mask_uid(p.get("uid"))
        # 毕业院校回退：本特性之前的缓存画像无 school 键，从采集原始数据 data_json 补
        if not p.get("school"):
            try:
                p["school"] = json.loads(r["data_json"]).get("school", "")
            except Exception:
                pass
        # 关注名单 uid 回退：懒加载词云特性之前的缓存画像 all_followings_raw 无 uid，
        # 从 data_json.followings 按名字补（补不上则该 chip 退化为纯展示）
        raw_fol = p.get("all_followings_raw") or []
        if raw_fol and not raw_fol[0].get("uid"):
            try:
                fol = json.loads(r["data_json"]).get("followings", [])
                uid_by_name = {f.get("name"): f.get("uid") for f in fol if f.get("name")}
                for item in raw_fol:
                    if not item.get("uid") and uid_by_name.get(item["name"]):
                        item["uid"] = uid_by_name[item["name"]]
            except Exception:
                pass
        profiles.append(p)
    fp_cmt = {t for k, t in load_false_positives(bvid) if k == "cmt"}
    problem_by_uid = {}
    for c in load_comments(bvid):
        if c["problem"] and str(c["rpid"]) not in fp_cmt:
            problem_by_uid.setdefault(c["uid"], []).append(c)
    for p in profiles:
        kept = problem_by_uid.get(p.get("uid"), [])
        p["comment_problem"] = {"hits": len(kept),
            "max_severity": max((c.get("problem_severity") or 1 for c in kept), default=0),
            "categories": list(dict.fromkeys(c["problem"] for c in kept))}
        for c in p.get("comments") or []:
            if str(c.get("rpid")) in fp_cmt:
                c["problem"] = ""
    # 样本分P 标注回填（渲染期，不落库）：多分P 视频才需要，见 _fill_sample_pages
    if len(_load_page_meta(bvid)) > 1:
        _fill_sample_pages(bvid, profiles)
    return profiles


def _attach_other_videos(bvid: str, profiles: list[dict]):
    """跨视频足迹：把「该用户在其他已分析视频中的出现、弹幕与评论」批量注入每个
    profile 的 other_videos 渲染期键（不落库），供 report.generate_user_card 渲染。

    数据源：senders 跨视频关联（uid 在其它 bvid 的行，JOIN videos 取标题）+
    danmaku 表弹幕样本 + comments 表评论样本。旧版本分析（danmaku/comments 无行）
    降级为只显示计数或「未留存」提示。

    批量策略：全部 uid 分块 IN 查询（SQLite 变量上限防护）只跑一轮，
    每 uid 按活跃度（弹幕数+评论数）取前 MAX_FOOTPRINT_VIDEOS 个视频，
    其余计 more；样本查询只针对保留的视频（每卡片至多 5×5 行，量级可控）。
    """
    uids: list[int] = []
    for p in profiles:
        try:
            u = int(p.get("uid"))
        except (TypeError, ValueError):
            continue
        if u and u not in uids:
            uids.append(u)
    if not uids:
        return

    by_uid: dict[int, dict[str, dict]] = {}      # uid → bvid → {mid_hashes, danmaku_count, title}
    comment_counts: dict[tuple[int, str], int] = {}
    with closing(get_db()) as conn:
        for i in range(0, len(uids), 500):       # 分块规避 SQLite 变量数上限
            chunk = uids[i:i + 500]
            qmarks = ",".join("?" * len(chunk))
            for r in conn.execute(f'''
                    SELECT s.uid, s.bvid, s.mid_hash, s.danmaku_count, v.title
                    FROM senders s JOIN videos v ON v.bvid = s.bvid
                    WHERE s.uid IN ({qmarks}) AND s.bvid != ?
            ''', (*chunk, bvid)).fetchall():
                ent = by_uid.setdefault(r["uid"], {}).setdefault(r["bvid"], {
                    "mid_hashes": [], "danmaku_count": r["danmaku_count"] or 0,
                    "title": r["title"] or ""})
                if r["mid_hash"] not in ent["mid_hashes"]:
                    ent["mid_hashes"].append(r["mid_hash"])
            for row in conn.execute(f'''
                    SELECT uid, bvid, COUNT(*) AS cnt FROM comments
                    WHERE uid IN ({qmarks}) AND bvid != ?
                    GROUP BY uid, bvid
            ''', (*chunk, bvid)).fetchall():
                comment_counts[(row["uid"], row["bvid"])] = row["cnt"]

        for p in profiles:
            try:
                uid = int(p.get("uid"))
            except (TypeError, ValueError):
                continue
            vids = by_uid.get(uid)
            if not vids:
                continue
            ranked = sorted(
                ((vb, ent["danmaku_count"] + comment_counts.get((uid, vb), 0), ent)
                 for vb, ent in vids.items()),
                key=lambda x: x[1], reverse=True)
            kept, rest = ranked[:MAX_FOOTPRINT_VIDEOS], ranked[MAX_FOOTPRINT_VIDEOS:]
            items = []
            for vb, _score, ent in kept:
                # 该视频 danmaku 表完全无行 → 旧版本分析（弹幕/评论均未留存），
                # 前端据此显示「未留存」而非「无」，语义更准确
                legacy = conn.execute(
                    "SELECT 1 FROM danmaku WHERE bvid = ? LIMIT 1", (vb,)).fetchone() is None
                dm_samples: list[str] = []
                for mh in ent["mid_hashes"]:
                    dm_samples.extend(r["content"] for r in conn.execute(
                        "SELECT content FROM danmaku WHERE bvid = ? AND mid_hash = ? "
                        "ORDER BY timestamp DESC LIMIT ?",
                        (vb, mh, MAX_FOOTPRINT_DANMAKU_SAMPLES)).fetchall())
                    if len(dm_samples) >= MAX_FOOTPRINT_DANMAKU_SAMPLES:
                        break
                cmt_samples = [
                    {"content": r["content"], "ctime": r["ctime"],
                     "like": r["like"], "is_sub": r["is_sub"], "problem": r["problem"]}
                    for r in conn.execute(
                        "SELECT content, ctime, like, is_sub, problem FROM comments "
                        "WHERE bvid = ? AND uid = ? ORDER BY like DESC, ctime DESC LIMIT ?",
                        (vb, uid, MAX_FOOTPRINT_COMMENT_SAMPLES)).fetchall()]
                items.append({
                    "bvid": vb,
                    "title": ent["title"] or vb,
                    "danmaku_count": ent["danmaku_count"],
                    "danmaku_samples": dm_samples[:MAX_FOOTPRINT_DANMAKU_SAMPLES],
                    "comment_count": comment_counts.get((uid, vb), 0),
                    "comment_samples": cmt_samples,
                    "legacy": legacy,
                })
            p["other_videos"] = {"items": items, "more": len(rest)}


def _sender_meta(bvid: str) -> dict:
    """发送者联查（spec 5）：mid_hash → {uid, name, spam_level, categories}。
    uid/name/spam_level 来自 senders LEFT JOIN users；categories 从 video_profiles.profile_json
    的 cringe 字段 Python 侧解析（非 SQL），并按内容扣除已标记误报的弹幕
    （与 video_page 的 _apply_danmaku_fp 同口径——UI 文案承诺「标记后不计入聚合」）；
    spam_level 命中刷屏误报（kind=spam，发送者粒度）时降级为 "低"（弹幕浏览器面板同口径）。
    senders 无行的 mid_hash 不在此表 → 未分析。
    连接用 closing 保证异常路径也关闭（参照 storage.py 模式）。"""
    ensure_video_profiles(bvid)
    fp = load_false_positives(bvid)
    fp_dm = {t for k, t in fp if k == "dm"}
    fp_spam = {t for k, t in fp if k == "spam"}
    with closing(get_db()) as conn:
        rows = conn.execute('''
        SELECT s.mid_hash, s.uid, s.spam_level, u.name, p.profile_json
        FROM senders s LEFT JOIN users u ON u.uid = s.uid
            LEFT JOIN video_profiles p ON p.bvid=s.bvid AND p.uid=s.uid
        WHERE s.bvid = ?
    ''', (bvid,)).fetchall()
    meta = {}
    for r in rows:
        categories = []
        if r["profile_json"]:
            try:
                cringe = json.loads(r["profile_json"]).get("cringe", {})
                categories = cringe.get("categories", []) or []
                if fp_dm and categories:
                    # 误报按内容去重扣除：仅当该画像 items/examples 命中误报内容时重算类别
                    items = cringe.get("items") or cringe.get("examples") or []
                    kept = [it for it in items if it.get("content") not in fp_dm]
                    if len(kept) != len(items):
                        categories = []
                        for it in kept:
                            if it.get("category") and it["category"] not in categories:
                                categories.append(it["category"])
            except Exception:
                categories = []
        spam_level = r["spam_level"]
        if fp_spam and r["mid_hash"] in fp_spam:
            spam_level = "低"   # 刷屏判定人工误报：展示层降级（与用户卡片/统计图同口径）
        # 隐藏信息（mask Cookie）：展示名只留末字。raw_name 只留在服务端供搜索比对，
        # 不得放进响应 JSON（否则遮蔽形同虚设）。
        raw_name = r["name"]
        name = _mask_name(raw_name) if _mask_on() else raw_name
        meta[r["mid_hash"]] = {"uid": r["uid"], "name": name, "raw_name": raw_name,
                               "spam_level": spam_level, "categories": categories}
    return meta


def _danmaku_panel_stats(bvid: str) -> dict:
    """弹幕浏览器统计面板（spec 4）：总弹幕数/合并后行数/独立发送者数/已解析发送者数
    + 问题弹幕类别分布 + 发送者弹幕数 Top10。无弹幕数据时只返回 {"total": 0}。"""
    with closing(get_db()) as conn:
        total = conn.execute("SELECT COUNT(*) FROM danmaku WHERE bvid = ?", (bvid,)).fetchone()[0]
        if total == 0:
            return {"total": 0}
        merged = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM danmaku WHERE bvid = ? GROUP BY mid_hash, content)",
            (bvid,)).fetchone()[0]
        senders_total = conn.execute(
            "SELECT COUNT(DISTINCT mid_hash) FROM danmaku WHERE bvid = ?", (bvid,)).fetchone()[0]
        # 与解析质量区块同口径：cmt: 合成键是问题评论作者，不是弹幕发送者
        resolved = conn.execute(
            "SELECT COUNT(*) FROM senders WHERE bvid = ? AND uid IS NOT NULL "
            "AND mid_hash NOT LIKE 'cmt:%'", (bvid,)).fetchone()[0]
        top10 = conn.execute('''
            SELECT mid_hash, COUNT(*) AS cnt FROM danmaku WHERE bvid = ?
            GROUP BY mid_hash ORDER BY cnt DESC LIMIT 10
        ''', (bvid,)).fetchall()
    meta = _sender_meta(bvid)
    cat_counts = {}
    for m in meta.values():
        for c in m["categories"]:
            cat_counts[c] = cat_counts.get(c, 0) + 1
    return {
        "total": total, "merged": merged, "senders": senders_total, "resolved": resolved,
        "categories": cat_counts,
        "top10": [{"mid_hash": r["mid_hash"], "count": r["cnt"],
                   "name": (meta.get(r["mid_hash"]) or {}).get("name") or r["mid_hash"]}
                  for r in top10],
    }


# ========== B站屏蔽列表导出（概览页操作条：导入B站后不再看到这些发送者的弹幕） ==========

_BLOCKLIST_CRITERIA = {"cringe", "spam", "cmt"}   # 合法导出标准（cringe=问题弹幕 / spam=刷屏 / cmt=问题评论）


def _build_blocklist(bvid: str, crit: set, max_uids: int,
                     include_lowconf: bool = False) -> dict:
    """构建B站播放器「弹幕屏蔽列表」可导入的用户屏蔽条目（uid → type=2 条目）。

    选人标准（crit 为 _BLOCKLIST_CRITERIA 的子集，可多选）：
      - cringe：问题弹幕发送者（画像 cringe 聚合非空；dm 误报按内容扣除后为空则不算）
      - spam：高/中风险刷屏发送者（spam 误报标记按低风险口径排除）
      - cmt：问题评论作者（comments.problem 非空，cmt 误报按 rpid 扣除）
    排序按烦人程度：问题严重度 > 刷屏分 > 弹幕数；截断至 max_uids（B站屏蔽列表容量约 200）。

    低置信度（confidence="低"，CRC32 反查可能误识别）默认排除——屏蔽错人比漏屏蔽更糟；
    问题评论命中的 uid 是评论区明文 UID，天然可信，不受该排除影响。
    未解析发送者（uid 为空）无 UID 可写，计入 skipped_unresolved 供 UI 提示。

    条目格式对齐播放器导出口径（面板右键导入 json）：type=2 用户屏蔽、filter=uid 字符串、
    opened=true、id 为序号（导入/同步后由服务端重新分配）、comment 为来源摘要。

    Returns: {"entries": [条目], "matched": 命中总数, "skipped_unresolved": int,
              "skipped_lowconf": int}
    """
    ensure_video_profiles(bvid)
    fp = load_false_positives(bvid)
    fp_dm = {t for k, t in fp if k == "dm"}
    fp_spam = {t for k, t in fp if k == "spam"}
    fp_cmt = {t for k, t in fp if k == "cmt"}

    info: dict[int, dict] = {}        # uid -> {sev, score, count, notes, lowconf, plain}
    skipped_unresolved = 0

    def _upsert(uid: int, sev: int = 0, score: float = 0.0, count: int = 0,
                note: str = "", lowconf: bool = False, plain: bool = False):
        ent = info.setdefault(uid, {"sev": 0, "score": 0.0, "count": 0,
                                    "notes": [], "lowconf": False, "plain": False})
        ent["sev"] = max(ent["sev"], sev)
        ent["score"] = max(ent["score"], score or 0.0)
        ent["count"] = max(ent["count"], count)
        if note and note not in ent["notes"]:
            ent["notes"].append(note)
        # lowconf 只增不清（多行合并时保守）；plain（明文评论来源）可抵消 lowconf
        ent["lowconf"] = ent["lowconf"] or lowconf
        ent["plain"] = ent["plain"] or plain

    with closing(get_db()) as conn:
        # 1) senders 行：spam 维度 + 画像 cringe/评论直引严重度 + 置信度
        rows = conn.execute('''
            SELECT s.mid_hash, s.uid, s.confidence, s.spam_level, s.spam_score,
                   s.danmaku_count, p.profile_json
            FROM senders s LEFT JOIN users u ON u.uid = s.uid
        LEFT JOIN video_profiles p ON p.bvid=s.bvid AND p.uid=s.uid
            WHERE s.bvid = ?
        ''', (bvid,)).fetchall()

        # 2) 问题评论作者（评论区明文 UID，无画像也计入；cmt 误报按 rpid 扣除）
        cmt_counts: dict[int, int] = {}
        if "cmt" in crit:
            sql = ("SELECT uid, COUNT(*) AS n, MAX(problem_severity) AS sev FROM comments "
                   "WHERE bvid = ? AND problem != ''")
            params: list = [bvid]
            if fp_cmt:
                marks = sorted(fp_cmt)
                sql += " AND CAST(rpid AS TEXT) NOT IN (%s)" % ",".join("?" * len(marks))
                params += marks
            sql += " GROUP BY uid"
            cmt_rows = conn.execute(sql, params).fetchall()
            cmt_counts = {r["uid"]: r["n"] for r in cmt_rows}
            cmt_severity = {r["uid"]: r["sev"] or 1 for r in cmt_rows}

    for r in rows:
        uid = r["uid"]
        dm_count = r["danmaku_count"] or 0
        lowconf = (r["confidence"] == "低")
        # cringe：画像聚合（dm 误报按内容扣除，与 _sender_meta 同口径）
        cringe_hit, cringe_sev, cringe_cats = False, 0, []
        if "cringe" in crit and r["profile_json"]:
            try:
                cr = json.loads(r["profile_json"]).get("cringe") or {}
                items = cr.get("items") or cr.get("examples") or []
                kept = [it for it in items if it.get("content") not in fp_dm]
                if kept:
                    cringe_hit = True
                    cringe_sev = max((it.get("severity", 1) for it in kept), default=1)
                    cringe_cats = list(dict.fromkeys(
                        it.get("category") for it in kept if it.get("category")))
            except Exception:
                cringe_hit = False
        # spam：高/中风险（spam 误报标记降级为低，与展示层同口径）
        spam_hit = ("spam" in crit and r["spam_level"] in ("高", "中")
                    and r["mid_hash"] not in fp_spam)

        if uid is None:
            # 未解析：仅 spam 维度可判（cringe 依赖画像、画像依赖 uid），计入提示数
            if spam_hit:
                skipped_unresolved += 1
            continue
        if cringe_hit:
            _upsert(uid, sev=cringe_sev, count=dm_count,
                    note="问题弹幕:" + "/".join(cringe_cats[:2]),
                    lowconf=lowconf)
        if spam_hit:
            _upsert(uid, score=r["spam_score"] or 0.0, count=dm_count,
                    note=f"刷屏{r['spam_level']}风险{r['spam_score'] or 0:.1f}分",
                    lowconf=lowconf)
        if not cringe_hit and not spam_hit and uid not in cmt_counts:
            # 与屏蔽无关的发送者也登记 count：同 uid 命中评论维度时弹幕数参与排序
            info.setdefault(uid, {"sev": 0, "score": 0.0, "count": dm_count,
                                  "notes": [], "lowconf": lowconf, "plain": False})

    for uid, n in cmt_counts.items():
        _upsert(uid, sev=cmt_severity[uid], note=f"问题评论{n}条", plain=True)

    # 筛选命中者：至少有一条 note（= 至少命中一项导出标准）
    matched_uids = [uid for uid, ent in info.items() if ent["notes"]]
    # 低置信度排除：仅无明文来源佐证的 uid（CRC32 反查可能误识别，屏蔽错人不可逆）
    if include_lowconf:
        excluded_lowconf: set[int] = set()
    else:
        excluded_lowconf = {uid for uid in matched_uids
                            if info[uid]["lowconf"] and not info[uid]["plain"]}
    selected = [uid for uid in matched_uids if uid not in excluded_lowconf]
    # 烦人程度排序：严重度 > 刷屏分 > 弹幕数（同分按 uid 稳定序）
    selected.sort(key=lambda u: (-info[u]["sev"], -info[u]["score"],
                                 -info[u]["count"], u))
    selected = selected[:max_uids]

    entries = []
    for i, uid in enumerate(selected, 1):
        entries.append({
            "type": 2, "filter": str(uid), "opened": True, "id": i,
            "comment": "；".join(info[uid]["notes"])[:60],
        })
    # detail：uid → 排序用计分（跨视频聚合复用；单视频路由不消费）
    detail = {uid: {"sev": info[uid]["sev"], "score": info[uid]["score"],
                    "count": info[uid]["count"]} for uid in matched_uids}
    return {"entries": entries, "matched": len(matched_uids),
            "skipped_unresolved": skipped_unresolved,
            "skipped_lowconf": len(excluded_lowconf), "detail": detail}


def _build_cross_blocklist(crit: set, min_videos: int, max_uids: int,
                           include_lowconf: bool = False) -> dict:
    """跨视频合并屏蔽榜：在 >= min_videos 个视频都命中问题标准的发送者优先。

    B站用户屏蔽是全站生效的，单视频屏蔽列表覆盖面有限；「在多个视频都发问题
    弹幕/刷屏」的人才是真正的惯犯，屏蔽性价比最高。实现上逐视频复用
    _build_blocklist（max 不设限拿全量命中集），按 uid 聚合：
      - videos_hit：命中视频数（排序首位——惯犯程度）
      - sev/score/count：各视频取最大值（烦人程度）
    低置信度口径与单视频一致（逐视频排除；include_lowconf 放开）。

    Returns: {"entries": [...], "matched": 符合视频数门槛的总人数, "videos": 库内视频数}
    """
    with closing(get_db()) as conn:
        bvids = [r["bvid"] for r in conn.execute("SELECT bvid FROM videos").fetchall()]

    agg: dict[int, dict] = {}      # uid -> {videos, sev, score, count, notes}
    for bvid in bvids:
        try:
            r = _build_blocklist(bvid, crit, 10 ** 9, include_lowconf=include_lowconf)
        except sqlite3.Error as e:
            print(f"[Web] 跨视频屏蔽榜：{bvid} 构建失败跳过: {e}")
            continue
        for e in r["entries"]:
            uid = int(e["filter"])
            d = (r.get("detail") or {}).get(uid) or {}
            ent = agg.setdefault(uid, {"videos": 0, "sev": 0, "score": 0.0,
                                       "count": 0, "notes": []})
            ent["videos"] += 1
            ent["sev"] = max(ent["sev"], d.get("sev", 0))
            ent["score"] = max(ent["score"], d.get("score", 0.0))
            ent["count"] = max(ent["count"], d.get("count", 0))
            note = e.get("comment") or ""
            if note and note not in ent["notes"]:
                ent["notes"].append(note)

    matched = [uid for uid, ent in agg.items() if ent["videos"] >= min_videos]
    matched.sort(key=lambda u: (-agg[u]["videos"], -agg[u]["sev"],
                                -agg[u]["score"], -agg[u]["count"], u))
    selected = matched[:max_uids]
    entries = []
    for i, uid in enumerate(selected, 1):
        ent = agg[uid]
        entries.append({
            "type": 2, "filter": str(uid), "opened": True, "id": i,
            "comment": (f"{ent['videos']}个视频命中；" + "；".join(ent["notes"]))[:60],
        })
    return {"entries": entries, "matched": len(matched), "videos": len(bvids)}


def _video_page_meta(video_info: dict | None) -> dict[int, dict]:
    """分P元信息：{分P序号: {"part": 分P标题, "duration": 该P时长秒数}}。

    无 pages 字段（旧报告缺 video_info_json / 单P旧数据）返回 {}，调用方自行降级。"""
    meta: dict[int, dict] = {}
    for i, p in enumerate((video_info or {}).get("pages") or []):
        if not isinstance(p, dict):
            continue
        try:
            pno = int(p.get("page") or i + 1)
        except (TypeError, ValueError):
            pno = i + 1
        meta[pno] = {"part": p.get("part") or "", "duration": int(p.get("duration") or 0)}
    return meta


def _load_page_meta(bvid: str) -> dict[int, dict]:
    """从 videos.video_info_json 读该视频的分P元信息（web 端没有现成 video_info 时的入口）。

    读不到（旧数据无元信息/行缺失）返回 {}，调用方按单分P 降级。"""
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT video_info_json FROM videos WHERE bvid = ?", (bvid,)).fetchone()
    if row is None:
        return {}
    try:
        return _video_page_meta(json.loads(row["video_info_json"] or "{}"))
    except Exception:
        return {}


def _fill_sample_pages(bvid: str, profiles: list[dict]):
    """渲染期回填画像样本的分P（不落库）：本特性之前的缓存画像没有 danmaku.video_pages
    （阶段6 未写），按 danmaku 表的 (mid_hash, content, time) 反查补上，旧报告无需重跑
    即可显示「P{n} mm:ss」。仅多分P 视频需要（单分P 无歧义、标了也是冗余）。

    歧义保护：同一 (mid_hash, content, time) 在多分P 出现时该样本记 0（不标分P），
    宁可不标也不标错。时间按毫秒精度 round 后比对。"""
    todo = [p for p in profiles if not ((p.get("danmaku") or {}).get("video_pages"))]
    hashes = sorted({p.get("_mid_hash") for p in todo if p.get("_mid_hash")})
    if not hashes:
        return
    page_by_key: dict[tuple, int] = {}
    with closing(get_db()) as conn:
        for i in range(0, len(hashes), 500):     # 分块规避 SQLite 变量数上限
            chunk = hashes[i:i + 500]
            qmarks = ",".join("?" * len(chunk))
            for r in conn.execute(
                    f"SELECT mid_hash, content, time, page FROM danmaku "
                    f"WHERE bvid = ? AND mid_hash IN ({qmarks})", (bvid, *chunk)).fetchall():
                key = (r["mid_hash"], r["content"], round(float(r["time"] or 0), 3))
                pg = int(r["page"] or 1)
                if key in page_by_key and page_by_key[key] != pg:
                    page_by_key[key] = 0          # 歧义：不标注
                else:
                    page_by_key.setdefault(key, pg)
    for p in todo:
        dm = p.get("danmaku") or {}
        mh = p.get("_mid_hash") or ""
        times = dm.get("video_times") or []
        pages = []
        for i, c in enumerate(dm.get("contents") or []):
            tv = times[i] if i < len(times) else 0
            try:
                key = (mh, c, round(float(tv or 0), 3))
            except (TypeError, ValueError):
                key = None
            pages.append(page_by_key.get(key, 0) if key else 0)
        dm["video_pages"] = pages
        dm["multi_page"] = True


def _danmaku_density(bvid: str, duration, video_info: dict | None = None) -> dict | None:
    """概览页弹幕密度时间轴：按「分P内部时间」分桶计数（一眼看到哪个片段弹幕爆发）。

    关键：分P（多P）视频的弹幕 time 是各分P内部时间（0~该P时长），而 videos.duration 是
    各分P时长之和；两者混用会把 P1..Pn 全部挤到时间轴开头、P1 结束后整段恒为 0
    （BV1mtTD6rEtQ 4 分P、总时长 418s，报告里 02:02 之后全 0 即此故）。
    故按分P分别建桶，前端用分P选择器切换、跳转链接带 p 参数。

    桶数约每 10 秒一桶，上限 DENSITY_BUCKETS；无弹幕数据或时长未知返回 None（不渲染）。
    返回 {"multi": 是否多分P, "onepage_fallback": 分P时长未知时的单轴降级,
          "pages": [{page, part, duration, labels, data, starts}], "default": 默认选中下标}。"""
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT page, time FROM danmaku WHERE bvid = ?", (bvid,)).fetchall()
    if not rows:
        return None
    total = int(duration or 0)
    grouped: dict[int, list[float]] = {}
    for r in rows:
        try:
            pno = int(r["page"] or 1)
        except (TypeError, ValueError):
            pno = 1
        grouped.setdefault(pno, []).append(float(r["time"] or 0))
    meta = _video_page_meta(video_info)
    # 各分P轴长：分P元信息优先；单分P无元信息时退回 videos.duration（保持旧报告口径不变）
    axes: dict[int, int] = {}
    for pno in grouped:
        d = (meta.get(pno) or {}).get("duration") or 0
        if d > 0:
            axes[pno] = int(d)
    if len(grouped) == 1 and not axes and total > 0:
        axes[next(iter(grouped))] = total
    elif 0 < len(axes) < len(grouped):
        # 部分分P有时长、部分缺失：缺失P 以该P弹幕最大时刻近似轴长兜底（尾部空桶
        # 恒 0 不影响阅读）——不兜底的话这些P 的弹幕会整块从时间轴消失（"全缺"
        # 才走下方单轴降级，部分缺时静默丢数据）。全缺时维持单轴降级口径不变
        for pno in grouped:
            if pno not in axes:
                approx = max(grouped[pno]) + 1 if grouped[pno] else 0
                if approx > 0:
                    axes[pno] = int(approx)
    fallback = False
    if not axes and len(grouped) > 1 and total > 0:
        # 多分P但分P时长全未知（旧报告缺 video_info_json.pages）：降级为旧的单轴口径——
        # 各分P弹幕合并到同一根轴（宁可后半段为 0 也不整块不渲染，前端会标注该降级）
        grouped = {0: [t for times in grouped.values() for t in times]}
        axes = {0: total}
        fallback = True
    if not axes:
        return None
    pages = []
    for pno in sorted(axes):
        times = grouped.get(pno, [])
        dur = axes[pno]
        buckets = min(DENSITY_BUCKETS, max(10, dur // 10))
        size = dur / buckets
        counts = [0] * buckets
        for t in times:
            i = int(t // size)
            counts[min(max(i, 0), buckets - 1)] += 1
        pages.append({
            "page": pno, "part": (meta.get(pno) or {}).get("part") or "", "duration": dur,
            "labels": [_fmt_video_time(i * size) for i in range(buckets)],
            # starts：每桶起始秒数（前端点击柱条跳转该分P对应时段，P1-a）
            "starts": [int(i * size) for i in range(buckets)],
            "data": counts,
        })
    # 默认选中弹幕最多的分P（主内容通常就是它，省去手动切换）
    default = max(range(len(pages)), key=lambda i: sum(pages[i]["data"]))
    return {"multi": len(pages) > 1, "onepage_fallback": fallback,
            "pages": pages, "default": default}


_PEAK_FLIGHT_GUARD = threading.Lock()
_PEAK_FLIGHTS: dict[str, dict] = {}


def _detect_density_peaks_once(bvid: str, peaks_payload: list, video_info: dict) -> dict:
    """按 bvid 单飞：同一视频同时只有一次 detect_density_peaks。失败返回空 dict，不标注。"""
    with _PEAK_FLIGHT_GUARD:
        flight = _PEAK_FLIGHTS.get(bvid)
        if flight is None:
            flight = {"event": threading.Event(), "result": {}}
            _PEAK_FLIGHTS[bvid] = flight
            owner = True
        else:
            owner = False
    if not owner:
        flight["event"].wait()
        return flight["result"] if isinstance(flight.get("result"), dict) else {}
    try:
        try:
            result = detect_density_peaks(bvid, peaks_payload, video_info)
        except Exception as e:
            print(f"[Web] 高能点标注失败（{bvid}）: {e}")
            result = {}
        flight["result"] = result if isinstance(result, dict) else {}
        return flight["result"]
    finally:
        flight["event"].set()
        with _PEAK_FLIGHT_GUARD:
            if _PEAK_FLIGHTS.get(bvid) is flight:
                _PEAK_FLIGHTS.pop(bvid, None)


def _annotate_density_peaks(bvid: str, density: dict | None, video_info: dict) -> dict | None:
    """密度时间轴高能点标注：峰值桶取弹幕样本喂 LLM 判断爆发原因，原地挂到各 page。

    峰值判据（每分P独立）：桶弹幕数 >= max(DENSITY_PEAK_MIN_COUNT, 该P桶数的P90)——
    低于最小条数的"峰"多为噪声；全视频按弹幕量取前 DENSITY_PEAK_MAX 个（跨分P）。
    标注结果（含缓存命中/LLM 失败降级为不标注）挂 page["peaks"] = [{"i": 桶下标,
    "label", "kind"}]；未配置 LLM_API_KEY 时直接原样返回（纯本地渲染零成本路径）。
    """
    if not density or not LLM_API_KEY:
        return density
    # 1) 逐分P找候选峰桶（P90 阈值 + 最小条数下限）
    candidates = []   # (count, page_idx, bucket)
    for pi, page in enumerate(density["pages"]):
        data = page["data"]
        if len(data) < 5:
            continue
        srt = sorted(data)
        p90 = srt[min(len(srt) - 1, int(len(srt) * 0.9))]
        threshold = max(DENSITY_PEAK_MIN_COUNT, p90)
        for b, n in enumerate(data):
            if n >= threshold:
                candidates.append((n, pi, b))
    if not candidates:
        return density
    candidates.sort(reverse=True)
    candidates = candidates[:DENSITY_PEAK_MAX]

    # 2) 逐峰取该时段弹幕样本（桶边界以 starts 为准；末桶右界为该P时长）
    peaks_payload: list[dict] = []
    with closing(get_db()) as conn:
        for n, pi, b in candidates:
            page = density["pages"][pi]
            start = (page["starts"] or [0] * len(page["data"]))[b]
            end = (page["starts"][b + 1] if b + 1 < len(page["starts"])
                   else page["duration"])
            samples = [r["content"] for r in conn.execute(
                "SELECT content FROM danmaku WHERE bvid = ? AND page = ? "
                "AND time >= ? AND time < ? ORDER BY timestamp DESC LIMIT ?",
                (bvid, page["page"], start, end, DENSITY_PEAK_SAMPLE)).fetchall()]
            if not samples:
                continue
            peaks_payload.append({"idx": len(peaks_payload), "label": page["labels"][b],
                                  "count": n, "samples": samples,
                                  "_page": pi, "_bucket": b})

    # 3) LLM 标注（cringe_detector：缓存/降级/幻觉编号过滤都在里面）
    # 同一视频并发打开报告时合并为一次调用；失败仍不标注
    verdicts = _detect_density_peaks_once(bvid, peaks_payload, video_info) if peaks_payload else {}
    for p in peaks_payload:
        v = verdicts.get(p["idx"])
        if not v:
            continue
        page = density["pages"][p["_page"]]
        page.setdefault("peaks", []).append(
            {"i": p["_bucket"], "label": v["label"], "kind": v["kind"]})
    return density


def _danmaku_attr_stats(bvid: str) -> dict | None:
    """弹幕属性分布（mode/color 已入库）：滚动/顶部/底部占比 + 颜色 Top12。
    滚动弹幕占比、颜色聚集度、顶/底弹幕对识别刷屏与水军有区分度。无数据返回 None。"""
    with closing(get_db()) as conn:
        modes = conn.execute(
            "SELECT mode, COUNT(*) AS cnt FROM danmaku WHERE bvid = ? GROUP BY mode",
            (bvid,)).fetchall()
        if not modes:
            return None
        colors = conn.execute(
            "SELECT color, COUNT(*) AS cnt FROM danmaku WHERE bvid = ? AND color != '' "
            "GROUP BY color ORDER BY cnt DESC LIMIT 12", (bvid,)).fetchall()
    mode_map = {"滚动": 0, "顶部": 0, "底部": 0, "其他": 0}
    for r in modes:
        if r["mode"] in (1, 2, 3):
            mode_map["滚动"] += r["cnt"]
        elif r["mode"] == 4:
            mode_map["底部"] += r["cnt"]
        elif r["mode"] == 5:
            mode_map["顶部"] += r["cnt"]
        else:
            mode_map["其他"] += r["cnt"]
    return {
        "mode": {k: v for k, v in mode_map.items() if v},
        "colors": [[r["color"], r["cnt"]] for r in colors],
    }


def _comment_author_key(mid_hash) -> bool:
    """问题评论作者的合成键 cmt:{uid}。弹幕面板「已解析发送者」与解析质量区块都要排除。"""
    return str(mid_hash or "").startswith("cmt:")


def _resolve_quality(bvid: str) -> dict | None:
    """概览页「解析质量」区块：用户身份可信度——解析方式分布（评论验证/全局库/CRC32反查
    等）、置信度分布、碰撞风险人数（CRC32 反查存在撞库误识别风险）。

    报告结论可靠度判断依据：高置信+明文验证占比越高越可信；碰撞风险人数多需谨慎解读。"""
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT method, confidence, uid, mid_hash, resolution_json FROM senders WHERE bvid = ?",
            (bvid,)).fetchall()
        total_senders = conn.execute(
            "SELECT COUNT(DISTINCT mid_hash) FROM danmaku WHERE bvid = ?", (bvid,)).fetchone()[0]
    if not rows:
        return None
    methods: dict[str, int] = {}
    confs: dict[str, int] = {}
    collision = 0
    for r in rows:
        m = r["method"] or "未知"
        methods[m] = methods.get(m, 0) + 1
        c = r["confidence"] or "无"
        confs[c] = confs.get(c, 0) + 1
        record = json.loads(r["resolution_json"] or "{}")
        if r["uid"] is not None and record.get("collision_risk", m == METHOD_CRC32_CRACK):
            collision += 1
    # 置信度按 高/中/低/无 固定序输出（图表颜色语义稳定）
    conf_order = ["高", "中", "低", "无"]
    return {
        "method_labels": list(methods.keys()),
        "method_data": list(methods.values()),
        "conf_labels": [c for c in conf_order if c in confs],
        "conf_data": [confs[c] for c in conf_order if c in confs],
        "collision": collision,
        "resolved": sum(r["uid"] is not None and not _comment_author_key(r["mid_hash"]) for r in rows),
        "ambiguous": sum(r["method"] == "候选歧义" for r in rows),
        "pending": sum(r["method"] == "待验证" for r in rows),
        "comment_authors": sum(_comment_author_key(r["mid_hash"]) for r in rows),
        "total_senders": total_senders,
    }


def _repeat_events_window_text(bvid: str, e: dict) -> str:
    """复读事件的时间标注：视频内时间（可点跳转核验，多分P 带 p 参数）+ 发送时间。

    两个时间轴各自达标才显示（见 spam_detector.detect_repeat_events 的双轴口径）。"""
    parts = []
    v = e.get("video")
    if v:
        t0, t1 = int(v["start"]), int(v["end"])
        page = int(v.get("page") or 1)
        multi = len(_load_page_meta(bvid)) > 1
        href = (f'https://www.bilibili.com/video/{esc(bvid)}?'
                + (f'p={page}&' if multi else '') + f't={t0}')
        parts.append(f'<a href="{href}" target="_blank" rel="noopener" title="跳到该分P 该时刻核验">'
                     f'视频{" P%d" % page if multi else ""} '
                     f'{_fmt_video_time(t0)} ~ {_fmt_video_time(t1)}</a>')
    s = e.get("send")
    if s:
        parts.append('发送 ' + time.strftime("%m-%d %H:%M", time.localtime(s["start"]))
                     + ' ~ ' + time.strftime("%H:%M", time.localtime(s["end"])))
    return " · ".join(parts) or "—"


def _variant_badge(e: dict) -> str:
    """写法变体徽标：归一化合并后仍显示主写法，另标注「＋N 变体」免被误读为只此一句。"""
    n = int(e.get("variant_count") or 1)
    if n <= 1:
        return ""
    return f' <span class="dm-time" title="同一次复读的其他写法">＋{n - 1} 变体</span>'


def _variant_title(e: dict) -> str:
    """内容单元格 tooltip：主写法 + 各变体条数（写法差异已合并为同一事件）"""
    lines = [f'{c}（{n} 条）' for c, n in (e.get("variants") or [])]
    if int(e.get("variant_count") or 1) > len(lines):
        lines.append(f'…共 {e["variant_count"]} 种写法')
    return "\n".join(lines) or e.get("content", "")


def _repeat_events_data(bvid: str) -> tuple[list, dict]:
    """群体复读事件取数：返回 (events, 全池分布)。无弹幕时返回 ([], {})。

    概览页区块与分享长图共用——两处口径必须一致，否则图上与页上数字对不上。"""
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT content, mid_hash, time, timestamp, page FROM danmaku WHERE bvid = ?",
            (bvid,)).fetchall()
    if not rows:
        return [], {}
    # sqlite3.Row 无 .get，转 dict 再喂给检测函数
    dict_rows = [dict(r) for r in rows]
    return detect_repeat_events(dict_rows), pool_distribution_from_rows(dict_rows)


def _repeat_events_block(bvid: str) -> str:
    """概览页「群体复读事件」区块：全视频维度的接龙/+1 队列刷屏检测
    （spam_detector.detect_repeat_events：同一内容在 60s 窗口内被 ≥5 个不同发送者
    发送且 ≥8 条——单人检测抓不到这种「一人一句」的群体刷屏）。

    时间口径为双轴：**视频内时间**（接龙发生在同一画面，观众可横跨数月；按分P 分桶，
    可点跳转核验）+ **发送时间**（真实时间短窗集中刷屏）。底部附全池分布自检行
    （pool_distribution_from_rows：发送者数、弹幕数与重复率的 P50/P95），供阈值校准。
    无弹幕数据返回 ""（不渲染区块）。"""
    events, dist = _repeat_events_data(bvid)
    if not events and not dist:
        return ""
    if events:
        trs = "".join(
            f'<tr><td title="{esc(_variant_title(e))}">{esc(_truncate(e["content"], 30))}'
            f'{_variant_badge(e)}</td>'
            f'<td>{e["sender_count"]}</td><td>{e["total"]}</td>'
            f'<td>{_repeat_events_window_text(bvid, e)}</td></tr>'
            for e in events)
        body = (f'<table><thead><tr><th>内容</th><th>发送者数</th><th>窗口条数</th>'
                f'<th>时间（视频内可点跳转核验 / 发送时间）</th></tr></thead><tbody>{trs}</tbody></table>')
        if len(events) >= REPEAT_EVENT_TOP_N:
            # 命中数达展示上限：明示是"最多的前 N 起"而不是全部
            body += (f'<div class="cringe-reason">命中数 ≥ 展示上限 {REPEAT_EVENT_TOP_N} 起，'
                     f'以上为发送者数最多的前 {REPEAT_EVENT_TOP_N} 起</div>')
    else:
        body = '<p class="empty-note">未检出群体复读事件</p>'
    # 全池分布自检：重复率 = 同一发送者所发内容中的重复占比
    dist_line = (f'全池 {dist.get("senders", 0):,} 个发送者 · '
                 f'弹幕数 P50={dist.get("count_p50", 0):g}/P95={dist.get("count_p95", 0):g} · '
                 f'重复率 P50={dist.get("repeat_p50", 0) * 100:.0f}%'
                 f'/P95={dist.get("repeat_p95", 0) * 100:.0f}%（阈值校准参考）')
    return f'''
    <div class="cringe-board">
        <h3>🔁 群体复读事件<span class="chart-hint">（同一内容在 60 秒窗口内被 ≥5 个不同发送者发送且 ≥8 条；时间轴分「视频内时间」＝同一画面接龙/+1 与「发送时间」＝短时间集中刷屏，前者可点跳转核验）</span></h3>
        {body}
        <div class="cringe-reason">{dist_line}</div>
    </div>'''


# ========== 误报标记（P2-a）/ 争执焦点（P0-b）/ 问题评论榜（P1-b） ==========

def _fp_btn(kind: str, target, marked: bool) -> str:
    """误报标记按钮（P2-a）：点击切换标记，标记后该条不再计入聚合，可再点撤销"""
    label = "撤销误报" if marked else "误报"
    cls = "fp-btn fp-btn-marked" if marked else "fp-btn"
    return (f'<button class="{cls}" data-kind="{esc(kind)}" data-target="{esc(str(target))}" '
            f'onclick="fpToggle(this)" '
            f'title="人工标记该条为 LLM 误报；标记后不计入聚合与疑似分，可撤销">{label}</button>')


def _apply_danmaku_fp(profiles: list[dict], fp_dm: set[str]) -> list[str]:
    """弹幕误报扣除（P2-a）：按内容把人工标记误报的问题弹幕从每个画像的 cringe 聚合中
    剔除（count/max_severity/categories/examples 全部重算，风险排序随之降级）。

    判定按内容去重（llm_cache 同源同罪），故误报粒度=内容，一次标记对所有发送者生效。
    旧 llm_cache 结果无全量 items 时回退用 examples（≤5 条）重算，count 可能偏小，
    --force 重跑后即为精确值。返回实际被扣除的误报内容列表（供榜单底部撤销入口）。"""
    if not fp_dm:
        return []
    used: list[str] = []
    for p in profiles:
        cr = p.get("cringe") or {}
        if not cr.get("count"):
            continue
        items = cr.get("items") or cr.get("examples") or []
        kept = [it for it in items if it.get("content") not in fp_dm]
        for it in items:
            c = it.get("content")
            if c in fp_dm and c not in used:
                used.append(c)
        if len(kept) == len(items):
            continue
        cr["count"] = len(kept)
        cr["max_severity"] = max((it.get("severity", 1) for it in kept), default=0)
        cats: list[str] = []
        for it in kept:
            if it.get("category") and it["category"] not in cats:
                cats.append(it["category"])
        cr["categories"] = cats
        cr["examples"] = kept[:5]
        cr["items"] = kept
        p["cringe"] = cr
    return used


def _apply_spam_fp(profiles: list[dict], fp_spam: set[str]) -> None:
    """刷屏判定人工误报：展示层把命中发送者降为低风险（库内原始值不动）。

    网页报告与分享长图共用，保证高风险刷屏人数和用户卡徽标一致。"""
    if not fp_spam:
        return
    for p in profiles:
        mh = (p.get("danmaku") or {}).get("mid_hash") or p.get("_mid_hash") or ""
        if mh and mh in fp_spam:
            dm = p.setdefault("danmaku", {})
            dm["spam_level"] = "低"
            dm["spam_fp"] = True


def _fp_dm_block(fp_contents: list[str]) -> str:
    """问题弹幕榜底部：已人工标记误报的弹幕内容（不计入上方聚合），点撤销恢复"""
    if not fp_contents:
        return ""
    items = "".join(f'<li>{esc(c)} {_fp_btn("dm", c, True)}</li>' for c in fp_contents)
    return (f'<details class="fp-block"><summary>🚫 已标记误报 {len(fp_contents)} 条'
            f'（不计入上方聚合，展开可撤销）</summary><ul class="fp-list">{items}</ul></details>')


# ========== 头像后台补采（争执焦点关系图：face_cache 缺失的 uid 异步补齐） ==========

_FACE_BACKFILL_RUNNING: set[int] = set()   # 在采 uid，防并发重复补采
_FACE_BACKFILL_LOCK = threading.Lock()


def _backfill_faces(uids: list[int]):
    """后台线程：逐个补采头像（名片接口很轻量），走 BiliAPIClient 限速；
    Cookie 失效/触发风控即整体放弃本次，下次渲染再试"""
    with _FACE_BACKFILL_LOCK:
        todo = [u for u in uids if u not in _FACE_BACKFILL_RUNNING]
        _FACE_BACKFILL_RUNNING.update(todo)
    if not todo:
        return
    try:
        client = _get_client()
    except Exception as e:
        print(f"[Face] 补采放弃（无可用登录态）: {e}")
        with _FACE_BACKFILL_LOCK:
            _FACE_BACKFILL_RUNNING.difference_update(todo)
        return

    def work():
        try:
            for uid in todo:
                try:
                    data = client.get(USER_CARD_URL, params={"mid": uid, "photo": "false"})
                except Exception as e:
                    # 分析 job 跑过后共享 client 的 raise_on_risk 已置 True，-412 会抛
                    # RiskControlError 而非返回 dict：兜底为"放弃本次补采"，下次渲染再试
                    print(f"[Face] 请求异常（{e}），放弃本次补采（剩 {len(todo) - todo.index(uid)} 个下次再采）")
                    break
                if data.get("code") == -412:
                    print(f"[Face] 触发风控，停止本次补采（剩 {len(todo) - todo.index(uid)} 个下次再采）")
                    break
                face = ((data.get("data") or {}).get("card") or {}).get("face", "")
                try:
                    save_face(uid, face)   # 空串也落库：标记查过，避免每次渲染重复请求
                except Exception as e:
                    # 写库失败（如数据库锁定）不得让补采线程崩溃：跳过本次，下次渲染再试
                    print(f"[Face] UID:{uid} 头像落库失败（{e}），跳过该条")
            print(f"[Face] 头像补采完成 {len(todo)} 个")
        finally:
            with _FACE_BACKFILL_LOCK:
                _FACE_BACKFILL_RUNNING.difference_update(todo)

    threading.Thread(target=work, daemon=True).start()


def _faces_for(uids: list[int]) -> dict[int, str]:
    """节点头像汇总：face_cache 优先，users.data_json 兜底（已采集用户），
    两侧都缺的触发后台补采（本次渲染先缺省，下次刷新带上）"""
    faces = load_faces(uids)
    miss_cache = [u for u in uids if u not in faces]
    if miss_cache:
        with closing(get_db()) as conn:
            for i in range(0, len(miss_cache), 500):
                chunk = miss_cache[i:i + 500]
                qm = ",".join("?" * len(chunk))
                for r in conn.execute(
                        f"SELECT uid, data_json FROM users WHERE uid IN ({qm})", chunk):
                    try:
                        f = json.loads(r["data_json"]).get("face", "")
                        if f:
                            faces[r["uid"]] = f
                    except Exception:
                        pass
    cached = load_face_cached_uids(uids)
    need = [u for u in uids if u not in faces and u not in cached]
    if need:
        _backfill_faces(need)
    return faces


def _attack_focus(bvid: str, fp_cmt: set[str]) -> dict:
    """争执焦点（P0-b）：问题回复按 parent_rpid 还原 A→B 攻击边，聚合挑事分/被攻击分。

    只统计问题回复（problem 非空且 parent_rpid>0、父级在库、非自回）；已标记误报的不参与。
    挑事者侧留至多 3 条代表原文（攻击原文 + 被攻击者的原评上下文，成对展示才能看懂为何而喷，
    同 (原文,对象) 去重）；被围攻者侧受害原评按条去重（同一条原评被 N 人围攻只展示一次，
    攻击条目挂在该原评下；不同原评各展示一次，至多 3 条）。
    展示名额随攻击边数浮动（保底 ATTACK_FOCUS_TOP_N，每10条攻击边+1，封顶 ATTACK_FOCUS_MAX_N）。
    返回 {"attackers": [{uid,name,count,categories,examples}],
           "victims": [{uid,name,count,comments}], "names": {uid: 昵称},
           "top_n": 动态名额, "edges": 攻击边数}，无攻击边时 attackers 为空列表。"""
    with closing(get_db()) as conn:
        rows = conn.execute('''
            SELECT c.rpid, c.uid AS attacker, c.uname AS aname, c.problem,
                   c.content AS reply_content, c.parent_rpid,
                   p.uid AS victim, p.uname AS vname, p.content AS parent_content,
                   gp.uid AS gp_uid, gp.uname AS gp_uname, gp.content AS gp_content,
                   ua.name AS a_dbname, uv.name AS v_dbname
            FROM comments c
            JOIN comments p ON p.bvid = c.bvid AND p.rpid = c.parent_rpid
            LEFT JOIN comments gp ON gp.bvid = p.bvid AND gp.rpid = p.parent_rpid
            LEFT JOIN users ua ON ua.uid = c.uid
            LEFT JOIN users uv ON uv.uid = p.uid
            WHERE c.bvid = ? AND c.problem != '' AND c.parent_rpid > 0 AND p.uid != c.uid
            ORDER BY c.like DESC
        ''', (bvid,)).fetchall()

    mask = _mask_on()

    def _nm(uname, dbname, uid) -> str:
        s = uname or dbname or f"UID:{uid}"
        return _mask_name(s) if mask else s   # 隐藏信息：昵称只留末字

    attackers: dict[int, dict] = {}
    victims: dict[int, dict] = {}
    edge_cnt = 0
    for r in rows:
        if str(r["rpid"]) in fp_cmt:
            continue
        edge_cnt += 1
        a, v = r["attacker"], r["victim"]
        a_name = _nm(r["aname"], r["a_dbname"], a)
        v_name = _nm(r["vname"], r["v_dbname"], v)
        ea = attackers.setdefault(a, {"uid": a, "name": a_name,
                                      "count": 0, "victims": Counter(), "categories": [],
                                      "examples": []})
        ea["count"] += 1
        ea["victims"][v] += 1
        if r["problem"] not in ea["categories"]:
            ea["categories"].append(r["problem"])
        # 代表原文：攻击话语 + 被攻击者的原评上下文（pair）；同 (原文,对象) 去重避免刷屏复读刷屏列表
        if len(ea["examples"]) < 3 and not any(
                x["content"] == r["reply_content"] and x["target"] == v_name
                for x in ea["examples"]):
            ea["examples"].append({"content": r["reply_content"], "target": v_name,
                                   "parent": r["parent_content"], "category": r["problem"],
                                   # 上游层昵称同样过遮蔽（mask 时只留末字）
                                   "gp_name": _mask_name(r["gp_uname"]) if mask else (r["gp_uname"] or ""),
                                   "gp_content": r["gp_content"],
                                   "gp_is_self": bool(r["gp_uid"]) and r["gp_uid"] == a})
        ev = victims.setdefault(v, {"uid": v, "name": v_name,
                                    "count": 0, "attackers": Counter(), "comments": {}})
        ev["count"] += 1
        ev["attackers"][a] += 1
        # 受害原评按条（parent_rpid）去重：同一条原评被 N 人围攻只展示一次，
        # 各攻击者的攻击原文挂在其下；不同原评各展示一次，至多 3 条
        vc = ev["comments"].get(r["parent_rpid"])
        if vc is None and len(ev["comments"]) < 3:
            vc = {"parent": r["parent_content"], "attacks": []}
            ev["comments"][r["parent_rpid"]] = vc
        if vc is not None and len(vc["attacks"]) < 3:
            vc["attacks"].append({"attack": r["reply_content"], "attacker": a_name,
                                  "category": r["problem"]})
    names = {u: e["name"] for u, e in attackers.items()}
    names.update({u: e["name"] for u, e in victims.items()})
    # 头像（关系图节点用）：face_cache + users.data_json 双源汇总，缺的异步补采
    uids = list(set(attackers) | set(victims))
    faces = _faces_for(uids)
    # 展示名额随攻击边数浮动：保底 ATTACK_FOCUS_TOP_N，每 10 条攻击边 +1，封顶 ATTACK_FOCUS_MAX_N
    top_n = min(ATTACK_FOCUS_MAX_N, max(ATTACK_FOCUS_TOP_N, ATTACK_FOCUS_TOP_N + edge_cnt // 10))
    return {
        "attackers": sorted(attackers.values(), key=lambda e: -e["count"]),
        "victims": sorted(victims.values(), key=lambda e: -e["count"]),
        "names": names,
        "faces": faces,
        "top_n": top_n,
        "edges": edge_cnt,
    }


def _truncate(text: str, limit: int = 80) -> str:
    """原文摘录截断（争执焦点/楼中楼引用展示用）；剥离 B站楼中楼自带的
    「回复 @xxx :」前缀——展示层已有「对 X 的原评」标签，前缀留着只会混淆视听"""
    text = (text or "").strip().replace("\n", " ")
    text = re.sub(r"^回复\s*@.+?\s*[:：]\s*", "", text)
    return text if len(text) <= limit else text[:limit] + "…"


# ========== 隐藏信息开关（Cookie mask=1，全站展示层遮蔽昵称/UID） ==========

def _light_user_cards(bvid: str, profiles: list, mask: bool, fp: set) -> str:
    """未生成深度画像的已解析用户展示本视频发言，只读数据库，不补采。"""
    covered = {p.get("uid") for p in profiles}
    grouped = {}
    for sender in load_senders(bvid):
        uid = sender.get("uid")
        if not uid or uid in covered:
            continue
        item = grouped.setdefault(uid, {"sender": sender, "contents": [], "count": 0})
        item["contents"].extend(sender.get("contents") or [])
        item["count"] += sender.get("danmaku_count", 0)
    if not grouped:
        return ""
    comments = {}
    for comment in load_comments(bvid):
        if comment["uid"] in grouped:
            comments.setdefault(comment["uid"], []).append(comment)
    result = []
    for uid, item in sorted(grouped.items(), key=lambda pair: pair[1]["count"], reverse=True):
        sender = item["sender"]
        record = sender.get("resolution") or {}
        card = (record.get("candidate_cards") or {}).get(str(uid)) or {}
        replies = comments.get(uid, [])
        name = card.get("name") or next((c.get("uname") for c in replies if c.get("uname")), None) or f"用户 {uid}"
        shown_name = _mask_name(name) if mask else name
        shown_uid = _mask_uid(uid) if mask else uid
        linkable = uid_linkable({"uid": uid, "resolve_method": sender.get("method"), "_resolution": record})
        tag = "a" if linkable else "div"
        attrs = f' href="https://space.bilibili.com/{uid}" target="_blank" rel="noopener noreferrer"' if linkable else ""
        texts = list(dict.fromkeys(item["contents"]))[:10]
        replies = replies[:10]
        dm = "".join(f"<li>{esc(text)}</li>" for text in texts)
        cmt = "".join(f"<li>{esc(mask_reply_prefix(c['content'], mask, _mask_name))}</li>" for c in replies)
        spam = "低" if ("spam", sender["mid_hash"]) in fp else sender.get("spam_level", "低")
        result.append(f'''<article class="user-card light-user-card" id="uid-{uid}"
            data-danmaku-count="{item['count']}" data-spam="{esc(spam)}" data-spam-score="{sender.get('spam_score', 0)}">
            <{tag}{attrs} class="light-user-link"><strong class="username">{esc(shown_name)}</strong>
            <span class="uid">UID:{esc(shown_uid)}</span><span>未深度采集 · 弹幕 {item['count']} 条 · 评论 {len(replies)} 条</span>
            <h4>弹幕</h4><ul>{dm or '<li>无弹幕记录</li>'}</ul>
            <h4>评论</h4><ul>{cmt or '<li>本视频已采评论中无记录</li>'}</ul></{tag}></article>''')
    return "".join(result)


def _mask_on() -> bool:
    """隐藏信息开关是否开启（Cookie mask=1）；无请求上下文（后台线程/直调）时不遮蔽"""
    try:
        return request.cookies.get("mask") == "1"
    except RuntimeError:
        return False


def _mask_name(name) -> str:
    """昵称遮蔽：只显示最后一个字，其余字符用固定颜文字代替
    （「洋葱调色盘」→「（￣▽￣）盘」；单字名/空名不变）"""
    s = str(name or "")
    if len(s) <= 1:
        return s
    return "（￣▽￣）" + s[-1]


def _mask_uid(uid) -> str:
    """UID 遮蔽：除前三位外全部替换为 *（3691006683777071 → 369*************；长度≤3 不变）"""
    s = str(uid or "")
    if len(s) <= 3:
        return s
    return s[:3] + "*" * (len(s) - 3)


def _mask_reply_text(text) -> str:
    """隐藏信息开启时，遮蔽楼中楼正文开头「回复 @昵称」里的昵称（规则同 _mask_name）。"""
    return mask_reply_prefix(text, _mask_on(), _mask_name)


def _attack_focus_html(data: dict) -> str:
    """争执焦点区块：左列挑事者（发起问题回复最多，攻击原文附被攻击者原评上下文），
    右列被围攻者（被问题回复命中最多，受害原评按条去重、攻击条目挂在其下）"""
    if not data or not data["attackers"]:
        return ""
    names = data["names"]
    # 隐藏信息：图节点 id 与明细条目 data-uid 同步用遮蔽值（前端点击节点靠它俩配对定位；
    # 遮蔽值碰撞时两个用户会合并为一个节点，属已知取舍）；/user/<uid> 链接保持原值
    _disp_uid = _mask_uid if _mask_on() else (lambda u: u)

    def _opp_text(counter: Counter) -> str:
        return "、".join(f'{esc(names.get(u, f"UID:{u}"))}×{n}' for u, n in counter.most_common(3))

    def _quotes(examples: list[dict]) -> str:
        """挑事者证据：被攻击者的原评（上下文）+ 攻击原文成对展示；
        原评本身也是回复时再多带一层上游原文——单看一层父评看不出这是
        「一来一回」的第几回合（常见误读：被攻击者先动的口）"""
        out = []
        for e in examples:
            if e.get("gp_content"):
                # 上游正是挑事者自己早前发的评论时明确标注（最常见的回骂场景）
                up_label = "更早一层（正是ta自己早前发的）" if e.get("gp_is_self") else "更早一层"
                out.append(f'<div class="af-quote">↪ {up_label}：{esc(e["gp_name"] or "未知")} 说：'
                           f'「{esc(_truncate(e["gp_content"], 50))}」</div>')
            out.append(
                f'<div class="af-quote">对 {esc(e["target"])} 的原评：'
                f'「{esc(_truncate(e["parent"], 50))}」</div>'
                f'<div class="af-quote af-quote-atk">⚔ 攻击：「{esc(_truncate(e["content"]))}」'
                f'{_problem_chip(e["category"])}</div>')
        return "".join(out)

    def _victim_quotes(comments: list[dict]) -> str:
        """被围攻者证据：受害原评按条去重只展示一次（被 N 人围攻不重复 N 次），
        各攻击者的攻击原文挂在该原评下（看出谁攻击了ta、攻击了什么）"""
        out = []
        for c in comments:
            out.append(f'<div class="af-quote">原评：「{esc(_truncate(c["parent"]))}」</div>')
            for at in c["attacks"]:
                out.append(
                    f'<div class="af-quote af-quote-atk">⚔ {esc(at["attacker"])} 攻击：'
                    f'「{esc(_truncate(at["attack"]))}」{_problem_chip(at["category"])}</div>')
        return "".join(out)

    def _item(e: dict, badge: str, opp_label: str, opp: str) -> str:
        return f'''<div class="af-item" data-side="a" data-uid="{_disp_uid(e["uid"])}">
            <div class="af-line">
                <a href="/user/{e["uid"]}" title="查看用户互动时间线">{esc(e["name"])}</a>
                <span class="hot-badge">{badge.format(e["count"])}</span>
                {_category_chips(e.get("categories", []))}
                <span class="af-targets">{opp_label} {opp}</span>
            </div>{_quotes(e["examples"])}</div>'''

    def _victim_item(e: dict) -> str:
        return f'''<div class="af-item" data-side="v" data-uid="{_disp_uid(e["uid"])}">
            <div class="af-line">
                <a href="/user/{e["uid"]}" title="查看用户互动时间线">{esc(e["name"])}</a>
                <span class="hot-badge">被攻击 {e["count"]} 次</span>
                <span class="af-targets">主要来源： {_opp_text(e["attackers"])}</span>
            </div>{_victim_quotes(list(e["comments"].values()))}</div>'''

    top_n = data.get("top_n", ATTACK_FOCUS_TOP_N)
    shown_attackers = data["attackers"][:top_n]
    shown_victims = data["victims"][:top_n]
    # 关系图画布数据：图节点按 uid 合并——同一人既是挑事者也是被围攻者时只画一个节点
    # （na=攻击次数 / nv=被攻击次数，攻击链 A→X→B 通过指向ta的边+ta发出的边体现）；
    # links 的端点放宽到任一身份上榜（链条边：目标仅以挑事者身份上榜也保留）
    merged: dict = {}
    faces = data.get("faces", {})
    for e in shown_attackers:
        ent = merged.setdefault(_disp_uid(e["uid"]),
                                {"id": _disp_uid(e["uid"]), "name": e["name"],
                                 "na": 0, "nv": 0, "face": faces.get(e["uid"], "")})
        ent["na"] = e["count"]
    for e in shown_victims:
        ent = merged.setdefault(_disp_uid(e["uid"]),
                                {"id": _disp_uid(e["uid"]), "name": e["name"],
                                 "na": 0, "nv": 0, "face": faces.get(e["uid"], "")})
        ent["nv"] = e["count"]
    graph_nodes = list(merged.values())
    shown_uids = {e["uid"] for e in shown_attackers} | {e["uid"] for e in shown_victims}
    graph_links = [{"s": _disp_uid(e["uid"]), "t": _disp_uid(v), "w": n}
                   for e in shown_attackers
                   for v, n in e["victims"].most_common() if v in shown_uids]
    graph = {"nodes": graph_nodes, "links": graph_links}
    graph_html = ""
    if graph_links:
        # 双引号属性 + esc() 整段转义：节点 name 来自外部可控昵称，
        # 单引号属性直拼 json.dumps 会被引号闭合注入（存储型 XSS）
        graph_html = (f'<div class="af-graph" data-af-graph="{esc(json.dumps(graph, ensure_ascii=False))}">'
                      f'<div class="af-graph-hint">🖱 拖空白平移 · 滚轮缩放 · 拖头像调位置</div>'
                      f'</div>')
    a_html = "".join(_item(e, "攻击 {} 次", "主要对象：", _opp_text(e["victims"]))
                     for e in shown_attackers)
    v_html = "".join(_victim_item(e) for e in shown_victims)
    return f'''
    <div class="cringe-board af-board">
        <h3>⚔️ 争执焦点（谁攻击谁：问题回复按 parent_rpid 还原攻击边 {data.get("edges", 0)} 条；
            多圆簇画布：每个冲突簇一个小圆（受害者居中、攻击者环绕），同一人只画一个节点——既攻击又被攻击的链条节点同时有进出边（标签显示 攻×n 被×n），箭头线随攻击者分色，节点大小/线粗映射攻击次数，悬停节点高亮其关系，点击定位到下方明细）</h3>
        {graph_html}
        <div class="af-cols">
            <div class="af-col"><h4>🗡 挑事者 Top{top_n}</h4>{a_html}</div>
            <div class="af-col"><h4>🛡 被围攻者 Top{top_n}</h4>{v_html}</div>
        </div>
    </div>'''


def _problem_comment_board(bvid: str, fp_cmt: set[str]) -> tuple[list[dict], list[dict], int]:
    """问题评论榜（P1-b）：全部问题评论按热度（点赞 + 回复数×权重）降序，高热度优先。

    楼中楼评论（is_sub）附被回复的父评论原文/作者，供判断语境。
    返回 (未标记误报的 Top N 条目, 已标记误报的条目, 扣除误报后的总数)；
    后者供底部撤销入口，总数给长图总览（不能用截断后的列表长度）。"""
    with closing(get_db()) as conn:
        rows = conn.execute('''
            SELECT c.rpid, c.uid, c.uname, c.content, c.ctime, c.like, c.reply_count,
                   c.is_sub, c.problem, u.name AS db_name,
                   p.content AS parent_content, p.uname AS parent_uname, pu.name AS parent_db_name,
                   p.uid AS parent_uid
            FROM comments c
            LEFT JOIN users u ON u.uid = c.uid
            LEFT JOIN comments p ON p.bvid = c.bvid AND p.rpid = c.parent_rpid
            LEFT JOIN users pu ON pu.uid = p.uid
            WHERE c.bvid = ? AND c.problem != ''
        ''', (bvid,)).fetchall()
    items, marked = [], []
    mn = _mask_name if _mask_on() else (lambda s: s)   # 隐藏信息：评论者/父评作者昵称只留末字
    for r in rows:
        it = {
            "rpid": r["rpid"], "uid": r["uid"],
            "name": mn(r["uname"] or r["db_name"] or f"UID:{r['uid']}"),
            "content": r["content"], "ctime": r["ctime"], "like": r["like"] or 0,
            "reply_count": r["reply_count"] or 0, "is_sub": r["is_sub"],
            "problem": r["problem"],
            "heat": (r["like"] or 0) + (r["reply_count"] or 0) * COMMENT_HEAT_REPLY_WEIGHT,
            # 楼中楼语境：被回复的父评论（父级未采集到时为空，前端不展示）
            "parent_content": r["parent_content"] or "",
            "parent_name": mn(r["parent_uname"] or r["parent_db_name"]
                              or (f"UID:{r['parent_uid']}" if r["parent_uid"] else "")),
        }
        (marked if str(r["rpid"]) in fp_cmt else items).append(it)
    items.sort(key=lambda x: -x["heat"])
    marked.sort(key=lambda x: -x["heat"])
    # 总数是扣除误报后的全量，榜单仍只返回前 N 条（长图总览用总数，不能用截断长度）
    total = len(items)
    return items[:PROBLEM_COMMENT_TOP_N], marked, total


def _problem_comment_board_html(items: list[dict], marked: list[dict], aid, up_mid: int) -> str:
    """问题评论榜 HTML（高回复评论页顶部区块）"""
    if not items and not marked:
        return ""

    def _row(it: dict, is_marked: bool) -> str:
        date = datetime.fromtimestamp(it["ctime"]).strftime("%Y-%m-%d") if it["ctime"] else ""
        sub_mark = '<span class="dm-time">回复</span> ' if it["is_sub"] else ""
        origin = (f'<a class="hot-origin" href="https://www.bilibili.com/video/av{aid}#reply{it["rpid"]}" '
                  f'target="_blank" rel="noopener">原文 ↗</a>') if aid else ""
        # 楼中楼语境（fix）：子评论贴出被回复的父评论原文，便于判断攻击对象
        parent_html = ""
        if it["is_sub"] and it["parent_content"]:
            parent_html = (f'<div class="pcb-parent">回复 @{esc(it["parent_name"])}：'
                           f'「{esc(_truncate(it["parent_content"]))}」</div>')
        return f'''<li class="pcb-item">
            <a class="hot-author" href="/user/{esc(it["uid"])}"
               title="查看该用户在已分析视频中的互动时间线">{esc(it["name"])}</a>{_role_badges(it["uid"], 0, up_mid)}
            {_problem_chip(it["problem"], is_marked)}{_fp_btn("cmt", it["rpid"], is_marked)}
            <span class="dm-time">热度 {it["heat"]:,}（👍{it["like"]:,} + 💬{it["reply_count"]:,}×{COMMENT_HEAT_REPLY_WEIGHT}）· {date}</span>
            {origin}{parent_html}<br>{sub_mark}{esc(_mask_reply_text(it["content"]))}</li>'''

    body = "".join(_row(it, False) for it in items)
    marked_html = ""
    if marked:
        mrows = "".join(_row(it, True) for it in marked)
        marked_html = (f'<details class="fp-block"><summary>🚫 已标记误报 {len(marked)} 条'
                       f'（不计入榜单与聚合，展开可撤销）</summary><ul class="fp-list">{mrows}</ul></details>')
    return f'''
    <div class="cringe-board">
        <h3>🔥 问题评论榜（按热度 = 点赞 + 回复数×{COMMENT_HEAT_REPLY_WEIGHT} 加权，最多 {PROBLEM_COMMENT_TOP_N} 条）</h3>
        <ul class="pcb-list">{body}</ul>
        {marked_html}
    </div>'''


def _cross_video_overlaps() -> list[dict]:
    """首页跨视频重叠用户面板：在 >= CROSS_VIDEO_MIN_VIDEOS 个已分析视频中都发过弹幕的
    发送者（全局 UID 映射沉淀后此查询很便宜），找水军/情绪带节奏用户比单视频报告强。

    每个视频附弹幕/评论明细样本（前端 <details> 展开查看「TA 在该视频说了什么」）：
    弹幕按发送时间倒序、评论按点赞降序各取 MAX_FOOTPRINT_*_SAMPLES 条；旧版本分析
    （danmaku 表无行）标注明细未留存。
    返回 [{uid, name, video_count, total_dm, videos: [{bvid,title,pubdate,dm_count,dm_samples,
    cmt_count,cmt_samples,legacy}]}]（pubdate 为视频发布日期 YYYY-MM-DD，可空），按视频数/弹幕数降序。"""
    with closing(get_db()) as conn:
        rows = conn.execute('''
            SELECT s.uid, u.name, COUNT(DISTINCT s.bvid) AS vcnt, SUM(s.danmaku_count) AS total_dm
            FROM senders s LEFT JOIN users u ON u.uid = s.uid
        LEFT JOIN video_profiles p ON p.bvid=s.bvid AND p.uid=s.uid
            WHERE s.uid IS NOT NULL
            GROUP BY s.uid HAVING vcnt >= ?
            ORDER BY vcnt DESC, total_dm DESC LIMIT ?
        ''', (CROSS_VIDEO_MIN_VIDEOS, CROSS_VIDEO_MAX_USERS)).fetchall()
        if not rows:
            return []
        uids = [r["uid"] for r in rows]
        qmarks = ",".join("?" * len(uids))
        # (uid, bvid) → 视频标题、该用户在该视频的 mid_hash 列表与弹幕计数
        vinfo: dict[tuple[int, str], dict] = {}
        # 视频发布日期按 bvid 只解析一次（video_info_json 里的 pubdate 为 Unix 时间戳，
        # 非分析时间；解析失败/无 pubdate 留空串，渲染层不显示日期）
        pubdate_cache: dict[str, str] = {}
        for r in conn.execute(f'''
                SELECT s.uid, s.bvid, s.mid_hash, s.danmaku_count, v.title, v.video_info_json FROM senders s
                JOIN videos v ON v.bvid = s.bvid
                WHERE s.uid IN ({qmarks})
        ''', uids).fetchall():
            if r["bvid"] not in pubdate_cache:
                ts = 0
                try:
                    ts = int(json.loads(r["video_info_json"] or "{}").get("pubdate") or 0)
                except Exception:
                    pass
                pubdate_cache[r["bvid"]] = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            ent = vinfo.setdefault((r["uid"], r["bvid"]), {
                "title": r["title"] or r["bvid"], "pubdate": pubdate_cache[r["bvid"]],
                "mid_hashes": [], "dm_count": 0})
            if r["mid_hash"] not in ent["mid_hashes"]:
                ent["mid_hashes"].append(r["mid_hash"])
            ent["dm_count"] += r["danmaku_count"] or 0

        # 明细样本：逐 (uid,bvid) 取弹幕/评论（50 用户×数视频，本地 SQLite 量级可控）
        legacy_cache: dict[str, bool] = {}
        videos_by_uid: dict[int, list] = {}
        for (uid, bvid), ent in vinfo.items():
            if bvid not in legacy_cache:
                legacy_cache[bvid] = conn.execute(
                    "SELECT 1 FROM danmaku WHERE bvid = ? LIMIT 1", (bvid,)).fetchone() is None
            dm_samples: list[str] = []
            for mh in ent["mid_hashes"]:
                dm_samples.extend(r["content"] for r in conn.execute(
                    "SELECT content FROM danmaku WHERE bvid = ? AND mid_hash = ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (bvid, mh, MAX_FOOTPRINT_DANMAKU_SAMPLES)).fetchall())
                if len(dm_samples) >= MAX_FOOTPRINT_DANMAKU_SAMPLES:
                    break
            cmt_count = conn.execute(
                "SELECT COUNT(*) FROM comments WHERE bvid = ? AND uid = ?",
                (bvid, uid)).fetchone()[0]
            cmt_samples = [
                {"content": r["content"], "like": r["like"], "problem": r["problem"] or ""}
                for r in conn.execute(
                    "SELECT content, like, problem FROM comments WHERE bvid = ? AND uid = ? "
                    "ORDER BY like DESC, ctime DESC LIMIT ?",
                    (bvid, uid, MAX_FOOTPRINT_COMMENT_SAMPLES)).fetchall()]
            videos_by_uid.setdefault(uid, []).append({
                "bvid": bvid, "title": ent["title"], "pubdate": ent["pubdate"],
                "dm_count": ent["dm_count"], "dm_samples": dm_samples[:MAX_FOOTPRINT_DANMAKU_SAMPLES],
                "cmt_count": cmt_count, "cmt_samples": cmt_samples,
                "legacy": legacy_cache[bvid],
            })
    return [{
        "uid": r["uid"],
        # 隐藏信息（mask Cookie）：昵称只留末字
        "name": _mask_name(r["name"] or f"UID:{r['uid']}") if _mask_on() else (r["name"] or f"UID:{r['uid']}"),
        "video_count": r["vcnt"],
        "total_dm": r["total_dm"] or 0,
        # 弹幕多的视频排前，展开列表一眼看到主战场
        "videos": sorted(videos_by_uid.get(r["uid"], []),
                         key=lambda v: -(v["dm_count"] + v["cmt_count"])),
    } for r in rows]


def _fmt_duration(sec) -> str:
    """秒 → mm:ss 或 h:mm:ss 时长文本（首页视频列表时长列）"""
    sec = int(sec or 0)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fmt_video_time(sec) -> str:
    """视频内时间（秒）→ mm:ss（用户时间线弹幕样本用）"""
    sec = int(sec or 0)
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


def _export_links(bvid: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """data/reports/ 下 report_{bvid}_*.csv/.json 下载链接，分（最新一组, 历史组）。

    文件名含时间戳，按文件名倒序即时间倒序；每种格式第一个为最新，其余收进历史折叠块。"""
    latest, history = [], []
    for ext in ("csv", "json"):
        # bvid 来自路由参数：glob.escape 防通配符注入匹配到他视频文件
        files = sorted(glob.glob(os.path.join(REPORT_DIR, f"report_{glob.escape(bvid)}_*.{ext}")),
                       reverse=True)
        for i, f in enumerate(files):
            (latest if i == 0 else history).append((os.path.basename(f), ext.upper()))
    return latest, history


def _hot_comments(bvid: str, aid: int | None) -> dict:
    """高回复评论页数据（潜在争执热点）：回复数 >= HOT_COMMENT_MIN_REPLIES 的主评论，
    按回复数降序；子回复全量取出（不截断），附 parent_rpid 供前端构建回复树、
    problem 供问题评论标注。

    昵称优先级：comments.uname（采集时落库）→ users.name（已分析用户）→ UID 回退。
    附 up_mid（视频 UP 主 uid），供渲染层标注「UP主」；「楼主」按主评论 uid 标注。

    返回 {"legacy": bool, "items": [...], "total_replies": int, "up_mid": int}；
    legacy=True 表示该视频评论回复数未留存（旧版本分析），页面显示对应空态。"""
    with closing(get_db()) as conn:
        if conn.execute("SELECT 1 FROM comments WHERE bvid = ? LIMIT 1", (bvid,)).fetchone() is None:
            return {"legacy": True, "items": [], "total_replies": 0, "up_mid": 0}
        # 视频 UP 主 uid（标注用；解析失败回退 0 即不标注）
        vrow = conn.execute("SELECT video_info_json FROM videos WHERE bvid = ?", (bvid,)).fetchone()
        try:
            up_mid = int((json.loads(vrow["video_info_json"]).get("owner") or {}).get("mid", 0)) if vrow else 0
        except Exception:
            up_mid = 0
        rows = conn.execute('''
            SELECT c.rpid, c.uid, c.uname, c.content, c.ctime, c.like, c.reply_count, c.problem, u.name
            FROM comments c LEFT JOIN users u ON u.uid = c.uid
            WHERE c.bvid = ? AND c.is_sub = 0 AND c.reply_count >= ?
            ORDER BY c.reply_count DESC, c.like DESC
            LIMIT ?
        ''', (bvid, HOT_COMMENT_MIN_REPLIES, HOT_COMMENT_MAX_SHOW)).fetchall()

        def _name(uname, db_name, uid) -> str:
            s = uname or db_name or f"UID:{uid}"
            return _mask_name(s) if _mask_on() else s   # 隐藏信息：昵称只留末字

        items = []
        for r in rows:
            # 完整回复树（不截断）：按发送时间升序，按 parent_rpid 嵌套缩进
            subs = conn.execute('''
                SELECT c.rpid, c.uid, c.uname, c.content, c.like, c.parent_rpid, c.problem, u.name
                FROM comments c LEFT JOIN users u ON u.uid = c.uid
                WHERE c.bvid = ? AND c.is_sub = 1 AND c.root_rpid = ?
                ORDER BY c.ctime ASC
            ''', (bvid, r["rpid"])).fetchall()
            items.append({
                "rpid": r["rpid"],
                "uid": r["uid"],
                "name": _name(r["uname"], r["name"], r["uid"]),
                "content": r["content"],
                "ctime": r["ctime"],
                "like": r["like"],
                "problem": r["problem"] or "",
                "reply_count": r["reply_count"],
                "subs": [{"rpid": s["rpid"], "uid": s["uid"],
                          "name": _name(s["uname"], s["name"], s["uid"]),
                          "content": s["content"], "like": s["like"],
                          "parent_rpid": s["parent_rpid"],
                          "problem": s["problem"] or ""} for s in subs],
            })
    return {"legacy": False, "items": items, "up_mid": up_mid,
            "total_replies": sum(i["reply_count"] for i in items)}


def _problem_chip(problem: str, marked: bool = False) -> str:
    """问题评论标注（LLM 判定类别，分色 chip 与问题弹幕榜视觉一致）；
    marked=True 表示已被人工标记误报（划线样式，不计入聚合）"""
    if not problem:
        return ""
    color = PROBLEM_CATEGORY_COLORS.get(problem, "#999999")
    cls = "problem-chip fp-chip-marked" if marked else "problem-chip"
    tip = "已被人工标记为误报（不计入聚合，点右侧「撤销误报」恢复）" if marked else "LLM 判定的问题评论类别"
    return (f'<span class="{cls}" style="background:{color}" '
            f'title="{tip}">{esc(problem)}</span>')


def _role_badges(uid: int, root_uid: int, up_mid: int) -> str:
    """评论区身份标注：UP主（视频作者）/ 楼主（主评论作者，root_uid=0 时不标注）"""
    badges = ""
    if up_mid and uid == up_mid:
        badges += '<span class="role-badge role-up">UP主</span>'
    if root_uid and uid == root_uid:
        badges += '<span class="role-badge role-op">楼主</span>'
    return badges


def _reply_tree_html(subs: list[dict], root_uid: int, up_mid: int, fp_cmt: set[str] = frozenset()) -> str:
    """把扁平子回复列表按 parent_rpid 嵌套成回复树 HTML（<ul> 嵌套即缩进层级）。

    楼主（root_uid）/UP主（up_mid）出现时加身份徽标；问题评论带误报标记按钮（P2-a）。
    parent_rpid 指向的父级不在已采集集合内（补采截断/旧库无该字段为 0）时挂到主楼下，
    保证不丢任何一条回复。脏数据 parent 成环时：visited 集合防重复渲染 +
    REPLY_TREE_MAX_DEPTH 深度上限，环内从 roots 不可达的节点平铺挂到末尾兜底（不静默丢弃）。"""
    nodes = {s["rpid"]: s for s in subs}
    children: dict[int, list[dict]] = {}
    roots = []
    for s in subs:
        p = s["parent_rpid"]
        if p and p in nodes and p != s["rpid"]:
            children.setdefault(p, []).append(s)
        else:
            roots.append(s)   # 直接回复主楼（parent=0/=root）、父级缺失或自指

    rendered: set = set()   # 已渲染 rpid：环上二次到达直接跳过（首次到达的实例已渲染）

    def render(node: dict, depth: int) -> str:
        if node["rpid"] in rendered:
            return ""
        rendered.add(node["rpid"])
        kids = ""
        if depth < REPLY_TREE_MAX_DEPTH:
            kids = "".join(render(ch, depth + 1) for ch in children.get(node["rpid"], []))
        sub_ul = f"<ul>{kids}</ul>" if kids else ""
        marked = str(node["rpid"]) in fp_cmt
        fp = _fp_btn("cmt", node["rpid"], marked) if node["problem"] else ""
        return (f'<li><div class="hot-reply"><span class="hot-sub-author">{esc(node["name"])}</span>'
                f'{_role_badges(node["uid"], root_uid, up_mid)}'
                f'：{esc(_mask_reply_text(node["content"]))} <span class="dm-time">👍{node["like"]:,}</span>'
                f'{_problem_chip(node["problem"], marked)}{fp}</div>{sub_ul}</li>')

    html = "".join(render(s, 0) for s in roots)
    # 环内节点兜底：parent 指针成环导致从 roots 不可达的节点，平铺挂到末尾不静默丢弃
    leftover = [s for s in subs if s["rpid"] not in rendered]
    if leftover:
        html += "".join(render(s, 0) for s in leftover)
    return "<ul class=\"hot-tree\">" + html + "</ul>"


def _attack_focus_tab_html(bvid: str, fp_cmt: set[str] = frozenset()) -> str:
    """「争执焦点」标签页（独立成页，不与问题评论榜混排——连线需要宽敞的两列排布）"""
    return _attack_focus_html(_attack_focus(bvid, fp_cmt)) \
        or '<p class="empty-note">本视频无问题评论攻击边</p>'


def _cmt_war_html(bvid: str, aid: int | None, fp_cmt: set[str] = frozenset()) -> str:
    """「问题评论榜」标签页（独立成页）：全部问题评论按热度加权排序，楼中楼附父评原文。
    （争执焦点已独立为「争执焦点」标签页，见 _attack_focus_tab_html）"""
    up_mid = 0
    with closing(get_db()) as conn:
        vrow = conn.execute("SELECT video_info_json FROM videos WHERE bvid = ?", (bvid,)).fetchone()
    try:
        up_mid = int((json.loads(vrow["video_info_json"]).get("owner") or {}).get("mid", 0)) if vrow else 0
    except Exception:
        up_mid = 0
    items, marked, _total = _problem_comment_board(bvid, fp_cmt)
    return _problem_comment_board_html(items, marked, aid, up_mid) \
        or '<p class="empty-note">本视频无问题评论命中</p>'


def _group_wordcloud(texts: list[str], top: int = 30) -> list[list]:
    """评论组词云数据：简单分词（2+连续中文字）词频 Top N；过滤只出现1次的噪声，
    全部只出现一次时退化为 Top N（保证有内容可看）。返回 [[词, 次数], ...]"""
    cnt: Counter = Counter()
    for t in texts:
        t = (t or "").strip()
        # 楼中楼原文带「回复 @xx :」前缀，先剥离避免「回复」成为噪声高频词
        t = re.sub(r"^回复\s*@[^\s:：]+\s*[:：]", "", t)
        # 剥离B站表情占位符（[笑哭]/[给心心]/[装扮名_xxx] 等方括号段），否则分词产出
        # 「笑哭」「应援装扮」这类表情噪声词压过真实话题词
        t = re.sub(r"\[[^\[\]]{1,30}\]", " ", t)
        # 剥离正文中的 @提及（含「回复 @xx :」前缀以外的裸 @昵称），昵称不是话题
        t = re.sub(r"@[^\s@:：，,\[\]]+", " ", t)
        cnt.update(w for w in _tokenize(t) if w != "回复")
    words = [(w, c) for w, c in cnt.most_common() if c >= 2][:top]
    if len(words) < 8:
        # 高频词太少的组用词频1的词补齐到至少8个，避免词云只有一两个词
        seen = {w for w, _ in words}
        words += [(w, c) for w, c in cnt.most_common() if c < 2 and w not in seen][:8 - len(words)]
    return [[w, c] for w, c in words]


def _hot_comments_html(bvid: str, aid: int | None, fp_cmt: set[str] = frozenset()) -> str:
    """高回复评论标签页 HTML（服务端渲染，spec：单独成页的潜在争执热点）

    回复树完整展示不截断：默认折叠到固定高度，「展开/折叠」按钮切换（hotToggle）。
    树内直接显示用户名（comments.uname 落库），楼主/UP主出现时分色标注；
    问题评论带误报标记按钮（P2-a），已标记误报的不计入统计。
    （争执焦点/问题评论榜已各自独立成页，见 _attack_focus_tab_html / _cmt_war_html）"""
    data = _hot_comments(bvid, aid)
    up_mid = data.get("up_mid", 0)
    if data["legacy"]:
        return ('<p class="empty-note">该视频为旧版本分析，评论回复数未留存；'
                '点「🔄 重新生成报告」重跑后可查看高回复评论</p>')
    if not data["items"]:
        return (f'<p class="empty-note">本视频没有回复数 ≥ {HOT_COMMENT_MIN_REPLIES} 的评论'
                f'（阈值 HOT_COMMENT_MIN_REPLIES 可在 config.py 调整）</p>')
    items_html = []
    problem_total = 0
    for it in data["items"]:
        date = datetime.fromtimestamp(it["ctime"]).strftime("%Y-%m-%d") if it["ctime"] else ""
        # 误报标记（P2-a）：已标记的不计入问题评论统计，chip 划线并带撤销按钮
        it_marked = str(it["rpid"]) in fp_cmt
        problem_total += (1 if it["problem"] and not it_marked else 0) + sum(
            1 for s in it["subs"] if s["problem"] and str(s["rpid"]) not in fp_cmt)
        if it["subs"]:
            tree_html = _reply_tree_html(it["subs"], it["uid"], up_mid, fp_cmt)
            subs_html = f'''
                <div class="hot-subs collapsed">
                    <div class="hot-sub-label">回复树（已采集 {len(it["subs"]):,} 条）</div>
                    {tree_html}
                </div>
                <button class="hot-toggle" onclick="hotToggle(this)">展开全部回复 ▾</button>'''
        else:
            subs_html = '<div class="hot-subs"><div class="hot-sub-label ov-none">暂无子回复被采集</div></div>'
        origin = (f'<a class="hot-origin" href="https://www.bilibili.com/video/av{aid}#reply{it["rpid"]}" '
                  f'target="_blank" rel="noopener">去B站围观 ↗</a>') if aid else ""
        # 评论组讨论主题词云（主楼+全部回复的词频，悬停静止弹窗用）。
        # 双引号属性 + esc：不依赖"分词只产出纯中文词"的隐式契约（未来分词支持
        # 含引号的英文词时单引号属性即破，此处按常规属性转义口径写）
        wc = _group_wordcloud([it["content"]] + [s["content"] for s in it["subs"]])
        wc_attr = f' data-wc="{esc(json.dumps(wc, ensure_ascii=False))}"' if wc else ""
        items_html.append(f'''
            <div class="hot-item"{wc_attr}>
                <div class="hot-head">
                    <a class="hot-author" href="/user/{esc(it["uid"])}"
                       title="查看该用户在已分析视频中的互动时间线">{esc(it["name"])}</a>
                    {_role_badges(it["uid"], 0, up_mid)}
                    <span class="hot-badge">💬 {it["reply_count"]:,} 条回复</span>
                    <span class="dm-time">👍{it["like"]:,} · {date}</span>
                    {_problem_chip(it["problem"], it_marked)}
                    {_fp_btn("cmt", it["rpid"], it_marked) if it["problem"] else ""}
                    {origin}
                </div>
                <div class="hot-content">{esc(_mask_reply_text(it["content"]))}</div>
                {subs_html}
            </div>''')
    problem_note = f' · 其中问题评论 {problem_total} 条已标注' if problem_total else ""
    return f'''
        <div class="hot-note">💥 回复数特别多的评论往往意味着争执：以下为该视频回复数 ≥
            {HOT_COMMENT_MIN_REPLIES} 的评论（按回复数降序，最多 {HOT_COMMENT_MAX_SHOW} 条）。
            悬停在某个评论组上静止片刻，可弹出该组讨论主题词云。</div>
        <div class="hot-stats">共 {len(data["items"])} 条高回复评论 · 合计 {data["total_replies"]:,} 条回复{problem_note}</div>
        <div class="hot-list">{"".join(items_html)}</div>'''


def _user_timeline(uid: int) -> dict:
    """用户互动时间线数据（/user/<uid> 页）：该用户在全部已分析视频中的弹幕/评论足迹，
    按最近互动时间倒序——「近期在哪些视频上有互动」的直接答案。

    与卡片「其他视频足迹」（按活跃度、限 5 个、无时间）互补而非重复：
    时间线覆盖全部视频、带发送时间、含评论-only 用户（senders 无行只有 comments 行）。
    弹幕计数取 senders.danmaku_count 与 danmaku 表行数两者较大值（旧版本视频只有前者）；
    时间戳只在新版 danmaku 表有，旧视频 last_ts=0（排序垫底并标注时间未留存）。"""
    with closing(get_db()) as conn:
        u = conn.execute("SELECT name, level FROM users WHERE uid = ?", (uid,)).fetchone()
        name = (u["name"] if u and u["name"] else f"UID:{uid}")
        level = (u["level"] or 0) if u else 0

        # 弹幕侧：senders 表覆盖全部解析用户（含旧版本视频）；时间戳与样本只在 danmaku 表有
        dm_agg: dict[str, dict] = {}
        for r in conn.execute(
                "SELECT bvid, SUM(danmaku_count) AS cnt FROM senders WHERE uid = ? GROUP BY bvid",
                (uid,)).fetchall():
            dm_agg[r["bvid"]] = {"dm_count": r["cnt"] or 0, "dm_last": 0}
        for r in conn.execute('''
                SELECT d.bvid, MAX(d.timestamp) AS last_ts, COUNT(*) AS cnt
                FROM senders s JOIN danmaku d ON d.bvid = s.bvid AND d.mid_hash = s.mid_hash
                WHERE s.uid = ? GROUP BY d.bvid
        ''', (uid,)).fetchall():
            ent = dm_agg.setdefault(r["bvid"], {"dm_count": 0, "dm_last": 0})
            ent["dm_last"] = r["last_ts"] or 0
            ent["dm_count"] = max(ent["dm_count"], r["cnt"])

        # 评论侧（comments 表；评论-only 用户只有这一侧数据）
        cmt_agg: dict[str, dict] = {}
        for r in conn.execute(
                "SELECT bvid, COUNT(*) AS cnt, MAX(ctime) AS last_ts FROM comments WHERE uid = ? GROUP BY bvid",
                (uid,)).fetchall():
            cmt_agg[r["bvid"]] = {"cmt_count": r["cnt"] or 0, "cmt_last": r["last_ts"] or 0}

        bvids = set(dm_agg) | set(cmt_agg)
        result = {"uid": uid, "name": name, "level": level, "items": [],
                  "total_dm": sum(e["dm_count"] for e in dm_agg.values()),
                  "total_cmt": sum(e["cmt_count"] for e in cmt_agg.values()),
                  "total_videos": len(bvids), "more": 0}
        if not bvids:
            return result

        titles: dict[str, str] = {}
        multi_pv: dict[str, bool] = {}   # 多分P 视频：样本 mm:ss 必须标出分P 才可解释
        for r in conn.execute(
                f"SELECT bvid, title, video_info_json FROM videos "
                f"WHERE bvid IN ({','.join('?' * len(bvids))})",
                tuple(sorted(bvids))).fetchall():
            titles[r["bvid"]] = r["title"] or ""
            try:
                multi_pv[r["bvid"]] = len((json.loads(r["video_info_json"] or "{}")
                                           .get("pages") or [])) > 1
            except Exception:
                multi_pv[r["bvid"]] = False
        mid_hashes_by_bvid: dict[str, list[str]] = {}
        for r in conn.execute("SELECT bvid, mid_hash FROM senders WHERE uid = ?", (uid,)).fetchall():
            mid_hashes_by_bvid.setdefault(r["bvid"], []).append(r["mid_hash"])

        ranked = sorted(bvids, key=lambda b: max(
            dm_agg.get(b, {}).get("dm_last", 0), cmt_agg.get(b, {}).get("cmt_last", 0)), reverse=True)
        kept, rest = ranked[:USER_TIMELINE_MAX_VIDEOS], ranked[USER_TIMELINE_MAX_VIDEOS:]
        result["more"] = len(rest)

        for b in kept:
            dm = dm_agg.get(b, {})
            cm = cmt_agg.get(b, {})
            dm_samples: list[dict] = []
            for mh in mid_hashes_by_bvid.get(b, []):
                dm_samples.extend(
                    {"content": r["content"], "ts": r["timestamp"], "vt": r["time"],
                     # 多分P 视频：time 是分P 内相对秒数，带上分P 才能解释 mm:ss
                     "vp": int(r["page"] or 1) if multi_pv.get(b) else 0}
                    for r in conn.execute(
                        "SELECT content, timestamp, time, page FROM danmaku "
                        "WHERE bvid = ? AND mid_hash = ? ORDER BY timestamp DESC LIMIT ?",
                        (b, mh, USER_TIMELINE_SAMPLES)).fetchall())
                if len(dm_samples) >= USER_TIMELINE_SAMPLES:
                    break
            cmt_samples = [{"content": r["content"], "ts": r["ctime"]} for r in conn.execute(
                "SELECT content, ctime FROM comments WHERE bvid = ? AND uid = ? "
                "ORDER BY ctime DESC LIMIT ?", (b, uid, USER_TIMELINE_SAMPLES)).fetchall()]
            result["items"].append({
                "bvid": b, "title": titles.get(b) or b,
                "dm_count": dm.get("dm_count", 0), "dm_samples": dm_samples[:USER_TIMELINE_SAMPLES],
                "cmt_count": cm.get("cmt_count", 0), "cmt_samples": cmt_samples,
                "last_ts": max(dm.get("dm_last", 0), cm.get("cmt_last", 0)),
            })
    return result



# ========== 路由 ==========

@app.route("/")
@_db_guard
def index():
    """首页：已分析视频列表（标题/BV号/时长/播放量/分析时间/弹幕数/画像人数/高/中风险人数）。

    搜索/列头排序/分页由 static/index.js 前端实现：每行带 data-title（标题+BV号小写）、
    data-time/data-dm/data-profiles 供排序；超 20 条分页。
    数据库锁定（分析任务写入期）由 _db_guard 降级为 503 友好页。"""
    with closing(get_db()) as conn:
        # 刷屏高/中统计以库内原始值为准：kind=spam 的发送者级人工误报只在报告页展示层降级
        # 只有目标采样的元信息不生成普通视频报告入口，独立目标入口仍保留。
        rows = conn.execute(f'''
            SELECT v.bvid, v.title, v.created_at, v.duration, v.view_count,
                   (SELECT COUNT(*) FROM danmaku d WHERE d.bvid = v.bvid) AS dm_count,
                   (SELECT COUNT(DISTINCT s.uid) FROM senders s
                    WHERE s.bvid = v.bvid AND s.uid IS NOT NULL) AS profile_count,
                   (SELECT COUNT(*) FROM senders s WHERE s.bvid = v.bvid AND s.spam_level = '高') AS spam_high,
                   (SELECT COUNT(*) FROM senders s WHERE s.bvid = v.bvid AND s.spam_level = '中') AS spam_mid
            FROM videos v WHERE {NORMAL_REPORT_PREDICATE} ORDER BY v.created_at DESC
        ''').fetchall()
    items = "".join(f'''<tr data-title="{esc(((r["title"] or "") + " " + r["bvid"]).lower())}"
        data-time="{esc(r["created_at"])}" data-dm="{r["dm_count"]}" data-profiles="{r["profile_count"]}">
        <td><a href="/video/{esc(r["bvid"])}">{esc(r["title"] or r["bvid"])}</a></td>
        <td>{esc(r["bvid"])}</td>
        <td>{_fmt_duration(r["duration"])}</td>
        <td>{(r["view_count"] or 0):,}</td>
        <td>{esc(r["created_at"])}</td>
        <td>{r["dm_count"]:,}</td>
        <td>{r["profile_count"]}</td>
        <td>{r["spam_high"]} / {r["spam_mid"]}</td>
    </tr>''' for r in rows)
    body = items or '<tr><td colspan="8" class="empty-note">暂无已分析视频，先运行 python run.py &lt;BV号&gt;</td></tr>'

    # 跨视频重叠用户面板：在多个已分析视频中都出现过的发送者（水军/情绪带节奏视角）
    overlaps = _cross_video_overlaps()
    overlap_html = ""
    if overlaps:
        # 每个视频一个 <details>：标题+弹幕/评论计数为摘要，展开可见 TA 在该视频的
        # 弹幕/评论明细样本（含问题评论标注）；旧版本分析标注明细未留存
        def _xv_video_block(v: dict) -> str:
            dm_lis = "".join(f"<li>{esc(c)}</li>" for c in v["dm_samples"])
            if not dm_lis:
                dm_lis = ('<li class="ov-none">弹幕明细未留存（该视频为旧版本分析）</li>'
                          if v["legacy"] and v["dm_count"] else '<li class="ov-none">无弹幕样本</li>')
            cmt_lis = "".join(
                f'<li>{esc(c["content"])} <span class="dm-time">👍{c["like"]:,}</span>'
                f'{_problem_chip(c["problem"])}</li>' for c in v["cmt_samples"])
            if not cmt_lis:
                cmt_lis = ('<li class="ov-none">评论未留存（该视频为旧版本分析）</li>'
                           if v["legacy"] else '<li class="ov-none">无评论</li>')
            return f'''<details class="xv-detail">
                <summary><a href="/video/{esc(v["bvid"])}">《{esc(v["title"])}》</a>{f'<span class="xv-counts">· {esc(v["pubdate"])}</span>' if v["pubdate"] else ""}
                    <span class="xv-counts">弹幕 {v["dm_count"]:,} · 评论 {v["cmt_count"]:,}</span></summary>
                <div class="xv-sec">💬 弹幕样本</div><ul class="xv-list">{dm_lis}</ul>
                <div class="xv-sec">📝 评论样本</div><ul class="xv-list">{cmt_lis}</ul>
            </details>'''

        rows_html = "".join(f'''<tr>
            <td><a href="/user/{esc(o["uid"])}">{esc(o["name"])}</a></td>
            <td>{o["total_dm"]:,}</td>
            <td class="xv-videos">{"".join(_xv_video_block(v) for v in o["videos"])}</td>
        </tr>''' for o in overlaps)
        overlap_html = f'''
    <div class="xv-panel">
        <h2>🔁 跨视频重叠用户（{len(overlaps)} 人）</h2>
        <table class="video-table xv-table">
            <thead><tr><th>用户</th><th>总弹幕数</th><th>出现的视频（点条目展开明细）</th></tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
    </div>'''

    # 跨视频屏蔽列表导出：B站用户屏蔽全站生效，「多个视频都命中」的惯犯优先屏蔽。
    # 面板与单视频导出同格式（REPORT_CSS 内联已带 .blk-* 样式）；有已分析视频即展示
    cross_blk_html = ""
    if rows:
        cross_blk_html = f'''
    <div class="xv-panel">
        <h2>🚫 跨视频屏蔽列表导出</h2>
        <details class="blk-export">
            <summary class="filter-btn"
                     title="把在多个视频都命中问题标准的发送者导出为B站播放器「弹幕屏蔽列表」
可导入的用户屏蔽 JSON——跨视频惯犯最值得屏蔽">展开导出面板</summary>
            <div class="blk-panel">
                <label><input type="checkbox" id="blkCringe" checked> 问题弹幕发送者</label>
                <label><input type="checkbox" id="blkSpam" checked> 高/中风险刷屏</label>
                <label><input type="checkbox" id="blkCmt"> 问题评论作者</label>
                <label class="blk-sub"><input type="checkbox" id="blkLowconf"
                        title="CRC32 反查的 UID 可能张冠李戴，勾选即接受误屏蔽风险"> 含低置信度UID（可能误屏蔽，慎选）</label>
                <label class="blk-sub">至少在 <input type="number" id="blkMinVideos" class="blk-num"
                        min="1" max="50" value="{CROSS_VIDEO_MIN_VIDEOS}"> 个视频命中</label>
                <label class="blk-sub">上限 <input type="number" id="blkMax" class="blk-num"
                        min="1" max="2000" value="{BLOCKLIST_MAX_UIDS}"> 条</label>
                <button class="filter-btn" onclick="blkExportCross()">⬇ 导出 JSON</button>
                <p class="blk-note">与单视频导出同一导入方法（网页播放器 → 弹幕设置 → 弹幕屏蔽
                列表 → 右键导入）；排序以「命中视频数」为首位——在多个视频都发问题弹幕/刷屏
                的人是惯犯，屏蔽性价比最高。库内共 {len(rows)} 个已分析视频。</p>
            </div>
        </details>
    </div>'''
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="referrer" content="no-referrer">
<title>B站弹幕用户画像分析 - 视频列表</title>
<style>{REPORT_CSS}</style>
<link rel="stylesheet" href="/static/index.css">
</head>
<body>
<div class="container">
    <div class="header"><h1>🎬 B站弹幕用户画像分析</h1><div class="meta">已分析视频列表</div></div>
    <div class="idx-controls">
        <input id="idxSearch" class="search-input" placeholder="搜索标题 / BV号..." aria-label="搜索视频标题或BV号">
        <a class="pager-btn target-entry" href="/targets">目标弹幕分析</a>
    </div>
    <table class="video-table">
        <thead><tr><th>标题</th><th>BV号</th><th>时长</th><th>播放量</th>
            <th data-sort="time">分析时间</th><th data-sort="dm">弹幕数</th><th data-sort="profiles">画像人数</th>
            <th>高/中风险</th></tr></thead>
        <tbody id="videoTbody">{body}</tbody>
    </table>
    <div class="idx-pager">
        <button id="idxPrev" class="pager-btn">上一页</button>
        <span id="idxPageInfo"></span>
        <button id="idxNext" class="pager-btn">下一页</button>
    </div>
    {cross_blk_html}
    {overlap_html}
</div>
<script src="/static/index.js"></script>
</body>
</html>'''


@app.route("/targets")
@_db_guard
def target_list_page():
    """独立目标报告列表，从首页搜索框右侧进入。"""
    return render_target_list_page(list_target_analyses(limit=getattr(_config, "TARGET_REPORT_LIST_LIMIT", 50)))


def _low_conf_tab_html(bvid: str) -> tuple[str, int]:
    """「低置信度」标签页：解析出 UID 但置信度为「低」的发送者（多为 CRC32 碰撞疑似误识别）。

    自动流水线对低置信度用户不做深度采集/画像（main.py 阶段5 只收高/中置信度），
    这里集中列出其 UID 与具体弹幕内容供人工复核；确认无误的可到「弹幕浏览器」
    勾选对应 mid_hash 手动强制分析（无视置信度）。返回 (HTML, 人数)。"""
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT mid_hash, uid, method, danmaku_count, contents_json, spam_level, resolution_json "
            "FROM senders WHERE bvid = ? AND (confidence = '低' OR method IN ('候选歧义', '待验证')) "
            "ORDER BY danmaku_count DESC", (bvid,)).fetchall()
    if not rows:
        return ('<p class="empty-note">无低置信度候选或待复核条目</p>', 0)
    mask = _mask_on()
    items = []
    for r in rows:
        uid = r["uid"]
        uid_disp = _mask_uid(uid) if mask else str(uid)
        record = json.loads(r["resolution_json"] or "{}")   # 先解析，链接判定要用它
        # 只有能确认账号存在的 UID 才给跳转——报告里不得出现点进去 404 的账号
        if uid is None:
            uid_html = '<span>尚未确定 UID</span>'
        elif uid_linkable({"uid": uid, "resolve_method": r["method"], "_resolution": record}):
            uid_html = f'<a href="https://space.bilibili.com/{uid}" target="_blank" rel="noopener">UID:{esc(uid_disp)}</a>'
        else:
            uid_html = f'<span title="存在性未确认或核验已过期，不提供跳转">UID:{esc(uid_disp)}</span>'
        candidate_html = "、".join(esc(_mask_uid(u) if mask else str(u)) for u in visible_candidate_uids({"mid_hash": r["mid_hash"], "resolution": record}))
        reason_html = f'<div class="chart-hint">{esc(record.get("reason", ""))}'
        if candidate_html:
            reason_html += f' · 候选（未确认身份）：{candidate_html}'
        reason_html += '</div>'
        try:
            contents = json.loads(r["contents_json"] or "[]")
        except Exception:
            contents = []
        cnt = Counter(c for c in contents if c)
        dm_html = "".join(
            f'<li>{esc(c)}' + (f' <span class="tl-time">×{n}</span>' if n > 1 else "") + "</li>"
            for c, n in cnt.most_common(15))
        if len(cnt) > 15:
            dm_html += f'<li class="ov-none">……另有 {len(cnt) - 15} 条不同内容</li>'
        spam = r["spam_level"] or ""
        spam_badge = f'<span class="spam-badge spam-{esc(spam)}">刷屏:{esc(spam)}</span>' if spam and spam != "低" else ""
        items.append(f'''<div class="af-item">
            <div class="af-line">
                {uid_html}
                <span class="hot-badge">{r["danmaku_count"]} 条弹幕</span>
                <span class="method-badge" title="UID 解析方式">{esc(r["method"] or "")}</span>
                <span class="risk-badge" title="低置信度解析存在 CRC32 撞库误识别风险">可能误识别</span>
                {spam_badge}
                <span class="uid">mid_hash:{esc(r["mid_hash"])}</span>
            </div>
            {reason_html}
            <ul class="ov-list">{dm_html or '<li class="ov-none">弹幕内容未留存</li>'}</ul>
        </div>''')
    head = (f'<div class="chart-hint" style="margin:4px 0 10px">以下 {len(rows)} 条为低置信度候选或待复核记录。'
            f'候选不代表身份确认；存在歧义或未完成验证的发送者保留匿名，不自动采集或屏蔽。</div>')
    return head + "".join(items), len(rows)


def _crc_candidate_data(bvid: str, target_record: dict | None = None,
                        include_resolved: bool = False) -> tuple[dict, list[dict]]:
    """按匿名 hash 读取问题证据和全部候选，GET 不调用破解、B站或 LLM。"""
    rows = load_danmaku(bvid)
    senders = (target_record["result"].get("senders", []) if target_record else load_senders(bvid))
    senders = [dict(s) for s in senders]
    for sender in senders:
        if is_pure_crc(sender):
            limit = sender.get("resolution", {}).get("max_search", _config.MITM_MAX_UID)
            cached = load_uid_candidates(sender["mid_hash"], limit, "mitm-v1")
            sender["_candidate_complete"] = cached is not None
            if cached is not None:
                sender["candidates"] = sorted(set(cached) | set(candidate_uids(sender)))
    if target_record and not rows:
        rows = target_record["result"].get("matches", [])
    problems = load_danmaku_problems(bvid)
    if not problems and rows:
        groups = {}
        for dm in rows:
            groups.setdefault(dm["mid_hash"], {"contents": []})["contents"].append(dm["content"])
        problems = load_cached_danmaku_problems(rows, groups, bvid)
    uids = sorted({u for s in senders if is_pure_crc(s) for u in candidate_uids(s)})
    # 展示用"全部结论"（含已过期）：过期只标待复核，绝不把"已确认不存在"当成未知，
    # 否则它会被重新渲染成可点击名片 → 点进去 404。
    verifications = load_uid_verify_verdicts(uids, getattr(_config, "UID_VERIFY_EXISTS_TTL", 604800),
                                             getattr(_config, "UID_VERIFY_MISSING_TTL", 3600))
    groups = build_crc_groups(senders, rows, problems, load_false_positives(bvid), verifications,
                              target_record["result"].get("matches", []) if target_record else None,
                              load_uid_verify_times(uids), include_resolved=include_resolved)
    record = {"bvid": bvid, "result": {"senders": senders, "matches": rows,
                                      "video_info": (target_record["result"].get("video_info", {})
                                                     if target_record else load_video_info(bvid) or {})}}
    if target_record:
        record["id"] = target_record["id"]
    return record, groups


def _crc_candidate_tab_html(bvid: str, target_record: dict | None = None) -> tuple[str, int]:
    record, groups = _crc_candidate_data(bvid, target_record)
    endpoint = (f'/api/target/{target_record["id"]}/candidate-space' if target_record
                else f'/api/video/{bvid}/candidate-space')
    html = render_crc_groups(groups, endpoint, _mask_on(), _mask_name, _mask_uid,
                             cached_assessments(record), render_space_assessment, bool(target_record))
    return html, len(groups)


@app.route("/video/<bvid>")
@_db_guard
def video_page(bvid: str):
    """报告页：常规八个标签页及低置信度后的纯破解候选页。

    整页 HTML 按 bvid 内存缓存（_PAGE_CACHE）：避免每次刷新重跑 _load_profiles/
    _attach_other_videos 逐用户查询；job（手动分析/重新生成）完成或删除时主动失效，
    且每次请求比对数据指纹，外部进程（run.py）落库导致的变化也能检出。
    数据库锁定（分析任务写入期）由 _db_guard 降级为 503 友好页。"""
    if not has_normal_report(bvid) and list_target_analyses(bvid, limit=1):
        return redirect("/targets")
    page_fp = _page_fingerprint(bvid)
    mask = _mask_on()   # 隐藏信息开关：开/关两版页面分开缓存，防串味
    cached_html = _page_cache_lookup(bvid, mask, page_fp)
    if cached_html is not None:
        return cached_html

    row = _load_video_row(bvid)
    if row is None:
        abort(404)
    try:
        video_info = json.loads(row["video_info_json"]) if row["video_info_json"] else {}
    except Exception:
        video_info = {}
    title = video_info.get("title") or row["title"] or bvid

    profiles = _load_profiles(bvid)
    _attach_other_videos(bvid, profiles)   # 跨视频足迹（渲染期注入，不落库）
    # 误报标记（P2-a）：弹幕侧按内容从 cringe 聚合扣除（用户疑似分随之降级）；
    # 评论侧传入高回复页（争执焦点/问题评论榜/回复树渲染时剔除或划线）
    fp = load_false_positives(bvid)
    fp_dm = {t for k, t in fp if k == "dm"}
    fp_cmt = {t for k, t in fp if k == "cmt"}
    fp_dm_used = _apply_danmaku_fp(profiles, fp_dm)
    # 刷屏判定人工误报（kind=spam，粒度=该视频该发送者）：展示层降级为低风险，
    # 统计图/筛选/排序随之不计入（与 dm/cmt 误报同口径），库内原始值不动
    _apply_spam_fp(profiles, {t for k, t in fp if k == "spam"})
    profiles = sort_profiles_by_risk(profiles)
    stats = generate_summary_stats(profiles)
    chart = generate_chart_data(profiles)
    cards_html = "".join(generate_user_card(p, mask=mask, mask_name=_mask_name) for p in profiles)
    cards_html += _light_user_cards(bvid, profiles, mask, fp)
    cards_html = cards_html or '<p class="empty-note">暂无已解析用户数据</p>'
    hot_tab = _hot_comments_html(bvid, row["aid"], fp_cmt)   # 高回复评论页（潜在争执热点）
    attack_tab = _attack_focus_tab_html(bvid, fp_cmt)        # 争执焦点页（谁攻击谁+连线）
    cmtwar_tab = _cmt_war_html(bvid, row["aid"], fp_cmt)     # 问题评论榜（热度加权）
    board_html = (generate_cringe_board(profiles, fp_renderer=lambda c: _fp_btn("dm", c, False))
                  or '<p class="empty-note">本视频无问题弹幕命中</p>')
    board_html += _fp_dm_block(fp_dm_used)   # 已标记误报弹幕的撤销入口
    panel = _danmaku_panel_stats(bvid)
    density = _danmaku_density(bvid, row["duration"], video_info)  # 概览页弹幕密度时间轴
    density = _annotate_density_peaks(bvid, density, video_info)  # 峰值时段 AI 高能点标注（失败/无Key 静默降级）
    rq = _resolve_quality(bvid)                          # 概览页解析质量区块
    repeat_block = _repeat_events_block(bvid)            # 概览页群体复读事件区块（含全池分布自检）
    dm_attrs = _danmaku_attr_stats(bvid)                 # 弹幕属性分布（mode/color）
    lowconf_tab, lowconf_count = _low_conf_tab_html(bvid)  # 低置信度页（疑似误识别用户复核）
    crc_tab, crc_count = _crc_candidate_tab_html(bvid)

    # CSV/JSON 导出下载链接：默认只显示最新一组，历史导出收进 <details> 折叠块（spec 6）
    latest, history = _export_links(bvid)

    def _dl_link(fname: str, ext: str) -> str:
        return f'<a class="filter-btn" href="/download/{esc(fname)}">{esc(ext)} 下载</a>'

    links = " ".join(_dl_link(f, e) for f, e in latest)
    if history:
        links += (f' <details class="dl-history"><summary>历史导出（{len(history)} 个）</summary>'
                  f'{" ".join(_dl_link(f, e) for f, e in history)}</details>')

    ai_count = sum(1 for p in profiles if p.get("ai_deep") or p.get("ai_analysis"))
    lv5_count = sum(1 for p in profiles if p.get("level", 0) >= 5)

    # 弹幕覆盖率（阶段2历史合并时写入 video_info；旧数据没有则不显示）
    coverage = video_info.get("danmaku_coverage")
    coverage_line = ""
    if coverage:
        coverage_line = (f" · 弹幕覆盖: 实时池 {coverage['realtime']:,} + "
                         f"历史新增 {coverage['history_new']:,} = {coverage['merged']:,} 条")

    # 弹幕覆盖率说明条（spec 7）：明示数据边界——实时池容量有限、历史快照回溯窗口有限、
    # 已解析发送者为按兴趣分阈值入选的子集
    coverage_note = (
        f'<div class="coverage-note">📊 数据边界：实时弹幕池仅保留最近若干条（容量由B站限定，'
        f'超出部分被顶出）；历史弹幕按日快照回溯，最多 {HISTORY_MAX_MONTHS} 个月 / {HISTORY_MAX_DAYS} 天，'
        f'更早的弹幕不在统计范围内。已解析发送者是按兴趣分阈值入选的子集，非全部弹幕发送者。</div>'
    )

    # 地域分布并入「用户标签 Top10」卡片（同属用户特征 Top10 维度，charts-grid 4 卡→3 卡）；
    # 无属地数据时不渲染地域小标题+canvas，前端 report.js 对 regionChart 判空
    region_sub = ('<h4 class="dm-sub-h">地域分布 Top10</h4><canvas id="regionChart"></canvas>'
                  if chart["region_labels"] else "")

    # 弹幕密度时间轴（概览页，宽幅）：无全量弹幕数据（旧版本分析）或时长未知时不渲染；
    # 分P视频按分P分别建桶（弹幕 time 是各P内部时间，与全片累计时长不可混用），
    # 前端 report.js 负责分P切换与「点击柱条跳转该分P对应时段」（P1-a）
    density_canvas = ""
    if density:
        if density["multi"]:
            dense_hint = (f'（{len(density["pages"])} 个分P，横轴为各分P内部时间；'
                          f'点击柱条跳转该分P对应时段核验）')
        else:
            dense_hint = "（点击柱条跳转对应时段核验）"
        # 高能点标注存在时提示悬停可见（LLM 对峰值时段爆发原因的判断；不做柱色高亮）
        if any(pg.get("peaks") for pg in density["pages"]):
            dense_hint += "；AI 已标注高能点，悬停柱条可查看爆发原因"
        dense_pager = ('<div class="density-pager" id="densityPager"></div>'
                       if density["multi"] else "")
        dense_note = ""
        if density["multi"]:
            # 数据边界：历史快照按 cid 维度采集，多分P视频只采了 P1 的历史弹幕
            dense_note = ('<div class="density-note">分P视频：各分P弹幕时间相互独立，横轴为'
                          '该分P内部时间，各P弹幕量不可直接横向比较。历史弹幕快照按分P分别采集'
                          '（B站历史接口按 cid 维度）；早于本版本分析的报告里，其余分P可能只有'
                          '实时弹幕池数据，重跑分析可补齐。</div>')
        elif density["onepage_fallback"]:
            dense_note = ('<div class="density-note">本视频为多分P但缺少分P时长元信息，'
                          '时间轴按全片累计时长降级展示，各分P弹幕会挤在时间轴前段。</div>')
        density_canvas = (f'<div class="chart-card chart-wide"><h3>弹幕密度时间轴'
                          f'<span class="chart-hint">{dense_hint}</span></h3>'
                          f'{dense_pager}<canvas id="densityChart"></canvas>{dense_note}</div>')

    # 解析质量区块（概览页）：用户身份可信度——解析方式/置信度分布 + 碰撞风险人数
    rq_block = ""
    if rq:
        collision_cls = "rq-danger" if rq["collision"] else "rq-ok"
        rq_block = f'''
        <div class="rq-block">
            <h3>🔍 解析质量（用户身份可信度）</h3>
            <div class="rq-summary">已解析发送者 <b>{rq["resolved"]:,}</b> / 全部发送者
                <b>{rq["total_senders"]:,}</b> · hash 关联碰撞风险
                <b class="{collision_cls}">{rq["collision"]} 人</b> · 歧义 {rq["ambiguous"]} · 待验证 {rq["pending"]}
                · 评论明文作者 {rq["comment_authors"]}（另计）；候选分配不代表身份确认</div>
            <div class="charts-grid rq-charts">
                <div class="chart-card"><h3>解析方式分布</h3><canvas id="rqMethodChart"></canvas></div>
                <div class="chart-card"><h3>置信度分布</h3><canvas id="rqConfChart"></canvas></div>
            </div>
        </div>'''

    # 弹幕浏览器标签页：统计面板服务端渲染；表格容器由 Task 6 前端填充
    if panel["total"] == 0:
        # 旧数据兼容（spec 3）：历史视频 danmaku 表无数据
        danmaku_tab = '<p class="empty-note">该视频为旧版本分析，无全量弹幕数据，--force 重采后可浏览</p>'
    else:
        top10_html = "、".join(
            # js_json 输出 JS 字符串字面量 + esc 转义属性：esc 的 &#x27; 在 JS 字符串
            # 上下文中会被 HTML 解码回单引号截断，故不用 esc 包裹单引号写法
            f"""<a onclick="filterSender({esc(js_json(t["mid_hash"]))})">{esc(t["name"])}({t["count"]})</a>"""
            for t in panel["top10"])
        # 弹幕属性分布（mode/color 已入库）：颜色 Top12 色块并入「问题弹幕类别分布」卡片
        # （同属内容特征维度，合并展示）；模式分布单独成卡
        color_chips = ""
        mode_card = ""
        if dm_attrs:
            if dm_attrs["colors"]:
                # 颜色值过 _norm_color 白名单（#rrggbb）：esc 只防 HTML 逃逸、不防
                # CSS 声明注入（background:url(...)），与 api_danmaku 出口同口径
                color_chips = ('<h4 class="dm-sub-h">弹幕颜色 Top12</h4><div class="dm-color-chips">'
                               + "".join(
                                   f'<span class="dm-color-chip"><i style="background:{_norm_color(c)}"></i>{esc(c)} ×{n:,}</span>'
                                   for c, n in dm_attrs["colors"]) + '</div>')
            if dm_attrs["mode"]:
                mode_card = '<div class="chart-card"><h3>弹幕模式分布</h3><canvas id="dmModeChart"></canvas></div>'
        danmaku_tab = f'''
        <div class="dm-panel">
            <div class="stat-card"><div class="num">{panel["total"]:,}</div><div class="label">总弹幕数</div></div>
            <div class="stat-card"><div class="num">{panel["merged"]:,}</div><div class="label">合并后行数</div></div>
            <div class="stat-card"><div class="num">{panel["senders"]:,}</div><div class="label">独立发送者</div></div>
            <div class="stat-card"><div class="num">{panel["resolved"]:,}</div><div class="label">已解析发送者</div></div>
        </div>
        <div class="charts-grid">
            <div class="chart-card"><h3>问题弹幕类别分布</h3><canvas id="dmCatChart"></canvas>{color_chips}</div>
            <div class="chart-card"><h3>发送者弹幕数 Top10（点击筛选）</h3><div class="top10-list">{top10_html}</div></div>
            {mode_card}
        </div>
        <div class="dm-controls">
            <input id="dmSearch" class="search-input" placeholder="搜索弹幕内容...">
            <input id="dmSender" class="search-input" placeholder="发送者（昵称/UID/mid_hash）">
            <select id="dmCategory"><option value="">全部类别</option>{"".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in PROBLEM_CATEGORY_COLORS)}</select>
            <select id="dmSpam"><option value="">全部风险</option><option>高</option><option>中</option><option>低</option><option>未分析</option></select>
            <label><input type="checkbox" id="dmAnalyzed"> 只看已解析</label>
            <select id="dmSort">
                <option value="video_time">视频时间</option>
                <option value="send_time">发送时间</option>
                <option value="dup_count">重复次数</option>
                <option value="sender_count">发送者弹幕数</option>
            </select>
            <select id="dmOrder"><option value="asc">升序</option><option value="desc">降序</option></select>
            <button id="dmAnalyzeBtn" class="filter-btn" onclick="startAnalysis()">分析选中发送者</button>
            <span id="dmAnalyzeStatus"></span>
        </div>
        <div id="dmFailBar" class="fail-detail" style="display:none">
            <div>部分发送者分析失败：</div>
            <ul id="dmFailList"></ul>
            <button id="dmRetryBtn" class="filter-btn">重试失败项</button>
            <button id="dmReloadBtn" class="filter-btn">刷新查看结果</button>
        </div>
        <div class="dm-table-wrap">
            <table class="dm-table">
                <thead><tr><th><input type="checkbox" id="dmCheckAll" title="全选当前页"></th><th>弹幕内容</th><th>发送者</th><th>视频时间</th><th>发送时间</th><th>类别</th><th>刷屏</th></tr></thead>
                <tbody id="dmTbody"></tbody>
            </table>
            <div id="dmSpinner" class="dm-spinner" style="display:none">弹幕加载中…</div>
            <div class="dm-pager">
                <button id="dmPrev" onclick="dmPage(-1)">上一页</button>
                <span id="dmPageInfo"></span>
                <button id="dmNext" onclick="dmPage(1)">下一页</button>
                <input id="dmGoto" type="number" min="1" placeholder="页码">
                <button onclick="dmGotoPage()">跳转</button>
                <select id="dmPageSize">
                    <option value="50">50 条/页</option>
                    <option value="100" selected>100 条/页</option>
                    <option value="200">200 条/页</option>
                </select>
            </div>
            <div id="dmError" class="dm-error-bar" style="display:none">
                <span id="dmErrorText"></span>
                <button class="dm-retry-btn" onclick="loadDanmaku()">重试</button>
            </div>
        </div>'''

    # 数据经内联 <script>window.__DATA__</script> 注入，静态 /static/report.js 读取（spec 8）
    page_data = {
        "chart": chart,
        "categories": panel.get("categories", {}),
        "categoryColors": PROBLEM_CATEGORY_COLORS,
        "upWordcloud": up_wordcloud_data(profiles),
        "bvid": bvid,
        "density": density,
        "resolveQuality": rq,
        "dmMode": dm_attrs["mode"] if dm_attrs else None,
    }

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="referrer" content="no-referrer">
<title>{esc(title)} - B站弹幕用户画像分析</title>
<script src="/static/chart.umd.min.js"></script>
<script src="/static/wordcloud2.min.js"></script>
<style>{REPORT_CSS}</style>
<link rel="stylesheet" href="/static/report.css">
<link rel="stylesheet" href="/static/candidate_space.css">
</head>
<body>
<div class="container">
    <div class="header">
        <h1><a href="/" style="color:white;text-decoration:none">🎬 B站弹幕用户画像分析</a></h1>
        <div class="meta">
            <strong>{esc(title)}</strong> · BV: {esc(bvid)} · 播放: {video_info.get('stat', {}).get('view', 0):,} ·
            弹幕: {video_info.get('stat', {}).get('danmaku', 0):,} ·
            评论: {video_info.get('stat', {}).get('reply', 0):,} ·
            分析用户数: {stats['total']} · 大会员: {stats['vip_count']} ·
            刷屏用户: {stats['spam_levels'].get('高', 0) + stats['spam_levels'].get('中', 0)} ·
            AI画像: {ai_count} · 低置信度: {lowconf_count}{coverage_line}
        </div>
    </div>

    <div class="tab-bar">
        <button class="tab-btn active" data-tab="overview" onclick="switchTab('overview')">概览</button>
        <button class="tab-btn" data-tab="users" onclick="switchTab('users')">用户画像</button>
        <button class="tab-btn" data-tab="danmaku" onclick="switchTab('danmaku')">弹幕浏览器</button>
        <button class="tab-btn" data-tab="cringe" onclick="switchTab('cringe')">问题弹幕榜</button>
        <button class="tab-btn" data-tab="attack" onclick="switchTab('attack')">争执焦点</button>
        <button class="tab-btn" data-tab="cmtwar" onclick="switchTab('cmtwar')">问题评论榜</button>
        <button class="tab-btn" data-tab="hot" onclick="switchTab('hot')">高回复评论</button>
        <button class="tab-btn" data-tab="lowconf" onclick="switchTab('lowconf')">低置信度</button>
        <button class="tab-btn" data-tab="crc" onclick="switchTab('crc')">纯破解候选（{crc_count}）</button>
    </div>

    <div id="tab-overview" class="tab-pane active">
        <div class="ov-actions">
            <a class="filter-btn" href="/">← 返回首页</a>
            <button class="filter-btn" onclick="reportRegen()">🔄 重新生成报告</button>
            <button class="filter-btn btn-danger" onclick="reportDelete()">🗑 删除报告</button>
            <button class="filter-btn" onclick="toggleMask()"
                    title="开启后全站昵称只显示最后一个字、UID 只显示前三位（Cookie 持久化，所有页面共享）">{'已隐藏信息' if mask else '未隐藏信息'}</button>
            <span class="ov-dl">{links}</span>
            <details class="blk-export">
                <summary class="filter-btn"
                         title="把问题发送者导出为B站播放器「弹幕屏蔽列表」可导入的用户屏蔽 JSON——导入后就看不到这些人的弹幕了">🚫 屏蔽列表导出</summary>
                <div class="blk-panel">
                    <label><input type="checkbox" id="blkCringe" checked> 问题弹幕发送者</label>
                    <label><input type="checkbox" id="blkSpam" checked> 高/中风险刷屏</label>
                    <label><input type="checkbox" id="blkCmt"> 问题评论作者</label>
                    <label class="blk-sub"><input type="checkbox" id="blkLowconf"
                            title="CRC32 反查的 UID 可能张冠李戴，勾选即接受误屏蔽风险"> 含低置信度UID（可能误屏蔽，慎选）</label>
                    <label class="blk-sub">上限 <input type="number" id="blkMax" class="blk-num"
                            min="1" max="2000" value="{BLOCKLIST_MAX_UIDS}"> 条</label>
                    <button class="filter-btn" onclick="blkExport({esc(js_json(bvid))})">⬇ 导出 JSON</button>
                    <p class="blk-note">导入方法：网页播放器 → 弹幕设置 → 弹幕屏蔽列表 → 面板空白处右键 → 导入 json，
                    导入后逐条确认开启/同步。B站屏蔽列表容量约 {BLOCKLIST_MAX_UIDS} 条，超出按严重度截断。
                    仅已解析 UID 可导出（低置信度默认排除，防误屏蔽无辜用户）；
                    导出含真实 UID，不受「隐藏信息」开关影响（屏蔽必须用真实 UID）。</p>
                </div>
            </details>
            <a class="filter-btn" href="/api/video/{esc(bvid)}/longimage" target="_blank" rel="noopener"
               title="把本视频的精华内容渲染成一张竖版长图（数据总览/问题弹幕榜/争执焦点/问题评论榜/群体复读/用户画像），可直接保存转发；跟随「隐藏信息」开关遮蔽昵称">🖼 导出长图</a>
            <span id="reportJobStatus"></span>
        </div>
        {coverage_note}
        <div class="stats-grid">
            <div class="stat-card"><div class="num">{stats['total']}</div><div class="label">分析用户</div></div>
            <div class="stat-card"><div class="num">{stats['vip_count']}</div><div class="label">大会员</div></div>
            <div class="stat-card"><div class="num">{lv5_count}</div><div class="label">Lv.5+</div></div>
            <div class="stat-card"><div class="num">{stats['spam_levels'].get('高', 0)}</div><div class="label">重度刷屏</div></div>
            <div class="stat-card"><div class="num">{stats['spam_levels'].get('中', 0)}</div><div class="label">中度刷屏</div></div>
            <div class="stat-card"><div class="num">{ai_count}</div><div class="label">AI画像</div></div>
        </div>
        {density_canvas}
        <div class="charts-grid">
            <div class="chart-card"><h3>用户等级分布</h3><canvas id="levelChart"></canvas></div>
            <div class="chart-card"><h3>刷屏风险分布</h3><canvas id="spamChart"></canvas></div>
            <div class="chart-card"><h3>用户标签 / 地域分布 Top10</h3>
                <h4 class="dm-sub-h">用户标签 Top10</h4><canvas id="tagChart"></canvas>
                {region_sub}</div>
        </div>
        {repeat_block}
        {rq_block}
    </div>

    <div id="tab-users" class="tab-pane">
        <div class="filter-bar">
            <button class="filter-btn active" data-filter="all" onclick="userFilter('all', this)">全部</button>
            <button class="filter-btn" data-filter="high-level" onclick="userFilter('high-level', this)">Lv.5+</button>
            <button class="filter-btn" data-filter="vip" onclick="userFilter('vip', this)">大会员</button>
            <button class="filter-btn" data-filter="official" onclick="userFilter('official', this)">认证用户</button>
            <button class="filter-btn" data-filter="spam" onclick="userFilter('spam', this)">刷屏用户</button>
            <button class="filter-btn" data-filter="creator" onclick="userFilter('creator', this)">UP主</button>
            <input id="userSearch" class="search-input" placeholder="搜索昵称/UID...">
            <select id="userSort" class="sort-select">
                <option value="risk">默认（风险序）</option>
                <option value="spam-score">刷屏分</option>
                <option value="danmaku">弹幕数</option>
                <option value="fans">粉丝数</option>
            </select>
            <span id="userResultCount" class="result-count"></span>
        </div>
        <div class="user-grid" id="userGrid">{cards_html}</div>
        <div id="userEmpty" class="empty-filter" style="display:none">没有符合条件的用户，请调整筛选或搜索词</div>
        <div id="userPager" class="user-pager"></div>
    </div>

    <div id="tab-danmaku" class="tab-pane">{danmaku_tab}</div>

    <div id="tab-cringe" class="tab-pane">{board_html}</div>

    <div id="tab-attack" class="tab-pane">{attack_tab}</div>

    <div id="tab-cmtwar" class="tab-pane">{cmtwar_tab}</div>

    <div id="tab-hot" class="tab-pane">{hot_tab}</div>

    <div id="tab-lowconf" class="tab-pane">{lowconf_tab}</div>
    <div id="tab-crc" class="tab-pane">{crc_tab}</div>

    <div id="wc-popup" class="wc-popup"><canvas id="wc-popup-canvas" width="276" height="216"></canvas></div>
</div>
<script>window.__DATA__ = {js_json(page_data)};</script>
<script src="/static/report.js"></script>
<script src="/static/candidate_space.js"></script>
</body>
</html>'''
    with _PAGE_CACHE_LOCK:
        # 不设条目上限：缓存规模随使用者浏览过的视频数增长，由使用者自行取舍；
        # 正确性不依赖淘汰——每条都带数据指纹，落后即重渲染，job/删除/误报也会主动失效
        _PAGE_CACHE[(bvid, mask)] = (page_fp, html, time.time())
    return html


@app.route("/user/<int:uid>")
@_db_guard
def user_page(uid: int):
    """用户互动时间线页：该用户在全部已分析视频中的弹幕/评论足迹，按最近互动倒序
    （与卡片「其他视频足迹」互补：全量视频 + 时间维度 + 覆盖评论-only 用户）。

    数据边界：仅覆盖本系统已分析视频；B站无按用户查询互动历史的公开接口，
    弹幕本身匿名，无法从 UID 出发反查站内全部弹幕。"""
    data = _user_timeline(uid)
    name, level = data["name"], data["level"]
    # 隐藏信息（mask Cookie）：昵称只留末字、UID 可见文本只留前三位；链接 href 保持原值
    if _mask_on():
        name = _mask_name(name)
        uid_disp = _mask_uid(uid)
    else:
        uid_disp = uid

    def _fmt_ts(ts: int) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "时间未留存"

    if data["total_videos"] == 0:
        items_html = ('<p class="empty-note">该用户在本系统已分析视频中暂无互动记录。'
                      '注意：弹幕需该用户的 mid_hash 被解析进画像才会归户；'
                      '评论仅在评论落库之后的运行中留存。</p>')
        stats_line = ""
        more_html = ""
    else:
        items = []
        for it in data["items"]:
            dm_html = "".join(
                f'<li>{esc(s["content"])} <span class="tl-time">视频 '
                f'{("P%d " % s["vp"]) if s.get("vp") else ""}{_fmt_video_time(s["vt"])}'
                f' · {_fmt_ts(s["ts"])}</span></li>'
                for s in it["dm_samples"]) or (
                '<li class="ov-none">弹幕明细未留存（该视频为旧版本分析）</li>'
                if it["dm_count"] else '<li class="ov-none">无弹幕样本</li>')
            cmt_html = "".join(
                f'<li>{esc(_mask_reply_text(s["content"]))} <span class="tl-time">{_fmt_ts(s["ts"])}</span></li>'
                for s in it["cmt_samples"]) or '<li class="ov-none">无评论</li>'
            items.append(f'''
                <div class="tl-item">
                    <div class="tl-head">
                        <a class="tl-video" href="/video/{esc(it["bvid"])}">《{esc(it["title"])}》</a>
                        <span class="tl-last">最近互动 {_fmt_ts(it["last_ts"])}</span>
                    </div>
                    <div class="tl-sub">🎤 弹幕 {it["dm_count"]:,} 条（样本）</div>
                    <ul class="ov-list">{dm_html}</ul>
                    <div class="tl-sub">📝 评论 {it["cmt_count"]:,} 条（样本）</div>
                    <ul class="ov-list">{cmt_html}</ul>
                </div>''')
        items_html = "".join(items)
        more_html = (f'<div class="tl-more">另有 {data["more"]} 个视频也出现过（按最近互动排序，未展示）</div>'
                     if data["more"] else "")
        stats_line = (f'<div class="tl-stats">涉及视频 {data["total_videos"]} 个 · '
                      f'弹幕 {data["total_dm"]:,} 条 · 评论 {data["total_cmt"]:,} 条</div>')

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="referrer" content="no-referrer">
<title>{esc(name)} 互动时间线 - B站弹幕用户画像分析</title>
<style>{REPORT_CSS}</style>
<link rel="stylesheet" href="/static/report.css">
</head>
<body>
<div class="container">
    <div class="header">
        <h1><a href="/" style="color:white;text-decoration:none">🎬 B站弹幕用户画像分析</a></h1>
        <div class="meta">
            <strong>🕐 {esc(name)} 的互动时间线</strong><br>
            UID: {esc(uid_disp)} | Lv.{esc(level)} |
            <a style="color:#ffd" href="https://space.bilibili.com/{uid}" target="_blank" rel="noopener">B站空间 ↗</a><br>
            <span style="opacity:0.8">仅覆盖本系统已分析视频；按最近互动时间倒序。</span>
        </div>
    </div>
    {stats_line}
    <div class="tl-list">{items_html}{more_html}</div>
</div>
</body>
</html>'''


@app.route("/api/video/<bvid>/analyze", methods=["POST"])
def api_analyze(bvid: str):
    """启动手动勾选发送者分析 job（spec 3.2）。起后台线程立即返回 job_id。

    未知 bvid → 404；空列表 → 400；数量超限 → 400；有任务在跑 → 409；无有效 Cookie → 503。
    """
    if _load_video_row(bvid) is None:
        return jsonify({"error": "未知视频"}), 404
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    raw_mids = body.get("mid_hashes")
    if not isinstance(raw_mids, list):
        return jsonify({"error": "mid_hashes 必须是数组"}), 400
    # 去重保序；非字符串/空串项丢弃
    mid_hashes = list(dict.fromkeys(h for h in raw_mids if isinstance(h, str) and h))
    if not mid_hashes:
        return jsonify({"error": "mid_hashes 为空"}), 400
    if len(mid_hashes) > ANALYZE_MAX_TARGETS:
        return jsonify({"error": f"单次最多分析 {ANALYZE_MAX_TARGETS} 个发送者"}), 400
    try:
        _get_client()
    except CookieInvalidError as e:
        return jsonify({"error": str(e) or "Cookie 失效，请先运行 python login.py"}), 503
    # 互斥：该视频已有 job 在跑则拒绝（原子检查+注册，防并发双击重复发起）
    job_id, reject = _try_register_job("analyze", bvid, total=len(mid_hashes))
    if job_id is None:
        return jsonify({"error": reject}), 409
    threading.Thread(target=_run_analysis_job_safe,
                     args=(job_id, bvid, mid_hashes), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/reload_client", methods=["POST"])
def api_reload_client():
    """重新加载登录态（重新登录/更换 Cookie 文件后免重启）：
    清 _client_failed 粘性标记并重建 client。成功 {"ok": true}；Cookie 仍失效 → 503。"""
    global _client, _client_failed, _POOL
    with _CLIENT_LOCK:
        _client = None
        _client_failed = False
    # 组合池持有旧 client（build_pool 把它作为主号长期持有）：不重置的话，
    # 手动分析 job 会继续用失效登录态打接口。新 job 会用新 client 重建池
    with _POOL_LOCK:
        _POOL = None
    try:
        _get_client()
    except CookieInvalidError as e:
        return jsonify({"error": str(e) or "Cookie 失效，请先运行 python login.py"}), 503
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>")
def api_job(job_id: str):
    """轮询 job 进度（spec 3.2）：total/done/current/errors/finished/results"""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "未知 job"}), 404
        # errors/results 列表深拷一层：job 线程仍在 append，浅拷贝会把运行中的列表引用交给序列化
        return jsonify(dict(job, errors=list(job["errors"]), results=list(job["results"])))


def _has_running_job(bvid: str) -> bool:
    """该视频是否有未完成任务（手动分析/重新生成），有则拒绝删除与重复发起（spec 9）"""
    with JOBS_LOCK:
        return any(j.get("bvid") == bvid and not j.get("finished") for j in JOBS.values())


def _try_register_job(kind: str, bvid: str, **extra) -> tuple[str | None, str]:
    """原子「检查-注册」job：一把锁内完成运行中判定与登记，消除并发双击/多标签页竞态
    （此前「先 _has_running_job 再写 JOBS」两次加锁之间存在窗口）。

    返回 (job_id, "") 或 (None, 拒绝原因)。JOBS 淘汰：只保留最近 WEB_JOB_MAX_KEPT 个，
    超出时按登记先后删最旧的已完成 job（运行中的 job 永不淘汰）。"""
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        if any(j.get("bvid") == bvid and not j.get("finished") for j in JOBS.values()):
            return None, "该视频已有任务在运行，请等待完成"
        JOBS[job_id] = {"kind": kind, "bvid": bvid, "done": 0, "current": "",
                        "errors": [], "finished": False, "results": [], **extra}
        if len(JOBS) > WEB_JOB_MAX_KEPT:
            done_ids = [jid for jid, j in JOBS.items() if j.get("finished")]
            for jid in done_ids[:len(JOBS) - WEB_JOB_MAX_KEPT]:
                JOBS.pop(jid, None)
    return job_id, ""


@app.route("/api/video/<bvid>/delete", methods=["POST"])
def api_delete_video(bvid: str):
    """删除该视频的全部分析数据与导出文件（spec 9：含共享缓存，不可恢复；有任务在跑则 409）"""
    if _load_video_row(bvid) is None:
        return jsonify({"error": "未知视频"}), 404
    # 与分析/重跑同一把锁内占位，消除「先查运行中再删除」的窗口
    job_id, reject = _try_register_job("delete", bvid, total=1, current="删除中")
    if job_id is None:
        return jsonify({"error": reject or "该视频有正在运行的任务，请等待完成后再删除"}), 409
    try:
        counts = delete_video_data(bvid)
        _invalidate_page_cache(bvid)
        removed_files = 0
        for ext in ("csv", "json"):
            # bvid 来自路由参数：glob.escape 防通配符注入匹配到他视频文件
            for prefix in ("report", "target"):
                for f in glob.glob(os.path.join(REPORT_DIR, f"{prefix}_{glob.escape(bvid)}_*.{ext}")):
                    os.remove(f)
                    removed_files += 1
        return jsonify({"ok": True, "removed_files": removed_files, **counts})
    finally:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                job["finished"] = True
                job["done"] = 1
                job["current"] = ""


def _run_regen_job(job_id: str, bvid: str):
    """后台完整重跑分析流水线（等价 run.py --force；launch_web=False 防止再起 web 服务/开浏览器）"""
    try:
        run_analysis(bvid, force=True, max_users=None, launch_web=False)
        with JOBS_LOCK:
            JOBS[job_id].update(done=1, current="", finished=True)
        print(f"[RegenJob {job_id}] 完成: {bvid}")
    except (Exception, SystemExit) as e:
        # SystemExit 不是 Exception 子类：run_analysis 在"视频信息失败且无缓存"等
        # 场景会 raise SystemExit(1)，不接住就会穿透后台线程 → job 永远 finished=False
        # → 该视频的删除/重新分析从此恒 409，只能重启服务
        detail = (f"流水线以退出码 {e.code} 终止（详见服务端日志）"
                  if isinstance(e, SystemExit) else str(e))
        with JOBS_LOCK:
            JOBS[job_id]["errors"].append({"mid_hash": None, "error": detail})
            JOBS[job_id]["finished"] = True
        print(f"[RegenJob {job_id}] 失败: {detail}")
    finally:
        _invalidate_page_cache(bvid)   # 无论成败都重取数据，报告页缓存失效


@app.route("/api/video/<bvid>/regenerate", methods=["POST"])
def api_regenerate(bvid: str):
    """重新生成报告：后台完整重跑分析流水线（spec 9；已有任务在跑则 409）"""
    if _load_video_row(bvid) is None:
        return jsonify({"error": "未知视频"}), 404
    # 原子「检查-注册」：一把锁内完成运行中判定与登记，消除并发竞态
    job_id, reject = _try_register_job("regen", bvid, total=1,
                                       current="重新生成中（完整流水线）")
    if job_id is None:
        return jsonify({"error": reject}), 409
    threading.Thread(target=_run_regen_job, args=(job_id, bvid), daemon=True).start()
    return jsonify({"job_id": job_id})


def _fp_affected(bvid: str, kind: str, target: str) -> int:
    """误报影响面：该 (bvid, kind, target) 命中的弹幕/评论条数。
    kind=dm 按内容全文匹配（误报粒度=内容，一次标记隐藏全部同名弹幕）；
    kind=cmt 按 rpid 精确命中（comments.rpid 为 INTEGER，需 CAST 后按文本比较）；
    kind=spam 为发送者粒度（target=mid_hash，仅该发送者的刷屏判定），恒为 1，无需 SQL。"""
    if kind == "spam":
        return 1
    with closing(get_db()) as conn:
        if kind == "dm":
            return conn.execute(
                "SELECT COUNT(*) FROM danmaku WHERE bvid = ? AND content = ?",
                (bvid, target)).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM comments WHERE bvid = ? AND CAST(rpid AS TEXT) = ?",
            (bvid, target)).fetchone()[0]


@app.route("/api/video/<bvid>/false_positive", methods=["POST"])
def api_false_positive(bvid: str):
    """误报标记切换（P2-a）：body {kind: dm|cmt|spam, target: 弹幕内容、评论rpid字符串或mid_hash}。

    幂等切换（已标记则撤销），返回 {"ok": true, "marked": bool, "affected": int}；
    affected 为该标记命中的弹幕/评论条数（kind=dm 一次标记隐藏全部同名弹幕，
    前端据此在确认框提示影响面）。count_only=true 时只读查询影响面不切换标记。
    报告页缓存即时失效。llm_cache 不动——误报是人工覆盖层，跨 --force 重跑保留。"""
    if _load_video_row(bvid) is None:
        return jsonify({"error": "未知视频"}), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "参数错误：请求体须为 JSON 对象"}), 400
    kind = data.get("kind")
    target = str(data.get("target") or "")
    if kind not in ("dm", "cmt", "spam") or not target:
        return jsonify({"error": "参数错误：kind 须为 dm/cmt/spam，target 不能为空"}), 400
    affected = _fp_affected(bvid, kind, target)
    if data.get("count_only"):
        return jsonify({"ok": True, "affected": affected})
    marked = toggle_false_positive(bvid, kind, target)
    _invalidate_page_cache(bvid)
    return jsonify({"ok": True, "marked": marked, "affected": affected})


# ========== UP 主投稿词云懒加载（报告页关注 chip 悬停时按需采集） ==========

_UP_WC_LOCK = threading.Lock()
_UP_WC_EVENTS: dict[int, threading.Event] = {}   # uid → 在采事件（并发悬停只采一次，其余等待）


@app.route("/api/up/<int:uid>/wordcloud")
def api_up_wordcloud(uid: int):
    """关注 UP 主的近期投稿词云（懒加载）：采集阶段只存关注名单、不逐个分析被关注 UP 主，
    报告页悬停 chip 时才经此接口按需采集（fetch_up_wordcloud：单请求+immediate 免限速轻量路径）。

    结果按 llm_cache 键 up:{uid} 缓存（跨视频复用，--force 清除范围不含 up: 前缀）。
    同一 uid 并发请求只采一次：后到者等在 Event 上（最多 25s）再读缓存。"""
    key = f"up:{uid}"
    cached = load_llm_cache(key)
    if cached:
        try:
            data = json.loads(cached)
            if data.get("words") or (data.get("empty_until") or 0) > time.time():
                return jsonify(data)
        except Exception:
            pass                                        # 缓存损坏按未命中重采
    with _UP_WC_LOCK:
        ev = _UP_WC_EVENTS.get(uid)
        if ev is None:
            ev = threading.Event()
            _UP_WC_EVENTS[uid] = ev
            owner = True
        else:
            owner = False
    if not owner:
        ev.wait(25)
        cached = load_llm_cache(key)
        if cached:
            try:
                return jsonify(json.loads(cached))
            except Exception:
                pass
        return jsonify({"error": "正在采集中，请稍候再悬停"}), 202
    try:
        client = _get_client()
        # 轻量采集：只发 1 个投稿列表请求（交互式免限速通道），悬停出词≈单次网络延迟
        info = fetch_up_wordcloud(uid, client)
        words = sorted((info.get("word_freq") or {}).items(), key=lambda x: x[1], reverse=True)[:80]
        data = {"words": [[w, c] for w, c in words], "name": info.get("name", "")}
        if not words:
            data["empty_until"] = time.time() + getattr(_config, "UP_WORDCLOUD_EMPTY_TTL", 300)
        save_llm_cache(key, json.dumps(data, ensure_ascii=False))
        return jsonify(data)
    except CookieInvalidError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        print(f"[Web] UP词云采集失败 uid={uid}: {e}")
        return jsonify({"error": "采集失败，请稍后重试"}), 502
    finally:
        with _UP_WC_LOCK:
            _UP_WC_EVENTS.pop(uid, None)
        ev.set()


_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _norm_color(value) -> str:
    """弹幕颜色归一：只放行 #rrggbb（旧库/异常数据可能存任意串），其余返回空串。

    颜色会进前端内联 style，白名单化可防 CSS 声明注入（如 background:url(...)）。"""
    return value if isinstance(value, str) and _COLOR_RE.fullmatch(value) else ""


@app.route("/api/video/<bvid>/blocklist")
def api_blocklist(bvid: str):
    """B站弹幕屏蔽列表导出（用户屏蔽条目 JSON 数组，播放器屏蔽列表面板右键导入）。

    GET 参数：crit=cringe,spam,cmt（导出标准，默认 cringe,spam）；max=条数上限
    （默认 BLOCKLIST_MAX_UIDS，夹紧到 [1,2000]）；lowconf=1 纳入低置信度 UID。
    纯读库无副作用；无命中条目时 404 JSON（前端 alert 提示而非下载空文件）。"""
    try:
        video_row = _load_video_row(bvid)
    except sqlite3.Error as e:
        print(f"[Web] 屏蔽列表导出失败（videos 表）: {e}")
        return jsonify({"error": "数据库查询失败，请稍后重试"}), 500
    if video_row is None:
        return jsonify({"error": "未知视频"}), 404
    crit = {c.strip() for c in request.args.get("crit", "cringe,spam").split(",")}
    crit &= _BLOCKLIST_CRITERIA
    if not crit:
        return jsonify({"error": "未选择任何导出标准（crit 可选 cringe/spam/cmt）"}), 400
    try:
        max_uids = int(request.args.get("max", BLOCKLIST_MAX_UIDS))
    except ValueError:
        max_uids = BLOCKLIST_MAX_UIDS
    max_uids = max(1, min(max_uids, 2000))
    include_lowconf = request.args.get("lowconf") == "1"
    try:
        result = _build_blocklist(bvid, crit, max_uids, include_lowconf=include_lowconf)
    except sqlite3.Error as e:
        print(f"[Web] 屏蔽列表构建失败: {e}")
        return jsonify({"error": "数据库查询失败，请稍后重试"}), 500
    if not result["entries"]:
        hint = ("；有未解析发送者命中标准但无 UID 可导出" if result["skipped_unresolved"]
                else "")
        return jsonify({"error": f"没有符合条件的已解析发送者{hint}"}), 404
    payload = json.dumps(result["entries"], ensure_ascii=False, indent=2)
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "", bvid) or "video"   # 文件名白名单化
    resp = Response(payload, mimetype="application/json")
    resp.headers["Content-Disposition"] = f'attachment; filename="blocklist_{safe_name}.json"'
    return resp


@app.route("/api/blocklist/cross")
def api_blocklist_cross():
    """跨视频合并屏蔽列表导出：在 >= min_videos 个视频都命中问题标准的发送者优先。

    与单视频导出同一条目格式（type=2 用户屏蔽）；排序以「命中视频数」（惯犯程度）
    为首位。min_videos 默认 CROSS_VIDEO_MIN_VIDEOS，夹紧到 [1,50]；其余参数同单视频。"""
    crit = {c.strip() for c in request.args.get("crit", "cringe,spam").split(",")}
    crit &= _BLOCKLIST_CRITERIA
    if not crit:
        return jsonify({"error": "未选择任何导出标准（crit 可选 cringe/spam/cmt）"}), 400
    try:
        min_videos = int(request.args.get("min_videos", CROSS_VIDEO_MIN_VIDEOS))
    except ValueError:
        min_videos = CROSS_VIDEO_MIN_VIDEOS
    min_videos = max(1, min(min_videos, 50))
    try:
        max_uids = int(request.args.get("max", BLOCKLIST_MAX_UIDS))
    except ValueError:
        max_uids = BLOCKLIST_MAX_UIDS
    max_uids = max(1, min(max_uids, 2000))
    include_lowconf = request.args.get("lowconf") == "1"
    try:
        result = _build_cross_blocklist(crit, min_videos, max_uids,
                                        include_lowconf=include_lowconf)
    except sqlite3.Error as e:
        print(f"[Web] 跨视频屏蔽列表构建失败: {e}")
        return jsonify({"error": "数据库查询失败，请稍后重试"}), 500
    if not result["entries"]:
        return jsonify({"error": f"没有在 ≥{min_videos} 个视频命中标准的已解析发送者"
                                 f"（库内共 {result['videos']} 个视频）"}), 404
    payload = json.dumps(result["entries"], ensure_ascii=False, indent=2)
    resp = Response(payload, mimetype="application/json")
    resp.headers["Content-Disposition"] = 'attachment; filename="blocklist_cross.json"'
    return resp


@app.route("/api/video/<bvid>/longimage")
@_db_guard
def api_longimage(bvid: str):
    """分享长图导出：把概览里最值得传播的内容渲染成一张竖版 PNG（Pillow 服务端出图）。

    与网页报告同口径：**同一批取数函数**（_load_profiles / _attack_focus /
    _problem_comment_board / _repeat_events_data / _danmaku_panel_stats）+ 误报扣除，
    保证图上的数字与榜单跟页面上一致。

    隐藏信息（mask Cookie）跟随网页开关：_load_profiles/_attack_focus/
    _problem_comment_board 内部已按 _mask_on() 遮蔽昵称，故开启后导出的长图也是
    遮蔽版（分享出去不泄露昵称）；关闭则与页面一致显示真实昵称。
    单次渲染约 1 秒（两遍绘制），不落盘、不缓存：数据更新后立刻反映到下一次导出。"""
    row = _load_video_row(bvid)
    if row is None:
        abort(404)
    try:
        video_info = json.loads(row["video_info_json"]) if row["video_info_json"] else {}
    except Exception:
        video_info = {}
    profiles = _load_profiles(bvid)
    if not profiles:
        return jsonify({"error": "该视频暂无画像数据（或尚未解析出任何 UID），无法出图"}), 404
    fp = load_false_positives(bvid)
    fp_dm = {t for k, t in fp if k == "dm"}
    fp_cmt = {t for k, t in fp if k == "cmt"}
    # 注意：_apply_danmaku_fp 是**就地**扣除误报弹幕，返回的是"被扣除的误报内容列表"
    # （供榜单底部撤销入口），不能拿返回值当 profiles——否则画像/榜单会整段消失
    _apply_danmaku_fp(profiles, fp_dm)
    _apply_spam_fp(profiles, {t for k, t in fp if k == "spam"})
    profiles = sort_profiles_by_risk(profiles)
    stats = generate_summary_stats(profiles)
    chart = generate_chart_data(profiles)
    # 问题弹幕类别分布与弹幕浏览器面板同源（cat_counts 只在 _danmaku_panel_stats 里算）
    panel = _danmaku_panel_stats(bvid) or {}
    chart["cat_counts"] = panel.get("categories", {})
    # 问题弹幕榜单排序与 generate_cringe_board 同口径（最高严重度 → 命中条数）
    cringe_entries = [p for p in profiles if (p.get("cringe") or {}).get("count", 0) >= 1]
    cringe_entries.sort(key=lambda p: ((p.get("cringe") or {}).get("max_severity", 0),
                                       (p.get("cringe") or {}).get("count", 0)), reverse=True)
    attack = _attack_focus(bvid, fp_cmt)
    problem_comments, _marked, comment_total = _problem_comment_board(bvid, fp_cmt)
    repeat_events, _dist = _repeat_events_data(bvid)
    low_conf = _low_conf_tab_html(bvid)[1]
    payload = build_payload(bvid, video_info, profiles, stats, chart, cringe_entries,
                            attack, problem_comments, repeat_events, low_conf,
                            mask=_mask_on(), mask_name=_mask_name,
                            comment_total=comment_total)
    try:
        img = render_long_image(payload)
    except Exception as e:
        # 出图失败不影响报告页：给前端一个可读错误，具体原因留在服务端日志
        print(f"[Web] 分享长图渲染失败（{bvid}）: {e}")
        return jsonify({"error": f"长图渲染失败: {e}"}), 500
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "", bvid) or "video"
    resp = Response(data, mimetype="image/png")
    # inline：浏览器直接显示长图（右键/长按保存），不强制下载
    resp.headers["Content-Disposition"] = f'inline; filename="longimage_{safe_name}.png"'
    resp.headers["Cache-Control"] = "no-store"
    print(f"[Web] 分享长图已生成（{bvid}）: {img.width}x{img.height}, {len(data) // 1024} KB")
    return resp


@app.route("/api/video/<bvid>/danmaku")
def api_danmaku(bvid: str):
    """弹幕 JSON API（spec 4）。
    命中该发送者的问题弹幕类别）、spam（高/中/低/未分析）、analyzed=1（只看已解析用户）、
    sort（video_time/send_time/dup_count/sender_count）、order（asc/desc）、page、page_size（50/100/200，默认100）。
    返回 {rows: [...], total: int, page: int, multi_page: bool}；每行 content/dup_count/
    mid_hash/uid/name/first_video_time/first_page/first_send_time/categories/spam_level/
    mode/color。first_page 为该弹幕首次出现的分P（多分P 视频前端渲染成「P{n} mm:ss」，
    单分P 恒为 1 且不渲染前缀）。
    """
    # 数据库锁定/查询异常 → 500 JSON（spec 7），与下方主查询同一降级口径；
    # 详细错误只打控制台，不外透（防内部路径/SQL 细节泄露）
    try:
        video_row = _load_video_row(bvid)
    except sqlite3.Error as e:
        print(f"[Web] 弹幕查询失败（videos 表）: {e}")
        return jsonify({"error": "数据库查询失败，请稍后重试"}), 500
    # 返回 None 是"未知视频→404"的正常路径，不能与异常混淆
    if video_row is None:
        return jsonify({"error": "未知视频"}), 404

    args = request.args
    search = args.get("search", "").strip()
    sender = args.get("sender", "").strip()
    category = args.get("category", "").strip()
    spam = args.get("spam", "").strip()
    analyzed = args.get("analyzed") == "1"
    order = "DESC" if args.get("order", "asc").lower() == "desc" else "ASC"
    try:
        page = max(1, int(args.get("page", "1")))
    except ValueError:
        page = 1

    # 每页条数：白名单 50/100/200，非法值回退默认 PAGE_SIZE（spec 3 每页条数选择）
    try:
        page_size = int(args.get("page_size", str(PAGE_SIZE)))
    except ValueError:
        page_size = PAGE_SIZE
    if page_size not in (50, 100, 200):
        page_size = PAGE_SIZE

    # 数据库锁定 → 500 JSON（与主查询同一降级口径）
    try:
        meta = _sender_meta(bvid)
    except sqlite3.Error as e:
        print(f"[Web] 弹幕查询失败（发送者联查）: {e}")
        return jsonify({"error": "数据库查询失败，请稍后重试"}), 500

    where = ["d.bvid = ?"]
    params: list = [bvid]

    if search:
        # LIKE 通配符转义（%/\_）：搜索词按字面匹配，不被当成通配模式
        like = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("d.content LIKE ? ESCAPE '\\'")
        params.append(f"%{like}%")

    if sender:
        # mid_hash 精确（8位hex小写），否则按昵称/UID 精确匹配反查 mid_hash 集合
        hashes = set()
        if len(sender) == 8 and all(c in "0123456789abcdef" for c in sender.lower()):
            hashes.add(sender.lower())
        for h, m in meta.items():
            # 比对原始昵称：展示名在 mask 下已遮蔽，拿它去比会让真名搜索失效。
            # raw_name 只存在服务端 meta，不会写入下面的响应 JSON。
            if (sender == m.get("name") or sender == m.get("raw_name")
                    or (m["uid"] is not None and str(m["uid"]) == sender)):
                hashes.add(h)
        if not hashes:
            return jsonify({"rows": [], "total": 0, "page": page})
        where.append("d.mid_hash IN (%s)" % ",".join("?" * len(hashes)))
        params.extend(sorted(hashes))

    if category:
        # 命中该类别的发送者集合（categories 是 Python 侧解析，先求集合再 SQL 过滤）
        hashes = [h for h, m in meta.items() if category in m["categories"]]
        if not hashes:
            return jsonify({"rows": [], "total": 0, "page": page})
        where.append("d.mid_hash IN (%s)" % ",".join("?" * len(hashes)))
        params.extend(hashes)

    if spam in ("高", "中", "低"):
        # 刷屏判定人工误报（kind=spam）：_sender_meta 已把命中者降级为"低"，
        # 筛选条件在 SQL 层同步——筛「低」时纳入、筛「高/中」时排除，与卡片/面板同口径
        fp_spam = {t for k, t in load_false_positives(bvid) if k == "spam"}
        if spam == "低" and fp_spam:
            where.append("(s.spam_level = ? OR s.mid_hash IN (%s))" % ",".join("?" * len(fp_spam)))
            params.extend(["低", *sorted(fp_spam)])
        else:
            where.append("s.spam_level = ?")
            params.append(spam)
            if fp_spam:
                where.append("(s.mid_hash IS NULL OR s.mid_hash NOT IN (%s))" % ",".join("?" * len(fp_spam)))
                params.extend(sorted(fp_spam))
    elif spam == "未分析":
        # senders 无行（未进解析名单）或旧缓存 spam_level 为 NULL 均属未分析
        where.append("s.spam_level IS NULL")

    if analyzed:
        where.append("s.uid IS NOT NULL")

    sort_col = {
        # 排序键用 first_pt（=分P*1e6+秒）：多分P 视频按 (分P, 分P内时间) 字典序才正确
        "video_time": "first_pt",
        "send_time": "first_send_time",
        "dup_count": "dup_count",
        "sender_count": "sender_count",
    }.get(args.get("sort", "video_time"), "first_pt")   # 兜底值必须是有效列（白名单防注入 + 防非法 sort 500）

    where_sql = " AND ".join(where)
    # sender_count：发送者在本视频的总弹幕数，子查询按 mid_hash 预聚合
    # 注意参数顺序：子查询的 bvid=? 在 SQL 文本中最先出现，绑定参数也要最先放
    base_sql = f'''
        FROM danmaku d
        LEFT JOIN senders s ON s.bvid = d.bvid AND s.mid_hash = d.mid_hash
        LEFT JOIN users u ON u.uid = s.uid
        LEFT JOIN (
            SELECT mid_hash, COUNT(*) AS cnt FROM danmaku WHERE bvid = ? GROUP BY mid_hash
        ) sc ON sc.mid_hash = d.mid_hash
        WHERE {where_sql}
        GROUP BY d.mid_hash, d.content
    '''
    count_sql = f"SELECT COUNT(*) FROM (SELECT d.mid_hash, d.content {base_sql})"
    rows_sql = f'''
        SELECT d.mid_hash, d.content, COUNT(*) AS dup_count,
               -- 首次出现：多分P 时按 (分P, 分P内时间) 取字典序最小值（MIN(time) 会跨分P 比大小，
               -- 把 P2 的 00:10 误当成比 P1 的 00:50 更早）；1e6 秒≈11.5 天，远超单P 时长
               MIN(d.page * 1000000 + CAST(d.time AS INTEGER)) AS first_pt,
               MIN(d.timestamp) AS first_send_time,
               MIN(d.mode) AS mode, MIN(d.color) AS color,
               s.uid AS uid, u.name AS name, s.spam_level AS spam_level,
               sc.cnt AS sender_count
        {base_sql}
        ORDER BY {sort_col} {order}, d.mid_hash, d.content
        LIMIT {page_size} OFFSET {(page - 1) * page_size}
    '''
    full_params = [bvid] + params

    # 数据库锁定/查询异常 → 500 JSON（spec 7），前端显示错误提示不崩溃
    # closing 保证异常路径连接也关闭，不泄漏
    try:
        with closing(get_db()) as conn:
            total = conn.execute(count_sql, full_params).fetchone()[0]
            raw_rows = conn.execute(rows_sql, full_params).fetchall()
    except sqlite3.Error as e:
        print(f"[Web] 弹幕查询失败（主查询）: {e}")
        return jsonify({"error": "数据库查询失败，请稍后重试"}), 500

    rows = []
    mask = _mask_on()   # 隐藏信息：行内昵称只留末字、UID 只留前三位（JSON 里 uid 变字符串无副作用，前端只展示）
    for r in raw_rows:
        m = meta.get(r["mid_hash"], {})
        rows.append({
            "content": r["content"],
            "dup_count": r["dup_count"],
            "mid_hash": r["mid_hash"],
            "uid": (_mask_uid(r["uid"]) if mask else (int(r["uid"]) if r["uid"] is not None else None)),
            "name": _mask_name(r["name"]) if mask else r["name"],
            # first_pt 解码：高位=分P，低位=分P 内秒数（前端多分P 时渲染成「P{n} mm:ss」）
            "first_video_time": int(r["first_pt"] or 0) % 1000000,
            "first_page": int(r["first_pt"] or 0) // 1000000,
            "first_send_time": r["first_send_time"],
            "categories": m.get("categories", []),
            # meta 的 spam_level 已扣刷屏误报（降级"低"），优先于 SQL 原值
            "spam_level": m.get("spam_level") or r["spam_level"] or "未分析",
            "sender_count": r["sender_count"],
            "mode": r["mode"] or 1,
            "color": _norm_color(r["color"]),
        })
    return jsonify({"rows": rows, "total": total, "page": page, "page_size": page_size,
                    # 多分P 视频：前端给「视频内时间」列加 P{n} 前缀（单分P 不显示）
                    "multi_page": len(_load_page_meta(bvid)) > 1})


@app.route("/target/<target_id>")
@_db_guard
def target_page(target_id: str):
    """读取已保存的目标分析；渲染时不重新匹配或发起采集。"""
    record = load_target_analysis(target_id)
    if record is None:
        abort(404)
    return render_target_report(record, request.args, _mask_on(), _mask_name, _mask_uid,
                                load_false_positives(record["bvid"]), cached_assessments(record),
                                has_normal_report(record["bvid"]), _crc_candidate_tab_html(record["bvid"], record)[0])


_CANDIDATE_SPACE_LOCK = threading.Lock()


@app.route("/api/target/<target_id>/candidate-space", methods=["POST"])
@_db_guard
def api_target_candidate_space(target_id: str):
    """仅核验报告中的碰撞候选；名片缺失可排除，空间线索不升级身份。"""
    record = load_target_analysis(target_id)
    if record is None:
        return jsonify({"error": "未知目标分析"}), 404
    # 旧快照可能只保存已分配 UID；与新页一样从本地数学缓存补齐全部候选。
    for sender in record["result"].get("senders", []):
        if is_pure_crc(sender) and not (sender.get("resolution", {}).get("candidates") or sender.get("candidates")):
            sender["candidates"] = load_uid_candidates(sender["mid_hash"], _config.MITM_MAX_UID, "mitm-v1") or []
    return _candidate_space_response(record)


@app.route("/api/video/<bvid>/candidate-space", methods=["POST"])
@_db_guard
def api_video_candidate_space(bvid: str):
    if _load_video_row(bvid) is None:
        return jsonify({"error": "未知视频"}), 404
    record, groups = _crc_candidate_data(bvid, include_resolved=True)
    hashes = {g["mid_hash"] for g in groups}
    record["result"]["senders"] = [s for s in record["result"]["senders"] if s["mid_hash"] in hashes]
    return _candidate_space_response(record)


def _candidate_space_response(record: dict):
    """兼容旧页的请求入口；报告不采集，旧脚本也不能触发B站请求。"""
    return jsonify({"error": "候选资料已改为命令行阶段采集，请重跑原分析命令补齐；报告页面只读"}), 410


def _updated_crc_group(record: dict, mid_hash: str) -> dict:
    """返回最新候选条目或明确移除信号，同时更新数量和空状态。"""
    target = load_target_analysis(record["id"]) if record.get("id") else None
    if record.get("id") and target is None:
        return {"group_html": "", "group_removed": False}
    current, groups = _crc_candidate_data(record["bvid"], target)
    group = next((g for g in groups if g["mid_hash"] == mid_hash), None)
    return {"group_html": (render_crc_group(group, request.path, _mask_on(), _mask_name, _mask_uid,
                                            cached_assessments(current), render_space_assessment) if group else ""),
            "group_removed": group is None, "candidate_count": len(groups),
            "empty_html": render_crc_groups([], request.path, _mask_on(), _mask_name, _mask_uid) if not groups else ""}


@app.route("/api/target/<target_id>/danmaku")
@_db_guard
def api_target_danmaku(target_id: str):
    record = load_target_analysis(target_id)
    if record is None:
        return jsonify({"error": "未知目标分析"}), 404
    result = record["result"]
    mid_hash = request.args.get("mid_hash", "")
    matches = result.get("matches", [])
    if mid_hash:
        matches = [dm for dm in matches if dm["mid_hash"] == mid_hash]
    matches = sorted(matches, key=lambda dm: (int(dm.get("page", 1) or 1), float(dm.get("time", 0) or 0)))
    size = max(1, getattr(_config, "TARGET_MATCH_PAGE_SIZE", 100))
    page, pages = page_number(request.args.get("page"), len(matches), size)
    senders = {s["mid_hash"]: s for s in result.get("senders", [])}
    multi_page = len(result.get("video_info", {}).get("pages", [])) > 1
    rows = []
    for dm in matches[(page - 1) * size:page * size]:
        sender = senders.get(dm["mid_hash"], {})
        uid = sender.get("uid")
        name = sender.get("name") or ""
        if _mask_on():
            uid, name = _mask_uid(uid), _mask_name(name)
        p, sec = max(1, int(dm.get("page", 1) or 1)), max(0, int(float(dm.get("time", 0) or 0)))
        label = f"{sec // 60:02d}:{sec % 60:02d}"
        if multi_page or p != 1:
            label = f"P{p} {label}"
        rows.append({"content": dm["content"], "mid_hash": dm["mid_hash"], "page": p, "time": dm.get("time", 0),
                     "time_label": label, "confidence": sender.get("confidence", "无"), "method": sender.get("method", ""),
                     "sender_label": f"{name} UID:{uid}" if uid else f"未解析 · {dm['mid_hash']}",
                     "video_url": f"https://www.bilibili.com/video/{record['bvid']}?p={p}&t={sec}"})
    return jsonify({"rows": rows, "page": page, "pages": pages, "page_size": size, "total": len(matches)})


@app.route("/download/<path:filename>")
def download(filename: str):
    """仅下载报告导出文件，禁止路径穿越。"""
    allowed = filename.startswith("report_") or (filename.startswith("target_") and filename.endswith((".csv", ".json")))
    if not allowed or "/" in filename or "\\" in filename or ".." in filename:
        abort(404)
    return send_from_directory(REPORT_DIR, filename, as_attachment=True)


@app.route("/api/target/<target_id>/export/<kind>")
@_db_guard
def export_target_snapshot(target_id: str, kind: str):
    """下载最新目标快照，避免存在性核验后仍导出旧 UID。"""
    from target_analysis import write_target_csv, public_target_result
    record = load_target_analysis(target_id)
    if record is None or kind not in ("csv", "json"):
        abort(404)
    result = {**record["result"], "selector": record["selector"]}
    if kind == "csv":
        output = io.StringIO(newline="")
        write_target_csv(result, output)
        body, mime = "\ufeff" + output.getvalue(), "text/csv"
    else:
        body, mime = json.dumps(public_target_result(result), ensure_ascii=False, indent=2), "application/json"
    filename = re.sub(r"[^0-9A-Za-z_.-]", "_", f"target_{record['bvid']}_{target_id}.{kind}")
    return Response(body, mimetype=mime, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.errorhandler(404)
def not_found(e):
    """未知 bvid / 未知路径 → 中文 404 页面（带站点样式与返回首页按钮，spec 6）"""
    message = ('目标报告不存在或链接已失效，请从<a href="/targets">「目标弹幕分析」列表</a>打开。'
               if request.path.startswith("/target/") else
               '视频不存在或尚未分析，请先运行 <code>python run.py &lt;BV号&gt;</code>')
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="referrer" content="no-referrer">
<title>404 - B站弹幕用户画像分析</title>
<style>{REPORT_CSS}</style>
<link rel="stylesheet" href="/static/report.css">
</head>
<body>
<div class="container">
    <div class="header"><h1>404</h1><div class="meta">页面或视频不存在</div></div>
    <div class="nf-card">
        <p>{message}</p>
        <p><a class="filter-btn" href="/">返回首页</a></p>
    </div>
</div>
</body>
</html>''', 404


def _pid_file(port: int) -> str:
    """web 服务 pidfile 路径（按端口区分，PROFILER_PORT 多实例互不干扰）"""
    return os.path.join(DATA_DIR, f"web_{port}.pid")


def _process_identity(pid: int) -> tuple[list[str], str, str] | None:
    """返回命令参数、启动标识、工作目录；无法验证时保守拒绝停止。"""
    if pid <= 0:
        return None
    try:
        if sys.platform.startswith("win"):
            import shlex
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" | "
                "Select-Object CommandLine,CreationDate | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=15)
            data = json.loads(out.stdout)
            argv = [a.strip('"') for a in shlex.split(data["CommandLine"], posix=False)]
            return argv, str(data["CreationDate"]), ""
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = [a for a in f.read().decode(errors="replace").split("\0") if a]
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            started = f.read().rsplit(")", 1)[1].split()[19]
        return argv, started, os.readlink(f"/proc/{pid}/cwd")
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        return None


def _write_pid(port: int):
    os.makedirs(DATA_DIR, exist_ok=True)
    identity = _process_identity(os.getpid())
    record = {"pid": os.getpid(), "port": port, "script": os.path.realpath(__file__),
              "started": identity[1] if identity else None, "cwd": os.getcwd()}
    with open(_pid_file(port), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)


def _clear_pid(port: int, force: bool = False):
    """删除该端口的 pidfile。

    默认**只删属于本进程的那条记录**：老实例退出时的 atexit / SIGTERM 处理器会无条件
    删除文件，若此时新实例已写入自己的记录，就会把新实例的 pidfile 误删——端口被占
    却再也停不掉。force=True 供 --stop 清理"已确认失效/已停止"的记录。"""
    path = _pid_file(port)
    if not force:
        try:
            with open(path, encoding="utf-8") as f:
                if int(json.load(f).get("pid", -1)) != os.getpid():
                    return
        except (OSError, ValueError, TypeError):
            return
    try:
        os.remove(path)
    except OSError:
        pass


def _pid_is_webpy(pid: int, started: str | None = None, recorded_cwd: str = "") -> bool:
    identity = _process_identity(pid)
    if identity is None:
        return False
    argv, actual_started, cwd = identity
    if started is not None and actual_started != started:
        return False
    if not argv or not re.fullmatch(r"(?:pythonw?|pypy)(?:[0-9.]+)?(?:\.exe)?",
                                    os.path.basename(argv[0]), re.IGNORECASE):
        return False
    # 只接受解释器的脚本参数，不接受编辑器参数、-c 字符串或另一个目录的 web.py。
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        arg = argv[i]
        if arg in ("-c", "-m"):
            return False
        i += 2 if arg in ("-X", "-W") else 1
    if i >= len(argv):
        return False
    script = argv[i]
    if not os.path.isabs(script):
        cwd = cwd or (recorded_cwd if started is not None else "")
        if not cwd:
            return False
        script = os.path.join(cwd, script)
    return os.path.realpath(script) == os.path.realpath(__file__)


def _parse_listener_pids(text: str, port: int = 0) -> list[int]:
    """从 ss / lsof / netstat 输出解析"监听进程 PID"（纯函数，便于离线测试）。

    - ss -ltnp:      ... users:(("python",pid=123,fd=3))
    - lsof -ti:      每行一个 PID
    - netstat -ano:  TCP  127.0.0.1:8001  0.0.0.0:0  LISTENING  123（按端口过滤，
                     否则会把别的端口的监听进程也算进来）"""
    pids: list[int] = []

    def _add(p: int):
        if p not in pids:
            pids.append(p)

    for m in re.finditer(r"pid=(\d+)", text or ""):
        _add(int(m.group(1)))
    if pids:
        return pids
    for line in (text or "").splitlines():
        s = line.strip()
        if s.isdigit():
            _add(int(s))
            continue
        if re.search(r"LISTENING", s, re.IGNORECASE) and (not port or (":%d" % port) in s):
            m = re.search(r"(\d+)\s*$", s)
            if m:
                _add(int(m.group(1)))
    return pids


def _listening_pids(port: int) -> list[int]:
    """监听该端口的进程 PID（pidfile 缺失/失效时的兜底）。

    依次尝试 ss（iproute2）→ lsof → netstat；都不可用返回 []。返回的 PID **必须**再用
    _pid_is_webpy 验明是本项目 web.py 才允许 kill——不做"谁占端口杀谁"。"""
    if sys.platform.startswith("win"):
        cmds = [["netstat", "-ano", "-p", "TCP"]]
    else:
        cmds = [["ss", "-ltnpH", "sport = :%d" % port], ["ss", "-ltnp", "sport = :%d" % port],
                ["lsof", "-ti", "tcp:%d" % port, "-sTCP:LISTEN"], ["netstat", "-ltnp"]]
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and (proc.stdout or "").strip():
            pids = _parse_listener_pids(proc.stdout, port)
            if pids:
                return pids
    return []


def _stop_server(port: int):
    """python web.py --stop：停止该端口的后台 web 服务并释放端口。

    先读 pidfile 并校验进程确为本项目 web.py（防 PID 复用误杀）；**pidfile 缺失、
    内容失效或指向的进程已不在时，兜底按「谁在监听该端口」反查**，仍须验明是本项目
    web.py 才动手——「端口被占用却停不掉」由此收敛（老实例退出误删 pidfile 是已知成因）。
    停止策略：SIGTERM 优雅退出，1 秒内未退出则 SIGKILL 兜底；最后清理 pidfile。"""
    pid_path = _pid_file(port)
    pid = None
    started = None
    recorded_cwd = ""
    try:
        with open(pid_path, encoding="utf-8") as f:
            record = json.load(f)
        pid = int(record["pid"])
        started = record["started"]
        recorded_cwd = record.get("cwd", "")
        if (not started or record["port"] != port
                or record["script"] != os.path.realpath(__file__)):
            raise ValueError("服务身份不匹配")
    except (OSError, ValueError, KeyError, TypeError):
        pid = None
    if pid is not None and not _pid_is_webpy(pid, started, recorded_cwd):
        print(f"[Web] 记录中的进程 {pid} 已不存在或不是本项目的 web.py，清理残留 pidfile")
        _clear_pid(port, force=True)
        pid = None
    if pid is None:
        # 兜底：pidfile 缺失/被误删/记录过期时按监听端口反查（仍须是本项目的 web.py）。
        # recorded_cwd 传本项目根目录：进程以相对路径启动时靠它解析脚本参数
        _here = os.path.dirname(os.path.abspath(__file__))
        for cand in _listening_pids(port):
            if _pid_is_webpy(cand, None, _here):
                print(f"[Web] pidfile 无有效记录，按监听端口反查到本项目的 web.py (pid={cand})")
                pid = cand
                break
    if pid is None:
        print(f"[Web] 未发现 {port} 端口的本项目 web.py（{pid_path} 无有效记录，"
              f"当前也没有本项目的 web.py 在监听该端口）")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        # 无权限/进程刚退出：不清 pidfile（保留记录便于提权后重试）
        print(f"[Web] 向进程 {pid} 发送停止信号失败（{e}），pidfile 保留，可稍后重试")
        return
    for _ in range(10):
        if not _pid_is_webpy(pid, started, recorded_cwd):
            break
        time.sleep(0.1)
    if _pid_is_webpy(pid, started, recorded_cwd):
        try:
            os.kill(pid, signal.SIGKILL)
        except (AttributeError, OSError):
            pass    # Windows 无 SIGKILL（os.kill 的 SIGTERM 即 TerminateProcess）
        time.sleep(0.1)
    _clear_pid(port, force=True)   # 记录属于被停掉的进程，需强制清理
    print(f"[Web] 已停止 {port} 端口的 web 服务 (pid={pid})，端口已释放")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="B站弹幕用户画像 Web 报告服务")
    parser.add_argument("--port", type=int, default=None, metavar="PORT",
                        help="监听端口（默认 8001；优先于 PROFILER_PORT 环境变量，跨平台）")
    parser.add_argument("--stop", action="store_true",
                        help="停止本端口后台运行的 web 服务并释放端口（配合 --port 指定端口）")
    args = parser.parse_args()
    # --port 优先；同时刷新模块级 _PORT，保证 _loopback_guard 的 Origin 白名单、
    # pidfile 路径与 --stop 都指向同一端口
    port = _resolve_port(args.port)
    _PORT = port
    if args.stop:
        _stop_server(port)
        sys.exit(0)

    init_db()

    # 端口占用检测提前于 pidfile 写入：若先写 pidfile 再 app.run 撞端口失败，
    # atexit 的 _clear_pid 会误删健康实例的 pidfile（--stop 随即失效）
    import socket
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            print(f"[Web] 端口 {port} 已被占用，请先 python web.py --stop --port {port}"
                  f"（同端口），或 python web.py --port <其它端口> 换端口")
            sys.exit(1)

    _write_pid(port)
    atexit.register(lambda: _clear_pid(port))

    # 鉴权威胁热身（后台线程，不阻塞启动）：BiliAPIClient 首次请求的 buvid3/
    # bili_ticket/WBI 密钥获取约 2-3s，提前完成让首次悬停词云也是 ~0.2s。
    # 热身失败不得把登录态粘成失效（见 _warm_client）。
    threading.Thread(target=_warm_client, daemon=True).start()

    def _on_term(signum, frame):
        # SIGTERM（来自 web.py --stop）：清 pidfile 后退出，端口随即释放
        _clear_pid(port)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)
    print(f"[Web] 交互式报告服务已启动: http://127.0.0.1:{port}")
    print("[Web] 停止服务: python web.py --stop"
          + (f" --port {port}" if port != 8001 else ""))
    # threaded=True：弹幕大查询/词云采集不阻塞其它请求（job 轮询、页面加载）
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
