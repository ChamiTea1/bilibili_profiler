"""
评论采集模块：提取评论区明文UID，用于mid_hash交叉验证

主评论走新接口 /x/v2/reply/wbi/main（wbi签名 + pagination_str 游标翻页），
失败时降级回旧接口 /x/v2/reply/main（next 游标）；子评论在主评论 rcount
超过内嵌预览数时调 /x/v2/reply/reply 按 pn 补采。每条评论提取 IP 属地
（reply_control.location）。
"""
import json
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed

from api_client import BiliAPIClient
from config import COMMENT_MAIN_WBI_URL, COMMENT_MAIN_URL, COMMENT_REPLY_URL
from config import MAX_COMMENT_PAGES, COMMENT_REPLY_MAX_PAGES, CHARGE_LIST_URL
from config import COMMENT_REFRESH_MAX_PAGES, UID_HARVEST_MAX_PAGES
from storage import (get_db, get_phase_state, load_comments, save_comments,
                     set_phase_state, save_global_uid, load_global_uid_map)
from uid_resolver import calc_crc32


def _parse_comment(r: dict, is_sub: bool) -> dict | None:
    """解析单条评论（主/子结构相同），无有效成员信息时返回 None"""
    if not r or not r.get("member"):
        return None

    member = r["member"]
    # member.mid 实测为 str（如 '888465'），统一归一化为 int：SQLite 缓存路径的
    # uid 是 int，str/int 混杂会导致属地注入（uid in location_map）与跨 run
    # 去重（seen_uids）静默失效；转换失败的脏数据按无有效成员处理
    try:
        mid = int(member.get("mid") or 0)
    except (TypeError, ValueError):
        return None
    if mid == 0:
        return None

    return {
        "uid": mid,
        # rpid 缺失/为0时以 id 字段兜底：多条 rpid=0 会在 UNIQUE(bvid, rpid, uid) 上塌缩互踩
        "rpid": r.get("rpid") or r.get("id", 0),
        "uname": member.get("uname", ""),
        "sign": member.get("sign", ""),
        "level": (member.get("level_info") or {}).get("current_level", 0),
        "avatar": member.get("avatar", ""),
        "content": (r.get("content") or {}).get("message", ""),
        "like": r.get("like", 0),
        "reply_count": 0 if is_sub else r.get("rcount", 0),
        "ctime": r.get("ctime", 0),
        "is_sub": is_sub,
        "parent_rpid": r.get("parent", 0) if is_sub else 0,  # 直接父级 rpid（回复树缩进用）
        "location": (r.get("reply_control") or {}).get("location", ""),
    }


def _rpid_of(r: dict):
    """主/子评论的 rpid 归一：缺 rpid 的脏行回退 id 字段；两者皆无返回 None。

    去重判定统一走本函数——直接用 r.get("rpid") 会让多条缺 rpid 的行在集合里
    塌缩成同一个 None，把"整页脏行"误判成"整页已采过的真重复页"而提前终止翻页。"""
    return r.get("rpid") or r.get("id") or None
