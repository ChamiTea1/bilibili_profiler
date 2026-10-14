#!/usr/bin/env python3
"""目标分析离线回归：独立临时库、模拟采集，禁止真实网络/Cookie/LLM 调用。"""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from target_filter import make_selector, analysis_id, match_danmaku

CASES = []


def case(name):
    def register(fn):
        CASES.append((name, fn))
        return fn
    return register


def raises(fn, kind=ValueError):
    try:
        fn()
    except kind:
        return
    raise AssertionError(f"未抛出 {kind.__name__}")


def dm(content, uid=123456789, page=1, sec=10):
    return {"content": content, "mid_hash": flow.calc_crc32(uid), "time": sec,
            "timestamp": 1700000000, "color": "#ffffff", "page": page,
            "dmid": f"{uid}:{page}:{sec}:{content}", "mode": 1, "pool": 0}


@contextlib.contextmanager
def pipeline(rows=None, bvid="BVTARGET", comment_map=None, probe=None, **overrides):
    rows = rows if rows is not None else [dm("目标", sec=20), dm("普通发言", sec=30), dm("目标", page=2, sec=5)]
    video = {"bvid": bvid, "title": "离线目标分析视频", "aid": 1, "cid": 1,
             "pages": [{"page": 1, "cid": 1}, {"page": 2, "cid": 2}]}
    mapping = comment_map if comment_map is not None else {flow.calc_crc32(123456789): {123456789}}
    def danmaku(*a, **kw):
        storage.save_video_info(bvid, video)
        storage.save_uid_evidence_maps(bvid, [(target.METHOD_COMMENT_VERIFY, mapping)])
        return video, rows, flow.group_by_sender(rows), []
    forbidden = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("目标模式不可进入完整视频流程"))
    stubs = {"get_auth_client": lambda: object(), "phase_login": forbidden, "build_pool": forbidden,
             "phase_danmaku": forbidden, "phase_comment": forbidden, "supplement_uid_replies": forbidden,
             "fetch_command_dms": lambda *a: [], "fetch_charge_uid_map": lambda *a: {},
             "phase_cringe": lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不可做全视频 LLM 判定")),
             "phase_comment_cringe": lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不可引入非目标评论作者"))}
    stubs.update(overrides)
    probe = probe or (lambda *a, **kw: ({}, [], {"requests": 0, "budget": 3, "reason": "end"}))
    with patch.multiple(flow, **stubs), patch.multiple(target, collect_target_danmaku=danmaku, probe_comment_uids=probe):
        yield video


def run(pattern="目标", **kwargs):
    return target.run_target_analysis("BVTARGET", make_selector(pattern), launch_web=False, skip_deep=True, **kwargs)


@case("普通文本自动完整匹配，不误收子串")
def literal():
    selector = make_selector("目标")
    assert selector["mode"] == "literal"
    assert [d["content"] for d in match_danmaku([{"content": c} for c in ("目标", "目标呀", " 目标")], selector)] == ["目标"]


@case("正则元字符自动识别并搜索")
def auto_regex():
    selector = make_selector(r"目标\d+|特别.*")
    assert selector["mode"] == "regex"
    rows = [{"content": c} for c in ("这条目标12", "普通", "特别弹幕")]
    assert [d["content"] for d in match_danmaku(rows, selector)] == ["这条目标12", "特别弹幕"]


@case("--literal 能完整匹配正则标点")
def punctuation():
    text = "这是真的吗？(a+b)."
    rows = [{"content": c} for c in (text, "这是真的吗？aabx")]
    assert match_danmaku(rows, make_selector(text, "literal")) == rows[:1]


@case("--regex 支持无元字符的子串搜索")
def forced_regex():
    assert match_danmaku([{"content": "目标呀"}], make_selector("目标", "regex"))


@case("空输入、错误正则和超长模式提前拒绝")
def invalid():
    for pattern in ("", "[", "a" * (config.TARGET_PATTERN_MAX_LENGTH + 1)):
        raises(lambda: make_selector(pattern))
    assert make_selector("[", "literal")["mode"] == "literal"


@case("原文空白保留，空弹幕池可匹配")
def whitespace():
    assert make_selector(" 目标 ")["pattern"] == " 目标 "
    assert match_danmaku([], make_selector(".*")) == []


@case("病态正则超时终止，不卡住主流程")
def regex_timeout():
    start = time.monotonic()
    raises(lambda: match_danmaku([{"content": "a" * 10000 + "!"}], make_selector("(a+)+$"), timeout=0.4))
    assert time.monotonic() - start < 4


@case("相同实际筛选条件复用存档，不同模式/视频独立")
def identities():
    selector = make_selector("目标")
    assert analysis_id("BVA", selector) == analysis_id("BVA", make_selector("目标", "literal"))
    assert len({analysis_id("BVA", selector), analysis_id("BVB", selector), analysis_id("BVA", make_selector("目标", "regex"))}) == 3


@case("低刷屏分目标照常解析，只采集命中者")
def direct_target():
    uid = 123456789
    storage.save_user_data(uid, "离线名字甲", 6, {"uid": uid, "name": "离线名字甲", "level": 6}, {})
    rows = [dm("目标"), dm("普通", sec=30), dm("无关", uid=88888888)]
    with pipeline(rows):
        key = run(skip_collect=True)
    result = storage.load_target_analysis(key)["result"]
    assert result["stats"]["profile_count"] == 1
    assert result["senders"][0]["confidence"] == "高"
    assert result["senders"][0]["matched_count"] == 1
    assert result["profiles"][0]["danmaku"]["count"] == 2
    assert flow.calc_crc32(88888888) not in [s["mid_hash"] for s in result["senders"]]


@case("目标分析及深掘均不改写常规发送者/视频画像")
def isolation():
    uid, h = 123456789, flow.calc_crc32(123456789)
    storage.save_sender("BVTARGET", h, uid, "中", "评论交叉验证", 88, ["原报告"], "高", 20)
    old_profile = {"uid": uid, "name": "原报告", "ai_deep": "原深掘"}
    storage.save_user_data(uid, "离线名字甲", 6, {"uid": uid, "name": "离线名字甲", "level": 6}, old_profile, bvid="BVTARGET")
    before = storage.load_senders("BVTARGET")
    before_profile = storage.load_video_profile("BVTARGET", uid)
    before_shared = storage.load_user_data(uid)[1]
    with pipeline():
        key = run(skip_collect=True)
    assert storage.load_senders("BVTARGET") == before, "常规发送者发生变化"
    assert storage.load_video_profile("BVTARGET", uid) == before_profile, "常规视频画像发生变化"
    assert storage.load_user_data(uid)[1] == before_shared, "共享旧画像发生变化"
    record = storage.load_target_analysis(key)
    assert record["result"]["profiles"][0]["name"] == "离线名字甲", "目标画像昵称错误"
    fake = type("Fake", (), {"analyze_deep": lambda self, *a: {uid: "目标深掘"}})
    profiles = [{"uid": uid}]
    with patch.multiple(flow, LLM_API_KEY="离线占位", LLMAnalyzer=fake):
        flow.phase_ai_analysis({"bvid": "BVTARGET"}, profiles, persist=False)
    assert profiles[0]["ai_deep"] == "目标深掘", "目标深掘未注入"
    assert storage.load_video_profile("BVTARGET", uid) == before_profile, "目标深掘改写常规画像"
    assert storage.load_user_data(uid)[1] == before_shared


@case("重复执行仅复用当前版本、相同证据的解析")
def cache_reuse():
    with pipeline():
        run(skip_collect=True)
        with patch.object(target, "resolve_all_senders", side_effect=AssertionError("应复用当前解析")):
            run(skip_collect=True)


@case("过期解析和 --force 都重新核验")
def cache_expiry():
    with pipeline():
        key = run(skip_collect=True)
        record = storage.load_target_analysis(key)
        record["result"]["senders"][0]["resolution"]["checked_at"] = 0
        storage.save_target_analysis(key, "BVTARGET", record["selector"], record["result"])
        with patch.object(target, "resolve_all_senders", wraps=target.resolve_all_senders) as resolve:
            run(skip_collect=True)
            assert resolve.call_count == 1
        with patch.object(target, "resolve_all_senders", wraps=target.resolve_all_senders) as resolve:
            run(force=True, skip_collect=True)
            assert resolve.call_count == 1


@case("人数上限按命中量排序，未入选者仍完整保留")
def cap():
    rows = [dm("目标", uid=22222222), dm("目标", sec=20), dm("目标", sec=30)]
    with pipeline(rows):
        key = run(max_users=1, skip_collect=True)
    result = storage.load_target_analysis(key)["result"]
    assert result["stats"]["omitted_count"] == 1 and len(result["matches"]) == 3
    assert len(result["senders"]) == 2 and result["senders"][0]["uid"] == 123456789
    assert result["senders"][1]["status"] == "未入选（人数上限）"


@case("目标画像的刷屏相对阈值仍使用全视频发送者分布")
def spam_population():
    rows = [dm("目标"), dm("普通", uid=88888888)]
    with pipeline(rows), patch.object(flow, "batch_detect_spam", wraps=flow.batch_detect_spam) as spam:
        run(skip_collect=True)
    assert set(spam.call_args.args[0]) == {flow.calc_crc32(123456789), flow.calc_crc32(88888888)}


@case("零命中生成报告，跳过评论/解析/用户/LLM")
def empty_report():
    forbidden = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("零命中不应进入后续阶段"))
    with pipeline(phase_comment=forbidden, phase_collect_users=forbidden, phase_ai_analysis=forbidden):
        key = run("没有命中", skip_collect=True)
    result = storage.load_target_analysis(key)["result"]
    assert result["status"] == "completed" and result["stats"]["matched_count"] == 0
    assert len(result["exports"]) == 2


