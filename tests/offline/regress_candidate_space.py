#!/usr/bin/env python3
"""候选空间辅助核验离线回归；仅用虚构公开样本与隔离数据库。"""
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
import config
import storage
import candidate_space as space
from api_client import RiskControlError
from uid_evidence import crc32_uid

CASES = []
UID, UP, BV = 12345678, 45678901, "BVSPACE"
HASH = crc32_uid(UID)


def case(name):
    def register(fn):
        CASES.append((name, fn))
        return fn
    return register


def dynamic(text="公开发言", bvid="", orig=None):
    item = {"id_str": "987654321", "modules": {"module_dynamic": {
        "desc": {"text": text}, "major": {"archive": {"bvid": bvid, "title": "离线投稿标题"}}}}}
    if orig:
        item["orig"] = orig
    return item


class Client:
    def __init__(self, **overrides):
        self.calls = []
        self.responses = {
            config.USER_CARD_URL: {"code": 0, "data": {"card": {"mid": UID, "name": "离线昵称甲", "sign": "公开签名"}}},
            config.USER_VIDEOS_URL: {"code": 0, "data": {"archives": []}},
            config.USER_DYNAMICS_URL: {"code": 0, "data": {"items": []}},
            config.USER_FOLLOWINGS_URL: {"code": 0, "data": {"list": []}}}
        self.responses.update({getattr(config, name): value for name, value in overrides.items()})

    def get(self, url, params):
        self.calls.append((url, params))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


def sample(client=None):
    return space.fetch_space_sample(UID, client or Client())


def assess(data, contents=None, bvid=BV):
    return space.assess_space(data, bvid, {"owner": {"mid": UP}}, contents or ["这是一条足够长的目标发言"])


def record():
    return {"id": "space-offline", "bvid": BV, "selector": {"mode": "literal", "pattern": "目标"},
            "updated_at": "2026-10-11 23:00:00", "result": {
                "video_info": {"title": "离线视频", "owner": {"mid": UP}}, "stats": {}, "profiles": [],
                "senders": [{"mid_hash": HASH, "uid": UID, "candidates": [UID], "selected": True,
                             "method": "CRC32破解", "confidence": "低", "resolution": {"reason": "无明文验证", "max_search": UID},
                             "status": "低置信度", "matched_count": 1, "danmaku_count": 1, "samples": ["目标"]}],
                "matches": [{"mid_hash": HASH, "content": "这是一条足够长的目标发言", "time": 1}]}}


def save_record():
    item = record()
    storage.save_uid_candidates(HASH, UID, "mitm-v1", [UID])
    storage.save_target_analysis(item["id"], BV, item["selector"], item["result"])
    return item


def post(client, uid=UID, mid_hash=HASH, **kwargs):
    return client.post("/api/target/space-offline/candidate-space", json={"uid": uid, "mid_hash": mid_hash}, **kwargs)


def clear_cache():
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("DELETE FROM uid_space_cache")
        conn.commit()


@case("公开空间最多四个限速接口调用，不翻页或启动代理池")
def bounded():
    client = Client(USER_VIDEOS_URL={"code": 0, "data": {"archives": [{"title": str(i)} for i in range(50)]}})
    data = sample(client)
    assert len(client.calls) == 4 and data["complete"]
    assert len(data["sources"]["videos"]["items"]) <= config.UID_SPACE_SAMPLE_ITEMS
    assert all(params.get("pn", 1) == 1 for _, params in client.calls)


@case("空公开空间是已检查，不能作为排除候选的证据")
def empty():
    result = assess(sample())
    assert "未发现关联" in result["verdict"] and "不能排除" in result["verdict"]
    assert all(c["status"] == "ok" for c in result["coverage"])


@case("公开转发目标视频支持同视频关联，仍不确认身份")
def same_video():
    orig = dynamic("原作者发言", BV)
    data = sample(Client(USER_DYNAMICS_URL={"code": 0, "data": {"items": [dynamic("转发这个视频", orig=orig)]}}))
    result = assess(data)
    assert result["evidence"][0]["kind"] == "video" and "身份仍未确认" in result["verdict"]
    assert not assess(data, bvid="BVOTHER")["evidence"]


@case("关注目标UP只是弱线索，不涉及明文身份确认")
def following():
    data = sample(Client(USER_FOLLOWINGS_URL={"code": 0, "data": {"list": [{"mid": UP, "uname": "离线UP"}]}}))
    result = assess(data)
    assert "弱关联" in result["verdict"] and result["evidence"][0]["kind"] == "following"


@case("候选自己的长发言重复作为文字线索，原文和链接保留")
def phrase():
    data = sample(Client(USER_DYNAMICS_URL={"code": 0, "data": {"items": [dynamic("引用：这是一条足够长的目标发言") ]}}))
    result = assess(data)
    assert result["evidence"][0]["kind"] == "text"
    assert "可能为引用" in result["evidence"][0]["label"]
    assert result["examples"][1]["url"].startswith("https://t.bilibili.com/")


