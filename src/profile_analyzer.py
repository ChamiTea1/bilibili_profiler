"""
用户画像分析引擎

将原始采集数据转化为结构化画像和标签。
"""
from datetime import datetime, timedelta, timezone


# ========== 标签生成器 ==========

def tag_account_type(uid: int, level: int) -> list[str]:
    """账号类型标签（按 UID 位数粗划注册年代；档位按 2026 年口径校准，
    随时间推移整体右移——8 位 UID 已是约 7 年老号，不宜再压在"中期"）"""
    tags = []
    uid_str = str(uid)
    if len(uid_str) <= 6:
        tags.append("B站原住民")
    elif len(uid_str) <= 8:
        tags.append("早期用户")
    elif len(uid_str) <= 9:
        tags.append("中期用户")
    else:
        tags.append("新用户")

    if level >= 6:
        tags.append("硬核用户")
    elif level >= 5:
        tags.append("资深用户")
    elif level >= 4:
        tags.append("活跃用户")
    elif level <= 2:
        tags.append("轻度用户")

    return tags


def tag_consumption(vip_type: int, vip_status: int, coins: int) -> list[str]:
    """消费习惯标签"""
    tags = []
    if vip_status == 1:
        if vip_type == 2:
            tags.append("年度大会员")
        else:
            tags.append("大会员")
    if coins > 1000:
        tags.append("硬币富翁")
    elif coins > 100:
        tags.append("硬币充裕")
    return tags


def tag_identity(official_type: int, is_senior: int, archive_count: int) -> list[str]:
    """身份标签"""
    tags = []
    if official_type >= 0:
        tags.append("认证用户")
    if is_senior:
        tags.append("硬核会员")
    if archive_count > 50:
        tags.append("高产UP主")
    elif archive_count > 10:
        tags.append("活跃创作者")
    elif archive_count > 0:
        tags.append("内容创作者")
    return tags


def tag_following_type(followings: list[dict]) -> list[str]:
    """从关注列表推断兴趣圈层"""
    if not followings:
        return []

    # 基于名称关键词的粗略分类（关键词须够特异："日常"这类泛词会让"游戏日常"
    # 之类名称同时命中游戏+生活两个圈层，虚高明显）
    keywords = {
        "知识": ["罗翔", "半佛", "智能路障", "键客行", "宋浩", "李永乐", "科普"],
        "游戏": ["老番茄", "纯黑", "敖厂长", "C菌", "女流", "游戏", "电竞", "LOL", "原神", "王者"],
        "生活": ["何同学", "影视飓风", "毕导", "大祥哥", "绵羊", "美食", "旅行", "vlog", "Vlog"],
        "虚拟": ["虚拟", "Vtuber", "Vup", "嘉然", "乃琳", "贝拉", "向晚", "阿梓", "七海"],
        "鬼畜": ["鬼畜", "伊丽莎白", "泽野", "螳螂", "短裙"],
        "动画": ["番剧", "动画", "动漫", "二次元", "MAD", "AMV"],
    }

    scores = {k: 0 for k in keywords}
    for f in followings:
        name = f.get("name", "")
        for cat, words in keywords.items():
            for w in words:
                if w in name:
                    scores[cat] += 1
                    break

    tags = []
    for cat, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        if score > 0:
            if score >= 3:
                tags.append(f"{cat}深度爱好者")
            elif score >= 1:
                tags.append(f"{cat}爱好者")

    return tags


def tag_favorite_style(folders: list[dict]) -> list[str]:
    """从收藏夹命名推断性格"""
    if not folders:
        return []

    tags = []
    names = [f.get("title", "").lower() for f in folders]
    name_str = " ".join(names)

    # 拖延型
    if any(w in name_str for w in ["待看", "稍后", "有空", "todo", "later"]):
        tags.append("拖延型")

    # 强迫症型
    if any(w in name_str for w in ["归档", "已完成", "看完", "整理", "分类"]):
        tags.append("强迫症型")

    # 文艺型（双字以上词，单字"星/月/梦/云/风"会把"星穹铁道""梦幻抽卡"等
    # 游戏名误挂文艺标签）
    if any(w in name_str for w in ["诗和远方", "诗意", "远方", "月亮", "月光", "星辰",
                                   "大海", "晚风", "云海", "梦境", "浪漫"]):
        tags.append("文艺型")

    # 抽象/年轻
    emoji_count = sum(1 for n in names for c in n if ord(c) > 0x1F300)
    if emoji_count >= 3:
        tags.append("emoji大师")

    # 松鼠症（大量收藏夹或大量内容）
    total_items = sum(f.get("media_count", 0) for f in folders)
    if len(folders) >= 5 or total_items >= 500:
        tags.append("松鼠症")

    return tags


