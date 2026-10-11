"""UID 明文候选的归一化与合并；同一 CRC32 的不同 UID 必须同时保留。"""
import zlib

PLAINTEXT_SOURCES = frozenset({"评论区验证", "评论收割", "充电名单", "互动弹幕", "视频信息"})


def crc32_uid(uid: int) -> str:
    return f"{zlib.crc32(str(uid).encode()) & 0xffffffff:08x}"


def candidates_of(value) -> set[int]:
    """兼容旧的单 UID 映射、新的集合及全局证据记录。"""
    if isinstance(value, dict):
        value = value.get("uids", [value.get("uid")])
    values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    result = set()
    for item in values:
        try:
            uid = int(item)
        except (TypeError, ValueError):
            continue
        if not isinstance(item, bool) and 0 < uid <= 2**63 - 1:
            result.add(uid)
    return result


def add_uid(mapping: dict, mid_hash: str, uid: int):
    """只加入正向校验相符的候选，避免脏映射参与归属。"""
    values = candidates_of(uid)
    if values and crc32_uid(next(iter(values))) == mid_hash:
        mapping[mid_hash] = candidates_of(mapping.get(mid_hash)) | values


def merge_uid_maps(*maps: dict) -> dict[str, set[int]]:
    result = {}
    for mapping in maps:
        for h, value in mapping.items():
            for uid in candidates_of(value):
                add_uid(result, h, uid)
    return result
