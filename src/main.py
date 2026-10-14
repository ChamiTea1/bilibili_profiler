"""
B站弹幕发送者用户画像分析系统 — 主控流程

用法:
    python src/main.py BVxxxxxxxx [--force]
    python src/main.py --batch videos.txt   # 批量模式：逐行读取BV号（忽略空行与 # 注释行）
    --force: 清除该视频的全部缓存（senders/孤立 users/videos），强制重新采集全部用户
"""
import sys
import os
import argparse
import re
import threading
import time
from datetime import datetime

import config
from config import (MAX_ANALYZE_USERS_HARD_CAP, ANALYZE_USERS_FLOOR, ANALYZE_USERS_RATIO,
                    LLM_API_KEY, LLM_DEEP_ENABLED, HISTORY_DANMAKU_ENABLED, REPORT_DIR,
                    COMMENT_AUTHOR_MIN_SEVERITY, COMMENT_AUTHOR_MIN_HITS,
                    HISTORY_MULTIPAGE_ENABLED, HISTORY_MULTIPAGE_MAX_PAGES,
                    MAX_COMMENT_PAGES)
from storage import init_db, save_video_info, save_sender, save_user_data
from storage import load_video_info
from storage import load_user_data, has_user_data, load_senders, update_user_profile
from storage import clear_video_cache, update_sender_spam, load_global_uid_map, save_uid_evidence_maps
from storage import save_comments, update_comment_problems
from storage import load_danmaku, load_comments, append_danmaku
from storage import get_phase_state, set_phase_state, load_false_positives, delete_senders_by_mid_hash
from auth import get_auth_client, LoginRequiredError, account_cookie_path, save_cookie_if_current
from combo_pool import build_pool
from api_client import RiskControlError
from danmaku import (collect_danmaku_data, group_by_sender, fetch_command_dms,
                    build_command_uid_map, fetch_danmaku, DanmakuCollectionError)
from danmaku import get_video_info
from danmaku_history import fetch_history_danmaku
from comment import collect_comment_data, fetch_charge_uid_map, refresh_comments
from comment import (build_comment_uid_map, build_comment_location_map, harvest_comment_uids,
                     supplement_uid_replies)
from uid_resolver import (resolve_all_senders, calc_crc32, METHOD_COMMENT_VERIFY, METHOD_PENDING,
                          build_evidence_maps, resolution_is_current, resolution_record)
from uid_evidence import add_uid, merge_uid_maps
from spam_detector import batch_detect_spam, distribution_stats, detect_repeat_events
from cringe_detector import detect_cringe_danmaku, detect_problem_comments
from user_collector import collect_user_data
from profile_analyzer import analyze_profile
from llm_analyzer import LLMAnalyzer
from exporter import export_csv, export_json
from web_autostart import maybe_launch_web


def print_banner():
    print("=" * 60)
    print("  B站弹幕发送者深度画像分析系统")
    print("=" * 60)
    print()


def phase_login():
    """阶段1: 扫码登录"""
    print("[Phase 1/6] 扫码登录...")
    return get_auth_client()


def _history_page_targets(video_info: dict) -> list[tuple[int, int]]:
    """待采历史弹幕的 [(分P序号, cid)] 列表。

    B站历史弹幕接口按 cid 维度（每个分P 一个弹幕池），只采分P 1 会让其余分P 永久
    只有实时池数据。分P 超上限（HISTORY_MULTIPAGE_MAX_PAGES）或开关关闭时只留分P 1。
    无 pages 元信息（旧数据/接口未下发）时退回主 cid，行为与旧版一致。"""
    pages = video_info.get("pages") or []
    targets: list[tuple[int, int]] = []
    for i, p in enumerate(pages):
        if not isinstance(p, dict) or not p.get("cid"):
            continue
        try:
            pno = int(p.get("page") or i + 1)
        except (TypeError, ValueError):
            pno = i + 1
        targets.append((pno, int(p["cid"])))
    if not targets:
        cid = video_info.get("cid") or 0
        if cid:
            targets.append((1, int(cid)))
    if not HISTORY_MULTIPAGE_ENABLED:
        return targets[:1]
    return targets[:max(1, HISTORY_MULTIPAGE_MAX_PAGES)]


def _fetch_history_all_pages(video_info: dict, client, bvid: str,
                             seen_dmids: set | None = None) -> list[dict]:
    """逐分P采集历史弹幕（各分P 独立检查点/独立续采；单P失败只跳过该P，不中断整体）。

    返回本轮新采的弹幕（含 page 标记）；全量请读库。"""
    all_new: list[dict] = []
    targets = _history_page_targets(video_info)
    if len(targets) > 1:
        print(f"[Phase 2] 分P历史弹幕：共 {len(targets)} 个分P（"
              f"{'、'.join('P%d' % p for p, _ in targets)}），各分P独立续采")
    for pno, cid in targets:
        try:
            all_new.extend(fetch_history_danmaku(cid, client, video_info.get("pubdate", 0),
                                                bvid=bvid, seen_dmids=seen_dmids, page=pno))
        except Exception as e:
            print(f"[Phase 2] 警告: 分P {pno} 历史弹幕采集失败（{e}），跳过该分P")
    return all_new


