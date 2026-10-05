"""
用户深度数据采集模块（四维度）

维度1: 用户主页信息
维度2: 互动内容足迹
维度3: 社交关系网络
维度4: 行为模式分析
"""
import time
from datetime import datetime, timezone, timedelta
from api_client import BiliAPIClient
from config import (
    USER_CARD_URL, USER_CARDS_BATCH_URL, USER_SPACE_URL, USER_VIDEOS_URL,
    USER_VIDEOS_LEGACY_URL,
    USER_DYNAMICS_URL, USER_FOLLOWINGS_URL, USER_FOLLOWERS_URL,
    USER_FAV_FOLDERS_URL, USER_FAV_CONTENTS_URL, USER_BANGUMI_URL,
    MAX_VIDEO_PAGES, MAX_DYNAMIC_PAGES, MAX_FOLLOWING_PAGES,
    MAX_FOLLOWER_PAGES, MAX_FAV_CONTENTS
)


def _safe_int(v, default=0):
    """B站数值字段可能返回 '--' 等字符串，强转失败时降级为默认值"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ========== 维度1：用户主页信息 ==========

def get_user_card(uid: int, client: BiliAPIClient) -> dict:
    """获取用户空间卡信息（最基础的信息）"""
    data = client.get(USER_CARD_URL, params={"mid": uid})
    if data.get("code") != 0:
        return {"error": data.get("message", "获取失败")}

    card = (data.get("data") or {}).get("card") or {}
    vip = card.get("vip", {})
    official = card.get("official_verify", {})

    return {
        "uid": uid,
        "name": card.get("name", ""),
        "face": card.get("face", ""),
        "sign": card.get("sign", ""),
        "sex": card.get("sex", ""),
        "level": card.get("level_info", {}).get("current_level", 0),
        "vip_type": vip.get("type", 0),        # 1:月度大会员, 2:年度大会员
        "vip_status": vip.get("status", 0),    # 1:有效
        "vip_due_date": vip.get("due_date", 0),
        "official_type": _safe_int(official.get("type", -1), -1),
        "official_title": official.get("desc", ""),
        "follower": card.get("fans", 0),
        "following": card.get("attention", 0),
        "like_num": (data.get("data") or {}).get("like_num", 0),
        "archive_count": card.get("archive_count", 0),
        "article_count": card.get("article_count", 0),
        # ip_location removed (B站已下线该字段)
    }


def get_user_cards_batch(uids: list[int], client: BiliAPIClient) -> dict[int, dict]:
    """批量获取用户名片（昵称/头像/认证/大会员），≤50 人/请求自动分批

    仅需登录态、无需 wbi 签名。返回 {uid: 名片信息}，失败批次跳过（降级不中断）。
    备用接口：当前主采集流程未调用，供后续批量预取使用。
    """
    result = {}
    uids = list(dict.fromkeys(uids))  # 去重保持顺序
    for i in range(0, len(uids), 50):
        batch = uids[i:i + 50]
        data = client.get(USER_CARDS_BATCH_URL, params={
            "uids": ",".join(str(u) for u in batch)
        })
        if data.get("code") != 0:
            continue
        for uid_str, card in (data.get("data") or {}).items():
            vip = card.get("vip") or {}
            official = card.get("official") or {}
            result[_safe_int(uid_str)] = {
                "name": card.get("name", ""),
                "face": card.get("face", ""),
                "official_type": _safe_int(official.get("type", -1), -1),
                "official_title": official.get("title") or official.get("desc", ""),
                "vip_type": _safe_int(vip.get("type", 0)),
                "vip_status": _safe_int(vip.get("status", 0)),
            }
    return result


_space_cookie_warned = False


def get_user_space_info(uid: int, client: BiliAPIClient) -> dict:
    """获取用户空间详细信息（含硬币数、直播等）

    使用 wbi 版 acc/info（client 对 /wbi/ 路径自动签名），要求 Cookie ≥3 项。
    """
    global _space_cookie_warned
    if not _space_cookie_warned:
        _space_cookie_warned = True
        cookie_count = len(client.get_cookies_dict())
        if cookie_count < 3:
            print(f"  [Collect] 警告：Cookie 仅 {cookie_count} 项（wbi/acc/info 需 ≥3 项），空间信息可能获取失败")

    data = client.get(USER_SPACE_URL, params={"mid": uid})
    if data.get("code") != 0:
        return {}

    info = data.get("data") or {}
    live = info.get("live_room") or {}

    return {
        "coins": info.get("coins", 0),
        "is_senior_member": info.get("is_senior_member", 0),  # 硬核会员
        "live_room_id": live.get("roomid", 0),
        "live_status": live.get("liveStatus", 0),
        "live_title": live.get("title", ""),
        "live_url": live.get("url", ""),
        "school": (info.get("school") or {}).get("name", ""),
        "profession": (info.get("profession") or {}).get("name", ""),
        "tags": info.get("tags") or [],
    }


# ========== 维度2：互动内容足迹 ==========

def get_user_videos(uid: int, client: BiliAPIClient, max_pages: int = MAX_VIDEO_PAGES) -> list[dict]:
    """获取用户投稿视频列表

    优先用 recArchivesByKeywords（文档注明暂无风控校验，无需 wbi 签名）；
    首页失败时降级旧 wbi/arc/search（需 dm_img 指纹参数，易触发 -352）。
    两条路径输出契约一致：bvid/title/description/play/comment/created/length/typeid/tag。
    """
    videos = _get_videos_rec(uid, client, max_pages)
    if videos is None:
        print(f"  [Collect] UID:{uid} 投稿新接口失败，降级旧 arc/search")
        videos = _get_videos_arc(uid, client, max_pages)
    return videos


def _get_videos_rec(uid: int, client: BiliAPIClient, max_pages: int) -> list[dict] | None:
    """recArchivesByKeywords 路径；首页 code != 0 返回 None 触发降级，空列表视为成功"""
    all_videos = []
    for page in range(1, max_pages + 1):
        data = client.get(USER_VIDEOS_URL, params={
            "mid": uid, "keywords": "", "ps": 30, "pn": page
        })
        if data.get("code") != 0:
            return None if page == 1 else all_videos

        d = data.get("data") or {}
        archives = d.get("archives") or []
        if not archives:
            break

        for v in archives:
            duration = _safe_int(v.get("duration", 0))
            all_videos.append({
                "bvid": v.get("bvid", ""),
                "title": v.get("title", ""),
                "description": v.get("desc", ""),
                "play": _safe_int((v.get("stat") or {}).get("view", 0)),
                "comment": 0,   # 该接口不返回评论数
                "created": _safe_int(v.get("pubdate", 0)),
                "length": f"{duration // 60}:{duration % 60:02d}" if duration else "",
                "typeid": 0,    # 该接口不返回分区ID
                "tag": "",
            })

        total = _safe_int((d.get("page") or {}).get("total", 0))
        if total and len(all_videos) >= total:
            break

    return all_videos


def _get_videos_arc(uid: int, client: BiliAPIClient, max_pages: int) -> list[dict]:
    """旧 wbi/arc/search 路径（降级用；返回 typeid 分区信息，但需指纹参数易 -352）"""
    all_videos = []
    for page in range(1, max_pages + 1):
        data = client.get(USER_VIDEOS_LEGACY_URL, params={
            "mid": uid, "ps": 30, "pn": page, "order": "pubdate"
        })
        if data.get("code") != 0:
            break

        vlist = ((data.get("data") or {}).get("list") or {}).get("vlist", [])
        if not vlist:
            break

        for v in vlist:
            all_videos.append({
                "bvid": v.get("bvid", ""),
                "title": v.get("title", ""),
                "description": v.get("description", ""),
                "play": _safe_int(v.get("play", 0)),
                "comment": _safe_int(v.get("comment", 0)),
                "created": _safe_int(v.get("created", 0)),
                "length": v.get("length", ""),
                "typeid": v.get("typeid", 0),
                "tag": v.get("tag") or "",
            })

        total = ((data.get("data") or {}).get("page") or {}).get("count", 0)
        if len(all_videos) >= total:
            break

    return all_videos


def get_user_dynamics(uid: int, client: BiliAPIClient, max_pages: int = MAX_DYNAMIC_PAGES) -> list[dict]:
    """获取用户动态列表"""
    all_dynamics = []
    offset = ""

    for _ in range(max_pages):
        params = {"host_mid": uid}
        if offset:
            params["offset"] = offset

        data = client.get(USER_DYNAMICS_URL, params=params)
        if data.get("code") != 0:
            break

        items = (data.get("data") or {}).get("items", [])
        if not items:
            break

        for item in items:
            modules = item.get("modules", {})
            author = modules.get("module_author", {})
            dynamic = modules.get("module_dynamic", {})
            desc = dynamic.get("desc", {})
            major = dynamic.get("major", {})

            content = desc.get("text", "") if desc else ""
            dyn_type = item.get("type", "")

            # 提取图片
            images = []
            if major and major.get("draw"):
                for img in major["draw"].get("items", []):
                    images.append(img.get("src", ""))

            # 提取视频信息
            video_info = None
            if major and major.get("archive"):
                archive = major["archive"]
                video_info = {
                    "title": archive.get("title", ""),
                    "bvid": archive.get("bvid", ""),
                    "play": _safe_int(archive.get("stat", {}).get("view", 0)),
                }

            stat = modules.get("module_stat", {})
            all_dynamics.append({
                "id": item.get("id_str", ""),
                "type": dyn_type,
                "content": content[:500],
                "images": images[:4],
                "timestamp": author.get("pub_ts", 0),
                "like": _safe_int(stat.get("like", {}).get("count", 0)),
                "comment": _safe_int(stat.get("comment", {}).get("count", 0)),
                "repost": _safe_int(stat.get("forward", {}).get("count", 0)),
                "video_info": video_info,
            })

        offset = (data.get("data") or {}).get("offset", "")
        if not (data.get("data") or {}).get("has_more", False):
            break

    return all_dynamics


def get_favorite_folders(uid: int, client: BiliAPIClient) -> list[dict]:
    """获取用户创建的收藏夹"""
    data = client.get(USER_FAV_FOLDERS_URL, params={"up_mid": uid})
    if data.get("code") != 0:
        return []

    folders = []
    data_obj = data.get("data") or {}
    for f in data_obj.get("list", []):
        folders.append({
            "id": f.get("id", 0),
            "title": f.get("title", ""),
            "media_count": _safe_int(f.get("media_count", 0)),
            "attr": f.get("attr", 0),  # 0:公开, 1:私密
        })
    return folders


def get_favorite_contents(media_id: int, client: BiliAPIClient, max_items: int = MAX_FAV_CONTENTS) -> list[dict]:
    """获取收藏夹内容"""
    data = client.get(USER_FAV_CONTENTS_URL, params={
        "media_id": media_id, "ps": max_items, "pn": 1
    })
    if data.get("code") != 0:
        return []

    items = []
    for item in (data.get("data") or {}).get("medias") or []:
        items.append({
            "id": item.get("id", 0),
            "title": item.get("title", ""),
            "upper": item.get("upper", {}).get("name", ""),
            "type": item.get("type", 0),
            "bvid": item.get("bvid", ""),
            "play": _safe_int(item.get("cnt_info", {}).get("play", 0)),
        })
    return items


# ========== 维度3：社交关系网络 ==========

def get_followings(uid: int, client: BiliAPIClient, max_pages: int = MAX_FOLLOWING_PAGES) -> list[dict]:
    """获取用户关注列表"""
    all_followings = []
    for page in range(1, max_pages + 1):
        data = client.get(USER_FOLLOWINGS_URL, params={
            "vmid": uid, "ps": 20, "pn": page
        })
        if data.get("code") != 0:
            break

        flist = (data.get("data") or {}).get("list", [])
        if not flist:
            break

        for f in flist:
            all_followings.append({
                "uid": f.get("mid", 0),
                "name": f.get("uname", ""),
                "sign": f.get("sign", ""),
                "official_type": _safe_int((f.get("official") or {}).get("type", -1), -1),
                "vip_type": _safe_int((f.get("vip") or {}).get("type", 0)),
                "face": f.get("face", ""),
            })

        total = (data.get("data") or {}).get("total", 0)
        if len(all_followings) >= total:
            break

    return all_followings


def get_followers(uid: int, client: BiliAPIClient, max_pages: int = MAX_FOLLOWER_PAGES) -> dict:
    """获取用户粉丝列表（采样）"""
    all_followers = []
    for page in range(1, max_pages + 1):
        data = client.get(USER_FOLLOWERS_URL, params={
            "vmid": uid, "ps": 20, "pn": page
        })
        if data.get("code") != 0:
            break

        flist = (data.get("data") or {}).get("list", [])
        if not flist:
            break

        for f in flist:
            all_followers.append({
                "uid": f.get("mid", 0),
                "name": f.get("uname", ""),
                "sign": f.get("sign", ""),
                "official_type": _safe_int((f.get("official") or {}).get("type", -1), -1),
                "vip_type": _safe_int((f.get("vip") or {}).get("type", 0)),
            })

        total = (data.get("data") or {}).get("total", 0)
        if len(all_followers) >= total:
            break

    return {"total": len(all_followers), "sample": all_followers}


# ========== 维度4：行为模式相关 ==========

def get_bangumi_list(uid: int, client: BiliAPIClient, btype: int = 1) -> list[dict]:
    """
    获取用户追番/追剧列表
    btype: 1=番剧, 2=追剧
    """
    try:
        data = client.get(USER_BANGUMI_URL, params={
            "vmid": uid, "type": btype, "pn": 1, "ps": 15
        })
    except Exception:
        return []
    if data.get("code") != 0:
        return []

    items = []
    for item in (data.get("data") or {}).get("list", []):
        items.append({
            "title": item.get("title", ""),
            "season_id": item.get("season_id", 0),
            "total": item.get("total", 0),
            "new_ep": item.get("new_ep", {}).get("index_show", ""),
            "cover": item.get("cover", ""),
            "is_finish": item.get("is_finish", 0),
        })
    return items


def analyze_activity_pattern(timestamps: list[int]) -> dict:
    """分析活跃时间模式"""
    if not timestamps:
        return {}

    hours = {}
    days = {}
    day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    for ts in timestamps:
        try:
            ts_int = int(ts)
            # 显式按东八区解读时间戳，活跃时段统计不依赖部署机器时区
            dt = datetime.fromtimestamp(ts_int, tz=timezone(timedelta(hours=8)))
            h = dt.hour
            d = day_names[dt.weekday()]
            hours[h] = hours.get(h, 0) + 1
            days[d] = days.get(d, 0) + 1
        except (ValueError, OSError, TypeError):
            continue

    peak_hour = max(hours.items(), key=lambda x: x[1])[0] if hours else None
    peak_day = max(days.items(), key=lambda x: x[1])[0] if days else None

    # 判断活跃类型
    activity_type = "未知"
    if peak_hour is not None:
        if 6 <= peak_hour < 12:
            activity_type = "早起型"
        elif 12 <= peak_hour < 18:
            activity_type = "午间活跃"
        elif 18 <= peak_hour < 24:
            activity_type = "晚间活跃"
        else:
            activity_type = "深夜党"   # 0-5 时：与 profile_analyzer 的"深夜党"标签同名，避免"夜猫子/深夜党"语义重叠

    return {
        "peak_hour": peak_hour,
        "peak_day": peak_day,
        "activity_type": activity_type,
        "hour_distribution": dict(sorted(hours.items())),
        "day_distribution": days,
    }


# ========== 统一采集接口 ==========

def collect_user_data(uid: int, client: BiliAPIClient, log=None) -> dict:
    """
    采集用户完整深度数据（四维度）

    log: 日志输出函数（默认 print）。多号并行分片采集时调用方传入行缓冲，
    把单用户的采集日志攒成原子块输出，避免多线程交错混排。

    Returns:
        包含所有维度的完整数据dict
    """
    if log is None:
        log = print
    log(f"  [Collect] UID:{uid} 开始采集...")

    # 维度1：主页信息
    card = get_user_card(uid, client)
    if "error" in card:
        return {"uid": uid, "error": card["error"]}

    space = get_user_space_info(uid, client)
    user_data = {**card, **space}

    # 追番
    try:
        user_data["bangumi"] = get_bangumi_list(uid, client, btype=1)
    except Exception:
        user_data["bangumi"] = []
    try:
        user_data["dramas"] = get_bangumi_list(uid, client, btype=2)
    except Exception:
        user_data["dramas"] = []

    # 收藏夹
    try:
        folders = get_favorite_folders(uid, client)
        user_data["favorite_folders"] = folders
    except Exception:
        folders = []
        user_data["favorite_folders"] = []
    if folders:
        try:
            user_data["favorite_contents"] = get_favorite_contents(folders[0]["id"], client)
        except Exception as e:
            # 收藏夹内容采集失败降级为空列表，不连累已采的主页/追番/收藏夹数据
            log(f"  [Collect] 警告: UID:{uid} 收藏夹内容采集失败（{e}），降级为空列表")
            user_data["favorite_contents"] = []
    else:
        user_data["favorite_contents"] = []

    log(f"  [Collect] UID:{uid} 维度1(主页/收藏)完成，采集互动足迹...")

    # 维度2：互动足迹
    try:
        user_data["videos"] = get_user_videos(uid, client)
    except Exception:
        user_data["videos"] = []
    try:
        user_data["dynamics"] = get_user_dynamics(uid, client)
    except Exception:
        user_data["dynamics"] = []

    log(f"  [Collect] UID:{uid} 维度2(互动足迹)完成，采集社交关系...")

    # 维度3：社交网络
    try:
        user_data["followings"] = get_followings(uid, client)
    except Exception:
        user_data["followings"] = []
    try:
        user_data["followers"] = get_followers(uid, client)
    except Exception:
        # 与 get_followers 正常返回结构（{"total", "sample"}）保持同型，防下游踩类型坑
        user_data["followers"] = {"total": 0, "sample": []}

    log(f"  [Collect] UID:{uid} 维度3(社交关系)完成，分析关注偏好/行为模式...")

    # UP主关注偏好：只存关注名单本身（全部关注，uid+name+sign），不再逐个分析
    # 被关注 UP 主的投稿/词频——那部分挪到报告页悬停时按需懒加载（/api/up/<uid>/wordcloud），
    # 初采不再为每个用户扇出批量请求，阶段5 显著提速
    user_data["following_summary"] = {"total": len(user_data["followings"])}

    # 维度4：行为模式（综合动态 + 视频投稿时间）
    dynamic_timestamps = [d["timestamp"] for d in user_data["dynamics"] if d.get("timestamp")]
    video_timestamps = [v["created"] for v in user_data.get("videos", []) if v.get("created")]
    all_timestamps = dynamic_timestamps + video_timestamps
    user_data["activity_pattern"] = analyze_activity_pattern(all_timestamps)

    # 最早活跃时间：采样范围内最早一条动态的发布时间（仅采样范围，非注册时间）
    if dynamic_timestamps:
        user_data["first_seen"] = min(dynamic_timestamps)

    log(f"  [Collect] UID:{uid} {user_data.get('name','')} Lv.{user_data.get('level',0)} 采集完成")
    return user_data