def _fetch_sub_replies(oid: int, root_rpid, rcount: int, preview: list[dict],
                       client: BiliAPIClient) -> list[dict]:
    """补采子评论：rcount 超过内嵌预览数时按 pn 翻页拉取全量子评论。

    补采成功（拿到比预览更多的子评论）时以补采结果替换预览（reply/reply 返回
    全量通常含预览条目，替换避免重复）；但 rcount 超过补采上限被截断时，按热度
    排序的预览条目可能不在已采前 N 页内，需把缺失的预览条目追加回去。
    补采失败/无新增时保留预览，降级不中断。
    """
    if rcount <= len(preview):
        return preview

    print(f"[Comment] 补采子评论 (root={root_rpid} 共{rcount}条)...")
    fetched = []
    total = 0   # 循环前初始化：首请求即失败 break 时 for-else 不触发，防御引用未赋值
    for pn in range(1, COMMENT_REPLY_MAX_PAGES + 1):
        try:
            data = client.get(COMMENT_REPLY_URL, params={
                "type": 1,
                "oid": oid,
                "root": root_rpid,
                "pn": pn,
                "ps": 20,
            })
        except Exception as e:
            # 单个主楼的子评论补采失败：保留内嵌预览降级，不中断整轮采集
            print(f"[Comment] 子评论补采请求异常 (root={root_rpid} pn={pn}): {e}，保留预览")
            break
        if data.get("code") != 0:
            print(f"[Comment] 子评论补采失败 (root={root_rpid} pn={pn}): {data.get('message')}，保留预览")
            break

        # 防御 data["data"] 为 None（风控/空结果时 API 会返回 data: null）
        page_data = data.get("data") or {}
        replies = page_data.get("replies") or []
        for sub in replies:
            c = _parse_comment(sub, is_sub=True)
            if c:
                fetched.append(c)

        # data.page.count 为子评论总数，翻够页数或本页为空即终止
        total = (page_data.get("page") or {}).get("count", 0)
        if not replies or pn * 20 >= total:
            break
    else:
        # 循环耗尽仍未翻完：子评论被补采上限截断
        if total > COMMENT_REPLY_MAX_PAGES * 20:
            print(f"[Comment] 子评论补采达上限 {COMMENT_REPLY_MAX_PAGES} 页 (root={root_rpid} rcount={total})，剩余截断")

    if len(fetched) <= len(preview):
        return preview

    # 截断边界：rcount 超过补采上限时，按热度排序的预览条目可能不在已采
    # 结果内，把缺失的预览条目（按 rpid 判重）追加回去，避免丢其 UID/属地
    fetched_rpids = {c["rpid"] for c in fetched}
    for c in preview:
        if c["rpid"] not in fetched_rpids:
            fetched.append(c)
    return fetched


def _collect_page(replies: list, oid: int, client: BiliAPIClient, executor=None) -> list[dict]:
    """解析一页主评论及其子评论（含补采）。

    子评论补采并行化：各主楼的楼中楼补采互相独立（wbi 主评论游标翻页是链式依赖
    无法分片，这里才是时间大头——实测子评论约占评论总量 3/4），client 为组合池
    且有多个分片时按账号分片并发补采（限速按号独立，吞吐≈账号数倍）；
    单池/裸 client 时保持串行。组装顺序不变：主评论紧跟其子评论。

    executor 由调用方按整次采集创建并复用（逐页建线程池会造成线程组反复创建销毁）。"""
    # 1) 主评+内嵌预览先按页序落位，需补采的主楼记录待办
    page_items: list[tuple[dict, list]] = []   # (主评论, 预览子评论)
    pending: list[tuple] = []                  # (rpid, rcount, preview)
    for r in replies:
        main = _parse_comment(r, is_sub=False)
        if not main:
            continue
        preview = []
        for sub in r.get("replies") or []:
            c = _parse_comment(sub, is_sub=True)
            if c:
                preview.append(c)
        page_items.append((main, preview))
        if main["reply_count"] > len(preview):
            pending.append((main["rpid"], main["reply_count"], preview))

    # 2) 补采（并发或串行）
    subs_map: dict = {}
    if pending:
        shards = client.shard_pools() if hasattr(client, "shard_pools") else []
        if len(shards) > 1 and executor is not None:
            futs = {executor.submit(_fetch_sub_replies, oid, rpid, rc, pv, shards[i % len(shards)]): rpid
                    for i, (rpid, rc, pv) in enumerate(pending)}
            for fut in as_completed(futs):
                subs_map[futs[fut]] = fut.result()
        else:
            for rpid, rc, pv in pending:
                subs_map[rpid] = _fetch_sub_replies(oid, rpid, rc, pv, client)

    # 3) 组装：子评论记录所属主评论 root_rpid（「高回复评论」页据此关联争议主楼与回复）
    comments = []
    for main, preview in page_items:
        comments.append(main)
        subs = subs_map.get(main["rpid"], preview)
        for c in subs:
            c["root_rpid"] = main["rpid"]
        comments.extend(subs)
    return comments


def _sub_reply_executor(client) -> ThreadPoolExecutor | None:
    """组合池多分片时为子评论补采创建线程池（整次采集共享一个，由调用方 try/finally 关闭）"""
    shards = client.shard_pools() if hasattr(client, "shard_pools") else []
    return ThreadPoolExecutor(max_workers=len(shards)) if len(shards) > 1 else None


