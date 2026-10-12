#!/usr/bin/env python3
"""全库审查缺陷回归：隔离 SQLite、模拟 HTTP/LLM，禁止真实网络和进程信号。"""
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest import SkipTest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
import config
import storage
import requests

TMP = tempfile.TemporaryDirectory(prefix="bili_fixes_")
storage.DB_PATH = str(Path(TMP.name) / "test.db")
config.DB_PATH = storage.DB_PATH
requests.sessions.Session.request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("禁止真实网络"))
with contextlib.redirect_stdout(io.StringIO()):
    storage.init_db()
import web
import main
import comment
import cringe_detector as cd
import llm_client as lc   # 假客户端注入点
import danmaku_history as dh
import danmaku
import api_client
import spam_detector

ok = fail = 0


def case(name):
    def wrap(fn):
        global ok, fail
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                fn()
            ok += 1
            print(f"  ✔ {name}")
        except SkipTest as exc:
            print(f"  跳过 {name}：{exc}")
        except Exception as exc:
            fail += 1
            print(f"  ✘ {name}：{type(exc).__name__}: {exc}")
        return fn
    return wrap


def dm(content, mid_hash="12345678", dmid=1):
    return {"mid_hash": mid_hash, "content": content, "time": 1, "timestamp": 1,
            "color": "#ffffff", "page": 1, "dmid": dmid}


def sender(bvid, uid=42, mid_hash="12345678"):
    storage.save_video_info(bvid, {"bvid": bvid, "title": bvid})
    storage.save_sender(bvid, mid_hash, uid, "高", "评论区验证", 1, [], "低", 0)


def profiles(bvid):
    with web.app.test_request_context("/"):
        return web._load_profiles(bvid)


@case("同一 UID 在两个视频中的弹幕、评论、AI 画像互不覆盖")
def profile_isolation():
    for bvid, text in [("BVISOA", "甲的弹幕"), ("BVISOB", "乙的弹幕")]:
        sender(bvid)
        storage.append_danmaku(bvid, [dm(text)], set())
        group = main.group_by_sender([dm(text)])
        resolved = {"12345678": {"uid": 42, "danmaku_count": 1, "contents": [text]}}
        main.phase_analyze(resolved, {}, {42: {"uid": 42, "name": "测试"}}, group,
                           video_info={"bvid": bvid})
    a = storage.load_video_profile("BVISOA", 42)
    a["ai_deep"] = "甲的 AI"
    storage.update_user_profile(42, a, bvid="BVISOA")
    assert profiles("BVISOA")[0]["danmaku"]["contents"] == ["甲的弹幕"]
    assert profiles("BVISOB")[0]["danmaku"]["contents"] == ["乙的弹幕"]
    assert "ai_deep" not in profiles("BVISOB")[0]
    storage.save_user_data(42, "新昵称", 5, {"uid": 42, "name": "新昵称"}, {})
    assert storage.load_video_profile("BVISOA", 42)["ai_deep"] == "甲的 AI"


@case("旧库归属不明的共享画像按本视频证据重建")
def legacy_profiles():
    for bvid, text in [("BVOLDA", "旧甲"), ("BVOLDB", "旧乙")]:
        sender(bvid, 43)
        storage.append_danmaku(bvid, [dm(text)], set())
    storage.save_user_data(43, "旧用户", 1, {"uid": 43, "name": "旧用户"},
                           {"uid": 43, "danmaku": {"contents": ["错误的共享内容"]}, "ai_deep": "归属未知"})
    assert profiles("BVOLDA")[0]["danmaku"]["contents"] == ["旧甲"]
    assert profiles("BVOLDB")[0]["danmaku"]["contents"] == ["旧乙"]
    assert "ai_deep" not in profiles("BVOLDA")[0]


@case("问题评论全部标误报后从屏蔽名单及画像风险统计扣除")
def comment_fp():
    sender("BVFPFIX", 99, "cmt:99")
    storage.save_comments("BVFPFIX", [{"uid": 99, "rpid": 99, "content": "误判评论"}])
    storage.update_comment_problems("BVFPFIX", {99: {"category": "人身攻击", "severity": 3}})
    storage.save_user_data(99, "测试", 1, {"uid": 99},
        {"uid": 99, "comment_problem": {"hits": 1, "max_severity": 3}}, bvid="BVFPFIX")
    storage.toggle_false_positive("BVFPFIX", "cmt", "99")
    assert web._build_blocklist("BVFPFIX", {"cmt"}, 200)["entries"] == []
    assert profiles("BVFPFIX")[0]["comment_problem"]["hits"] == 0
    storage.toggle_false_positive("BVFPFIX", "cmt", "99")
    assert web._build_blocklist("BVFPFIX", {"cmt"}, 200)["detail"][99]["sev"] == 3


