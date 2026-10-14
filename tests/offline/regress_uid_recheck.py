# -*- coding: utf-8 -*-
"""终审复核与候选三态回归（20 项）

硬要求：**报告里不能出现点进去 404 的 UID 账号**。本用例锁定三件事：
  1) 展示三态：只有"已核验存在"的候选渲染成可点击链接；"已确认不存在"进已排除；
     "未知/结论过期"进待复核——后两类一律纯文本，不给跳转；
  2) 过期不再复活：账号不存在结论（-404）过了 1 小时 TTL 也只标"待复核"，不退回"未知"；
  3) 候选终审循环：出现"上一份结论为 not_exists、本轮为 exists"的调整就等待后继续复核，直到一轮内
     没有调整；明确 -404 累计确认达阈值才判 final（彻底排除）。

离线回归脚本：隔离临时库 + 假核验函数，**不联网、不读 Cookie、不消耗 LLM**。
从仓库任意目录均可运行：  python tests/offline/regress_uid_recheck.py
"""
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

TMP = tempfile.mkdtemp(prefix="uid_recheck_")
import config
import storage

storage.DB_PATH = os.path.join(TMP, "t.db")
config.DB_PATH = storage.DB_PATH
storage.init_db()

import crc_candidates as crc
import candidate_cards as cards
from uid_evidence import crc32_uid

A, B = 12345678, 7533489035
H = crc32_uid(A)
ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  ✔ %s %s" % (name, extra))
    else:
        fail += 1; print("  ✘ %s %s" % (name, extra))


def sender(recorded=None, waiting=None, method="候选歧义", cands=None):
    cands = list(cands or (A, B))
    resolution = {"candidates": cands, "evidence": [], "checked_at": time.time(),
                  "candidate_version": "mitm-v1",
                  "candidate_status": {str(cands[0]): "not_exists", **(recorded or {})}}
    if waiting:
        resolution["recheck_pending"] = waiting
    return {"mid_hash": crc32_uid(cands[0]), "uid": None, "confidence": "无", "method": method,
            "danmaku_count": 2, "contents": ["问题原文"], "spam_level": "低",
            "resolution": resolution}


def rows():
    return [{"mid_hash": H, "content": "问题原文", "timestamp": 1700000000, "time": 1,
             "color": "#fff", "dmid": "1", "page": 1, "mode": 1, "pool": 0}]


def problem():
    return {"问题原文": {"content": "问题原文", "category": "人身攻击", "severity": 2, "reason": "离线"}}


def render(verdicts, row):
    groups = crc.build_crc_groups([row], rows(), problem(), set(), verdicts, None, {},
                                  include_resolved=True)
    return groups, crc.render_crc_groups(groups, "/endpoint", False, str, str)


def links(html):
    return set(re.findall(r"space\.bilibili\.com/(\d+)", html))


print("=== 1. 过期的 -404 不得复活成可点击候选 ===")
storage.save_uid_verify_cache(A, "not_exists", {}, time.time() - 4.5 * 3600)   # 远超 1 小时 TTL
verdicts = storage.load_uid_verify_verdicts([A, B], 7 * 86400, 3600)
groups, html = render(verdicts, sender())
cand = {c["uid"]: c for c in groups[0]["candidates"]}
check("过期账号不存在结论仍判 not_exists（不退回未知）",
      cand[A]["status"] == "not_exists" and cand[A]["fresh"] is False,
      "(status=%s fresh=%s)" % (cand[A]["status"], cand[A]["fresh"]))
check("过期账号不存在候选不出现在可点击链接里", str(A) not in links(html), "(链接 %s)" % sorted(links(html)))
check("报告不保留已排除候选的 UID 或名片",
      "已排除账号" not in html and str(A) not in html)

print("=== 2. 未核验（未知）的候选也不得可点击 ===")
groups2, html2 = render({}, sender())
check("未知候选无跳转链接", str(A) not in links(html2) and str(B) not in links(html2))
check("未知候选进入「待复核」区块且文案明确",
      "待复核账号" in html2 and "不提供跳转" in html2 and "待复核" in html2)