def phase_danmaku(bvid: str, client, resume: bool = True):
    """阶段2: 采集弹幕（实时弹幕池 + 可选历史弹幕快照合并 + 互动弹幕明文mid）

    断点续采（resume=True 的非 --force 路径）：
    - danmaku 表已有完整数据（done=1，或旧版完整落库——判据为新格式哨兵 format 缺失
      且弹幕非空）→ 旧版落库直接读库跳过弹幕网络重采；done=1 仍调用
      fetch_history_danmaku 做滚动补采（其内部 done 感知：自动回拨 last_date
      只补采最近几天、无新增时幂等快速返回），随后读库
      （视频信息/互动弹幕仍新鲜拉取，各 1 个请求）；
    - 有部分数据（哨兵 format 存在但无 done）→ 实时池已在库不重新拉，
      历史弹幕从 last_date 检查点续采（danmaku_history 内逐日增量落库，
      last_date 也缺失则从头逐日采集，与库内已有 dmid 去重）；
    - 无数据 → 全新采集：实时池先增量落库，再历史逐日续采，任意时刻中断可续。

    resume 判据依据 phase_state 哨兵而非弹幕非空：空弹幕视频也会写 format/phase_done，
    若按 load_danmaku 非空判定会对空列表恒 falsy 而永远全量重采。
    关闭历史采集时完成标记是 phase_done，不是 done（done 专供历史模块判断分P是否采完）。
    实时池失败记 realtime_ok=0，不把已经完成的历史 done 打回去。
    """
    print("\n[Phase 2/6] 采集弹幕数据...")

    if resume:
        done = get_phase_state(bvid, "danmaku", "done")
        phase_done = get_phase_state(bvid, "danmaku", "phase_done")
        last = get_phase_state(bvid, "danmaku", "last_date")
        fmt = get_phase_state(bvid, "danmaku", "format")
        existing = load_danmaku(bvid)
        # phase_done 只表示「关闭历史采集时本阶段已处理完」，用来跳过实时池重采。
        # 真正的历史完成仍看 done，避免把已采完的历史打成每次从头重采。
        if existing or done == "1" or phase_done == "1" or fmt is not None:
            prev_info = load_video_info(bvid) or {}
            try:
                video_info = get_video_info(bvid, client)
            except Exception as e:
                # 拉取失败（接口风控/网络异常）回退已落库的旧视频信息继续续采，
                # 本地也无缓存可回退时才报错退出
                if not prev_info:
                    print(f"[Phase 2] 错误: 获取视频信息失败（{e}），且本地无缓存可回退")
                    raise SystemExit(1)
                print(f"[Phase 2] 警告: 获取视频信息失败（{e}），回退使用本地缓存的视频信息")
                video_info = dict(prev_info)
            # 续采不重新计算覆盖率，沿用上次已落库的展示数据（半成品续采分支会在
            # 历史合并后补算缺失的覆盖率），必须在 save_video_info 覆盖前取出
            if prev_info.get("danmaku_coverage"):
                video_info["danmaku_coverage"] = prev_info["danmaku_coverage"]
            pending_pages = prev_info.get("realtime_failed_pages") or []
            if not existing and "realtime_failed_pages" not in prev_info:
                # 旧版本曾把请求全失败写成空池完成；无法确认的空池至少重新验证一次。
                pending_pages = list(range(1, len(video_info.get("pages") or [1]) + 1))
            if pending_pages:
                remaining = []
                seen = {d["dmid"] for d in existing if d.get("dmid")}
                pages = video_info.get("pages") or [{"cid": video_info.get("cid"), "page": 1}]
                for pg in pending_pages:
                    try:
                        cid = pages[pg - 1]["cid"]
                        retry = fetch_danmaku(cid, client)
                        for d in retry:
                            d["page"] = pg
                        append_danmaku(bvid, retry, seen)
                    except Exception as e:
                        print(f"[Phase 2] 警告: 分P {pg} 实时弹幕补采失败（{e}），下次重试")
                        remaining.append(pg)
                video_info["realtime_failed_pages"] = remaining
            else:
                video_info["realtime_failed_pages"] = []
            save_video_info(bvid, video_info)
            command_dms = fetch_command_dms(video_info, client)
            video_info["command_dms"] = command_dms
            # 哨兵 format 存在说明是新流程落的数据：有它而无 done 必为半成品；
            # 三者皆无才是本功能上线前的旧版完整落库。否则中断点早于首个检查点时
            # （库里只有实时池、无任何 danmaku 检查点）会被误判为完整而永久跳过历史补采
            if done == "1":
                # 已完整采集：仍调用历史采集函数做滚动补采（done 感知：内部自动回拨
                # last_date 只补采最近几天、幂等快速返回），补采失败降级沿用库内数据；
                # 落库按 dmid 去重，弹幕总量统计读库重算，重复运行幂等
                if HISTORY_DANMAKU_ENABLED:
                    try:
                        # 逐分P滚动补采（已 done 的分P 只补最近几天；新增分P 从零续采）
                        _fetch_history_all_pages(video_info, client, bvid)
                    except Exception as e:
                        print(f"[Phase 2] 警告: 历史弹幕滚动补采失败（{e}），沿用库内已有数据")
                danmaku_list = load_danmaku(bvid)
                print(f"[Phase 2] 断点续采：从库读回 {len(danmaku_list)} 条弹幕，跳过弹幕重采")
            elif done is None and last is None and fmt is None:
                # 旧版完整落库：直接读库，跳过弹幕网络重采
                danmaku_list = existing
                print(f"[Phase 2] 断点续采：从库读回 {len(danmaku_list)} 条弹幕，跳过弹幕重采")
            else:
                # 半成品：历史弹幕从检查点续采（实时池已在库）
                if HISTORY_DANMAKU_ENABLED:
                    history_new_list = _fetch_history_all_pages(video_info, client, bvid)
                else:
                    history_new_list = []
                    # 不写 done：历史模块会把分P1 的 done=1 当成历史已完成。
                    if not video_info.get("realtime_failed_pages"):
                        set_phase_state(bvid, "danmaku", "phase_done", "1")
                danmaku_list = load_danmaku(bvid)
                # 半成品续采完成后补算覆盖率（此前只有全新路径会写）：沿用全新路径
                # 同一算法，但实时池数量以续采前库内已有条数近似（含此前已采的部分历史，
                # 无法精确拆分），history 只含本轮新采快照，merged 为库内全量
                if "danmaku_coverage" not in video_info:
                    video_info["danmaku_coverage"] = {
                        "realtime": len(existing),
                        "history": len(history_new_list),
                        "history_new": len(danmaku_list) - len(existing),
                        "merged": len(danmaku_list),
                    }
                    save_video_info(bvid, video_info)
                print(f"[Phase 2] 断点续采：历史弹幕续采完成，库内共 {len(danmaku_list)} 条")
            if video_info.get("realtime_failed_pages"):
                # 下一轮只重试实时池，不能把已经完成的分P1 历史 done 清掉。
                set_phase_state(bvid, "danmaku", "realtime_ok", "0")
            return video_info, danmaku_list, group_by_sender(danmaku_list), command_dms

    # 视频不存在/已删除或触发风控时给友好中文提示并以非零码退出，不再抛原始 traceback
    try:
        video_info, danmaku_list, sender_groups = collect_danmaku_data(bvid, client)
    except DanmakuCollectionError as e:
        video_info, danmaku_list, sender_groups = e.video_info, [], {}
        print(f"[Phase 2] 警告: {e}；本轮继续历史弹幕和评论分析")
    except Exception as e:
        print(f"[Phase 2] 错误: 视频信息/弹幕获取失败: {e}")
        print("[Phase 2] 可能原因: 视频不存在或已删除、BV号错误、或触发风控，请检查后重试")
        raise SystemExit(1)

    # 新格式哨兵：必须先于任何数据落库写入（先于实时池 append 与历史逐日检查点），
    # 否则中断点早于首个检查点时重跑无法与旧版完整落库区分
    set_phase_state(bvid, "danmaku", "format", "v2")
    # 先记录待采分P；即使实时池落库后、历史采集中断，也能重试未确认的实时页。
    pending_info = dict(video_info, realtime_failed_pages=list(
        range(1, len(video_info.get("pages") or [1]) + 1)))
    save_video_info(bvid, pending_info)

    # 实时弹幕池先增量落库存底（中断后重跑可基于库内数据续采历史弹幕）
    seen_dmids: set = set()
    try:
        append_danmaku(bvid, danmaku_list, seen_dmids)
        save_video_info(bvid, video_info)
    except Exception as e:
        print(f"[Phase 2] 警告: 弹幕落库失败（{e}），web.py 弹幕浏览器将无本视频数据")
        video_info["realtime_failed_pages"] = pending_info["realtime_failed_pages"]

    # 实时弹幕池只保留最近几千条；开启历史弹幕时逐日拉取弹幕池快照补全历史
    # （bvid 传入后逐日增量落库+检查点，中断可续；fetch 内部完成时置 done=1）
    if HISTORY_DANMAKU_ENABLED:
        try:
            history_list = _fetch_history_all_pages(video_info, client, bvid, seen_dmids)
            merged = load_danmaku(bvid)
            video_info["danmaku_coverage"] = {
                "realtime": len(danmaku_list),
                "history": len(history_list),
                "history_new": len(merged) - len(danmaku_list),
                "merged": len(merged),
            }
            danmaku_list, sender_groups = merged, group_by_sender(merged)
        except Exception as e:
            print(f"[Main] 警告：历史弹幕采集失败，降级为仅实时弹幕池: {e}")
    elif not video_info.get("realtime_failed_pages"):
        # 历史采集关闭：phase_done 表示本阶段已处理完，避免每次重采实时池。
        # 不写 done=1，否则之后打开历史采集会把分P1 当成已完成、只补最近几天。
        set_phase_state(bvid, "danmaku", "phase_done", "1")

    # 落库时机在历史合并之后：danmaku_coverage 由上方写入 video_info，
    # 提前保存会导致 Web 概览页拿不到覆盖率
    if video_info.get("realtime_failed_pages"):
        set_phase_state(bvid, "danmaku", "realtime_ok", "0")
    save_video_info(bvid, video_info)

    print(f"[Phase 2] 已落库 {len(load_danmaku(bvid))} 条弹幕（danmaku 表）")

    # 互动弹幕（含明文mid，需SESSDATA；失败降级不影响主流程）
    command_dms = fetch_command_dms(video_info, client)
    video_info["command_dms"] = command_dms

    return video_info, danmaku_list, sender_groups, command_dms


def build_video_meta_uid_map(video_info: dict) -> dict[str, set[int]]:
    """视频元信息明文 UID 源（P2-b）：UP主本人 + 联合投稿 staff + 简介@提及
    （desc_v2 中 type=1 的 biz_id 即被@用户 UID）。返回 {crc32_hex: {uid, ...}}，
    供阶段4并入交叉验证映射；这些用户不一定是弹幕发送者，只有 mid_hash 命中时才生效。"""
    uids: set[int] = set()
    owner_mid = (video_info.get("owner") or {}).get("mid")
    if owner_mid:
        uids.add(int(owner_mid))
    for st in video_info.get("staff") or []:        # 联合投稿成员
        if st.get("mid"):
            uids.add(int(st["mid"]))
    for d in video_info.get("desc_v2") or []:       # 简介里的 @提及
        if d.get("type") == 1 and d.get("biz_id"):
            uids.add(int(d["biz_id"]))
    result = {}
    for uid in uids:
        add_uid(result, calc_crc32(uid), uid)
    return result


