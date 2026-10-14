"""CRC32 候选公开名片：轻量限速读取，唯一有效者按实用分级标高。"""
from contextlib import closing
import time

import config
from candidate_space import candidate_uids, fetch_space_sample, sample_is_fresh
from crc_candidates import is_pure_crc
from storage import (load_uid_candidates, load_uid_verify_cache, load_uid_verify_verdicts,
                     save_uid_verify_cache, refresh_sender_resolution, get_db,
                     load_uid_space_cache, load_uid_verify_times)
from uid_resolver import (_verify_single, _finalize, resolution_record,
                          UID_VERIFY_EXISTS_TTL, UID_VERIFY_MISSING_TTL, crack_crc32,
                          CANDIDATE_VERSION, VERIFY_NOT_ELIGIBLE, candidate_eligibility)


def read_candidate_card(uid: int, client=None, require_level: bool = False) -> tuple[str, dict, bool]:
    """一个名片接口即够；完整存在名片/明确账号不存在复用缓存，不采投稿或动态。"""
    if client is None:
        return "unknown", {}, False
    try:
        attempts = max(1, int(getattr(config, "UID_LEVEL_RETRY_ATTEMPTS", 2)))
    except (TypeError, ValueError):
        attempts = 2
    attempts = attempts if require_level else 1
    last_status, last_info, last_risk = "unknown", {}, False
    for attempt in range(attempts):
        cached = load_uid_verify_cache([uid], UID_VERIFY_EXISTS_TTL, UID_VERIFY_MISSING_TTL).get(uid)
        if cached and (cached[0] == "not_exists" or ("sign" in cached[1] and "face" in cached[1]
                                                  and (not require_level or "level" in cached[1]))):
            return *cached, False
        status, info, risk = _verify_single(uid, client)
        last_status, last_info, last_risk = status, info, risk
        # 只有账号存在但等级字段缺失才补一次；风控、网络异常和 404 不重复请求。
        if status != "exists" or not require_level or "level" in info or risk or attempt + 1 >= attempts:
            if status != "unknown":
                save_uid_verify_cache(uid, status, info)
            return status, info, risk
    return last_status, last_info, last_risk


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
        if status == "exists" and ("sign" not in info or "face" not in info or "level" not in info) and not stopped and not cache_only:
            current, details, risk = read_candidate_card(uid, client, require_level=True)
            if current != "unknown":
                status, info = current, details
            else:
                note = "命令行名片读取失败，保留已有存在性结论"
            if risk:
                stopped = True
        elif status == "exists" and stopped:
            note = "命令行触发风控，未完成的资料已标注"
        if candidate_eligibility(status, info) == VERIFY_NOT_ELIGIBLE:
            # 只有已确认存在的账号才检查等级；404 候选不会触发额外名片读取。
            # Lv0 候选作为内部排除状态保存，报告层不展示其名片或 UID。
            status = VERIFY_NOT_ELIGIBLE
            cards[str(uid)] = {"uid": uid, "status": status,
                               "note": "当前 Lv0，不具备普通视频弹幕权限"}
        else:
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
            if status in ("not_exists", VERIFY_NOT_ELIGIBLE):
                record.get("space_samples", {}).pop(str(uid), None)
            elif uid in samples:
                record.setdefault("space_samples", {})[str(uid)] = samples[uid]
            record["report_prepared_at"] = time.time()
            if on_progress:
                on_progress(h, sender)
        print(f"[候选报告] {i}/{len(uids)} · UID:{uid} · "
              f"{'已排除' if status in ('not_exists', VERIFY_NOT_ELIGIBLE) else '账号存在' if status == 'exists' else '核验未知'}")
    # 名片补齐期间可能取得新的 -404，必须在进入画像流程之前统一收敛。
    times = load_uid_verify_times(uids)

    def card_status(u, pool=cards):
        item = pool.get(str(u))
        if isinstance(item, dict) and item.get("status"):
            return item["status"]
        return "unknown"

    def card_detail(u, pool=cards):
        item = pool.get(str(u))
        return item if isinstance(item, dict) else {}

    for h, sender in senders.items():
        record = sender["resolution"]
        stored = candidate_uids(sender)
        limit = record.get("max_search", config.MITM_MAX_UID)
        if type(limit) is not int or not 0 < limit <= config.MITM_MAX_UID:
            limit = config.MITM_MAX_UID
        # 没有 candidate_complete、且已存列表与数学全集不一致时才并集，避免残缺列表被标高。
        cracked = [] if "candidate_complete" in record else (crack_crc32(sender.get("mid_hash") or h, limit) or [])
        values = stored
        if cracked and set(stored) != set(cracked):
            values = sorted(set(stored) | set(cracked))
            record["candidates"] = values
        # 只有最终候选确实等于 crack_crc32 的结果，才沿用 resolution_record 的算法版本。
        if cracked and set(values) == set(cracked):
            record["candidate_version"] = CANDIDATE_VERSION
        verdict = _finalize(h, sender.get("danmaku_count", sender.get("count", 0)), None, values, None,
                            card_status, card_detail)
        uid, confidence, method, info, risk, _ = verdict
        statuses = {str(u): card_status(u) for u in values}
        record.update(candidate_status=statuses,
                      candidate_checked_at={str(u): times.get(str(u), record.get("checked_at", 0)) for u in values},
                      assigned=uid is not None, collision_risk=risk, reason=resolution_record(h, verdict, {})["reason"])
        if (uid is None and all(status in ("not_exists", VERIFY_NOT_ELIGIBLE) for status in statuses.values())
                and any(status == VERIFY_NOT_ELIGIBLE for status in statuses.values())):
            record["reason"] = "搜索范围内候选均不存在或不具备普通视频弹幕权限"
        sender.update(uid=uid, confidence=confidence, method=method, user_info=info or {}, collision_risk=risk)
        if on_progress:
            on_progress(h, sender)
    if stopped:
        print("[候选报告] 已触发风控，剩余项保留缺失标记；报告页面不会后台补采")



