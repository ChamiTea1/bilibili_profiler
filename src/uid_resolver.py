"""
UID解析引擎：mid_hash MITM 反查 + 明文交叉验证 + 碰撞消歧

B站弹幕中的用户标识历史：
- 2021年前：使用 CRC32(UID) 作为 midHash，可反向破解
- 2021年后：新用户UID过长（16位随机），数学上不可解
  → 解决方案：MITM 反查覆盖全部 ≤10 位 UID（秒级返回全部候选），
    再经评论等明文来源交叉验证 / 候选交集消歧 / 存在性验证压制置信度

存在性验证走批量名片接口（≤50 UID/请求，返回 data 仅含存在的 UID，
缺席=确定不存在，已实测）；整批失败时降级单点逐验。验证结果三态化：
仅 -404 判定"不存在"，其余失败（风控/网络/非JSON降级）一律"未知"——
避免把瞬态请求失败误判成"候选不存在"（曾致风控窗口内整批发送者
被误判为碰撞假阳性）。"未知"不缓存结论，下轮运行自动重试。
"""
import zlib
from typing import Optional, Tuple

from api_client import BiliAPIClient
from config import MITM_MAX_UID, USER_CARD_URL, USER_CARDS_BATCH_URL
from crc_rainbow import lookup

# 解析方法标识（入库/缓存推断均引用这些常量，勿散落硬编码字符串）
METHOD_COMMENT_VERIFY = "评论区验证"
METHOD_CRC32_CRACK = "CRC32破解"
METHOD_CRC32_COLLISION = "CRC32碰撞"
METHOD_UNKNOWN = "未知"

# 存在性验证三态（verify_uid_exists 返回值第一元）
VERIFY_EXISTS = "exists"          # code=0 且名片有效
VERIFY_NOT_EXISTS = "not_exists"  # 仅 code=-404（或 code=0 但名片为空）
VERIFY_UNKNOWN = "unknown"        # 风控/网络/降级返回等一切其他失败


def calc_crc32(uid: int) -> str:
    """计算UID的标准CRC32（B站使用的算法）"""
    return format(zlib.crc32(str(uid).encode()) & 0xFFFFFFFF, "08x")


def crack_crc32(crc32_hash: str, max_search: int = MITM_MAX_UID) -> list:
    """
    MITM 反查 mid_hash，返回全部候选 UID（升序，每个都经 zlib 校验）

    ≤10 位空间每个 hash 平均约 2.3 个候选，消歧（评论交集/存在性验证/
    置信度压制）由调用方 resolve_sender 负责。16 位随机长 UID 数学上不可解。

    Args:
        crc32_hash: 弹幕 mid_hash（8位hex）
        max_search: UID 覆盖上限（语义由 MITM_MAX_UID 承接，保留参数名兼容旧调用）

    Returns:
        候选 UID 列表（可能为空=未命中）
    """
    return lookup(crc32_hash, max_uid=max_search)


def verify_uid_exists(uid: int, client: BiliAPIClient) -> Tuple[str, dict]:
    """
    通过API验证UID是否存在（三态）

    Returns:
        (状态, 用户基础信息dict)
        状态为 VERIFY_EXISTS 时 info 有效；VERIFY_NOT_EXISTS / VERIFY_UNKNOWN 时为空
    """
    try:
        data = client.get(USER_CARD_URL, params={"mid": uid})
        code = data.get("code")
        if code == 0:
            card = (data.get("data") or {}).get("card") or {}
            if not card or not card.get("mid"):
                return VERIFY_NOT_EXISTS, {}
            return VERIFY_EXISTS, {
                "uid": uid,
                "name": card.get("name", ""),
                "face": card.get("face", ""),
                "sign": card.get("sign", ""),
                "level": card.get("level_info", {}).get("current_level", 0),
                "sex": card.get("sex", ""),
                "vip_type": card.get("vip", {}).get("type", 0),
                "vip_status": card.get("vip", {}).get("status", 0),
                "official_type": card.get("official_verify", {}).get("type", -1),
                "follower": card.get("fans", 0),
                "following": card.get("attention", 0),
            }
        if code == -404:
            return VERIFY_NOT_EXISTS, {}
        # 风控/签名/降级返回等：不能据此判定"不存在"
        print(f"[Resolver] 警告: UID:{uid} 存在性验证异常 (code={code} {data.get('message')!r})，按未知处理")
        return VERIFY_UNKNOWN, {}
    except Exception as e:
        print(f"[Resolver] 警告: UID:{uid} 存在性验证请求异常 ({e})，按未知处理")
        return VERIFY_UNKNOWN, {}


def _verify_chunk_single(chunk: list, client: BiliAPIClient, found: dict, unknown: set):
    """单个 chunk 降级为单点逐验（保留三态语义），结果就地并入 found/unknown"""
    for u in chunk:
        status, info = verify_uid_exists(u, client)
        if status == VERIFY_EXISTS:
            found[u] = info
        elif status == VERIFY_UNKNOWN:
            unknown.add(u)


