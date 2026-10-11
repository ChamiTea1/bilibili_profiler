"""目标模式的轻量取数：当前弹幕池、已有缓存与找到目标即停的评论搜索。"""
import hashlib
import json

import config
from api_client import RiskControlError
from comment import _parse_comment
from danmaku import get_video_info, fetch_all_danmaku, group_by_sender, DanmakuCollectionError
from storage import (load_video_info, save_video_info, load_danmaku, append_danmaku,
                     get_phase_state, set_phase_state, save_uid_evidence_maps)
from uid_evidence import add_uid, candidates_of
from uid_resolver import calc_crc32, METHOD_COMMENT_VERIFY


def _dm_key(dm: dict) -> tuple:
    if dm.get("dmid"):
        return ("dmid", str(dm["dmid"]))
    return tuple(dm.get(k) for k in ("page", "mid_hash", "content", "time", "timestamp", "mode", "color", "pool"))


def collect_target_danmaku(bvid: str, client, include_history: bool = False):
    """默认只更新实时池并复用已采弹幕，历史网络补采须显式开启。"""
    if include_history:
        import main
        return main.phase_danmaku(bvid, client, resume=True)
    cached_video = load_video_info(bvid) or {}
    cached_dms = load_danmaku(bvid)
    try:
        video = get_video_info(bvid, client)
    except Exception as e:
        if not cached_video:
            raise
        print(f"[Target] 视频信息更新失败，使用已存信息：{e}")
        video = dict(cached_video)
    video["bvid"] = bvid
    # 不把目标模式的实时池写成常规分析的完整历史缓存。
    if not cached_dms and get_phase_state(bvid, "danmaku", "format") is None:
        set_phase_state(bvid, "danmaku", "format", "v2")
    try:
        current = fetch_all_danmaku(video, client)
    except DanmakuCollectionError:
        current = []
        print("[Target] 当前弹幕池获取失败，继续匹配已存弹幕")
    seen = {_dm_key(dm) for dm in cached_dms}
    added = []
    for dm in current:
        key = _dm_key(dm)
        if key not in seen:
            seen.add(key)
            added.append(dm)
    append_danmaku(bvid, added, {dm["dmid"] for dm in cached_dms if dm.get("dmid")})
    if cached_video.get("danmaku_coverage"):
        video["danmaku_coverage"] = cached_video["danmaku_coverage"]
    command_dms = cached_video.get("command_dms") or []
    if command_dms:
        video["command_dms"] = command_dms
    save_video_info(bvid, video)
    combined = load_danmaku(bvid)
    print(f"[Target] 当前池 {len(current)} 条，复用已存 {len(cached_dms)} 条，去重后 {len(combined)} 条；本轮不补采历史")
    return video, combined, group_by_sender(combined), command_dms