def http_client():
    client = api_client.BiliAPIClient()
    client._ensure_buvid3 = lambda: None
    client._ensure_bili_ticket = lambda: None
    return client


@case("GET/POST 的 HTTP 412 响应触发降速与风控信号")
def http_412():
    response = requests.Response()
    response.status_code = 412
    response._content = b""
    for method in ("get", "post"):
        cli = http_client()
        cli.raise_on_risk = True
        cli._request_locked = lambda *a, **k: response
        with patch.object(api_client.time, "sleep"):
            try:
                getattr(cli, method)("https://offline.invalid")
            except api_client.RiskControlError:
                pass
            else:
                raise AssertionError(f"{method} 未报告风控")
        assert cli._throttle > 1


@case("GET/POST 的 HTTP 503 可以重试后成功")
def http_503():
    bad, good = requests.Response(), requests.Response()
    bad.status_code, bad._content = 503, b""
    good.status_code, good._content = 200, b'{"code":0}'
    for method in ("get", "post"):
        cli = http_client()
        with patch.object(cli, "_request_locked", side_effect=[bad, good]) as request, \
             patch.object(api_client.time, "sleep"):
            assert getattr(cli, method)("https://offline.invalid")["code"] == 0
            assert request.call_count == 2


def reply(rpid, uid=None, text=None):
    return {"rpid": rpid, "member": {"mid": str(uid or rpid)},
            "content": {"message": text or f"评论{rpid}"}, "rcount": 0}


class CommentClient:
    def __init__(self):
        self.pages = []

    def get(self, url, params=None, **kwargs):
        offset = json.loads(params["pagination_str"])["offset"]
        page = int(offset or 1)
        self.pages.append(page)
        return {"code": 0, "data": {"replies": [reply(page)], "cursor": {
            "is_end": page == 3, "pagination_reply": {"next_offset": str(page + 1)}}}}


@case("评论数据和游标的事务失败后回滚，重跑补齐后三页")
def atomic_checkpoint():
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("""CREATE TRIGGER fail_checkpoint BEFORE INSERT ON phase_state
            WHEN NEW.bvid='BVATOMIC' AND NEW.key='page' AND NEW.value='2'
            BEGIN SELECT RAISE(ABORT, '模拟中断'); END""")
        conn.commit()
    try:
        comment.fetch_comments(1, CommentClient(), max_pages=10, bvid="BVATOMIC")
    except sqlite3.IntegrityError:
        pass
    assert [c["rpid"] for c in storage.load_comments("BVATOMIC")] == [1]
    assert storage.get_phase_state("BVATOMIC", "comment", "page") == "1"
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("DROP TRIGGER fail_checkpoint")
        conn.commit()
    client = CommentClient()
    comment.fetch_comments(1, client, max_pages=10, bvid="BVATOMIC")
    assert client.pages == [2, 3]
    assert {c["rpid"] for c in storage.load_comments("BVATOMIC")} == {1, 2, 3}


@case("旧版中断留下的重放评论页继续推进，不能误标完成")
def replay_page():
    storage.save_comments("BVREPLAY", [{"uid": i, "rpid": i, "content": f"评论{i}"} for i in (1, 2)],
                          checkpoint={"mode": "wbi", "page": 1, "offset": "2"})
    client = CommentClient()
    comment.fetch_comments(1, client, max_pages=10, bvid="BVREPLAY")
    assert client.pages == [2, 3]
    assert {c["rpid"] for c in storage.load_comments("BVREPLAY")} == {1, 2, 3}