print("=== 3. 唯一存在晋升只认新鲜证据 ===")
# A=过期不存在、B=新鲜存在：A 的证据过期，不能据此声称"唯一存在·高"
storage.save_uid_verify_cache(B, "exists", {"uid": B, "name": "乙", "sign": "s", "face": "https://x/y.png"})
verdicts3 = storage.load_uid_verify_verdicts([A, B], 7 * 86400, 3600)
_, html3 = render(verdicts3, sender())
check("含过期证据时不得判「唯一存在候选 · 高」", "唯一存在候选" not in html3)
check("新鲜存在的候选才可点击", links(html3) == {str(B)}, "(链接 %s)" % sorted(links(html3)))

print("=== 4. 终审循环：调整→等待→收敛；双 404 判 final ===")
storage.save_uid_verify_cache(A, "not_exists", {}, time.time())
storage.save_uid_verify_cache(B, "not_exists", {}, time.time())
row = sender(recorded={str(B): "not_exists"})
plan = [("not_exists", A), ("exists", B),       # 第 1 轮：B 调整（此前 404，现在存在）
        ("not_exists", B), ("not_exists", A),   # 第 2 轮：先重查调整的 B；A 第二次确认 → final
        ("not_exists", B)]                      # 第 3 轮：B 第二次确认 → final，随后收敛
seen = []


def fake_verify(uid, client):
    status, expect = plan[len(seen)]
    seen.append(uid)
    assert uid == expect, "核验顺序/对象不符: %s != %s" % (uid, expect)
    info = {"uid": uid, "name": "X", "sign": "s", "face": "https://x/y.png"} if status == "exists" else {}
    return status, info, False


slept = []
captured = {}
with patch.object(cards, "_verify_single", side_effect=fake_verify), \
     patch("time.sleep", side_effect=lambda s: slept.append(s)), \
     patch.object(config, "UID_RECHECK_INTERVAL", 7):
    stats = cards.recheck_ambiguous_candidates({H: row}, None,
                                               on_progress=lambda h, info: captured.update(info["resolution"]))
check("调整被识别，并按配置间隔等待后继续复核",
      stats["flipped"] == 1 and slept == [7, 7], "(flipped=%s slept=%s)" % (stats["flipped"], slept))
check("核验次数与轮次符合预期（3 轮 5 次）",
      stats["checked"] == 5 and len(seen) == 5, "(checked=%s)" % stats["checked"])
check("一轮内无调整即收敛：不再有待复核项", stats["pending"] == 0 and stats["stopped"] is False,
      "(pending=%s)" % stats["pending"])
check("两个候选各累计 2 次明确 -404 → 均判 final 彻底排除",
      stats["finalized"] == 2
      and storage.load_uid_verify_verdicts([A], 7 * 86400, 3600)[A]["final"] is True
      and storage.load_uid_verify_verdicts([B], 7 * 86400, 3600)[B]["final"] is True)
check("调整过的候选重查后确认次数重新累计（不是靠旧结论直接判死）",
      (storage.load_uid_verify_verdicts([B], 7 * 86400, 3600)[B]["info"] or {}).get("miss_count") == 2)

def clear_verdicts():
    """清空核验库：让后续小节从"无结论"状态开始（A/B 是唯一真实碰撞对，不能换 UID）。"""
    from contextlib import closing
    with closing(storage.get_db()) as conn:
        conn.execute("DELETE FROM uid_verify_cache")
        conn.commit()


print("=== 5. 风控熔断：停止并留待下次 ===")
clear_verdicts()
row5 = sender(recorded={str(B): "not_exists"})
with patch.object(cards, "_verify_single", return_value=("unknown", {}, True)):
    stats5 = cards.recheck_ambiguous_candidates({row5["mid_hash"]: row5}, None,
                                                on_progress=lambda h, info: captured.update(info["resolution"]))
check("风控信号 → 立即停止且剩余项留待下次",
      stats5["stopped"] is True and stats5["pending"] >= 1,
      "(stopped=%s pending=%s)" % (stats5["stopped"], stats5["pending"]))
check("未收敛写入 recheck_pending（含冷却时刻）",
      isinstance(captured.get("recheck_pending"), dict) and captured["recheck_pending"],
      "(%s)" % (list((captured.get("recheck_pending") or {}).keys())[:3]))