def probe_comment_uids(video: dict, client, target_hashes: set[str], max_requests: int | None = None,
                       on_progress=None):
    """按页寻找目标并保留其原文，默认不限制页数；全部找到或接口边界即停。

    先查主评论及内嵌预览，仍缺目标才查隐藏回复。不调用全量评论采集，
    不保存其他作者正文。独立检查点将目标原文和下一页游标一同保存。
    max_requests 仅供调用方显式约束，正常目标流程不设置此上限。
    """
    budget = None if max_requests is None else max(0, max_requests)
    stats = {"requests": 0, "budget": budget, "target_count": len(target_hashes), "matched_count": 0,
             "reason": "disabled" if budget == 0 else "not_needed", "reply_requests": 0}
    result, matched_comments = {}, []
    oid, bvid = video.get("aid"), video.get("bvid", "")
    if not oid or not target_hashes or budget == 0:
        return result, matched_comments, stats
    digest = hashlib.sha256("|".join(sorted(target_hashes)).encode()).hexdigest()[:24]
    phase = f"target_uid_probe:v2:{digest}"
    try:
        state = json.loads(get_phase_state(bvid, phase, "state") or "{}")
    except (ValueError, TypeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    # 已翻完的搜索在下次重跑重新查看首页；找到的正文仍保留。
    if state.get("stage") == "end":
        state = {"comments": state.get("comments", []), "evidence": state.get("evidence", {})}
    mode, token, page = state.get("mode", "wbi"), state.get("token", ""), state.get("page", 0)
    if mode not in ("wbi", "legacy"):
        mode, token, page = "wbi", "", 0
    roots = state.get("roots", [])
    stage, root_index, reply_page = state.get("stage", "main"), state.get("root_index", 0), state.get("reply_page", 1)
    matched_comments = [c for c in state.get("comments", []) if isinstance(c, dict)]
    for h, uids in state.get("evidence", {}).items():
        if h in target_hashes:
            for uid in candidates_of(uids):
                add_uid(result, h, uid)
    seen_comments = {(c["uid"], str(c["rpid"])) for c in matched_comments}
    seen_roots = {str(r["rpid"]) for r in roots}
    seen_pages = set()
    last_signature = state.get("last_signature")

    def persist():
        save_uid_evidence_maps(bvid, [(METHOD_COMMENT_VERIFY, result)])
        payload = {"mode": mode, "token": token, "page": page, "stage": stage,
                   "roots": roots, "root_index": root_index, "reply_page": reply_page,
                   "comments": matched_comments, "evidence": {h: sorted(v) for h, v in result.items()},
                   "last_signature": last_signature}
        set_phase_state(bvid, phase, "state", json.dumps(payload, ensure_ascii=False))
        stats["matched_count"] = len(result)
        if on_progress:
            on_progress(matched_comments, dict(stats))

    def inspect(items, is_sub=False, root_rpid=0):
        for item in items:
            if not isinstance(item, dict):
                continue
            member = item.get("member")
            for uid in candidates_of(member.get("mid") if isinstance(member, dict) else None):
                h = calc_crc32(uid)
                if h not in target_hashes:
                    continue
                add_uid(result, h, uid)
                try:
                    parsed = _parse_comment(item, is_sub)
                except (AttributeError, TypeError, ValueError):
                    parsed = None
                if parsed:
                    key = (parsed["uid"], str(parsed["rpid"]))
                    if key not in seen_comments:
                        parsed["root_rpid"] = root_rpid if is_sub else 0
                        matched_comments.append(parsed)
                        seen_comments.add(key)

    print(f"[Target] 搜索 {len(target_hashes)} 个目标的评论，找到全部目标即停（无默认页数上限）")
    stats["reason"] = "searching"
    while budget is None or stats["requests"] < budget:
        if target_hashes <= result.keys():
            stats["reason"] = "found_all"
            break
        if stage == "replies" and root_index >= len(roots):
            stage, stats["reason"] = "end", "end"
            persist()
            break
        params = {"oid": oid, "type": 1}
        if stage == "replies":
            root = roots[root_index]
            url = config.COMMENT_REPLY_URL
            params.update(root=root["rpid"], pn=reply_page, ps=20)
        elif mode == "wbi":
            url = config.COMMENT_MAIN_WBI_URL
            params.update(mode=3, pagination_str=json.dumps({"offset": token}))
        else:
            url = config.COMMENT_MAIN_URL
            try:
                params.update(mode=3, next=int(token or 0), ps=20)
            except (TypeError, ValueError):
                stats["reason"] = "invalid_cursor"
                break
        stats["requests"] += 1
        stats["reply_requests"] += stage == "replies"
        try:
            response = client.get(url, params=params)
        except RiskControlError:
            stats["reason"] = "risk"
            break
        except Exception:
            response = None
        code = response.get("code") if isinstance(response, dict) else None
        if code in (-412, -352, -403):
            stats["reason"] = "risk"
            break
        data = response.get("data") if isinstance(response, dict) else None
        valid = code == 0 and isinstance(data, dict)
        if valid:
            replies = data.get("replies")
            cursor = data.get("cursor") or {}
            if replies is None and isinstance(cursor, dict) and cursor.get("is_end"):
                replies = []
            child_page = data.get("page")
            if replies is None and stage == "replies" and isinstance(child_page, dict) and child_page.get("count") == 0:
                replies = []
            valid = isinstance(replies, list) and (stage == "replies" or isinstance(cursor, dict))
        if not valid:
            if stage == "main" and mode == "wbi" and page == 0:
                mode, token = "legacy", ""
                continue
            stats["reason"] = "api_error"
            break
        signature_rows = []
        for item in replies:
            if isinstance(item, dict):
                member = item.get("member")
                signature_rows.append((str(item.get("rpid") or item.get("id") or ""),
                                       str(member.get("mid") if isinstance(member, dict) else "")))
        signature = json.dumps([stage, params.get("root"), signature_rows])
        if signature_rows and (signature == last_signature or signature in seen_pages):
            stats["reason"] = "repeated_page"
            break
        seen_pages.add(signature)
        last_signature = signature
        if stage == "main":
            for root in replies:
                if not isinstance(root, dict):
                    continue
                previews = root.get("replies") or []
                previews = previews if isinstance(previews, list) else []
                root_rpid = root.get("rpid") or root.get("id") or 0
                inspect([root])
                inspect(previews, is_sub=True, root_rpid=root_rpid)
                try:
                    count = int(root.get("rcount") or 0)
                except (ValueError, TypeError):
                    count = 0
                if root_rpid and count > len(previews) and str(root_rpid) not in seen_roots:
                    roots.append({"rpid": root_rpid})
                    seen_roots.add(str(root_rpid))
            page += 1
            ended = bool(cursor.get("is_end")) or not replies
            pagination = cursor.get("pagination_reply")
            next_token = (pagination.get("next_offset") if isinstance(pagination, dict) else None) if mode == "wbi" else cursor.get("next")
            if ended:
                stage = "replies"
            elif (next_token is None or str(next_token) == str(token)) and not target_hashes <= result.keys():
                stats["reason"] = "invalid_cursor"
                # 保存已发现原文，但不把未知的下一页写成首页或完成。
                page -= 1
                last_signature = state.get("last_signature")
                persist()
                break
            else:
                token = str(next_token)
        else:
            inspect(replies, is_sub=True, root_rpid=root["rpid"])
            pagination = data.get("page")
            try:
                total = int(pagination.get("count") or 0) if isinstance(pagination, dict) else 0
                size = int(pagination.get("size") or 20) if isinstance(pagination, dict) else 20
            except (TypeError, ValueError):
                total, size = 0, 20
            if not replies or (total > 0 and reply_page * max(1, size) >= total):
                root_index, reply_page = root_index + 1, 1
            else:
                reply_page += 1
        # 一页全部过完再判断，以保留同页 CRC32 碰撞候选。
        if target_hashes <= result.keys():
            stats["reason"] = "found_all"
        persist()
        print(f"[Target] 评论搜索第 {stats['requests']} 次请求：找到 {len(result)}/{len(target_hashes)} 个目标，保留 {len(matched_comments)} 条目标评论")
        if stats["reason"] == "found_all":
            break
    else:
        stats["reason"] = "budget"
    if stats["reason"] == "found_all":
        persist()
    stats["matched_count"] = len(result)
    print(f"[Target] 评论搜索结束：{stats['requests']} 次请求，找到 {len(result)}/{len(target_hashes)} 个目标（{stats['reason']}）")
    return result, matched_comments, stats