@case("低置信度候选保留原文但不自动采集")
def low_confidence():
    def resolve(groups, *a, **kw):
        return {h: {**g, "uid": 77777777, "confidence": "低", "method": "CRC32破解", "danmaku_count": g["count"],
                    "candidates": [77777777], "resolution": {}, "collision_risk": True} for h, g in groups.items()}
    with pipeline(), patch.object(target, "resolve_all_senders", resolve), patch.object(flow, "collect_user_data", side_effect=AssertionError("低置信度不可采集")):
        key = run(force=True)
    result = storage.load_target_analysis(key)["result"]
    assert not result["profiles"] and result["stats"]["low_confidence_count"] == 1
    assert result["senders"][0]["candidates"] == [77777777]


@case("多 UID 歧义保留候选，不生成画像")
def ambiguous():
    def resolve(groups, *a, **kw):
        return {h: {**g, "uid": None, "confidence": "无", "method": "碰撞歧义", "danmaku_count": g["count"],
                    "candidates": [11111111, 22222222], "resolution": {"reason": "多个候选"}} for h, g in groups.items()}
    with pipeline(), patch.object(target, "resolve_all_senders", resolve):
        key = run(force=True, skip_collect=True)
    result = storage.load_target_analysis(key)["result"]
    assert result["stats"]["unresolved_count"] == 1 and len(result["senders"][0]["candidates"]) == 2


