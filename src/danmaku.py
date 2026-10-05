"""
弹幕采集与解析模块
"""
from collections import defaultdict
from lxml import etree
from typing import Optional

from api_client import BiliAPIClient
from config import VIDEO_INFO_URL, DANMAKU_XML_URL, DANMAKU_VIEW_URL
from danmaku_history import _read_varint, _skip_field, _take_bytes  # 复用历史弹幕的 wire 手写解析
from uid_resolver import calc_crc32


def get_video_info(bvid: str, client: BiliAPIClient) -> dict:
    """获取视频基础信息"""
    data = client.get(VIDEO_INFO_URL, params={"bvid": bvid})
    if data.get("code") != 0:
        raise Exception(f"获取视频信息失败: {data.get('message')}")
    return data["data"]


def get_cid_for_page(video_info: dict, page_index: int = 0) -> int:
    """获取指定分P的cid"""
    pages = video_info.get("pages", [])
    if not pages:
        return video_info.get("cid", 0)
    if 0 <= page_index < len(pages):
        return pages[page_index]["cid"]
    return pages[0]["cid"]


# 防御性XML解析器：禁用外部实体与网络访问，防XXE/实体膨胀攻击
_SAFE_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


def parse_danmaku_xml(xml_bytes: bytes) -> list[dict]:
    """
    解析B站弹幕XML
    
    d标签属性: p="时间,模式,字号,颜色,时间戳,池,用户ID(hash),弹幕ID"
    """
    root = etree.fromstring(xml_bytes, parser=_SAFE_XML_PARSER)
    # 根标签必须是 <i>（B站弹幕XML格式）；良构的错误页（如<html>）会被静默解析为0条弹幕，
    # 这里抛出让上层把该页计入失败
    if root.tag != "i":
        raise etree.XMLSyntaxError(f"非弹幕XML（根标签<{root.tag}>）", 0, 0, 0)
    danmaku_list = []

    for d in root.findall(".//d"):
        p_attr = d.get("p", "")
        content = d.text or ""
        attrs = p_attr.split(",")
        if len(attrs) < 8:
            continue

        try:
            # 发送者标识固定为8位CRC32十六进制hash，统一按hex处理。
            # 注意：不能用isdigit()特判——约2.3%的CRC32 hash恰好全是十进制数字
            # （如"12345678"），误判为明文数字UID转码后会产生完全不同的值。
            mid_hash = attrs[6].lower()
            if not mid_hash:
                continue   # p 属性第7段为空串：无发送者标识，不计入发送者聚合
            # 只接受 1~8 位 hex：B站偶发异常字段若含非 hex 字符，zfill 后也拼不出
            # 合法 hash（与 calc_crc32 的 8 位键对不上），入聚合只会污染键空间
            if len(mid_hash) > 8 or any(c not in "0123456789abcdef" for c in mid_hash):
                continue
            # 左补零对齐历史池/calc_crc32 的 8 位格式（实测实时池个别条目不足8位）
            mid_hash = mid_hash.zfill(8)

            danmaku_list.append({
                "content": content,
                "time": float(attrs[0]),         # 视频内出现时间(秒)
                "mode": int(attrs[1]),            # 弹幕模式
                "fontsize": int(attrs[2]),        # 字号
                "color": f"#{int(attrs[3]):06x}", # 颜色
                "timestamp": int(attrs[4]),       # 发送时间戳
                "pool": int(attrs[5]),            # 弹幕池
                "mid_hash": mid_hash,             # 发送者标识（8位CRC32十六进制hash）
                "dmid": int(attrs[7]) if attrs[7].isdigit() else 0,
            })
        except (ValueError, IndexError):
            continue

    return danmaku_list


def fetch_danmaku(cid: int, client: BiliAPIClient) -> list[dict]:
    """获取单cid的弹幕列表"""
    resp = client.get_raw(DANMAKU_XML_URL, params={"oid": cid})
    return parse_danmaku_xml(resp.content)


def fetch_all_danmaku(video_info: dict, client: BiliAPIClient) -> list[dict]:
    """获取视频所有分P的弹幕（单页失败只跳过该页，不中断整体采集）"""
    pages = video_info.get("pages", [])
    if not pages:
        pages = [{"cid": video_info.get("cid", 0), "page": 1}]

    all_danmaku = []
    failed_pages = []
    for idx, page in enumerate(pages):
        cid = page.get("cid")
        if not cid:
            # 分P元信息缺 cid（异常/缺字段）：跳过该页，不让 KeyError 中断整轮采集
            failed_pages.append(idx + 1)
            print(f"[Danmaku] 警告：分P {idx + 1} 缺少 cid，已跳过")
            continue
        try:
            dms = fetch_danmaku(cid, client)
        except Exception as e:
            # 降级而非中断：网络抖动或412错误页（非XML）等单页异常仅跳过该分P
            failed_pages.append(idx + 1)
            print(f"[Danmaku] 警告：分P {idx + 1} 弹幕采集失败，已跳过该页: {e}")
            continue
        for dm in dms:
            dm["page"] = idx + 1
        all_danmaku.extend(dms)

    if failed_pages:
        print(f"[Danmaku] 警告：共 {len(failed_pages)}/{len(pages)} 页采集失败"
              f"（失败分P: {failed_pages}），弹幕数据可能不完整")
    if len(failed_pages) == len(pages) and pages:
        print("[Danmaku] 【严重警告】所有分P弹幕均采集失败，返回空弹幕列表！")

    return all_danmaku


