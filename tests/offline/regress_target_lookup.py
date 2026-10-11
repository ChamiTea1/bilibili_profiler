#!/usr/bin/env python3
"""目标模式轻量取数回归：找到即停、评论原文与独立续页，禁止真实网络。"""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
import config
import storage
import requests
import main
import target_analysis as target
import target_lookup as lookup
from target_filter import make_selector
from api_client import RiskControlError

TMP = tempfile.TemporaryDirectory(prefix="target_lookup_")
storage.DB_PATH = config.DB_PATH = str(Path(TMP.name) / "test.db")
config.REPORT_DIR = str(Path(TMP.name) / "reports")
requests.sessions.Session.request = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("禁止真实网络"))
main.get_auth_client = lambda: (_ for _ in ()).throw(AssertionError("禁止读取真实 Cookie"))
with contextlib.redirect_stdout(io.StringIO()):
    storage.init_db()

passed = failed = 0


def case(name):
    def run(fn):
        global passed, failed
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                fn()
            passed += 1
            print(f"  ✔ {name}")
        except Exception as exc:
            failed += 1
            print(f"  ✘ {name}：{type(exc).__name__}: {exc}")
        return fn
    return run


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


def video(bvid):
    return {"bvid": bvid, "aid": 42, "cid": 10, "title": "离线示例", "pages": [{"cid": 10}, {"cid": 20}]}


def dm(content="目标", uid=123456789, page=1, dmid=1):
    return {"content": content, "mid_hash": main.calc_crc32(uid), "page": page, "dmid": dmid,
            "time": 5, "timestamp": 1700000000, "color": "#ffffff", "mode": 1, "pool": 0}


def reply(uid, rpid, **kwargs):
    return {"member": {"mid": str(uid), "uname": "示例用户"}, "rpid": rpid,
            "content": {"message": "目标用户评论"}, **kwargs}


def response(replies, next_offset="next", end=False):
    return {"code": 0, "data": {"replies": replies,
                               "cursor": {"is_end": end, "pagination_reply": {"next_offset": next_offset}}}}


@case("默认只读当前池及缓存，不补历史，保留多分P与完整数据")
def current_and_cache():
    bvid = "BVCURRENT"
    cached = [dm("缓存历史", page=2, dmid=101), dm(dmid=102)]
    storage.save_video_info(bvid, video(bvid))
    storage.append_danmaku(bvid, cached, set())
    with patch.object(lookup, "get_video_info", return_value=video(bvid)), patch.object(lookup, "fetch_all_danmaku", return_value=[cached[1], dm("新增", dmid=103)]), patch.object(main, "_fetch_history_all_pages", side_effect=AssertionError("不应补历史")):
        meta, rows, groups, _ = lookup.collect_target_danmaku(bvid, object())
    assert len(rows) == 3 and any(row["page"] == 2 for row in rows)
    assert sum(group["count"] for group in groups.values()) == 3 and meta["bvid"] == bvid


@case("无dmid弹幕重跑去重，已有数据不删除")
def zero_dmid():
    bvid = "BVZEROID"
    row = dm(dmid=0)
    with patch.object(lookup, "get_video_info", return_value=video(bvid)), patch.object(lookup, "fetch_all_danmaku", return_value=[row]):
        lookup.collect_target_danmaku(bvid, object())
        lookup.collect_target_danmaku(bvid, object())
    assert len(storage.load_danmaku(bvid)) == 1


@case("新目标缓存不能冒充常规历史采集完成")
def incomplete_cache():
    bvid = "BVFRESH"
    with patch.object(lookup, "get_video_info", return_value=video(bvid)), patch.object(lookup, "fetch_all_danmaku", return_value=[dm()]):
        lookup.collect_target_danmaku(bvid, object())
    assert storage.get_phase_state(bvid, "danmaku", "format") == "v2"
    assert storage.get_phase_state(bvid, "danmaku", "done") is None
    assert storage.get_phase_state(bvid, "comment", "done") is None


@case("目标实时更新不改常规历史断点和覆盖统计")
def normal_checkpoint():
    bvid = "BVEXISTING"
    meta = dict(video(bvid), danmaku_coverage={"history": 99, "merged": 100})
    storage.save_video_info(bvid, meta)
    storage.append_danmaku(bvid, [dm()], set())
    storage.set_phase_state(bvid, "danmaku", "done", "1")
    storage.set_phase_state(bvid, "danmaku", "last_date", "2026-01-01")
    with patch.object(lookup, "get_video_info", return_value=video(bvid)), patch.object(lookup, "fetch_all_danmaku", return_value=[dm(dmid=2)]):
        lookup.collect_target_danmaku(bvid, object())
    assert storage.get_phase_state(bvid, "danmaku", "done") == "1"
    assert storage.get_phase_state(bvid, "danmaku", "last_date") == "2026-01-01"
    assert storage.load_video_info(bvid)["danmaku_coverage"] == meta["danmaku_coverage"]


