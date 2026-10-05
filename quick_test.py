"""
快速分析 - 只分析刷屏得分最高的前N个发送者
用法: python quick_test.py [BV号] [--top N]
"""
import sys, os
import argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# 强制行缓冲：输出被重定向/管道时也能实时看到进度（默认块缓冲会长时间无输出）
sys.stdout.reconfigure(line_buffering=True)

from config import LLM_API_KEY, DATA_DIR
from auth import get_auth_client
from api_client import RiskControlError
from combo_pool import build_pool
from danmaku import collect_danmaku_data, group_by_sender
from spam_detector import batch_detect_spam
from cringe_detector import detect_cringe_danmaku
from comment import fetch_comments, build_comment_uid_map, build_comment_location_map
from uid_resolver import resolve_sender
from user_collector import collect_user_data
from profile_analyzer import analyze_profile
from llm_analyzer import LLMAnalyzer
from storage import init_db, save_video_info, load_video_info
from web_autostart import maybe_launch_web

# 冒烟采样上限：不跑全量视频，只取少量数据验证流水线
QUICK_DANMAKU_LIMIT = 100   # 弹幕只取实时池前 100 条（跳过历史快照合并）
QUICK_COMMENT_PAGES = 3     # 评论只翻 3 页（≈60 条主评论）
QUICK_COMMENT_LIMIT = 50    # 评论截断条数


class _Tee:
    """stdout/stderr 双写：终端 + data/quick_test.log（冒烟日志可回溯，重定向时也有文件）"""

    def __init__(self, path: str, stream):
        # buffering=1 行缓冲：日志实时可见（tail -f 可追），避免块缓冲长时间无输出
        # "a" 追加：重跑不清空历史日志，便于回溯上一次冒烟失败时的输出
        self._file = open(path, "a", encoding="utf-8", buffering=1)
        self._out = stream

    def write(self, s):
        self._out.write(s)
        self._file.write(s)

    def flush(self):
        self._out.flush()
        self._file.flush()

    # 文件对象协议属性：转发到底层流，兼容下游库对 sys.stdout/stderr 的能力探测
    @property
    def encoding(self):
        return self._out.encoding

    def fileno(self):
        return self._out.fileno()

    def isatty(self):
        return self._out.isatty()