@case("旧评论接口同样原子保存页数据与全部检查点")
def legacy_checkpoint():
    fake = SimpleNamespace(get=lambda *a, **k: {"code": 0, "data": {"replies": [reply(31)],
                            "cursor": {"is_end": True, "next": 0}}})
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("""CREATE TRIGGER fail_legacy_checkpoint BEFORE INSERT ON phase_state
            WHEN NEW.bvid='BVLEGACYTX' AND NEW.key='page'
            BEGIN SELECT RAISE(ABORT, '模拟旧接口中断'); END""")
        conn.commit()
    try:
        comment._fetch_comments_legacy(1, fake, 2, bvid="BVLEGACYTX")
    except sqlite3.IntegrityError:
        pass
    assert storage.load_comments("BVLEGACYTX") == []
    assert storage.get_phase_state("BVLEGACYTX", "comment", "mode") is None
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("DROP TRIGGER fail_legacy_checkpoint")
        conn.commit()
    comment._fetch_comments_legacy(1, fake, 2, bvid="BVLEGACYTX")
    assert len(storage.load_comments("BVLEGACYTX")) == 1
    assert storage.get_phase_state("BVLEGACYTX", "comment", "done") == "1"


@case("历史月份业务失败不写完成，重跑能补齐失败月份")
def failed_month():
    with patch.object(dh, "HISTORY_DANMAKU_ENABLED", True), \
         patch.object(dh, "_month_range", return_value=["2026-06"]), \
         patch.object(dh, "_fetch_month_dates", return_value=None):
        dh.fetch_history_danmaku(1, None, bvid="BVMONTH")
    assert storage.get_phase_state("BVMONTH", "danmaku", "done") != "1"
    assert storage.get_phase_state("BVMONTH", "danmaku", "failed_months") == "2026-06"
    with patch.object(dh, "HISTORY_DANMAKU_ENABLED", True), \
         patch.object(dh, "_month_range", return_value=["2026-06"]), \
         patch.object(dh, "_fetch_month_dates", return_value=["2026-06-01"]), \
         patch.object(dh, "_fetch_day_danmaku", return_value=[dm("历史")]):
        dh.fetch_history_danmaku(1, None, bvid="BVMONTH")
    assert storage.get_phase_state("BVMONTH", "danmaku", "done") == "1"
    assert len(storage.load_danmaku("BVMONTH")) == 1


@case("已完成视频也会重试失败的旧月份，突破滚动窗口高水位")
def refresh_failed_month():
    storage.set_phase_state("BVOLDMONTH", "danmaku", "done", "1")
    storage.set_phase_state("BVOLDMONTH", "danmaku", "failed_months", "2020-01")
    with patch.object(dh, "HISTORY_DANMAKU_ENABLED", True), \
         patch.object(dh, "_month_range", return_value=["2026-10"]), \
         patch.object(dh, "_fetch_month_dates", side_effect=lambda c, m, cli: ["2020-01-01"] if m == "2020-01" else []), \
         patch.object(dh, "_fetch_day_danmaku", return_value=[dm("旧月份", dmid=2)]) as fetch:
        dh.fetch_history_danmaku(1, None, bvid="BVOLDMONTH")
    assert fetch.call_count == 1


class FakeOpenAI:
    calls = []

    def __init__(self, **kwargs):
        assert kwargs["max_retries"] == 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["messages"][0]["content"])
        result = [{"i": payload["start"] + i, "category": "人身攻击", "severity": 3}
                  for i, item in enumerate(payload["batch"]) if item["content"] == "坏评论"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])


builder = lambda batch, start, vi: json.dumps({"batch": batch, "start": start})


@case("LLM 整批移动后缓存下标仍正确且不重请求已判内容")
def shifted_batch():
    FakeOpenAI.calls = []
    batch = [{"content": "普通评论"}, {"content": "坏评论"}]
    with patch.object(lc, "OpenAI", FakeOpenAI), patch.object(cd, "LLM_CONCURRENCY", 1), \
         patch.object(cd, "LLM_FALLBACK", ("", "", "", "")):
        first = cd._judge_batches(batch, 2, {}, builder, "测试", "review-shift")
        shifted = cd._judge_batches([{"content": "新增甲"}, {"content": "新增乙"}] + batch,
                                   2, {}, builder, "测试", "review-shift")
    assert first[0][0]["i"] == 1 and shifted[0][0]["i"] == 3
    assert shifted[1] == 0 and len(FakeOpenAI.calls) == 2


