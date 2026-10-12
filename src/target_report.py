"""目标弹幕报告：筛选概览、画像卡片、命中弹幕与目标评论原文。"""
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlencode

import config
from report import REPORT_CSS, esc, js_json, generate_user_card
from candidate_space import candidate_uids
from crc_candidates import build_crc_groups, render_crc_groups, render_candidate_card
from storage import load_uid_verify_cache
from uid_resolver import UID_VERIFY_EXISTS_TTL, UID_VERIFY_MISSING_TTL
from uid_evidence import confidence_label


def page_number(value, total: int, size: int) -> tuple[int, int]:
    pages = max(1, (total + size - 1) // size)
    try:
        page = int(value)
    except (ValueError, TypeError):
        page = 1
    return max(1, min(page, pages)), pages


def target_list_html(rows: list[dict]) -> str:
    if not rows:
        return ""
    items = "".join(f'''<li><a href="/target/{esc(row['id'])}">{esc(row['summary']['title'])}</a>
        · {esc('正则搜索' if row['selector']['mode'] == 'regex' else '完整匹配')}
        <code>{esc(row['selector']['pattern'])}</code> · 命中 {row['summary'].get('matched_count', 0)} 条
        · 画像 {row['summary'].get('profile_count', 0)} 人 · {esc(row['updated_at'])}</li>''' for row in rows)
    return f'<section class="xv-panel" id="target-reports"><h2>最近的目标报告（{len(rows)} 份）</h2><ul class="target-report-list">{items}</ul></section>'


def render_target_list_page(rows: list[dict]) -> str:
    """独立目标列表页仅读取存档摘要，不触发视频或用户采集。"""
    content = target_list_html(rows) or '''<section class="xv-panel"><p class="empty-note">
        暂无目标分析报告，可先运行 <code>python run.py &lt;BV号&gt; "目标弹幕"</code>。</p></section>'''
    return f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><meta name="referrer" content="no-referrer">
        <title>目标弹幕分析 - 报告列表</title><style>{REPORT_CSS}</style>
        <link rel="stylesheet" href="/static/index.css"></head><body><div class="container">
        <header class="header"><h1>目标弹幕分析</h1><div class="meta">目标报告列表 · 按最近更新时间排列</div></header>
        <nav class="idx-controls"><a class="pager-btn target-entry" href="/">返回首页</a></nav>
        {content}</div></body></html>'''


def render_space_assessment(assessment: dict, mask: bool, mask_name, mask_uid) -> str:
    """空间线索只用于人工核验，所有公开原文按文本转义。"""
    def entry(item):
        text = item.get("text", "")
        if mask and item.get("kind") == "following":
            text = mask_name(text)
        url = item.get("url", "")
        # 即使缓存损坏，也不允许把任意链接协议注入报告。
        allowed = url.startswith(("https://www.bilibili.com/video/BV", "https://t.bilibili.com/",
                                  "https://space.bilibili.com/"))
        link = f' <a href="{esc(url)}" target="_blank" rel="noopener">查看原文</a>' if allowed else ""
        return f'<li><b>{esc(item.get("label") or item["source"])}</b> · {esc(text)}{link}</li>'

    uid = mask_uid(assessment["uid"]) if mask else str(assessment["uid"])
    name = mask_name(assessment.get("name")) if mask else assessment.get("name", "")
    statuses = {"ok": "已检查", "unavailable": "不可用", "risk": "风控中止", "skipped": "未请求", "not_exists": "账号不存在"}
    coverage = "；".join(f'{c["source"]}：{statuses.get(c["status"], "不可用")}（{c["count"]} 条）'
                         for c in assessment["coverage"])
    evidence = "".join(entry(e) for e in assessment["evidence"])
    examples = "".join(entry(e) for e in assessment["examples"])
    return f'''<div class="space-assessment"><p><b>{esc(assessment['verdict'])}</b></p>
        <p>{esc(name)} · UID:{esc(uid)} · {esc(assessment['checked_at'])}</p>
        <ul>{evidence}</ul>{'<p>未找到可列出的支持线索。</p>' if not evidence else ''}
        <details><summary>公开内容样本（可比较主题与措辞）</summary><ul>{examples}</ul>
        {'<p>无可见文字样本。</p>' if not examples else ''}</details>
        <p class="target-note">{esc(coverage)}</p><p class="target-note">{esc(assessment['limit_note'])}</p></div>'''


def render_target_report(record: dict, args, mask: bool, mask_name, mask_uid,
                         false_positives: set | None = None, candidate_checks: dict | None = None,
                         normal_report_available: bool = True, crc_tab_html: str | None = None) -> str:
    data, selector = record["result"], record["selector"]
    target_id, bvid = record["id"], record["bvid"]
    stats = data["stats"]
    profiles, senders = data.get("profiles", []), data.get("senders", [])
    user_size = max(1, getattr(config, "TARGET_PROFILE_PAGE_SIZE", 24))
    sender_size = max(1, getattr(config, "TARGET_MATCH_PAGE_SIZE", 100))
    user_page, user_pages = page_number(args.get("users_page"), len(profiles), user_size)
    sender_page, sender_pages = page_number(args.get("senders_page"), len(senders), sender_size)
    comments = sorted(data.get("comments", []), key=lambda c: c.get("ctime") or 0, reverse=True)
    comment_page, comment_pages = page_number(args.get("comments_page"), len(comments), sender_size)
    disp_uid = mask_uid if mask else str
    disp_name = mask_name if mask else str
    profile_pages = {p["uid"]: i // user_size + 1 for i, p in enumerate(profiles)}
    verifications = load_uid_verify_cache(sorted({u for s in senders for u in candidate_uids(s)}),
                                         UID_VERIFY_EXISTS_TTL, UID_VERIFY_MISSING_TTL)

    def pager(key, current, pages, tab):
        def link(n, label):
            params = {"users_page": user_page, "senders_page": sender_page, "comments_page": comment_page, key: n}
            return f'<a class="pager-btn" href="?{esc(urlencode(params))}#tab={tab}">{label}</a>'
        return f'<div class="user-pager">{link(current-1, "上一页") if current>1 else ""}<span>第 {current} / {pages} 页</span>{link(current+1, "下一页") if current<pages else ""}</div>'

    rows = []
    for sender in senders[(sender_page - 1) * sender_size:sender_page * sender_size]:
        uid = sender.get("uid")
        label = (f'{esc(disp_name(sender.get("name") or ""))} UID:{esc(disp_uid(uid))}' if uid else "未解析")
        if uid in profile_pages:
            label = f'<a href="?users_page={profile_pages[uid]}#uid-{uid}">{label}</a>'
        candidates = "、".join(disp_uid(u) for u in sender.get("candidates", [])) or "无"
        reason = sender.get("resolution", {}).get("reason", "")
        space_items = []
        if sender.get("confidence") not in ("高", "中"):
            for candidate in candidate_uids(sender):
                status, info = verifications.get(candidate, ("unknown", {}))
                if status == "not_exists":
                    continue
                assessment = (candidate_checks or {}).get((sender["mid_hash"], candidate))
                result_html = render_space_assessment(assessment, mask, mask_name, mask_uid) if assessment else ""
                c = {**info, "uid": candidate, "status": status,
                     "card_ready": status == "exists" and "sign" in info}
                space_items.append(render_candidate_card(c, sender["mid_hash"], f"/api/target/{target_id}/candidate-space",
                                                         mask, mask_name, mask_uid, result_html, auto_load=False))
        rows.append(f'''<tr><td>{label}<br><code>{esc(sender['mid_hash'])}</code></td>
            <td>{sender['matched_count']} / {sender['danmaku_count']}</td>
            <td>{esc(sender.get('method') or '待解析')} · {esc(confidence_label(sender.get('confidence', '无'), sender.get('method', '')))}<br>
                <span class="target-note">{esc(sender['status'])}</span>
                <details><summary>解析证据</summary>候选 UID：{esc(candidates)}<br>{esc(reason or '暂无补充说明')}{''.join(space_items)}</details></td>
            <td>{'<br>'.join(esc(c) for c in sender.get('samples', []))}<br>
                <button class="filter-btn target-filter" data-hash="{esc(sender['mid_hash'])}">查看全部命中</button></td></tr>''')
    sender_table = (f'<div class="dm-table-wrap"><table class="dm-table"><thead><tr><th>发送者</th><th>命中 / 本视频已采弹幕</th><th>解析与采集状态</th><th>命中原文样本</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
                    if rows else '<p class="empty-note">当前已采弹幕没有匹配结果。可修改筛选条件，或在补采弹幕后重跑。</p>')
    cards = []
    false_positives = false_positives or set()
    for source in profiles[(user_page - 1) * user_size:user_page * user_size]:
        profile = deepcopy(source)
        # 独立快照仍遵守当前人工纠偏；只改展示副本，撤销标记后可恢复原判定。
        if ("spam", profile.get("target_mid_hash")) in false_positives:
            profile.setdefault("danmaku", {}).update(spam_level="低", spam_score=0, spam_fp=True)
            profile["tags"] = [tag for tag in profile.get("tags", []) if tag not in ("重度刷屏", "中度刷屏")]
        for comment in profile.get("comments", []):
            if ("cmt", str(comment.get("rpid"))) in false_positives:
                comment["problem"] = ""
        if mask:
            profile["name"] = mask_name(profile.get("name"))
            profile["_uid_disp"] = mask_uid(profile.get("uid"))
        profile.pop("_mid_hash", None)
        h = profile.get("target_mid_hash", "")
        cards.append(f'''<section><div class="target-card-context">目标命中 {profile.get('target_count', 0)} 条
            <button class="filter-btn target-filter" data-hash="{esc(h)}">核验命中原文</button></div>{generate_user_card(profile)}</section>''')
    card_html = '<div class="user-grid">' + "".join(cards) + '</div>' if cards else '<p class="empty-note">暂无画像；解析未确认或用户资料未采集。可去掉 --skip-collect 重跑以续采。</p>'
    comment_rows = []
    trusted_uids = {s.get("uid") for s in senders if s.get("confidence") in ("高", "中")}
    for comment in comments[(comment_page - 1) * sender_size:comment_page * sender_size]:
        uid = comment["uid"]
        label = f'{esc(disp_name(comment.get("uname") or ""))} UID:{esc(disp_uid(uid))}'
        if uid not in trusted_uids:
            label += '<br><span class="target-note">碰撞候选，身份待核验</span>'
        try:
            posted = datetime.fromtimestamp(comment.get("ctime") or 0).strftime("%Y-%m-%d %H:%M:%S") if comment.get("ctime") else "未知"
        except (ValueError, TypeError, OSError, OverflowError):
            posted = "未知"
        try:
            rpid = int(comment.get("rpid") or 0)
        except (ValueError, TypeError):
            rpid = 0
        source = f'<a href="https://www.bilibili.com/video/{esc(bvid)}/#reply{rpid}" target="_blank" rel="noopener">查看原评论</a>' if rpid > 0 else ""
        comment_rows.append(f'''<tr><td>{label}</td><td class="target-comment-text">{esc(comment.get('content') or '（正文不可用）')}</td>
            <td>{'回复' if comment.get('is_sub') else '主评论'}<br>{esc(posted)}<br>点赞 {esc(comment.get('like') or 0)}<br>{source}</td></tr>''')
    comment_html = (f'<div class="dm-table-wrap"><table class="dm-table"><thead><tr><th>作者</th><th>评论原文</th><th>类型与时间</th></tr></thead><tbody>{"".join(comment_rows)}</tbody></table></div>'
                    if comment_rows else '<p class="empty-note">尚未找到目标作者的可见评论；可查看下方搜索状态。发送过弹幕不一定也发过评论。</p>')
    metrics = [("采集池弹幕", "pool_count"), ("命中弹幕", "matched_count"), ("命中发送者", "sender_count"),
               ("入选解析", "selected_count"), ("已生成画像", "profile_count"),
               ("未解析", "unresolved_count"), ("低置信度", "low_confidence_count"), ("超出人数上限", "omitted_count")]
    metric_html = "".join(f'<div class="target-metric"><strong>{stats.get(key, 0):,}</strong><span>{label}</span></div>' for label, key in metrics)
    export_kinds = dict.fromkeys(name.rsplit(".", 1)[-1] for name in data.get("exports", []) if name.endswith((".csv", ".json")))
    exports = " ".join(f'<a class="filter-btn" href="/api/target/{esc(target_id)}/export/{kind}">{kind.upper()} 下载</a>' for kind in export_kinds)
    history = data.get("coverage", {}).get("history", [])
    history_enabled = data.get("coverage", {}).get("history_enabled", True)
    lightweight = data.get("coverage", {}).get("source_mode") == "realtime_cache"
    source_note = ("本轮仅更新当前弹幕池并复用已有缓存，未补采历史弹幕；可加 --target-history 扩大搜索范围。"
                   if lightweight else "本轮按配置补采历史弹幕，仍受历史窗口和规模上限影响。")
    lookup = data.get("uid_lookup", {})
    reasons = {"found_all": "已找到全部待搜索目标", "end": "可见评论已翻完", "risk": "触发风控，已保留进度",
               "api_error": "接口异常，已保留进度", "invalid_cursor": "分页游标异常，已保留进度",
               "repeated_page": "接口返回重复页面，已停止", "not_needed": "复用已找到的评论", "budget": "达到调用方显式限额"}
    lookup_note = (f"本轮评论搜索 {lookup.get('requests', 0)} 次请求（其中楼中楼 {lookup.get('reply_requests', 0)} 次），默认无页数上限；"
                   f"{reasons.get(lookup.get('reason'), '尚未完成')}。仅保留目标作者原文，找到全部目标即停。" if lookup else "尚无评论搜索记录。")
    history_lines = "".join(f'<li>P{row["page"]}：历史窗口{"已完成" if row.get("done") == "1" else "未完成或未启用"}；'
                            f'规模截断{"是" if row.get("truncated") == "1" else "否"}；'
                            f'待补日期 {esc(row.get("failed_dates") or "无")}；待补月份 {esc(row.get("failed_months") or "无")}</li>' for row in history)
    failed = data.get("coverage", {}).get("failed_realtime_pages", [])
    status = {"running": "分析进行中", "interrupted": "分析已中断，重跑可续采", "completed": "分析完成"}.get(data.get("status"), "")
    if crc_tab_html is None:
        crc_tab_html = render_crc_groups(build_crc_groups(senders, data.get("matches", []), {},
                                                         false_positives, target_matches=data.get("matches", [])),
                                        f"/api/target/{target_id}/candidate-space", mask, mask_name, mask_uid,
                                        candidate_checks, render_space_assessment, target_mode=True)
    video_link = f'<a class="filter-btn" href="/video/{esc(bvid)}">视频报告</a>' if normal_report_available else ""
    return f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><meta name="referrer" content="no-referrer">
        <title>目标弹幕分析 - {esc(data['video_info'].get('title', bvid))}</title>
        <style>{REPORT_CSS}</style><link rel="stylesheet" href="/static/report.css"><link rel="stylesheet" href="/static/target.css">
        <link rel="stylesheet" href="/static/candidate_space.css"></head>
        <body><div class="container"><header class="header"><h1>目标弹幕分析</h1>
        <div class="meta">{esc(data['video_info'].get('title', bvid))} · {esc(bvid)} · {esc(status)} · {esc(record['updated_at'])}</div></header>
        <div class="ov-actions"><a class="filter-btn" href="/targets">返回目标列表</a><a class="filter-btn" href="/">返回首页</a>{video_link}
        {exports}<button class="filter-btn" id="targetMask">{'显示信息' if mask else '隐藏信息'}</button></div>
        <div class="target-query"><b>{'正则搜索' if selector['mode']=='regex' else '原文完整匹配'}</b> <code>{esc(selector['pattern'])}</code></div>
        <nav class="tab-bar"><button class="tab-btn active" data-tab="overview">筛选概览</button><button class="tab-btn" data-tab="users">用户画像</button><button class="tab-btn" data-tab="matches">命中弹幕</button><button class="tab-btn" data-tab="comments">目标评论</button><button class="tab-btn" data-tab="crc">纯破解候选（{crc_tab_html.count('class="crc-sender"')}）</button></nav>
        <main><div class="tab-pane active" id="tab-overview"><div class="target-metrics">{metric_html}</div>
        <p class="target-note">命中表示满足本次筛选条件。UID 来自匿名 hash 解析，候选及置信度须结合原文核验；低置信度结果不会自动采集画像。</p>
        <details class="target-coverage"><summary>采集覆盖范围</summary><p>{esc(source_note)}历史网络补采{'已启用' if history_enabled else '未启用（以下为已有检查点，可能仅代表实时池完成）'}。实时采集失败的分P：{esc('、'.join(str(p) for p in failed) or '无')}。</p><p>{esc(lookup_note)}</p><ul>{history_lines}</ul></details>
        <h2>命中发送者</h2><p class="target-note">按命中条数、该发送者在本视频的弹幕数排序；人数上限外的发送者仍保留命中记录。样本每人最多 3 条，完整原文在「命中弹幕」。待复核候选的长条名片和空间资料在「纯破解候选」页，由命令行采集保存；点击名片直达空间，页面不补采。</p>{sender_table}{pager('senders_page',sender_page,sender_pages,'overview')}</div>
        <div class="tab-pane" id="tab-users"><p class="target-note">画像参考入选者在该视频的已采弹幕和个人行为资料；命中条数单独显示。每页 {user_size} 人，共 {len(profiles)} 人。</p>{card_html}{pager('users_page',user_page,user_pages,'users')}</div>
        <div class="tab-pane" id="tab-matches"><div class="dm-controls"><span id="targetScope">全部命中</span><button class="filter-btn" id="targetReset">显示全部</button></div>
        <div class="dm-table-wrap"><table class="dm-table"><thead><tr><th>视频内时刻</th><th>命中原文</th><th>发送者</th><th>解析置信度</th></tr></thead><tbody id="targetMatches"></tbody></table></div>
        <div class="dm-pager"><button id="targetPrev">上一页</button><span id="targetPage"></span><button id="targetNext">下一页</button></div><p id="targetError" role="status"></p></div>
        <div class="tab-pane" id="tab-comments"><p class="target-note">以下是搜索途中发现及缓存中的目标作者评论，保留原文；找到全部目标即停，不代表这些作者的全部评论。</p>
        <p class="target-note">{esc(lookup_note)} 共保留 {len(comments)} 条。</p>{comment_html}{pager('comments_page',comment_page,comment_pages,'comments')}</div>
        <div class="tab-pane" id="tab-crc">{crc_tab_html}</div></main></div>
        <script>window.__TARGET__={js_json({'id': target_id, 'mask': mask})};</script><script src="/static/target.js"></script><script src="/static/candidate_space.js"></script></body></html>'''