@case("按需探测只复核新增目标证据，保留已有本视频关联")
def comment_merge():
    uid, h = 765432101, flow.calc_crc32(765432101)
    rows = [dm("目标"), dm("目标", uid=uid)]
    def resolve(groups, mapping, *a, **kw):
        return {key: {**group, "uid": uid if key == h else 123456789,
                      "confidence": "中" if key in mapping else "低", "method": "测试解析",
                      "danmaku_count": group["count"], "resolution": {}}
                for key, group in groups.items()}
    def probe(*a, **kw):
        return {h: {uid}}, [], {"requests": 1, "budget": 3, "reason": "found_all"}
    with pipeline(rows, probe=probe), patch.object(target, "resolve_all_senders", resolve):
        key = run(force=True, skip_collect=True)
    senders = storage.load_target_analysis(key)["result"]["senders"]
    assert {row["uid"] for row in senders} == {123456789, uid}
    assert all(row["confidence"] == "中" for row in senders)


@case("采集异常后仍用其他 UID 来源并保留报告")
def comment_fallback():
    def broken(*a, **kw):
        raise RuntimeError("离线模拟失败")
    def resolve(groups, *a, **kw):
        return {h: {**g, "uid": None, "confidence": "无", "method": "待验证", "danmaku_count": g["count"], "resolution": {}} for h, g in groups.items()}
    with pipeline(probe=broken), patch.object(target, "resolve_all_senders", resolve):
        key = run(force=True, skip_collect=True)
    result = storage.load_target_analysis(key)["result"]
    assert result["status"] == "completed" and result["uid_lookup"]["reason"] == "api_error"


@case("中断保留 UID 检查点，重跑可继续用户采集")
def interrupted():
    def interrupted(*a, **kw):
        raise KeyboardInterrupt()
    with pipeline(phase_collect_users=interrupted):
        raises(lambda: run(skip_collect=True), KeyboardInterrupt)
    key = analysis_id("BVTARGET", make_selector("目标"))
    result = storage.load_target_analysis(key)["result"]
    assert result["status"] == "interrupted" and result["senders"][0]["uid"] == 123456789
    with pipeline():
        run(skip_collect=True)
    assert storage.load_target_analysis(key)["result"]["status"] == "completed"


