"""按目标弹幕选人，复用采集/解析/画像流程，结果独立于常规视频报告。"""
from collections import Counter, defaultdict
from datetime import datetime
import csv
import json
import os
import re

import config
from exporter import _flatten_profile, _sanitize_csv_cell
from storage import (init_db, load_senders, load_global_uid_map, save_uid_evidence_maps,
                     load_target_analysis, save_target_analysis, get_phase_state,
                     load_comment_uids, load_comments)
from target_filter import analysis_id, make_selector, match_danmaku
from target_lookup import collect_target_danmaku, probe_comment_uids
from uid_evidence import add_uid
from candidate_cards import prepare_candidate_reports
from uid_resolver import (METHOD_COMMENT_VERIFY, METHOD_AMBIGUOUS, calc_crc32, resolve_all_senders,
                          build_evidence_maps, resolution_is_current)


def resolve_targets(bvid: str, groups: dict, sources: list, pool, previous: dict,
                    force: bool = False) -> dict:
    """筛选本身不是身份证据；沿用常规解析的版本/证据指纹/有效期校验。"""
    save_uid_evidence_maps(bvid, sources)
    mapping, methods = build_evidence_maps(bvid, sources, load_global_uid_map())
    cached = {r["mid_hash"]: r for r in load_senders(bvid)}
    cached.update(previous)
    todo, results = {}, {}
    for mid_hash, group in groups.items():
        row = cached.get(mid_hash)
        if not force and row and resolution_is_current(row, mid_hash, mapping, methods):
            record = row["resolution"]
            results[mid_hash] = {**group, **row, "contents": group["contents"],
                                 "danmaku_count": group["count"], "user_info": {},
                                 "candidates": record.get("candidates", []),
                                 "collision_risk": record.get("collision_risk", True)}
        else:
            todo[mid_hash] = group
    if todo:
        results.update(resolve_all_senders(todo, mapping, pool, method_map=methods))
    return results


def write_target_csv(result: dict, output) -> None:
    """写出当前快照，供 CLI 文件导出和 Web 即时下载共用。"""
    profiles = {p["uid"]: p for p in result.get("profiles", [])}
    extra = ["匿名hash", "匹配方式", "筛选条件", "命中条数", "解析方式", "置信度", "候选UID", "状态", "命中样本", "目标评论"]
    writer = csv.DictWriter(output, fieldnames=list(_flatten_profile({})) + extra)
    writer.writeheader()
    for sender in result.get("senders", []):
        profile = profiles.get(sender.get("uid"), {})
        row = _flatten_profile(profile)
        row.update({"UID": sender.get("uid") or "", "昵称": sender.get("name", ""),
                    "弹幕数": sender["danmaku_count"], "匿名hash": sender["mid_hash"],
                    "匹配方式": result["selector"]["mode"], "筛选条件": result["selector"]["pattern"],
                    "命中条数": sender["matched_count"], "解析方式": sender.get("method", ""),
                    "置信度": sender.get("confidence", "无"),
                    "候选UID": "、".join(str(v) for v in sender.get("candidates", [])),
                    "状态": sender["status"], "命中样本": " / ".join(sender.get("samples", [])),
                    "目标评论": " / ".join(c.get("content", "") for c in result.get("comments", [])
                                        if c["uid"] == sender.get("uid"))})
        writer.writerow({k: _sanitize_csv_cell(v) for k, v in row.items()})


