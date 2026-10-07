"""
刷屏智能检测模块

原则：不删除、不过滤，只标记和分析。
区分正常重复（如应援、玩梗）与恶意刷屏（垃圾内容、机器人）。
"""
import difflib
import random
import re
import string
import unicodedata
from collections import Counter
from typing import Tuple

from config import (SPAM_HIGH_THRESHOLD, SPAM_MEDIUM_THRESHOLD,
                    SPAM_BURST_WINDOW_SECONDS, SPAM_BURST_HIGH_COUNT,
                    SPAM_BURST_MEDIUM_COUNT, SPAM_VARIANT_SIMILARITY,
                    SPAM_VARIANT_MIN_COUNT, SPAM_BURST_MIN_COUNT,
                    SPAM_COMBO_BONUS, SPAM_RELATIVE_MIN_POOL,
                    SPAM_RELATIVE_REPEAT_FLOOR, SPAM_RELATIVE_COUNT_FLOOR,
                    SPAM_RELATIVE_SCORE, REPEAT_EVENT_WINDOW_SECONDS,
                    REPEAT_EVENT_MIN_SENDERS, REPEAT_EVENT_MIN_TOTAL,
                    REPEAT_EVENT_TOP_N, SPAM_PAIRWISE_UNIQUE_CAP)


def content_similarity(a: str, b: str) -> float:
    """计算两条内容的相似度（0-1）"""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def analyze_content_repeat(contents: list[str], pairwise: bool = True) -> Tuple[float, float, float]:
    """
    分析内容重复率

    Args:
        pairwise: 是否做两两相似度计算（变种刷屏判定用）。O(u²)×difflib 在唯一内容
            很多时是性能炸弹（水军号上千条不同弹幕会卡死数小时），调用方应在
            条数不足 SPAM_VARIANT_MIN_COUNT 时传 False 跳过；唯一内容数超过
            _PAIRWISE_UNIQUE_CAP 时确定性抽样后再比（结果跨运行稳定）。

    Returns:
        (整体重复率, 两两相似度加权和, 两两相似度对数)
        不物化全量相似度列表（n(n-1)/2 个 float），调用方按 sum/count 取均值。
    """
    n = len(contents)
    if n <= 1:
        return 0.0, 0.0, 0

    counts = Counter(contents)
    unique = list(counts)
    repeat_rate = 1 - len(unique) / n

    if not pairwise:
        return repeat_rate, 0.0, 0

    # 唯一内容数超限：固定种子抽样（确定性，重跑结果一致），防 O(u²) 爆炸
    sampled = len(unique) > SPAM_PAIRWISE_UNIQUE_CAP
    if sampled:
        u_all = len(unique)
        unique = random.Random(0).sample(unique, SPAM_PAIRWISE_UNIQUE_CAP)

    # 先按内容去重，只对唯一内容两两比较，再用出现次数加权展开为全量两两相似度。
    # 与原 O(n²) 全量比较数学等价（相同内容相似度恒为 1，唯一对 (a,b) 贡献
    # count[a]*count[b] 个相同 sim），复杂度降为 O(u²)，u = 唯一内容数；
    # 且边算边累加 sum/count，不再物化 n(n-1)/2 个元素的列表。
    #
    # ① 相同内容对：sim 恒为 1，按出现次数加权；O(u) 全量精确累加，不受抽样影响。
    sim_sum = 0.0
    sim_count = 0
    for c in counts.values():
        k = c * (c - 1) // 2
        sim_sum += float(k)
        sim_count += k

    # ② 跨内容对：真正的 O(u²) 部分，超限后只在抽样子集上计算
    cross_sum = 0.0
    cross_w = 0
    for i in range(len(unique)):
        ci = counts[unique[i]]
        for j in range(i + 1, len(unique)):
            k = ci * counts[unique[j]]
            cross_sum += content_similarity(unique[i], unique[j]) * k
            cross_w += k

    if sampled:
        # ⚠️ 抽样后必须把②还原到全量口径，才能与①（全量）相加。
        # 原实现直接把抽样得到的 cross 量与全量的相同内容对相加：保留了全部
        # sim=1 的质量、却只保留 C(s,2)/C(u,2) 的跨内容对质量，均值被系统性
        # 推向 1（弹幕越多偏差越大），规则3（SPAM_VARIANT_SIMILARITY）因此误判
        # 「变种刷屏」并叠加组合加分。
        # 还原：抽样覆盖 C(s,2)/C(u,2) 的对，按该比例放大相似度之和；
        # 对权重直接用 O(u) 精确值 (n² - Σc²)/2，无需估算。
        s = len(unique)
        cross_sum *= (u_all * (u_all - 1)) / (s * (s - 1))
        total_n = sum(counts.values())
        cross_w = (total_n * total_n - sum(c * c for c in counts.values())) / 2

    sim_sum += cross_sum
    sim_count += cross_w

    return repeat_rate, sim_sum, sim_count


