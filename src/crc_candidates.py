"""纯 CRC32 候选展示：匿名问题证据与候选账号分开，不自动选择碰撞 UID。"""
from collections import Counter, defaultdict
import time
from urllib.parse import urlsplit

import config
from candidate_space import candidate_uids
from report import esc
from uid_evidence import PLAINTEXT_SOURCES

CRC_METHODS = {"CRC32破解", "候选歧义", "待验证", "CRC32碰撞"}


def is_pure_crc(sender: dict) -> bool:
    resolution = sender.get("resolution") or {}
    if not isinstance(resolution, dict) or sender.get("method") not in CRC_METHODS:
        return False
    return not any(isinstance(e, dict) and e.get("source") in PLAINTEXT_SOURCES
                   for e in resolution.get("evidence") or [])


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
        candidates = []
        for uid in uids:
            status, info = (verifications or {}).get(uid, ("unknown", {}))
            note = "最近核验结果" if status != "unknown" else "尚待验证"
            if status == "unknown":
                previous = recorded.get(str(uid))
                ttl = getattr(config, "UID_VERIFY_EXISTS_TTL", 604800) if previous == "exists" else getattr(config, "UID_VERIFY_MISSING_TTL", 3600)
                try:
                    times = resolution.get("candidate_checked_at") or {}
                    at = (times if isinstance(times, dict) else {}).get(str(uid), resolution.get("checked_at", 0))
                    recent = (0 <= now - float(at) < ttl and
                              (verification_times or {}).get(str(uid), -1) < float(at))
                except (TypeError, ValueError):
                    recent = False
                if previous in ("exists", "not_exists") and recent:
                    status, note = previous, "解析时的存在性核验"
                elif previous in ("exists", "not_exists"):
                    note = "旧存在性核验已过期，需复核"
            candidates.append({"uid": uid, "status": status, "name": info.get("name", ""),
                               "face": info.get("face", ""), "sign": info.get("sign", ""),
                               "card_ready": status == "not_exists" or (status == "exists" and "sign" in info and "face" in info),
                               "note": note})
        exists = [c["uid"] for c in candidates if c["status"] == "exists"]
        known = all(c["status"] != "unknown" for c in candidates)
        complete = sender.get("_candidate_complete", resolution.get("candidate_version") == "mitm-v1")
        resolved = (complete and known and len(exists) == 1 and sender.get("uid") == exists[0]
                    and sender.get("confidence") == "高" and sender.get("method") == "CRC32破解")
        if resolved and not include_resolved:
            continue
        predicted = ((exists[0], "高", "CRC32破解") if len(exists) == 1 else
                     (None, "无", "候选歧义" if exists else "CRC32碰撞"))
        needs_reconcile = known and (not complete or (sender.get("uid"), sender.get("confidence"), sender.get("method")) != predicted)
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
    sign = c.get("sign") or ("未设置签名" if c.get("card_ready") else "签名待读取")
    return f'''<article class="space-candidate" data-hash="{esc(mid_hash)}" data-uid="{uid}" data-endpoint="{esc(endpoint)}"
        data-card-ready="{'1' if c.get('card_ready') or not auto_load else '0'}">
        <a class="candidate-card" href="https://space.bilibili.com/{uid}" target="_blank" rel="noopener">
            {avatar}<span class="candidate-info"><strong class="candidate-name">{esc(name or '昵称待读取')}</strong>
            <span class="candidate-sign">{esc(sign)}</span><span class="candidate-meta">UID:{esc(uid_disp)}
            <span class="candidate-status">{esc(status)}</span></span></span><span class="candidate-arrow" aria-hidden="true">↗</span></a>
        <div class="candidate-actions"><span class="candidate-card-note" role="status"></span>
        <button class="filter-btn candidate-card-retry" hidden>重试名片</button>
        <button class="filter-btn candidate-space" data-hash="{esc(mid_hash)}" data-uid="{uid}" data-endpoint="{esc(endpoint)}">核验空间线索</button></div>
        <div class="space-result" role="status">{result}</div></article>'''


def render_crc_group(group: dict, endpoint: str, mask: bool, mask_name, mask_uid,
                     assessments: dict | None = None, assessment_renderer=None) -> str:
    """匿名证据在上，全部候选按长条名片依次排列。"""
    h = group["mid_hash"]
    exists = [c for c in group["candidates"] if c["status"] == "exists"]
    unknown = any(c["status"] == "unknown" for c in group["candidates"])
    unique = len(exists) == 1 and not unknown and group.get("candidate_complete", False)
    candidates, missing = [], []
    for c in group["candidates"]:
        uid = c["uid"]
        uid_disp = mask_uid(uid) if mask else str(uid)
        if c["status"] == "not_exists":
            missing.append(f'<li>UID:{esc(uid_disp)} · 明确不存在 · {esc(c["note"])}</li>')
            continue
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
    conclusion = ("搜索范围内唯一存在候选 · 高（存在性排除）" if unique
                  else "搜索范围内候选均不存在" if not candidates else "候选身份未确认")
    return f'''<section class="crc-sender" data-hash="{esc(h)}" data-endpoint="{esc(endpoint)}"
        data-uid="{group['candidates'][0]['uid']}" data-reconcile="{'1' if group.get('reconcile_needed') else '0'}"
        data-card-ready="{'0' if group.get('reconcile_needed') else '1'}"><div class="crc-heading"><b>匿名发送者 <code>{esc(h)}</code></b>
        <span class="hot-badge">{group['danmaku_count']} 条已采弹幕</span><span class="method-badge">{esc(group['method'])}</span>
        <span class="risk-badge">{esc(conclusion)}</span><span class="crc-status-note candidate-card-note" role="status"></span></div><p class="chart-hint">{esc(group['reason'])}</p>
        {evidence}<div class="crc-candidate-list">{''.join(candidates)}</div>
        {'<details><summary>已排除账号（明确不存在）</summary><ul class="ov-list">' + ''.join(missing) + '</ul></details>' if missing else ''}</section>'''


def render_crc_groups(groups: list[dict], endpoint: str, mask: bool, mask_name, mask_uid,
                      assessments: dict | None = None, assessment_renderer=None,
                      target_mode: bool = False) -> str:
    if not groups:
        return '''<div class="crc-candidates" data-count="0"><p class="empty-note">暂无待复核的纯 CRC32 候选记录。
            已完成存在性排除的高置信度发送者保留在正常解析结果中。</p></div>'''
    scope = ("本页列出本次目标筛选中的纯破解候选；命中筛选不等于问题判定。"
             if target_mode else "本页集中列出有问题弹幕或高中风险刷屏、且解析时没有明文来源佐证的记录。")
    head = f'''<p class="chart-hint">{scope} 共 <span data-crc-total>{len(groups)}</span> 个待复核匿名发送者。同一 hash 的所有已存候选均列出。</p>
        <p class="chart-hint">点击长条名片直达空间；可见候选按限速读取公开名片，不采评论或完整画像。
        仅名片接口明确返回 -404 才排除；其他候选全部明确不存在时，唯一存在者标为高（存在性排除）并移出本页。</p>
        <p class="chart-hint">原文属于匿名发送者，不能据此断言下方每个候选账号都发过这些弹幕。
        仍可能碰撞搜索范围外的 UID；不写入明文证据库，未知和歧义候选不用于 UID 屏蔽。</p>'''
    return f'<div class="crc-candidates" data-count="{len(groups)}">' + head + ''.join(
        render_crc_group(g, endpoint, mask, mask_name, mask_uid, assessments, assessment_renderer) for g in groups) + '</div>'
