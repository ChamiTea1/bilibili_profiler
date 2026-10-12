"""UID 候选解析：CRC32 仅提供关联，明文来源、存在性与身份归属分别判定。

同一 hash 的多名明文用户全部保留；多候选及未完成验证不自动选人。
长 UID 可通过明文正向匹配，但无法仅凭 32 位校验值唯一恢复。
"""
import hashlib
import json
import sqlite3
import time
from typing import Optional, Tuple

import config
from api_client import BiliAPIClient, RiskControlError
from config import MITM_MAX_UID, USER_CARD_URL, USER_CARDS_BATCH_URL
from crc_rainbow import lookup
from storage import (load_uid_candidates, save_uid_candidates, load_uid_verify_cache,
                     save_uid_verify_cache, load_uid_verify_times)
from uid_evidence import candidates_of, crc32_uid, add_uid, PLAINTEXT_SOURCES, LOCAL_PLAIN_REASON, CRC_UNIQUE_REASON

METHOD_COMMENT_VERIFY = "评论区验证"
METHOD_CRC32_CRACK = "CRC32破解"
METHOD_CRC32_COLLISION = "CRC32碰撞"
METHOD_UNKNOWN = "未知"
METHOD_AMBIGUOUS = "候选歧义"
METHOD_PENDING = "待验证"
VERIFY_EXISTS = "exists"
VERIFY_NOT_EXISTS = "not_exists"
VERIFY_UNKNOWN = "unknown"
RESOLVER_VERSION = "evidence-v4"
CANDIDATE_VERSION = "mitm-v1"
UID_VERIFY_EXISTS_TTL = getattr(config, "UID_VERIFY_EXISTS_TTL", 7 * 86400)
UID_VERIFY_MISSING_TTL = getattr(config, "UID_VERIFY_MISSING_TTL", 3600)
UID_VERIFY_FALLBACK_BUDGET = getattr(config, "UID_VERIFY_FALLBACK_BUDGET", 50)
UID_RESOLUTION_CACHE_TTL = getattr(config, "UID_RESOLUTION_CACHE_TTL", 86400)


class VerificationBudget:
    """同一轮逐人解析共享补验预算与风控停采信号。"""
    def __init__(self):
        self.remaining = max(0, UID_VERIFY_FALLBACK_BUDGET)
        self.stopped = False


def calc_crc32(uid: int) -> str:
    return crc32_uid(uid)


def crack_crc32(crc32_hash: str, max_search: int = MITM_MAX_UID) -> list[int]:
    """候选按数学算法版本和搜索上限缓存，每个 UID 必须正向校验相符。"""
    try:
        h = f"{int(crc32_hash, 16):08x}"
    except (TypeError, ValueError):
        return []
    if len(h) != 8:
        return []
    max_search = max(0, min(max_search, MITM_MAX_UID))
    try:
        cached = load_uid_candidates(h, max_search, CANDIDATE_VERSION)
        if cached is not None:
            return cached
    except sqlite3.Error:
        pass  # 轻量入口尚未初始化数据库时仍可本地解析。
    result = [u for u in lookup(h, max_uid=max_search) if u > 0]
    try:
        save_uid_candidates(h, max_search, CANDIDATE_VERSION, result)
    except sqlite3.Error:
        pass
    return result


def build_evidence_maps(bvid: str, sources: list[tuple[str, dict]], global_map: dict) -> tuple[dict, dict]:
    """合并候选及来源上下文；同视频证据优先描述，但不丢弃跨视频冲突。"""
    mapping, methods = {}, {}
    for source, source_map in sources:
        for h, value in source_map.items():
            for uid in candidates_of(value):
                if crc32_uid(uid) != h:
                    continue
                add_uid(mapping, h, uid)
                methods.setdefault(h, {}).setdefault(uid, {"source": source, "local": True})
    for h, entry in global_map.items():
        # 兼容旧调用方，但旧的破解条目永远不能转换为明文证据。
        evidence = entry.get("evidence", [{"uid": entry.get("uid"), "source": entry.get("source"), "bvid": ""}])
        for ev in evidence:
            source = ev.get("source")
            for uid in candidates_of(ev.get("uid")):
                if source not in PLAINTEXT_SOURCES or crc32_uid(uid) != h:
                    continue
                add_uid(mapping, h, uid)
                item = {"source": source, "local": bool(bvid and ev.get("bvid") == bvid)}
                previous = methods.setdefault(h, {}).get(uid)
                if previous is None or (item["local"] and not previous["local"]):
                    methods[h][uid] = item
    return mapping, methods