def _burst_max(sorted_ts: list[int], window_seconds: int) -> int:
    """滑动窗口双指针 O(n)：任意 window_seconds 秒窗口内的最大弹幕条数。

    供机器人模式与「高频爆发」规则共用；要求 sorted_ts 已升序。"""
    burst = 0
    left = 0
    for right in range(len(sorted_ts)):
        while sorted_ts[right] - sorted_ts[left] > window_seconds:
            left += 1
        burst = max(burst, right - left + 1)
    return burst


def detect_bot_pattern(timestamps: list[int]) -> float:
    """
    检测机器人模式

    特征：
    - 时间间隔过于规律（标准差极小）
    - 短时间内大量发送

    Returns:
        bot_score (0-1)，越高越像机器人
    """
    if len(timestamps) < 3:
        return 0.0

    # 按时间排序
    sorted_ts = sorted(timestamps)
    intervals = [sorted_ts[i] - sorted_ts[i - 1] for i in range(1, len(sorted_ts))]

    if not intervals:
        return 0.0

    avg_interval = sum(intervals) / len(intervals)
    if avg_interval == 0:
        return 1.0  # 同一时间发送多条，高度疑似机器人

    # 计算间隔的标准差系数（变异系数）
    variance = sum((x - avg_interval) ** 2 for x in intervals) / len(intervals)
    std = variance ** 0.5
    cv = std / avg_interval if avg_interval > 0 else 0

    # 变异系数极小（<0.1）说明间隔非常规律，疑似机器人
    bot_score = max(0, 1 - cv * 10)  # cv越小，分数越高

    # 短时间内爆发：滑动窗口双指针 O(n) 统计窗口内最大条数
    burst_count = _burst_max(sorted_ts, SPAM_BURST_WINDOW_SECONDS)

    if burst_count >= SPAM_BURST_HIGH_COUNT:
        bot_score = max(bot_score, 0.7)
    elif burst_count >= SPAM_BURST_MEDIUM_COUNT:
        bot_score = max(bot_score, 0.4)

    return min(1.0, bot_score)