def select_problem_comment_authors(comments: list[dict], comment_problems: dict) -> dict[int, dict]:
    """问题评论作者直引画像（P0-a）：严重度>=COMMENT_AUTHOR_MIN_SEVERITY 或
    命中条数>=COMMENT_AUTHOR_MIN_HITS 的作者，凭评论明文 UID 直接进画像名单。

    返回 {uid: {"hits": 命中条数, "max_severity": 最高严重度}}（未过阈值的不含）。"""
    rpid_uid = {c.get("rpid"): c.get("uid") for c in comments if c.get("rpid") and c.get("uid")}
    stats: dict[int, dict] = {}
    for rpid, v in (comment_problems or {}).items():
        uid = rpid_uid.get(rpid)
        if not uid:
            continue
        st = stats.setdefault(uid, {"hits": 0, "max_severity": 0})
        st["hits"] += 1
        st["max_severity"] = max(st["max_severity"], v.get("severity", 1))
    return {uid: st for uid, st in stats.items()
            if st["max_severity"] >= COMMENT_AUTHOR_MIN_SEVERITY
            or st["hits"] >= COMMENT_AUTHOR_MIN_HITS}


def sync_problem_comment_authors(bvid: str, comments: list, resolved: dict) -> int:
    """扣掉误报评论后直引问题评论作者，并删掉本轮不再入选的 cmt: 发送者。

    误报（false_positive kind=cmt）不参与计数。本轮名单外的 cmt: 行，以及 uid
    已经在弹幕解析名单里的 cmt: 行，连同「该 uid 已无其他发送者」时的画像一起删。
    非 cmt: 行不动。返回本轮新写入的人数。
    """
    existing_uids = {info["uid"] for key, info in resolved.items()
                     if info.get("uid") and not str(key).startswith("cmt:")}
    fp_cmt = {target for kind, target in load_false_positives(bvid) if kind == "cmt"}
    current_verdicts = {}
    for comment in comments:
        if not comment.get("problem"):
            continue
        if str(comment.get("rpid")) in fp_cmt:
            continue
        current_verdicts[comment["rpid"]] = {
            "category": comment["problem"],
            "severity": comment.get("problem_severity") or 1,
        }
    cmt_authors = select_problem_comment_authors(comments, current_verdicts)
    keep = {uid for uid in cmt_authors if uid not in existing_uids}
    for key in list(resolved):
        if str(key).startswith("cmt:") and resolved[key].get("uid") not in keep:
            resolved.pop(key, None)
    stale = [row["mid_hash"] for row in load_senders(bvid)
             if str(row["mid_hash"]).startswith("cmt:") and row.get("uid") not in keep]
    if stale:
        delete_senders_by_mid_hash(bvid, stale)
    added = 0
    for uid, stats in cmt_authors.items():
        if uid not in keep:
            continue
        key = f"cmt:{uid}"
        resolved[key] = {"uid": uid, "confidence": "高", "method": "问题评论",
                         "user_info": {}, "danmaku_count": 0, "contents": [],
                         "spam_level": "低", "spam_score": 0.0, "collision_risk": False,
                         "comment_problem": stats}
        save_sender(bvid=bvid, mid_hash=key, uid=uid, confidence="高", method="问题评论",
                    danmaku_count=0, contents=[], spam_level="低", spam_score=0.0)
        added += 1
    if added:
        print(f"[Phase 4+] 问题评论作者直引: {added} 人并入画像名单"
              f"（严重度≥{COMMENT_AUTHOR_MIN_SEVERITY} 或 命中≥{COMMENT_AUTHOR_MIN_HITS} 条，"
              f"共候选 {len(cmt_authors)} 人）")
    return added


def phase_comment(video_info: dict, client, resume: bool = True):
    """阶段3: 采集评论 + 充电名单（失败不影响后续流程）

    断点续采（resume=True 的非 --force 路径）：comments 表有完整数据
    （done=1，或旧版落库——判据为新格式哨兵 format 缺失）→ 直接读库跳过网络重采；
    truncated=1 且游标已到页数上限时同样走增量刷新，但不把截断写成 done；
    有部分数据（哨兵 format 存在但无 done、也还没到上限）→ collect_comment_data 内部
    从游标续页，已入库评论靠 UNIQUE 约束去重。"""
    print("\n[Phase 3/6] 采集评论区数据...")
    bvid = video_info.get("bvid", "")
    aid = video_info.get("aid", 0)
    # 充电名单（1 个请求，各路径都新鲜拉取）
    up_mid = (video_info.get("owner") or {}).get("mid", 0)

    if resume and bvid:
        existing = load_comments(bvid)
        if existing:
            done = get_phase_state(bvid, "comment", "done")
            mode = get_phase_state(bvid, "comment", "mode")
            fmt = get_phase_state(bvid, "comment", "format")
            try:
                comment_page = int(get_phase_state(bvid, "comment", "page") or 0)
            except (TypeError, ValueError):
                comment_page = 0
            # 截断到采集上限后，续采的页码区间是空的，重跑会零请求返回且永远不刷新。
            # 按「已到采集上限」走增量刷新；截断不是采完，这里不写 done。
            truncated_at_cap = (get_phase_state(bvid, "comment", "truncated") == "1"
                                and comment_page >= MAX_COMMENT_PAGES)
            # 哨兵 format 存在而无 done 必为半成品；三者皆无才是旧版完整落库。
            # 否则中断点早于首个游标检查点时会被误判为完整而永久跳过剩余翻页
            old_complete = done is None and mode is None and fmt is None
            if done == "1" or old_complete or truncated_at_cap:
                # 完整数据（含旧版完整落库）：先按时间序增量刷新（新评论撞整页已见即停，
                # 旧评论顺带刷新热度；失败降级沿用库内数据），再读库
                if aid:
                    try:
                        refresh_comments(aid, client, bvid)
                        existing = load_comments(bvid)
                    except Exception as e:
                        print(f"[Phase 3] 警告: 评论增量刷新失败（{e}），沿用库内已有数据")
                if truncated_at_cap and done != "1" and not old_complete:
                    print(f"[Phase 3] 评论已到采集上限（{len(existing)} 条），改为增量刷新，不写 done")
                else:
                    print(f"[Phase 3] 断点续采：从库读回 {len(existing)} 条评论，跳过评论重采")
                charge_uid_map = fetch_charge_uid_map(bvid, aid, up_mid, client) if up_mid else {}
                comment_uid_map = build_comment_uid_map(existing)
                # 纯 UID 收割并入评论明文映射（收割源同为评论明文，证据等级一致）
                if aid:
                    try:
                        comment_uid_map = merge_uid_maps(comment_uid_map, harvest_comment_uids(aid, client, bvid))
                    except Exception as e:
                        print(f"[Phase 3] 警告: 评论UID收割失败（{e}），不影响已有映射")
                return (existing, comment_uid_map,
                        build_comment_location_map(existing), charge_uid_map)
            # 半成品：游标续页（collect_comment_data 返回库内全量）
            print(f"[Phase 3] 断点续采：评论从检查点续采（库内已有 {len(existing)} 条）")

    if not aid:
        print("[Phase 3] 警告: 未获取到有效 aid，跳过评论采集（将仅用CRC32破解）")
        return [], {}, {}, {}
    comments, comment_uid_map, comment_location_map = [], {}, {}
    try:
        comments, comment_uid_map, comment_location_map = collect_comment_data(aid, client, bvid=bvid or None)
    except Exception as e:
        print(f"[Phase 3] 评论采集失败 (将仅用其他来源): {e}")
        # 页数据已在采集途中落库。异常不能把内存列表清成空，否则后续画像丢掉已有评论。
        if bvid:
            comments = load_comments(bvid)
            comment_uid_map = build_comment_uid_map(comments)
            comment_location_map = build_comment_location_map(comments)
    # 纯 UID 收割并入评论明文映射（收割源同为评论明文，证据等级一致），失败不影响主流程
    if bvid:
        try:
            comment_uid_map = merge_uid_maps(comment_uid_map, harvest_comment_uids(aid, client, bvid))
        except Exception as e:
            print(f"[Phase 3] 警告: 评论UID收割失败（{e}），不影响已有映射")
    # 充电名单（独立降级：评论失败也照常尝试）
    charge_uid_map = {}
    if up_mid:
        charge_uid_map = fetch_charge_uid_map(bvid, aid, up_mid, client)
    return comments, comment_uid_map, comment_location_map, charge_uid_map


