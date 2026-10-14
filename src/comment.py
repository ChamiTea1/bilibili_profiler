"""
评论采集模块：提取评论区明文UID，用于mid_hash交叉验证

主评论走新接口 /x/v2/reply/wbi/main（wbi签名 + pagination_str 游标翻页），
失败时降级回旧接口 /x/v2/reply/main（next 游标）；子评论在主评论 rcount
超过内嵌预览数时调 /x/v2/reply/reply 按 pn 补采。每条评论提取 IP 属地
（reply_control.location）。
"""
import json
import config
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed

from api_client import BiliAPIClient, RiskControlError
from config import COMMENT_MAIN_WBI_URL, COMMENT_MAIN_URL, COMMENT_REPLY_URL
from config import MAX_COMMENT_PAGES, COMMENT_REPLY_MAX_PAGES, CHARGE_LIST_URL
from config import COMMENT_REFRESH_MAX_PAGES, UID_HARVEST_MAX_PAGES
from storage import (get_db, get_phase_state, load_comments, load_comment_uids,
                     save_comments, set_phase_state, load_uid_harvest_seen, save_uid_harvest_page)
from uid_resolver import calc_crc32
from uid_evidence import add_uid, candidates_of


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
        # （count 可能为 null——风控/异常响应下 `pn * 20 >= None` 会 TypeError 逃逸）
        total = (page_data.get("page") or {}).get("count") or 0
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
    自然终止（is_end 为真，或有评论且接口不再给下一页游标）才写 done=1。
    code=0 但 replies 为空、且 is_end 不为真时不算结束：风控降级也会返回空列表。
    中途失败、页数耗尽截断（另写 truncated=1 供查询）、本轮 0 请求
    （resume_page 已达上限）均不写 done，下次重跑仍从最后游标续采。"""
    all_comments = []
    offset = resume_offset
    natural_end = True   # 错误中断/截断/零请求时不写 done，下次重跑续采
    made_request = False      # 本轮是否实际发出过请求（resume_page 已达上限时循环体不执行）
    first_request = True      # 本轮第一个请求失败即整体降级旧接口
    # 真重复页检测的 rpid 集合：该接口的 next_offset 可能连续多页相同但内容不同（实测），
    # 只有"整页无新 rpid"才是真的重复页。续采时从库重建（此前运行已落库的评论也算
    # "见过"），否则续采首页无法识别为重复页、子评论也会被重复补采
    seen_rpids = set()
    seen_this_run = set()
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
            # 空列表且 is_end 不为真：不能把 natural_end 留成 True。对齐 UID 收割对
            # 「空结构且非 is_end」的处理，风控降级同样会给出这种空页。
            if not (page_data.get("cursor") or {}).get("is_end"):
                natural_end = False
            break

        # 本轮重复页终止；数据库已有页可能是旧版中断重放，仍须继续推进游标。
        # rpid 缺失的脏行回退 r.get("id")，仍无则视为新行——否则多条缺 rpid 的行会
        # 在 seen_rpids 里塌缩成同一个 None 而被误判成"真重复页"提前终止翻页
        new_replies = [r for r in replies if _rpid_of(r) not in seen_rpids]
        if not new_replies and all(_rpid_of(r) in seen_this_run for r in replies):
            print(f"[Comment] wbi/main 第{page}页重复本轮已采页面，终止翻页")
            natural_end = False
            break
        seen_this_run.update(x for x in (_rpid_of(r) for r in replies) if x)
        seen_rpids.update(x for x in (_rpid_of(r) for r in new_replies) if x)

        # 翻页：cursor.pagination_reply.next_offset 为不透明游标字符串，is_end 终止
        cursor = page_data.get("cursor") or {}
        next_offset = (cursor.get("pagination_reply") or {}).get("next_offset")

        page_comments = _collect_page(new_replies, oid, client, executor=executor)
        all_comments.extend(page_comments)
        if bvid:
            # 整页采完（含子评论补采）才落库+推进检查点：页内中断重跑会整页重采（UNIQUE 去重兜底）
            save_comments(bvid, page_comments, checkpoint={"mode": "wbi", "page": page,
                "offset": next_offset or offset, "done": "1" if cursor.get("is_end", False) or not next_offset else "0",
                "truncated": "0"})
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
            # 与 wbi 路径相同：空 replies 只有 is_end 才算自然结束。
            if not (page_data.get("cursor") or {}).get("is_end"):
                natural_end = False
            break

        page_comments = _collect_page(replies, oid, client, executor=executor)
        all_comments.extend(page_comments)
        cursor = page_data.get("cursor") or {}
        next_page = cursor.get("next", 0)
        if bvid:
            save_comments(bvid, page_comments, checkpoint={"mode": "legacy", "page": page,
                "offset": next_page, "done": "1" if cursor.get("is_end", False) or not next_page else "0",
                "truncated": "0"})
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
    新主评论照常补采子评论；已见主楼保存新预览，有回复缺口时补采，
    同时回写 like/reply_count/uname/location，刷新旧评论热度。
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
            with closing(get_db()) as conn:
                sub_counts = {r["root_rpid"]: r["n"] for r in conn.execute(
                    "SELECT root_rpid, COUNT(*) AS n FROM comments WHERE bvid=? AND is_sub=1 GROUP BY root_rpid",
                    (bvid,))}
            growing = [r for r in stale_src if int(r.get("rcount") or 0) > sub_counts.get(_rpid_of(r), 0)]
            steady = [r for r in stale_src if r not in growing]
            stale = _collect_page(growing, oid, client, executor=ex) if growing else []
            for r in steady:
                c = _parse_comment(r, is_sub=False)
                if c:
                    stale.append(c)
                for sub in r.get("replies") or []:
                    c = _parse_comment(sub, is_sub=True)
                    if c:
                        c["root_rpid"] = _rpid_of(r)
                        stale.append(c)
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


def _checkpoint_int(bvid: str, phase: str, key: str, default: int = 0) -> int:
    try:
        return max(0, int(get_phase_state(bvid, phase, key) or default))
    except (TypeError, ValueError):
        return default


def harvest_comment_uids(oid: int, client: BiliAPIClient, bvid: str,
                         max_pages: int = UID_HARVEST_MAX_PAGES) -> dict:
    """以本视频已见评论 ID 判断刷新边界，全局已知 UID 仍记录本视频证据。

    每页 UID、评论 ID 与游标原子提交；刷新有独立请求预算，正常续页保持累计上限。
    """
    done = get_phase_state(bvid, "uid_harvest", "done") == "1"
    local_uids = load_comment_uids(bvid)
    seen_rpids = load_uid_harvest_seen(bvid)
    offset, page_start, mode = "", 0, 2
    if not done:
        page_start = _checkpoint_int(bvid, "uid_harvest", "page")
        offset = get_phase_state(bvid, "uid_harvest", "offset") or ""
        if page_start or offset:
            mode = _checkpoint_int(bvid, "uid_harvest", "mode", 2)
            if mode not in (2, 3):
                mode, page_start, offset = 2, 0, ""
        else:
            truncated = get_phase_state(bvid, "comment", "truncated") == "1"
            complete = (get_phase_state(bvid, "comment", "done") == "1"
                        or get_phase_state(bvid, "comment", "format") is None)
            if not truncated and complete and local_uids:
                set_phase_state(bvid, "uid_harvest", "done", "1")
                return {}
            if truncated and get_phase_state(bvid, "comment", "mode") == "wbi":
                offset = get_phase_state(bvid, "comment", "offset") or ""
                if offset:
                    mode = 3
    limit = min(max_pages, getattr(config, "UID_HARVEST_REFRESH_MAX_PAGES", 10)) if done else max_pages
    harvested = {}
    for page in range(page_start + 1, limit + 1):
        try:
            response = client.get(COMMENT_MAIN_WBI_URL, params={"oid": oid, "type": 1, "mode": mode,
                                 "pagination_str": json.dumps({"offset": offset})})
            if not isinstance(response, dict) or response.get("code") != 0 or not isinstance(response.get("data"), dict):
                break
            data = response["data"]
            replies = data.get("replies")
            if replies is None:
                if not (data.get("cursor") or {}).get("is_end"):
                    break  # 空结构可能是降级响应，不能据此写完成。
                replies = []
            if not isinstance(replies, list):
                break
            roots = {int(_rpid_of(r)) for r in replies if isinstance(r, dict) and _rpid_of(r)}
            # 没有有效 ID 的页面不能作为“已采到边界”的证据。
            boundary = done and bool(replies) and len(roots) == len(replies) and roots <= seen_rpids
            uids, rpids = set(), set()
            for r in replies:
                for item in [r, *(r.get("replies") or [])]:
                    if not isinstance(item, dict):
                        continue
                    rpid = _rpid_of(item)
                    if rpid:
                        rpids.add(int(rpid))
                    for uid in candidates_of((item.get("member") or {}).get("mid")):
                        uids.add(uid)
                        add_uid(harvested, calc_crc32(uid), uid)
            cursor = data.get("cursor") or {}
            next_offset = (cursor.get("pagination_reply") or {}).get("next_offset")
            ended = not replies or cursor.get("is_end", False) or not next_offset
            checkpoint = {} if done else {"page": page, "offset": next_offset or offset, "mode": mode,
                                           "done": "1" if ended else "0"}
            save_uid_harvest_page(bvid, uids, rpids, checkpoint)
            seen_rpids.update(rpids)
            if boundary or ended:
                break
            if next_offset == offset:
                break
            offset = next_offset
        except Exception as exc:
            print(f"[Comment] UID收割第{page}页失败（{exc}），保留已完成页并等待重跑")
            break
    print(f"[Comment] UID收割：本轮关联 {sum(len(v) for v in harvested.values())} 个明文候选")
    return harvested


def supplement_uid_replies(oid: int, client: BiliAPIClient, bvid: str, comments: list[dict],
                           target_hashes: set[str], known_map: dict) -> dict:
    """优先补采高回复但未收完整的主楼，按目标 hash 命中收益控制请求预算。

    仅收 UID，不新增正文或绕过限速；每个主楼单独保存游标，失败不写完成。
    """
    pending = {h for h in target_hashes if not candidates_of(known_map.get(h))}
    budget = max(0, getattr(config, "UID_REPLY_TOTAL_PAGES", 10))
    if not oid or not pending or not budget:
        return {}
    children = {}
    for c in comments:
        if c.get("is_sub") and c.get("root_rpid"):
            children[c["root_rpid"]] = children.get(c["root_rpid"], 0) + 1
    roots = [c for c in comments if not c.get("is_sub") and c.get("rpid")
             and c.get("reply_count", 0) > children.get(c["rpid"], 0)]
    roots.sort(key=lambda c: (c.get("reply_count", 0), c.get("like", 0)), reverse=True)
    result, requests_made, hits = {}, 0, set()
    for root in roots[:getattr(config, "UID_REPLY_MAX_ROOTS", 5)]:
        phase = f"uid_reply:{root['rpid']}"
        if get_phase_state(bvid, phase, "done") == "1":
            continue
        page = _checkpoint_int(bvid, phase, "page") + 1
        dry_pages = 0
        while budget > 0:
            budget -= 1
            requests_made += 1
            try:
                response = client.get(COMMENT_REPLY_URL, params={"oid": oid, "type": 1,
                                     "root": root["rpid"], "pn": page, "ps": 20})
                if isinstance(response, dict) and response.get("code") in (-412, -352, -403):
                    budget = 0  # 风控时整轮停采，不继续换主楼请求。
                    break
                if not isinstance(response, dict) or response.get("code") != 0 or not isinstance(response.get("data"), dict):
                    break
                data = response["data"]
                replies = data.get("replies") or []
                if not isinstance(replies, list):
                    break
                uids, rpids, page_hits = set(), set(), set()
                for r in replies:
                    if not isinstance(r, dict):
                        continue
                    rpid = _rpid_of(r)
                    if rpid:
                        rpids.add(int(rpid))
                    for uid in candidates_of((r.get("member") or {}).get("mid")):
                        uids.add(uid)
                        h = calc_crc32(uid)
                        add_uid(result, h, uid)
                        if h in pending:
                            page_hits.add(h)
                count = int((data.get("page") or {}).get("count") or 0)
                ended = not replies or (count > 0 and page * 20 >= count)
                save_uid_harvest_page(bvid, uids, rpids, {"page": page, "done": "1" if ended else "0"}, phase=phase)
                new_hits = page_hits - hits
                hits.update(page_hits)
                dry_pages = 0 if new_hits else dry_pages + 1
                if ended or hits >= pending or dry_pages >= getattr(config, "UID_REPLY_NO_HIT_PAGES", 3):
                    break
                page += 1
            except RiskControlError:
                budget = 0
                break
            except Exception as exc:
                print(f"[Comment] UID楼中楼补采失败（{exc}），保留检查点")
                break
        if not budget or hits >= pending:
            break
    print(f"[Comment] UID定向补采：{requests_made} 次请求，新增 {len(hits)} 个目标 hash 关联（仍需碰撞复核）")
    return result


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


def build_comment_uid_map(comments: list[dict]) -> dict[str, set[int]]:
    """
    构建 CRC32(mid) -> mid 映射表，用于弹幕mid_hash交叉验证

    Returns:
        {crc32_hash: {uid, ...}} 候选映射字典
    """
    uid_map = {}
    for c in comments:
        for uid in candidates_of(c.get("uid")):
            add_uid(uid_map, calc_crc32(uid), uid)

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
                add_uid(uid_map, calc_crc32(pay_mid), pay_mid)
        if uid_map:
            print(f"[Comment] 充电名单: {len(uid_map)} 个明文UID")
    except Exception as e:
        print(f"[Comment] 充电名单获取异常: {e}（降级跳过）")
        return {}
    return uid_map