def analyze_spam(danmaku_contents: list[str], timestamps: list[int]) -> dict:
    """
    综合分析刷屏程度
    
    Returns:
        {
            "count": int,                # 弹幕总数
            "unique_count": int,         # 唯一内容数
            "repeat_rate": float,        # 重复率 0-1
            "avg_similarity": float,     # 平均内容相似度
            "avg_interval": float,       # 平均发送间隔（秒）
            "burst_max": int,            # 滑动窗口内最大条数（SPAM_BURST_WINDOW_SECONDS 秒窗）
            "bot_score": float,          # 机器人评分 0-1
            "spam_score": float,         # 综合刷屏评分 0-1
            "spam_level": str,           # 高/中/低
            "reason": str,               # 判定理由
        }
    """
    count = len(danmaku_contents)
    unique_contents = set(danmaku_contents)
    unique_count = len(unique_contents)

    # 内容重复率（两两相似度仅在变种刷屏可能触发时才计算：条数不足
    # SPAM_VARIANT_MIN_COUNT 时规则3必不触发，O(u²) 的 difflib 全量比较直接跳过）
    repeat_rate, sim_sum, sim_count = analyze_content_repeat(
        danmaku_contents, pairwise=count >= SPAM_VARIANT_MIN_COUNT)
    avg_similarity = sim_sum / sim_count if sim_count else 0.0

    # 时间间隔
    if len(timestamps) >= 2:
        sorted_ts = sorted(timestamps)
        intervals = [sorted_ts[i] - sorted_ts[i - 1] for i in range(1, len(sorted_ts))]
        avg_interval = sum(intervals) / len(intervals)
    else:
        avg_interval = 0.0

    # 机器人检测
    bot_score = detect_bot_pattern(timestamps)
    sorted_ts = sorted(timestamps)
    burst_max = _burst_max(sorted_ts, SPAM_BURST_WINDOW_SECONDS) if sorted_ts else 0

    # 综合刷屏评分：规则各自产出候选分，最终分 = 最高分 + 每多触发一条规则加成
    # SPAM_COMBO_BONUS（封顶 1.0）——「max 取极值」会让多个中等信号叠加的刷子
    # （重复中等+间隔偏规律+相似度偏高）得分反不如单触一条规则的人，弱证据累积
    # 恰是机器人最典型的特征，故改为组合计分。
    rule_scores: list[float] = []
    reasons = []

    # 规则1：大量重复内容
    if count >= SPAM_HIGH_THRESHOLD[0] and repeat_rate >= SPAM_HIGH_THRESHOLD[1]:
        rule_scores.append(0.85)
        reasons.append(f"大量重复({count}条, 重复率{repeat_rate:.0%})")
    elif count >= SPAM_MEDIUM_THRESHOLD[0] and repeat_rate >= SPAM_MEDIUM_THRESHOLD[1]:
        rule_scores.append(0.6)
        reasons.append(f"中度重复({count}条, 重复率{repeat_rate:.0%})")

    # 规则2：机器人模式
    if bot_score >= 0.7:
        rule_scores.append(0.8)
        reasons.append(f"机器人模式(评分{bot_score:.2f})")
    elif bot_score >= 0.4:
        rule_scores.append(0.5)
        reasons.append(f"疑似机器人(评分{bot_score:.2f})")

    # 规则3：内容高度相似但不完全相同（变种刷屏）
    # unique_count >= 2 是本规则的语义前提：内容完全相同时 avg_similarity 恒为 1.0，
    # 会连本规则一起命中——但那属于规则1「大量重复」的范畴，理由写成「变种刷屏」
    # 名不副实，还会多叠一次 SPAM_COMBO_BONUS 把分数抬高，挤占真正的变种刷屏者。
    if (avg_similarity >= SPAM_VARIANT_SIMILARITY and count >= SPAM_VARIANT_MIN_COUNT
            and unique_count >= 2):
        rule_scores.append(0.7)
        reasons.append(f"变种刷屏(相似度{avg_similarity:.0%})")

    # 规则4：短时间内爆发（滑动窗口口径）——窗口内最大条数达标即触发；
    # 旧口径用全时段平均间隔，「长期低频+某一刻爆发」的用户会被平均值稀释漏检。
    if burst_max >= SPAM_BURST_MIN_COUNT:
        rule_scores.append(0.75)
        reasons.append(f"高频爆发({SPAM_BURST_WINDOW_SECONDS}秒内最多{burst_max}条)")

    if rule_scores:
        spam_score = min(1.0, max(rule_scores) + SPAM_COMBO_BONUS * (len(rule_scores) - 1))
    else:
        spam_score = 0.0

    # 判定等级
    if spam_score >= 0.7:
        level = "高"
    elif spam_score >= 0.4:
        level = "中"
    else:
        level = "低"

    return {
        "count": count,
        "unique_count": unique_count,
        "repeat_rate": repeat_rate,
        "avg_similarity": avg_similarity,
        "avg_interval": avg_interval,
        "burst_max": burst_max,
        "bot_score": bot_score,
        "spam_score": spam_score,
        "spam_level": level,
        "reason": "; ".join(reasons) if reasons else "正常发言",
    }