def _fetch_comments_wbi(oid: int, client: BiliAPIClient, max_pages: int,
                        bvid: str | None = None, resume_offset: str = "",
                        resume_page: int = 0, executor=None) -> list[dict] | None:
    """wbi/main 游标翻页采集主评论。本轮首个请求（含续采第一页）失败返回 None
    （调用方降级旧接口）。

    bvid 提供时启用断点续采：每页落库 + phase_state 记录游标/页码/模式，
    中断后重跑从 resume_offset 继续翻页（已入库评论靠 UNIQUE 约束去重）。
    自然终止（is_end/无新评论/真重复页）才写 done=1；中途失败、页数耗尽
    截断（另写 truncated=1 供查询）、本轮 0 请求（resume_page 已达上限）
    均不写 done，下次重跑仍从最后游标续采。"""
    all_comments = []
    offset = resume_offset
    natural_end = True   # 错误中断/截断/零请求时不写 done，下次重跑续采
    made_request = False      # 本轮是否实际发出过请求（resume_page 已达上限时循环体不执行）
    first_request = True      # 本轮第一个请求失败即整体降级旧接口
    # 真重复页检测的 rpid 集合：该接口的 next_offset 可能连续多页相同但内容不同（实测），
    # 只有"整页无新 rpid"才是真的重复页。续采时从库重建（此前运行已落库的评论也算
    # "见过"），否则续采首页无法识别为重复页、子评论也会被重复补采
    seen_rpids = set()
    if bvid and (resume_offset or resume_page):
        with closing(get_db()) as conn:
            seen_rpids = {row["rpid"] for row in conn.execute(
                "SELECT rpid FROM comments WHERE bvid = ?", (bvid,))}
    if resume_offset:
        print(f"[Comment] 断点续采：从第 {resume_page + 1} 页的游标继续")

    for page in range(resume_page + 1, max_pages + 1):
        made_request = True
        try:
            data = client.get(COMMENT_MAIN_WBI_URL, params={
                "oid": oid,
                "type": 1,
                "mode": 3,       # 按热度排序
                # pagination_str 为 JSON 字符串参数，client.get 的 params 会 urlencode
                "pagination_str": json.dumps({"offset": offset}),
            })
        except Exception as e:
            # 网络抖动/组合池风控兜底耗尽：与"接口返回错误码"同口径降级——首请求异常
            # 整体降级旧接口，中途异常保留已采部分且不写 done，下次重跑续采
            print(f"[Comment] wbi/main 第{page}页请求异常（{e}）")
            natural_end = False
            if first_request:
                return None
            break

        if data.get("code") != 0:
            print(f"[Comment] wbi/main 获取评论失败 (第{page}页): {data.get('message')}")
            natural_end = False
            if first_request:
                return None  # 本轮首个请求即失败（签名/风控等），整体降级旧接口
            break            # 中途失败保留已采部分（不写 done，下次重跑续采）
        first_request = False

        # 防御 data["data"] 为 None（风控/空结果时 API 会返回 data: null）
        page_data = data.get("data") or {}
        replies = page_data.get("replies") or []
        if not replies:
            break

        # 真重复页检测：整页 rpid 都已见过才终止（next_offset 重复不代表内容重复，实测确认）；
        # rpid 缺失的脏行回退 r.get("id")，仍无则视为新行——否则多条缺 rpid 的行会
        # 在 seen_rpids 里塌缩成同一个 None 而被误判成"真重复页"提前终止翻页
        new_replies = [r for r in replies if _rpid_of(r) not in seen_rpids]
        if not new_replies:
            print(f"[Comment] wbi/main 第{page}页无新评论（真重复页），终止翻页")
            break
        seen_rpids.update(x for x in (_rpid_of(r) for r in new_replies) if x)

        # 翻页：cursor.pagination_reply.next_offset 为不透明游标字符串，is_end 终止
        cursor = page_data.get("cursor") or {}
        next_offset = (cursor.get("pagination_reply") or {}).get("next_offset")

        page_comments = _collect_page(new_replies, oid, client, executor=executor)
        all_comments.extend(page_comments)
        if bvid:
            # 整页采完（含子评论补采）才落库+推进检查点：页内中断重跑会整页重采（UNIQUE 去重兜底）
            save_comments(bvid, page_comments)
            set_phase_state(bvid, "comment", "mode", "wbi")
            set_phase_state(bvid, "comment", "page", str(page))
            set_phase_state(bvid, "comment", "offset", next_offset or offset)
        print(f"[Comment] 第 {page}/{max_pages} 页: +{len(new_replies)} 条主评论（累计 {len(all_comments)} 条含子评论）")

        if cursor.get("is_end", False) or not next_offset:
            break
        offset = next_offset
    else:
        if made_request:
            # 循环耗尽（未提前终止）：评论区被采集上限截断——截断不是自然结束，
            # 只标 truncated=1 供查询，不写 done（重跑从最后游标续采剩余页）
            print(f"[Comment] 已达采集上限 {max_pages} 页，评论区可能未采完（可调大 MAX_COMMENT_PAGES）")
            if bvid:
                set_phase_state(bvid, "comment", "truncated", "1")
            natural_end = False

    if not made_request:
        # 本轮实际请求数为 0（resume_page 已达上限）：无法判断是否采完，不写 done
        natural_end = False

    if bvid and natural_end:
        set_phase_state(bvid, "comment", "done", "1")
    return all_comments