@case("多分P命中原文按分P排序，并准确跳转")
def multipart_api():
    with pipeline():
        key = run(skip_collect=True)
    response = web.app.test_client().get(f"/api/target/{key}/danmaku").get_json()
    assert response["total"] == 2
    assert [r["page"] for r in response["rows"]] == [1, 2]
    assert response["rows"][1]["video_url"].endswith("?p=2&t=5")
    assert response["rows"][1]["time_label"] == "P2 00:05"
    assert storage.load_target_analysis(key)["result"]["profiles"][0]["danmaku"]["video_pages"] == [1, 1, 2]


@case("历史覆盖范围按实际分P检查点读取")
def coverage():
    storage.set_phase_state("BVTARGET", "danmaku", "done:p2", "0")
    storage.set_phase_state("BVTARGET", "danmaku", "failed_dates:p2", "2026-01-01")
    with pipeline():
        key = run(skip_collect=True)
    history = storage.load_target_analysis(key)["result"]["coverage"]["history"]
    assert history[1]["failed_dates"] == "2026-01-01" and history[1]["done"] == "0"


@case("目标报告及 API 隐藏昵称/UID，正常时显示原值")
def mask():
    with pipeline():
        key = run(skip_collect=True)
    client = web.app.test_client()
    assert "离线名字甲" in client.get(f"/target/{key}").text
    client.set_cookie("mask", "1")
    body = client.get(f"/target/{key}").text
    assert "离线名字甲" not in body and "UID:123******" in body
    payload = client.get(f"/api/target/{key}/danmaku").get_json()
    assert "123456789" not in json.dumps(payload) and "（￣▽￣）甲" in payload["rows"][0]["sender_label"]


@case("筛选表达式与原文转义，不注入 HTML/脚本")
def xss():
    text = '<script>alert("测试")</script>'
    with pipeline([dm(text)]):
        key = target.run_target_analysis("BVTARGET", make_selector(text, "literal"), launch_web=False, skip_collect=True, skip_deep=True)
    body = web.app.test_client().get(f"/target/{key}").text
    assert text not in body and "&lt;script&gt;" in body


@case("目标画像展示沿用误报纠偏，撤销后恢复原快照")
def false_positive():
    with pipeline():
        key = run(skip_collect=True)
    record = storage.load_target_analysis(key)
    profile = record["result"]["profiles"][0]
    profile["danmaku"].update(spam_level="高", spam_score=20)
    profile["tags"] = ["重度刷屏"]
    profile["comments"] = [{"rpid": "target-fp", "content": "评论样本", "problem": "人身攻击"}]
    storage.save_target_analysis(key, "BVTARGET", record["selector"], record["result"])
    h = profile["target_mid_hash"]
    storage.toggle_false_positive("BVTARGET", "spam", h)
    storage.toggle_false_positive("BVTARGET", "cmt", "target-fp")
    client = web.app.test_client()
    body = client.get(f"/target/{key}").text
    assert "低·已标误报" in body and "重度刷屏" not in body and "人身攻击" not in body
    assert "评论样本" in body
    storage.toggle_false_positive("BVTARGET", "spam", h)
    storage.toggle_false_positive("BVTARGET", "cmt", "target-fp")
    body = client.get(f"/target/{key}").text
    assert "高风险 20.00分" in body and "重度刷屏" in body and "人身攻击" in body


@case("独立目标评论页显示完整原文、回复身份与跳转，即使没有画像")
def target_comments():
    with pipeline():
        key = run(skip_collect=True)
    record = storage.load_target_analysis(key)
    record["result"]["profiles"] = []
    record["result"]["comments"] = [{"uid": 123456789, "rpid": 99, "uname": "评论名字乙",
                                        "content": '<script>攻击</script>\n第二行', "is_sub": True,
                                        "ctime": 1700000000, "like": 8}]
    record["result"]["uid_lookup"] = {"requests": 7, "reply_requests": 1, "reason": "found_all"}
    storage.save_target_analysis(key, "BVTARGET", record["selector"], record["result"])
    client = web.app.test_client()
    body = client.get(f"/target/{key}#tab=comments").text
    assert 'id="tab-comments"' in body and 'data-tab="comments"' in body
    assert "&lt;script&gt;攻击&lt;/script&gt;\n第二行" in body and "<script>攻击</script>" not in body
    assert "评论名字乙" in body and "UID:123456789" in body and "点赞 8" in body and "#reply99" in body
    assert "默认无页数上限" in body and "已找到全部待搜索目标" in body
    client.set_cookie("mask", "1")
    masked = client.get(f"/target/{key}").text
    assert "评论名字乙" not in masked and "UID:123******" in masked
    assert "&lt;script&gt;攻击&lt;/script&gt;\n第二行" in masked