def _batch_verify_uids(uids: list[int], client: BiliAPIClient) -> Tuple[dict, set]:
    """
    批量名片接口预验证存在性（≤50 UID/请求）

    接口行为（已实测）：返回 data 仅包含存在的 UID，不存在的直接缺席，
    与单点接口 code=0/-404 一一对应。请求异常（组合池风控/代理错误等）或
    整批失败的 chunk 降级为单点 verify_uid_exists 逐验，仍失败的计入"未知"
    集合——本函数任何情况下只返回 (found, unknown)，绝不外抛异常中断分析。

    Returns:
        (存在UID→名片info, 状态未知UID集合)；两集合之外的 UID 即确定不存在
    """
    found: dict[int, dict] = {}
    unknown: set[int] = set()
    chunks = [uids[i:i + 50] for i in range(0, len(uids), 50)]
    for ci, chunk in enumerate(chunks, 1):
        try:
            data = client.get(USER_CARDS_BATCH_URL, params={
                "uids": ",".join(str(u) for u in chunk)
            })
        except Exception as e:
            # 组合池抛 RiskControlError/ProxyConnError 等：降级单点逐验，不外抛
            print(f"[Resolver] 警告: 第 {ci}/{len(chunks)} 批批量验证请求异常 ({e})，降级单点逐验")
            _verify_chunk_single(chunk, client, found, unknown)
            print(f"[Resolver] 预验证 {ci}/{len(chunks)} 批（存在 {len(found)} / 未知 {len(unknown)}）")
            continue
        if data.get("code") == 0:
            # 返回对账：返回 UID 必须全部属于本 chunk 且条数不超过请求数，否则视为
            # 接口静默截断/串包——此时"缺席即不存在"的语义不再可信，降级单点逐验
            batch_data = data.get("data") or {}
            chunk_set = set(chunk)
            entries = []
            truncated = len(batch_data) > len(chunk)
            for uid_str, card in batch_data.items():
                if truncated or not card:
                    continue
                try:
                    uid = int(uid_str)
                except (TypeError, ValueError):
                    continue
                if uid not in chunk_set:
                    truncated = True
                    continue
                entries.append((uid, card))
            if truncated:
                print(f"[Resolver] 警告: 第 {ci}/{len(chunks)} 批批量返回与请求对账不符"
                      f"（疑静默截断），降级单点逐验")
                _verify_chunk_single(chunk, client, found, unknown)
            else:
                for uid, card in entries:
                    # 批量名片为精简字段集：接口本身不含 sign/level/sex/follower/following
                    # （单点 verify_uid_exists 才有），属预期差异，不为补字段额外发请求
                    found[uid] = {
                        "uid": uid,
                        "name": card.get("name", ""),
                        "face": card.get("face", ""),
                        "vip_type": (card.get("vip") or {}).get("type", 0),
                        "vip_status": (card.get("vip") or {}).get("status", 0),
                        "official_type": (card.get("official") or {}).get("type", -1),
                    }
        else:
            # 整批失败（风控/网络等）：降级单点逐验，保留三态语义
            print(f"[Resolver] 警告: 第 {ci}/{len(chunks)} 批批量验证失败"
                  f" (code={data.get('code')} {data.get('message')!r})，降级单点逐验")
            _verify_chunk_single(chunk, client, found, unknown)
        print(f"[Resolver] 预验证 {ci}/{len(chunks)} 批（存在 {len(found)} / 未知 {len(unknown)}）")
    return found, unknown


def cross_verify_with_comments(mid_hash: str, comment_uid_map: dict[str, int]) -> Optional[int]:
    """
    用评论区UID映射交叉验证mid_hash

    原理：评论区能拿到明文UID，计算其CRC32，看是否与弹幕mid_hash匹配

    Returns:
        匹配到的UID，或None
    """
    return comment_uid_map.get(mid_hash)


def _plaintext_result(method_map: dict | None, source_crc: str, danmaku_count: int):
    """
    明文交叉验证命中时的 (method, confidence, collision_risk) 判定

    全局库中 source="CRC32破解" 的条目复用时保留碰撞风险标记与置信度压制
    （跨视频口径一致，避免复用后风险角标消失、置信度膨胀）；其余明文源
    （评论/充电名单/互动弹幕）置信度按弹幕数规则。
    """
    method = (method_map or {}).get(source_crc, METHOD_COMMENT_VERIFY)
    if method == METHOD_CRC32_CRACK:
        return method, ("中" if danmaku_count >= 2 else "低"), True
    return method, ("高" if danmaku_count >= 2 else "中"), False