def phase_resolve(bvid: str, sender_groups: dict, comment_uid_map: dict, client,
                  max_users: int | None = None, charge_uid_map: dict | None = None,
                  command_uid_map: dict | None = None, meta_uid_map: dict | None = None,
                  spam_results: dict | None = None, cringe_results: dict | None = None,
                  report_cache_only: bool = False):
    """兴趣阈值控制新解析；已有结论按版本、证据指纹及有效期复核。"""
    print("\n[Phase 4/6] 解析发送者UID...")
    cached = {r["mid_hash"]: r for r in load_senders(bvid)}
    sources = [(METHOD_COMMENT_VERIFY, comment_uid_map), ("充电名单", charge_uid_map or {}),
               ("互动弹幕", command_uid_map or {}), ("视频信息", meta_uid_map or {})]
    save_uid_evidence_maps(bvid, sources)
    mapping, methods = build_evidence_maps(bvid, sources, load_global_uid_map())
    spam_results, cringe_results = spam_results or {}, cringe_results or {}
    interested = [h for h in sender_groups if spam_results.get(h, {}).get("spam_level") in ("高", "中")
                  or cringe_results.get(h, {}).get("count", 0) >= 1]
    interested.sort(key=lambda h: (spam_results.get(h, {}).get("spam_score", 0),
                    cringe_results.get(h, {}).get("max_severity", 0), sender_groups[h]["count"]), reverse=True)
    cap = max_users if max_users is not None else min(MAX_ANALYZE_USERS_HARD_CAP,
        max(ANALYZE_USERS_FLOOR, int(len(sender_groups) * ANALYZE_USERS_RATIO)))
    selected = set(interested[:max(0, cap)])
    todo, results = {}, {}
    for h, group in sender_groups.items():
        row = cached.get(h)
        if row and resolution_is_current(row, h, mapping, methods):
            record = row["resolution"]
            results[h] = {**group, **row, "danmaku_count": group["count"], "contents": group["contents"],
                          "user_info": {}, "collision_risk": record.get("collision_risk", True),
                          "candidates": record.get("candidates", [])}
        elif h in selected or (row and h in mapping):
            # 明文证据变化只需本地判断，不受兴趣阈值和网络名额限制。
            todo[h] = group
        elif row:
            # 老的猜测不继续用作归属；保留旧 UID 作审计，等待命中选人阈值再验证。
            verdict = (None, "无", METHOD_PENDING, {}, True, (row.get("resolution") or {}).get("candidates", []))
            record = resolution_record(h, verdict, mapping, methods)
            record.update(previous_uid=row["uid"] or (row.get("resolution") or {}).get("previous_uid"),
                          reason="旧结论失效，本轮未入选验证名单")
            results[h] = {**group, "uid": None, "confidence": "无", "method": METHOD_PENDING,
                          "danmaku_count": group["count"], "user_info": {}, "collision_risk": True,
                          "candidates": verdict[-1], "resolution": record}
    if todo:
        results.update(resolve_all_senders(todo, mapping, client, method_map=methods))
    from candidate_cards import prepare_candidate_reports
    def save_resolution(h, info):
        old = cached.get(h)
        if old and old["uid"] != info["uid"]:
            info["resolution"]["previous_uid"] = old["uid"]
        save_sender(bvid, h, info["uid"], info["confidence"], info["method"],
                    info["danmaku_count"], info["contents"],
                    spam_results.get(h, {}).get("spam_level", (old or {}).get("spam_level", "低")),
                    spam_results.get(h, {}).get("spam_score", (old or {}).get("spam_score", 0.0)),
                    resolution=info["resolution"])
    for h, info in results.items():
        save_resolution(h, info)
    prepare_candidate_reports({h: info for h, info in results.items() if h in interested}, client,
                               on_progress=save_resolution, cache_only=report_cache_only)
    assigned = sum(v.get("uid") is not None for v in results.values())
    print(f"[Phase 4] 兴趣命中 {len(interested)}，验证名额 {len(selected)}，已分配候选 {assigned}/{len(results)}；分配不代表身份确认")
    return results


def phase_spam(bvid: str, sender_groups: dict, danmaku_list: list | None = None) -> dict:
    """阶段2.5: 刷屏检测（提前到解析前，以 spam_score 驱动兴趣分选人；
    检测完成后回写库中已存在行的真实结果；本轮新解析行由阶段4 save_sender 直接写入真实值）。

    danmaku_list 传入时附带做两件事（纯本地统计，不影响选人）：
    群体复读事件计数（全视频维度接龙/+1 队列检测，明细在报告概览页）与
    全池分布自检打印（P50/P90/P95，供阈值校准参考）。"""
    print("\n[Phase 2.5] 刷屏行为检测...")
    spam_results = batch_detect_spam(sender_groups)
    high_spam = sum(1 for v in spam_results.values() if v["spam_level"] == "高")
    med_spam = sum(1 for v in spam_results.values() if v["spam_level"] == "中")
    print(f"[Phase 2.5] 检测完成: 高风险 {high_spam} | 中风险 {med_spam}")

    dist = distribution_stats(spam_results)
    if dist.get("senders"):
        print(f"[Phase 2.5] 全池分布: 弹幕数 P50={dist['count_p50']:.0f}/P95={dist['count_p95']:.0f} | "
              f"重复率 P50={dist['repeat_p50']:.0%}/P95={dist['repeat_p95']:.0%} | "
              f"刷屏分 P50={dist['score_p50']:.2f}/P95={dist['score_p95']:.2f}")
    if danmaku_list:
        events = detect_repeat_events(danmaku_list)
        print(f"[Phase 2.5] 群体复读事件: {len(events)} 起"
              + (f"（最多一起 {events[0]['sender_count']} 人同刷）" if events else ""))

    # 回写库中已存在行的真实检测结果（缓存行；本轮新解析行由阶段4 save_sender 直接写入真实值，此处 UPDATE 对其命中 0 行无影响）
    for mid_hash, result in spam_results.items():
        update_sender_spam(bvid, mid_hash, result["spam_level"], result["spam_score"])
    print(f"[Phase 2.5] 已回写 {len(spam_results)} 个发送者的刷屏检测结果")
    return spam_results


def phase_cringe(danmaku_list: list, sender_groups: dict, video_info: dict) -> dict:
    """阶段2.6: 问题弹幕检测（LLM，未配置 Key 或失败时返回空 dict 降级）"""
    print("\n[Phase 2.6] 问题弹幕检测（LLM）...")
    try:
        return detect_cringe_danmaku(danmaku_list, sender_groups, video_info)
    except Exception as e:
        print(f"[Phase 2.6] 警告: 问题弹幕检测失败（{e}），降级跳过")
        return {}


def phase_comment_cringe(comments: list, video_info: dict) -> dict | None:
    """阶段3.5: 问题评论检测（LLM）

    返回 {rpid: {category, severity, reason}}；未配置 Key / 无评论 / 检测失败返回 None。
    成功结果附 judged_rpids，只有成功覆盖的评论能清除旧标注；失败范围保留。"""
    if not comments:
        return None
    if not LLM_API_KEY:
        print("\n[Phase 3.5] 问题评论检测：未配置 LLM_API_KEY，跳过")
        return None
    print("\n[Phase 3.5] 问题评论检测（LLM）...")
    try:
        return detect_problem_comments(comments, video_info)
    except Exception as e:
        print(f"[Phase 3.5] 警告: 问题评论检测失败（{e}），降级跳过")
        return None