@case("实时池失败降级到缓存，并保留失败分P")
def realtime_failure():
    bvid = "BVREALFAIL"
    meta = dict(video(bvid), realtime_failed_pages=[1, 2])
    storage.save_video_info(bvid, meta)
    storage.append_danmaku(bvid, [dm("库存")], set())
    with patch.object(lookup, "get_video_info", return_value=meta), patch.object(lookup, "fetch_all_danmaku", side_effect=lookup.DanmakuCollectionError(meta)):
        result, rows, _, _ = lookup.collect_target_danmaku(bvid, object())
    assert rows[0]["content"] == "库存" and result["realtime_failed_pages"] == [1, 2]


@case("视频信息失败仅有库存时可降级")
def metadata_failure():
    bvid = "BVMETAFAIL"
    storage.save_video_info(bvid, video(bvid))
    with patch.object(lookup, "get_video_info", side_effect=RuntimeError("离线失败")), patch.object(lookup, "fetch_all_danmaku", return_value=[]):
        assert lookup.collect_target_danmaku(bvid, object())[0]["bvid"] == bvid
        try:
            lookup.collect_target_danmaku("BVNONEXIST", object())
        except RuntimeError:
            pass
        else:
            raise AssertionError("无元信息不应凭空生成报告")


@case("实时模式按分P各取一次XML，不访问历史接口")
def live_calls():
    class LiveClient(Client):
        def get_raw(self, url, params):
            self.calls.append((url, params))
            data = f'<i><d p="5,1,25,16777215,1700000000,0,{main.calc_crc32(123456789)},{params["oid"]}">目标</d></i>'.encode()
            return type("Raw", (), {"content": data})()
    client = LiveClient([{"code": 0, "data": video("BVLIVE")}])
    _, rows, _, _ = lookup.collect_target_danmaku("BVLIVE", client)
    assert len(rows) == 2 and [r["page"] for r in rows] == [1, 2]
    assert [url for url, _ in client.calls] == [config.VIDEO_INFO_URL, config.DANMAKU_XML_URL, config.DANMAKU_XML_URL]


@case("历史补采仅显式启用时调用原采集阶段")
def optional_history():
    with patch.object(main, "phase_danmaku", return_value=(video("BVHIST"), [], {}, [])) as phase:
        lookup.collect_target_danmaku("BVHIST", object(), include_history=True)
    assert phase.call_count == 1 and phase.call_args.kwargs == {"resume": True}


@case("无目标/无aid/关闭预算均不请求评论")
def no_probe():
    client = Client([])
    h = main.calc_crc32(123456789)
    for meta, targets, budget in ((video("BVNO"), set(), 3), ({"bvid": "BVNO"}, {h}, 3), (video("BVNO"), {h}, 0)):
        assert lookup.probe_comment_uids(meta, client, targets, budget)[2]["requests"] == 0
    assert client.calls == []


@case("内嵌回复找到目标页末即停，不补子评论或保存其他作者正文")
def preview_hit():
    uid, bvid = 135791357, "BVPREVIEW"
    h = main.calc_crc32(uid)
    client = Client([response([reply(999, 1, rcount=10000, replies=[reply(uid, 2)])])])
    mapping, comments, stats = lookup.probe_comment_uids(video(bvid), client, {h})
    assert mapping == {h: {uid}} and stats["requests"] == 1 and stats["reason"] == "found_all"
    assert len(comments) == 1 and comments[0]["uid"] == uid
    assert len(client.calls) == 1 and client.calls[0][0] == config.COMMENT_MAIN_WBI_URL
    assert storage.load_comments(bvid) == []
    assert storage.get_phase_state(bvid, "comment", "done") is None
    assert storage.get_phase_state(bvid, "uid_harvest", "done") is None


@case("同页CRC碰撞候选全部保留，不命中第一人就结束解析页面")
def collision():
    a, b = 12345678, 7533489035
    h = main.calc_crc32(a)
    assert h == main.calc_crc32(b)
    client = Client([response([reply(a, 1), reply(b, 2)])])
    mapping, comments, stats = lookup.probe_comment_uids(video("BVCOLLISION"), client, {h})
    assert mapping[h] == {a, b} and len(comments) == 2 and stats["requests"] == 1