def group_by_sender(danmaku_list: list[dict]) -> dict[str, dict]:
    """
    按发送者聚合弹幕
    
    Returns:
        {mid_hash: {
            "mid_hash": str,
            "count": int,
            "contents": [str],
            "timestamps": [int],
            "video_times": [float],
            "colors": [str],
            "pages": [int],            # 去重后的分P 列表（该发送者出现在哪些分P）
            "video_pages": [int],      # 与 contents/video_times 同下标的逐条分P
        }}
    """
    groups = defaultdict(lambda: {
        "mid_hash": "",
        "count": 0,
        "contents": [],
        "timestamps": [],
        "video_times": [],
        "colors": [],
        "pages": set(),
        "video_pages": [],
    })

    for dm in danmaku_list:
        mh = dm["mid_hash"]
        g = groups[mh]
        g["mid_hash"] = mh
        g["count"] += 1
        g["contents"].append(dm["content"])
        g["timestamps"].append(dm["timestamp"])
        g["video_times"].append(dm["time"])
        g["colors"].append(dm["color"])
        # video_pages 与 contents/video_times 严格同下标：报告靠它给样本标分P
        # （time 是「所在分P 内的相对秒数」，多分P 时不标分P 无法解释）
        try:
            g["video_pages"].append(int(dm.get("page", 1) or 1))
        except (TypeError, ValueError):
            g["video_pages"].append(1)
        g["pages"].add(dm.get("page", 1))

    # 将set转为list以便JSON序列化
    for g in groups.values():
        g["pages"] = sorted(g["pages"])

    return dict(groups)


def collect_danmaku_data(bvid: str, client: BiliAPIClient) -> tuple[dict, list[dict], dict[str, dict]]:
    """
    采集视频完整弹幕数据
    
    Returns:
        (video_info, danmaku_list, sender_groups)
    """
    print(f"[Danmaku] 获取视频信息: {bvid}")
    video_info = get_video_info(bvid, client)
    title = video_info.get("title", "")
    print(f"[Danmaku] 视频: {title}")

    print(f"[Danmaku] 获取弹幕...")
    danmaku_list = fetch_all_danmaku(video_info, client)
    print(f"[Danmaku] 获取到 {len(danmaku_list)} 条弹幕")

    sender_groups = group_by_sender(danmaku_list)
    print(f"[Danmaku] 独立发送者: {len(sender_groups)} 人")

    return video_info, danmaku_list, sender_groups


# ========== 互动弹幕（commandDms，含明文 mid） ==========

def _parse_command_dm(data: bytes, corrupt: list | None = None) -> dict:
    """解析单个 CommandDm 嵌套消息：mid=3(varint), command=4, content=5。
    corrupt 提供时把长度越界截断的位置记入其中（供调用方计数告警）。"""
    fields = {}
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_no, wire_type = tag >> 3, tag & 0x07
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
            fields[field_no] = value
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            chunk, pos, truncated = _take_bytes(data, pos, length)
            if truncated and corrupt is not None:
                corrupt.append(pos)
            fields[field_no] = chunk
        else:
            pos = _skip_field(data, pos, wire_type)

    def _s(field_no: int) -> str:
        raw = fields.get(field_no, b"")
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""

    return {
        "mid": fields.get(3, 0),
        "command": _s(4),
        "content": _s(5),
    }


def fetch_command_dms(video_info: dict, client: BiliAPIClient) -> list[dict]:
    """
    采集互动弹幕 commandDms（UP主头像弹幕/关联视频/引导关注，含明文 mid）

    接口需 SESSDATA；protobuf wire 手写解析（复用 danmaku_history 先例）。
    多分P视频只采集第一P（互动弹幕按 cid 维度，主价值是UP主mid）。
    失败降级返回空列表，不影响主流程。

    Returns:
        [{"mid": int, "command": str, "content": str}]
    """
    try:
        cid = get_cid_for_page(video_info, 0)
        aid = video_info.get("aid", 0)
        if not cid:
            return []
        raw = client.get_raw(DANMAKU_VIEW_URL, params={"type": 1, "oid": cid, "pid": aid}).content
        items = []
        corrupt = []   # 损坏条目计数（长度越界截断/单条解析失败），结束统一告警
        pos = 0
        while pos < len(raw):
            tag, pos = _read_varint(raw, pos)
            field_no, wire_type = tag >> 3, tag & 0x07
            if field_no == 9 and wire_type == 2:  # commandDms
                length, pos = _read_varint(raw, pos)
                elem_bytes, pos, truncated = _take_bytes(raw, pos, length)
                if truncated:
                    corrupt.append(pos)
                try:
                    items.append(_parse_command_dm(elem_bytes, corrupt))
                except Exception:
                    # 单条损坏跳过不丢整份（对齐 parse_danmaku_proto 的 per-item 策略）
                    corrupt.append(pos)
                    continue
            else:
                pos = _skip_field(raw, pos, wire_type)
        if corrupt:
            print(f"[Danmaku] 警告：互动弹幕响应含 {len(corrupt)} 处损坏，已截断/跳过")
        if items:
            print(f"[Danmaku] 互动弹幕: {len(items)} 条（含明文mid）")
        return items
    except Exception as e:
        print(f"[Danmaku] 互动弹幕获取失败: {e}（降级跳过）")
        return []


def build_command_uid_map(command_dms: list[dict]) -> dict:
    """互动弹幕明文 mid → crc32->UID 映射"""
    uid_map = {}
    for item in command_dms:
        mid = item.get("mid")
        if mid:
            uid_map[calc_crc32(int(mid))] = int(mid)
    return uid_map