def _evidence(mid_hash: str, mapping: dict, methods: dict | None) -> dict[int, dict]:
    result = {}
    method = (methods or {}).get(mid_hash, METHOD_COMMENT_VERIFY)
    for uid in candidates_of(mapping.get(mid_hash)):
        if crc32_uid(uid) != mid_hash:
            continue
        item = method.get(uid, {}) if isinstance(method, dict) else {"source": method, "local": True}
        if item.get("source") in PLAINTEXT_SOURCES:
            result[uid] = {"source": item["source"], "local": bool(item.get("local"))}
    return result


def evidence_fingerprint(mid_hash: str, mapping: dict, methods: dict | None = None,
                         max_search: int = MITM_MAX_UID) -> str:
    payload = [RESOLVER_VERSION, min(max_search, MITM_MAX_UID), sorted(_evidence(mid_hash, mapping, methods).items())]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def resolution_record(mid_hash: str, result: tuple, mapping: dict, methods: dict | None = None,
                      max_search: int = MITM_MAX_UID) -> dict:
    uid, confidence, method, _info, risk, candidates = result
    reasons = {METHOD_AMBIGUOUS: "同一 hash 存在多名有效候选，暂不归属",
               METHOD_PENDING: "仍有候选验证未知，不能视为已排除",
               METHOD_CRC32_CRACK: CRC_UNIQUE_REASON,
               METHOD_CRC32_COLLISION: "搜索范围内的候选均已确认不存在",
               METHOD_UNKNOWN: "搜索范围内无候选，也没有明文关联证据"}
    reason = reasons.get(method, LOCAL_PLAIN_REASON if confidence == "高"
                         else "仅跨视频明文候选，尚缺本视频关联证据")
    record = {"version": RESOLVER_VERSION, "fingerprint": evidence_fingerprint(mid_hash, mapping, methods, max_search),
            "checked_at": time.time(), "candidates": candidates, "collision_risk": risk,
            "max_search": min(max_search, MITM_MAX_UID), "candidate_version": CANDIDATE_VERSION,
            "reason": reason, "assigned": uid is not None,
            "evidence": [{"uid": u, **ev} for u, ev in sorted(_evidence(mid_hash, mapping, methods).items())]}
    if method == METHOD_CRC32_CRACK:
        cached = _cached_verifications(candidates)
        record["candidate_status"] = {str(u): cached.get(u, (VERIFY_UNKNOWN, {}))[0] for u in candidates}
        try:
            record["candidate_checked_at"] = load_uid_verify_times(candidates)
        except sqlite3.Error:
            record["candidate_checked_at"] = {}
    return record


def resolution_is_current(row: dict, mid_hash: str, mapping: dict, methods: dict | None = None) -> bool:
    record = row.get("resolution") or {}
    if row.get("method") in (METHOD_PENDING, METHOD_UNKNOWN):
        return False  # 未知没有有效结论，重跑继续验证。
    if row.get("method") == METHOD_CRC32_CRACK:
        uids = candidates_of(record.get("candidates"))
        cached = _cached_verifications(sorted(uids))
        # 高置信度依赖所有阴性结论；其中一条过期，即须重验，不能按一天缓存跳过。
        if (row.get("confidence") != "高" or not uids or set(cached) != uids
                or {u for u, (status, _) in cached.items() if status == VERIFY_EXISTS} != {row.get("uid")}):
            return False
    return (record.get("version") == RESOLVER_VERSION
            and record.get("fingerprint") == evidence_fingerprint(mid_hash, mapping, methods)
            and 0 <= time.time() - record.get("checked_at", 0) < UID_RESOLUTION_CACHE_TTL)


def _cached_verifications(uids: list[int]) -> dict:
    try:
        return load_uid_verify_cache(uids, UID_VERIFY_EXISTS_TTL, UID_VERIFY_MISSING_TTL)
    except sqlite3.Error:
        return {}


def _cache_verification(uid: int, status: str, info: dict):
    try:
        save_uid_verify_cache(uid, status, info)
    except sqlite3.Error:
        pass


