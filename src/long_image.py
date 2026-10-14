"""
分享长图渲染（Pillow 服务端出图）

把报告页「概览」里最值得传播的内容渲染成一张竖版 PNG：封面头 → 数据总览
→ 问题弹幕类别分布 → 问题弹幕榜 → 争执焦点双榜 → 问题评论榜 → 群体复读事件
→ 用户画像卡 → 数据边界页脚。

设计取舍（为什么不用前端 html2canvas / 无头浏览器截图）：
- 报告页是交互式多标签页 + 前端分页，截当前视图只能拿到局部，拿不到全量榜单；
  服务端从库里取数重绘才能拼出一张信息完整、版面可控的图。
- 本项目零构建、无 Node 工具链，也不愿为一次出图引入 headless Chromium（数百 MB 依赖）。
  Pillow 已是既有依赖（qrcode 的图片后端），CJK 字体系统自带，零新增依赖即可出图。

纯渲染：所有数据由调用方（web.py / main.py）以 payload 字典传入，本模块不查库、
不发网络请求，便于离线回归测试。字体缺失/数据不全时降级而非崩溃。
"""
import os
import re
import time

from PIL import Image, ImageDraw, ImageFont

from report import PROBLEM_CATEGORY_COLORS as CATEGORY_COLORS, mask_reply_prefix
from config import (LONG_IMAGE_WIDTH, LONG_IMAGE_MIN_WIDTH, LONG_IMAGE_MAX_PIXELS,
                    LONG_IMAGE_FONT, LONG_IMAGE_SCALE, LONG_IMAGE_CRADLE_TOP_N,
                    LONG_IMAGE_ATTACK_TOP_N, LONG_IMAGE_COMMENT_TOP_N,
                    LONG_IMAGE_REPEAT_TOP_N, LONG_IMAGE_USER_TOP_N,
                    LONG_IMAGE_QUOTE_MAX_LINES)

# ========== 配色（沿用 report.py 的 B站蓝粉体系，与网页观感一致） ==========
BG = "#eef1f6"          # 页面底色
CARD = "#ffffff"        # 卡片底
INK = "#1f2430"         # 主文字
INK_SOFT = "#5b6474"    # 次级文字
INK_FAINT = "#98a1b0"   # 弱化文字（注释、时间戳）
BRAND = "#00a1d6"       # B站蓝
BRAND2 = "#fb7299"      # B站粉
HEADER_TOP = "#00a1d6"
HEADER_BOTTOM = "#fb7299"
DIVIDER = "#e6eaf0"
SPAM_COLORS = {"高": "#e0393e", "中": "#f5a623", "低": "#3ec6a6"}
CATEGORY_FALLBACK = "#8b93a1"


def _spam_color(level: str) -> str:
    return SPAM_COLORS.get(level or "低", CATEGORY_FALLBACK)
# ========== 字体 ==========
# 只探测带 CJK 字形的字体：渲染的是中文标题+原文，DejaVu 之类纯拉丁字体
# 会把整段中文画成豆腐块，比不出图更糟。覆盖 Linux/macOS/Windows 常见路径。
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",              # macOS
    "/System/Library/Fonts/Hiragino Sans GB.ttc",       # macOS
    "C:/Windows/Fonts/msyh.ttc",                        # Windows 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",                       # Windows 黑体
)

# 探测结果全局缓存：一次进程内反复出图不重复扫盘
_FONT_CACHE: dict = {}
def _find_font() -> tuple:
    """定位系统 CJK 字体，返回 (字体路径, 字体集合索引)。

    Noto Sans CJK 是 .ttc 字体集合，索引 0 是日文字形（部分汉字写法与简中不同），
    优先取索引 2（SC 简体），不可用时退回 0。用户可在 config.LONG_IMAGE_FONT
    指定任意 ttf/otf/ttc。全都找不到时返回 (None, 0) 由调用方降级为出图失败。
    """
    if "font" in _FONT_CACHE:
        return _FONT_CACHE["font"]

    explicit = (LONG_IMAGE_FONT or "").strip()
    if explicit and not os.path.isfile(explicit):
        raise FileNotFoundError(f"LONG_IMAGE_FONT 指向的字体不存在: {explicit}")

    candidates = [explicit] if explicit else list(_FONT_CANDIDATES)
    found = (None, 0)
    for path in candidates:
        if not os.path.isfile(path):
            continue
        for index in ((2, 0) if path.lower().endswith(".ttc") else (0,)):
            try:
                ImageFont.truetype(path, 20, index=index)
            except Exception:
                continue
            found = (path, index)
            break
        if found[0]:
            break

    _FONT_CACHE["font"] = found
    return found
class FontBook:
    """按字号缓存字体实例，避免每次出图重复解析 ttc 字体集合。"""

    def __init__(self):
        path, index = _find_font()
        if not path:
            raise RuntimeError(
                "未找到可用的中文字体，长图无法生成。请安装 fonts-noto-cjk"
                "（apt install fonts-noto-cjk），或在 config.LONG_IMAGE_FONT 指定字体路径")
        self._path, self._index = path, index
        self._cache: dict = {}

    def get(self, size: int):
        """取指定字号的字体实例；解析失败时退回 Pillow 内置字体（不崩）。"""
        if size not in self._cache:
            try:
                self._cache[size] = ImageFont.truetype(self._path, size, index=self._index)
            except Exception:
                self._cache[size] = ImageFont.load_default()
        return self._cache[size]


