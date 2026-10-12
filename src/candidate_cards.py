"""CRC32 候选公开名片：轻量限速读取，唯一有效者按实用分级标高。"""
from contextlib import closing
import time

import config
from candidate_space import candidate_uids, fetch_space_sample, sample_is_fresh
from crc_candidates import is_pure_crc
from storage import (load_uid_candidates, load_uid_verify_cache, save_uid_verify_cache,
                     refresh_sender_resolution, get_db, load_uid_space_cache, load_uid_verify_times)
from uid_resolver import (_verify_single, _finalize, resolution_record,
                          UID_VERIFY_EXISTS_TTL, UID_VERIFY_MISSING_TTL, crack_crc32)


def read_candidate_card(uid: int, client=None) -> tuple[str, dict, bool]:
    """一个名片接口即够；完整阳性名片/明确阴性复用缓存，不采投稿或动态。"""
    cached = load_uid_verify_cache([uid], UID_VERIFY_EXISTS_TTL, UID_VERIFY_MISSING_TTL).get(uid)
    if cached and (cached[0] == "not_exists" or ("sign" in cached[1] and "face" in cached[1])):
        return *cached, False
    if client is None:
        return "unknown", {}, False
    status, info, risk = _verify_single(uid, client)
    save_uid_verify_cache(uid, status, info)
    return status, info, risk


def cache_space_card(sample: dict) -> None:
    """辅助空间样本的名片结论回写存在性缓存；时间沿用原核验时刻。"""
    card = sample.get("sources", {}).get("card", {})
    status = card.get("status")
    if status == "not_exists":
        save_uid_verify_cache(sample["uid"], "not_exists", {}, sample.get("card_checked_at", sample["checked_at"]))
    elif status == "ok" and card.get("items"):
        item = card["items"][0]
        info = {"uid": sample["uid"], "name": item.get("name", ""), "sign": item.get("text", "")}
        if "face" in item:
            info["face"] = item["face"]
        # 旧空间样本没有头像，保留已缓存的头像等名片字段。
        previous = load_uid_verify_cache([sample["uid"]], UID_VERIFY_EXISTS_TTL,
                                         UID_VERIFY_MISSING_TTL).get(sample["uid"])
        if previous and previous[0] == "exists":
            info = {**previous[1], **info}
        save_uid_verify_cache(sample["uid"], "exists", info, sample.get("card_checked_at", sample["checked_at"]))


def prepare_candidate_reports(resolved: dict, client, on_progress=None, cache_only: bool = False) -> None:
    """命令行完成候选名片与空间取数并冻结快照；失败状态也随报告保存。"""
    senders = {h: info for h, info in resolved.items() if is_pure_crc(info)
               and (info.get("candidates") or info.get("resolution", {}).get("candidates"))}
    for h, info in senders.items():
        info.setdefault("mid_hash", h)
    uids = sorted({u for info in senders.values() for u in candidate_uids(info)})
    if not uids:
        return
    print(f"[候选报告] 命令行准备 {len(senders)} 个发送者、{len(uids)} 个候选的名片与空间资料...")
    verified = load_uid_verify_cache(uids, UID_VERIFY_EXISTS_TTL, UID_VERIFY_MISSING_TTL)
    samples = load_uid_space_cache(uids)
    stopped = any(info.get("verification_stopped") for info in senders.values())
    cards = {}
    for i, uid in enumerate(uids, 1):
        status, info = verified.get(uid, ("unknown", {}))
        note = "命令行已核验" if status != "unknown" else "命令行存在性核验未完成，保留未知"
        if status == "exists" and ("sign" not in info or "face" not in info) and not stopped and not cache_only:
            current, details, risk = read_candidate_card(uid, client)
            if current != "unknown":
                status, info = current, details
            else:
                note = "命令行名片读取失败，保留已有存在性结论"
            if risk:
                stopped = True
        elif status == "exists" and stopped:
            note = "命令行触发风控，未完成的资料已标注"
        cards[str(uid)] = {"uid": uid, "status": status, **info, "note": note}
        if cache_only and status == "exists" and ("sign" not in info or "face" not in info):
            cards[str(uid)]["note"] = "本轮只读已有缓存，未保存的名片资料请在命令行补采"
        if status == "exists" and not stopped and not cache_only:
            sample = samples.get(uid)
            if not sample or not sample_is_fresh(sample):
                at = load_uid_verify_times([uid]).get(str(uid))
                sample = fetch_space_sample(uid, client, card_info=info, card_checked_at=at)
                samples[uid] = sample
            if any(s.get("status") == "risk" for s in sample.get("sources", {}).values()):
                stopped = True
        for h, sender in senders.items():
            if uid not in candidate_uids(sender):
                continue
            record = sender.setdefault("resolution", {})
            record.setdefault("candidate_cards", {})[str(uid)] = cards[str(uid)]
            if uid in samples:
                record.setdefault("space_samples", {})[str(uid)] = samples[uid]
            record["report_prepared_at"] = time.time()
            if on_progress:
                on_progress(h, sender)
        print(f"[候选报告] {i}/{len(uids)} · UID:{uid} · "
              f"{'明确不存在' if status == 'not_exists' else '账号存在' if status == 'exists' else '核验未知'}")
    # 名片补齐期间可能取得新的 -404，必须在进入画像流程之前统一收敛。
    times = load_uid_verify_times(uids)
    for h, sender in senders.items():
        values = candidate_uids(sender)
        verdict = _finalize(h, sender.get("danmaku_count", sender.get("count", 0)), None, values, None,
                            lambda u: cards[str(u)]["status"], lambda u: cards[str(u)])
        uid, confidence, method, info, risk, _ = verdict
        record = sender["resolution"]
        record.update(candidate_status={str(u): cards[str(u)]["status"] for u in values},
                      candidate_checked_at={str(u): times.get(str(u), record.get("checked_at", 0)) for u in values},
                      assigned=uid is not None, collision_risk=risk, reason=resolution_record(h, verdict, {})["reason"])
        sender.update(uid=uid, confidence=confidence, method=method, user_info=info or {}, collision_risk=risk)
        if on_progress:
            on_progress(h, sender)
    if stopped:
        print("[候选报告] 已触发风控，剩余项保留缺失标记；报告页面不会后台补采")