def _card_info(uid: int, card: dict, batch: bool = False) -> dict:
    result = {"uid": uid, "name": card.get("name", ""), "face": card.get("face", ""),
              "vip_type": (card.get("vip") or {}).get("type", 0),
              "vip_status": (card.get("vip") or {}).get("status", 0)}
    official = card.get("official" if batch else "official_verify") or {}
    result["official_type"] = official.get("type", -1)
    if not batch:
        result.update(sign=card.get("sign", ""), level=(card.get("level_info") or {}).get("current_level", 0),
                      sex=card.get("sex", ""), follower=card.get("fans", 0), following=card.get("attention", 0))
    return result


def _verify_single(uid: int, client: BiliAPIClient) -> tuple[str, dict, bool]:
    """第三元为风控信号；只有明确 -404 才判不存在，空名片和脏返回均未知。"""
    try:
        data = client.get(USER_CARD_URL, params={"mid": uid})
        if not isinstance(data, dict):
            return VERIFY_UNKNOWN, {}, False
        code = data.get("code")
        if code == -404:
            return VERIFY_NOT_EXISTS, {}, False
        if code != 0:
            return VERIFY_UNKNOWN, {}, code in (-412, -352, -403)
        card = (data.get("data") or {}).get("card")
        if not isinstance(card, dict) or int(card.get("mid", 0)) != uid:
            return VERIFY_UNKNOWN, {}, False
        return VERIFY_EXISTS, _card_info(uid, card), False
    except RiskControlError:
        return VERIFY_UNKNOWN, {}, True
    except Exception:
        return VERIFY_UNKNOWN, {}, False


def verify_uid_exists(uid: int, client: BiliAPIClient) -> Tuple[str, dict]:
    cached = _cached_verifications([uid])
    if uid in cached:
        return cached[uid]
    status, info, _risk = _verify_single(uid, client)
    _cache_verification(uid, status, info)
    return status, info


def _batch_verify_uids(uids: list[int], client: BiliAPIClient,
                       budget: VerificationBudget | None = None) -> Tuple[dict, set]:
    """批量阳性可确认存在；缺席需单点 -404 确认，降级请求有整轮预算。

    预算耗尽或风控时保持未知，下轮使用已确认的缓存接续，绝不展开风控请求风暴。
    """
    uids = sorted(set(uids))
    cached = _cached_verifications(uids)
    found = {u: info for u, (status, info) in cached.items() if status == VERIFY_EXISTS}
    todo = [u for u in uids if u not in cached]
    unknown = set(todo)
    budget = budget if budget is not None else VerificationBudget()
    for start in range(0, len(todo), 50):
        if budget.stopped:
            break
        chunk = todo[start:start + 50]
        singles = list(chunk)
        try:
            data = client.get(USER_CARDS_BATCH_URL, params={"uids": ",".join(map(str, chunk))})
            if isinstance(data, dict) and data.get("code") in (-412, -352, -403):
                budget.stopped = True
                break
            if isinstance(data, dict) and data.get("code") == 0 and isinstance(data.get("data"), dict):
                entries = []
                for key, card in data["data"].items():
                    uid = int(key)
                    if uid not in chunk or not isinstance(card, dict) or not card:
                        raise ValueError("批量名片返回结构异常")
                    if "mid" in card and int(card["mid"]) != uid:
                        raise ValueError("批量名片 UID 不符")
                    entries.append((uid, _card_info(uid, card, batch=True)))
                for uid, info in entries:
                    found[uid] = info
                    unknown.discard(uid)
                    _cache_verification(uid, VERIFY_EXISTS, info)
                singles = [u for u in chunk if u not in found]
        except RiskControlError:
            budget.stopped = True
            break
        except Exception:
            pass  # 无效或非 JSON 返回只触发有预算的单点验证。
        stopped = False
        for uid in singles:
            if budget.remaining <= 0:
                break
            budget.remaining -= 1
            status, info, risk = _verify_single(uid, client)
            if status == VERIFY_EXISTS:
                found[uid] = info
            if status != VERIFY_UNKNOWN:
                unknown.discard(uid)
                _cache_verification(uid, status, info)
            if risk:
                budget.stopped = True
                stopped = True
                break
        if stopped:
            break
    return found, unknown


def _plain_result(evidence: dict[int, dict]):
    candidates = sorted(evidence)
    if len(candidates) > 1:
        return None, "无", METHOD_AMBIGUOUS, None, True, candidates
    uid = candidates[0]
    ev = evidence[uid]
    # 相同 hash 的弹幕数不是新的身份区分信息；关联置信度不随条数升级。
    return uid, ("高" if ev["local"] else "低"), ev["source"], None, True, candidates