def _fetch_comments_legacy(oid: int, client: BiliAPIClient, max_pages: int,
                           bvid: str | None = None, resume_next: int = 0,
                           resume_page: int = 0, executor=None) -> list[dict]:
    """旧接口 /x/v2/reply/main（next 游标）采集，作为 wbi/main 失败时的降级路径。
    断点续采语义同 _fetch_comments_wbi（next 为整数页游标）。"""
    all_comments = []
    next_page = resume_next
    natural_end = True
    made_request = False   # 本轮是否实际发出过请求（resume_page 已达上限时循环体不执行）
    if resume_next:
        print(f"[Comment] 断点续采：从旧接口第 {resume_page + 1} 页继续")

    for page in range(resume_page + 1, max_pages + 1):
        made_request = True
        try:
            data = client.get(COMMENT_MAIN_URL, params={
                "type": 1,
                "oid": oid,
                "mode": 3,       # 按热度排序
                "next": next_page,
                "ps": 20,
            })
        except Exception as e:
            # 网络/组合池风控兜底耗尽：与错误码同口径降级（不写 done，保留已采部分）
            print(f"[Comment] 旧接口第{page}页请求异常（{e}），保留已采部分")
            natural_end = False
            break

        if data.get("code") != 0:
            print(f"[Comment] 获取评论失败: {data.get('message')}")
            natural_end = False
            break

        # 防御 data["data"] 为 None（风控/空结果时 API 会返回 data: null）
        page_data = data.get("data") or {}
        replies = page_data.get("replies") or []
        if not replies:
            break

        page_comments = _collect_page(replies, oid, client, executor=executor)
        all_comments.extend(page_comments)
        cursor = page_data.get("cursor") or {}
        next_page = cursor.get("next", 0)
        if bvid:
            save_comments(bvid, page_comments)
            set_phase_state(bvid, "comment", "mode", "legacy")
            set_phase_state(bvid, "comment", "page", str(page))
            set_phase_state(bvid, "comment", "offset", str(next_page))
        print(f"[Comment] 旧接口第 {page}/{max_pages} 页: +{len(replies)} 条主评论（累计 {len(all_comments)} 条含子评论）")

        if cursor.get("is_end", False) or not next_page:
            break
    else:
        if made_request:
            # 循环耗尽（未提前终止）：评论区被采集上限截断——截断不是自然结束，
            # 只标 truncated=1 供查询，不写 done（重跑从最后游标续采剩余页）
            print(f"[Comment] 已达采集上限 {max_pages} 页，评论区可能未采完（可调大 MAX_COMMENT_PAGES）")
            if bvid:
                set_phase_state(bvid, "comment", "truncated", "1")
            natural_end = False

    if not made_request:
        # 本轮实际请求数为 0（resume_page 已达上限）：无法判断是否采完，不写 done
        natural_end = False

    if bvid and natural_end:
        set_phase_state(bvid, "comment", "done", "1")
    return all_comments