@case("调用方显式限额时独立续页，常规游标不变")
def budget_resume():
    uid, bvid = 246802468, "BVPROBEBOUNDS"
    h = main.calc_crc32(uid)
    storage.set_phase_state(bvid, "comment", "offset", "常规游标")
    client = Client([response([reply(9000 + i, i)], next_offset=str(i)) for i in range(1, 4)])
    mapping, _, stats = lookup.probe_comment_uids(video(bvid), client, {h}, max_requests=3)
    assert not mapping and stats["requests"] == 3 and stats["reason"] == "budget"
    assert len(client.calls) == 3
    again = Client([response([reply(uid, 4)], next_offset="4")])
    assert lookup.probe_comment_uids(video(bvid), again, {h})[0] == {h: {uid}}
    assert json.loads(again.calls[0][1]["pagination_str"])["offset"] == "3"
    assert storage.get_phase_state(bvid, "comment", "offset") == "常规游标"


@case("旧接口降级计入同一预算，额度不足不追加请求")
def fallback_budget():
    uid, h = 987123987, main.calc_crc32(987123987)
    success = {"code": 0, "data": {"replies": [reply(uid, 1)], "cursor": {"is_end": True, "next": 1}}}
    client = Client([{"code": -400}, success])
    assert lookup.probe_comment_uids(video("BVFALLBACK"), client, {h}, 2)[0] == {h: {uid}}
    assert [url for url, _ in client.calls] == [config.COMMENT_MAIN_WBI_URL, config.COMMENT_MAIN_URL]
    limited = Client([{"code": -400}])
    assert lookup.probe_comment_uids(video("BVFALLBACKONE"), limited, {h}, 1)[2]["requests"] == 1
    assert len(limited.calls) == 1


@case("风控立即停止，不切旧接口继续尝试")
def risk_stop():
    h = main.calc_crc32(123456789)
    for index, item in enumerate(({"code": -412}, {"code": -352}, {"code": -403}, RiskControlError("风控"))):
        client = Client([item])
        stats = lookup.probe_comment_uids(video(f"BVRISK{index}"), client, {h})[2]
        assert stats["reason"] == "risk" and len(client.calls) == 1


@case("重复页面和缺失游标不会无限翻页或伪造完成标记")
def bad_cursor():
    h = main.calc_crc32(123456789)
    repeated = response([reply(111, 1)], "same")
    client = Client([repeated, repeated])
    assert lookup.probe_comment_uids(video("BVREPEATPROBE"), client, {h})[2]["reason"] == "repeated_page"
    missing = Client([{"code": 0, "data": {"replies": [reply(111, 1)]}}])
    assert lookup.probe_comment_uids(video("BVCURSORMISSING"), missing, {h})[2]["reason"] == "invalid_cursor"
    with storage.get_db() as conn:
        assert not conn.execute("SELECT 1 FROM phase_state WHERE bvid=? AND key='done' AND value='1'", ("BVCURSORMISSING",)).fetchone()


@case("坏正文仍可保留有效UID，坏响应不写完成")
def malformed_data():
    uid, h = 112233445, main.calc_crc32(112233445)
    client = Client([response([reply(uid, 1, content="非字典正文")])])
    mapping, comments, _ = lookup.probe_comment_uids(video("BVBODYBAD"), client, {h})
    assert mapping == {h: {uid}} and not comments
    client = Client([{"code": 0, "data": {"replies": None, "cursor": []}}, {"code": 0, "data": None}])
    assert lookup.probe_comment_uids(video("BVDATABAD"), client, {h})[2]["reason"] == "api_error"


@case("库存评论只读选中UID，兼容小SQLite参数上限")
def filtered_comments():
    bvid = "BVFILTERCOMMENTS"
    storage.save_comments(bvid, [{"uid": i, "rpid": i, "content": f"评论{i}"} for i in range(1, 14)])
    original = storage.get_db
    def limited():
        conn = original()
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 5)
        return conn
    with patch.object(storage, "get_db", limited):
        assert {c["uid"] for c in storage.load_comments(bvid, uids=list(range(1, 12, 2)))} == set(range(1, 12, 2))
        assert storage.load_comments(bvid, uids=[]) == []
    assert len(storage.load_comments(bvid)) == 13