@case("通用短句及重复字符不误判措辞支持")
def generic():
    data = sample(Client(USER_DYNAMICS_URL={"code": 0, "data": {"items": [dynamic("好评如潮 哈哈哈哈哈哈哈哈哈哈哈哈") ]}}))
    assert not assess(data, ["好评如潮", "哈哈哈哈哈哈哈哈哈哈"])["evidence"]


@case("转发的他人原文不能当作候选自己的相同发言")
def original_text():
    data = sample(Client(USER_DYNAMICS_URL={"code": 0, "data": {"items": [dynamic("转发", orig=dynamic("这是一条足够长的目标发言"))]}}))
    assert not assess(data)["evidence"]


@case("投稿标题不能当作候选自己的弹幕措辞")
def title_text():
    data = sample(Client(USER_VIDEOS_URL={"code": 0, "data": {"archives": [{"title": "这是一条足够长的目标发言", "bvid": "BVOTHER"}]}}))
    assert not assess(data)["evidence"]


@case("不可见或缺失列表与真正空列表区分，不误排除")
def unavailable():
    for response in ({"code": -40301}, {"code": 0, "data": {}}):
        data = sample(Client(USER_FOLLOWINGS_URL=response))
        assert not data["complete"] and "资料不足" in assess(data)["verdict"]
        assert data["sources"]["followings"]["status"] == "unavailable"


@case("名片必须UID相符，错误账号中止后续请求")
def wrong_account():
    client = Client(USER_CARD_URL={"code": 0, "data": {"card": {"mid": UP, "name": "错误账号"}}})
    result = sample(client)
    assert len(client.calls) == 1 and "错误账号" not in str(result)
    assert "资料不足" in assess(result)["verdict"]


@case("名片失败不继续采集未经核对的账号")
def failed_card():
    client = Client(USER_CARD_URL={"code": -1})
    assert not sample(client)["complete"] and len(client.calls) == 1


@case("风控响应与异常均停止后续接口请求")
def risk():
    for response in ({"code": -412}, RiskControlError("离线风控")):
        client = Client(USER_VIDEOS_URL=response)
        data = sample(client)
        assert len(client.calls) == 2 and data["sources"]["videos"]["status"] == "risk"
        assert data["sources"]["dynamics"]["status"] == "skipped"


@case("单个非名片接口异常保留其余来源，不打印敏感异常正文")
def partial():
    data = sample(Client(USER_VIDEOS_URL=RuntimeError("敏感请求参数"), USER_FOLLOWINGS_URL={"code": 0, "data": {"list": [{"mid": UP}]}}))
    assert "敏感请求参数" not in str(data) and not data["complete"]
    assert "弱关联" in assess(data)["verdict"]


@case("明确不存在单独提示重核解析，不修改解析记录")
def missing():
    client = Client(USER_CARD_URL={"code": -404})
    result = sample(client)
    assert len(client.calls) == 1 and "账号不存在" in assess(result)["verdict"]


@case("完整与部分缓存不同有效期，过期、未来和损坏记录无效")
def expiry():
    data = sample()
    now = data["checked_at"]
    assert space.sample_is_fresh(data, now + 100)
    assert not space.sample_is_fresh(data, now + config.UID_SPACE_CACHE_TTL + 1)
    data["complete"] = False
    assert space.sample_is_fresh(data, now + 1)
    assert not space.sample_is_fresh(data, now + config.UID_SPACE_RETRY_AFTER + 1)
    assert not space.sample_is_fresh(data, now - 1)
    assert not space.sample_is_fresh({"version": 1})


@case("所有CRC碰撞候选均保留，不能按空间兴趣自动选择其中一个")
def collisions():
    sender = record()["result"]["senders"][0]
    other = 7533489035
    assert crc32_uid(other) == HASH
    sender["uid"] = None
    sender["candidates"] = [UID, other, UP]
    assert space.candidate_uids(sender) == [UID, other]
    sender["selected"] = False
    assert not space.candidate_uids(sender)


@case("损坏缓存及UID不符缓存忽略，不覆盖原有用户资料")
def bad_cache():
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("INSERT OR REPLACE INTO uid_space_cache VALUES (?, ?)", (UID, "坏json"))
        conn.commit()
    assert not storage.load_uid_space_cache([UID])
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("INSERT OR REPLACE INTO uid_space_cache VALUES (?, ?)", (UID, json.dumps({"uid": UP})))
        conn.commit()
    assert not storage.load_uid_space_cache([UID])
    storage.save_user_data(UID, "旧昵称", 6, {"uid": UID, "name": "旧昵称", "dynamics": ["完整旧采集"]}, {})
    old = storage.load_user_data(UID)
    sample()
    assert storage.load_user_data(UID) == old