print("=== 6. 冷却与 final 周期：不重复打扰 ===")
clear_verdicts()
row6 = sender(recorded={str(B): "not_exists"},
              waiting={str(A): {"next_check_at": time.time() + 600},
                       str(B): {"next_check_at": time.time() + 600}})
with patch.object(cards, "_verify_single", side_effect=AssertionError("冷却期间不应发起核验")):
    stats6 = cards.recheck_ambiguous_candidates({row6["mid_hash"]: row6}, None)
check("冷却期内的候选一律不复核（到期目标为 0，零请求）",
      stats6["checked"] == 0 and stats6["targets"] == 0,
      "(targets=%s checked=%s)" % (stats6["targets"], stats6["checked"]))

clear_verdicts()
storage.save_uid_verify_cache(A, "not_exists", {"final": True}, time.time())
storage.save_uid_verify_cache(B, "not_exists", {"final": True}, time.time())
final_row = sender(recorded={str(B): "not_exists"})
with patch.object(cards, "_verify_single", side_effect=AssertionError("final 未到周期不应复核")):
    stats7 = cards.recheck_ambiguous_candidates({final_row["mid_hash"]: final_row}, None)
check("final 结论在复核周期内不再核验", stats7["checked"] == 0 and stats7["targets"] == 2,
      "(targets=%s checked=%s)" % (stats7["targets"], stats7["checked"]))

print("=== 8. 缓存里没有 not_exists 时，exists 不算调整 ===")
clear_verdicts()
row8 = sender(recorded={str(A): "not_exists", str(B): "not_exists"})
slept8 = []


def verify_exists(uid, client):
    return "exists", {"uid": uid, "name": "已存在", "sign": "s", "face": "https://x/y.png"}, False


with patch.object(cards, "_verify_single", side_effect=verify_exists), \
     patch("time.sleep", side_effect=lambda s: slept8.append(s)), \
     patch.object(config, "UID_RECHECK_INTERVAL", 7), \
     patch.object(config, "UID_RECHECK_MAX_ROUNDS", 3):
    stats8 = cards.recheck_ambiguous_candidates({H: row8}, None)
check("上一份为 unknown、本轮 exists：flipped==0 且不会把轮数跑满",
      stats8["flipped"] == 0 and stats8["checked"] == 2 and slept8 == [] and stats8["pending"] == 0,
      "(flipped=%s checked=%s slept=%s pending=%s)" % (
          stats8["flipped"], stats8["checked"], slept8, stats8["pending"]))

print("=== 9. 一批第一个就风控时，同批其余 UID 也留在 recheck_pending ===")
clear_verdicts()
row9 = sender(recorded={str(A): "not_exists", str(B): "not_exists"})
captured9 = {}
with patch.object(cards, "_verify_single", return_value=("unknown", {}, True)):
    stats9 = cards.recheck_ambiguous_candidates({H: row9}, None,
                                                on_progress=lambda h, info: captured9.update(info["resolution"]))
pending9 = captured9.get("recheck_pending") or {}
check("风控中断后本批未查 UID 仍写入 recheck_pending",
      stats9["stopped"] is True and stats9["checked"] == 1
      and str(A) in pending9 and str(B) in pending9,
      "(checked=%s pending=%s)" % (stats9["checked"], sorted(pending9)))

print("=== 10. 调整重查后按最终存在性回写发送者 ===")
clear_verdicts()
storage.save_uid_verify_cache(A, "not_exists", {"miss_count": 1}, time.time())
storage.save_uid_verify_cache(B, "not_exists", {}, time.time())
row10 = sender(recorded={str(A): "not_exists", str(B): "not_exists"})
row10["uid"] = A
plan10 = [("not_exists", A), ("exists", B), ("exists", B)]
seen10 = []


def verify_converge(uid, client):
    status, expect = plan10[len(seen10)]
    seen10.append(uid)
    assert uid == expect, "核验顺序不符: %s != %s" % (uid, expect)
    info = {"uid": uid, "name": "乙", "sign": "s", "face": "https://x/y.png"} if status == "exists" else {}
    return status, info, False