@case("LLM 全部失败保留旧评论标注，部分成功空结果只清成功范围")
def partial_verdicts():
    storage.save_comments("BVJFAIL", [{"uid": 7, "rpid": i, "content": f"评论{i}"} for i in (7, 8)])
    storage.update_comment_problems("BVJFAIL", {7: "人身攻击", 8: "引战阴阳"})
    with patch.object(cd, "_judge_batches", return_value=(cd.BatchVerdicts([], []), 2, 2)), \
         patch.object(cd, "LLM_API_KEY", "offline"):
        verdicts = cd.detect_problem_comments(storage.load_comments("BVJFAIL"), {"bvid": "BVJFAIL"})
    storage.update_comment_problems("BVJFAIL", verdicts, judged_rpids=verdicts.judged_rpids)
    assert all(c["problem"] for c in storage.load_comments("BVJFAIL"))
    with patch.object(cd, "_judge_batches", return_value=(cd.BatchVerdicts([], [0]), 1, 2)), \
         patch.object(cd, "LLM_API_KEY", "offline"):
        verdicts = cd.detect_problem_comments(storage.load_comments("BVJFAIL"), {"bvid": "BVJFAIL"})
    storage.update_comment_problems("BVJFAIL", verdicts, judged_rpids=verdicts.judged_rpids)
    marks = {c["rpid"]: c["problem"] for c in storage.load_comments("BVJFAIL")}
    assert marks == {7: "", 8: "引战阴阳"}


@case("评论判定截断保留未入选评论的旧标注")
def truncated_verdicts():
    storage.save_comments("BVJTRUNC", [{"uid": 10, "rpid": i, "content": f"评论{i}", "like": i} for i in (1, 2)])
    storage.update_comment_problems("BVJTRUNC", {1: "人身攻击", 2: "引战阴阳"})
    with patch.object(cd, "_judge_batches", return_value=(cd.BatchVerdicts([], [0]), 0, 1)), \
         patch.object(cd, "LLM_API_KEY", "offline"), patch.object(cd, "COMMENT_CRINGE_MAX_ITEMS", 1):
        verdicts = cd.detect_problem_comments(storage.load_comments("BVJTRUNC"), {"bvid": "BVJTRUNC"})
    storage.update_comment_problems("BVJTRUNC", verdicts, judged_rpids=verdicts.judged_rpids)
    assert {c["rpid"]: c["problem"] for c in storage.load_comments("BVJTRUNC")} == {1: "人身攻击", 2: ""}


@case("无 problem 列的旧评论库可以启动迁移并保留原数据")
def old_database():
    path = str(Path(TMP.name) / "old.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE comments(id INTEGER PRIMARY KEY,bvid TEXT,uid INTEGER,rpid INTEGER,"
                     "content TEXT,ctime INTEGER,like INTEGER,is_sub INTEGER,UNIQUE(bvid,rpid,uid))")
        conn.execute("INSERT INTO comments VALUES(1, 'BVOLD', 1, 1, '旧数据', 0, 0, 0)")
    with patch.object(storage, "DB_PATH", path):
        storage.init_db()
        assert storage.load_comments("BVOLD")[0]["content"] == "旧数据"


@case("使用提交的配置模板能够导入 Web 与长图模块")
def template_startup():
    template = Path(TMP.name) / "install" / "src" / "config.py"
    template.parent.mkdir(parents=True)
    template.write_text((ROOT / "config.example.py").read_text(encoding="utf-8"), encoding="utf-8")
    code = f"""import sys, importlib.util
spec=importlib.util.spec_from_file_location('config', {str(template)!r})
cfg=importlib.util.module_from_spec(spec)
spec.loader.exec_module(cfg)
sys.modules['config']=cfg
sys.path.insert(0, 'src')
import web
"""
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr


@case("新评论明文证据升级碰撞 UID 的旧解析，不受兴趣阈值阻挡")
def uid_upgrade():
    storage.save_sender("BVUIDFIX", "9ae0daaf", 12345678, "低", "CRC32破解", 1, ["测试"], "低", 0)
    resolved = main.phase_resolve("BVUIDFIX", main.group_by_sender([dm("测试", "9ae0daaf")]),
                                  {"9ae0daaf": 7533489035}, None)
    assert resolved["9ae0daaf"]["uid"] == 7533489035
    assert resolved["9ae0daaf"]["confidence"] == "高"
    assert resolved["9ae0daaf"]["collision_risk"]