def _finalize(mid_hash: str, danmaku_count: int, plain_uid: Optional[int],
              candidates: list, method_map: dict | None, status_of, info_of):
    """
    存在性已知后的纯本地消歧（批量预验证/单点逐验两路径共用，保证判定口径一致）

    Args:
        plain_uid: 明文交叉验证命中的 UID（未命中为 None）
        candidates: MITM 全部候选
        status_of: uid -> VERIFY_EXISTS / VERIFY_NOT_EXISTS / VERIFY_UNKNOWN
        info_of: uid -> 名片 info 或 None

    Returns:
        (uid, confidence, method, user_info, collision_risk, candidates)，同 resolve_sender
    """
    # === 方法1：明文交叉验证（合并映射，来源由 method_map 标注） ===
    # 明文证据本身权威：状态未知（验证请求失败）时仍采信；仅确定不存在才放弃（理论不发生）
    if plain_uid is not None and status_of(plain_uid) != VERIFY_NOT_EXISTS:
        method, confidence, risk = _plaintext_result(method_map, mid_hash, danmaku_count)
        return plain_uid, confidence, method, info_of(plain_uid), risk, []

    # === 方法2：MITM 反查 + 碰撞消歧 ===
    if not candidates:
        return None, "无", METHOD_UNKNOWN, None, False, candidates

    # 注：原"候选 ∩ 明文 UID 集合"消歧分支已删除——在 comment_uid_map 满足
    # key == crc32(value) 的不变式下，交集中的候选 u 必然也有
    # comment_uid_map[mid_hash] == u（候选均经 zlib 校验 crc32(u) == mid_hash），
    # 即方法1 的明文命中，已被先行短路处理，该分支数学上不可达。

    # 按存在性分桶判定
    existing = [u for u in candidates if status_of(u) == VERIFY_EXISTS]
    unknown_n = sum(1 for u in candidates if status_of(u) == VERIFY_UNKNOWN)

    if len(existing) == 1:
        # 恰好 1 个存在 → 方法 CRC32破解，置信度上限"中"（沿用压置信度规则）
        uid = existing[0]
        confidence = "中" if danmaku_count >= 2 else "低"  # 单条弹幕碰撞风险更高
        return uid, confidence, METHOD_CRC32_CRACK, info_of(uid), True, candidates
    if len(existing) > 1:
        # 多个存在 → 取最小 UID（注册更早、更可能活跃），置信度"低"
        existing.sort()
        uid = existing[0]
        print(f"[Resolver] 警告: hash {mid_hash} 有 {len(existing)} 个候选同时存在，"
              f"取最小 UID:{uid}（误识别风险高）")
        return uid, "低", METHOD_CRC32_CRACK, info_of(uid), True, candidates
    if unknown_n:
        # 存在候选但状态未知（请求失败/风控）：不判定、不缓存碰撞结论，下轮重试
        print(f"[Resolver] 警告: hash {mid_hash} 有 {unknown_n} 个候选存在性未知"
              f"（请求失败/风控），本轮不判定，下轮重试")
        return None, "无", METHOD_UNKNOWN, None, False, candidates

    # 候选全部确定不存在 → 碰撞假阳性
    return None, "无", METHOD_CRC32_COLLISION, None, False, candidates


def resolve_sender(
    mid_hash: str,
    danmaku_contents: list[str],
    comment_uid_map: dict[str, int],
    client: BiliAPIClient,
    max_search: int = MITM_MAX_UID,
    method_map: dict | None = None,
) -> Tuple[Optional[int], str, str, Optional[dict], bool, list]:
    """
    解析发送者UID，综合多种方法（单点逐验路径，供 quick_test 等轻量入口使用；
    主流程批量入口见 resolve_all_senders）

    优先级：
    1. 明文交叉验证（评论/充电名单/互动弹幕/全局库合并映射，100%准确）
    2. MITM 反查 + 存在性验证消歧（候选∩明文集合必已被方法1覆盖，见 _finalize 注）
    3. 标记为未知

    Args:
        mid_hash: 弹幕中的用户标识
        danmaku_contents: 该发送者的所有弹幕内容
        comment_uid_map: 明文源合并映射 crc32->UID（评论优先，见 main.phase_resolve）
        client: API客户端
        max_search: UID 覆盖上限（MITM_MAX_UID）
        method_map: 伴随映射 mid_hash->来源方法名（充电名单/互动弹幕等），
                    缺省时命中一律记"评论区验证"（向后兼容）

    Returns:
        (uid, confidence, method, user_info, collision_risk, candidates)
        candidates: MITM 全部候选（未走 MITM 路径时为空列表），供报告/全局库判断
    """
    # 存在性单点缓存：同一 UID 在一次解析内不重复请求
    verify_cache: dict[int, Tuple[str, dict]] = {}

    def status_of(u: int) -> str:
        if u not in verify_cache:
            verify_cache[u] = verify_uid_exists(u, client)
        return verify_cache[u][0]

    def info_of(u: int):
        status_of(u)
        info = verify_cache[u][1]
        return info or None

    # 方法1：明文命中先验（通过则无需 MITM 反查）
    plain_uid = cross_verify_with_comments(mid_hash, comment_uid_map)
    if plain_uid is not None and status_of(plain_uid) != VERIFY_NOT_EXISTS:
        method, confidence, risk = _plaintext_result(method_map, mid_hash, len(danmaku_contents))
        return plain_uid, confidence, method, info_of(plain_uid), risk, []

    # 方法2：MITM 反查
    candidates = crack_crc32(mid_hash, max_search=max_search)
    if not candidates:
        return None, "无", METHOD_UNKNOWN, None, False, candidates
    if len(candidates) > 1:
        print(f"[Resolver] 注意: hash {mid_hash} 有 {len(candidates)} 个碰撞候选，逐个验证存在性")
    return _finalize(mid_hash, len(danmaku_contents), None, candidates,
                     method_map, status_of, info_of)