# ========== 文本折行 ==========
# 中文没有词边界，按字符累计宽度折行；连续空白（含换行、全角空格）先折叠，
# 避免弹幕原文里的换行/制表符在图上留出大片空白
_WS_RE = re.compile(r"[\s\u3000]+")


def _clean(text) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()
def _line_height(font) -> int:
    """行高取字体自身 ascent+descent，比固定倍数更贴合不同字号的实际观感。"""
    try:
        ascent, descent = font.getmetrics()
        return max(1, int(ascent + descent))
    except Exception:
        return max(1, int(getattr(font, "size", 16) * 1.45))


def _ellipsize(draw, text: str, font, max_w: int) -> str:
    """给超长行尾补省略号：逐字回退直到省略号本身能放下。"""
    tail = "…"
    if draw.textlength(text + tail, font=font) <= max_w:
        return text + tail
    while text and draw.textlength(text + tail, font=font) > max_w:
        text = text[:-1]
    return (text + tail) if text else tail


def _fit(draw, text: str, font, max_w: int) -> str:
    """不折行的场合（数字、徽标标签）用：单行放不下就截断加省略号。"""
    text = _clean(text)
    if draw.textlength(text, font=font) <= max_w:
        return text
    return _ellipsize(draw, text, font, max_w)
def _wrap(draw, text: str, font, max_w: int,
          max_lines: int = LONG_IMAGE_QUOTE_MAX_LINES) -> list:
    """按像素宽度把文本折成若干行；超出 max_lines 时末行截断加省略号。

    max_w 传 <= 0 或 max_lines < 1 时返回空列表（调用方据此跳过绘制，
    不做「画到画布外」这类防御——版面尺寸由 _layout 统一保证为正）。
    """
    if max_w <= 0 or max_lines < 1:
        return []
    text = _clean(text)
    if not text:
        return []
    if draw.textlength(text, font=font) <= max_w:
        return [text]
    lines, cur = [], ""
    for ch in text:
        if cur and draw.textlength(cur + ch, font=font) > max_w:
            lines.append(cur)
            cur = ch
            if len(lines) >= max_lines:
                break
        else:
            cur += ch
    if len(lines) >= max_lines:
        # 还有被丢弃的剩余内容：末行补省略号，明示「原文更长」
        lines[-1] = _ellipsize(draw, lines[-1], font, max_w)
        return lines
    if cur:
        lines.append(cur)
    return lines
# ========== 数值/时间格式化 ==========
def _fmt_count(n) -> str:
    """万/亿计数：长图上「1291632」不如「129.2万」好读。"""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def _fmt_duration(sec) -> str:
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return "—"
    if sec <= 0:
        return "—"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_ts(ts) -> str:
    """时间戳 → YYYY-MM-DD；缺失/非法返回破折号而非1970。"""
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        return "—"
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts > 0 else "—"