def _percentile(values: list[float], q: float) -> float:
    """朴素分位数（线性插值）；values 非空"""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _apply_relative_outliers(results: dict[str, dict]) -> None:
    """相对阈值补强（第二遍，原地修改）。

    绝对阈值对全体视频一刀切（500 条弹幕的小视频偏松、8 万条的大视频偏紧）；
    这里以本视频全池分布取相对离群：弹幕量与重复率双双超过全池 P95（且过绝对
    下限防小样本噪声）的「低」风险发送者保底提到中档，避免大水军池里的相对
    高 repeat 用户被绝对阈值漏掉。"""
    pool = [r for r in results.values() if r["count"] >= SPAM_RELATIVE_COUNT_FLOOR]
    if len(pool) < SPAM_RELATIVE_MIN_POOL:
        return
    p95_repeat = _percentile([r["repeat_rate"] for r in pool], 0.95)
    p95_count = _percentile([r["count"] for r in pool], 0.95)
    for r in results.values():
        if r["spam_level"] != "低":
            continue
        if (r["count"] >= max(p95_count, SPAM_RELATIVE_COUNT_FLOOR)
                and r["repeat_rate"] >= max(p95_repeat, SPAM_RELATIVE_REPEAT_FLOOR)):
            r["spam_score"] = max(r["spam_score"], SPAM_RELATIVE_SCORE)
            r["spam_level"] = "中"
            reason = f"相对离群(重复率{r['repeat_rate']:.0%}超全池P95={p95_repeat:.0%})"
            r["reason"] = reason if r["reason"] == "正常发言" else r["reason"] + "; " + reason


def batch_detect_spam(sender_groups: dict[str, dict]) -> dict[str, dict]:
    """
    批量检测所有发送者的刷屏程度（绝对规则 + 全池相对离群补强两遍）

    Args:
        sender_groups: {mid_hash: group_data}

    Returns:
        {mid_hash: spam_analysis_result}
    """
    results = {}
    for mid_hash, group in sender_groups.items():
        result = analyze_spam(
            group["contents"],
            group["timestamps"]
        )
        results[mid_hash] = result
    _apply_relative_outliers(results)
    return results


def distribution_stats(results: dict[str, dict]) -> dict:
    """全池分布自检（阈值校准参考）：弹幕数/重复率/刷屏分的 P50/P90/P95。

    输入为 batch_detect_spam 的结果；人工调阈值或误报数据积累后评估
    规则 precision 时，用这个分布判断当前阈值松紧。"""
    if not results:
        return {"senders": 0}
    counts = [r["count"] for r in results.values()]
    repeats = [r["repeat_rate"] for r in results.values()]
    scores = [r["spam_score"] for r in results.values()]
    return {
        "senders": len(results),
        "count_p50": _percentile(counts, 0.50),
        "count_p90": _percentile(counts, 0.90),
        "count_p95": _percentile(counts, 0.95),
        "repeat_p50": _percentile(repeats, 0.50),
        "repeat_p90": _percentile(repeats, 0.90),
        "repeat_p95": _percentile(repeats, 0.95),
        "score_p50": _percentile(scores, 0.50),
        "score_p90": _percentile(scores, 0.90),
        "score_p95": _percentile(scores, 0.95),
    }


