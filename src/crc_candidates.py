"""纯 CRC32 候选展示：匿名问题证据与候选账号分开，不自动选择碰撞 UID。"""
from collections import Counter, defaultdict
import re
import time
from urllib.parse import urlsplit

import config
from candidate_space import candidate_uids
from report import esc
from uid_evidence import PLAINTEXT_SOURCES
from uid_resolver import candidate_eligibility

CRC_METHODS = {"CRC32破解", "候选歧义", "待验证", "CRC32碰撞"}


def is_pure_crc(sender: dict) -> bool:
    resolution = sender.get("resolution") or {}
    if not isinstance(resolution, dict) or sender.get("method") not in CRC_METHODS:
        return False
    return not any(isinstance(e, dict) and e.get("source") in PLAINTEXT_SOURCES
                   for e in resolution.get("evidence") or [])


_SELF_SPACE_ANCHOR = re.compile(
    r'<a\b[^>]*\bhref="https://space\.bilibili\.com/(\d+)[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL)


def strip_self_space_links(html: str, uid) -> str:
    """去掉指向该候选本人的空间链接，改成纯文本；视频、动态和他人空间链接保留。"""
    if not html:
        return html or ""
    try:
        target = str(int(uid))
    except (TypeError, ValueError):
        return html

    def replace(match):
        return match.group(2) if match.group(1) == target else match.group(0)

    return _SELF_SPACE_ANCHOR.sub(replace, html)


def visible_candidate_uids(sender: dict) -> list[int]:
    """报告和导出只保留可行/未知候选；完整数学集合仅供命令行解析使用。"""
    record = sender.get("resolution") or {}
    statuses = record.get("candidate_status") or {}
    cards = record.get("candidate_cards") or {}
    values = []
    for uid in candidate_uids(sender):
        card = cards.get(str(uid)) or {}
        status = statuses.get(str(uid), card.get("status", "unknown"))
        if candidate_eligibility(status, card) not in ("not_exists", "not_eligible"):
            values.append(uid)
    return values


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_verdict(raw, exists_ttl: float, missing_ttl: float, now: float) -> tuple:
    """把核验来源统一成 (status, info, verified_at, fresh, final)。

    兼容两种形状：新结论 dict（load_uid_verify_verdicts，带 verified_at/fresh/final）
    与旧二元组 (status, info)（旧调用方与旧用例）。"""
    if isinstance(raw, dict):
        status = raw.get("status", "unknown")
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        at = _as_float(raw.get("verified_at"))
        if "fresh" in raw:
            fresh = bool(raw.get("fresh"))
        else:
            ttl = exists_ttl if status == "exists" else missing_ttl
            fresh = 0 <= now - at < ttl
        return status, info, at, fresh, bool(raw.get("final"))
    if isinstance(raw, (tuple, list)) and raw:
        status = raw[0]
        info = raw[1] if len(raw) > 1 and isinstance(raw[1], dict) else {}
        # 旧 loader 已按 TTL 过滤，拿到的即为新鲜结论
        return status, info, 0.0, status != "unknown", bool(info.get("final"))
    return "unknown", {}, 0.0, False, False


def build_crc_groups(senders: list[dict], danmaku: list[dict], problems: dict,
                     false_positives: set, verifications: dict | None = None,
                     target_matches: list[dict] | None = None, verification_times: dict | None = None,
                     include_resolved: bool = False) -> list[dict]:
    """常规报告按问题弹幕/高中刷屏选入；目标报告另保留用户显式筛选的匹配记录。"""
    by_hash, matches = defaultdict(list), defaultdict(list)
    for dm in danmaku:
        by_hash[dm.get("mid_hash")].append(dm)
    for dm in target_matches or []:
        matches[dm.get("mid_hash")].append(dm)
    fp_dm = {str(c).strip() for k, c in false_positives if k == "dm"}
    fp_spam = {h for k, h in false_positives if k == "spam"}
    output = []
    now = time.time()
    for sender in senders:
        if not is_pure_crc(sender):
            continue
        uids = candidate_uids(sender)
        if not uids:
            continue
        h = sender["mid_hash"]
        rows = by_hash[h] or matches[h]
        contents = Counter((dm.get("content") or "").strip() for dm in rows)
        if not contents:
            contents = Counter(str(c).strip() for c in sender.get("contents", sender.get("samples", [])))
        problem_items = []
        for content, count in contents.items():
            item = problems.get(content)
            if content not in fp_dm and isinstance(item, dict) and item.get("category"):
                problem_items.append({**item, "content": content, "count": count})
        spam = sender.get("spam_level", "低") if h not in fp_spam else "低"
        matched = matches[h]
        if not problem_items and spam not in ("高", "中") and not matched:
            continue
        resolution = sender.get("resolution") or {}
        recorded = resolution.get("candidate_status") or {}
        if not isinstance(recorded, dict):
            recorded = {}
        checked = resolution.get("candidate_checked_at") or {}
        checked = checked if isinstance(checked, dict) else {}
        exists_ttl = getattr(config, "UID_VERIFY_EXISTS_TTL", 604800)
        missing_ttl = getattr(config, "UID_VERIFY_MISSING_TTL", 3600)
        candidates = []
        for uid in uids:
            saved_card = (resolution.get("candidate_cards") or {}).get(str(uid))
            frozen = isinstance(saved_card, dict) and saved_card.get("uid") == uid
            card_at = _as_float(checked.get(str(uid), resolution.get("checked_at", 0)))
            # 结论来源：核验缓存优先，冻结名片若更新则覆盖（谁更新用谁）。
            status, info, at, fresh, final = _read_verdict(
                (verifications or {}).get(uid), exists_ttl, missing_ttl, now)
            note = {"exists": "已核验存在", "not_exists": "已核验不存在",
                    "not_eligible": "当前 Lv0，不具备普通视频弹幕权限"}.get(status, "尚待验证")
            if frozen and card_at >= at:
                status = saved_card.get("status", "unknown")
                info = saved_card
                at = card_at
                final = bool(saved_card.get("final"))
                fresh = 0 <= now - at < (exists_ttl if status in ("exists", "not_eligible") else missing_ttl)
                note = saved_card.get("note") or note
            status = candidate_eligibility(status, info)
            if status == "unknown":
                # 解析时记录过的结论：过期也不当作"未知"（那会让它重新变成可点击名片）
                previous = recorded.get(str(uid))
                if previous in ("exists", "not_exists", "not_eligible"):
                    prev_at = _as_float(checked.get(str(uid), resolution.get("checked_at", 0)))
                    ttl = exists_ttl if previous in ("exists", "not_eligible") else missing_ttl
                    status, at = previous, prev_at
                    fresh = 0 <= now - prev_at < ttl
                    note = "解析时已核验"
            if status in ("not_exists", "not_eligible") and not fresh:
                note = (note or "已核验不存在") + "（结论已过期，待复核）"
            elif status == "exists" and not fresh:
                note = (note or "已核验存在") + "（结论已过期，待复核）"
            # 硬约束：只有"新鲜核验为存在"的账号才允许出现跳转链接——过期的账号存在结论
            # 也可能早已注销，点进去就是 404，因此未收敛时不进入报告。
            clickable = status == "exists" and fresh
            candidates.append({"uid": uid, "status": status, "fresh": fresh, "final": final, "at": at,
                               "clickable": clickable,
                               "name": info.get("name", ""),
                               "face": info.get("face", ""), "sign": info.get("sign", ""),
                               "card_ready": status in ("not_exists", "not_eligible") or (status == "exists" and "sign" in info and "face" in info),
                               "note": note})
        # 全部候选都已稳定排除（404/Lv0）时没有可展示的身份，直接移出候选页。
        final_ttl = max(3600, int(getattr(config, "UID_RECHECK_FINAL_TTL", 30 * 86400)))
        now = time.time()
        unresolved = any(
            c["status"] == "unknown"
            or (c["status"] == "exists" and not c.get("fresh"))
            or (c["status"] == "not_eligible" and not c.get("fresh"))
            or (c["status"] == "not_exists" and not c.get("fresh")
                and not (c.get("final") and 0 <= now - c.get("at", now) < final_ttl))
            for c in candidates)
        if not any(c["status"] == "exists" for c in candidates) and not unresolved:
            continue

        # 判定只用"新鲜且已知"的证据；展示一律采用最新结论（三态见 render_crc_group）。
        # 过期证据不得用于"唯一存在→升高 UID"，否则等于用陈旧信息给账号定性。
        exists = [c["uid"] for c in candidates if c["status"] == "exists" and c["fresh"]]
        known = all(c["status"] != "unknown" for c in candidates)
        fresh_all = all(c["fresh"] for c in candidates)
        complete = sender.get("_candidate_complete", resolution.get("candidate_version") == "mitm-v1")
        final_ttl = max(3600, int(getattr(config, "UID_RECHECK_FINAL_TTL", 30 * 86400)))
        # 普通 404 缓存只有 1 小时，但终审已累计确认并标记 final 的不存在结论
        # 在终审有效期内仍然是稳定排除证据，不能因此把唯一存在者重新放回候选页。
        stable_excluded = all(
            (c["status"] == "not_eligible" and c.get("fresh"))
            or (c["status"] == "not_exists" and
                (c.get("fresh") or (c.get("final") and
                 0 <= now - c.get("at", now) < final_ttl)))
            or (c["status"] == "exists" and c.get("fresh"))
            for c in candidates)
        resolved = (complete and known and stable_excluded and len(exists) == 1 and sender.get("uid") == exists[0]
                    and sender.get("confidence") == "高" and sender.get("method") == "CRC32破解")
        if resolved and not include_resolved:
            continue
        predicted = ((exists[0], "高", "CRC32破解") if len(exists) == 1 else
                     (None, "无", "候选歧义" if exists else "CRC32碰撞"))
        needs_reconcile = known and fresh_all and (not complete or (sender.get("uid"), sender.get("confidence"), sender.get("method")) != predicted)
        output.append({"mid_hash": h, "method": sender.get("method", ""), "reason": resolution.get("reason", ""),
                       "danmaku_count": sender.get("danmaku_count", len(rows)), "spam_level": spam,
                       "problems": problem_items, "samples": contents.most_common(5),
                       "matches": list(dict.fromkeys(dm.get("content", "") for dm in matched)),
                       "matched_count": len(matched), "candidates": candidates,
                       "candidate_complete": complete,
                       "reconcile_needed": needs_reconcile and all(c["card_ready"] for c in candidates)})
    return sorted(output, key=lambda g: (-len(g["problems"]), {"高": 0, "中": 1}.get(g["spam_level"], 2),
                                         -g["matched_count"], -g["danmaku_count"], g["mid_hash"]))


def render_candidate_card(c: dict, mid_hash: str, endpoint: str, mask: bool, mask_name, mask_uid,
                          result: str = "", auto_load: bool = True) -> str:
    """整张名片可点击，辅助核验按钮独立于链接，避免嵌套交互元素。"""
    uid = c["uid"]
    uid_disp = mask_uid(uid) if mask else str(uid)
    name = mask_name(c.get("name")) if mask else c.get("name", "")
    face = c.get("face", "")
    try:
        parsed = urlsplit(face) if isinstance(face, str) else None
        if not parsed or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            face = ""
        elif parsed.scheme == "http":
            face = "https:" + face[5:]
    except ValueError:
        face = ""
    avatar = (f'<img class="candidate-avatar" src="{esc(face)}" alt="" loading="lazy" referrerpolicy="no-referrer">'
              if face else '<span class="candidate-avatar candidate-placeholder" aria-hidden="true">?</span>')
    status = ("高（存在性排除） · 唯一有效候选" if c.get("unique_verified") else
              "账号存在 · 身份未确认" if c.get("status") == "exists" else "存在性待验证 · 身份未确认")
    sign = c.get("sign") or ("未设置签名" if c.get("card_ready") else "签名未取得")
    # 硬约束：只有新鲜核验为存在、且调用方显式 clickable=True 才给跳转。
    # 缺 clickable 一律不可点，避免过期 exists 被默认成链接。
    clickable = c.get("status") == "exists" and c.get("clickable") is True
    if not clickable:
        result = strip_self_space_links(result, uid)
    head = (f'<a class="candidate-card" href="https://space.bilibili.com/{uid}" target="_blank" rel="noopener">'
            if clickable else '<div class="candidate-card candidate-card-plain">')
    tail_tag = "</a>" if clickable else "</div>"
    return f'''<article class="space-candidate" data-hash="{esc(mid_hash)}" data-uid="{uid}" data-endpoint="{esc(endpoint)}"
        data-card-ready="{'1' if c.get('card_ready') or not auto_load else '0'}">
        {head}
            {avatar}<span class="candidate-info"><strong class="candidate-name">{esc(name or '昵称未取得')}</strong>
            <span class="candidate-sign">{esc(sign)}</span><span class="candidate-meta">UID:{esc(uid_disp)}
            <span class="candidate-status">{esc(status)}</span></span></span><span class="candidate-arrow" aria-hidden="true">↗</span>{tail_tag}
        <div class="candidate-actions"><span class="candidate-card-note">{esc(c.get('note') or '命令行采集快照；缺失资料请重跑原分析命令补齐')}</span></div>
        <div class="space-result">{result or '<p class="chart-hint">命令行尚未保存空间辅助资料；此页不会补采。</p>'}</div></article>'''


def render_crc_group(group: dict, endpoint: str, mask: bool, mask_name, mask_uid,
                     assessments: dict | None = None, assessment_renderer=None) -> str:
    """匿名证据在上；三态分流——只有已核验存在的候选才可点击。

    exists → 可点击名片；unknown / 结论过期 → 命令行未收敛时不展示候选（避免把复核工作留给报告）；
    not_exists / not_eligible → 不展示账号资料。"""
    h = group["mid_hash"]
    exists = [c for c in group["candidates"] if c["clickable"]]
    final_ttl = max(3600, int(getattr(config, "UID_RECHECK_FINAL_TTL", 30 * 86400)))
    now = time.time()

    def stable(c):
        if c.get("status") == "exists":
            return bool(c.get("fresh"))
        if c.get("status") == "not_eligible":
            return bool(c.get("fresh"))
        if c.get("status") == "not_exists":
            return bool(c.get("fresh")) or bool(c.get("final")) and 0 <= now - c.get("at", now) < final_ttl
        return False

    pending = [c for c in group["candidates"] if not stable(c)]
    # 同组仍有候选未完成终审时，不能只展示当前已确认存在的一个，避免造成“唯一候选已确定”的假象。
    if pending:
        exists = []
    excluded = [c for c in group["candidates"] if c["status"] in ("not_exists", "not_eligible")]
    unique = (len(exists) == 1 and not pending and group.get("candidate_complete", False)
              and all(c.get("fresh") for c in group["candidates"]))
    candidates = []
    for c in exists:
        uid = c["uid"]
        assessment = (assessments or {}).get((h, uid))
        result = assessment_renderer(assessment, mask, mask_name, mask_uid) if assessment and assessment_renderer else ""
        candidates.append(render_candidate_card({**c, "unique_verified": unique}, h, endpoint, mask, mask_name, mask_uid, result))

    problems = "".join(f'''<li><span class="method-badge">{esc(it['category'])}</span> {esc(it['content'])}
            <span class="chart-hint">×{it['count']} · {esc(it.get('reason', ''))}</span></li>''' for it in group["problems"])
    matches = "".join(f'<li>{esc(c)}</li>' for c in group["matches"])
    samples = "".join(f'<li>{esc(c)} <span class="chart-hint">×{n}</span></li>' for c, n in group["samples"])
    evidence = (f'<details open><summary>问题弹幕原文（{len(group["problems"])} 种）</summary><ul class="ov-list">{problems}</ul></details>' if problems else "")
    evidence += f'<details open><summary>目标命中原文（{group["matched_count"]} 条）</summary><ul class="ov-list">{matches}</ul></details>' if matches else ""
    if group["spam_level"] in ("高", "中"):
        evidence += f'<details><summary>{esc(group["spam_level"])}风险刷屏 · 内容样本</summary><ul class="ov-list">{samples}</ul></details>'
    conclusion = ("搜索范围内唯一存在候选 · 高（存在性与权限排除）" if unique
                  else "搜索范围内没有可行 UID" if excluded and not candidates and not pending
                  else "命令行核验未完成，候选暂不展示" if pending
                  else "候选身份未确认")
    return f'''<section class="crc-sender" data-hash="{esc(h)}" data-endpoint="{esc(endpoint)}"
        data-uid="{exists[0]['uid'] if exists else ''}" data-reconcile="{'1' if group.get('reconcile_needed') else '0'}"
        data-card-ready="{'0' if group.get('reconcile_needed') else '1'}"><div class="crc-heading"><b>匿名发送者 <code>{esc(h)}</code></b>
        <span class="hot-badge">{group['danmaku_count']} 条已采弹幕</span><span class="method-badge">{esc(group['method'])}</span>
        <span class="risk-badge">{esc(conclusion)}</span><span class="crc-status-note candidate-card-note" role="status"></span></div><p class="chart-hint">{esc(group['reason'])}</p>
        {evidence}<div class="crc-candidate-list">{''.join(candidates)}</div>
        {''}
        </section>'''


def render_crc_groups(groups: list[dict], endpoint: str, mask: bool, mask_name, mask_uid,
                      assessments: dict | None = None, assessment_renderer=None,
                      target_mode: bool = False) -> str:
    if not groups:
        return '''<div class="crc-candidates" data-count="0"><p class="empty-note">暂无待复核的纯 CRC32 候选记录。
            未完成的核验已在命令行阶段隐藏；请重跑命令行分析后再生成报告。</p></div>'''
    scope = ("本页列出本次目标筛选中的纯破解候选；命中筛选不等于问题判定。"
             if target_mode else "本页集中列出有问题弹幕或高中风险刷屏、且解析时没有明文来源佐证的记录。")
    head = f'''<p class="chart-hint">{scope} 共 <span data-crc-total>{len(groups)}</span> 个匿名发送者。候选核验在命令行阶段完成，未完成的候选不会在报告中展示。</p>
        <p class="chart-hint">点击长条名片直达空间；名片和空间辅助资料由命令行阶段采集，此页只展示已保存数据。
        名片接口明确返回 -404 或账号明确为 Lv0 时排除；其余候选全部排除后，唯一可行者标为高置信度并移出本页。</p>
        <p class="chart-hint">原文属于匿名发送者，不能据此断言下方每个候选账号都发过这些弹幕。
        仍可能碰撞搜索范围外的 UID；不写入明文证据库，未知和歧义候选不用于 UID 屏蔽。</p>'''
    return f'<div class="crc-candidates" data-count="{len(groups)}">' + head + ''.join(
        render_crc_group(g, endpoint, mask, mask_name, mask_uid, assessments, assessment_renderer) for g in groups) + '</div>'
