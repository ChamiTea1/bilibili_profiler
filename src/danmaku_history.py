"""
历史弹幕采集模块（弹幕池历史快照）

实时弹幕池（XML 接口）只保留最近几千条，历史弹幕接口可按日拉取历史快照：
- history/index：查询某月哪些日期有弹幕（需登录）
- history/seg.so：拉取某日期的弹幕池快照 protobuf（需 SESSDATA）

【实测语义 2026-08-03 验证】seg.so 并非"该日发送的弹幕"，而是"截至该日期的
最新 1000 条弹幕池快照"（每日上限 1000，弹幕 ctime 可早于请求日期；相邻日
快照可能大量重叠或完全不重叠）。逐日遍历 + 按 dmid 去重可逼近全量历史弹幕，
但热门日期（如发布初期）快照间的弹幕滚动仍可能丢失。
另：历史接口的 midHash 省略前导零（需 zfill(8)）；weight/pool 字段不下发（恒 0）。

protobuf 结构（DmSegMobileReply）：
    repeated DanmakuElem elems = 1;
    DanmakuElem: id=1(varint), progress=2(varint毫秒), mode=3, fontsize=4,
                 color=5, midHash=6(string), content=7(string), ctime=8(varint),
                 weight=9, pool=11, idStr=12(string)
为免引入 protobuf 依赖，wire 格式手写解析。
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

from api_client import BiliAPIClient
from config import (
    DANMAKU_HISTORY_INDEX_URL,
    DANMAKU_HISTORY_SEG_URL,
    HISTORY_DANMAKU_ENABLED,
    HISTORY_MAX_MONTHS,
    HISTORY_MAX_DAYS,
    HISTORY_RECENT_REFRESH_DAYS,
)

# done=1 的已完成视频重复调用时，回拨检查点滚动补采最近 HISTORY_RECENT_REFRESH_DAYS
# 天的弹幕快照（弹幕池每日滚动，旧快照不含新弹幕；dmid 幂等去重，重复调用开销极小）


# ========== protobuf wire 手写解析 ==========

def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """读取 varint，返回 (值, 新位置)"""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("varint 越界")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint 过长")


def _take_bytes(data: bytes, pos: int, length: int) -> tuple[bytes, int, bool]:
    """截取 length 字节，返回 (片段, 新位置, 是否被截断)。
    长度超剩余字节说明流已损坏：截断到末尾（保留已解析部分），由调用方计数告警。"""
    end = pos + length
    truncated = end > len(data)
    if truncated:
        end = len(data)
    return data[pos:end], end, truncated


def _skip_field(data: bytes, pos: int, wire_type: int) -> int:
    """跳过未知字段，返回新位置；长度超剩余字节说明流损坏，抛错由调用方按截断处理"""
    if wire_type == 0:      # varint
        _, pos = _read_varint(data, pos)
        return pos
    if wire_type == 1:      # 64-bit
        end = pos + 8
    elif wire_type == 2:    # length-delimited
        length, pos = _read_varint(data, pos)
        end = pos + length
    elif wire_type == 5:    # 32-bit
        end = pos + 4
    else:
        raise ValueError(f"不支持的 wire type: {wire_type}")
    if end > len(data):
        raise ValueError(f"字段长度越界（wire type {wire_type}，位置 {pos}）")
    return end


def _parse_elem(data: bytes, corrupt: list | None = None) -> dict:
    """解析单个 DanmakuElem 嵌套消息，输出与 danmaku.parse_danmaku_xml 同构的 dict。
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

    id_str = _s(12)
    return {
        "content": _s(7),                                     # 弹幕内容
        "time": fields.get(2, 0) / 1000.0,                    # 视频内出现时间(秒)，progress 为毫秒
        "mode": fields.get(3, 1),                             # 弹幕模式
        "fontsize": fields.get(4, 25),                        # 字号
        "color": f"#{fields.get(5, 0xffffff):06x}",           # 颜色
        "timestamp": fields.get(8, 0),                        # 发送时间戳(ctime)
        "pool": fields.get(11, 0),                            # 弹幕池
        # midHash 是 CRC32 hex 字符串，但历史接口省略前导零（实测约 6% 为 6~7 位），
        # 左补零对齐实时池 XML 的 8 位格式，否则与 CRC32 反查结果对不上
        "mid_hash": _s(6).lower().zfill(8),                   # 发送者标识（8位CRC32十六进制hash）
        "dmid": int(id_str) if id_str.isdigit() else fields.get(1, 0),
        "weight": fields.get(9, 0),                           # 弹幕权重（历史接口特有）
    }