def main():
    parser = argparse.ArgumentParser(description="快速分析 - 采样少量数据验证流水线（弹幕100/评论50/用户N）")
    parser.add_argument("bvid", nargs="?", default="BV1ebg16jEhp",
                        help="视频BV号 (默认 BV1ebg16jEhp)")
    parser.add_argument("--top", "-n", type=int, default=10,
                        help="分析刷屏得分最高的前N个发送者 (默认 10)")
    args = parser.parse_args()
    bvid = args.bvid
    top_n = max(1, args.top)  # 夹紧下限：--top 0/负数无意义

    # 全程日志双写到 data/quick_test.log（后台/重定向运行时也能回看进度）
    os.makedirs(DATA_DIR, exist_ok=True)
    log_path = os.path.join(DATA_DIR, "quick_test.log")
    sys.stdout = _Tee(log_path, sys.stdout)
    sys.stderr = _Tee(log_path, sys.stderr)
    print(f"[日志] 同步写入 {log_path}")

    print(f"🎯 快速分析: {bvid}  (刷屏 Top {top_n})")
    print(f"   策略: 采样弹幕{QUICK_DANMAKU_LIMIT}条/评论{QUICK_COMMENT_LIMIT}条 → 刷屏检测 → 只解 Top{top_n} UID\n")

    # 1. 登录
    print("[1/6] 登录...")
    client = get_auth_client()
    # 账号×IP 组合池（同主流程：风控换号+切节点，故障自动降级）
    pool = build_pool(client)

    # 2. 采集弹幕（冒烟：只取实时池前 100 条，跳过历史快照合并）
    print("[2/6] 采集弹幕（采样）...")
    video_info, danmaku_list, _ = collect_danmaku_data(bvid, pool)
    print(f"   视频: {video_info.get('title')}")
    danmaku_list = danmaku_list[:QUICK_DANMAKU_LIMIT]
    sender_groups = group_by_sender(danmaku_list)

    # 采样数据不落库：弹幕采样只调用了 collect_danmaku_data 的解析与 group_by_sender 聚合，
    # 全程未调用任何弹幕落库函数；评论采样同样只做内存统计（无 phase_state 哨兵的采样落库
    # 会被 run.py 断点续采误判为"旧版完整落库"而跳过全量评论采集）。视频信息落库对齐主流程口径：先在内存合并
    # 库内旧行的 danmaku_coverage，避免裸 API 返回整行覆盖把弹幕覆盖率冲掉
    try:
        init_db()
        prev_info = load_video_info(bvid) or {}
        if prev_info.get("danmaku_coverage"):
            video_info["danmaku_coverage"] = prev_info["danmaku_coverage"]
        save_video_info(bvid, video_info)
    except Exception as e:
        print(f"   警告: 视频信息落库失败（{e}）")

    print(f"   弹幕采样: {len(danmaku_list)} 条, 发送者: {len(sender_groups)} 人")

    # 3. 刷屏检测 + 问题弹幕检测 → 兴趣分 Top N（对齐主流程兴趣口径）
    print("[3/6] 刷屏检测 + 问题弹幕检测...")
    spam_results = batch_detect_spam(sender_groups)
    try:
        cringe_results = detect_cringe_danmaku(danmaku_list, sender_groups, video_info) if LLM_API_KEY else {}
    except Exception as e:
        # 对齐主流程 phase_cringe：问题弹幕检测失败只警告不中断，降级为空结果
        print(f"   问题弹幕检测失败（{e}），降级跳过")
        cringe_results = {}
    scored = [
        (mid_hash, r["spam_score"], r["spam_level"])
        for mid_hash, r in spam_results.items()
    ]
    scored.sort(key=lambda x: (x[1], cringe_results.get(x[0], {}).get("max_severity", 0)),
                reverse=True)
    targets = scored[:top_n]
    for i, (mid, score, level) in enumerate(targets, 1):
        grp = sender_groups[mid]
        print(f"   #{i} mid_hash={mid} score={score:.2f} level={level} 弹幕{grp['count']}条")

    # 4. 收集评论（冒烟：只翻 3 页再截断 50 条）
    print("[4/6] 收集评论（采样）...")
    comments = []
    try:
        # 不传 bvid：fetch_comments 带 bvid 会逐页落库并写 phase_state 游标检查点，
        # 冒烟采样必须保持纯内存（采样落库会污染 run.py 的评论断点续采判据）
        comments = fetch_comments(video_info.get("aid", 0), pool, max_pages=QUICK_COMMENT_PAGES)
        comments = comments[:QUICK_COMMENT_LIMIT]
        comment_uid_map = build_comment_uid_map(comments)
        print(f"   评论采样: {len(comments)} 条, UID映射: {len(comment_uid_map)} 个")
    except Exception as e:
        # 对齐主流程 phase_comment：评论采集失败降级为仅用CRC32破解，只警告不中断
        print(f"   评论采集失败 (将仅用CRC32破解): {e}")
        comment_uid_map = {}
    uid_comments: dict[int, list] = {}
    for c in comments:
        uid_comments.setdefault(c["uid"], []).append(c)
    for lst in uid_comments.values():
        lst.sort(key=lambda x: x.get("like", 0), reverse=True)

    # 5. 逐个解析 + 采集 + 画像 + AI
    profiles = []
    for rank, (mid_hash, score, level) in enumerate(targets, 1):
        print(f"\n{'='*40}")
        print(f"  #{rank} mid_hash={mid_hash}")
        group = sender_groups[mid_hash]

        # 解析 UID
        uid, confidence, method, _, collision_risk, _ = resolve_sender(
            mid_hash, group["contents"], comment_uid_map, pool
        )
        if not uid:
            print(f"  ❌ UID 解析失败!")
            continue
        risk_note = " ⚠️可能误识别" if collision_risk else ""
        print(f"  ✅ UID={uid} (方法: {method}, 置信度: {confidence}){risk_note}")

        # 采集数据（对齐主流程：采集失败跳过该用户，不生成幽灵画像）
        try:
            user_data = collect_user_data(uid, pool)
        except Exception as e:
            print(f"  ❌ 用户数据采集失败: {e}")
            continue
        if "error" in user_data:
            print(f"  ❌ 用户数据采集失败: {user_data['error']}")
            continue
        dm_stats = {"count": group["count"], "contents": group["contents"],
                    "video_times": group.get("video_times", []),
                    "video_pages": group.get("video_pages", []),
                    "multi_page": len(video_info.get("pages") or []) > 1}
        spam = spam_results.get(mid_hash, {})
        profile = analyze_profile(user_data, dm_stats, spam)
        profile["collision_risk"] = collision_risk
        profile["comments"] = uid_comments.get(uid, [])[:10]
        profile["cringe"] = cringe_results.get(mid_hash, {})
        profiles.append(profile)

    # 6. AI 重点深掘（对齐主流程：7a 粗筛已砍，只保留 top K 深掘）
    if profiles and LLM_API_KEY:
        print(f"\n[6/6] AI 重点深掘 ({len(profiles)}人)...")
        try:
            analyzer = LLMAnalyzer()
            deep = analyzer.analyze_deep(profiles, video_info, top_k=top_n)
            for p in profiles:
                uid = p.get("uid")
                if uid in deep:
                    p["ai_deep"] = deep[uid]
        except Exception as e:
            print(f"   AI 分析失败: {e}")

    # 静态 HTML 报告已被 web.py 交互式报告完全替换
    print(f"\n✅ 分析完成: {len(profiles)} 人生成画像")
    # 自动启动 web.py 并打开报告页（冒烟场景不阻塞结束：失败只打印 URL 降级）
    maybe_launch_web(bvid)


if __name__ == "__main__":
    try:
        main()
    except RiskControlError as e:
        # 对齐 main.py：组合池兜底耗尽（整圈账号全风控+长冷却后仍失败）时非零退出
        print(f"\n[退出] 风控兜底耗尽（{e}），快速分析终止")
        raise SystemExit(1)
