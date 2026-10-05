"""
画像数据导出模块

将发送者画像同步导出为 CSV（Excel 汇总查看）和 JSON（完整数据留档/二次分析）。
文件名前缀 report_{BV号}_{时间}；静态单文件 HTML 报告已移除，下载链接由 web.py 报告页提供。
"""
import csv
import json

# CSV 公式注入防护：以 = + - @ 或制表符开头的文本单元格会被 Excel/WPS 当公式执行
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t")
# 需剥离的控制字符（\x00 等；保留 \t \n \r，制表符由前置单引号兜底）
_CSV_CTRL_CHARS = frozenset(
    chr(c) for c in list(range(0x00, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20)) + [0x7F])


def _sanitize_csv_cell(value):
    """清洗 CSV 文本单元格：剥离控制字符；公式触发前缀前置英文单引号（非字符串原样返回）"""
    if not isinstance(value, str):
        return value
    s = "".join(ch for ch in value if ch not in _CSV_CTRL_CHARS)
    if s.startswith(_CSV_FORMULA_PREFIXES):
        s = "'" + s
    return s


def _flatten_profile(profile: dict) -> dict:
    """将 analyze_profile 的嵌套画像结构压平为一行 CSV 记录。

    只取常用扁平字段；嵌套字段（视频/收藏/关注明细等）不展开，完整数据走 JSON 导出。
    None 统一转为空字符串，避免 CSV 中出现字面量 None。
    """
    danmaku = profile.get("danmaku") or {}
    tags = profile.get("tags") or []

    vip = "无"
    if profile.get("vip_status") == 1:
        vip = "年度大会员" if profile.get("vip_type") == 2 else "大会员"

    row = {
        "UID": profile.get("uid"),
        "昵称": profile.get("name"),
        "性别": profile.get("sex"),
        "等级": profile.get("level"),
        "会员": vip,
        "IP属地": profile.get("ip_location"),
        "粉丝数": profile.get("follower"),
        "关注数": profile.get("following"),
        "获赞数": profile.get("like_num"),
        "硬币数": profile.get("coins"),
        "投稿数": profile.get("archive_count"),
        "弹幕数": danmaku.get("count"),
        "弹幕重复率": danmaku.get("repeat_rate"),
        "刷屏等级": danmaku.get("spam_level"),
        "刷屏原因": danmaku.get("spam_reason"),
        "碰撞风险": "是" if profile.get("collision_risk") else "否",
        "标签": "、".join(str(t) for t in tags),
        "最早活跃距今(天)": profile.get("oldest_activity_days"),
        "签名": profile.get("sign"),
    }
    return {k: ("" if v is None else v) for k, v in row.items()}


def export_csv(profiles: list[dict], path: str) -> None:
    """发送者画像汇总导出 CSV（utf-8-sig 带 BOM，便于 Excel 直接打开不乱码）

    文本单元格经 _sanitize_csv_cell 清洗：防公式注入 + 剥离控制字符。"""
    rows = [{k: _sanitize_csv_cell(v) for k, v in _flatten_profile(p).items()}
            for p in profiles]
    # 空画像列表也照常产出带表头的 CSV，字段取自一条样本行
    fieldnames = list(_flatten_profile({}).keys())

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def export_json(video_info: dict, profiles: list[dict], path: str) -> None:
    """完整画像数据导出 JSON（保留全部嵌套字段，ensure_ascii=False 保留中文）"""
    payload = {
        "video_info": video_info,
        "profiles": profiles,
    }
    with open(path, "w", encoding="utf-8") as f:
        # default=str 兜底非 JSON 原生类型（如 datetime），保证导出永不因个别字段失败
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