@case("目标评论分页、歧义标记和原文导出保持独立")
def comments_pagination():
    with pipeline():
        key = run(skip_collect=True)
    record = storage.load_target_analysis(key)
    record["result"]["comments"] = [{"uid": 555666777, "rpid": i + 1, "content": f"独立原文{i}", "ctime": i + 1}
                                       for i in range(5)]
    storage.save_target_analysis(key, "BVTARGET", record["selector"], record["result"])
    with patch.object(config, "TARGET_MATCH_PAGE_SIZE", 2):
        body = web.app.test_client().get(f"/target/{key}?comments_page=999").text
    assert "独立原文0" in body and "独立原文4" not in body and "碰撞候选，身份待核验" in body
    assert "第 3 / 3 页" in body and "comments_page=2" in body
    record["result"]["comments"] = [{"uid": 123456789, "rpid": 1, "content": "=目标评论公式"}]
    prefix = str(Path(TMP.name) / "comment_export")
    target.export_target(record["result"], prefix)
    with open(prefix + ".csv", encoding="utf-8-sig") as f:
        assert next(csv.DictReader(f))["目标评论"] == "'=目标评论公式"
    assert json.loads(Path(prefix + ".json").read_text())["comments"][0]["content"] == "=目标评论公式"


@case("命中 API 分页限界、按 hash 过滤、未知存档404")
def pagination():
    with pipeline([dm("目标", sec=i) for i in range(7)]):
        key = run(skip_collect=True)
    with patch.object(config, "TARGET_MATCH_PAGE_SIZE", 2):
        client = web.app.test_client()
        first = client.get(f"/api/target/{key}/danmaku?page=bad").get_json()
        last = client.get(f"/api/target/{key}/danmaku?page=999").get_json()
        empty = client.get(f"/api/target/{key}/danmaku?mid_hash=不存在").get_json()
    assert first["page"] == 1 and len(first["rows"]) == 2
    assert last["page"] == 4 and len(last["rows"]) == 1
    assert empty["total"] == 0 and empty["pages"] == 1
    assert client.get("/target/doesnotexist").status_code == 404
    missing = client.get("/target/doesnotexist").text
    assert "目标报告不存在或链接已失效" in missing and "视频不存在或尚未分析" not in missing
    assert client.get("/api/target/doesnotexist/danmaku").status_code == 404


@case("首页搜索框旁进入独立目标列表，多份存档及空列表均可查看")
def index_links():
    with pipeline():
        key = run("目标", skip_collect=True)
        other = run("另一目标", skip_collect=True)
    client = web.app.test_client()
    home = client.get("/").text
    from lxml import html
    search = html.fromstring(home).get_element_by_id("idxSearch")
    assert search.getnext().get("href") == "/targets"
    assert f"/target/{key}" not in home and f"/target/{other}" not in home
    with patch.object(web, "_get_client", side_effect=AssertionError("列表页不可登录或采集")):
        body = client.get("/targets").text
    assert f"/target/{key}" in body and f"/target/{other}" in body and 'href="/"' in body
    with patch.object(web, "list_target_analyses", return_value=[]):
        empty = client.get("/targets")
    assert empty.status_code == 200 and "暂无目标分析报告" in empty.text
    assert len(storage.list_target_analyses("BVTARGET")) >= 2


@case("CSV包含未解析者及防公式注入，JSON保留原文与筛选")
def export():
    result = {"selector": make_selector("=危险", "literal"), "profiles": [], "matches": [{"content": "=危险"}],
              "senders": [{"mid_hash": flow.calc_crc32(123456789), "uid": None, "name": "+名字", "danmaku_count": 9,
                           "matched_count": 2, "status": "未解析", "samples": ["=危险"], "candidates": [123456789]}]}
    prefix = str(Path(TMP.name) / "target_export")
    target.export_target(result, prefix)
    with open(prefix + ".csv", encoding="utf-8-sig") as f:
        row = next(csv.DictReader(f))
    assert row["昵称"].startswith("'+") and row["命中样本"].startswith("'=") and row["UID"] == ""
    assert row["候选UID"] == "123456789" and row["状态"] == "未解析"
    assert json.loads(Path(prefix + ".json").read_text())["matches"][0]["content"] == "=危险"