def pool_distribution_from_rows(rows: list[dict]) -> dict:
    """从弹幕行直接算全池分布（web 概览页用，无需重跑完整 analyze_spam）：
    每发送者弹幕数/重复率的 P50/P90/P95。rows: [{mid_hash, content}]。"""
    per_sender: dict[str, list[str]] = {}
    for r in rows:
        mh = r.get("mid_hash") or ""
        if mh:
            per_sender.setdefault(mh, []).append(r.get("content") or "")
    if not per_sender:
        return {"senders": 0}
    counts = [len(v) for v in per_sender.values()]
    repeats = [1 - len(set(v)) / len(v) if v else 0.0 for v in per_sender.values()]
    return {
        "senders": len(per_sender),
        "count_p50": _percentile(counts, 0.50),
        "count_p95": _percentile(counts, 0.95),
        "repeat_p50": _percentile(repeats, 0.50),
        "repeat_p95": _percentile(repeats, 0.95),
    }


# 复读内容归一化：连续重复的**非 ASCII 字母**折叠成一个（？？？→？、666→6、哈哈哈→哈），
# 但保留英文单词原样（否则 good→god 这类会被误并成同一句复读）
_REPEAT_RUN_RE = re.compile(r"([^A-Za-z])\1+")
# 需要剥掉的首尾装饰（NFKC 后全角标点多已折叠为 ASCII 标点，故两者都列）
_TRIM_PUNCT = string.punctuation + "。，、！？；：、“”‘’（）《》【】〈〉「」『』—…·～"


def _content_key(content: str) -> str:
    """复读内容的归一化键：全半角、空白、首尾标点、连续重复字符的写法差异视为同一句。

    动机：同一波复读里「原神牛逼」「原神牛逼！」「原神牛逼！！！」会被算成三起事件，
    榜单被写法变体占满、人数还被拆散（BV1BtoYBaELd 实测 138 组、BV1mtTD6rEtQ 51 组）。
    纯标点内容（？/…）退化为"折叠自身"而不是空串，否则「？」会与「……」并成一句。"""
    s = unicodedata.normalize("NFKC", content or "").strip()
    s = re.sub(r"\s+", "", s)
    core = s.strip(_TRIM_PUNCT)
    if core:
        s = core
    return _REPEAT_RUN_RE.sub(r"\1", s).lower()


def _peak_window(items: list[tuple[float, str]], window_seconds: int) -> tuple:
    """滑动窗口求峰值，返回 (不同发送者数, 窗口内条数, 窗口起, 窗口止)。

    items: [(时间, mid_hash)]（内部自行排序；窗口为 [t, t+window_seconds] 闭区间口径）"""
    if not items:
        return (0, 0, 0, 0)
    items = sorted(items, key=lambda x: x[0])
    best = (0, 0, items[0][0], items[0][0])
    senders: Counter = Counter()
    left = 0
    for right, (t_r, mh_r) in enumerate(items):
        senders[mh_r] += 1
        while t_r - items[left][0] > window_seconds:
            mh_l = items[left][1]
            senders[mh_l] -= 1
            if senders[mh_l] <= 0:
                del senders[mh_l]
            left += 1
        if (len(senders), right - left + 1) > (best[0], best[1]):
            best = (len(senders), right - left + 1, items[left][0], t_r)
    return best