def phase_collect_users(resolved: dict, pool, max_users: int | None = None, force: bool = False,
                         cache_only: bool = False):
    """阶段5: 深度采集用户数据（名单已由阶段4兴趣定员；max_users 为手动硬上限
    （--max-users 传入时），None 默认采集100人；成功立即落库可断点续采；force=True 跳过缓存强制重采）

    cache_only=True（--skip-collect）：只读库内已采数据，完全不发起网络请求——阶段5
    请求量最大（每人约 21 个请求）、最易触发风控；未采用户本轮不生成画像，去掉参数重跑即续采。

    组合池（ComboPool）：每个请求由池透明接管，风控保留节点并进入出口冷却，
    代理连接故障才切节点；预算耗尽抛 RiskControlError，本 uid 按失败跳过。"""
    print("\n[Phase 5/6] 深度采集用户信息...")
    if max_users is None:
        max_users = max(1, int(getattr(config, "PROFILE_DEEP_MAX_USERS", 100)))

    # 筛选需要采集的用户（有UID且置信度 acceptable）
    uids_to_collect = []
    for mid_hash, info in resolved.items():
        uid = info.get("uid")
        confidence = info.get("confidence", "无")
        if uid and confidence in ("高", "中"):
            uids_to_collect.append((mid_hash, uid))

    # 按弹幕数量降序（名单已由阶段4兴趣定员；仅在显式传入 max_users 时按上限截断）
    # 这里我们没法直接排序，因为resolved没有原始count，但我们可以 trust danmaku_count
    uids_to_collect.sort(key=lambda x: resolved[x[0]]["danmaku_count"], reverse=True)

    # 同一 uid 可能被多个 mid_hash 命中（如评论交叉验证与 CRC32 破解指向同一人），
    # 去重后只采集一次，后续 mid_hash 在阶段6复用 user_data_map 中同一份数据
    seen_uids = set()
    deduped = []
    for mid_hash, uid in uids_to_collect:
        if uid in seen_uids:
            continue
        seen_uids.add(uid)
        deduped.append((mid_hash, uid))
    skipped_dup = len(uids_to_collect) - len(deduped)
    if skipped_dup > 0:
        print(f"[Phase 5] 去重: {skipped_dup} 个 mid_hash 指向重复 UID，合并采集")
    uids_to_collect = deduped[:max_users] if max_users is not None else deduped

    total = len(uids_to_collect)
    print(f"[Phase 5] 需采集用户: {total} 人" + (f" (上限 {max_users})" if max_users is not None else ""))

    # --skip-collect：只读库内已采数据，一个请求都不发。未采用户本轮不生成画像，
    # 去掉该参数重跑时会被上方缓存命中逻辑自动续采，不丢数据
    if cache_only:
        user_data_map: dict = {}
        hit = miss = read_fail = 0
        for mid_hash, uid in uids_to_collect:
            try:
                cached_row = load_user_data(uid) if has_user_data(uid) else None
            except Exception as e:
                cached_row = None
                read_fail += 1
                print(f"  [警告] UID:{uid} 缓存读取失败（{e}），按未采集处理")
            if cached_row:
                user_data_map[uid] = cached_row[0]
                hit += 1
            else:
                miss += 1
        print(f"[Phase 5] 跳过采集（--skip-collect）: 库内命中 {hit} 人，未采集 {miss} 人"
              + (f"（其中 {read_fail} 人缓存读取失败）" if read_fail else "")
              + "；未采集者本轮不出画像")
        if not hit:
            print("[Phase 5] 警告: 库内无该视频的已采用户数据，本轮不会生成任何用户画像"
                  "（报告仅剩弹幕/评论相关标签页）")
        return user_data_map

    user_data_map = {}
    processed = set()
    print_lock = threading.Lock()   # 单用户日志原子输出锁：多线程分片并行时不交错

    def collect_one(mid_hash, uid, sub_pool, idx):
        """采集单个用户：缓存命中跳过；成功立即落库（断点续采），降速倍率回落一档。
        组合池接管风控轮换与兜底冷却；兜底耗尽抛 RiskControlError，按失败跳过本 uid。
        缓存读写（sqlite）异常同样纳入逐人容错：打印警告降级处理，
        不得从 fut.result() 炸穿整个阶段5。返回 (uid, data|None)。

        该用户的采集过程日志进行级缓冲（log=buf.append），完成时持锁一次性
        整块输出——多号并行分片下不再出现 A/B 两个用户的日志逐行交错。"""
        with print_lock:
            print(f"  [{idx}/{total}] 采集 UID:{uid}...")
        # 检查是否已缓存（--force 时跳过缓存强制重采，结果覆盖写 users 表）
        if not force:
            try:
                cached = load_user_data(uid) if has_user_data(uid) else None
            except Exception as e:
                cached = None
                with print_lock:
                    print(f"  [警告] UID:{uid} 缓存读取失败（{e}），按未缓存处理")
            if cached:
                with print_lock:
                    print(f"  [缓存] UID:{uid} 使用已采集数据")
                return uid, cached[0]
        buf: list[str] = []
        try:
            data = collect_user_data(uid, sub_pool, log=buf.append)
        except Exception as e:
            data = {"error": str(e)}
        if "error" in data:
            # 失败不落库，重跑时会重新采集
            with print_lock:
                for line in buf:
                    print(line)
                print(f"  [{idx}/{total}] [失败] UID:{uid} {data['error']}")
            return uid, None
        # 立即落库：Ctrl+C 中断后已采集数据不丢失，重跑时命中上方缓存跳过。
        # profile 暂存空 dict，阶段6分析后以 INSERT OR REPLACE 覆盖
        save_err = None
        try:
            save_user_data(uid, data.get("name", ""), data.get("level", 0), data, {})
        except Exception as e:
            # 落库失败不丢内存数据：本轮画像分析仍可用，仅重跑时会重新采集
            save_err = e
        # 单元级成功：降速倍率回落一档（sub_pool 可能是裸 client，做鸭子兼容）
        used = sub_pool.current[1] if hasattr(sub_pool, "current") else sub_pool
        if hasattr(used, "reward_throttle_batch"):
            used.reward_throttle_batch()
        with print_lock:
            for line in buf:
                print(line)
            if save_err:
                print(f"  [警告] UID:{uid} 落库失败（{save_err}），本轮继续使用内存数据，重跑将重新采集")
        return uid, data

    # 多号并行分片：限速是 per-client 实例的，N 个账号并行 ≈ N 倍吞吐；
    # 子池风控只切节点不换号（其它号正被别的分片占用），长冷却兜底照旧
    shards = pool.shard_pools() if hasattr(pool, "shard_pools") else [pool]
    if len(shards) > 1 and total > 1:
        print(f"[Phase 5] 多号并行分片: {len(shards)} 个账号并行采集（限速按号独立）")
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def run_shard(sub_pool, tasks):
            """单个 worker 线程固定串行消费一个分片的全部任务。
            线程↔分片绑定：若共享提交队列按任务逐个分配子池，某线程长冷却时
            其它线程仍会用同一子池打请求，限速按号独立即失效；按分片分组后
            每组 submit 一个串行处理函数，保证一个子池同一时刻只被一个线程使用。"""
            return [collect_one(mh, uid, sub_pool, idx) for idx, (mh, uid) in tasks]

        # 任务按分片轮询分组（与原按提交序分配相同的分布），保留全局进度序号
        groups = [[] for _ in shards]
        for i, task in enumerate(enumerate(uids_to_collect, 1)):
            groups[i % len(shards)].append(task)

        # 手动管理 executor：正常路径 wait=True 收尾；Ctrl+C 时 cancel_futures
        # 取消未开始的排队任务并立即 re-raise，避免 with 语义的 shutdown(wait=True)
        # 把排队任务全跑完才退出（挂死）
        ex = ThreadPoolExecutor(max_workers=len(shards))
        interrupted = False
        try:
            futures = [ex.submit(run_shard, sp, g) for sp, g in zip(shards, groups) if g]
            for fut in as_completed(futures):
                for uid, data in fut.result():
                    if data is not None:
                        user_data_map[uid] = data
                        processed.add(uid)
        except KeyboardInterrupt:
            interrupted = True
            raise
        finally:
            if interrupted:
                ex.shutdown(wait=False, cancel_futures=True)
            else:
                ex.shutdown(wait=True)
    else:
        for idx, (mid_hash, uid) in enumerate(uids_to_collect, 1):
            uid, data = collect_one(mid_hash, uid, shards[0], idx)
            if data is not None:
                user_data_map[uid] = data
                processed.add(uid)

    print(f"\n[Phase 5] 采集完成: {len(processed)}/{total}")
    return user_data_map