def export_target(result: dict, prefix: str) -> list[str]:
    """JSON 保留全量命中及解析证据；CSV 包括未解析/未入选发送者。"""
    with open(prefix + ".csv", "w", newline="", encoding="utf-8-sig") as f:
        write_target_csv(result, f)
    with open(prefix + ".json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    return [os.path.basename(prefix + ext) for ext in (".csv", ".json")]


def run_target_analysis(bvid: str, selector: dict, force: bool = False, max_users: int | None = None,
                        launch_web: bool = True, skip_collect: bool = False,
                        port: int | None = None, skip_deep: bool = False,
                        target_history: bool = False, target_pool: bool = False) -> str:
    # 延迟导入主流程，保持扁平模块约定并避免循环导入。
    import main as flow
    if not re.fullmatch(r"BV[A-Za-z0-9]+", bvid):
        raise ValueError("BV号格式不正确")
    selector = make_selector(selector["pattern"], selector["mode"])
    if max_users is not None and max_users < 1:
        raise ValueError("--max-users 必须大于零")
    if port is not None:
        os.environ["PROFILER_PORT"] = str(port)
    cap = max_users if max_users is not None else getattr(config, "TARGET_MAX_USERS", config.MAX_ANALYZE_USERS_HARD_CAP)
    target_id = analysis_id(bvid, selector)
    init_db()
    old = load_target_analysis(target_id)
    previous = {r["mid_hash"]: r for r in (old or {}).get("result", {}).get("senders", [])}
    print(f"[Target] 目标弹幕分析：{'正则搜索' if selector['mode'] == 'regex' else '原文完整匹配'} {selector['pattern']!r}")
    timer = flow.PhaseTimer()
    print("[Target 1/4] 登录主号并定位目标弹幕...")
    client = timer.run("主号登录", flow.get_auth_client)
    # 少量目标使用主号的限速客户端，避免命中前校验全部小号、拉订阅及启动核心。
    pool = client
    video, danmaku, groups, command_dms = timer.run("目标弹幕取数", collect_target_danmaku,
                                                 bvid, client, include_history=target_history)
    matches = timer.run("目标匹配", match_danmaku, danmaku, selector)
    counts = Counter(dm["mid_hash"] for dm in matches)
    order = sorted(counts, key=lambda h: (-counts[h], -groups[h]["count"], h))
    selected = {h: groups[h] for h in order[:cap]}
    samples = defaultdict(list)
    for dm in matches:
        h = dm["mid_hash"]
        if len(samples[h]) < 3 and dm["content"] not in samples[h]:
            samples[h].append(dm["content"])
    # 覆盖范围与实际采集检查点一同冻结；不把当前弹幕池称为视频全部历史弹幕。
    history = []
    for page, _ in flow._history_page_targets(video):
        history.append({"page": page, **{key: get_phase_state(bvid, "danmaku", key if page == 1 else f"{key}:p{page}")
                       for key in ("done", "truncated", "failed_dates", "failed_months")}})
    result = {"id": target_id, "bvid": bvid, "selector": selector, "video_info": video,
              "status": "running", "matches": matches, "profiles": [], "senders": [],
              "comments": [c for c in (old or {}).get("result", {}).get("comments", [])
                           if calc_crc32(c["uid"]) in selected],
              "coverage": {"history_enabled": target_history and flow.HISTORY_DANMAKU_ENABLED,
                           "source_mode": "history" if target_history else "realtime_cache", "history": history,
                           "failed_realtime_pages": video.get("realtime_failed_pages", [])},
              "stats": {"pool_count": len(danmaku), "matched_count": len(matches),
                        "sender_count": len(order), "selected_count": len(selected),
                        "omitted_count": len(order) - len(selected), "profile_count": 0}}
    print(f"[Target] 命中 {len(matches)} 条、{len(order)} 个发送者，入选 {len(selected)} 个（上限 {cap}）")

    def checkpoint(resolved=None, profiles=None):
        resolved = resolved or {}
        profiles = profiles or []
        profiled = {p["uid"] for p in profiles}
        rows = []
        for h in order:
            info = resolved.get(h, {})
            uid, confidence = info.get("uid"), info.get("confidence", "无")
            status = ("未入选（人数上限）" if h not in selected else "待解析" if not info else
                      "未解析" if not uid else "低置信度，待核验" if confidence not in ("高", "中") else
                      "已生成画像" if uid in profiled else "未生成画像（采集跳过或失败）")
            name = (next((p.get("name", "") for p in profiles if p["uid"] == uid), "")
                    or info.get("user_info", {}).get("name", "")) if uid else ""
            rows.append({"mid_hash": h, "uid": uid, "confidence": confidence,
                         "method": info.get("method", ""), "resolution": info.get("resolution", {}),
                         "candidates": info.get("candidates", []), "collision_risk": info.get("collision_risk", True),
                         "danmaku_count": groups[h]["count"], "matched_count": counts[h],
                         "samples": samples[h], "selected": h in selected, "status": status, "name": name})
        result["senders"], result["profiles"] = rows, profiles
        result["stats"].update(profile_count=len(profiles),
                               unresolved_count=sum(r["selected"] and not r["uid"] for r in rows),
                               low_confidence_count=sum(r["selected"] and r["uid"] is not None and r["confidence"] not in ("高", "中") for r in rows))
        save_target_analysis(target_id, bvid, selector, result)

    checkpoint()
    resolved, profiles = {}, []
    try:
        if selected:
            if target_pool:
                pool = timer.run("可选账号与代理池", flow.build_pool, client)
            print(f"[Target 2/4] 只解析 {len(selected)} 个命中发送者，先用缓存、元信息与 CRC32 候选...")
            comment_map = {}
            for uid in load_comment_uids(bvid):
                h = calc_crc32(uid)
                if h in selected:
                    add_uid(comment_map, h, uid)
            for comment in result["comments"]:
                add_uid(comment_map, calc_crc32(comment["uid"]), comment["uid"])
            sources = [(METHOD_COMMENT_VERIFY, comment_map),
                       ("互动弹幕", flow.build_command_uid_map(command_dms)),
                       ("视频信息", flow.build_video_meta_uid_map(video))]
            resolved = timer.run("目标发送者UID", resolve_targets, bvid, selected, sources, pool, previous, force)
            checkpoint(resolved)

            def pending_hashes():
                return {h for h in selected if resolved.get(h, {}).get("confidence") not in ("高", "中")
                        and resolved.get(h, {}).get("method") != METHOD_AMBIGUOUS}

            def add_sources(extra_sources):
                # 仅有新明文证据的 hash 复核；其余不重复验证数学候选。
                affected = set()
                for source, mapping in extra_sources:
                    narrowed = {h: value for h, value in mapping.items() if h in selected}
                    sources.append((source, narrowed))
                    affected.update(narrowed)
                if affected:
                    refreshed = resolve_targets(bvid, {h: selected[h] for h in affected}, sources, pool, resolved)
                    resolved.update(refreshed)

            # 互动弹幕/充电名单是少量元信息请求，也只在初步结论不够时补充。
            pending = pending_hashes()
            if pending:
                command_map = flow.build_command_uid_map(command_dms or flow.fetch_command_dms(video, pool))
                up_mid = (video.get("owner") or {}).get("mid", 0)
                charge_map = flow.fetch_charge_uid_map(bvid, video.get("aid", 0), up_mid, pool) if up_mid else {}
                add_sources([("互动弹幕", command_map), ("充电名单", charge_map)])
            def merge_comments(comments):
                existing = {(c["uid"], str(c["rpid"])): c for c in result["comments"]}
                for comment in comments:
                    existing[(comment["uid"], str(comment["rpid"]))] = comment
                result["comments"] = list(existing.values())

            known_uids = {info["uid"] for info in resolved.values()
                          if info.get("uid") and info.get("confidence") in ("高", "中")}
            merge_comments(load_comments(bvid, uids=list(known_uids)))
            comments_by_hash = defaultdict(set)
            for comment in result["comments"]:
                comments_by_hash[calc_crc32(comment["uid"])].add(comment["uid"])
            # 已确认 UID 但没有评论原文时也搜索，不能因身份缓存命中而漏掉用户要看的评论。
            pending = pending_hashes() | {h for h in selected
                      if resolved.get(h, {}).get("uid") not in comments_by_hash[h]}
            lookup = {"requests": 0, "budget": None, "target_count": len(pending),
                      "matched_count": 0, "reason": "not_needed", "reply_requests": 0}

            def probe_progress(comments, stats):
                merge_comments(comments)
                result["uid_lookup"] = stats
                checkpoint(resolved, profiles)

            if pending:
                try:
                    extra, probe_comments, lookup = timer.run("目标评论搜索", probe_comment_uids, video, pool, pending,
                                                              on_progress=probe_progress)
                    merge_comments(probe_comments)
                    add_sources([(METHOD_COMMENT_VERIFY, extra)])
                except Exception as e:
                    print(f"[Target] 评论 UID 探测失败，保留已有解析：{e}")
                    lookup = result.get("uid_lookup", lookup)
                    lookup["reason"] = "api_error"
            result["uid_lookup"] = lookup
            checkpoint(resolved)
            timer.run("候选报告资料", prepare_candidate_reports, resolved, pool,
                      on_progress=lambda *_: checkpoint(resolved, profiles), cache_only=skip_collect)
            checkpoint(resolved, profiles)
            known_uids = {info["uid"] for info in resolved.values()
                          if info.get("uid") and info.get("confidence") in ("高", "中")}
            comments = [c for c in result["comments"] if c["uid"] in known_uids]
            locations = flow.build_comment_location_map(comments)
            print(f"[Target 3/4] 只采集 {len(known_uids)} 个已关联发送者的资料和行为...")
            users = timer.run("用户采集", flow.phase_collect_users, resolved, pool, force=force, cache_only=skip_collect)
            uid_comments = defaultdict(list)
            for comment in sorted(comments, key=lambda c: c.get("like", 0) or 0, reverse=True):
                uid_comments[comment["uid"]].append(comment)
            # 相对离群规则的 P95 必须来自全池，不能只用命中队列重算阈值。
            spam = flow.batch_detect_spam(groups)
            profiles = timer.run("画像分析", flow.phase_analyze, resolved, spam, users, selected,
                                 locations, uid_comments, video, persist=False)
            by_uid = {p["uid"]: p for p in profiles}
            for h, info in resolved.items():
                profile = by_uid.get(info.get("uid"))
                if profile:
                    profile.update(resolve_method=info.get("method", ""), resolve_confidence=info.get("confidence", ""),
                                   target_mid_hash=h, target_count=counts[h])
            checkpoint(resolved, profiles)
            if profiles and flow._deep_enabled(skip_deep):
                timer.run("AI深掘", flow.phase_ai_analysis, video, profiles, persist=False)
        result["status"] = "completed"
        checkpoint(resolved, profiles)
    except BaseException:
        result["status"] = "interrupted"
        checkpoint(resolved, profiles)
        raise
    os.makedirs(config.REPORT_DIR, exist_ok=True)
    prefix = os.path.join(config.REPORT_DIR, f"target_{bvid}_{target_id}_{datetime.now():%Y%m%d_%H%M%S_%f}")
    try:
        result["exports"] = export_target(result, prefix)
    except OSError as e:
        print(f"[Target] 导出失败，数据库报告已保存：{e}")
    save_target_analysis(target_id, bvid, selector, result)
    print("[Target 4/4] 独立目标报告已生成")
    timer.summary()
    print(f"[Target] 已保存目标报告：/target/{target_id}")
    if launch_web:
        flow.maybe_launch_web(bvid, target_id=target_id)
    return target_id