@case("读取报告和已缓存核验都不联网、不读Cookie、既有解析保持不变")
def cached_api():
    item = save_record()
    sample()
    client = web.app.test_client()
    with patch.object(web, "_get_client", side_effect=AssertionError("缓存不登录")):
        body = client.get("/target/space-offline").text
        assert "命令行" in body and "CRC32破解 · 低" in body
        assert post(client).status_code == 410
    updated = storage.load_target_analysis(item["id"])["result"]
    assert updated["senders"][0]["uid"] == UID and updated["senders"][0]["confidence"] == "低"
    assert updated["profiles"] == item["result"]["profiles"] and updated["matches"] == item["result"]["matches"]


@case("拒绝非法UID、非报告候选和CRC不符，不能任意探测账号")
def bad_api():
    save_record()
    client = web.app.test_client()
    with patch.object(web, "_get_client", side_effect=AssertionError("非法候选不登录")):
        for uid in (True, 1.2, -1, "1 OR 1=1", "0", UP, 2**63):
            assert post(client, uid=uid).status_code == 410
        assert post(client, mid_hash="ffffffff").status_code == 410
        assert client.post("/api/target/unknown/candidate-space", json={"uid": UID}).status_code == 404
        assert client.post("/api/target/space-offline/candidate-space", json=[]).status_code == 410


@case("缓存与接口公开原文防XSS，昵称及UID遮蔽")
def masking():
    save_record()
    sample(Client(USER_CARD_URL={"code": 0, "data": {"card": {"mid": UID, "name": "离线昵称甲", "sign": "<script>危险</script>"}}}))
    client = web.app.test_client()
    client.set_cookie("mask", "1")
    html = client.get("/target/space-offline").text
    assert "离线昵称甲" not in html and "UID:123*****" in html
    assert "&lt;script&gt;危险&lt;/script&gt;" in html and "<script>危险</script>" not in html