def parse_danmaku_proto(data: bytes) -> list[dict]:
    """
    解析 DmSegMobileReply protobuf 字节流（历史弹幕 seg.so 响应）

    顶层结构只有 field 1（elems，length-delimited 嵌套消息）重复出现。
    单条 elem 解析失败跳过不中断；字段长度越界按损坏截断（保留已解析部分）并计数告警。
    """
    danmaku_list = []
    corrupt = []   # 损坏位置计数（长度越界截断/顶层流损坏），结束统一告警
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _read_varint(data, pos)
            field_no, wire_type = tag >> 3, tag & 0x07
            if field_no == 1 and wire_type == 2:
                length, pos = _read_varint(data, pos)
                elem_bytes, pos, truncated = _take_bytes(data, pos, length)
                if truncated:
                    corrupt.append(pos)
                try:
                    danmaku_list.append(_parse_elem(elem_bytes, corrupt))
                except Exception:
                    # 单条弹幕损坏不中断整日解析
                    continue
            else:
                pos = _skip_field(data, pos, wire_type)
        except (ValueError, IndexError):
            # 顶层流损坏：已解析的部分仍然可用，直接返回
            corrupt.append(pos)
            break
    if corrupt:
        print(f"[历史弹幕] 警告：响应体含 {len(corrupt)} 处损坏字段，已按截断处理")
    return danmaku_list


# ========== 历史弹幕采集 ==========

def _month_range(start_ym: tuple[int, int], end_ym: tuple[int, int]) -> list[str]:
    """生成 [start, end] 闭区间的 'YYYY-MM' 月份列表"""
    months = []
    y, m = start_ym
    while (y, m) <= end_ym:
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return months


def _state_key(key: str, page: int) -> str:
    """分P检查点键：分P 1 沿用原键名（既有报告的分P1检查点照旧生效，不触发重采），
    其余分P 追加 ":{pN}" 后缀——各分P 的日期集/高水位/done 相互独立，互不污染。"""
    return key if page <= 1 else f"{key}:p{page}"


def _load_date_set(bvid: str, key: str, page: int = 1) -> set[str]:
    """读取 phase_state 中的逗号分隔日期串（failed_dates/fetched_dates）为集合"""
    from storage import get_phase_state
    raw = get_phase_state(bvid, "danmaku", _state_key(key, page)) or ""
    return {d for d in raw.split(",") if d}


def _save_date_set(bvid: str, key: str, dates: set[str], page: int = 1):
    """回写逗号分隔日期串（空集合写空串，读取侧按空集处理）"""
    from storage import set_phase_state
    set_phase_state(bvid, "danmaku", _state_key(key, page), ",".join(sorted(dates)))


def _fetch_month_dates(cid: int, month: str, client: BiliAPIClient) -> list[str] | None:
    """查询某月有弹幕的日期列表；失败返回 None（该月按失败处理，与"本月无弹幕"的空列表区分）"""
    data = client.get(DANMAKU_HISTORY_INDEX_URL, params={"type": 1, "oid": cid, "month": month})
    if data.get("code") != 0:
        print(f"[历史弹幕] 警告：{month} 月份索引获取失败（{data.get('message')}），跳过该月")
        return None
    return data.get("data") or []


def _fetch_day_danmaku(cid: int, date: str, client: BiliAPIClient) -> list[dict]:
    """拉取并解析某日期的弹幕池快照（截至该日的最新1000条，非"该日发送的弹幕"）

    合法 DmSegMobileReply 首字段为 elems（field 1, length-delimited），首字节必为 0x0A；
    不符合特征（HTTP200 的 HTML 错误页/空响应等）抛 ValueError，由调用方按当日失败处理，
    避免错误页被静默解析为 0 条照常计天推进。"""
    resp = client.get_raw(DANMAKU_HISTORY_SEG_URL, params={"type": 1, "oid": cid, "date": date})
    body = resp.content
    if not body:
        return []   # 当日弹幕池快照为空（索引列出但无内容），按合法空日处理，不计失败
    if body[0] != 0x0A:
        raise ValueError(f"响应体不符合 DmSegMobileReply 特征（长度 {len(body)}），疑似错误页")
    return parse_danmaku_proto(body)