def tag_activity_pattern(activity: dict) -> list[str]:
    """活跃模式标签"""
    tags = []
    act_type = activity.get("activity_type", "未知")
    if act_type != "未知":
        tags.append(act_type)

    peak_hour = activity.get("peak_hour")
    if peak_hour is not None:
        # 与 activity_type 的粗粒度时段标签互补而非重复：同一语义只打一次
        if 0 <= peak_hour < 6:
            if "深夜党" not in tags:
                tags.append("深夜党")
        elif 22 <= peak_hour < 24:
            if "夜猫子" not in tags:
                tags.append("夜猫子")

    return tags


def tag_spam(spam_level: str) -> list[str]:
    """刷屏标签"""
    if spam_level == "高":
        return ["重度刷屏"]
    elif spam_level == "中":
        return ["中度刷屏"]
    return []


# ========== 综合分析 ==========

def analyze_profile(user_data: dict, danmaku_stats: dict, spam_stats: dict) -> dict:
    """
    综合分析用户画像
    
    Args:
        user_data: collect_user_data() 返回的完整用户数据
        danmaku_stats: 该用户在视频中的弹幕统计
        spam_stats: 刷屏检测结果
    
    Returns:
        结构化画像dict
    """
    uid = user_data.get("uid", 0)
    level = user_data.get("level", 0)

    # 基础标签
    tags = []
    tags.extend(tag_account_type(uid, level))
    tags.extend(tag_consumption(
        user_data.get("vip_type", 0),
        user_data.get("vip_status", 0),
        user_data.get("coins", 0)
    ))
    tags.extend(tag_identity(
        user_data.get("official_type", -1),
        user_data.get("is_senior_member", 0),
        user_data.get("archive_count", 0)
    ))
    tags.extend(tag_following_type(user_data.get("followings", [])))
    tags.extend(tag_favorite_style(user_data.get("favorite_folders", [])))
    tags.extend(tag_activity_pattern(user_data.get("activity_pattern", {})))
    tags.extend(tag_spam(spam_stats.get("spam_level", "低")))

    # 去重保持顺序
    seen = set()
    unique_tags = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            unique_tags.append(t)

    # 采样到的最早活跃距今（天）：first_seen 只是动态翻页（最多10页）采到的最早一条，
    # 对新用户接近注册时间，对老用户仅覆盖近期，不能当作账号年龄
    oldest_activity_days = None
    first_seen = user_data.get("first_seen", 0)
    if first_seen:
        try:
            oldest_activity_days = (datetime.now(timezone(timedelta(hours=8)))
                                     - datetime.fromtimestamp(int(first_seen), tz=timezone(timedelta(hours=8)))).days
        except (ValueError, TypeError, OSError):
            oldest_activity_days = None

    # 收藏夹分析
    folders = user_data.get("favorite_folders", [])
    fav_analysis = {
        "folder_count": len(folders),
        "total_items": sum(f.get("media_count", 0) for f in folders),
        "names": [f.get("title", "") for f in folders],
    }

    # 关注分析
    followings = user_data.get("followings", [])
    following_analysis = {
        "sample_count": len(followings),
        "vip_ratio": sum(1 for f in followings if f.get("vip_type", 0) > 0) / len(followings) if followings else 0,
        "official_ratio": sum(1 for f in followings if f.get("official_type", -1) >= 0) / len(followings) if followings else 0,
        "top_names": [f.get("name", "") for f in followings[:10]],
    }

    # 视频分析（recent 带 bvid，报告渲染为新标签页超链接）
    videos = user_data.get("videos", [])
    video_analysis = {
        "count": len(videos),
        "total_play": sum(v.get("play", 0) for v in videos),
        "avg_play": sum(v.get("play", 0) for v in videos) / len(videos) if videos else 0,
        "recent": [{"title": v.get("title", ""), "bvid": v.get("bvid", "")} for v in videos[:3]],
    }

    # 动态分析
    dynamics = user_data.get("dynamics", [])
    dynamic_analysis = {
        "count": len(dynamics),
        "total_likes": sum(d.get("like", 0) for d in dynamics),
        "recent_contents": [d.get("content", "")[:100] for d in dynamics],
    }

    # 直播分析
    live = {
        "has_room": bool(user_data.get("live_room_id", 0)),
        "is_live": user_data.get("live_status", 0) == 1,
        "room_title": user_data.get("live_title", ""),
    }

    # 追番分析
    bangumi = user_data.get("bangumi", [])
    dramas = user_data.get("dramas", [])

    return {
        "uid": uid,
        "name": user_data.get("name", "未知"),
        "face": user_data.get("face", ""),
        "sign": user_data.get("sign", ""),
        "sex": user_data.get("sex", ""),
        "school": user_data.get("school", ""),   # 毕业院校（空间信息，可能为空）
        "level": level,
        # 来自评论区 IP 属地（阶段6由 comment_location_map 注入 user_data），无属地为 None
        "ip_location": user_data.get("ip_location"),
        "vip_type": user_data.get("vip_type", 0),
        "vip_status": user_data.get("vip_status", 0),
        "official_type": user_data.get("official_type", -1),
        "follower": user_data.get("follower", 0),
        "following": user_data.get("following", 0),
        "like_num": user_data.get("like_num", 0),
        "coins": user_data.get("coins", 0),
        "archive_count": user_data.get("archive_count", 0),
        "tags": unique_tags,
        "oldest_activity_days": oldest_activity_days,
        "activity_pattern": user_data.get("activity_pattern", {}),
        "favorite": fav_analysis,
        "following_analysis": following_analysis,
        "following_summary": user_data.get("following_summary", {}),
        "all_following_names": [f.get("name", "") for f in followings],
        # 带 uid：报告页关注 chip 悬停时按需懒加载该 UP 主的投稿词云（/api/up/<uid>/wordcloud）
        "all_followings_raw": [{"uid": f.get("uid", 0), "name": f.get("name", ""),
                                "sign": f.get("sign", "")} for f in followings],
        "video": video_analysis,
        "dynamic": dynamic_analysis,
        "live": live,
        "bangumi_count": len(bangumi),
        "bangumi_titles": [b.get("title", "") for b in bangumi],
        "drama_count": len(dramas),
        "drama_titles": [d.get("title", "") for d in dramas],
        "danmaku": {
            "count": danmaku_stats.get("count", 0),
            "contents": danmaku_stats.get("contents", []),
            "video_times": danmaku_stats.get("video_times", []),
            # 逐条分P（与 contents/video_times 同下标）+ 是否多分P：报告据此把
            # mm:ss 渲染成「P{n} mm:ss」，否则多分P 视频里的时间无法解释
            "video_pages": danmaku_stats.get("video_pages", []),
            "multi_page": bool(danmaku_stats.get("multi_page")),
            "repeat_rate": spam_stats.get("repeat_rate", 0),
            "spam_level": spam_stats.get("spam_level", "低"),
            "spam_score": spam_stats.get("spam_score", 0.0),
            "spam_reason": spam_stats.get("reason", ""),
        },
    }


def analyze_commenter_profile(comment_data: dict) -> dict:
    """为仅来自评论区的用户生成简易画像"""
    return {
        "uid": comment_data.get("uid", 0),
        "name": comment_data.get("uname", "未知"),
        "level": comment_data.get("level", 0),
        "sign": comment_data.get("sign", ""),
        "avatar": comment_data.get("avatar", ""),
        "tags": ["评论区用户"],
        "source": "comment",
    }
