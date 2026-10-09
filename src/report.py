"""
报告渲染函数库

静态单文件 HTML 报告已移除（被 web.py 交互式报告完全替换）。
本模块保留可复用的渲染件：用户卡片/问题弹幕榜/图表统计/基础 CSS，
由 web.py 服务端渲染时组装。
"""
import json
import re
import html as _html
from datetime import datetime
from collections import Counter
from urllib.parse import urlparse


# 弹幕样本与时间戳数量不一致的告警去重（web.py 每次页面重建都会调用本模块，
# 同一视频多用户/多次渲染只告警一次；按 (contents, video_times) 长度对去重，集合有界）
_DM_MISMATCH_WARNED: set = set()


def _warn_dm_mismatch(n_contents: int, n_times: int) -> None:
    """contents 与 video_times 数量不一致时打印一次告警（历史残留画像/合成键）。"""
    sig = (n_contents, n_times)
    if sig in _DM_MISMATCH_WARNED:
        return
    _DM_MISMATCH_WARNED.add(sig)
    print(f"  [Report] 警告: 弹幕样本与时间戳数量不一致 "
          f"(contents={n_contents}, video_times={n_times})，已按 contents 补齐（缺失时间显示 00:00）")


# 报告基础样式（原静态 HTML 骨架的 <style> 内容平移；web.py 页面模板注入）
REPORT_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:#e8ecf1; color:#333; line-height:1.7; font-size:17px; }
.container { max-width:1400px; margin:0 auto; padding:24px; }