def fetch_history_danmaku(cid: int, client: BiliAPIClient, pubdate: Optional[int] = None,
                          bvid: str | None = None, seen_dmids: set | None = None,
                          page: int = 1) -> list[dict]:
    """
    采集视频历史弹幕（弹幕池快照逐日遍历；cid 为待采分P 的 cid）

    从 pubdate 月份起到当前月逐月查询日期索引，再逐日拉取 seg.so。月份与月内日期
    均按降序遍历：天数上限（HISTORY_MAX_DAYS）耗尽时保留最新日期（近期弹幕对画像
    价值更高）。返回的是原始快照合并结果（相邻日快照可能重叠），调用方需按 dmid 去重。
    单月/单日失败打印警告跳过并计入失败清单（failed_dates 记账），不中断。

    bvid 提供时启用断点续采：逐日增量落库（append_danmaku 按 dmid 去重）+
    phase_state 检查点（last_date 高水位 / fetched_dates 已采日期集 / failed_dates
    失败日清单）。续采判据以已采日期集为准（last_date 兼容旧检查点）：失败日不被
    高水位跳过，每次采集（含续采）都会重试补采，成功即销账。

    page 为 cid 对应的分P 序号（弹幕池按 cid 维度，多分P 视频每个分P 各采一份）：
    落库的每条弹幕带上 page，检查点键按 _state_key 分P 隔离——分P 1 沿用原键名，
    既有报告不会因升级而重采分P 1；其余分P 从零开始独立续采。

    done 语义：仅当时间窗完整迭代且无任何失败才写 done=1（截断属设计性上限，
    done 照写但另写 truncated=1 供查询区分"采完"与"采到上限"）；有失败或月份
    索引不完整时不写 done，保留续采入口。done=1 的视频重复调用时回拨检查点，
    滚动补采最近 HISTORY_RECENT_REFRESH_DAYS 天（弹幕池每日滚动补新弹幕，dmid 幂等去重），
    failed_dates 非空也照常补采，完成后保持 done=1。

    seen_dmids 为实时池/库中已有 dmid 集合（None 时从库加载），跨调用复用。

    Returns:
        本轮新采集的弹幕 dict 列表（续采时只含新日期部分；全量请读库）
    """
    if not HISTORY_DANMAKU_ENABLED:
        return []

    last_date = None
    done_before = False
    fetched_dates: set[str] = set()
    failed_dates: set[str] = set()
    if bvid:
        from storage import (append_danmaku, get_phase_state, load_danmaku,
                             set_phase_state)
        last_date = get_phase_state(bvid, "danmaku", _state_key("last_date", page)) or None
        done_before = get_phase_state(bvid, "danmaku", _state_key("done", page)) == "1"
        fetched_dates = _load_date_set(bvid, "fetched_dates", page)
        failed_dates = _load_date_set(bvid, "failed_dates", page)
        if seen_dmids is None:
            seen_dmids = {r["dmid"] for r in load_danmaku(bvid) if r["dmid"]}
    seen_dmids = seen_dmids if seen_dmids is not None else set()
    tag = f"P{page} " if page > 1 else ""   # 日志前缀：多分P 时标明是哪个分P 在采

    # B站弹幕快照按北京时间划日：显式 UTC+8，消除宿主机时区依赖
    today = datetime.now(timezone(timedelta(hours=8))).date()

    # done=1 的已完成视频重复调用：回拨检查点滚动补采最近 HISTORY_RECENT_REFRESH_DAYS 天
    # （dmid 幂等去重，重复调用快速；last_date 仍是历史高水位，不落库回拨值）
    # 回拨不设 last_date >= today 前置条件：跨天重跑时（last_date 停在昨天）若不回拨，
    # 已达 HISTORY_MAX_DAYS 上限的视频会在天数检查处直接 break——滚动窗口一天也补不进，
    # 新增弹幕永久冻结（refresh_mode 豁免上限，见下方建清单处）
    refresh_mode = done_before
    if refresh_mode:
        cutoff = (today - timedelta(days=HISTORY_RECENT_REFRESH_DAYS)).isoformat()
        fetched_dates = {d for d in fetched_dates if d <= cutoff}
        last_date = cutoff
        refresh_from = today - timedelta(days=HISTORY_RECENT_REFRESH_DAYS - 1)
        print(f"[历史弹幕] {tag}已采集完成，滚动补采最近 {HISTORY_RECENT_REFRESH_DAYS} 天"
              f"（{refresh_from.isoformat()} ~ {today.isoformat()}，弹幕池每日滚动，dmid 幂等去重）")
    elif bvid and last_date:
        if fetched_dates:
            print(f"[历史弹幕] {tag}断点续采：已采 {len(fetched_dates)} 个日期（最新 {last_date}），"
                  f"未采日期与失败日将续采")
        else:
            # 旧版检查点（无日期集）：只能以高水位判断，语义与升序采集时代一致
            print(f"[历史弹幕] {tag}断点续采（旧版检查点）：{last_date} 及以前的日期视为已完成，"
                  f"新日期与失败日将续采")

    # 续采高水位快照：进入循环前固定，循环内 last_date 只增用于落库持久化——
    # 降序遍历中若用活值做跳过判据，会先推高水位再把同轮更早日全部误跳过
    resume_before = last_date

    end_ym = (today.year, today.month)
    if pubdate:
        # 与 today 一致按北京时间换算发布月：宿主机非东八区时月界会整体偏一个月
        start = datetime.fromtimestamp(pubdate, timezone(timedelta(hours=8)))
        start_ym = (start.year, start.month)
    else:
        # 无发布时间时只从当月回溯 HISTORY_MAX_MONTHS 个月
        y, m = end_ym
        m -= HISTORY_MAX_MONTHS - 1
        while m <= 0:
            y, m = y - 1, m + 12
        start_ym = (y, m)

    months = _month_range(start_ym, end_ym)
    if len(months) > HISTORY_MAX_MONTHS:
        # 超上限时保留最近的月份（近期弹幕对画像价值更高）
        months = months[-HISTORY_MAX_MONTHS:]
        print(f"[历史弹幕] {tag}时间跨度超限，仅回溯最近 {HISTORY_MAX_MONTHS} 个月（{months[0]} 起）")

    all_danmaku = []
    # 已采天数以唯一日期集为准（len(fetched_dates)），不再读 fetched_days 检查点：
    # 计数器口径下滚动补采的近几天会被重复计入，长期重跑会虚涨并提前触发天数上限
    fetched_days = len(fetched_dates)
    truncated = False               # 天数上限耗尽：更早日期未采集（设计性截断）
    window_complete = True          # 月份索引全部成功才算时间窗完整

    # 1) 先串行建待采日期清单（月份索引每月一个请求，远少于天数）；
    #    月份降序 + 月内日期降序：上限耗尽保留最新日期。
    #    refresh_mode 豁免天数上限：滚动补采窗口固定为 cutoff 之后的几天（≤3 天），
    #    上限管的是"回溯多早"而非"补采几条"；已达上限的 done 视频不豁免则永远补不进新弹幕
    work_dates: list[str] = []
    for month in reversed(months):
        if not refresh_mode and fetched_days + len(work_dates) >= HISTORY_MAX_DAYS:
            truncated = True
            break
        try:
            dates = _fetch_month_dates(cid, month, client)
        except Exception as e:
            # 网络/风控异常：该月按失败处理（不写 done），并立即落盘挂账待下轮补采
            print(f"[历史弹幕] {tag}警告：{month} 月份索引请求异常（{e}），跳过该月")
            window_complete = False
            if bvid:
                _save_date_set(bvid, "failed_dates", failed_dates, page)
            continue
        if dates is None:
            # 索引返回失败（code != 0，非异常路径）：该月按失败处理，保留续采入口。
            # ⚠️ 不能与"本月无弹幕"的空列表同等处理——那样 window_complete 仍为 True，
            # 该月日期从未进入待采清单，最后却在 :433 照写 done=1，之后滚动补采
            # 只回拨最近几天，该月弹幕就此永久静默丢失且不再重试。
            window_complete = False
            if bvid:
                _save_date_set(bvid, "failed_dates", failed_dates, page)
            continue
        if not dates:
            continue   # 该月确实无弹幕（空列表），属合法空月
        for date in sorted(dates, reverse=True):
            if not refresh_mode and fetched_days + len(work_dates) >= HISTORY_MAX_DAYS:
                truncated = True
                break
            # 续采判据：有日期集时**只认日期集**。降序遍历下 last_date 是"最新已采日"，
            # 拿它当高水位会把中断点之前、从未采集的更早日期整段误跳过（永久漏采且照写 done）。
            # 高水位兜底只保留两种语义成立的场景：旧版检查点（无日期集，升序时代
            # "last_date 及以前已完成"成立）、已完成视频的滚动补采（done=1 时窗口内
            # 日期均已处置，含设计性截断——不该被重新翻出来补采）。
            watermark_skip = bool(resume_before) and (done_before or not fetched_dates)
            if date not in failed_dates and (
                    date in fetched_dates or (watermark_skip and date <= resume_before)):
                continue            # 该日已落库（失败日除外：不被高水位跳过，重试补采）
            work_dates.append(date)

    # 2) 按日并发采集：历史快照每天一个独立请求、无游标链，组合池多号分片时
    #    按账号并行（限速按号独立，吞吐≈账号数倍）；单池/裸 client 时串行照旧。
    #    网络在 worker 线程，落库/检查点推进在主线程 _apply 内串行完成（线程安全）。
    shards = client.shard_pools() if hasattr(client, "shard_pools") else []
    if len(shards) > 1:
        print(f"[历史弹幕] {tag}多号并行分片: {len(shards)} 个账号并行采集 {len(work_dates)} 天")

    def _one_day(args):
        idx, date = args
        sp = shards[idx % len(shards)] if len(shards) > 1 else client
        try:
            dms = _fetch_day_danmaku(cid, date, sp)
            # 落库前打上分P 标记：danmaku.time 是分P 内相对秒数，必须能追溯出自哪个分P
            for d in dms:
                d["page"] = page
            return date, dms, None
        except Exception as e:
            return date, None, e

    def _apply(date, dms, err):
        nonlocal fetched_days, last_date
        if err is not None:
            # 降级而非中断：单日失败仅跳过该日并记账，后续运行优先补采；
            # 挂账立即落盘——否则中途异常退出会让本次失败日无人记账
            print(f"[历史弹幕] {tag}警告：{date} 弹幕采集失败，已跳过: {err}")
            failed_dates.add(date)
            if bvid:
                _save_date_set(bvid, "failed_dates", failed_dates, page)
            return
        failed_dates.discard(date)
        all_danmaku.extend(dms)
        if bvid:
            # 逐日增量落库 + 检查点：中断后重跑按已采日期集续采
            fetched_dates.add(date)
            fetched_days = len(fetched_dates)   # 唯一已采日数：滚动补采的重采日不重复计数
            append_danmaku(bvid, dms, seen_dmids)
            if not last_date or date > last_date:
                last_date = date   # 高水位线
            set_phase_state(bvid, "danmaku", _state_key("last_date", page), last_date)
            set_phase_state(bvid, "danmaku", _state_key("fetched_days", page), str(fetched_days))
            _save_date_set(bvid, "fetched_dates", fetched_dates, page)
            _save_date_set(bvid, "failed_dates", failed_dates, page)
        else:
            fetched_days += 1
        print(f"[历史弹幕] {tag}{date}: {len(dms)} 条（已采日数 {fetched_days}，本轮新采 {len(all_danmaku)} 条）")

    if work_dates:
        if len(shards) > 1:
            ex = ThreadPoolExecutor(max_workers=len(shards))
            cancel = False
            try:
                futs = [ex.submit(_one_day, a) for a in enumerate(work_dates)]
                for fut in as_completed(futs):
                    _apply(*fut.result())
            except BaseException:
                # Ctrl+C / 落库或检查点写失败等：取消排队任务并立即收尾，
                # 异常继续向上传播由调用方降级。在途日期的结果无人消费，
                # 但未落库即未计入 fetched_dates，下轮重跑按日期集自动补采。
                cancel = True
                raise
            finally:
                ex.shutdown(wait=not cancel, cancel_futures=cancel)
        else:
            for a in enumerate(work_dates):
                _apply(*_one_day(a))

    if truncated:
        print(f"[历史弹幕] {tag}已达天数上限 {HISTORY_MAX_DAYS}，更早的日期未采集（上限耗尽保留最新日期）")

    if bvid:
        _save_date_set(bvid, "failed_dates", failed_dates, page)
        if truncated:
            # 截断语义：已达 HISTORY_MAX_DAYS 上限，更早日期属设计性放弃而非失败——
            # done 照写（重跑不会也不应去补更早日期），truncated=1 单独可查
            set_phase_state(bvid, "danmaku", _state_key("truncated", page), "1")
        if done_before:
            pass   # 滚动补采路径：保持 done=1
        elif window_complete and not failed_dates:
            # 时间窗完整迭代且无任何失败（含截断场景）才标完成；否则保留续采入口
            set_phase_state(bvid, "danmaku", _state_key("done", page), "1")
        else:
            print(f"[历史弹幕] {tag}时间窗未完整采集（失败 {len(failed_dates)} 天"
                  f"{'' if window_complete else '，含月份索引失败'}），不写 done，重跑可续采")
    print(f"[历史弹幕] {tag}共采集 {fetched_days} 天，{len(all_danmaku)} 条历史弹幕")
    return all_danmaku