def reconcile_crc_sender(bvid: str, mid_hash: str, target_id: str | None = None) -> dict | None:
    """最新快照仍为纯破解时，唯一存在且其他全部明确缺失才分配 UID。"""
    def transform(sender):
        if not is_pure_crc(sender) or sender.get("selected") is False:
            return None
        old = sender.get("resolution") or {}
        saved = old.get("candidates") or sender.get("candidates")
        if not saved:
            saved = load_uid_candidates(mid_hash, config.MITM_MAX_UID, "mitm-v1")
        if not saved:
            return None  # 只有旧分配 UID、没有完整候选集，不能据此声称已消歧。
        limit = old.get("max_search", config.MITM_MAX_UID)
        if type(limit) is not int or not 0 < limit <= config.MITM_MAX_UID:
            return None
        complete = crack_crc32(mid_hash, limit)
        uids = sorted(set(complete) | set(candidate_uids({**sender, "candidates": saved})))
        verified = load_uid_verify_cache(uids, UID_VERIFY_EXISTS_TTL, UID_VERIFY_MISSING_TTL)
        statuses, checked, latest_times = {}, {}, {}
        # 不把旧状态重新盖上当前时间，否则会无限延长阴性有效期。
        with closing(get_db()) as conn:
            for start in range(0, len(uids), 500):
                chunk = uids[start:start + 500]
                for row in conn.execute(f"SELECT uid, verified_at FROM uid_verify_cache WHERE uid IN ({','.join('?' for _ in chunk)})", chunk):
                    latest_times[str(row["uid"])] = row["verified_at"]
                    if row["uid"] in verified:
                        checked[str(row["uid"])] = row["verified_at"]
        now = time.time()
        recorded = old.get("candidate_status") or {}
        times = old.get("candidate_checked_at") or {}
        recorded = recorded if isinstance(recorded, dict) else {}
        times = times if isinstance(times, dict) else {}
        for uid in uids:
            status = verified.get(uid, ("unknown", {}))[0]
            if status == "unknown":
                previous = recorded.get(str(uid), "unknown")
                at = times.get(str(uid), old.get("checked_at", 0))
                ttl = UID_VERIFY_EXISTS_TTL if previous == "exists" else UID_VERIFY_MISSING_TTL
                try:
                    if (previous in ("exists", "not_exists") and 0 <= now - float(at) < ttl
                            and latest_times.get(str(uid), -1) < float(at)):
                        status, checked[str(uid)] = previous, float(at)
                except (TypeError, ValueError):
                    pass
            statuses[str(uid)] = status
        verdict = _finalize(mid_hash, sender.get("danmaku_count", 0), None, uids, None,
                            lambda u: statuses[str(u)], lambda u: verified.get(u, ("unknown", {}))[1])
        uid, confidence, method, info, risk, _ = verdict
        reason = resolution_record(mid_hash, verdict, {})["reason"]
        updated = {"uid": uid, "confidence": confidence, "method": method, "candidates": uids,
                   "collision_risk": risk, "name": (info or {}).get("name", "") if uid else "",
                   "status": "已核验高置信度，重跑可生成画像" if uid else "未解析",
                   "resolution": {**old, "candidates": uids, "candidate_status": statuses,
                                  "max_search": limit, "candidate_version": "mitm-v1",
                                  "candidate_checked_at": checked, "existence_checked_at": now,
                                  "assigned": uid is not None, "reason": reason, "collision_risk": risk}}
        if sender.get("uid") and sender["uid"] != uid:
            updated["resolution"]["previous_uid"] = sender["uid"]
        return updated
    return refresh_sender_resolution(bvid, mid_hash, transform, target_id)