@case("旧主楼新回复保存预览并补采楼中楼，含直接父级")
def refresh_children():
    storage.save_comments("BVCHILD", [{"uid": 1, "rpid": 10, "content": "老主楼", "reply_count": 0}])
    root = reply(10, 1, "老主楼")
    root.update(rcount=2, replies=[dict(reply(11, 2, "预览回复"), parent=10)])
    fake = SimpleNamespace(get=lambda *a, **k: {"code": 0, "data": {
        "replies": [root], "cursor": {"is_end": True}}})
    with patch.object(comment, "_fetch_sub_replies", side_effect=lambda oid, rpid, rc, pv, cli: pv + [{
        "uid": 3, "rpid": 12, "content": "补采回复", "is_sub": True, "parent_rpid": 11}]) as fetch:
        comment.refresh_comments(1, fake, "BVCHILD")
    assert fetch.call_count == 1
    saved = {c["rpid"]: c for c in storage.load_comments("BVCHILD")}
    assert set(saved) == {10, 11, 12} and saved[12]["parent_rpid"] == 11


@case("CLI 深掘和同条数 UID 更新都会使报告页面缓存失效")
def fingerprint_updates():
    before = web._page_fingerprint("BVISOA")
    p = storage.load_video_profile("BVISOA", 42)
    p["ai_deep"] = "新的深掘结果"
    storage.update_user_profile(42, p, bvid="BVISOA")
    assert before != web._page_fingerprint("BVISOA")
    before = web._page_fingerprint("BVISOA")
    storage.save_sender("BVISOA", "12345678", 43, "高", "评论区验证", 1, [], "低", 0)
    assert before != web._page_fingerprint("BVISOA")


@case("服务身份拒绝编辑器、其他目录和复用 PID；从不发送错误信号")
def pid_identity():
    cwd = str(ROOT)
    for argv, started in [(["vim", "web.py"], "1"), (["python", "/tmp/web.py"], "1"),
                          (["python", "web.py"], "2"), (["python", "-c", "web.py"], "1")]:
        with patch.object(web, "_process_identity", return_value=(argv, started, cwd)):
            assert not web._pid_is_webpy(1234, "1")
    with patch.object(web, "_process_identity", return_value=(["python3.12", "-u", "web.py"], "1", cwd)):
        assert web._pid_is_webpy(1234, "1")
    with patch.object(web, "DATA_DIR", TMP.name):
        Path(web._pid_file(9000)).write_text(json.dumps({"pid": 1234, "port": 9000,
            "script": os.path.realpath(web.__file__), "started": "1"}), encoding="utf-8")
        with patch.object(web, "_process_identity", return_value=(["python", "web.py"], "2", cwd)), \
             patch.object(web.os, "kill") as kill:
            web._stop_server(9000)
            kill.assert_not_called()


@case("UP 词云临时失败不缓存，下一次能成功；旧空缓存可重新采集")
def wordcloud_retry():
    fake = SimpleNamespace(get=lambda *a, **k: {"code": -1})
    with patch.object(web, "_get_client", return_value=fake):
        assert web.app.test_client().get("/api/up/7654321/wordcloud").status_code == 502
    assert storage.load_llm_cache("up:7654321") is None
    storage.save_llm_cache("up:7654321", '{"words": [], "name": ""}')
    fake.get = lambda *a, **k: {"code": 0, "data": {"list": {"vlist": [{"author": "测试", "title": "游戏攻略"}]}}}
    with patch.object(web, "_get_client", return_value=fake):
        data = web.app.test_client().get("/api/up/7654321/wordcloud").get_json()
    assert data["words"]


@case("无法读取进程工作目录时，已核验启动标识和记录目录仍能识别服务")
def recorded_process_cwd():
    with patch.object(web, "_process_identity", return_value=(["python", "web.py"], "1", "")):
        assert web._pid_is_webpy(1234, "1", str(ROOT))
        assert not web._pid_is_webpy(1234, "2", str(ROOT))


@case("六人六条窗口不能覆盖五人八条的达标复读事件")
def repeat_window():
    rows = [dict(dm("复读", f"a{i}"), time=i, timestamp=0) for i in range(6)]
    rows += [dict(dm("复读", f"b{i % 5}"), time=200 + i, timestamp=0) for i in range(8)]
    event = spam_detector.detect_repeat_events(rows)[0]
    assert event["sender_count"] == 5 and event["total"] == 8