def _fmt_video_time(sec) -> str:
    """视频内时间轴 → mm:ss / h:mm:ss（复读事件、弹幕时间戳用）。"""
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return "—"
    h, rem = divmod(max(0, sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ========== 绘制层 ==========
class _NullDraw:
    """测量阶段的空绘制器：吞掉所有几何/文本绘制调用。

    长图总高事先未知（取决于各区块折行后的行数），故先空跑一遍量出总高，
    再按总高建画布重画。空跑时区块代码无需写 if 分支，绘制调用照常发出。
    """

    def __getattr__(self, name):
        return lambda *a, **k: None


class Painter:
    """逻辑坐标绘制器：区块代码一律用逻辑像素（长图基准宽 1080）书写，
    由本类统一乘超采样系数，保证出图锐利且版面代码与倍率解耦。"""

    def __init__(self, width: int, fonts: FontBook, scale: int = 1, draw=None):
        self.width = width
        self.fonts = fonts
        self.scale = max(1, int(scale))
        self.draw = draw if draw is not None else _NullDraw()
        # 度量始终用真实字体的 textlength（1x1 小画布），与绘制模式无关
        self._measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        # painting=False 即量高阶段。区块代码据此决定「先铺底再压字」的绘制顺序，
        # 因为渐变底必须先于文字落笔，而文字高度只有折行后才确定（见 _block_header）
        self.painting = not isinstance(self.draw, _NullDraw)

    # -- 度量（任何模式下都走真实字体） --
    def font(self, size: int):
        return self.fonts.get(max(1, int(round(size * self.scale))))

    def text_w(self, text: str, size: int) -> float:
        """文本宽（逻辑像素），折行/截断的唯一度量来源。"""
        return self._measure.textlength(_clean(text), font=self.font(size)) / self.scale

    def line_h(self, size: int) -> float:
        return _line_height(self.font(size)) / self.scale

    def _s(self, v) -> float:
        """逻辑像素 → 设备像素。"""
        return float(v) * self.scale
    # -- 几何 --
    def rect(self, x, y, w, h, fill=None, radius=0, outline=None, width=1):
        box = [self._s(x), self._s(y), self._s(x + w), self._s(y + h)]
        if radius:
            self.draw.rounded_rectangle(box, radius=self._s(radius), fill=fill,
                                        outline=outline, width=max(1, self._s(width)))
        else:
            self.draw.rectangle(box, fill=fill, outline=outline,
                               width=max(1, self._s(width)))

    def gradient(self, x, y, w, h, c1, c2):
        """竖向线性渐变：逐行描一条横线（无 numpy 依赖，1080 宽仅千余行）。"""
        n = max(1, int(self._s(h)))
        r1, g1, b1 = _rgb(c1)
        r2, g2, b2 = _rgb(c2)
        for i in range(n):
            t = i / max(1, n - 1)
            col = (int(r1 + (r2 - r1) * t), int(g1 + (g2 - g1) * t), int(b1 + (b2 - b1) * t))
            yy = self._s(y) + i
            self.draw.line([self._s(x), yy, self._s(x + w), yy], fill=col)

    def line(self, x, y, w, color, weight=1):
        self.draw.rectangle([self._s(x), self._s(y), self._s(x + w), self._s(y + weight)],
                             fill=color)

    def hairline(self, x, y, w, color=DIVIDER):
        self.line(x, y, w, color, 1)
    # -- 文本 --
    def lines(self, s, size, max_w, max_lines=1) -> list:
        """按逻辑宽度 max_w 折行（内部换算到设备像素后交给 _wrap）。"""
        return _wrap(self._measure, s, self.font(size), self._s(max_w), max_lines)

    def text(self, x, y, s, size, color, max_lines=1, max_w=None):
        """折行文本块；返回实际占用高度（逻辑像素），供调用方顺次排版。"""
        f = self.font(size)
        lh = self.line_h(size)
        lines = self.lines(s, size, max_w if max_w else self.width, max_lines)
        for k, line in enumerate(lines):
            self.draw.text((self._s(x), self._s(y) + k * self._s(lh)), line, font=f, fill=color)
        return len(lines) * lh

    def chip(self, x, y, text, size, fg="#ffffff", bg="#8b93a1",
             pad_x=9, radius=6) -> float:
        """圆角色块标签（问题类别徽标等）；返回占用宽度，供同行连排。"""
        f = self.font(size)
        label = _clean(text)
        tw = self._measure.textlength(label, font=f) / self.scale
        h = self.line_h(size) + 4
        w = tw + pad_x * 2
        self.rect(x, y, w, h, fill=bg, radius=radius or h / 2)
        self.draw.text((self._s(x + pad_x), self._s(y + 2)), label, font=f, fill=fg)
        return w
def _rgb(color: str) -> tuple:
    """#rrggbb → (r,g,b)；已是 RGB 元组则原样返回。"""
    if isinstance(color, (tuple, list)):
        return tuple(int(c) for c in color[:3])
    c = str(color or "").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        return (0, 0, 0)
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def _section_title(p: Painter, x, y, w, title, hint="") -> float:
    """区块标题：左侧品牌色竖条 + 标题 + 右侧灰色说明；返回占用高度。"""
    size = 25
    bar_w, bar_h = 5, p.line_h(size) - 4
    p.rect(x, y, bar_w, bar_h, fill=BRAND, radius=2)
    ty = y + (bar_h - p.line_h(size)) / 2
    used = p.text(x + bar_w + 10, ty, title, size, INK, max_w=w - bar_w - 10)
    if hint:
        fs = 17
        hw = min(p.text_w(hint, fs), w * 0.62)
        p.draw.text((p._s(x + w - hw), p._s(ty + (p.line_h(size) - p.line_h(fs)) / 2)),
                    _fit(p._measure, hint, p.font(fs), p._s(hw)), font=p.font(fs), fill=INK_FAINT)
    return max(used, bar_h)
def _block_header(p: Painter, x, y, w, data: dict) -> float:
    """封面头：蓝粉渐变底 + 视频标题 + UP/时长/日期 + 关键数字条。

    渐变底必须先于文字落笔，而文字高度依赖折行、只有走完折行才知道整块多高，
    故先算版式高度，再画底，最后压字。
    """
    pad = 34
    inner_w = w - pad * 2

    title_lines = p.lines(_clean(data.get("title") or "未知视频"), 40, inner_w, 4)
    t_lh = p.line_h(40)
    h_title = len(title_lines) * t_lh

    meta = " · ".join(v for v in (_clean(data.get("up_name")),
                                   _fmt_duration(data.get("duration")),
                                   _fmt_ts(data.get("pubdate"))) if v and v != "—")
    h_meta = p.line_h(21) if meta else 0

    # 数字条：数值有意义才展示（缺项不占位），全缺时回退到仅弹幕数
    raw = [("弹幕", data.get("danmaku_count")), ("播放", data.get("view_count")),
           ("评论", data.get("reply_count")), ("分析用户", data.get("user_count"))]
    stats = [(k, _fmt_count(v)) for k, v in raw if v]
    if not stats:
        stats = [("弹幕", "0")]
    gap, ch = 12, 76
    total = pad + h_title + 14 + h_meta + 18 + ch + pad

    if p.painting:
        p.gradient(x, y, w, total, HEADER_TOP, HEADER_BOTTOM)
        cy = y + pad
        for line in title_lines:
            p.draw.text((p._s(x + pad), p._s(cy)), line, font=p.font(40), fill="#ffffff")
            cy += t_lh
        cy += 14
        if meta:
            p.text(x + pad, cy, meta, 21, "#dff0fa", max_w=inner_w)
            cy += h_meta
        cy += 18
        cw = (inner_w - gap * (len(stats) - 1)) / len(stats)
        for i, (k, v) in enumerate(stats):
            bx = x + pad + i * (cw + gap)
            p.rect(bx, cy, cw, ch, fill="#ffffff", radius=12)
            p.text(bx + 14, cy + 12, v, 27, INK, max_w=cw - 28)
            p.text(bx + 14, cy + 47, k, 17, INK_FAINT, max_w=cw - 28)
    return total
def _block_overview(p: Painter, x, y, w, data: dict) -> float:
    """数据总览：3×2 指标卡（分析用户/高风险刷屏/问题弹幕/问题评论/复读事件/低置信度）。"""
    items = [
        ("分析用户", _fmt_count(data.get("user_count")), BRAND),
        ("高风险刷屏", _fmt_count(data.get("spam_high")), "#e0393e"),
        ("问题弹幕", _fmt_count(data.get("cringe_count")), "#c0392b"),
        ("问题评论", _fmt_count(data.get("comment_problem")), "#8e44ad"),
        ("复读事件", _fmt_count(data.get("repeat_events")), "#f39c12"),
        ("低置信度", _fmt_count(data.get("low_conf")), INK_FAINT),
    ]
    h = _section_title(p, x, y, w, "数据总览", "统计口径见页脚") + 20
    gap = 14
    cols = 3
    cw = (w - gap * (cols - 1)) / cols
    ch = 92
    for i, (k, v, color) in enumerate(items):
        bx = x + (i % cols) * (cw + gap)
        by = y + h + (i // cols) * (ch + gap)
        p.rect(bx, by, cw, ch, fill=CARD, radius=12)
        p.text(bx + 16, by + 14, v, 30, color, max_w=cw - 32)
        p.text(bx + 16, by + 54, k, 18, INK_SOFT, max_w=cw - 32)
    rows = (len(items) + cols - 1) // cols
    return h + rows * ch + (rows - 1) * gap
def _block_categories(p: Painter, x, y, w, cats: list) -> float:
    """问题弹幕类别分布：横向条形图（复用 report.PROBLEM_CATEGORY_COLORS 配色）。"""
    if not cats:
        return 0.0
    h = _section_title(p, x, y, w, "问题弹幕类别分布", f"共 {sum(c for _, c in cats)} 人命中") + 20
    bar_h = 26
    gap = 14
    label_w = 96
    val_w = 58
    track_w = w - label_w - val_w - 16
    mx = max(c for _, c in cats) or 1
    for i, (name, cnt) in enumerate(cats):
        by = y + h + i * (bar_h + gap)
        p.text(x, by + (bar_h - p.line_h(19)) / 2, name, 19, INK, max_w=label_w - 10)
        p.rect(x + label_w, by, track_w, bar_h, fill="#eef1f5", radius=bar_h / 2)
        bw = max(bar_h, track_w * cnt / mx)
        color = CATEGORY_COLORS.get(name, CATEGORY_FALLBACK)
        p.rect(x + label_w, by, bw, bar_h, fill=color, radius=bar_h / 2)
        p.draw.text((p._s(x + w - val_w), p._s(by + (bar_h - p.line_h(18)) / 2)),
                    str(cnt), font=p.font(18), fill=INK_SOFT)
    return h + len(cats) * (bar_h + gap) - gap
def _entry(p: Painter, x, y, w, e: dict) -> float:
    """榜单条目：序号 + 昵称 + 类别色标 + 右侧计数 + 代表原文（折行截断）。

    e 结构：{rank, name, cats, right, quote}。
    rank 从 1 起；right 为右侧计数文本（如「12 条」）。
    """
    pad = 16
    iw = w - pad * 2
    rank = int(e.get("rank") or 0)

    name = _clean(e.get("name") or "未知")
    right = _clean(e.get("right") or "")
    rw = 34
    right_w = p.text_w(right, 19) if right else 0.0
    name_f = p.font(22)
    name_max = iw - rw - right_w - 24
    name = _fit(p._measure, name, name_f, p._s(max(10, name_max)))
    name_h = p.line_h(22)

    cats = [c for c in (e.get("cats") or []) if c]
    quote = _clean(e.get("quote") or "")
    quote_lines = p.lines(quote, 19, iw, 2) if quote else []
    quote_h = len(quote_lines) * p.line_h(19) if quote_lines else 0.0
    cat_h = (p.line_h(17) + 4) if cats else 0.0

    total = pad + name_h + (4 + cat_h if cats else 0) + (8 + quote_h if quote_lines else 0) + pad

    if p.painting:
        cy = y + pad
        # 序号：前 3 名用品牌色实心圆，其余浅灰数字，视觉上突出 TOP 3
        if rank == 1:
            p.rect(x + pad, cy + 1, 26, 26, fill=BRAND2, radius=13)
            p.draw.text((p._s(x + pad + 13), p._s(cy + 4)), str(rank),
                        font=p.font(19), fill="#ffffff", anchor="ma")
        else:
            p.draw.text((p._s(x + pad + 13), p._s(cy + 4)), str(rank),
                        font=p.font(19), fill=INK_FAINT, anchor="ma")
        p.draw.text((p._s(x + pad + rw), p._s(cy)), name, font=name_f, fill=INK)
        if right:
            p.draw.text((p._s(x + w - pad), p._s(cy + 3)), right,
                        font=p.font(19), fill=INK_SOFT, anchor="ra")
        cy += name_h + 4
        if cats:
            cx = x + pad
            for c in cats:
                cw = p.chip(cx, cy, c, 17, bg=CATEGORY_COLORS.get(c, CATEGORY_FALLBACK))
                if cx + cw > x + w - pad:
                    break
                cx += cw + 6
            cy += cat_h
        if quote_lines:
            cy += 8
            lh = p.line_h(19)
            for k, line in enumerate(quote_lines):
                p.draw.text((p._s(x + pad), p._s(cy + k * lh)), line,
                            font=p.font(19), fill=INK_SOFT)
    return total
def _card_title_h(p: Painter) -> float:
    """卡片内标题行的高度（标题 25px 与右侧灰色说明取行高较大者）。"""
    return max(p.line_h(25), p.line_h(17))


def _draw_card_title(p: Painter, x, y, w, title, hint=""):
    """卡片内标题：品牌色竖条 + 标题 + 右对齐灰色说明。"""
    h = _card_title_h(p)
    p.rect(x, y + (h - p.line_h(25)) / 2 + 2, 5, p.line_h(25) - 4, fill=BRAND, radius=2)
    p.draw.text((p._s(x + 15), p._s(y + (h - p.line_h(25)) / 2)), title,
                font=p.font(25), fill=INK)
    if hint:
        hw = min(p.text_w(hint, 17), w * 0.6)
        p.draw.text((p._s(x + w), p._s(y + (h - p.line_h(17)) / 2)),
                    _fit(p._measure, hint, p.font(17), p._s(hw)),
                    font=p.font(17), fill=INK_FAINT, anchor="ra")


def _board(p: Painter, x, y, w, spec: tuple) -> float:
    """榜单区块外壳：白卡 + 标题 + 条目列表；entries 为空时整块不渲染（返回 0）。

    spec = (标题, 右侧灰色说明, 条目列表)。参数打包成元组是为了让所有区块
    统一走 fn(painter, x, y, w, arg) 一种调用签名。
    """
    title, hint, entries = spec
    """榜单区块外壳：白卡 + 标题 + 条目列表；entries 为空时整块不渲染（返回 0）。"""
    if not entries:
        return 0.0
    pad = 22
    iw = w - pad * 2
    th = _card_title_h(p)
    gap = 16
    heights = [_entry(p, x + pad, y + pad + th + gap, iw, e) for e in entries]
    total = pad * 2 + th + gap + sum(heights)

    if p.painting:
        p.rect(x, y, w, total, fill=CARD, radius=14)
        _draw_card_title(p, x + pad, y + pad, iw, title, hint)
        cy = y + pad + th + gap
        for k, e in enumerate(entries):
            if k:
                p.hairline(x + pad, cy, iw)
            _entry(p, x + pad, cy, iw, e)
            cy += heights[k]
    return total
def _block_attack(p: Painter, x, y, w, spec: tuple) -> float:
    """争执焦点：挑事者 / 被围攻者左右双栏（原网页是关系图，长图里改成对照两列）。

    spec = (挑事者列表, 被围攻者列表)。
    """
    attackers, victims = spec
    if not attackers and not victims:
        return 0.0
    pad = 22
    iw = w - pad * 2
    th = _card_title_h(p)
    gap = 16
    col_gap = 18
    cw = (iw - col_gap) / 2

    def _col_h(cols):
        if not cols:
            return 0.0
        return sum(_entry(p, x, y, cw, e) for e in cols)

    # 列头（「挑事者」「被围攻者」）也占一行，须计入卡高，否则条目会溢出白卡
    head_h = p.line_h(20) + 8
    ah, vh = _col_h(attackers), _col_h(victims)
    body_h = max(ah, vh)
    total = pad * 2 + th + gap + head_h + body_h

    if p.painting:
        p.rect(x, y, w, total, fill=CARD, radius=14)
        _draw_card_title(p, x + pad, y + pad, iw, "争执焦点",
                         f"挑事 {len(attackers)} 人 / 被围攻 {len(victims)} 人")
        cy = y + pad + th + gap
        p.text(x + pad, cy, "挑事者", 20, "#c0392b", max_w=cw)
        p.text(x + pad + cw + col_gap, cy, "被围攻者", 20, "#2471a3", max_w=cw)
        cy += head_h
        p.hairline(x + pad, cy - 4, iw)
        for col_x, cols in ((x + pad, attackers), (x + pad + cw + col_gap, victims)):
            ty = cy
            for k, e in enumerate(cols):
                if k:
                    p.hairline(col_x, ty, cw)
                ty += _entry(p, col_x, ty, cw, e)
    return total
def _block_repeat(p: Painter, x, y, w, events: list) -> float:
    """群体复读事件：内容大字 + 元信息（分P 时刻/人数/条数/变体数）。"""
    if not events:
        return 0.0
    pad = 22
    iw = w - pad * 2
    th = _card_title_h(p)
    gap = 16
    row_gap = 16

    rows = []
    for e in events:
        content = _clean(e.get("content") or "")
        lines = p.lines(content, 24, iw, 1) if content else [""]
        meta = []
        v = e.get("video") or {}
        if v:
            page = int(v.get("page") or 1)
            meta.append(f"P{page} {_fmt_video_time(v.get('start'))}")
        if e.get("sender_count"):
            meta.append(f"{e['sender_count']} 人")
        if e.get("total"):
            meta.append(f"{e['total']} 条")
        variants = int(e.get("variant_count") or 1)
        meta.append(f"＋{variants - 1} 变体" if variants > 1 else "单一写法")
        rows.append((lines, p.line_h(24), " · ".join(meta), p.line_h(19)))

    body_h = sum(max(c, mh) + row_gap for _, c, _, mh in rows) - row_gap
    total = pad * 2 + th + gap + body_h

    if p.painting:
        p.rect(x, y, w, total, fill=CARD, radius=14)
        _draw_card_title(p, x + pad, y + pad, iw, "群体复读事件", "60s 窗内 ≥5 人 ≥8 条")
        cy = y + pad + th + gap
        for lines, content_h, meta, meta_h in rows:
            p.draw.text((p._s(x + pad), p._s(cy)), lines[0], font=p.font(24), fill=INK)
            p.draw.text((p._s(x + pad), p._s(cy + content_h + 2)),
                        _fit(p._measure, meta, p.font(19), p._s(iw)),
                        font=p.font(19), fill=INK_FAINT)
            cy += max(content_h, meta_h) + row_gap
    return total
def _block_footer(p: Painter, x, y, w, spec: tuple) -> float:
    """页脚：数据边界说明（必须写清分析口径，避免长图脱离网页后被误读）+ 品牌行。

    spec = (说明段落列表, 品牌行文本)。
    """
    notes, brand = spec
    pad = 26
    iw = w - pad * 2
    texts = [_clean(n) for n in notes if _clean(n)]
    line_h = p.line_h(18)
    heights = [len(p.lines(t, 18, iw, 4)) * line_h for t in texts]
    brand_h = p.line_h(19)
    total = pad + sum(heights) + (14 if heights else 0) + 18 + brand_h + pad

    if p.painting:
        p.rect(x, y, w, total, fill="#e3e8ef", radius=14)
        cy = y + pad
        for t, th in zip(texts, heights):
            p.text(x + pad, cy, t, 18, INK_SOFT, max_lines=4, max_w=iw)
            cy += th
        if texts:
            cy += 14
        p.hairline(x + pad, cy, iw, color="#cfd6e0")
        cy += 18
        p.text(x + pad, cy, brand, 19, INK_FAINT, max_w=iw)
    return total
def build_payload(bvid: str, video_info: dict, profiles: list, stats: dict,
                  chart: dict, cringe_entries: list, attack: dict,
                  comments: list, repeat_events: list, low_conf: int,
                  repeat_pool: dict | None = None, *,
                  mask: bool = False, mask_name=None,
                  comment_total: int | None = None) -> dict:
    """把各阶段已装配好的数据整成渲染 payload。

    调用方（web.py）负责查库与误报扣除，本函数只做取字段/排序/截断，
    不再触碰数据源，便于离线测试直接喂字典。
    mask/mask_name：遮蔽楼中楼正文「回复 @昵称」前缀（与昵称同一规则）。
    comment_total：扣除误报后的问题评论总数；榜单仍只画前 N 条。
    不传则沿用 comments 的长度（调用方未截断时与总数相同）。
    """
    stat = video_info.get("stat") or {}
    owner = video_info.get("owner") or {}
    danmaku = profiles[0].get("danmaku") if profiles else {}

    def _vis(text) -> str:
        """原文上图前：先遮蔽回复前缀里的昵称，再折叠空白。"""
        return _clean(mask_reply_prefix(text or "", bool(mask), mask_name))

    spam_high = sum(1 for p in profiles
                    if (p.get("danmaku") or {}).get("spam_level") == "高")
    cringe_count = sum(1 for p in profiles
                      if (p.get("cringe") or {}).get("count") or 0)

    # 问题弹幕榜条目：按严重度+条数降序，与网页榜单同口径
    ce = []
    for p in cringe_entries[:LONG_IMAGE_CRADLE_TOP_N]:
        cr = p.get("cringe") or {}
        ex = (cr.get("examples") or [{}])[0] or {}
        quote = _vis(ex.get("content") or "")
        reason = _clean(ex.get("reason") or "")
        if reason:
            quote = f"{quote}（{_clean(ex.get('category') or '')}: {reason}）" if quote else reason
        ce.append({"rank": len(ce) + 1,
                   "name": p.get("name") or "未知",
                   "cats": list(cr.get("categories") or []),
                   "right": f"{cr.get('count', 0)} 条",
                   "quote": quote})
    # 争执焦点：挑事者与被围攻者各取前 N，各带一条代表原文
    attackers = []
    for a in (attack.get("attackers") or [])[:LONG_IMAGE_ATTACK_TOP_N]:
        ex = (a.get("examples") or [{}])[0] or {}
        attackers.append({"rank": len(attackers) + 1,
                          "name": a.get("name") or "未知",
                          "cats": list(a.get("categories") or []),
                          "right": f"{a.get('count', 0)} 次",
                          "quote": _vis(ex.get("content") or "")})
    victims = []
    for v in (attack.get("victims") or [])[:LONG_IMAGE_ATTACK_TOP_N]:
        cm = list((v.get("comments") or {}).values())
        quote = ""
        if cm:
            quote = _vis(cm[0].get("parent") or "")
            atk = (cm[0].get("attacks") or [{}])[0] or {}
            attack_text = _vis(atk.get("attack") or "")
            if attack_text:
                quote = f"原评「{quote}」← {attack_text}"
        victims.append({"rank": len(victims) + 1,
                        "name": v.get("name") or "未知",
                        "cats": [],
                        "right": f"{v.get('count', 0)} 次",
                        "quote": quote})
    # 问题评论榜：已按热度降序（网页侧排好），直接取前 N
    pc = []
    for c in comments[:LONG_IMAGE_COMMENT_TOP_N]:
        heat = (c.get("like") or 0) + (c.get("reply_count") or 0) * 10
        right = f"❤{c.get('like') or 0}" + (f" ↩{c.get('reply_count') or 0}"
                                         if c.get("reply_count") else "")
        pc.append({"rank": len(pc) + 1,
                   "name": c.get("name") or "未知",
                   "cats": [c["problem"]] if c.get("problem") else [],
                   "right": right,
                   "quote": _vis(c.get("content") or ""),
                   "_heat": heat})

    # 复读事件：按发送者数降序
    rp = sorted(repeat_events or [],
                key=lambda e: -(int(e.get("sender_count") or 0)))\
        [:LONG_IMAGE_REPEAT_TOP_N]
    # 用户画像卡：沿用网页的风险排序（AI 深掘优先 → 刷屏等级 → 兴趣分）
    us = []
    for up in profiles[:LONG_IMAGE_USER_TOP_N]:
        d = up.get("danmaku") or {}
        c = up.get("cringe") or {}
        us.append({"name": up.get("name") or "未知",
                   "level": up.get("level") or 0,
                   "spam": d.get("spam_level") or "低",
                   "danmaku": d.get("count") or 0,
                   "cringe": c.get("count") or 0,
                   "tags": list(up.get("tags") or []),
                   "ai": _clean(up.get("ai_deep") or up.get("ai_analysis") or "")})

    cats = sorted(((k, v) for k, v in (chart.get("cat_counts") or {}).items()),
                  key=lambda kv: -kv[1])[:8]

    return {
        "title": video_info.get("title") or bvid,
        "bvid": bvid,
        "up_name": owner.get("name") or "",
        "pubdate": video_info.get("pubdate") or 0,
        "duration": video_info.get("duration") or danmaku.get("duration") or 0,
        "view_count": stat.get("view") or 0,
        "danmaku_count": stat.get("danmaku") or 0,
        "reply_count": stat.get("reply") or 0,
        "user_count": len(profiles),
        "spam_high": spam_high,
        "cringe_count": cringe_count,
        "comment_problem": len(comments) if comment_total is None else int(comment_total),
        "repeat_events": len(repeat_events or []),
        "low_conf": low_conf,
        "categories": cats,
        "cringe_entries": ce,
        "attackers": attackers,
        "victims": victims,
        "problem_comments": pc,
        "repeat_events_top": rp,
        "users": us,
    }
FOOTER_NOTES = [
    "数据边界：实时弹幕池仅保留最近若干条（容量由B站限定）；历史弹幕按日快照回溯，更早的弹幕不在统计范围内。",
    "已解析发送者是按兴趣分阈值入选的子集，非全部弹幕发送者；昵称/UID 可经网页报告「隐藏信息」开关遮蔽。",
    "问题弹幕与问题评论由 LLM 按二游社区语境判定，可能存在误判，报告页支持逐条标记误报。",
]
BRAND_LINE = "bilibili_profiler · B站弹幕发送者画像分析系统（报告生成于 {ts}）"


def _footer_notes(payload: dict) -> list:
    """页脚说明：固定口径说明 + 本图实际的截断情况（必须告知是「前 N」而非全量）。"""
    notes = list(FOOTER_NOTES)
    trunc = []
    if len(payload.get("cringe_entries") or []) >= LONG_IMAGE_CRADLE_TOP_N:
        trunc.append(f"问题弹幕榜仅列前 {LONG_IMAGE_CRADLE_TOP_N} 人")
    if len(payload.get("problem_comments") or []) >= LONG_IMAGE_COMMENT_TOP_N:
        trunc.append(f"问题评论榜仅列前 {LONG_IMAGE_COMMENT_TOP_N} 条")
    if len(payload.get("users") or []) >= LONG_IMAGE_USER_TOP_N:
        trunc.append(f"画像卡仅列前 {LONG_IMAGE_USER_TOP_N} 人")
    if trunc:
        notes.insert(0, "本图为精选摘要（" + "、".join(trunc) + "），完整数据见网页报告。")
    return notes
def render_long_image(payload: dict, width: int = LONG_IMAGE_WIDTH) -> Image.Image:
    """按payload 渲染长图并返回 PIL.Image（不落盘，便于调用方决定存哪）。

    两遍绘制：先用空绘制器量出整图高度，再按该高度建画布正式落笔。
    返回缩采样后的最终图（倍率 1 时即原尺寸）。
    """
    width = int(width or LONG_IMAGE_WIDTH)
    if width < LONG_IMAGE_MIN_WIDTH:
        raise ValueError(f"长图宽度过小（{width} < {LONG_IMAGE_MIN_WIDTH}），中文排版会挤成竖条")

    fonts = FontBook()
    scale = max(1, int(LONG_IMAGE_SCALE))
    pad = 30
    iw = width - pad * 2
    gap = 18

    def _blocks(p: Painter) -> list:
        """区块序列：[渲染函数, 参数]；空区块（无数据）由函数自行返回 0高度并跳过。"""
        blocks = [(_block_header, payload)]

        ov = dict(payload)
        ov.update({"spam_high": payload.get("spam_high"),
                   "low_conf": payload.get("low_conf")})
        blocks.append((_block_overview, ov))
        blocks.append((_block_categories, payload.get("categories") or []))
        blocks.append((_board, ("问题弹幕榜",
                                f"{payload.get('cringe_count') or 0} 人命中",
                                payload.get("cringe_entries") or [])))
        blocks.append((_block_attack, (payload.get("attackers") or [],
                                      payload.get("victims") or [])))
        blocks.append((_board, ("问题评论榜", "按点赞+回复数排序",
                                payload.get("problem_comments") or [])))
        blocks.append((_block_repeat, payload.get("repeat_events_top") or []))

        users = payload.get("users") or []
        if users:
            blocks.append((_block_users_wrap, users))
        blocks.append((_block_footer,
                       (_footer_notes(payload),
                        BRAND_LINE.format(ts=time.strftime("%Y-%m-%d %H:%M")))))
        return blocks
    # ---- 第一遍：量高（空绘制器） ----
    probe = Painter(width, fonts, scale)
    heights = []
    for fn, arg in _blocks(probe):
        h = fn(probe, pad, 0, iw, arg)
        heights.append(h)
    body = sum(h for h in heights if h) + gap * max(0, len([h for h in heights if h]) - 1)
    total_h = int(body + pad * 2)

    if total_h * width * scale * scale > LONG_IMAGE_MAX_PIXELS:
        raise ValueError(f"长图尺寸过大（{width}×{total_h}），请调小 LONG_IMAGE_WIDTH 或榜单 TOP_N")

    # ---- 第二遍：正式落笔 ----
    img = Image.new("RGB", (width * scale, total_h * scale), BG)
    painter = Painter(width, fonts, scale, draw=ImageDraw.Draw(img))
    cy = float(pad)
    for (fn, arg), h in zip(_blocks(painter), heights):
        if h:
            fn(painter, pad, cy, iw, arg)
            cy += h + gap

    if scale > 1:
        img = img.resize((width, total_h), Image.LANCZOS)
    return img


def _block_users_wrap(p: Painter, x, y, w, users: list) -> float:
    """用户画像区块外壳：白卡 + 标题 + 2 列卡网格。"""
    if not users:
        return 0.0
    pad = 22
    iw = w - pad * 2
    th = _card_title_h(p)
    gap = 16
    inner_gap = 14
    cols = 2
    cw = (iw - inner_gap) / cols
    rows = (len(users) + cols - 1) // cols
    card_h = _user_card_h(p, cw)
    body_h = rows * card_h + (rows - 1) * inner_gap
    total = pad * 2 + th + gap + body_h

    if p.painting:
        p.rect(x, y, w, total, fill=CARD, radius=14)
        _draw_card_title(p, x + pad, y + pad, iw, "用户画像", f"共 {len(users)} 人")
        for i, u in enumerate(users):
            bx = x + pad + (i % cols) * (cw + inner_gap)
            by = y + pad + th + gap + (i // cols) * (card_h + inner_gap)
            _user_card(p, bx, by, cw, card_h, u)
    return total
def _user_card_h(p: Painter, cw: float) -> float:
    """画像卡高度：固定结构（标题/副行/标签/AI摘句），与实际内容无关，故可一次算清。"""
    return 16 + p.line_h(21) + 6 + p.line_h(18) + 8 + (p.line_h(16) + 4) + 10 + p.line_h(17) + 16


def _user_card(p: Painter, x, y, w, h, u: dict):
    """单张画像卡：昵称+等级 / 刷屏等级徽标+条数 / 标签 / AI 定性首句。"""
    cy = y + 16
    name = _fit(p._measure, _clean(u.get("name") or "未知"), p.font(21),
                p._s(max(20, w - 32 - p.text_w(f"Lv.{u.get('level') or 0}", 17) - 14)))
    p.draw.text((p._s(x + 16), p._s(cy)), name, font=p.font(21), fill=INK)
    p.draw.text((p._s(x + w - 16), p._s(cy + 3)), f"Lv.{u.get('level') or 0}",
                font=p.font(17), fill=INK_FAINT, anchor="ra")
    cy += p.line_h(21) + 6

    spam = str(u.get("spam") or "低")
    label = f"刷屏{spam}"
    bh = p.line_h(18) + 4
    badge_w = p.text_w(label, 18) + 20
    p.rect(x + 16, cy, badge_w, bh, fill=_spam_color(spam), radius=5)
    p.draw.text((p._s(x + 16 + badge_w / 2), p._s(cy + 2)), label,
                font=p.font(18), fill="#ffffff", anchor="ma")
    meta = []
    if u.get("danmaku"):
        meta.append(f"{u['danmaku']} 条弹幕")
    if u.get("cringe"):
        meta.append(f"{u['cringe']} 条问题弹幕")
    if meta:
        p.draw.text((p._s(x + 28 + badge_w), p._s(cy + 2)), " · ".join(meta),
                    font=p.font(18), fill=INK_SOFT)
    cy += p.line_h(18) + 8

    tx = x + 16
    for t in [t for t in (u.get("tags") or []) if t][:4]:
        tw = p.chip(tx, cy, t, 16, bg="#eef4f8", fg=INK_SOFT)
        if tx + tw > x + w - 16:
            break
        tx += tw + 6
    cy += p.line_h(16) + 4 + 10

    ai = _clean(u.get("ai") or "")
    if ai:
        lines = p.lines(re.split(r"[。\n]", ai)[0], 17, w - 32, 1)
        if lines:
            p.draw.text((p._s(x + 16), p._s(cy)), lines[0], font=p.font(17), fill=BRAND)
