"""
问题弹幕检测（LLM 判定）

对合并去重后的全部弹幕按批喂给 LLM，判定八类问题弹幕：
中二抒情 / 尬夸捧杀 / 引战阴阳 / 人身攻击 / 恶意剧透 / 广告引流 / 键政敏感 / 批评吐槽。
按发送者聚合输出，驱动兴趣分选人与报告问题弹幕榜。
未配置 LLM_API_KEY 时返回空 dict（降级不中断）；失败批次重试链：
429 退避 → 换备用厂商 → 整轮等待重试（总耗时超 LLM_RETRY_BUDGET_SECONDS 熔断放弃剩余批次）；
鉴权/参数类致命错误直接上抛，由调用方捕获降级。

历史说明：模块与函数名沿用 cringe（尬语）命名是兼容旧调用方的最小改动，
实际判定范围已扩展为"问题弹幕"。
"""
import hashlib
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import openai
from openai import OpenAI

from config import (LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_MAX_TOKENS, LLM_CONCURRENCY,
                    LLM_FALLBACK, CRINGE_BATCH_SIZE, COMMENT_CRINGE_BATCH_SIZE,
                    COMMENT_CRINGE_MAX_ITEMS, LLM_RETRY_BUDGET_SECONDS,
                    LLM_TRANSIENT_RETRIES, LLM_BATCH_THINKING)
from storage import load_llm_cache, save_llm_cache

# 问题弹幕类别（prompt 与聚合均引用，勿散落硬编码字符串）
PROBLEM_CATEGORIES = ["中二抒情", "尬夸捧杀", "引战阴阳", "人身攻击", "恶意剧透", "广告引流", "键政敏感", "批评吐槽"]

# 判定缓存口径版本号：判定口径或缓存结构变更时 bump，旧缓存自动失效（旧孤儿键不清理）
_DM_CACHE_VERSION = "v4"    # 问题弹幕（v4：批次缓存 key 改稳定前缀，不再嵌整段 digest）
_CMT_CACHE_VERSION = "v4"   # 问题评论（v4：整段缓存值由 {rpid:verdict} 改 {content:verdict}，修复续采新增同内容评论漏标）

# 整轮重试总耗时熔断预算与瞬态重试次数已迁移至
# config.LLM_RETRY_BUDGET_SECONDS / config.LLM_TRANSIENT_RETRIES
# 致命错误（鉴权失败/权限不足/请求参数错误/资源不存在等 4xx 类）：重试与换厂商均无意义，直接上抛由调用方降级
_FATAL_LLM_ERRORS = (openai.AuthenticationError, openai.PermissionDeniedError,
                     openai.BadRequestError, openai.NotFoundError)


class _UnparseableResponse(ValueError):
    """LLM 响应为空或无法解析为 JSON 数组：恒思考模型（glm-5.3）间歇性空正文，
    属可重试的抖动，先同厂商短退避再换厂商"""


class _ContentFiltered(Exception):
    """厂商内容审核拦截（如 GLM 1301 contentFilter）：是该批内容触发的批次级失败，
    同厂商重试无意义、更不该按致命错误中止整轮判定——直接换厂商，全部厂商都被拦
    则该批按最终失败跳过（不进整轮重试，同一内容必然重复触发）"""


class _BudgetExhausted(Exception):
    """退避等待将超出 LLM_RETRY_BUDGET_SECONDS 总耗时预算：该批按最终失败跳过，
    不再让整轮重试把耗时拖过预算（预算耗尽时重跑可借批次缓存续判）"""