@case("目标流程先解析后按需探测，只为命中者采集，不启动池")
def target_order():
    bvid, uid = "BVORDER", 551122334
    row = dm(uid=uid)
    h = row["mid_hash"]
    events = []
    client = object()
    def resolve(groups, *a, **kw):
        events.append("resolve")
        return {key: {**group, "uid": uid, "confidence": "中" if events.count("resolve") > 1 else "低",
                      "method": "测试解析", "danmaku_count": group["count"], "resolution": {}}
                for key, group in groups.items()}
    def probe(*a, **kw):
        events.append("probe")
        assert events[0] == "resolve" and a[2] == {h}
        return {h: {uid}}, [], {"requests": 1, "budget": 3, "reason": "found_all"}
    def collect(resolved, pool, **kw):
        events.append("collect")
        assert pool is client and set(resolved) == {h}
        return {uid: {"uid": uid, "name": "目标用户", "level": 6}}
    forbidden = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不能走完整采集或启动IP池"))
    rows = [row, dm("无关", uid=661122334, dmid=2)]
    with patch.multiple(main, get_auth_client=lambda: client, build_pool=forbidden, phase_comment=forbidden,
                        supplement_uid_replies=forbidden, fetch_command_dms=lambda *a: [], phase_collect_users=collect), patch.multiple(target, collect_target_danmaku=lambda *a, **kw: (video(bvid), rows, main.group_by_sender(rows), []), probe_comment_uids=probe, resolve_all_senders=resolve):
        key = target.run_target_analysis(bvid, make_selector("目标"), launch_web=False, skip_deep=True)
    assert events == ["resolve", "probe", "resolve", "collect"]
    result = storage.load_target_analysis(key)["result"]
    assert result["stats"]["profile_count"] == 1 and result["uid_lookup"]["requests"] == 1
    assert result["coverage"]["source_mode"] == "realtime_cache"


@case("已有可靠本视频关联及评论正文时复用，不抓额外元信息")
def known_target():
    bvid, uid = "BVKNOWNLOCAL", 661199223
    row = dm(uid=uid)
    storage.save_uid_evidence_maps(bvid, [(target.METHOD_COMMENT_VERIFY, {row["mid_hash"]: {uid}})])
    storage.save_comments(bvid, [{"uid": uid, "rpid": 1, "content": "已存原文"}])
    forbidden = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("已有本视频关联无需额外探测"))
    with patch.multiple(main, get_auth_client=lambda: object(), build_pool=forbidden, phase_comment=forbidden,
                        fetch_command_dms=forbidden, fetch_charge_uid_map=forbidden), patch.multiple(target, collect_target_danmaku=lambda *a, **kw: (video(bvid), [row], main.group_by_sender([row]), []), probe_comment_uids=forbidden):
        key = target.run_target_analysis(bvid, make_selector("目标"), launch_web=False, skip_collect=True, skip_deep=True)
    result = storage.load_target_analysis(key)["result"]
    assert result["uid_lookup"]["requests"] == 0 and result["uid_lookup"]["reason"] == "not_needed"
    assert result["comments"][0]["content"] == "已存原文"


@case("默认超过3页继续搜索，第7页找到即停")
def no_default_limit():
    uid, h = 228877665, main.calc_crc32(228877665)
    pages = [response([reply(90000 + i, i)], str(i)) for i in range(1, 7)]
    pages.append(response([reply(uid, 7)], "7"))
    client = Client(pages)
    mapping, comments, stats = lookup.probe_comment_uids(video("BVSEVENTH"), client, {h})
    assert mapping == {h: {uid}} and stats["requests"] == 7 and stats["budget"] is None
    assert stats["reason"] == "found_all" and len(client.calls) == 7
    assert comments[0]["content"] == "目标用户评论"


@case("多个目标逐页找到，不能找到第一人就停")
def all_targets():
    a, b = 77112233, 88112233
    ah, bh = main.calc_crc32(a), main.calc_crc32(b)
    client = Client([response([reply(a, 1)], "1"), response([reply(111, 2)], "2"), response([reply(b, 3)], "3")])
    mapping, comments, stats = lookup.probe_comment_uids(video("BVMULTITARGET"), client, {ah, bh})
    assert mapping == {ah: {a}, bh: {b}} and {c["uid"] for c in comments} == {a, b}
    assert stats["requests"] == 3 and stats["reason"] == "found_all"