def refresh_comments(oid: int, client: BiliAPIClient, bvid: str,
                     max_pages: int = COMMENT_REFRESH_MAX_PAGES) -> int:
    """评论增量刷新（评论已采完整即 done=1 的视频重跑时由阶段3调用）。

    主采集按热度序（mode=3），新评论在热度序里位置不定，无法判定增量边界；
    故刷新改用时间倒序（mode=2）从最新往回翻，整页都是已入库 rpid 即停。
    新主评论照常补采子评论；已见主评论轻量回写（ON CONFLICT 更新
    like/reply_count/uname/location，顺带刷新旧评论热度，不重复补采子评论）。
    任何失败只告警、保留已刷新部分；不写任何检查点（done 保持 1）。
    返回本轮新增主评论数。"""
    with closing(get_db()) as conn:
        seen_rpids = {row["rpid"] for row in conn.execute(
            "SELECT rpid FROM comments WHERE bvid = ?", (bvid,))}
    offset = ""
    new_total = 0
    ex = _sub_reply_executor(client)   # 子评论补采线程池整轮共享（逐页建池会反复创建销毁线程组）
    try:
        for page in range(1, max_pages + 1):
            try:
                data = client.get(COMMENT_MAIN_WBI_URL, params={
                    "oid": oid,
                    "type": 1,
                    "mode": 2,       # 时间倒序：最新评论在前，增量边界可判定
                    "pagination_str": json.dumps({"offset": offset}),
                })
            except Exception as e:
                print(f"[Comment] 增量刷新第{page}页请求异常（{e}），保留已刷新部分")
                break
            if data.get("code") != 0:
                print(f"[Comment] 增量刷新第{page}页失败: {data.get('message')}，保留已刷新部分")
                break
            page_data = data.get("data") or {}
            replies = page_data.get("replies") or []
            if not replies:
                break

            stale_src = [r for r in replies if _rpid_of(r) in seen_rpids]
            new_replies = [r for r in replies if _rpid_of(r) not in seen_rpids]
            if new_replies:
                page_comments = _collect_page(new_replies, oid, client, executor=ex)
                save_comments(bvid, page_comments)
                seen_rpids.update(x for x in (_rpid_of(r) for r in new_replies) if x)
                new_total += len(new_replies)
            # 已见主评论轻量回写：刷新 like/reply_count 等（高回复榜热度不再停在首采快照）
            stale = [c for c in (_parse_comment(r, is_sub=False) for r in stale_src) if c]
            if stale:
                save_comments(bvid, stale)
            print(f"[Comment] 增量刷新第 {page}/{max_pages} 页: +{len(new_replies)} 条新主评论")

            if not new_replies:
                break   # 整页都是已见评论：到达增量边界
            cursor = page_data.get("cursor") or {}
            next_offset = (cursor.get("pagination_reply") or {}).get("next_offset")
            if cursor.get("is_end", False) or not next_offset:
                break
            offset = next_offset
        else:
            print(f"[Comment] 增量刷新达上限 {max_pages} 页，可能还有更多新评论（可调大 COMMENT_REFRESH_MAX_PAGES）")
    finally:
        if ex is not None:
            ex.shutdown(wait=True)

    print(f"[Comment] 增量刷新完成：新增主评论 {new_total} 条")
    return new_total