def phase_analyze(resolved: dict, spam_results: dict, user_data_map: dict, sender_groups: dict,
                  comment_location_map: dict | None = None, uid_comments: dict | None = None,
                  video_info: dict | None = None, persist: bool = True):
    """阶段6: 画像分析

    comment_location_map（uid→评论IP属地）与 uid_comments（uid→本视频评论）在此处
    注入而非依赖落库数据：users 表缓存的旧 user_data 没有这两个字段，
    每次运行时注入才能保证缓存命中路径也带出属地与评论。

    video_info 仅用于判定是否多分P：danmaku.time 是「所在分P 内的相对秒数」，
    多分P 时报告必须把样本的分P 标出来，否则同一个 mm:ss 无法解释。
    """
    print("\n[Phase 6/6] 画像分析...")
    comment_location_map = comment_location_map or {}
    uid_comments = uid_comments or {}
    multi_page = len((video_info or {}).get("pages") or []) > 1
    profiles = []

    for mid_hash, info in resolved.items():
        uid = info.get("uid")
        if not uid or uid not in user_data_map:
            continue

        user_data = dict(user_data_map[uid])
        user_data.pop("ip_location", None)

        # 评论IP属地贯通：原样保留 API 返回格式（如 "IP属地：江苏"），无属地则不设该键
        if uid in comment_location_map:
            user_data["ip_location"] = comment_location_map[uid]
        spam = spam_results.get(mid_hash, {})

        # 构建弹幕统计：contents/video_times/video_pages 同源于本轮 sender_groups，
        # 三者按下标一一对应（见 danmaku.group_by_sender）；multi_page 供报告标注样本分P
        grp = sender_groups.get(mid_hash, {})
        danmaku_stats = {
            "count": info["danmaku_count"],
            "contents": info["contents"],
            "video_times": grp.get("video_times", []),
            "video_pages": grp.get("video_pages", []),
            "multi_page": multi_page,
        }

        # 逐人容错：单个用户分析/落库失败只跳过该用户，不中断整个阶段（对齐阶段5粒度）
        try:
            profile = analyze_profile(user_data, danmaku_stats, spam)
            # 碰撞风险标记传入报告，供"可能误识别"徽标展示
            profile["collision_risk"] = info.get("collision_risk", False)
            # 本视频评论（按点赞降序，至多10条）与问题弹幕聚合，供报告展示与 LLM 深掘证据包
            profile["comments"] = uid_comments.get(uid, [])[:10]
            profile["cringe"] = info.get("cringe", {})
            # 问题评论直引标记（P0-a）：命中统计透传画像，供报告卡片标注与风险排序
            if info.get("comment_problem"):
                profile["comment_problem"] = info["comment_problem"]
            profiles.append(profile)

            # 保存到数据库
            if persist:
                save_user_data(uid, user_data.get("name", ""), user_data.get("level", 0), user_data, profile, bvid=(video_info or {}).get("bvid"))
        except Exception as e:
            print(f"  [Phase 6] 警告: UID:{uid} 画像分析失败，已跳过: {e}")

    print(f"[Phase 6] 生成 {len(profiles)} 份画像")
    return profiles


def phase_ai_analysis(video_info: dict, profiles: list[dict], persist: bool = True):
    """阶段7: LLM 重点深掘（兴趣分 top K 单人单调用，结果直接注入 profile；
    全员粗筛已砍——命中人数扩大后粗筛是 token 大头，普通用户由规则标签勾画轮廓）"""
    if not LLM_API_KEY:
        print("\n[Phase 7] 跳过 (未在 config.py 或环境变量中设置 LLM_API_KEY)")
        return

    try:
        analyzer = LLMAnalyzer()
    except Exception as e:
        print(f"[Phase 7] LLM 初始化失败: {e}")
        return

    print("\n[Phase 7] LLM 重点深掘（兴趣分 top K 单人单调用）...")
    try:
        deep = analyzer.analyze_deep(profiles, video_info)
        for p in profiles:
            uid = p.get("uid")
            if uid in deep:
                p["ai_deep"] = deep[uid]
                # 深掘结果必须落库：web 报告读的是所属视频的 video_profiles，
                # 阶段6已在深掘前落库，这里注入后不回写的话报告页永远看不到
                try:
                    if persist:
                        update_user_profile(uid, p, bvid=video_info.get("bvid"))
                except Exception as e:
                    print(f"[Phase 7] 警告: UID:{uid} 深掘结果落库失败（{e}），重跑可补")
        print(f"[Phase 7] 完成: {len(deep)} 人生成深度画像")
    except Exception as e:
        print(f"[Phase 7] LLM 分析失败: {e}")