def detect_repeat_events(rows: list[dict],
                         window_seconds: int = REPEAT_EVENT_WINDOW_SECONDS,
                         min_senders: int = REPEAT_EVENT_MIN_SENDERS,
                         min_total: int = REPEAT_EVENT_MIN_TOTAL,
                         top_n: int = REPEAT_EVENT_TOP_N) -> list[dict]:
    """群体复读事件检测（全视频维度，补单人检测的最大盲区）。

    单人检测（analyze_spam）抓不到「一人一句的接龙/+1 队列」——每个发送者只发
    一两条、单看完全正常，合起来才是刷屏事件。这里按内容聚合全体弹幕，同一内容
    的窗口内 ≥min_senders 个不同发送者且 ≥min_total 条即记一次（每内容只报峰值）。

    **双时间轴**（两条都算，各自达标才写进结果）：
    - 视频内时间轴（主，video）：接龙/+1 发生在**同一个视频时间点**——观众可能相隔
      几个月才看到这里，但都在同一画面刷同一句话。窗口按 time 计算且**必须分P**
      （time 是分P 内相对秒数，跨分P 比大小无意义）。
    - 发送时间轴（send）：同一内容在真实时间的短窗口内被集中刷出，是水军/集中刷屏
      的形态。只看它会把「复读」整体漏掉——实测 BV1mtTD6rEtQ：视频内时间轴命中
      20 起（「许愿不歪」54 人 / 56 条挤在同一画面），发送时间轴 0 起（观众横跨
      三个月）；全库 30 个视频则是 331 : 18。

    Args:
        rows: [{content, mid_hash, time, timestamp, page}]（time 为分P 内秒数）

    Returns:
        按发送者数降序的事件 [{content, sender_count, total, video, send,
        variants, variant_count}]，video={"page","start","end"}（分P 内秒数，可能为
        None）、send={"start","end"}（Unix 时间戳，可能为 None）；sender_count/total
        取达标轴中的较强者；content 为归一化键下最多的原始写法，variants 为前 5 种
        写法及条数（「原神牛逼」+「原神牛逼！」这类变体已并入同一事件）。
    """
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        content = r.get("content") or ""
        if content:
            # 按归一化键聚合：同一波复读的写法变体（标点/重复字符/全半角）算同一句
            by_key.setdefault(_content_key(content), []).append(r)

    def _ok(peak: tuple) -> bool:
        return peak[0] >= min_senders and peak[1] >= min_total

    events = []
    for content, items in by_key.items():
        if len(items) < min_total:
            continue
        # 1) 发送时间轴：真实时间戳，跨分P 合并（集中刷屏与分P 无关）
        send_peak = (0, 0, 0, 0)
        try:
            send_peak = _peak_window(
                [(float(r.get("timestamp") or 0), r.get("mid_hash") or "") for r in items
                 if r.get("timestamp")], window_seconds)
        except (TypeError, ValueError):
            pass
        # 2) 视频内时间轴：按分P 分别求峰，取各分P 中最强的一处
        video = None   # (peak, page)
        by_page: dict[int, list[tuple[float, str]]] = {}
        for r in items:
            try:
                by_page.setdefault(int(r.get("page") or 1), []).append(
                    (float(r.get("time") or 0), r.get("mid_hash") or ""))
            except (TypeError, ValueError):
                continue
        for pg, its in by_page.items():
            peak = _peak_window(its, window_seconds)
            if video is None or (peak[0], peak[1]) > (video[0][0], video[0][1]):
                video = (peak, pg)
        video_ok = video is not None and _ok(video[0])
        send_ok = _ok(send_peak)
        if not (video_ok or send_ok):
            continue
        # 主指标取达标轴中的较强者（同分取条数多者，保证确定性与"更能说明问题"的一侧）
        head = video[0] if video_ok and (not send_ok or (video[0][0], video[0][1]) >= (send_peak[0], send_peak[1])) else send_peak
        # 展示用主写法＝该归一化键下出现最多的原始内容；其余写法做变体清单（tooltip 用）
        variant_counts = Counter(r["content"] for r in items).most_common()
        events.append({
            "content": variant_counts[0][0],
            "sender_count": head[0],
            "total": head[1],
            "video": ({"page": video[1], "start": video[0][2], "end": video[0][3]}
                      if video_ok else None),
            "send": ({"start": int(send_peak[2]), "end": int(send_peak[3])} if send_ok else None),
            "variants": [[c, n] for c, n in variant_counts[:5]],
            "variant_count": len(variant_counts),
        })

    events.sort(key=lambda e: (e["sender_count"], e["total"]), reverse=True)
    return events[:top_n]