def _batch_thinking_extra(base_url: str, model: str) -> dict:
    """判定批次思考参数（强度由 config.LLM_BATCH_THINKING 控制：off/low/default）。
    分类任务推理增益微弱，推理 token 计费且占 max_tokens 预算，故默认 off：
    - off：deepseek 与 GLM-4.x 用 thinking:disabled，GLM-5.x 恒思考只能压 low；
    - low：deepseek 无档位概念保持原生，GLM 用 enabled+low；
    - default：不下发任何思考参数（厂商原生行为，GLM-5.x 即全量思考）。
    深掘（llm_analyzer）不走此函数，始终保默认档。"""
    mode = LLM_BATCH_THINKING
    if mode == "default":
        return {}
    if "bigmodel.cn" in base_url:
        if mode == "low":
            return {"extra_body": {"thinking": {"type": "enabled", "level": "low"}}}
        # off：GLM-4.x 可彻底关闭；5.x 恒思考（disabled 会 400）压 low 兜底
        if model.startswith("glm-4"):
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {"extra_body": {"thinking": {"type": "enabled", "level": "low"}}}
    if "deepseek" in base_url:
        # deepseek 思考只有开/关：off 显式关闭；low/default 保持原生（开）
        return {"extra_body": {"thinking": {"type": "disabled"}}} if mode == "off" else {}
    return {}


def _dedup_contents(danmaku_list: list[dict]) -> list[dict]:
    """按内容去重弹幕，附出现次数（降低 token 消耗）。返回 [{content, count}]，按次数降序"""
    counts = {}
    for dm in danmaku_list:
        c = (dm.get("content") or "").strip()
        if c:
            counts[c] = counts.get(c, 0) + 1
    items = [{"content": c, "count": n} for c, n in counts.items()]
    # 次数相同按内容字典序决胜——保证跨运行排序稳定，批次级缓存才能命中
    items.sort(key=lambda x: (-x["count"], x["content"]))
    return items


def _video_context(video_info: dict) -> str:
    """视频语境行：标题 + 简介截断，帮助 LLM 锚定游戏/社区语境（二游判定防现实语境误读）"""
    title = video_info.get("title", "未知视频")
    desc = (video_info.get("desc") or "").strip().replace("\n", " ")[:100]
    ctx = f"视频《{title}》"
    if desc:
        ctx += f"（简介：{desc}）"
    return ctx


def _build_prompt(batch: list[dict], start_idx: int, video_info: dict) -> str:
    """构建单批问题弹幕判定 prompt（编号为全局下标，便于跨批映射）"""
    lines = [f'{start_idx + i}. {it["content"]}（出现{it["count"]}次）' for i, it in enumerate(batch)]
    return f"""你是熟悉二次元游戏（原神、鸣潮等二游）社区生态的内容审核专家。以下是B站{_video_context(video_info)}的弹幕列表（已按内容去重）。
语境提示：这是二游视频，游戏世界观名词、剧情设定、角色名、卡池/强度讨论、社区黑话都属于游戏语境，不要按现实语境过度解读。
请逐条判定是否属于以下八类"问题弹幕"之一：
- 中二抒情：咯噔文学、疼痛文学、自我感动式过度抒情。发癫文学、"XX我老婆"等二游常见发电行为属正常玩梗，不算
- 尬夸捧杀：无脑吹、饭圈式夸张应援、明显违心的吹捧；对角色/剧情/演出的正常夸赞不算
- 引战阴阳：拉踩其他游戏/角色/阵营/玩家群体、对线、反串、阴阳怪气等攻击性内容
- 人身攻击：辱骂、诅咒、攻击其他观众/UP主/声优；只针对游戏内容本身的批评归「批评吐槽」类
- 恶意剧透：泄露剧情关键信息、结局、反转，或未实装角色/卡池的内鬼爆料
- 广告引流：打广告、推广、引流到其他平台或商品（含代练、卖号、私服）
- 键政敏感：把游戏内容借题引申到现实政治人物、事件、意识形态的键政引战；游戏世界观内的国家/战争/政变等设定讨论不算，歌词/台词/梗中引用的符号性词汇也不算。例：「至冬解体」是游戏国家设定联想、「火焰和钢铁--镰刀与锤子」是歌词接龙，都不算键政敏感
- 批评吐槽：对游戏剧情、角色强度、运营策划的批评、锐评、吐槽（对事不对人；含辱骂或攻击玩家/UP主的归人身攻击；此类严重度一般给1）
正常玩梗、合理讨论、普通应援不算问题弹幕，宁漏勿冤。

弹幕列表：
{chr(10).join(lines)}

请严格只输出一个 JSON 数组，每个元素对应一条判定（只输出判为问题弹幕的条目）：
[{{"i": 编号, "category": "中二抒情|尬夸捧杀|引战阴阳|人身攻击|恶意剧透|广告引流|键政敏感|批评吐槽", "severity": 1到3的整数, "reason": "10字内理由"}}]
没有问题弹幕就输出 []。不要输出任何 JSON 之外的内容。"""