with patch.object(cards, "_verify_single", side_effect=verify_converge), \
     patch("time.sleep", side_effect=lambda s: None), \
     patch.object(config, "UID_RECHECK_INTERVAL", 0):
    stats10 = cards.recheck_ambiguous_candidates({H: row10}, None)
check("一轮内收敛后 uid 改为唯一存在者且置信度为高",
      stats10["flipped"] == 1 and stats10["pending"] == 0 and stats10["checked"] == 3
      and row10["uid"] == B and row10["confidence"] == "高" and seen10 == [A, B, B],
      "(uid=%s confidence=%s method=%s seen=%s)" % (
          row10["uid"], row10["confidence"], row10.get("method"), seen10))
check("被替换的旧 uid 留在 resolution.previous_uid",
      row10["resolution"].get("previous_uid") == A
      and storage.load_uid_verify_verdicts([A], 7 * 86400, 3600)[A]["final"] is True)

print("=== 11. 无效存在结论不冲掉 final，有效名片可调整 ===")
clear_verdicts()
storage.save_uid_verify_cache(A, "not_exists", {"miss_count": 2, "final": True})
storage.save_uid_verify_cache(A, "exists", {})
kept = storage.load_uid_verify_verdicts([A], 7 * 86400, 3600)[A]
check("已 final 的 not_exists 不被无 UID 的空的存在结论覆盖",
      kept["status"] == "not_exists" and kept["final"] is True and kept["info"].get("miss_count") == 2,
      "(status=%s final=%s miss=%s)" % (kept["status"], kept["final"], kept["info"].get("miss_count")))
storage.save_uid_verify_cache(A, "exists", {"uid": A, "name": "常规调整", "sign": "s", "face": "https://x/y.png"})
overwritten = storage.load_uid_verify_verdicts([A], 7 * 86400, 3600)[A]
check("带自洽 UID 的常规账号存在可以覆盖 final not_exists", overwritten["status"] == "exists",
      "(status=%s)" % overwritten["status"])
storage.save_uid_verify_cache(B, "not_exists", {"miss_count": 2, "final": True})
storage.save_uid_verify_cache(B, "not_exists", {})
kept_b = storage.load_uid_verify_verdicts([B], 7 * 86400, 3600)[B]
check("常规写 not_exists 空字典不丢已有 miss_count 与 final",
      kept_b["status"] == "not_exists" and kept_b["info"].get("miss_count") == 2 and kept_b["final"] is True,
      "(miss=%s final=%s)" % (kept_b["info"].get("miss_count"), kept_b["final"]))

print("=== 7. 画像卡/榜单的 UID 链接同样受存在性约束 ===")
from report import generate_user_card

BASE = {"uid": A, "name": "甲", "face": "https://i0.hdslb.com/x.png", "level": 6,
        "follower": 0, "following": 0, "resolve_method": "CRC32破解"}
now_ts = time.time()
fresh_card = {**BASE, "_resolution": {"candidate_status": {str(A): "exists"},
                                       "candidate_checked_at": {str(A): now_ts}}}
check("CRC 来源 + 新鲜存在 → 画像卡给跳转",
      f"space.bilibili.com/{A}" in generate_user_card(fresh_card))
stale_card = {**BASE, "_resolution": {"candidate_status": {str(A): "exists"},
                                       "candidate_checked_at": {str(A): now_ts - 8 * 86400}}}
check("CRC 来源但核验已过期 → 不给跳转（可能已注销）",
      f"space.bilibili.com/{A}" not in generate_user_card(stale_card))
neg_card = {**BASE, "_resolution": {"candidate_status": {str(A): "not_exists"},
                                     "candidate_checked_at": {str(A): now_ts}}}
check("账号不存在结论 → 不给跳转", f"space.bilibili.com/{A}" not in generate_user_card(neg_card))
plain_card = {"uid": B, "name": "乙", "face": "https://i0.hdslb.com/y.png", "level": 6,
              "follower": 0, "following": 0, "resolve_method": "评论区验证"}
check("明文来源（评论区验证）→ 照常可跳转",
      f"space.bilibili.com/{B}" in generate_user_card(plain_card))

print("")
print("==== 终审复核与候选三态: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