@case("目标导出可下载且不会混入常规导出列表")
def download():
    with pipeline():
        key = run(skip_collect=True)
    exports = storage.load_target_analysis(key)["result"]["exports"]
    client = web.app.test_client()
    assert all(client.get("/download/" + name).status_code == 200 for name in exports)
    assert not any(name in str(web._export_links("BVTARGET")) for name in exports)
    assert client.get("/download/target_secret.txt").status_code == 404
    assert client.get("/download/target_..%5Csecret.json").status_code == 404


@case("常规强制刷新保留目标快照，明确删除视频才删除")
def deletion():
    video = {"bvid": "BVDELETE", "title": "删除验证"}
    storage.save_video_info("BVDELETE", video)
    selector = make_selector("目标")
    key = analysis_id("BVDELETE", selector)
    storage.save_target_analysis(key, "BVDELETE", selector, {"video_info": video, "stats": {}, "status": "completed"})
    storage.clear_video_cache("BVDELETE")
    assert storage.load_target_analysis(key)
    storage.delete_video_data("BVDELETE")
    assert storage.load_target_analysis(key) is None


@case("命令行自动识别、原文覆盖与选项透传")
def cli():
    with patch.object(target, "run_target_analysis") as invoke, patch.object(sys, "argv", ["run.py", "BVTARGET", "^目标.*", "--max-users", "3", "--skip-collect", "--skip-deep", "--port", "9000"]):
        flow.main()
    assert invoke.call_args.args[1]["mode"] == "regex"
    assert invoke.call_args.kwargs == {"force": False, "max_users": 3, "skip_collect": True, "skip_deep": True,
                                       "port": 9000, "target_history": False, "target_pool": False}
    with patch.object(target, "run_target_analysis") as invoke, patch.object(sys, "argv", ["run.py", "BVTARGET", "[目标]", "--literal"]):
        flow.main()
    assert invoke.call_args.args[1]["mode"] == "literal"


@case("错误正则与参数组合在登录前失败，旧命令仍可用")
def cli_bad():
    with patch.object(flow, "get_auth_client", side_effect=AssertionError("错误参数不可登录")):
        for args in (["BVTARGET", "["], ["BVTARGET", "目标", "--batch", "文件.txt"], ["BVTARGET", "--regex"], ["BVTARGET", ""], ["BVTARGET", "--target-history"]):
            with patch.object(sys, "argv", ["run.py", *args]), contextlib.redirect_stderr(io.StringIO()):
                raises(flow.main, SystemExit)
    with patch.object(flow, "run_analysis") as invoke, patch.object(sys, "argv", ["run.py", "BVTARGET"]):
        flow.main()
    assert invoke.call_count == 1


@case("非法 BV/人数上限在采集前拒绝")
def bad_input():
    with patch.object(flow, "get_auth_client", side_effect=AssertionError("不应登录")):
        raises(lambda: target.run_target_analysis("BV../secret", make_selector("目标")))
        raises(lambda: run(max_users=0))


@case("自动打开独立目标页而非普通视频报告")
def launch():
    import web_autostart
    output = io.StringIO()
    with patch.object(web_autostart, "WEB_AUTOSTART", False), patch.dict(os.environ, {"PROFILER_PORT": "9000"}), contextlib.redirect_stdout(output):
        web_autostart.maybe_launch_web("BVTARGET", target_id="a" * 24)
    assert "http://127.0.0.1:9000/target/" + "a" * 24 in output.getvalue()


def main():
    global config, storage, flow, target, web, TMP
    import config
    import storage
    import requests
    import main as flow
    import target_analysis as target
    TMP = tempfile.TemporaryDirectory(prefix="bili_target_")
    storage.DB_PATH = config.DB_PATH = str(Path(TMP.name) / "isolated.db")
    config.REPORT_DIR = str(Path(TMP.name) / "reports")
    requests.sessions.Session.request = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("禁止真实网络"))
    with contextlib.redirect_stdout(io.StringIO()):
        storage.init_db()
    import web
    web.REPORT_DIR = config.REPORT_DIR
    passed = failed = 0
    try:
        for name, fn in CASES:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    fn()
                passed += 1
                print(f"  ✔ {name}")
            except Exception as e:
                failed += 1
                print(f"  ✘ {name}：{type(e).__name__}: {e}")
    finally:
        TMP.cleanup()
    print(f"==== 目标弹幕分析: {passed} 项通过, {failed} 项失败 ====")
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