def _parse_verdicts(raw_text: str) -> list[dict] | None:
    """从 LLM 响应提取 JSON 数组（容错：截取首个 [ 到末个 ]）；无法解析为 JSON 数组时返回 None"""
    left, right = raw_text.find("["), raw_text.rfind("]")
    if left == -1 or right <= left:
        return None
    try:
        data = json.loads(raw_text[left:right + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return [v for v in data if isinstance(v, dict) and "i" in v]


def _judge_batches(items: list[dict], batch_size: int, video_info: dict,
                   prompt_builder, label: str, cache_prefix: str = "") -> tuple[list[dict], int, int]:
    """并发执行 LLM 判定批次，返回 (原始verdicts, 失败批次数, 总批次数)。

    实际并发路数 = min(批次数, LLM_CONCURRENCY)——判定阶段无缓存命中，越早判完越好，
    批次少时不空占线程、批次多时打满上限。单批失败重试链：429 限速同厂商退避最多 2 次、
    网络瞬态错误（超时/连接失败）同厂商短退避重试 LLM_TRANSIENT_RETRIES 次
    → 换备用厂商（LLM_FALLBACK，双厂商 key 都配了才启用）→ 仍失败进入整轮
    等待重试（60s×轮次递增、封顶 300s）。整轮重试受总耗时预算 LLM_RETRY_BUDGET_SECONDS
    熔断：超预算放弃剩余失败批次（返回的失败批次数 >0，调用方不得写整段缓存）；
    鉴权/参数类致命错误（_FATAL_LLM_ERRORS）直接上抛，由调用方捕获降级。

    cache_prefix 提供时启用批次级缓存（key = 前缀@batch:模型名:本批内容指纹）。
    前缀须为稳定前缀（不嵌整段内容 digest），否则内容集合规模变化会使全部已完成
    批次缓存错位失效。响应先经 _parse_verdicts 校验可解析才落缓存——空串/坏响应
    视为批次失败进重试链、不落缓存；缓存命中时同样校验，损坏内容视为未命中。
    判定中途（Ctrl+C/崩溃）重跑时已完成的批次直接命中，零 LLM 调用。
    """
    # 厂商链：主用 + 备用（备用 key 为空则不启用）
    providers = [(LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, "主用")]
    if LLM_FALLBACK[0]:
        providers.append(LLM_FALLBACK)
    clients = {}            # 惰性建 client，线程间共享（openai client 线程安全）
    clients_lock = threading.Lock()

    def client_of(pi: int) -> OpenAI:
        if pi not in clients:
            with clients_lock:              # 双检：并发首用时只建一个 client
                if pi not in clients:
                    key, base, _, _ = providers[pi]
                    clients[pi] = OpenAI(api_key=key, base_url=base)
        return clients[pi]

    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    total = len(batches)
    workers = max(1, min(total, LLM_CONCURRENCY))
    # 总耗时预算：整轮重试与单批退避共用同一截止时刻，避免单轮内多次长退避把实际耗时
    # 拖到远超 LLM_RETRY_BUDGET_SECONDS（此前只在整轮间隙检查，单轮内不受约束）
    deadline = time.monotonic() + LLM_RETRY_BUDGET_SECONDS

    def _sleep_within_budget(sec: float) -> bool:
        """预算内退避：把等待压缩进剩余预算；预算已耗尽返回 False，由调用方按最终失败处理"""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(sec, remaining))
        return True

    # 注意：key 固定嵌主用厂商的 LLM_MODEL——备用厂商兜底产出的判定也会存进该命名空间
    # （跨厂商略有混样，换取中断重跑时的缓存命中率；判定口径以内容为准，与厂商基本无关，可接受）
    def batch_cache_key(bi: int) -> str:
        digest = hashlib.sha256("\n".join(sorted(
            it["content"] for it in batches[bi])).encode("utf-8")).hexdigest()[:16]
        return f"{cache_prefix}@batch:{LLM_MODEL}:{digest}"

    def work(bi: int) -> str:
        bkey = batch_cache_key(bi) if cache_prefix else ""
        if bkey:
            cached = load_llm_cache(bkey)
            if cached:      # None 与空串均视为未命中
                if _parse_verdicts(cached) is not None:
                    print(f"[{label}] 批次 {bi + 1} 命中批次缓存，跳过 LLM 请求")
                    return cached
                print(f"[{label}] 批次 {bi + 1} 缓存内容损坏（无法解析），重新判定")
        prompt = prompt_builder(batches[bi], bi * batch_size, video_info)
        last_err = None
        for pi, (_, _, model, tag) in enumerate(providers):
            if pi > 0:
                # 透出主用厂商的失败原因（类型+摘要），否则换厂商兜底成功后根因无从排查
                print(f"[{label}] 批次 {bi + 1} 主厂商失败（{type(last_err).__name__}: "
                      f"{str(last_err)[:120]}），换备用厂商（{tag}）重试...")
            transient_retries = 0
            for retry in range(3):      # 限速退避：429 时等一会再试，最多 2 次重试
                try:
                    extra = _batch_thinking_extra(providers[pi][1], model)
                    resp = client_of(pi).chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=LLM_MAX_TOKENS,
                        temperature=0.3,  # 判定类任务低温，减少格式漂移
                        **extra,
                    )
                    raw = resp.choices[0].message.content or ""
                    if _parse_verdicts(raw) is None:
                        # 空串/坏响应：不落缓存、不算成功，进同厂商短退避重试链
                        raise _UnparseableResponse(
                            f"响应无法解析为 JSON 数组（前100字符: {raw[:100]!r}）")
                    if bkey:
                        save_llm_cache(bkey, raw)
                    return raw
                except openai.RateLimitError as e:
                    last_err = e
                    if retry == 2:
                        break           # 本厂商限速重试耗尽，换下一厂商
                    wait = 10 * (retry + 1) + random.uniform(0, 3)
                    print(f"[{label}] 批次 {bi + 1} 触发限速，{wait:.0f}s 后重试...")
                    if not _sleep_within_budget(wait):
                        raise _BudgetExhausted(f"限速退避 {wait:.0f}s 超出剩余预算") from e
                except openai.BadRequestError as e:
                    if "1301" in str(e) or "contentFilter" in str(e):
                        raise _ContentFiltered(str(e)[:200]) from e   # 内容审核：批次级失败换厂商
                    raise               # 其他参数类 400 属致命错误，直接上抛
                except (openai.APITimeoutError, openai.APIConnectionError, _UnparseableResponse) as e:
                    last_err = e
                    transient_retries += 1
                    if transient_retries > LLM_TRANSIENT_RETRIES:
                        break           # 瞬态错误/空坏响应同厂商重试耗尽，换下一厂商
                    wait = 3 * transient_retries + random.uniform(0, 1)
                    print(f"[{label}] 批次 {bi + 1} 瞬态错误（{type(e).__name__}），{wait:.0f}s 后同厂商重试...")
                    if not _sleep_within_budget(wait):
                        raise _BudgetExhausted(f"瞬态退避 {wait:.0f}s 超出剩余预算") from e
                except _FATAL_LLM_ERRORS:
                    raise               # 致命错误（鉴权/参数类）重试与换厂商均无意义，直接上抛
                except Exception as e:
                    last_err = e
                    break               # 其他错误重试同厂商无意义，直接换下一厂商
        raise last_err

    verdicts: dict[int, list] = {}   # 按批次下标暂存：聚合顺序不随并发完成顺序漂移（examples 跨运行稳定）

    def run_pass(pending: list[int]) -> list[int]:
        """跑一轮并发判定，返回仍失败的批次下标；致命错误取消其余批次后直接上抛"""
        still_failed = []
        fatal = None
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(work, bi): bi for bi in pending}
            for fut in as_completed(futures):
                bi = futures[fut]
                try:
                    raw = fut.result()
                except _FATAL_LLM_ERRORS as e:
                    print(f"[{label}] 错误: 批次 {bi + 1} 致命错误（{type(e).__name__}: {e}），中止判定")
                    fatal = fatal or e
                    for f in futures:
                        f.cancel()      # 取消未开始的批次（进行中的让其自然结束）
                    continue
                except _ContentFiltered:
                    # 厂商内容审核拦截：该批内容必然重复触发，按最终失败跳过、不进整轮重试
                    print(f"[{label}] 警告: 批次 {bi + 1} 被厂商内容审核拦截，该批判空跳过（不重试）")
                    final_failed.append(bi)
                    continue
                except _BudgetExhausted as e:
                    # 退避等待超出总耗时预算：该批按最终失败跳过，避免把整轮拖过预算
                    print(f"[{label}] 警告: 批次 {bi + 1} 等待超出耗时预算（{e}），该批跳过（不重试）")
                    final_failed.append(bi)
                    continue
                except Exception as e:
                    print(f"[{label}] 警告: 批次 {bi + 1} 请求失败（{e}），稍后重试")
                    still_failed.append(bi)
                    continue
                batch_verdicts = _parse_verdicts(raw)
                if batch_verdicts is None:          # 防御：work 已校验，理论不可达
                    still_failed.append(bi)
                    continue
                if not batch_verdicts:
                    # 空数组是合法结果：模型常在 [] 后附一段"未发现问题"的自然语言说明，
                    # 这不算解析失败（真正的坏响应已在 work() 抛 _UnparseableResponse 重试）
                    print(f"[{label}] 批次 {bi + 1}/{total} 完成（无问题条目）")
                else:
                    print(f"[{label}] 批次 {bi + 1}/{total} 完成（解析 {len(batch_verdicts)} 条）")
                verdicts[bi] = batch_verdicts
        if fatal is not None:
            raise fatal
        return still_failed

    # 失败批次整轮重试：等待递增（60s×轮次，封顶 300s），整轮重试与单批退避共用同一
    # 总耗时预算（deadline）——超预算放弃剩余批次并告警（调用方不得写整段缓存）
    print(f"[{label}] 判定 {total} 批（并发 {workers} 路，LLM请求中）...")
    final_failed: list[int] = []   # 内容审核拦截/预算耗尽等必然或无需重复的批次：跳过不重试
    pending = run_pass(list(range(total)))
    round_no = 1
    while pending:
        wait = min(60 * round_no, 300) + random.uniform(0, 10)
        print(f"[{label}] {len(pending)} 个批次未成功，{wait:.0f}s 后重试（第 {round_no} 轮）...")
        if not _sleep_within_budget(wait):
            print(f"[{label}] ⚠ 警告: 重试总耗时已达预算 {LLM_RETRY_BUDGET_SECONDS}s，"
                  f"放弃剩余 {len(pending)}/{total} 个批次"
                  f"（本次结果不完整、不写整段缓存，重跑可借批次缓存续判）")
            break
        pending = run_pass(pending)
        round_no += 1
    flat_verdicts = [v for bi in sorted(verdicts) for v in verdicts[bi]]
    return flat_verdicts, len(pending) + len(final_failed), total