@case("成功空弹幕视频仍运行问题评论作者画像流程")
def empty_danmaku_comments():
    comment_data = [{"uid": 777, "rpid": 777, "content": "问题评论"}]
    verdicts = cd.CommentVerdicts({777: {"category": "人身攻击", "severity": 3}}, [777])
    with patch.object(main, "phase_login", return_value=None), patch.object(main, "build_pool", return_value=None), \
         patch.object(main, "phase_danmaku", return_value=({"bvid": "BVEMPTYFIX"}, [], {}, [])), \
         patch.object(main, "phase_comment", return_value=(comment_data, {}, {}, {})) as collect, \
         patch.object(main, "phase_cringe", return_value={}), \
         patch.object(main, "phase_comment_cringe", return_value=verdicts), \
         patch.object(main, "phase_collect_users", return_value={777: {"uid": 777, "name": "评论作者"}}), \
         patch.object(main, "phase_ai_analysis"), patch.object(main, "REPORT_DIR", str(Path(TMP.name) / "reports")):
        main.run_analysis("BVEMPTYFIX", launch_web=False)
    assert collect.call_count == 1
    assert storage.load_video_profile("BVEMPTYFIX", 777)["comment_problem"]["hits"] == 1


@case("弹幕接口全部失败不当作成功空集，成功空 XML 可返回空")
def failed_danmaku():
    with patch.object(danmaku, "fetch_danmaku", side_effect=RuntimeError("模拟失败")):
        try:
            danmaku.fetch_all_danmaku({"cid": 1}, None)
        except RuntimeError:
            pass
        else:
            raise AssertionError("全部失败被当成空集")
    with patch.object(danmaku, "fetch_danmaku", return_value=[]):
        assert danmaku.fetch_all_danmaku({"cid": 1}, None) == []


@case("多分P实时池失败可重跑补齐，即使已有历史完成标记")
def retry_danmaku_page():
    info = {"bvid": "BVPGFAIL", "pages": [{"cid": 1}, {"cid": 2}], "realtime_failed_pages": [2]}
    storage.save_video_info("BVPGFAIL", info)
    storage.append_danmaku("BVPGFAIL", [dm("第一P")], set())
    storage.set_phase_state("BVPGFAIL", "danmaku", "done", "1")
    with patch.object(main, "get_video_info", return_value=dict(info)), \
         patch.object(main, "fetch_command_dms", return_value=[]), \
         patch.object(main, "HISTORY_DANMAKU_ENABLED", False), \
         patch.object(main, "fetch_danmaku", return_value=[dm("第二P", dmid=2)]) as fetch:
        main.phase_danmaku("BVPGFAIL", None)
    assert fetch.call_count == 1 and len(storage.load_danmaku("BVPGFAIL")) == 2
    assert storage.load_video_info("BVPGFAIL")["realtime_failed_pages"] == []


@case("实时池全部失败时主流程仍可降级分析评论，采集状态保持未完成")
def failed_danmaku_degrades():
    info = {"bvid": "BVALLFAIL", "cid": 1, "realtime_failed_pages": [1]}
    with patch.object(main, "collect_danmaku_data", side_effect=danmaku.DanmakuCollectionError(info)), \
         patch.object(main, "HISTORY_DANMAKU_ENABLED", False), \
         patch.object(main, "fetch_command_dms", return_value=[]):
        result = main.phase_danmaku("BVALLFAIL", None, resume=False)
    assert result[1] == [] and result[0]["bvid"] == "BVALLFAIL"
    assert storage.get_phase_state("BVALLFAIL", "danmaku", "done") != "1"


@case("实时池落库后历史采集中断，重跑仍补齐此前失败的分P")
def interrupted_danmaku():
    info = {"bvid": "BVREALTX", "pages": [{"cid": 1}, {"cid": 2}], "realtime_failed_pages": [2]}
    row = dm("已采第一页")
    with patch.object(main, "collect_danmaku_data", return_value=(info, [row], main.group_by_sender([row]))), \
         patch.object(main, "HISTORY_DANMAKU_ENABLED", True), \
         patch.object(main, "_fetch_history_all_pages", side_effect=KeyboardInterrupt):
        try:
            main.phase_danmaku("BVREALTX", None, resume=False)
        except KeyboardInterrupt:
            pass
    assert storage.load_video_info("BVREALTX")["realtime_failed_pages"] == [2]
    with patch.object(main, "get_video_info", return_value={"bvid": "BVREALTX", "pages": info["pages"]}), \
         patch.object(main, "fetch_command_dms", return_value=[]), \
         patch.object(main, "HISTORY_DANMAKU_ENABLED", False), \
         patch.object(main, "fetch_danmaku", return_value=[dm("补齐第二页", dmid=2)]):
        main.phase_danmaku("BVREALTX", None)
    assert len(storage.load_danmaku("BVREALTX")) == 2