def harvest_comment_uids(oid: int, client: BiliAPIClient, bvid: str,
                         max_pages: int = UID_HARVEST_MAX_PAGES) -> dict[str, int]:
    """纯 UID 收割（阶段3尾部调用）：翻评论区只提取发送者 uid → 正向 CRC32 映射，
    不落评论内容、不补采子评论——目的是提高弹幕发送者解析率（评论者即明文 UID 源），
    收割结果沉淀 global_uid_map（来源「评论收割」，明文优先级），跨视频复用。

    断点：phase_state(bvid, "uid_harvest", page/offset/mode/done)，中断/达累计上限
    （UID_HARVEST_MAX_PAGES，跨运行累计）下轮续翻。首轮启动判据：正文未被截断且
    评论表非空时收割无增量价值（全部评论者 uid 已在库），直接标 done 跳过
    （评论采集整体失败、表为空时例外——收割是唯一 UID 来源，不能跳）；
    正文被截断且有 wbi 游标时接续其热度序游标翻页——热度序与时间序的页码互不
    对应，但截断点之后恰是正文未覆盖的部分，不必从第 1 页重扫。done=1 后重跑转为
    刷新模式：时间序（mode=2）从最新往回翻，整页无新 uid（对照本视频已入库
    评论 + 全局映射库 + 本轮已收）即停。单页失败只告警中断本轮（不写 done）。
    返回本轮新收割的 {crc32_hex: uid}。"""
    done = get_phase_state(bvid, "uid_harvest", "done") == "1"
    # 已知集合：本视频已入库评论 uid + 全局映射库 uid（碰撞守卫：crc 已被占且 uid 不同则跳过）
    local_uids: set[int] = {c["uid"] for c in load_comments(bvid)}
    known_uids: set[int] = set(local_uids)
    global_map = load_global_uid_map()
    known_uids.update(v["uid"] for v in global_map.values())

    offset, page_start, mode = "", 0, 2
    if not done:
        page_start = int(get_phase_state(bvid, "uid_harvest", "page") or 0)
        offset = get_phase_state(bvid, "uid_harvest", "offset") or ""
        if page_start or offset:
            mode = int(get_phase_state(bvid, "uid_harvest", "mode") or 2)   # 续翻沿用原模式
        else:
            # 首轮启动判据：
            # 1) 正文未被截断且评论表非空 → 全部评论者 uid 已在库，专门收割无增量价值，
            #    标 done 跳过（后续重跑走刷新模式捕捉新评论者）；
            #    例外：评论采集整体失败（表空）时收割是唯一 UID 来源，不能跳；
            # 2) 正文被截断且有 wbi 游标 → 接续热度序游标翻页，不重扫已采页
            c_truncated = get_phase_state(bvid, "comment", "truncated") == "1"
            if not c_truncated and local_uids:
                print("[Comment] UID收割：评论已完整采集（无截断），评论者 UID 已在库，跳过收割")
                set_phase_state(bvid, "uid_harvest", "done", "1")
                return {}
            if (c_truncated and get_phase_state(bvid, "comment", "mode") == "wbi"
                    and (get_phase_state(bvid, "comment", "offset") or "")):
                offset = get_phase_state(bvid, "comment", "offset")
                mode = 3
                print("[Comment] UID收割：正文采集被截断，从热度序截断游标接续翻页（不重扫已采页）")
        if page_start:
            print(f"[Comment] UID收割续翻：从第 {page_start + 1} 页继续")
    else:
        print("[Comment] UID收割：刷新模式（翻至整页无新 uid 即停）")

    harvested: dict[str, int] = {}
    page = page_start
    while page < max_pages:
        page += 1
        try:
            data = client.get(COMMENT_MAIN_WBI_URL, params={
                "oid": oid,
                "type": 1,
                "mode": mode,    # 首轮沿用正文热度序游标（截断场景）或时间序；刷新固定时间序
                "pagination_str": json.dumps({"offset": offset}),
            })
        except Exception as e:
            print(f"[Comment] UID收割第{page}页请求异常（{e}），本轮中止（已收 {len(harvested)} 个）")
            break
        if data.get("code") != 0:
            print(f"[Comment] UID收割第{page}页失败: {data.get('message')}，本轮中止（已收 {len(harvested)} 个）")
            break
        page_data = data.get("data") or {}
        replies = page_data.get("replies") or []
        if not replies:
            if not done:
                set_phase_state(bvid, "uid_harvest", "done", "1")
            break

        page_new = 0
        for r in replies:
            mids = [(r.get("member") or {}).get("mid")]
            mids += [(s.get("member") or {}).get("mid") for s in (r.get("replies") or [])]
            for mid in mids:
                try:
                    uid = int(mid or 0)
                except (TypeError, ValueError):
                    continue
                if not uid or uid in known_uids:
                    continue
                crc = calc_crc32(uid)
                if crc in global_map and global_map[crc]["uid"] != uid:
                    continue        # crc 碰撞：全局库已有他人，保留先见者
                known_uids.add(uid)
                if crc not in harvested:
                    harvested[crc] = uid
                    save_global_uid(crc, uid, "评论收割")
                    page_new += 1

        if page % 20 == 0 or page_new:
            print(f"[Comment] UID收割第 {page}/{max_pages} 页: +{page_new}（本轮累计 {len(harvested)} 个）")

        if done and page_new == 0:
            break                   # 刷新模式：整页无新 uid，到达边界
        cursor = page_data.get("cursor") or {}
        next_offset = (cursor.get("pagination_reply") or {}).get("next_offset")
        if cursor.get("is_end", False) or not next_offset:
            if not done:
                set_phase_state(bvid, "uid_harvest", "done", "1")
            break
        offset = next_offset
        if not done:
            set_phase_state(bvid, "uid_harvest", "page", str(page))
            set_phase_state(bvid, "uid_harvest", "offset", offset)
            set_phase_state(bvid, "uid_harvest", "mode", str(mode))
    else:
        print(f"[Comment] UID收割达累计上限 {max_pages} 页，剩余评论区下轮续翻"
              f"（可调大 UID_HARVEST_MAX_PAGES）")

    print(f"[Comment] UID收割完成：本轮新收 {len(harvested)} 个 uid 映射")
    return harvested