def detect_cringe_danmaku(danmaku_list: list[dict], sender_groups: dict[str, dict],
                          video_info: dict) -> dict[str, dict]:
    """
    问题弹幕检测主入口

    Returns:
        {mid_hash: {
            "count": int,            # 该发送者被判问题弹幕的去重内容条数
            "max_severity": int,     # 最高严重度 1-3
            "categories": [str],     # 涉及的问题弹幕类别
            "examples": [{content, category, severity, reason}],  # 至多5条代表原文
            "items": [{content, category, severity, reason}],     # 全量命中（供误报重算聚合）
        }}
        未配置 Key / 全部批次失败 / 无问题弹幕时为相应子集或空 dict
    """
    if not LLM_API_KEY:
        print("[问题弹幕] 未配置 LLM_API_KEY，跳过问题弹幕检测")
        return {}

    items = _dedup_contents(danmaku_list)
    if not items:
        return {}

    # 判定结果缓存：同一视频去重内容集合未变 → 直接复用（重跑零 LLM 调用）
    bvid = video_info.get("bvid", "")
    cache_key = ""
    if bvid:
        # 确定性排序：同内容集合无论输入次序都产生相同 digest（count 降序 + 内容字典序决胜）
        hash_items = sorted(items, key=lambda x: (-x["count"], x["content"]))
        digest = hashlib.sha256(
            json.dumps(hash_items, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        cache_key = f"cringe:{bvid}:{_DM_CACHE_VERSION}:{LLM_MODEL}:{digest}"
        cached = load_llm_cache(cache_key)
        if cached:      # 空串视为未命中
            try:
                results = json.loads(cached)
                if not isinstance(results, dict):
                    raise ValueError("缓存结果不是 dict")
                print(f"[问题弹幕] 缓存命中（{len(results)} 个发送者），跳过 LLM 判定")
                return results
            except (json.JSONDecodeError, ValueError):
                print("[问题弹幕] 警告: 缓存内容损坏，重新判定")

    # 内容 -> 发送者集合（同一内容可能被多人发送，各自归属）
    content_senders: dict[str, set] = {}
    for mid_hash, group in sender_groups.items():
        for c in group.get("contents", []):
            content_senders.setdefault((c or "").strip(), set()).add(mid_hash)

    # 批次缓存前缀用稳定前缀（不嵌整段 digest）：内容集合规模变化时已完成批次缓存仍命中
    raw_verdicts, failed, total = _judge_batches(
        items, CRINGE_BATCH_SIZE, video_info, _build_prompt, "问题弹幕",
        cache_prefix=f"cringe:{bvid}:{_DM_CACHE_VERSION}" if bvid else "")

    verdicts = []
    seen_idx = set()
    for v in raw_verdicts:
        idx = v.get("i")
        if not (isinstance(idx, int) and not isinstance(idx, bool)
                and 0 <= idx < len(items) and v.get("category") in PROBLEM_CATEGORIES):
            continue
        if idx in seen_idx:
            continue        # LLM 重复输出同一编号，去重防聚合虚高
        seen_idx.add(idx)
        v["_content"] = items[idx]["content"]
        verdicts.append(v)
    print(f"[问题弹幕] 全部 {total} 批完成（失败 {failed} 批）：采纳 {len(verdicts)} 条")

    # 按发送者聚合
    results: dict[str, dict] = {}
    for v in verdicts:
        content = v["_content"]
        for mid_hash in content_senders.get(content, ()):
            ent = results.setdefault(mid_hash, {"count": 0, "max_severity": 0,
                                                "categories": [], "examples": [],
                                                "items": []})
            ent["count"] += 1
            sev = v.get("severity", 1)
            # 归一化一次、两处复用：挡 bool/字符串/超界值，钳制到 1-3 的 int
            sev = sev if isinstance(sev, int) and not isinstance(sev, bool) and 1 <= sev <= 3 else 1
            ent["max_severity"] = max(ent["max_severity"], sev)
            cat = v["category"]
            if cat not in ent["categories"]:
                ent["categories"].append(cat)
            item = {"content": content, "category": cat,
                    "severity": sev, "reason": v.get("reason", "")}
            # items 存全量命中（误报标记后 web 端按内容重算聚合）；examples 仍是前5条代表原文
            ent["items"].append(item)
            if len(ent["examples"]) < 5:
                ent["examples"].append(item)

    if cache_key:
        if failed == 0:
            save_llm_cache(cache_key, json.dumps(results, ensure_ascii=False))
        else:
            print(f"[问题弹幕] 警告: {failed}/{total} 批失败，结果不完整，不写整段缓存（重跑可补齐）")

    print(f"[问题弹幕] 检测完成: {len(verdicts)} 条问题弹幕，涉及 {len(results)} 个发送者")
    return results


# ========== 问题评论检测（同一 LLM 判定口径，对象为评论） ==========

def _build_comment_prompt(batch: list[dict], start_idx: int, video_info: dict) -> str:
    """构建单批问题评论判定 prompt（编号为全局下标，便于跨批映射）"""
    lines = [f'{start_idx + i}. {it["content"]}' for i, it in enumerate(batch)]
    return f"""你是熟悉二次元游戏（原神、鸣潮等二游）社区生态的内容审核专家。以下是B站{_video_context(video_info)}的评论列表（已按内容去重）。
语境提示：这是二游视频，游戏世界观名词、剧情设定、角色名、卡池/强度讨论、社区黑话都属于游戏语境，不要按现实语境过度解读。
请逐条判定是否属于以下八类"问题评论"之一：
- 中二抒情：咯噔文学、疼痛文学、自我感动式过度抒情。发癫文学、"XX我老婆"等二游常见发电行为属正常玩梗，不算
- 尬夸捧杀：无脑吹、饭圈式夸张应援、明显违心的吹捧；对角色/剧情/演出的正常夸赞不算
- 引战阴阳：拉踩其他游戏/角色/阵营/玩家群体、对线、反串、阴阳怪气等攻击性内容
- 人身攻击：辱骂、诅咒、攻击其他观众/UP主/声优；只针对游戏内容本身的批评归「批评吐槽」类
- 恶意剧透：泄露剧情关键信息、结局、反转，或未实装角色/卡池的内鬼爆料
- 广告引流：打广告、推广、引流到其他平台或商品（含代练、卖号、私服）
- 键政敏感：把游戏内容借题引申到现实政治人物、事件、意识形态的键政引战；游戏世界观内的国家/战争/政变等设定讨论不算，歌词/台词/梗中引用的符号性词汇也不算。例：「至冬解体」是游戏国家设定联想、「火焰和钢铁--镰刀与锤子」是歌词接龙，都不算键政敏感
- 批评吐槽：对游戏剧情、角色强度、运营策划的批评、锐评、吐槽（对事不对人；含辱骂或攻击玩家/UP主的归人身攻击；此类严重度一般给1）
正常玩梗、合理讨论不算问题评论，宁漏勿冤。

评论列表：
{chr(10).join(lines)}

请严格只输出一个 JSON 数组，每个元素对应一条判定（只输出判为问题评论的条目）：
[{{"i": 编号, "category": "中二抒情|尬夸捧杀|引战阴阳|人身攻击|恶意剧透|广告引流|键政敏感|批评吐槽", "severity": 1到3的整数, "reason": "10字内理由"}}]
没有问题评论就输出 []。不要输出任何 JSON 之外的内容。"""


def detect_problem_comments(comments: list[dict], video_info: dict) -> dict[int, dict]:
    """
    问题评论检测主入口（LLM 判定，与问题弹幕同口径）

    Args:
        comments: comment.collect_comment_data 返回的评论列表（含 rpid/content/like）

    Returns:
        {rpid: {"category": str, "severity": int, "reason": str}}
        未配置 Key / 全部批次失败 / 无问题评论时为空 dict
    """
    if not LLM_API_KEY:
        print("[问题评论] 未配置 LLM_API_KEY，跳过问题评论检测")
        return {}
    if not comments:
        return {}

    # 按内容去重（刷屏复制粘贴的评论只判定一次），内容 → rpid 集合用于回映
    content_rpids: dict[str, list[int]] = {}
    like_of: dict[str, int] = {}
    for c in comments:
        content = (c.get("content") or "").strip()
        rpid = c.get("rpid")
        if not content or not rpid:
            continue
        content_rpids.setdefault(content, []).append(rpid)
        like_of[content] = max(like_of.get(content, 0), c.get("like") or 0)
    items = [{"content": c} for c in content_rpids]
    # 评论量比弹幕更难压缩：按最高点赞降序截断，优先判定可见度高的评论
    # （点赞相同按内容字典序决胜——保证跨运行排序稳定，批次级缓存才能命中）
    items.sort(key=lambda x: (-like_of.get(x["content"], 0), x["content"]))
    if len(items) > COMMENT_CRINGE_MAX_ITEMS:
        print(f"[问题评论] 去重后 {len(items)} 条超出上限，按点赞截取前 {COMMENT_CRINGE_MAX_ITEMS} 条")
        items = items[:COMMENT_CRINGE_MAX_ITEMS]
    if not items:
        return {}

    # 判定结果缓存：同一视频去重内容集合未变 → 直接复用（重跑零 LLM 调用）
    bvid = video_info.get("bvid", "")
    cache_key = ""
    if bvid:
        digest = hashlib.sha256(
            json.dumps(sorted(it["content"] for it in items), ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        cache_key = f"cmt:{bvid}:{_CMT_CACHE_VERSION}:{LLM_MODEL}:{digest}"
        cached = load_llm_cache(cache_key)
        if cached:      # 空串视为未命中
            try:
                content_verdicts = json.loads(cached)
                if not isinstance(content_verdicts, dict):
                    raise ValueError("缓存结果不是 dict")
                # 缓存语义为 内容→判定：用当前 内容→rpids 映射展开，
                # 续采新增的同内容评论（新 rpid）也能正确回标
                results = {}
                for content, verdict in content_verdicts.items():
                    for rpid in content_rpids.get(content, []):
                        results[rpid] = verdict
                print(f"[问题评论] 缓存命中（{len(content_verdicts)} 条问题内容，回标 {len(results)} 条评论），跳过 LLM 判定")
                return results
            except (json.JSONDecodeError, ValueError, TypeError):
                print("[问题评论] 警告: 缓存内容损坏，重新判定")

    # 批次缓存前缀用稳定前缀（不嵌整段 digest）：内容集合规模变化时已完成批次缓存仍命中
    raw_verdicts, failed, total = _judge_batches(
        items, COMMENT_CRINGE_BATCH_SIZE, video_info, _build_comment_prompt, "问题评论",
        cache_prefix=f"cmt:{bvid}:{_CMT_CACHE_VERSION}" if bvid else "")

    results: dict[int, dict] = {}
    content_verdicts: dict[str, dict] = {}      # 内容→判定（整段缓存的存储语义）
    for v in raw_verdicts:
        idx = v.get("i")
        if not (isinstance(idx, int) and not isinstance(idx, bool)
                and 0 <= idx < len(items) and v.get("category") in PROBLEM_CATEGORIES):
            continue
        sev = v.get("severity", 1)
        # 归一化：挡 bool/字符串/超界值，钳制到 1-3 的 int
        sev = sev if isinstance(sev, int) and not isinstance(sev, bool) and 1 <= sev <= 3 else 1
        verdict = {"category": v["category"], "severity": sev, "reason": v.get("reason", "")}
        content_verdicts[items[idx]["content"]] = verdict
        # 同一内容的所有 rpid 都标注（复制粘贴刷屏的评论同源同罪）
        for rpid in content_rpids.get(items[idx]["content"], []):
            results[rpid] = verdict
    print(f"[问题评论] 全部 {total} 批完成（失败 {failed} 批）：标注 {len(results)} 条")

    if cache_key:
        if failed == 0:
            save_llm_cache(cache_key, json.dumps(content_verdicts, ensure_ascii=False))
        else:
            print(f"[问题评论] 警告: {failed}/{total} 批失败，结果不完整，不写整段缓存（重跑可补齐）")

    print(f"[问题评论] 检测完成: {len(results)} 条问题评论")
    return results