def _finalize(mid_hash: str, danmaku_count: int, plain_uid: Optional[int],
              candidates: list, method_map: dict | None, status_of, info_of):
    """全部候选核验完成且唯一存在时按实用分级标高，未知及多名存在不选人。"""
    if plain_uid is not None:
        return _plain_result({plain_uid: {"source": (method_map or {}).get(mid_hash, METHOD_COMMENT_VERIFY), "local": True}})
    if not candidates:
        return None, "无", METHOD_UNKNOWN, None, False, []
    existing = [u for u in candidates if status_of(u) == VERIFY_EXISTS]
    unknown = [u for u in candidates if status_of(u) not in (VERIFY_EXISTS, VERIFY_NOT_EXISTS)]
    if len(existing) > 1:
        return None, "无", METHOD_AMBIGUOUS, None, True, candidates
    if unknown:
        return None, "无", METHOD_PENDING, None, True, candidates
    if len(existing) == 1:
        uid = existing[0]
        return uid, "高", METHOD_CRC32_CRACK, info_of(uid), True, candidates
    return None, "无", METHOD_CRC32_COLLISION, None, False, candidates


def _resolve_many(sender_groups: dict, mapping: dict, client, max_search: int, methods: dict | None,
                  budget: VerificationBudget | None = None) -> dict:
    budget = budget if budget is not None else VerificationBudget()
    plain, candidate_map, universe = {}, {}, set()
    for h in sender_groups:
        ev = _evidence(h, mapping, methods)
        if ev:
            plain[h] = _plain_result(ev)
        else:
            candidates = crack_crc32(h, max_search)
            candidate_map[h] = candidates
            universe.update(candidates)
    found, unknown = _batch_verify_uids(sorted(universe), client, budget) if universe else ({}, set())
    def status_of(uid):
        return VERIFY_EXISTS if uid in found else VERIFY_UNKNOWN if uid in unknown else VERIFY_NOT_EXISTS
    result = {}
    for h, group in sender_groups.items():
        verdict = plain[h] if h in plain else _finalize(h, group["count"], None, candidate_map[h], methods, status_of, found.get)
        uid, confidence, method, info, risk, candidates = verdict
        result[h] = {**group, "uid": uid, "confidence": confidence, "method": method,
                     "verification_stopped": budget.stopped,
                     "user_info": info or {}, "collision_risk": risk, "candidates": candidates,
                     "danmaku_count": group["count"],
                     "resolution": resolution_record(h, verdict, mapping, methods, max_search)}
        if h in candidate_map:
            result[h]["resolution"]["candidate_status"] = {str(u): status_of(u) for u in candidates}
            try:
                result[h]["resolution"]["candidate_checked_at"] = load_uid_verify_times(candidates)
            except sqlite3.Error:
                pass
    return result


def resolve_sender(mid_hash: str, danmaku_contents: list[str], comment_uid_map: dict,
                   client: BiliAPIClient, max_search: int = MITM_MAX_UID,
                   method_map: dict | None = None,
                   verification_budget: VerificationBudget | None = None) -> Tuple[Optional[int], str, str, Optional[dict], bool, list]:
    item = _resolve_many({mid_hash: {"count": len(danmaku_contents), "contents": danmaku_contents}},
                         comment_uid_map, client, max_search, method_map, verification_budget)[mid_hash]
    return item["uid"], item["confidence"], item["method"], item["user_info"], item["collision_risk"], item["candidates"]


def resolve_all_senders(sender_groups: dict, comment_uid_map: dict, client: BiliAPIClient,
                        max_search: int = MITM_MAX_UID, method_map: dict | None = None) -> dict:
    print(f"[Resolver] 解析 {len(sender_groups)} 个发送者（保留碰撞候选，验证失败保持未知）...")
    results = _resolve_many(sender_groups, comment_uid_map, client, max_search, method_map)
    assigned = sum(v["uid"] is not None for v in results.values())
    ambiguous = sum(v["method"] == METHOD_AMBIGUOUS for v in results.values())
    pending = sum(v["method"] == METHOD_PENDING for v in results.values())
    print(f"[Resolver] 已分配候选 UID {assigned}/{len(results)}，歧义 {ambiguous}，待验证 {pending}；分配不代表身份确认")
    return results