@case("旧页面采集入口返回410，不启动客户端或改写画像映射")
def uncached_api():
    item = save_record()
    clear_cache()
    client = web.app.test_client()
    with contextlib.closing(storage.get_db()) as conn:
        before = {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")] for table in ("senders", "video_profiles", "global_uid_map", "uid_evidence", "users")}
    fake = Client()
    with patch.object(web, "_get_client", return_value=fake), patch.object(web, "_get_pool", side_effect=AssertionError("不启动代理池")):
        assert post(client).status_code == 410
    assert len(fake.calls) == 0
    with contextlib.closing(storage.get_db()) as conn:
        after = {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")] for table in before}
    updated = storage.load_target_analysis(item["id"])["result"]
    assert before == after and updated["senders"][0]["confidence"] == "低"
    assert updated["profiles"] == item["result"]["profiles"] and updated["matches"] == item["result"]["matches"]


@case("旧页面请求不读取Cookie、不注册采集任务")
def failed_login():
    save_record()
    clear_cache()
    client = web.app.test_client()
    with patch.object(web, "_get_client", side_effect=web.CookieInvalidError("请重新登录")):
        assert post(client).status_code == 410
    assert not web._has_running_job(BV) and not web._CANDIDATE_SPACE_LOCK.locked()
    with patch.object(web, "_get_client", return_value=Client()):
        assert post(client).status_code == 410


@case("旧页面请求即使已有任务也不新增候选采集")
def busy():
    save_record()
    clear_cache()
    client = web.app.test_client()
    with patch.object(web, "_get_client", side_effect=AssertionError("忙碌时不登录")):
        web._CANDIDATE_SPACE_LOCK.acquire()
        try:
            assert post(client).status_code == 410
        finally:
            web._CANDIDATE_SPACE_LOCK.release()
        job_id, _ = web._try_register_job("offline", BV)
        try:
            assert post(client).status_code == 410
        finally:
            web.JOBS[job_id]["finished"] = True
    assert not web._CANDIDATE_SPACE_LOCK.locked()


@case("核验沿用本机Origin防护，外站不能发起空间采集")
def origin():
    save_record()
    client = web.app.test_client()
    assert post(client, headers={"Origin": "https://outside.example"}).status_code == 403


@case("核验使用全部命中原文，不限报告前三条样本")
def all_contents():
    item = record()
    assert space.sender_contents(item, item["result"]["senders"][0]) == ["这是一条足够长的目标发言"]


@case("损坏样本结构不能命中有效缓存")
def bad_sample():
    data = sample()
    data["sources"]["videos"]["items"] = ["错误格式"]
    assert not space.sample_is_fresh(data)


@case("删除目标视频同时清除低置信度候选的空间样本")
def delete_cache():
    save_record()
    sample()
    storage.delete_video_data(BV)
    assert not storage.load_uid_space_cache([UID])


@case("首页只放独立目标列表入口，不把局部弹幕缓存当作全视频报告")
def target_only_index():
    save_record()
    storage.save_video_info(BV, {"title": "只有目标采样", "bvid": BV})
    client = web.app.test_client()
    body = client.get("/").text
    assert 'href="/targets"' in body and f'href="/video/{BV}"' not in body
    assert 'href="/target/space-offline"' not in body
    assert 'href="/target/space-offline"' in client.get("/targets").text
    assert not storage.has_normal_report(BV)


@case("旧目标快照无须重新采集，也能从普通视频列表隐藏")
def legacy_target_index():
    item = record()
    item["result"]["coverage"] = {"source_mode": "realtime_cache"}
    storage.save_target_analysis(item["id"], BV, item["selector"], item["result"])
    assert not storage.has_normal_report(BV)


@case("目标模式补采历史的done标记也不能代替常规报告")
def history_only():
    storage.set_phase_state(BV, "danmaku", "done", "1")
    assert not storage.has_normal_report(BV)
    assert f'href="/video/{BV}"' not in web.app.test_client().get("/").text


@case("过期的普通视频链接跳转到独立目标列表页")
def stale_video_link():
    response = web.app.test_client().get(f"/video/{BV}")
    assert response.status_code == 302 and response.headers["Location"] == "/targets"


@case("目标报告不再提供尚未生成的普通视频报告链接")
def target_toolbar():
    client = web.app.test_client()
    body = client.get("/target/space-offline").text
    assert f'href="/video/{BV}"' not in body and 'href="/targets">返回目标列表' in body


@case("已有常规发送者的视频保留普通入口，不受目标分析影响")
def existing_normal_senders():
    storage.save_sender(BV, HASH, UID, "低", "CRC32破解", 1, ["正常弹幕"], "低", 0)
    assert storage.has_normal_report(BV)
    body = web.app.test_client().get("/").text
    assert f'href="/video/{BV}"' in body and 'href="/targets"' in body
    assert 'href="/target/space-offline"' in web.app.test_client().get("/targets").text
    assert f'href="/video/{BV}"' in web.app.test_client().get("/target/space-offline").text
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("DELETE FROM senders WHERE bvid=?", (BV,))
        conn.commit()


@case("已有常规评论的视频即使没有画像仍保留报告入口")
def existing_normal_comments():
    storage.save_comments(BV, [{"uid": UID, "rpid": "space-normal-comment", "content": "正常评论"}])
    assert storage.has_normal_report(BV)
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("DELETE FROM comments WHERE bvid=?", (BV,))
        conn.commit()


@case("常规分析完成标记保留零画像报告")
def completed_normal():
    storage.set_phase_state(BV, "report", "ready", "1")
    assert storage.has_normal_report(BV)
    assert f'href="/video/{BV}"' in web.app.test_client().get("/").text


@case("已有视频画像即使没有发送者记录也保留普通报告入口")
def existing_normal_profiles():
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("DELETE FROM phase_state WHERE bvid=? AND phase='report'", (BV,))
        conn.commit()
    storage.save_user_data(UID, "常规画像", 6, {"uid": UID}, {"uid": UID}, bvid=BV)
    assert storage.has_normal_report(BV)


@case("公开内容样本均衡展示投稿和动态，不被投稿列表挤掉")
def balanced_examples():
    data = sample(Client(USER_VIDEOS_URL={"code": 0, "data": {"archives": [{"title": "投稿" + str(i)} for i in range(20)]}},
                        USER_DYNAMICS_URL={"code": 0, "data": {"items": [dynamic("自己的公开动态") ]}}))
    assert any(item["source"] == "动态首页" for item in assess(data)["examples"])


@case("既有常规元信息无目标快照时保持兼容")
def legacy_normal():
    storage.save_video_info("BVLEGACYNORMAL", {"title": "旧常规报告"})
    assert storage.has_normal_report("BVLEGACYNORMAL")


def main():
    global web
    import requests
    requests.sessions.Session.request = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("禁止真实网络"))
    with tempfile.TemporaryDirectory(prefix="bili_candidate_") as tmp:
        storage.DB_PATH = config.DB_PATH = str(Path(tmp) / "isolated.db")
        with contextlib.redirect_stdout(io.StringIO()):
            storage.init_db()
        import web
        passed = failed = 0
        for name, fn in CASES:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    fn()
                passed += 1
                print(f"  ✔ {name}")
            except Exception as exc:
                failed += 1
                print(f"  ✘ {name}：{type(exc).__name__}: {exc}")
        print(f"==== 候选空间辅助核验: {passed} 项通过, {failed} 项失败 ====")
        return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
