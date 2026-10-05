"""
MITM（中间相遇）CRC32 反查：mid_hash → 全部候选 UID

算法：zlib.crc32 可链式调用，且对定长 5 字节后缀 s，f_s(x)=crc32(s,x) 是 x 的
仿射函数，线性部分与 s 内容无关（同 5 个 0x00 字节）。故
    f_s(x) = _advance5(x) ^ crc32(b"\\x00"*5) ^ crc32(s)
预计算 10 万个 5 位后缀串（"00000"~"99999"）的 crc 建内存小表（首次查询时
惰性构建，秒级，无需落盘大表）；查询时枚举 ≤5 位前缀（6~10 位 UID 共 99999 个
前缀），反查所需后缀 crc 得候选，最后逐候选 zlib 校验保证精确。

参考：esterTion/BiliBili_crc2mid、Aruelius/crc32-crack（MoePus MITM）。
覆盖范围：全部 ≤10 位 UID（16 位随机长 UID 数学上不可解，见 spec 第 9 节）。
纯标准库实现，不引入第三方依赖。
"""
import threading
import zlib

from config import MITM_MAX_UID

_SUFFIX_DIGITS = 5                    # 后缀定长（十进制位）
_SUFFIX_COUNT = 10 ** _SUFFIX_DIGITS  # 100000
_MAX_PREFIX = 10 ** _SUFFIX_DIGITS    # 前缀最多 5 位：99999

# 惰性构建的进程级缓存
_suffix_crc_map: dict | None = None   # crc32("%05d" % n) -> [n, ...]
_small_uid_map: dict | None = None    # crc32(str(uid)) -> [uid, ...]，uid < 100000
_prefix_crc: list | None = None       # [crc32(str(p)) for p in range(100000)]
_prefix_adv5: list | None = None      # [_advance5(crc32(str(p)))]，建表时预计算，lookup 复用
_zeros5_crc: int = 0                  # crc32(b"\x00" * 5)
_crc_byte_table: list | None = None   # 标准 CRC32 字节推进表（多项式 0xEDB88320）
_build_lock = threading.RLock()      # 建表双检锁；RLock 允许 _ensure_tables 内嵌套构建字节表


def _get_byte_table() -> list:
    """标准 CRC32 表（与 zlib 相同的多项式），用于手写字节推进。

    惰性构建结果幂等，加锁只为避免并发首建重复计算；用 RLock 以便 _ensure_tables
    持锁期间（预计算 _prefix_adv5 时会调用 _advance5）重入构建不死锁。"""
    global _crc_byte_table
    if _crc_byte_table is None:
        with _build_lock:
            if _crc_byte_table is None:
                table = []
                for i in range(256):
                    c = i
                    for _ in range(8):
                        c = (0xEDB88320 ^ (c >> 1)) if (c & 1) else (c >> 1)
                    table.append(c)
                _crc_byte_table = table
    return _crc_byte_table


def _advance5(crc: int) -> int:
    """从链式值 crc 推进 5 个 0x00 字节，等价于 zlib.crc32(b"\\x00"*5, crc)"""
    table = _get_byte_table()
    state = crc ^ 0xFFFFFFFF
    for _ in range(5):
        state = table[state & 0xFF] ^ (state >> 8)
    return state ^ 0xFFFFFFFF


def _ensure_tables():
    """首次查询时惰性构建全部内存表（约 2 秒，之后驻留内存约 40MB）

    线程安全：双检锁保证并发首次查询只构建一次；各表在局部变量中
    构建完毕后一次性赋给全局，其他线程不会读到半成品状态。
    """
    global _suffix_crc_map, _small_uid_map, _prefix_crc, _prefix_adv5, _zeros5_crc
    if _suffix_crc_map is not None:  # 快路径：无锁检查
        return
    with _build_lock:
        if _suffix_crc_map is not None:  # 双检：持锁期间可能已被其他线程构建
            return
        suffix_map: dict[int, list] = {}
        small_map: dict[int, list] = {}
        prefix_crc = [0] * _SUFFIX_COUNT
        for n in range(_SUFFIX_COUNT):
            c = zlib.crc32(str(n).encode())
            suffix_map.setdefault(zlib.crc32(("%05d" % n).encode()), []).append(n)
            small_map.setdefault(c, []).append(n)
            prefix_crc[n] = c
        # 预计算全部前缀的 5 字节推进值，供每次 lookup 复用（省逐 hash 的 10 万次 _advance5）
        prefix_adv5 = [_advance5(c) for c in prefix_crc]
        # 全部构建完成后一次性赋给全局；_suffix_crc_map 是快路径的"就绪"判据，
        # 必须最后一个发布——否则并发首次查询可能读到半成品（_prefix_adv5=None 崩、
        # _zeros5_crc=0 静默漏候选）
        _small_uid_map = small_map
        _prefix_crc = prefix_crc
        _prefix_adv5 = prefix_adv5
        _zeros5_crc = zlib.crc32(b"\x00" * 5)
        _suffix_crc_map = suffix_map


def lookup(crc32_hash: str, max_uid: int = MITM_MAX_UID) -> list:
    """
    MITM 反查：输入 8 位 hex（如 '4200b4cd'），返回全部候选 UID（升序）。

    覆盖 ≤10 位 UID（由 max_uid 控制，默认 MITM_MAX_UID=10^10）。
    每个返回候选都经过 zlib 校验，数学上精确；候选数平均约 2.3 个
    （10^10 空间对 2^32 哈希空间），消歧由调用方负责。
    输入非法时返回空列表。
    """
    try:
        target = int(crc32_hash.strip(), 16)
    except (ValueError, AttributeError):
        return []
    if not (0 <= target <= 0xFFFFFFFF):
        return []

    _ensure_tables()
    max_uid = min(max_uid, MITM_MAX_UID)
    results = set()

    # 1) UID < 100000（不足 6 位，无前缀可拆）：直接查小表
    for uid in _small_uid_map.get(target, ()):
        if uid <= max_uid:
            results.add(uid)

    # 2) 6~10 位 UID：前缀（str(prefix)）+ 5 位定长后缀
    #    前缀推进值建表时已预计算（_prefix_adv5），此处只做 XOR + 查表
    z5 = _zeros5_crc
    suffix_map = _suffix_crc_map
    prefix_adv5 = _prefix_adv5
    for prefix in range(1, _MAX_PREFIX):
        uid_base = prefix * _SUFFIX_COUNT
        if uid_base > max_uid:
            break
        need = target ^ prefix_adv5[prefix] ^ z5
        for n in suffix_map.get(need, ()):
            uid = uid_base + n
            if uid <= max_uid:
                results.add(uid)

    # 3) 逐候选 zlib 校验（仿射推导理论上精确，校验防御实现错误）并排序
    return sorted(u for u in results if zlib.crc32(str(u).encode()) == target)