def resolve_all_senders(
    sender_groups: dict[str, dict],
    comment_uid_map: dict[str, int],
    client: BiliAPIClient,
    max_search: int = MITM_MAX_UID,
    method_map: dict | None = None,
) -> dict[str, dict]:
    """
    批量解析所有发送者UID（批量预验证路径：全部候选一次性≤50人/请求批量验证，
    请求量较逐候选单点验证降约50倍，随后纯本地消歧，判定口径与 resolve_sender 一致）

    Returns:
        {mid_hash: {
            "uid": int or None,
            "confidence": str,
            "method": str,
            "user_info": dict,
            "danmaku_count": int,
            "contents": [str],
            "collision_risk": bool,
            "candidates": [int],  # MITM 全部候选（未走 MITM 路径时为空列表）
        }}
    """
    results = {}
    total = len(sender_groups)
    resolved = 0

    print(f"[Resolver] 开始解析 {total} 个发送者...")

    # 第1步：本地 MITM 反查全部 hash，收集待验证 UID 全集（明文命中 + 候选）
    print(f"[Resolver] MITM 反查 {total} 个 hash（本地计算）...")
    plain_hits: dict[str, int] = {}
    cand_map: dict[str, list] = {}
    universe: set[int] = set()
    for mid_hash in sender_groups:
        plain_uid = cross_verify_with_comments(mid_hash, comment_uid_map)
        if plain_uid is not None:
            # 明文已命中的 hash 在 _finalize 方法1 即短路，跳过 MITM 反查、
            # 候选记空列表且不并入待验证 universe（省本地计算与无谓验证请求）
            plain_hits[mid_hash] = plain_uid
            universe.add(plain_uid)
            cand_map[mid_hash] = []
            continue
        cands = crack_crc32(mid_hash, max_search=max_search)
        cand_map[mid_hash] = cands
        universe.update(cands)

    # 第2步：批量预验证存在性（整批失败自动降级单点逐验）
    print(f"[Resolver] 批量预验证 {len(universe)} 个候选 UID（每批≤50）...")
    found_map, unknown_set = _batch_verify_uids(sorted(universe), client)

    def status_of(u: int) -> str:
        if u in found_map:
            return VERIFY_EXISTS
        if u in unknown_set:
            return VERIFY_UNKNOWN
        return VERIFY_NOT_EXISTS

    def info_of(u: int):
        return found_map.get(u)

    # 第3步：纯本地消歧
    print(f"[Resolver] 预验证完成（存在 {len(found_map)} / 未知 {len(unknown_set)}），开始消歧...")
    for idx, (mid_hash, group) in enumerate(sender_groups.items(), 1):
        uid, confidence, method, user_info, collision_risk, candidates = _finalize(
            mid_hash, group["count"], plain_hits.get(mid_hash), cand_map[mid_hash],
            method_map, status_of, info_of)

        results[mid_hash] = {
            "uid": uid,
            "confidence": confidence,
            "method": method,
            "user_info": user_info or {},
            "danmaku_count": group["count"],
            "contents": group["contents"],
            "collision_risk": collision_risk,
            "candidates": candidates,
            "timestamps": group["timestamps"],
            "video_times": group["video_times"],
            "colors": group["colors"],
            "pages": group["pages"],
        }

        if uid:
            resolved += 1
            name = user_info.get("name", "?") if user_info else "?"
            risk_note = " ⚠️可能误识别" if collision_risk else ""
            print(f"  [{idx}/{total}] ✅ {mid_hash} → UID:{uid} ({name}) 方法:{method} 置信度:{confidence}{risk_note}")
        else:
            print(f"  [{idx}/{total}] ✗ {mid_hash} 未识别")

    print(f"[Resolver] 解析完成: {resolved}/{total} 个发送者成功识别")
    return results