def fetch_comments(oid: int, client: BiliAPIClient, max_pages: int = MAX_COMMENT_PAGES,
                   bvid: str | None = None) -> list[dict]:
    """
    获取视频评论列表（主评论+子评论）

    优先走 wbi/main 游标接口；首页即失败时降级回旧 /x/v2/reply/main 接口。

    Args:
        oid: 视频aid
        max_pages: 最大翻页数
        bvid: 提供时启用断点续采（每页落库 + phase_state 游标检查点）；
              此前中断的采集从检查点续页，已入库评论靠 UNIQUE 约束去重。
              检查点模式与本次实际走通的接口不同（wbi/legacy 互换）时忽略游标从头翻页。

    Returns:
        本轮新采评论列表（续采时只含新页部分；全量请用 load_comments 读库）
    """
    resume_offset, resume_next, resume_page = "", 0, 0
    mode = None
    if bvid:
        mode = get_phase_state(bvid, "comment", "mode")
        try:
            resume_page = int(get_phase_state(bvid, "comment", "page") or 0)
        except (TypeError, ValueError):
            # 检查点脏值：按无续采处理（从头翻页，UNIQUE 约束幂等去重兜底）
            print("[Comment] 警告：评论页码检查点脏值，忽略续采从头翻页")
            resume_page = 0
        offset_ck = get_phase_state(bvid, "comment", "offset") or ""
        if mode == "wbi":
            resume_offset = offset_ck
        elif mode == "legacy":
            try:
                resume_next = int(offset_ck or 0)
            except (TypeError, ValueError):
                print(f"[Comment] 警告：旧接口游标检查点脏值（{offset_ck!r}），忽略续采从头翻页")
                resume_next = 0
                resume_page = 0
    # 检查点 mode 与本次实际走通的接口不一致时，页码/游标一并归零从头翻页（UNIQUE 幂等）
    # 子评论补采线程池按整次采集共享（逐页建池会反复创建销毁线程组）
    ex = _sub_reply_executor(client)
    try:
        comments = _fetch_comments_wbi(oid, client, max_pages, bvid=bvid,
                                       resume_offset=resume_offset,
                                       resume_page=resume_page if mode == "wbi" else 0,
                                       executor=ex)
        if comments is None:
            print("[Comment] wbi/main 接口不可用，降级为旧版 /x/v2/reply/main")
            comments = _fetch_comments_legacy(oid, client, max_pages, bvid=bvid,
                                              resume_next=resume_next,
                                              resume_page=resume_page if mode == "legacy" else 0,
                                              executor=ex)
        return comments
    finally:
        if ex is not None:
            ex.shutdown(wait=True)