@case("主评未找到才查隐藏回复，找到即停且不补采其他楼")
def hidden_reply():
    uid, h = 332211889, main.calc_crc32(332211889)
    child = {"code": 0, "data": {"replies": [reply(uid, 101, parent=1)], "page": {"count": 80, "size": 20}}}
    client = Client([response([reply(111, 1, rcount=80), reply(222, 2, rcount=100)], end=True), child])
    mapping, comments, stats = lookup.probe_comment_uids(video("BVHIDDENREPLY"), client, {h})
    assert mapping == {h: {uid}} and stats["requests"] == 2 and stats["reply_requests"] == 1
    assert client.calls[1][0] == config.COMMENT_REPLY_URL and client.calls[1][1]["root"] == 1
    assert len(comments) == 1 and comments[0]["is_sub"] and comments[0]["root_rpid"] == 1
    assert storage.load_comments("BVHIDDENREPLY") == []


@case("没有目标时翻完可见评论退出，不冒充常规评论采集完成")
def exhausted():
    h = main.calc_crc32(889977665)
    child = {"code": 0, "data": {"replies": [reply(333, 3)], "page": {"count": 1, "size": 20}}}
    client = Client([response([reply(111, 1, rcount=1)], end=True), child])
    mapping, comments, stats = lookup.probe_comment_uids(video("BVEXHAUSTED"), client, {h})
    assert not mapping and not comments and stats["reason"] == "end" and len(client.calls) == 2
    assert storage.get_phase_state("BVEXHAUSTED", "comment", "done") is None


@case("中断保存已找到原文与下一页，恢复时不丢第一人的评论")
def persist_partial():
    a, b = 334455667, 445566778
    ah, bh = main.calc_crc32(a), main.calc_crc32(b)
    class Interrupted(Client):
        def get(self, url, params=None):
            if self.calls:
                raise KeyboardInterrupt()
            return super().get(url, params)
    initial = Interrupted([response([reply(a, 1)], "1")])
    try:
        lookup.probe_comment_uids(video("BVPARTIALTEXT"), initial, {ah, bh})
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("必须模拟中断")
    again = Client([response([reply(b, 2)], "2")])
    mapping, comments, stats = lookup.probe_comment_uids(video("BVPARTIALTEXT"), again, {ah, bh})
    assert mapping == {ah: {a}, bh: {b}} and {c["uid"] for c in comments} == {a, b}
    assert json.loads(again.calls[0][1]["pagination_str"])["offset"] == "1"
    assert stats["requests"] == 1


@case("已知身份但缺正文也搜索，原文保留到报告且供画像引用")
def known_without_text():
    bvid, uid = "BVKNOWNTEXT", 991122334
    row = dm(uid=uid)
    h = row["mid_hash"]
    storage.save_uid_evidence_maps(bvid, [(target.METHOD_COMMENT_VERIFY, {h: {uid}})])
    client = Client([response([reply(uid, 31, content={"message": "目标作者的完整原文"})])])
    with patch.multiple(main, get_auth_client=lambda: client, fetch_command_dms=lambda *a: [],
                        collect_user_data=lambda *a, **kw: {"uid": uid, "name": "目标用户", "level": 6}), patch.object(target, "collect_target_danmaku", return_value=(video(bvid), [row], main.group_by_sender([row]), [])):
        key = target.run_target_analysis(bvid, make_selector("目标"), launch_web=False, skip_deep=True)
    result = storage.load_target_analysis(key)["result"]
    assert len(client.calls) == 1 and result["uid_lookup"]["reason"] == "found_all"
    assert result["comments"][0]["content"] == "目标作者的完整原文"
    assert result["profiles"][0]["comments"][0]["content"] == "目标作者的完整原文"
    assert storage.load_comments(bvid) == []


@case("可选池和历史参数透传；无命中即使请求池也不启动")
def optional_switches():
    forbidden = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("无命中不能启动池"))
    with patch.object(main, "get_auth_client", return_value=object()), patch.object(main, "build_pool", forbidden), patch.object(target, "collect_target_danmaku", return_value=(video("BVOPTION"), [], {}, [])) as source:
        target.run_target_analysis("BVOPTION", make_selector("目标"), target_history=True, target_pool=True, launch_web=False)
    assert source.call_args.kwargs == {"include_history": True}
    with patch.object(target, "run_target_analysis") as run, patch.object(sys, "argv", ["run.py", "BVOPTION", "目标", "--target-history", "--target-pool"]):
        main.main()
    assert run.call_args.kwargs["target_history"] and run.call_args.kwargs["target_pool"]


TMP.cleanup()
print(f"==== 目标轻量取数: {passed} 项通过, {failed} 项失败 ====")
raise SystemExit(int(failed > 0))