.header { background:linear-gradient(135deg,#00a1d6,#fb7299); color:white; padding:22px 32px; border-radius:16px; margin-bottom:20px; box-shadow:0 10px 40px rgba(0,161,214,0.2); }
.header h1 { font-size:26px; margin-bottom:6px; }
.header .meta { opacity:0.9; font-size:14px; }

.stats-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:15px; margin-bottom:30px; }
.stat-card { background:white; padding:20px; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.04); text-align:center; }
.stat-card .num { font-size:36px; font-weight:700; color:#00a1d6; }
.stat-card .label { font-size:13px; color:#999; margin-top:5px; }

.charts-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(400px,1fr)); gap:20px; margin-bottom:30px; }
.chart-card { background:white; padding:25px; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.04); }
.chart-card h3 { font-size:17px; margin-bottom:15px; color:#555; }

.filter-bar { display:flex; gap:10px; margin-bottom:20px; flex-wrap:wrap; }
.filter-btn { padding:8px 20px; border:2px solid #e0e0e0; border-radius:25px; background:white; cursor:pointer; font-size:14px; transition:all 0.2s; }
.filter-btn:hover { border-color:#00a1d6; color:#00a1d6; }
.filter-btn.active { background:#00a1d6; color:white; border-color:#00a1d6; }

.user-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(520px,1fr)); gap:20px; }
.user-card { background:white; border-radius:14px; overflow:hidden; box-shadow:0 4px 24px rgba(0,0,0,0.10); transition:all 0.3s; border:1px solid #e8e8e8; }
.user-card:hover { transform:translateY(-3px); box-shadow:0 8px 40px rgba(0,0,0,0.15); }

.card-header { display:flex; align-items:flex-start; padding:20px; background:linear-gradient(135deg,#fafafa,#f0f0f0); }
.avatar { width:60px; height:60px; border-radius:50%; overflow:hidden; margin-right:15px; flex-shrink:0; background:#e0e0e0; display:flex; align-items:center; justify-content:center; }
.avatar img { width:100%; height:100%; object-fit:cover; }
.avatar-text { font-size:24px; font-weight:bold; color:#00a1d6; }
.header-info { flex:1; }
.name-line { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:6px; }
.username { font-size:20px; font-weight:600; }
.username-link { text-decoration:none; color:inherit; }
.username-link:hover { color:#00a1d6; }
.uid { font-size:12px; color:#999; background:#f0f0f0; padding:2px 8px; border-radius:10px; }
.level-badge { font-size:12px; background:#00a1d6; color:white; padding:2px 8px; border-radius:10px; }
.vip-badge { font-size:12px; background:#fb7299; color:white; padding:2px 8px; border-radius:10px; }
.risk-badge { font-size:12px; background:#ff9800; color:white; padding:2px 8px; border-radius:10px; }
.sign { font-size:13px; color:#666; margin-top:4px; }
.tags { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
.tag { font-size:12px; background:#e3f2fd; color:#1976d2; padding:3px 10px; border-radius:12px; }

.stats-bar { display:grid; grid-template-columns:repeat(5,1fr); gap:10px; padding:15px 20px; border-bottom:1px solid #f0f0f0; }
.stats-bar .stat { text-align:center; }
.stats-bar .num { font-size:18px; font-weight:700; color:#00a1d6; }
.stats-bar .label { font-size:12px; color:#999; }

.card-body { padding:15px 20px; }
.section { margin-bottom:15px; padding-bottom:15px; border-bottom:1px solid #f5f5f5; }
.section:last-child { border-bottom:none; margin-bottom:0; }
.section h4 { font-size:15px; color:#555; margin-bottom:10px; display:flex; align-items:center; gap:8px; }
.spam-badge { font-size:12px; padding:2px 8px; border-radius:10px; margin-left:auto; }
.spam-低 { background:#e8f5e9; color:#388e3c; }
.spam-中 { background:#fff3e0; color:#f57c00; }
.spam-高 { background:#ffebee; color:#d32f2f; }
.detail { font-size:13px; color:#666; margin-bottom:8px; }
.reason { font-size:12px; color:#d32f2f; margin-top:6px; }
.info-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; font-size:18px; color:#555; }
.list { list-style:none; font-size:13px; color:#555; }
.list li { padding:4px 0; border-bottom:1px dotted #eee; }
.list li:last-child { border-bottom:none; }

/* 弹幕编号列表 */
.dm-list { font-size:14px; color:#444; padding-left:24px; margin:6px 0; max-height:200px; overflow-y:auto; }
.dm-list li { padding:6px 0; border-bottom:1px dotted #eee; line-height:1.5; }
.dm-list li:last-child { border-bottom:none; }

.dm-time { font-size:12px; color:#999; margin-left:6px; font-family:monospace; background:#f0f0f0; padding:1px 6px; border-radius:4px; }

.samples { display:flex; flex-wrap:wrap; gap:6px; }
.sample { font-size:13px; background:#f5f5f5; color:#555; padding:4px 10px; border-radius:8px; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sample.following { background:#e8f5e9; color:#2e7d32; }

.ai-section { background:#f8f9ff; border-radius:8px; padding:12px 15px; }
.ai-section h4 { color:#6c5ce7; }
.ai-text { font-size:14px; color:#555; line-height:1.8; }
.ai-text p { margin:6px 0; }
.ai-text strong { color:#333; }
.ai-text br { display:block; content:''; margin:2px 0; }

/* 问题弹幕榜 */
.cringe-board { background:white; padding:25px; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.04); margin-bottom:30px; }
.cringe-board h3 { font-size:17px; margin-bottom:15px; color:#555; }
.cringe-board table { width:100%; border-collapse:collapse; font-size:14px; }
.cringe-board th, .cringe-board td { text-align:left; padding:8px 10px; border-bottom:1px solid #f0f0f0; vertical-align:top; }
.cringe-board th { color:#999; font-weight:500; }
.cringe-board a { color:#00a1d6; text-decoration:none; }
.cringe-reason { font-size:12px; color:#999; }

/* 关注偏好 */
.fol-section { background:#f0f4ff; border-radius:8px; padding:14px 16px; }
.fol-stats { display:flex; gap:16px; flex-wrap:wrap; font-size:14px; color:#555; margin-bottom:10px; }
.fol-stats span { background:white; padding:3px 10px; border-radius:10px; font-weight:500; }
.up-chips { display:flex; flex-wrap:wrap; gap:8px; max-height:120px; overflow-y:auto; }
.up-chip { font-size:14px; background:white; color:#2e7d32; padding:5px 12px; border-radius:10px; cursor:pointer; transition:all 0.2s; border:1px solid #e8f5e9; position:relative; }
.up-chip:hover { background:#2e7d32; color:white; transform:scale(1.05); box-shadow:0 2px 8px rgba(46,125,50,0.3); z-index:10; }

/* 词云弹窗 */
.wc-popup { display:none; position:fixed; z-index:9999; background:white; border-radius:12px; box-shadow:0 8px 30px rgba(0,0,0,0.2); padding:12px; width:300px; height:240px; }
.wc-popup canvas { width:100%; height:100%; }

/* UID 解析徽标与直播徽标（spec 7） */
.method-badge { font-size:11px; background:#f0f0f0; color:#888; padding:2px 8px; border-radius:10px; margin-left:6px; cursor:help; }
.live-badge { font-size:12px; background:#f44336; color:white; padding:2px 8px; border-radius:10px; }
.school-badge { font-size:12px; background:#e8f5e9; color:#2e7d32; padding:2px 8px; border-radius:10px; }

@media(max-width:768px){
.user-grid { grid-template-columns:1fr; }
.stats-bar { grid-template-columns:repeat(3,1fr); }
.info-grid { grid-template-columns:repeat(2,1fr); }
.charts-grid { grid-template-columns:1fr; }
}
"""


def esc(s):
    """HTML 转义用户可控文本（含引号，防属性注入）"""
    return _html.escape(str(s) if s is not None else "", quote=True)


# 问题弹幕类别分色（问题弹幕榜标签底色；与 cringe_detector.PROBLEM_CATEGORIES 对齐）
PROBLEM_CATEGORY_COLORS = {
    "中二抒情": "#9c6ade",
    "尬夸捧杀": "#f06292",
    "引战阴阳": "#e53935",
    "人身攻击": "#b71c1c",
    "恶意剧透": "#fb8c00",
    "广告引流": "#8d6e63",
    "键政敏感": "#546e7a",
    "批评吐槽": "#42a5f5",
}


def _category_chips(categories: list) -> str:
    """类别列表 → 分色标签 HTML（未知类别用灰色兜底）"""
    chips = []
    for cat in categories:
        color = PROBLEM_CATEGORY_COLORS.get(cat, "#999999")
        chips.append(f'<span style="display:inline-block;background:{color};color:#fff;'
                     f'font-size:12px;border-radius:4px;padding:1px 8px;margin:1px 2px;">{esc(cat)}</span>')
    return "".join(chips)


def safe_url(url):
    """URL 白名单校验：仅允许 http/https 且 hostname 非空（挡 "https:" 这类残串），其余返回空串"""
    url = str(url) if url else ""
    p = urlparse(url)
    if p.scheme in ("http", "https") and p.hostname:
        return esc(url)
    return ""


def js_json(obj):
    """json.dumps 后转义 </，防 </script> 截断逃逸"""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def generate_user_card(profile: dict) -> str:
    """生成单个用户的画像卡片HTML"""
    uid = profile.get("uid", 0)
    # 隐藏信息（web.py mask Cookie 开启时注入 _uid_disp）：可见 UID 文本用遮蔽值；
    # href/id 锚点/data-* 保持原值（链接仍指向真实 /user/<uid>，属已知取舍）
    uid_disp = profile.get("_uid_disp") or uid
    name = profile.get("name", "未知")
    face = profile.get("face", "")
    sign = profile.get("sign", "")
    level = profile.get("level", 0)
    sex = profile.get("sex", "")
    tags = profile.get("tags", [])

    # 标签HTML
    tag_html = "".join(f'<span class="tag">{esc(t)}</span>' for t in tags)

    # 基础信息（数值字段可能为 None：统一次级默认，避免 :, 格式化崩溃）
    follower = profile.get("follower") or 0
    following = profile.get("following") or 0
    like_num = profile.get("like_num") or 0

    # 弹幕信息
    dm = profile.get("danmaku", {})
    dm_count = dm.get("count", 0)
    dm_contents = dm.get("contents", [])
    dm_times = dm.get("video_times", [])
    # contents 与 video_times 理论同源等长；历史残留画像/合成键可能不等，
    # 以 contents 为准按索引配对、缺失位用 0 占位，避免 zip 静默截断丢样本，并告警留痕
    if len(dm_contents) != len(dm_times):
        _warn_dm_mismatch(len(dm_contents), len(dm_times))
    dm_pages = dm.get("video_pages") or []
    # 多分P：画像里记了 multi_page 即标；旧画像无该键时按样本分P 兜底推断
    multi_page = bool(dm.get("multi_page")) or (bool(dm_pages) and max(dm_pages) > 1)
    # 分P 未知的样本记 0（web 端回填歧义时不标分P，宁可不标也不标错）；
    # 样本 time 为 null 的同样按 0 占位（旧画像残留，防 t//60 与排序比较崩溃）
    paired = [(c, (dm_times[i] or 0) if i < len(dm_times) else 0,
               int(dm_pages[i] or 0) if i < len(dm_pages) else 0)
              for i, c in enumerate(dm_contents)]
    # 多分P 时先按分P 再按分P 内时间排序（time 是分P 内相对秒数，跨分P 比大小无意义）
    paired.sort(key=(lambda x: (x[2], x[1])) if multi_page else (lambda x: x[1]))
    dm_items = []
    for c, t, pg in paired:
        m, s = int(t // 60), int(t % 60)
        stamp = f"P{pg} {m:02d}:{s:02d}" if (multi_page and pg) else f"{m:02d}:{s:02d}"
        dm_items.append(f"<li>{esc(c)} <span class=\"dm-time\">{stamp}</span></li>")
    dm_list = "".join(dm_items)
    spam_level = dm.get("spam_level", "低")
    spam_reason = dm.get("spam_reason", "")
    spam_class = f"spam-{esc(spam_level)}"

    # 精确刷屏分值 + UID 解析方式/置信度徽标（tooltip 呈现，spec 7）；
    # resolve_method/resolve_confidence 由 web.py _load_profiles 渲染期注入，缺失时不渲染徽标
    spam_score = dm.get("spam_score") or 0.0   # senders.spam_score 可空，None 防御
    # 刷屏判定人工误报（kind=spam，web.py 展示层已把 spam_level 降为"低"）：
    # 已标记时徽标文本改「低·已标误报」并渲染「撤销误报」按钮；未标记的高/中风险渲染「误报」按钮。
    # mid_hash 取 web.py _load_profiles 注入的渲染期键 _mid_hash，缺失时不渲染按钮
    spam_fp = bool(dm.get("spam_fp"))
    mid_hash = profile.get("_mid_hash", "")
    spam_badge_text = "低·已标误报" if spam_fp else f"{spam_level}风险 {spam_score:.2f}分"
    spam_fp_btn = ""
    if mid_hash and spam_fp:
        spam_fp_btn = (f'<button class="fp-btn fp-btn-marked" data-kind="spam" '
                       f'data-target="{esc(mid_hash)}" onclick="fpToggle(this)" '
                       f'title="已人工标记为误报（按低风险展示）；点击撤销恢复原判定">撤销误报</button>')
    elif mid_hash and spam_level in ("高", "中"):
        spam_fp_btn = (f'<button class="fp-btn" data-kind="spam" '
                       f'data-target="{esc(mid_hash)}" onclick="fpToggle(this)" '
                       f'title="人工标记该发送者的刷屏判定为误报；标记后按低风险展示，可撤销">误报</button>')
    resolve_method = profile.get("resolve_method", "")
    resolve_confidence = profile.get("resolve_confidence", "")
    method_badge = (f'<span class="method-badge" title="UID 解析方式：{esc(resolve_method)}">解析:{esc(resolve_method)}</span>'
                    if resolve_method else "")
    conf_badge = (f'<span class="method-badge" title="解析置信度：{esc(resolve_confidence)}（低置信度可能误识别）">置信度:{esc(resolve_confidence)}</span>'
                  if resolve_confidence and resolve_confidence != "无" else "")   # "无"=无置信度信息，不渲染

    # 活动模式
    act = profile.get("activity_pattern", {})
    act_type = act.get("activity_type", "未知")
    peak_hour = act.get("peak_hour")
    peak_day = act.get("peak_day")

    # 收藏夹
    fav = profile.get("favorite", {})
    fav_names = fav.get("names", [])[:5]

    # IP属地（格式 "IP属地：江苏"，可能为空）
    ip_location = profile.get("ip_location", "")

    # 视频（recent 带 bvid，渲染为新标签页超链接；过滤空 bvid 防死链）
    vid = profile.get("video") or {}
    vid_count = vid.get("count") or 0
    vid_recent = [v for v in vid.get("recent", [])[:3] if v.get("bvid")]

    # 动态
    dyn = profile.get("dynamic") or {}
    dyn_count = dyn.get("count") or 0
    dyn_total_likes = dyn.get("total_likes", 0)

    # 采集时间（users.collected_at 渲染期注入，ISO 串取日期部分）；缺失不渲染
    collected_at = (profile.get("collected_at") or "")[:10]

    # 关注偏好
    fol_summary = profile.get("following_summary", {})
    all_names = profile.get("all_following_names", [])
    all_raw = profile.get("all_followings_raw", [])
    # name → raw info 映射
    raw_map = {r.get("name", ""): r for r in all_raw}
    fol_section = ""
    if all_names:
        # 构建 name → up_details 映射（旧版缓存画像才有深度分析；新口径采集阶段只存名单，
        # 词云改为悬停时经 /api/up/<uid>/wordcloud 懒加载）
        up_detail_map = {}
        for i, up in enumerate(fol_summary.get("up_details", [])):
            up_detail_map[up.get("name", "")] = (i, up)

        cats_str = "、".join(f"{esc(c)}({n})" for c, n in fol_summary.get("top_categories", [])[:4])
        up_names = ""
        # 循环变量用 up_name/raw_sign，避免遮蔽卡片头部使用的外层 name/sign（用户名/签名）
        for up_name in all_names:
            if up_name in up_detail_map:
                i, up = up_detail_map[up_name]
                wf = up.get("word_freq", [])
                tip = (f"粉丝:{up.get('follower') or 0:,} | 投稿:{up.get('video_count') or 0}"
                       f" | 分区:{up.get('top_category') or '?'}")
                kw = ", ".join(w for w, _ in wf[:5]) if wf else ""
                if kw:
                    tip += f" | 关键词: {kw}"
                up_id = f"up_{uid}_{i}"
                up_names += f'<span class="up-chip" data-upid="{esc(up_id)}" data-uid="{esc(uid)}" title="{esc(tip)}">{esc(up_name)}</span>'
            else:
                raw = raw_map.get(up_name, {})
                raw_sign = raw.get("sign", "")
                tip = f"签名: {raw_sign}；" if raw_sign else ""
                tip += "悬停加载该 UP 主近期投稿词云"
                # data-up-uid：懒加载词云的目标 UP 主 uid（旧版缓存画像缺 uid 时退化为纯展示）
                up_uid = raw.get("uid", 0)
                lazy_attr = f' data-up-uid="{esc(up_uid)}"' if up_uid else ""
                up_names += f'<span class="up-chip"{lazy_attr} title="{esc(tip)}">{esc(up_name)}</span>'

        total = fol_summary.get("total", len(all_names))
        cats_span = f"<span>偏好: {cats_str}</span>" if cats_str else ""
        fol_section = f'''
            <div class="section fol-section">
                <h4>🕸️ 关注偏好</h4>
                <div class="fol-stats">
                    <span>关注 {esc(total)} 人 (全部展示)</span>
                    {cats_span}
                </div>
                <div class="up-chips">{up_names}</div>
            </div>'''

    # 直播信息（spec 7）：有直播间才渲染小节；直播中加徽标
    live = profile.get("live", {})
    live_section = ""
    if live.get("has_room"):
        live_status_badge = '<span class="live-badge">直播中</span>' if live.get("is_live") else ""
        live_title = f'：{esc(live.get("room_title", ""))}' if live.get("room_title") else ""
        live_section = f'''
            <div class="section">
                <h4>📡 直播 {live_status_badge}</h4>
                <div class="detail">有直播间{live_title}</div>
            </div>'''

    # 追番
    bangumi = profile.get("bangumi_titles", [])[:3]
    dramas = profile.get("drama_titles", [])[:3]

    # AI画像分析（仅深掘；ai_analysis 为兼容旧报告数据保留）
    ai_deep = profile.get("ai_deep", "")
    ai_text = ai_deep or profile.get("ai_analysis", "")
    ai_heading = "🤖 AI 深度画像"
    ai_section = ""
    if ai_text:
        # 简单渲染：先转义，再用正则把成对的 **粗体** 渲染为 <strong>
        safe = esc(ai_text)
        safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
        paragraphs = safe.split("\n")
        ai_html = "".join(f"<p>{p}</p>" if p.strip() else "<br>" for p in paragraphs)
        ai_section = f'''
            <div class="section ai-section">
                <h4>{ai_heading}</h4>
                <div class="ai-text">{ai_html}</div>
            </div>'''

    # 问题弹幕内联标记（弹幕行为行尾；类别用分色 chips，与问题弹幕榜视觉一致）
    cringe = profile.get("cringe", {})
    cringe_note = (f'，其中问题弹幕 {cringe["count"]} 条（{_category_chips(cringe.get("categories", []))}）'
                   if cringe.get("count") else "")
    # 问题评论直引标记（P0-a）：因问题评论达阈值并入画像的作者，弹幕行为行尾标注命中统计
    cp = profile.get("comment_problem") or {}
    cp_note = (f'，问题评论 {cp["hits"]} 条（最高严重度 {cp["max_severity"]}）'
               if cp.get("hits") else "")

    # 本视频评论小节（按点赞降序，至多10条，来自阶段6注入；is_sub 子评论加前缀标注，
    # problem 非空（LLM 问题评论判定）时尾部分色类别标注）
    comments = profile.get("comments", [])
    cmt_section = ""
    if comments:
        items = []
        for c in comments:
            ts = c.get("ctime", 0)
            date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            sub_mark = '<span class="dm-time">回复</span> ' if c.get("is_sub") else ""
            problem_chip = _category_chips([c["problem"]]) if c.get("problem") else ""
            items.append(f"<li>{sub_mark}{esc(c.get('content',''))} "
                         f"<span class=\"dm-time\">👍{c.get('like',0)} {date}</span>{problem_chip}</li>")
        cmt_section = f'''
            <div class="section">
                <h4>💬 TA 在本视频的评论</h4>
                <ol class="dm-list">{"".join(items)}</ol>
            </div>'''

    # 跨视频足迹小节（web.py 渲染期注入 other_videos；无数据不渲染）
    other_videos = profile.get("other_videos") or {}
    ov_items = other_videos.get("items") or []
    ov_more = other_videos.get("more", 0)
    ov_section = ""
    if ov_items:
        ov_html = []
        for it in ov_items:
            title = it.get("title") or it.get("bvid", "")
            dm_samples = it.get("danmaku_samples") or []
            cmt_samples = it.get("comment_samples") or []
            dm_count = it.get("danmaku_count", 0)
            cmt_count = it.get("comment_count", 0)
            legacy = it.get("legacy", False)   # 旧版本分析：弹幕/评论均未留存
            dm_html = "".join(f"<li>{esc(c)}</li>" for c in dm_samples) or (
                '<li class="ov-none">弹幕明细未留存（该视频为旧版本分析）</li>'
                if legacy or dm_count else '<li class="ov-none">无弹幕样本</li>')
            cmt_lis = []
            for c in cmt_samples:
                ts = c.get("ctime", 0)
                date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
                sub_mark = '<span class="dm-time">回复</span> ' if c.get("is_sub") else ""
                problem_chip = _category_chips([c["problem"]]) if c.get("problem") else ""
                cmt_lis.append(f'<li>{sub_mark}{esc(c.get("content", ""))} '
                               f'<span class="dm-time">👍{c.get("like", 0)} {date}</span>{problem_chip}</li>')
            cmt_html = "".join(cmt_lis) or (
                '<li class="ov-none">评论未留存（该视频为旧版本分析）</li>'
                if legacy else '<li class="ov-none">无评论</li>')
            ov_html.append(f'''
                <div class="ov-item">
                    <div class="ov-head"><a class="ov-link" href="/video/{esc(it["bvid"])}">《{esc(title)}》</a>
                        <span class="ov-note">弹幕 {dm_count:,} · 评论 {cmt_count:,}</span></div>
                    <div class="ov-sub">💬 弹幕样本</div><ul class="ov-list">{dm_html}</ul>
                    <div class="ov-sub">📝 评论样本</div><ul class="ov-list">{cmt_html}</ul>
                </div>''')
        ov_more_html = (f'<div class="ov-more">另有 {ov_more} 个已分析视频也出现过（可在对应视频报告中查看）</div>'
                        if ov_more else "")
        ov_section = f'''
            <div class="section">
                <h4>🔗 其他视频足迹</h4>
                <div class="other-videos">{"".join(ov_html)}</div>
                {ov_more_html}
            </div>'''

    # 头像 (可点击跳转B站主页；referrerpolicy="no-referrer" 防 hdslb 防盗链拦截导致头像空白)
    profile_url = safe_url(f"https://space.bilibili.com/{uid}")
    avatar_html = f'<a href="{profile_url}" target="_blank" rel="noopener"><img src="{safe_url(face)}" alt="{esc(name)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display=\'none\'"></a>' if face else f'<div class="avatar-text">{esc(name[0]) if name else "?"}</div>'

    return f'''
    <div class="user-card" id="uid-{esc(uid)}" data-level="{esc(level)}" data-vip="{'true' if profile.get('vip_status',0)==1 else 'false'}" data-spam="{esc(spam_level)}" data-official="{'true' if profile.get('official_type',-1)>=0 else 'false'}" data-is-up="{'true' if profile.get('archive_count',0)>0 else 'false'}" data-spam-score="{spam_score:.2f}" data-danmaku-count="{esc(dm_count)}" data-fans="{esc(follower)}">
        <div class="card-header">
            <div class="avatar">{avatar_html}</div>
            <div class="header-info">
                <div class="name-line">
                    <a href="{profile_url}" target="_blank" rel="noopener" class="username-link"><span class="username">{esc(name)}</span></a>
                    <span class="uid">UID:{esc(uid_disp)}</span>
                    <span class="level-badge">Lv.{esc(level)}</span>
                    {f'<span class="school-badge" title="毕业院校（来自空间信息）">🎓 {esc(profile.get("school", ""))}</span>' if profile.get('school') else ''}
                    { '<span class="vip-badge">大会员</span>' if profile.get('vip_status')==1 else '' }
                    { '<span class="risk-badge" title="该UID由CRC32反查（MITM）得出，存在碰撞误识别风险">可能误识别</span>' if profile.get('collision_risk') else '' }
                </div>
                <div class="tags">{tag_html}</div>
                {f'<div class="sign">{esc(sign)}</div>' if sign else ''}
            </div>
        </div>

        <div class="stats-bar">
            <div class="stat"><div class="num">{follower:,}</div><div class="label">粉丝</div></div>
            <div class="stat"><div class="num">{following:,}</div><div class="label">关注</div></div>
            <div class="stat"><div class="num">{like_num:,}</div><div class="label">获赞</div></div>
            <div class="stat"><div class="num">{vid_count}</div><div class="label">投稿</div></div>
            <div class="stat"><div class="num">{dyn_count}</div><div class="label">动态</div></div>
        </div>

        <div class="card-body">
            <div class="section">
                <h4>🎤 弹幕行为 <span class="spam-badge {spam_class}">{esc(spam_badge_text)}</span>{method_badge}{conf_badge}{spam_fp_btn}</h4>
                <div class="detail">共发送 {esc(dm_count)} 条弹幕{cringe_note}{cp_note}</div>
                <ol class="dm-list">{dm_list}</ol>
                {f'<div class="reason">判定: {esc(spam_reason)}</div>' if spam_reason else ''}
            </div>
            {cmt_section}
            {ov_section}
            <div class="section">
                <h4>👤 基础信息</h4>
                <div class="info-grid">
                    <span>性别: {esc(sex) or "未知"}</span>
                    {f'<span>{esc(ip_location)}</span>' if ip_location else ''}
                    <span>活跃模式: {esc(act_type)}</span>
                    {f'<span>高峰时段: {esc(peak_hour)}:00</span>' if peak_hour is not None else ''}
                    {f'<span>活跃星期: {esc(peak_day)}</span>' if peak_day else ''}
                    <span>投稿数: {esc(profile.get("archive_count", 0))}</span>
                    {f'<span>动态获赞: {dyn_total_likes:,}</span>' if dyn_total_likes else ''}
                    {f'<span>采集时间: {esc(collected_at)}</span>' if collected_at else ''}
                </div>
            </div>
            {live_section}

            {f'''<div class="section"><h4>📁 收藏夹 ({esc(fav.get("folder_count",0))}个)</h4><div class="samples">{''.join(f'<span class="sample">{esc(n)}</span>' for n in fav_names)}</div></div>''' if fav_names else ''}

            {f'''<div class="section"><h4>🎬 最近投稿</h4><ul class="list">{''.join(f'<li><a href="https://www.bilibili.com/video/{esc(v.get("bvid",""))}" target="_blank" rel="noopener">{esc(v.get("title",""))}</a></li>' for v in vid_recent)}</ul></div>''' if vid_recent else ''}

            {fol_section}

            {f'''<div class="section"><h4>📺 追番/追剧</h4><div class="samples">{''.join(f'<span class="sample">{esc(b)}</span>' for b in bangumi + dramas)}</div></div>''' if bangumi or dramas else ''}

{ai_section}
        </div>
    </div>
    '''


def generate_summary_stats(profiles: list[dict]) -> dict:
    """生成汇总统计数据"""
    levels = Counter()
    vip_count = 0
    spam_levels = Counter()
    tags = Counter()

    for p in profiles:
        levels[p.get("level", 0)] += 1
        if p.get("vip_status") == 1:
            vip_count += 1
        spam_levels[p.get("danmaku", {}).get("spam_level", "低")] += 1
        for t in p.get("tags", []):
            tags[t] += 1

    return {
        "total": len(profiles),
        "levels": dict(levels),
        "vip_count": vip_count,
        "spam_levels": dict(spam_levels),
        "top_tags": dict(tags.most_common(15)),
    }


def sort_profiles_by_risk(profiles: list[dict]) -> list[dict]:
    """用户卡片排序：AI 深掘画像优先（有 ai_deep/ai_analysis 的排最前）；
    再按风险等级 高→中→低；同级按兴趣分（刷屏分/问题弹幕严重度/弹幕数）降序"""
    risk_rank = {"高": 0, "中": 1, "低": 2}

    def _num(p: dict, section: str, key: str) -> float:
        """排序键数值防御：旧画像 JSON 里 section 可能整体为 null、数值键也可能为
        null（senders.spam_score 可空）——直接 .get().get() 会 -None 抛 TypeError
        炸整页，统一按 0 参与排序（与 generate_user_card 的 None 防御同口径）"""
        v = (p.get(section) or {}).get(key)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    return sorted(profiles, key=lambda p: (
        # AI 深掘画像置顶：深掘内容是报告价值最高的部分，应最先看到
        0 if (p.get("ai_deep") or p.get("ai_analysis")) else 1,
        risk_rank.get((p.get("danmaku") or {}).get("spam_level", "低"), 2),
        -_num(p, "danmaku", "spam_score"),
        -_num(p, "cringe", "max_severity"),
        # 问题评论直引作者（P0-a）：无弹幕问题记录时按问题评论严重度/命中数参与排序
        -_num(p, "comment_problem", "max_severity"),
        -_num(p, "comment_problem", "hits"),
        -_num(p, "danmaku", "count"),
    ))


def generate_chart_data(profiles: list[dict]) -> dict:
    """概览标签页四个图表的数据（等级/刷屏风险/标签/地域分布 Top10+其他）"""
    stats = generate_summary_stats(profiles)
    data = {
        "level_labels": [f"Lv.{i}" for i in range(7)],
        "level_data": [stats["levels"].get(i, 0) for i in range(7)],
        "spam_labels": ["低", "中", "高"],
        "spam_data": [stats["spam_levels"].get(l, 0) for l in ["低", "中", "高"]],
        "tag_labels": list(stats["top_tags"].keys())[:10],
        "tag_data": list(stats["top_tags"].values())[:10],
    }
    # 地域分布：从 ip_location（格式 "IP属地：江苏"）提取省份，Top10 + 其他；
    # 注意无评论属地的用户该键存在但值为 None，不能用 p.get("ip_location", "")
    region_counts = Counter()
    for p in profiles:
        loc = p.get("ip_location") or ""
        if loc.startswith("IP属地："):
            province = loc[len("IP属地："):].strip()
            if province:
                region_counts[province] += 1
    region_top = region_counts.most_common(10)
    region_labels = [k for k, _ in region_top]
    region_data = [v for _, v in region_top]
    other_count = sum(region_counts.values()) - sum(region_data)
    if other_count > 0:
        region_labels.append("其他")
        region_data.append(other_count)
    data["region_labels"] = region_labels
    data["region_data"] = region_data
    return data


def generate_cringe_board(profiles: list[dict], fp_renderer=None) -> str:
    """问题弹幕榜：按发送者聚合（最高严重度、条数降序），无命中时返回空串。

    fp_renderer: 可选回调 (弹幕内容) -> str，渲染每条代表原文的误报标记按钮（P2-a）。"""
    cringe_entries = [p for p in profiles if p.get("cringe", {}).get("count", 0) >= 1]
    cringe_entries.sort(key=lambda p: (p["cringe"].get("max_severity", 0), p["cringe"]["count"]),
                        reverse=True)
    if not cringe_entries:
        return ""
    rows = []
    for p in cringe_entries:
        cr = p["cringe"]
        example = (cr.get("examples") or [{}])[0]
        fp_html = fp_renderer(example.get("content", "")) if fp_renderer else ""
        rows.append(
            f'<tr><td><a href="https://space.bilibili.com/{esc(p.get("uid", 0))}" target="_blank" rel="noopener">{esc(p.get("name", "未知"))}</a></td>'
            f'<td>{esc(cr["count"])}</td>'
            f'<td>{_category_chips(cr.get("categories", []))}</td>'
            f'<td>{esc(cr.get("max_severity", 0))}</td>'
            f'<td>{esc(example.get("content", ""))} {fp_html}<br>'
            f'<span class="cringe-reason">{esc(example.get("category", ""))}: {esc(example.get("reason", ""))}</span></td></tr>'
        )
    return f'''
    <div class="cringe-board">
        <h3>🚨 问题弹幕榜（{len(cringe_entries)} 人命中）</h3>
        <table>
            <thead><tr><th>用户</th><th>命中条数</th><th>类别</th><th>最高严重度</th><th>代表原文</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>'''


def up_wordcloud_data(profiles: list[dict]) -> dict:
    """收集所有UP主词云数据：{up_{uid}_{i}: [[词, 权重], ...]}，供 up-chip 悬停词云弹窗"""
    result = {}
    for p in profiles:
        uid = p.get("uid", 0)
        fol = p.get("following_summary", {})
        for i, up in enumerate(fol.get("up_details", [])):
            wf = up.get("word_freq", [])
            if wf:
                result[f"up_{uid}_{i}"] = wf
    return result