@case("弹幕事务回滚时不污染去重集合，随后重试仍能入库")
def danmaku_rollback():
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("""CREATE TRIGGER fail_danmaku BEFORE INSERT ON danmaku
            WHEN NEW.bvid='BVDMROLLBACK' BEGIN SELECT RAISE(ABORT, '模拟弹幕落库失败'); END""")
        conn.commit()
    seen = set()
    try:
        storage.append_danmaku("BVDMROLLBACK", [dm("重试弹幕", dmid=321)], seen)
    except sqlite3.IntegrityError:
        pass
    assert seen == set()
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("DROP TRIGGER fail_danmaku")
        conn.commit()
    assert storage.append_danmaku("BVDMROLLBACK", [dm("重试弹幕", dmid=321)], seen) == 1
    assert seen == {321}


@case("快速分析导出本轮画像 HTML/JSON/CSV，保持全量检查点隔离")
def quick_report():
    import quick_test as qt
    with patch.object(qt, "get_auth_client", return_value=None), patch.object(qt, "build_pool", return_value=None), \
         patch.object(qt, "collect_danmaku_data", return_value=({"bvid": "BVQUICKFIX", "title": "冒烟"}, [dm("测试")], {})), \
         patch.object(qt, "fetch_comments", return_value=[]), patch.object(qt, "LLM_API_KEY", ""), \
         patch.object(qt, "resolve_sender", return_value=(888, "高", "评论区验证", {}, False, [])), \
         patch.object(qt, "collect_user_data", return_value={"uid": 888, "name": "冒烟用户"}), \
         patch.object(qt, "_Tee", side_effect=lambda path, stream: stream), \
         patch.object(qt, "DATA_DIR", TMP.name), patch.object(qt, "WEB_AUTOSTART", True), \
         patch.object(qt.webbrowser, "open") as launch, \
         patch.object(sys, "argv", ["quick_test.py", "BVQUICKFIX", "--top", "1"]):
        qt.main()
    directory = Path(TMP.name) / "quick_reports"
    assert {p.suffix for p in directory.iterdir()} == {".html", ".json", ".csv"}
    html = next(directory.glob("*.html")).read_text(encoding="utf-8")
    assert "冒烟用户" in html and "快速采样报告" in html
    assert launch.call_args.args[0].startswith("file:")
    assert storage.get_phase_state("BVQUICKFIX", "danmaku", "done") is None


@case("浏览器 UID 为空、零、负数或非整数时不生成无效链接")
def null_uid_js():
    if not shutil.which("node"):
        raise SkipTest("未安装 Node，仅跳过 JavaScript 执行验证")
    script = (ROOT / "static/report.js").read_text(encoding="utf-8")
    expr = re.search(r"const sender = (row\.uid[^\n]+)", script)[1].strip()
    code = f"""for(const uid of [null, undefined, '', 0, -1, 1.2, '***']) {{
        const row={{uid}}, uidNum=Number(row.uid); if({expr}) throw Error(String(uid));
        }} const row={{uid:42}}, uidNum=Number(row.uid); if(!({expr})) throw Error('合法 UID');"""
    proc = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr


@case("LLM 预算到期后不再发请求，单次超时受剩余预算约束")
def llm_budget():
    clock = {"now": 0, "calls": []}
    class SlowOpenAI:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def create(self, **kwargs):
            clock["calls"].append((clock["now"], kwargs["timeout"]))
            raise RuntimeError("模拟服务故障")
    def sleep(seconds):
        clock["now"] += seconds
    with patch.object(lc, "OpenAI", SlowOpenAI), patch.object(cd, "LLM_CONCURRENCY", 1), \
         patch.object(cd, "LLM_RETRY_BUDGET_SECONDS", 1), \
         patch.object(cd.time, "monotonic", lambda: clock["now"]), patch.object(cd.time, "sleep", sleep), \
         patch.object(cd, "LLM_FALLBACK", ("", "", "", "")):
        result = cd._judge_batches([{"content": "测试"}], 1, {}, builder, "预算测试")
    assert clock["calls"] == [(0, 1)] and result[1] == 1


print(f"\n==== 全库审查修复: {ok} 项通过, {fail} 项失败 ====")
TMP.cleanup()
sys.exit(1 if fail else 0)