def recheck_ambiguous_candidates(resolved: dict, client, on_progress=None) -> dict:
    """阶段4候选终审复核：收敛歧义候选的存在性，保证报告里不出现点进去 404 的账号。

    只处理"纯 CRC 破解、候选 ≥2、无明文证据可交叉验证"的组：
      1) 复核组内"曾判定不存在"或"尚未定论"的候选；
      2) 一轮里若出现"上一份结论是 not_exists、本轮却是 exists"的账号，说明此前那次 404 不可信
         （疑似风控伪装），按 UID_RECHECK_INTERVAL 等待后连同"本轮仍未知"的继续复核；
         从未判过 404 的 unknown→exists 是正常确认，写入 exists 后不再入队；
      3) 一轮内没有任何调整 ⇒ 收敛；仍为明确 -404 的候选累计确认达
         UID_RECHECK_CONFIRMATIONS 次后写 final（彻底判定不存在）。

    必须有终止条件（否则风控持续时会无限打接口）：单轮预算 + 最大轮数 + 风控熔断；
    未收敛的写 resolution["recheck_pending"]（含 next_check_at），冷却后下次运行继续。
    返回统计字典：{targets, checked, flipped, finalized, pending, stopped}。"""
    stats = {"targets": 0, "checked": 0, "flipped": 0, "finalized": 0, "pending": 0, "stopped": False}
    if not getattr(config, "UID_RECHECK_ENABLED", True):
        return stats
    rounds = max(1, int(getattr(config, "UID_RECHECK_MAX_ROUNDS", 3)))
    interval = max(0, int(getattr(config, "UID_RECHECK_INTERVAL", 90)))
    budget = max(1, int(getattr(config, "UID_RECHECK_BUDGET", 50)))
    cooldown = max(60, int(getattr(config, "UID_RECHECK_COOLDOWN", 1800)))
    need = max(1, int(getattr(config, "UID_RECHECK_CONFIRMATIONS", 2)))
    final_ttl = max(3600, int(getattr(config, "UID_RECHECK_FINAL_TTL", 30 * 86400)))

    now = time.time()
    senders = {h: info for h, info in resolved.items()
               if is_pure_crc(info) and len(candidate_uids(info)) > 1}
    if not senders:
        return stats
    targets: dict = {}
    for h, info in senders.items():
        record = info.get("resolution") or {}
        recorded = record.get("candidate_status") or {}
        recorded = recorded if isinstance(recorded, dict) else {}
        waiting = record.get("recheck_pending") or {}
        waiting = waiting if isinstance(waiting, dict) else {}
        for uid in candidate_uids(info):
            status = recorded.get(str(uid), "unknown")
            if status in ("exists", VERIFY_NOT_ELIGIBLE):
                continue            # 已确认存在：可点击名片有据，无需复核
            entry = waiting.get(str(uid))
            if isinstance(entry, dict) and float(entry.get("next_check_at") or 0) > now:
                continue            # 冷却中：上次已复核过，别每轮都打
            targets.setdefault(uid, []).append(h)
    stats["targets"] = len(targets)
    if not targets:
        return stats

    verdicts = load_uid_verify_verdicts(sorted(targets), UID_VERIFY_EXISTS_TTL, UID_VERIFY_MISSING_TTL)
    confirmed, queue, previous = {}, [], {}
    for uid in sorted(targets):
        verdict = verdicts.get(uid)
        if verdict and verdict["status"] == "not_exists" and verdict.get("final") \
                and now - float(verdict["verified_at"] or 0) < final_ttl:
            continue                # 已终审排除且未到复核周期
        queue.append(uid)
        cached = (verdict or {}).get("info") or {}
        confirmed[uid] = int(cached.get("miss_count") or 0)
        # 调整只和上一份核验结论比；初始结论来自全量裁决，不看 resolution 里的旧状态。
        previous[uid] = (verdict or {}).get("status") or "unknown"
    if not queue:
        return stats

    results: dict = {}
    card_info: dict = {}
    finalized: set = set()
    stopped = False
    for round_no in range(1, rounds + 1):
        batch, queue = queue[:budget], queue[budget:]
        flips, unknowns, unstable = [], [], []
        for index, uid in enumerate(batch):
            status, card, risk = _verify_single(uid, client)
            stats["checked"] += 1
            if risk:
                stopped = True      # 风控：当前 UID 和本批还没查的都留到下次
                queue = batch[index:] + queue
                break
            if status == "exists":
                # 只有上一份是 not_exists（曾 404）才算调整；unknown→exists 是正常确认。
                if previous.get(uid) == "not_exists":
                    flips.append(uid)
                    confirmed[uid] = 0
                save_uid_verify_cache(uid, "exists", card, respect_final=False)
                previous[uid] = "exists"
                if isinstance(card, dict) and card:
                    card_info[uid] = card
            elif status == "not_exists":
                confirmed[uid] = confirmed.get(uid, 0) + 1
                final = confirmed[uid] >= need
                save_uid_verify_cache(uid, "not_exists",
                                      {"miss_count": confirmed[uid], "final": final},
                                      respect_final=False)
                previous[uid] = "not_exists"
                if final:
                    if uid not in finalized:
                        finalized.add(uid)
                        stats["finalized"] += 1
                else:
                    unstable.append(uid)   # 还差确认次数，下一轮再确认一次
            else:
                unknowns.append(uid)       # 未知不改写上一份确定性结论
            results[uid] = candidate_eligibility(status, card)
        if flips:
            stats["flipped"] += len(flips)
        # 下一轮：调整的（结论已被推翻，必须重查）+ 没查成的 + 还差确认次数的
        retry = flips + unknowns + unstable + queue
        queue = retry
        if stopped or not retry:
            break
        if round_no < rounds:
            time.sleep(interval)

    stats["pending"] = len(queue)
    stats["stopped"] = stopped
    deadline = now + cooldown
    for h, info in senders.items():
        uids = candidate_uids(info)
        touched = [u for u in uids if u in results]
        waiting_now = [u for u in uids if u in queue]
        if not touched and not waiting_now:
            continue
        record = info.setdefault("resolution", {})
        if touched:
            # 循环结束后只按最终存在性回写一次，发生调整的候选在循环内已经重查过。
            recorded = record.get("candidate_status") if isinstance(record.get("candidate_status"), dict) else {}
            merged = {str(u): recorded.get(str(u), "unknown") for u in uids}
            for uid in touched:
                merged[str(uid)] = results[uid]
            old_uid = info.get("uid")

            def status_of(u, mapping=merged):
                return mapping.get(str(u), "unknown")

            def info_of(u, saved=card_info, rec=record, sender=info):
                got = saved.get(u)
                if isinstance(got, dict) and got:
                    return got
                raw = verdicts.get(u)
                if (isinstance(raw, dict) and raw.get("status") == "exists"
                        and isinstance(raw.get("info"), dict) and raw["info"]):
                    return raw["info"]
                frozen = (rec.get("candidate_cards") or {}).get(str(u))
                if isinstance(frozen, dict) and frozen:
                    return frozen
                if sender.get("uid") == u and isinstance(sender.get("user_info"), dict):
                    return sender["user_info"]
                return {}

            uid, confidence, method, user_info, risk, _cands = _finalize(
                h, info.get("danmaku_count", info.get("count", 0)), None, uids, None, status_of, info_of)
            info.update(uid=uid, confidence=confidence, method=method,
                        user_info=user_info or {}, collision_risk=risk)
            verdict = (uid, confidence, method, user_info, risk, uids)
            record.update(assigned=uid is not None, collision_risk=risk,
                          reason=resolution_record(h, verdict, {})["reason"])
            if old_uid and old_uid != uid:
                record["previous_uid"] = old_uid
        status_map = record.setdefault("candidate_status", {})
        times_map = record.setdefault("candidate_checked_at", {})
        for uid in touched:
            status_map[str(uid)] = results[uid]
            checked_at = time.time()
            times_map[str(uid)] = checked_at
            # 名片快照与存在性裁决必须一起更新。否则新鲜缓存已经 exists，
            # 旧 candidate_cards 却会凭旧状态覆盖报告展示。
            cards = record.setdefault("candidate_cards", {})
            old_card = cards.get(str(uid)) if isinstance(cards.get(str(uid)), dict) else {}
            fresh_card = card_info.get(uid) if isinstance(card_info.get(uid), dict) else {}
            card = {**old_card, **fresh_card, "uid": uid, "status": results[uid],
                    "checked_at": checked_at,
                    "note": "终审复核已确认存在" if results[uid] == "exists"
                    else "终审复核明确不存在" if results[uid] == "not_exists"
                    else "终审复核结果未知"}
            if results[uid] in ("not_exists", VERIFY_NOT_ELIGIBLE):
                card = {"uid": uid, "status": results[uid], "checked_at": checked_at}
                record.get("space_samples", {}).pop(str(uid), None)
            cards[str(uid)] = card
        pending_map = record.setdefault("recheck_pending", {})
        for uid in waiting_now:
            pending_map[str(uid)] = {"next_check_at": deadline, "reason": "存在性复核未收敛"}
        for uid in uids:
            if str(uid) not in pending_map:
                continue
            if uid not in waiting_now:
                pending_map.pop(str(uid), None)   # 本轮已收敛，清掉旧的冷却标记
        if on_progress:
            on_progress(h, info)
    return stats

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
        # 不把旧状态重新盖上当前时间，否则会无限延长账号不存在有效期。
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
            status = candidate_eligibility(*verified.get(uid, ("unknown", {})))
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