class PhaseTimer:
    """流水线阶段计时器：run(name, fn, *args) 计时执行并记录，summary() 打印
    各阶段耗时 + 总耗时（批量模式下每个视频各出一份）。"""

    def __init__(self):
        self.records: list[tuple[str, float]] = []
        self.start = time.time()

    def run(self, name: str, fn, *args, **kwargs):
        t0 = time.time()
        r = fn(*args, **kwargs)
        self.records.append((name, time.time() - t0))
        return r

    @staticmethod
    def _fmt(sec: float) -> str:
        sec = int(sec)
        h, sec = divmod(sec, 3600)
        m, s = divmod(sec, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def summary(self):
        total = time.time() - self.start
        print("\n[计时] 各阶段耗时：")
        for name, dt in self.records:
            print(f"[计时]   {name}: {self._fmt(dt)}")
        print(f"[计时] 总耗时: {self._fmt(total)}")


def _persist_sessions(pool) -> None:
    """把池内各账号当前的登录态回写磁盘（失败只警告，绝不影响分析结果）。

    B站会在活跃会话中续期 SESSDATA，但那只体现在内存 session 里；不回写的话文件里
    永远是登录那一刻的登录态，服务端一到期就彻底作废。save_cookie_if_current 会先比对
    磁盘上的 refresh_token，避免覆盖其它进程（web.py）刚刷新出的新登录态。"""
    try:
        accounts = pool.accounts
    except AttributeError:
        return
    saved = 0
    for name, client in accounts:
        try:
            path = None if name == "主号" else account_cookie_path(name)
            if save_cookie_if_current(client, path):
                saved += 1
        except Exception as e:
            print(f"[Auth] 账号 {name} 登录态回写失败（忽略）: {e}")
    if saved:
        print(f"[Auth] 已回写 {saved} 个账号的登录态（含服务端续期）")


def _comment_save_key(comment: dict) -> tuple:
    """用来判断这条评论是否已经按同样内容落过库。problem 由单独的回写更新，不参与比较。"""
    return (comment.get("rpid", 0), comment.get("uid"), comment.get("content", ""),
            comment.get("like", 0) or 0, comment.get("reply_count", 0) or 0,
            comment.get("ctime", 0) or 0, bool(comment.get("is_sub")),
            comment.get("root_rpid", 0) or 0, comment.get("parent_rpid", 0) or 0,
            comment.get("uname") or "", comment.get("location") or "")


def _comments_pending_save(bvid: str, comments: list) -> bool:
    """内存评论里是否有库中还没有的内容。空列表返回假，避免把已有评论当成需要覆盖。"""
    pending = [_comment_save_key(c) for c in comments if c.get("uid") and c.get("content")]
    if not pending:
        return False
    stored = {_comment_save_key(c) for c in load_comments(bvid)}
    return any(row not in stored for row in pending)


def _deep_enabled(skip_deep: bool = False) -> bool:
    """阶段7（LLM 重点深掘）是否执行：全局开关与单次 --skip-deep 必须同时为真。

    LLM_DEEP_ENABLED（config.py）是"永久关"；--skip-deep 是"这次不跑"——
    两者取与，便于同一份配置下临时省 token/时间（深掘结果有 deep:{uid} 缓存，
    下次开启只补没算过的人）。"""
    return bool(LLM_DEEP_ENABLED) and not skip_deep

def run_analysis(bvid: str, force: bool = False, max_users: int | None = None, launch_web: bool = True,
                 skip_collect: bool = False, port: int | None = None, skip_deep: bool = False):
    """
    执行完整分析流程

    launch_web=False 供批量模式使用（避免逐视频开浏览器标签页）
    skip_collect=True（--skip-collect）：阶段5 只读库内已采数据、不发起采集请求
    skip_deep=True（--skip-deep）：跳过阶段7 LLM 重点深掘（只少 ai_deep 文字画像，
    其余判定/画像/报告照常；深掘结果有缓存，之后开启只补没算过的人）
    port（--port）：Web 报告端口；写入 PROFILER_PORT 环境变量，供 web_autostart
    拉起子进程与拼接报告页 URL 时读取（Windows 上用参数比设环境变量可靠）
    """
    print_banner()
    timer = PhaseTimer()   # 全流程计时：各阶段耗时 + 总耗时，结束时打印

    # 初始化数据库
    init_db()

    # --max-users 夹紧到 >=1（对齐 quick_test）：0/负数会让下游切片语义反转
    if max_users is not None:
        max_users = max(1, max_users)

    # --port：写入环境变量后，web_autostart.maybe_launch_web 拉起的 web.py 子进程
    # （继承环境）与它拼接的报告页 URL 会一致地用该端口；未传则沿用现有环境/默认 8001
    if port is not None:
        os.environ["PROFILER_PORT"] = str(port)

    # 阶段1: 登录
    client = timer.run("阶段1 登录", phase_login)

    # 账号组合池：稳定入口绑定，风控冷却，连接故障才切节点/按入口降级。
    # 显式外部配置优先，有订阅时优先内置隔离核心，再探测外部静态入口。
    pool = timer.run("阶段1+ 建组合池", build_pool, client)

    # --force: 登录成功后清除该视频的全部缓存，后续阶段全部重新采集
    # （放在登录后，避免登录失败/取消时缓存已清但新数据未采）
    if force:
        clear_video_cache(bvid)
        print(f"[Main] --force 已清除 {bvid} 的缓存，全部重新采集")

    # --skip-collect 提示：明确本轮不碰阶段5，避免误解为"采集失败"
    if skip_collect:
        print("[Main] --skip-collect 已启用：阶段5 只读库内已采数据，不发起任何采集请求")
        if force:
            print("[Main] 警告: --skip-collect 与 --force 同用，--force 已清除该视频缓存"
                  "（含不再被其他视频引用的用户画像），本轮可能无画像可复用")

    # 断点续采：阶段2/3 各自按库内数据存在性独立判断（phase_danmaku/phase_comment
    # 内部的 resume 逻辑），弹幕/评论/解析/采集任意位置中断后重跑都能续上；
    # 要刷新数据用 --force（清除该视频全部缓存与检查点）。

    # 阶段2: 弹幕
    video_info, danmaku_list, sender_groups, command_dms = timer.run(
        "阶段2 弹幕采集", phase_danmaku, bvid, pool, resume=not force)

    if not danmaku_list:
        print("[Main] 当前无弹幕，继续采集和分析评论")

    # 阶段2.5: 刷屏检测（本地，提前到解析前驱动选人）
    spam_results = timer.run("阶段2.5 刷屏检测", phase_spam, bvid, sender_groups, danmaku_list)

    # 阶段2.6: 问题弹幕检测（LLM，可降级；批次级缓存，中断后重跑已完成批次直接命中）
    cringe_results = timer.run("阶段2.6 问题弹幕检测", phase_cringe, danmaku_list, sender_groups, video_info)

    # 阶段3: 评论 + 充电名单（comment_location_map 为 uid→IP属地，uid_comments 阶段6贯通进画像）
    comments, comment_uid_map, comment_location_map, charge_uid_map = timer.run(
        "阶段3 评论采集", phase_comment, video_info, pool, resume=not force)
    # 采集和刷新已经逐页落库。只有本轮内存里真有尚未入库的评论才写回
    # （例如采集失败前抓在内存里的页）。从库读出来再整表写只会抬修订号和 hit_count。
    # 空列表不写，正常重跑不会把已有评论清掉。
    try:
        if _comments_pending_save(bvid, comments):
            save_comments(bvid, comments)
    except Exception as e:
        print(f"    警告: 评论落库失败（{e}），跨视频足迹将缺评论")

    target_hashes = {h for h in sender_groups
                     if spam_results.get(h, {}).get("spam_level") in ("高", "中")
                     or cringe_results.get(h, {}).get("count", 0) >= 1}
    try:
        extra = supplement_uid_replies(video_info.get("aid") or 0, pool, bvid, comments,
                                       target_hashes, comment_uid_map)
        comment_uid_map = merge_uid_maps(comment_uid_map, extra)
    except Exception as e:
        print(f"[Phase 3] 警告: UID 定向补采失败（{e}），继续使用已有证据")

    # uid → 该用户在本视频的评论（按点赞降序），供阶段6注入画像与阶段7深掘证据包
    uid_comments: dict[int, list] = {}
    for c in comments:
        uid_comments.setdefault(c["uid"], []).append(c)
    for lst in uid_comments.values():
        lst.sort(key=lambda x: x.get("like", 0), reverse=True)

    # 阶段3.5: 问题评论检测（LLM，可降级）：结果回写 comments.problem 列（web 端高回复
    # 评论页标注）并就地注入 comment dict（uid_comments 共享同一批 dict 引用，阶段6画像
    # 的「TA 在本视频的评论」随之带出标注）
    comment_problems = timer.run("阶段3.5 问题评论检测", phase_comment_cringe, comments, video_info)
    if comment_problems is not None:
        # 只回写成功覆盖范围，成功空批次清旧标注，失败和截断批次保持原样。
        try:
            update_comment_problems(bvid, comment_problems,
                                    judged_rpids=getattr(comment_problems, "judged_rpids", None))
        except Exception as e:
            print(f"    警告: 问题评论回写失败（{e}），web 端评论标注将缺失")
        for c in comments:
            v = comment_problems.get(c.get("rpid"))
            if v:
                c["problem"] = v["category"]
                c["problem_severity"] = v.get("severity", 1)
            elif c.get("rpid") in getattr(comment_problems, "judged_rpids", comment_problems):
                c["problem"] = ""
                c["problem_severity"] = 0

    # 阶段4: UID解析（兴趣分驱动选人）
    resolved = timer.run("阶段4 UID解析", phase_resolve, bvid, sender_groups, comment_uid_map, pool,
                         max_users=max_users, charge_uid_map=charge_uid_map,
                         command_uid_map=build_command_uid_map(command_dms),
                         meta_uid_map=build_video_meta_uid_map(video_info),
                         spam_results=spam_results, cringe_results=cringe_results, report_cache_only=skip_collect)

    # 合并刷屏/问题弹幕数据到resolved（阶段5置信度过滤与阶段6画像注入均从此处取）
    for mid_hash in resolved:
        if mid_hash in spam_results:
            resolved[mid_hash]["spam_level"] = spam_results[mid_hash]["spam_level"]
            resolved[mid_hash]["spam_score"] = spam_results[mid_hash]["spam_score"]
        if mid_hash in cringe_results:
            resolved[mid_hash]["cringe"] = cringe_results[mid_hash]

    # 问题评论作者直引：扣误报后再并入，并清掉本轮不再入选的 cmt: 行。
    sync_problem_comment_authors(bvid, comments, resolved)

    # UID 终审必须在用户采集和画像之前完成：终审可能把歧义候选收敛到新 UID，
    # 也可能撤销旧 UID。这样本轮阶段5/6只会采集并生成最终归属的画像。
    try:
        from candidate_cards import recheck_ambiguous_candidates

        def _save_recheck(h, info):
            save_sender(bvid, h, info.get("uid"), info.get("confidence", "无"), info.get("method", ""),
                        info.get("danmaku_count", 0), info.get("contents", []),
                        info.get("spam_level", "低"), info.get("spam_score", 0.0),
                        resolution=info.get("resolution"))

        _stats = recheck_ambiguous_candidates(resolved, client, on_progress=_save_recheck)
        if _stats["targets"]:
            print(f"[Phase 4+] 歧义候选终审复核: 目标 {_stats['targets']} 人，复核 {_stats['checked']} 次，"
                  f"调整 {_stats['flipped']}，终审排除 {_stats['finalized']}，待下轮 {_stats['pending']}"
                  + ("（风控暂停，冷却后下次运行继续）" if _stats["stopped"] else ""))
    except Exception as e:
        print(f"[Phase 4+] UID终审复核异常（忽略，不影响后续采集）: {e}")

    # 阶段5: 用户采集（组合池透明接管风控轮换）
    user_data_map = timer.run("阶段5 用户采集", phase_collect_users, resolved, pool,
                              max_users=max_users if max_users is not None else getattr(config, "PROFILE_DEEP_MAX_USERS", 100),
                              force=force, cache_only=skip_collect)

    # 阶段6: 画像分析（评论IP属地/本视频评论/问题弹幕在此贯通进画像）
    profiles = timer.run("阶段6 画像分析", phase_analyze, resolved, spam_results, user_data_map,
                         sender_groups, comment_location_map, uid_comments, video_info)

    # 阶段7: LLM 重点深掘（结果在 phase 内直接注入 profile）
    # 跳过条件：config.LLM_DEEP_ENABLED=False（永久关）或本次 --skip-deep（临时关）
    if _deep_enabled(skip_deep):
        timer.run("阶段7 LLM深掘", phase_ai_analysis, video_info, profiles)
    else:
        why = "--skip-deep" if skip_deep else "config.py 中 LLM_DEEP_ENABLED=False"
        print(f"\n[Phase 7] 跳过 LLM 深掘（{why}）；其余报告内容不受影响")

    # 空视频或零画像也可能已完成常规分析；与目标模式共享的元信息明确区分。
    set_phase_state(bvid, "report", "ready", "1")

    # 阶段8: 会话回写。B站在会话中下发的续期 Set-Cookie 只存在内存 session 里，
    # 进程退出即丢；收尾时把仍有效的登录态落盘，让下次运行直接复用（含服务端续期）。
    _persist_sessions(pool)

    # 静态单文件 HTML 报告已被交互式 Web 报告（web.py）完全替换，不再生成 .html
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_base = os.path.join(REPORT_DIR, f"report_{bvid}_{ts}")

    # 同步导出 CSV/JSON（web.py 报告页提供下载链接）；导出失败只警告降级
    export_t0 = time.time()
    os.makedirs(REPORT_DIR, exist_ok=True)
    try:
        csv_path = export_base + ".csv"
        export_csv(profiles, csv_path)
        print(f"[Export] CSV 已导出: {csv_path}")
    except Exception as e:
        print(f"[Export] 警告: CSV 导出失败: {e}")
    try:
        json_path = export_base + ".json"
        export_json(video_info, profiles, json_path)
        print(f"[Export] JSON 已导出: {json_path}")
    except Exception as e:
        print(f"[Export] 警告: JSON 导出失败: {e}")
    timer.records.append(("导出 CSV/JSON", time.time() - export_t0))

    print("\n" + "=" * 60)
    print("  分析完成!")
    print(f"  视频: {video_info.get('title', '')}")
    print(f"  分析用户: {len(profiles)} 人")
    print("=" * 60)
    timer.summary()

    # 分析完毕自动启动 web.py 并打开报告页（WEB_AUTOSTART 可关；失败只打印 URL 降级）
    if launch_web:
        maybe_launch_web(bvid)
    else:
        print("  运行 python web.py 查看交互式报告")


def load_batch_bvids(path: str) -> list[str]:
    """读取批量 BV 号清单：逐行读取，忽略空行与 # 注释（含行内注释，
    如 "BV1xxx # 备注"），去重保持顺序"""
    bvids = []
    seen = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or line in seen:
                continue
            seen.add(line)
            bvids.append(line)
    return bvids


def run_batch(batch_file: str, force: bool = False, max_users: int | None = None,
              skip_collect: bool = False, port: int | None = None, skip_deep: bool = False):
    """批量分析：逐个视频调用 run_analysis，单个失败只警告不中断，最后打印汇总"""
    bvids = load_batch_bvids(batch_file)
    if not bvids:
        print(f"[Batch] 清单为空（无有效BV号）: {batch_file}")
        return

    total = len(bvids)
    print(f"[Batch] 共 {total} 个视频待分析（清单: {batch_file}）")

    succeeded = []
    failed = []
    for idx, bvid in enumerate(bvids, 1):
        print(f"\n{'=' * 60}")
        print(f"  [Batch {idx}/{total}] {bvid}")
        print(f"{'=' * 60}")
        if not bvid.startswith("BV"):
            print(f"[Batch] 警告: {bvid} 格式不正确（应以 BV 开头），跳过")
            failed.append(bvid)
            continue
        try:
            run_analysis(bvid, force=force, max_users=max_users, launch_web=False,
                         skip_collect=skip_collect, port=port, skip_deep=skip_deep)
            succeeded.append(bvid)
        except KeyboardInterrupt:
            # Ctrl+C 不再继续后续视频，但仍打印已完成的汇总
            print(f"\n[Batch] 用户中断，跳过后续 {total - idx} 个视频")
            failed.append(bvid)
            break
        except SystemExit as e:
            # 阶段函数对视频不存在/已删除/风控会 raise SystemExit——它继承
            # BaseException，不在此接住会击穿整个批量循环（后续视频全跳过、无汇总）
            print(f"\n[Batch] 警告: {bvid} 分析终止（退出码 {e.code}），继续下一个")
            failed.append(bvid)
        except LoginRequiredError as e:
            # 登录需要人工扫码：无人值守的批量继续跑只会逐个空等 3 分钟扫码超时，
            # 中止整批并给出指引（已完成的视频仍打印汇总）
            print(f"\n[Batch] 登录已失效且自动刷新失败（{e}），中止批量分析")
            print("[Batch] 请先运行 python login.py 重新扫码登录，再重跑批量")
            failed.append(bvid)
            break
        except Exception as e:
            print(f"\n[Batch] 警告: {bvid} 分析失败，继续下一个: {e}")
            failed.append(bvid)

    # 汇总
    print(f"\n{'=' * 60}")
    print(f"  批量分析汇总: 成功 {len(succeeded)} / 失败 {len(failed)} / 共 {total}")
    if failed:
        print(f"  失败列表: {', '.join(failed)}")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="B站弹幕发送者用户画像分析")
    parser.add_argument("bvid", nargs="?", help="视频BV号，如 BV1vu4y1b7Y9")
    parser.add_argument("target", nargs="?", help="目标弹幕；含正则元字符时自动使用正则搜索，否则完整匹配")
    matching = parser.add_mutually_exclusive_group()
    matching.add_argument("--literal", action="store_true", help="把第二个参数当作原文完整匹配（含标点时可用）")
    matching.add_argument("--regex", action="store_true", help="强制把第二个参数当作正则搜索")
    parser.add_argument("--target-history", action="store_true", help="目标模式额外补采历史弹幕；默认仅当前池和已有缓存")
    parser.add_argument("--target-pool", action="store_true", help="目标模式在命中后启用多账号/IP池；默认仅用主号限速客户端")
    parser.add_argument("--force", action="store_true",
                        help="常规模式清除该视频缓存重采；目标模式重新解析/采集入选者，保留共享采集检查点")
    parser.add_argument("--max-users", type=int, default=None,
                        help="覆盖分析人数上限；常规模式按兴趣阈值选人，目标模式按命中条数选人（默认最多1000）")
    parser.add_argument("--batch", metavar="FILE",
                        help="批量模式：从文件逐行读取BV号（忽略空行与 # 注释行）")
    parser.add_argument("--skip-collect", action="store_true",
                        help="跳过阶段5用户采集的网络请求：只用库内已采数据（未采用户本轮不出画像），"
                             "常规模式与 --force 同用会因缓存被清而无画像可用；目标模式保留已采资料")
    parser.add_argument("--skip-deep", action="store_true",
                        help="跳过阶段7 LLM 重点深掘（只少 ai_deep 文字画像，判定/画像/报告照常；"
                             "深掘有缓存，之后重跑只补没算过的人）")
    parser.add_argument("--port", type=int, default=None, metavar="PORT",
                        help="Web 报告端口（默认 8001；等价于设置 PROFILER_PORT，"
                             "但 Windows 的 cmd/PowerShell 上参数写法才不会踩坑）")
    args = parser.parse_args()

    if args.target is not None and args.batch:
        parser.error("目标弹幕分析暂不支持 --batch")
    if (args.literal or args.regex) and args.target is None:
        parser.error("--literal / --regex 需要提供目标弹幕")
    if (args.target_history or args.target_pool) and args.target is None:
        parser.error("--target-history / --target-pool 需要提供目标弹幕")
    selector = None
    if args.target is not None:
        from target_filter import make_selector
        try:
            selector = make_selector(args.target, "literal" if args.literal else "regex" if args.regex else "auto")
        except ValueError as e:
            parser.error(str(e))

    if args.batch:
        try:
            run_batch(args.batch, force=args.force, max_users=args.max_users,
                      skip_collect=args.skip_collect, port=args.port, skip_deep=args.skip_deep)
        except OSError as e:
            print(f"错误: 批量清单文件不可读: {args.batch} ({e})")
            sys.exit(1)
        return

    if not args.bvid:
        print("错误: 需提供 BV号 或 --batch 清单文件")
        sys.exit(1)

    bvid = args.bvid.strip()
    if not re.fullmatch(r"BV[A-Za-z0-9]+", bvid):
        print("错误: BV号格式不正确，应以 BV 开头且只包含字母和数字")
        sys.exit(1)

    try:
        if selector is not None:
            from target_analysis import run_target_analysis
            run_target_analysis(bvid, selector, force=args.force, max_users=args.max_users,
                                skip_collect=args.skip_collect, port=args.port, skip_deep=args.skip_deep,
                                target_history=args.target_history, target_pool=args.target_pool)
        else:
            run_analysis(bvid, force=args.force, max_users=args.max_users,
                         skip_collect=args.skip_collect, port=args.port, skip_deep=args.skip_deep)
    except RiskControlError as e:
        print(f"[Main] 风控兜底耗尽（{e}），本视频分析终止")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n\n[Exit] 用户中断，进度已保存，可重新运行恢复")
    except Exception as e:
        print(f"\n[Error] 分析失败: {e}")
        raise


if __name__ == "__main__":
    main()