def build_comment_uid_map(comments: list[dict]) -> dict[str, int]:
    """
    构建 CRC32(mid) -> mid 映射表，用于弹幕mid_hash交叉验证

    Returns:
        {crc32_hash: uid} 映射字典
    """
    uid_map = {}
    seen_uids = set()

    for c in comments:
        uid = c["uid"]
        if uid in seen_uids:
            continue
        seen_uids.add(uid)

        # 计算该UID的CRC32（B站使用的标准CRC32）
        crc = calc_crc32(uid)
        # CRC32碰撞时保留先见者并告警，避免弹幕发送者被误归属到后到的用户
        if crc in uid_map and uid_map[crc] != uid:
            print(f"[Comment] 警告: CRC32碰撞 {crc}，已归属UID {uid_map[crc]}，忽略 {uid}")
        else:
            uid_map.setdefault(crc, uid)

    return uid_map


def build_comment_location_map(comments: list[dict]) -> dict[int, str]:
    """
    构建 uid -> IP属地 映射表（如 "IP属地：江苏"），供画像地域维度使用

    同一用户多条评论属地不一致时保留先见者（与 build_comment_uid_map 同策略）。
    """
    location_map = {}
    for c in comments:
        uid = c["uid"]
        location = c.get("location", "")
        if location and uid not in location_map:
            location_map[uid] = location
    return location_map


def collect_comment_data(aid: int, client: BiliAPIClient,
                         bvid: str | None = None) -> tuple[list[dict], dict[str, int], dict[int, str]]:
    """
    采集评论数据并构建UID映射与IP属地映射

    Args:
        bvid: 提供时启用断点续采（fetch_comments 内部逐页落库+游标检查点），
              返回值为库存全量评论（含此前已入库部分）

    Returns:
        (comments_list, crc32_to_uid_map, uid_to_location_map)
    """
    print(f"[Comment] 获取评论区 (AID:{aid})...")
    if bvid:
        # 新格式哨兵：先于首页落库写标记，与"本功能前旧版完整落库"区分开——
        # 否则中断点早于首个游标检查点时，重跑会被误判为完整而跳过剩余翻页
        set_phase_state(bvid, "comment", "format", "v2")
    comments = fetch_comments(aid, client, bvid=bvid)
    if bvid:
        # 续采路径 fetch 只返回本轮新页；全量从库读回
        comments = load_comments(bvid)
    uid_map = build_comment_uid_map(comments)
    location_map = build_comment_location_map(comments)
    sub_count = sum(1 for c in comments if c.get("is_sub"))
    print(f"[Comment] 获取到 {len(comments)} 条评论（含子评论 {sub_count} 条），"
          f"提取 {len(uid_map)} 个独立用户UID，{len(location_map)} 个IP属地")
    return comments, uid_map, location_map


def fetch_charge_uid_map(bvid: str, aid: int, up_mid: int, client) -> dict:
    """
    视频充电鸣谢名单 → crc32->UID 映射（明文 UID 源，置信度同评论验证）

    充电用户是重度粉丝，与弹幕发送者重合率高。名单仅含本月充电用户；
    无充电数据（code=62001）或接口异常时返回空映射，零成本降级。

    Returns:
        {crc32_hex: uid}
    """
    uid_map = {}
    try:
        data = client.get(CHARGE_LIST_URL, params={"mid": up_mid, "aid": aid})
        if data.get("code") != 0:
            # 62001=不需要展示充电信息（无充电数据），属正常情况不告警
            if data.get("code") != 62001:
                print(f"[Comment] 充电名单获取失败: {data.get('message')}（降级跳过）")
            return {}
        for item in (data.get("data") or {}).get("list") or []:
            # 单条脏数据跳过，不归零整批
            try:
                pay_mid = int(item.get("pay_mid") or 0)
            except (TypeError, ValueError):
                continue
            if pay_mid:
                uid_map[calc_crc32(pay_mid)] = pay_mid
        if uid_map:
            print(f"[Comment] 充电名单: {len(uid_map)} 个明文UID")
    except Exception as e:
        print(f"[Comment] 充电名单获取异常: {e}（降级跳过）")
        return {}
    return uid_map
