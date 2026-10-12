#!/usr/bin/env python3
"""纯破解候选页与匿名问题证据回归：隔离临时库，不联网、不读Cookie、不调用LLM。"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
import requests
import config
import storage
import cringe_detector as cd
import uid_resolver as resolver
import crc_candidates as crc
import candidate_cards as cards
from api_client import RiskControlError
from uid_evidence import crc32_uid
import web

A, B = 12345678, 7533489035
H, BV = crc32_uid(A), "BVCRCTEST"
CASES = []


def case(name):
    def register(fn):
        CASES.append((name, fn))
        return fn
    return register


def sender(method="候选歧义", **extra):
    return {"mid_hash": H, "uid": None, "confidence": "无", "method": method,
            "danmaku_count": 2, "contents": ["问题原文", "普通发言"], "spam_level": "低",
            "resolution": {"candidates": [A, B], "evidence": [], "checked_at": time.time()}, **extra}


def rows():
    return [{"mid_hash": H, "content": content, "timestamp": 1700000000, "time": i,
             "color": "#fff", "dmid": str(i), "page": 1, "mode": 1, "pool": 0}
            for i, content in enumerate(["问题原文", "普通发言"])]


def problem(content="问题原文", category="人身攻击"):
    return {"content": content, "category": category, "severity": 2, "reason": "离线判定"}


def groups(**kw):
    return crc.build_crc_groups([kw.pop("row", sender())], rows(), kw.pop("problems", {"问题原文": problem()}),
                                kw.pop("fp", set()), **kw)


def setup_video(row=None, with_problem=True):
    row = row or sender()
    storage.save_video_info(BV, {"title": "离线候选报告", "bvid": BV})
    storage.append_danmaku(BV, rows(), set())
    storage.save_uid_candidates(H, config.MITM_MAX_UID, "mitm-v1", [A, B])
    storage.save_sender(BV, H, row["uid"], row["confidence"], row["method"], row["danmaku_count"],
                        row["contents"], row["spam_level"], 0, resolution=row["resolution"])
    if with_problem:
        storage.save_danmaku_problems(BV, {"问题原文": problem()}, {"问题原文", "普通发言"})


def target_record():
    storage.save_uid_candidates(H, config.MITM_MAX_UID, "mitm-v1", [A, B])
    row = sender(selected=True, matched_count=1, samples=["问题原文"], status="待核验")
    return {"id": "crc-offline", "bvid": BV, "selector": {"pattern": "问题原文", "mode": "literal"},
            "result": {"video_info": {"title": "离线目标", "bvid": BV}, "stats": {}, "profiles": [],
                       "senders": [row], "matches": rows()[:1]}, "updated_at": "2026-10-12 08:00:00"}


@case("多名存在的数学候选全部列出，匿名发送者仍不分配UID")
def all_candidates():
    out = groups(verifications={A: ("exists", {"name": "甲"}), B: ("exists", {"name": "乙"})})
    assert [c["uid"] for c in out[0]["candidates"]] == [A, B]
    assert all(c["status"] == "exists" for c in out[0]["candidates"]) and sender()["uid"] is None


@case("存在性未知不能当成不存在，仍列入可核验候选")
def unknown():
    out = groups(verifications={A: ("exists", {})})
    assert out[0]["candidates"][1]["status"] == "unknown"


@case("明确不存在单独折叠，完整数学候选集仍保留")
def missing():
    out = groups(verifications={A: ("exists", {}), B: ("not_exists", {})})
    html = crc.render_crc_groups(out, "/endpoint", False, str, str)
    assert "已排除账号" in html and str(A) in html and str(B) in html
    assert html.count('class="space-candidate"') == 1


@case("有明文来源的低置信度与明文歧义不混入纯破解页")
def plaintext():
    assert not groups(row=sender(method="评论区验证"))
    row = sender()
    row["resolution"]["evidence"] = [{"uid": A, "source": "评论区验证", "local": False}]
    assert not groups(row=row)


@case("CRC32正向不符的脏候选剔除，不依赖UID排序选择人")
def bad_uid():
    row = sender()
    row["resolution"]["candidates"] = [B, 456789, A]
    assert [c["uid"] for c in groups(row=row)[0]["candidates"]] == [A, B]


@case("没有问题弹幕或高中刷屏的普通记录不纳入常规候选页")
def no_problem():
    assert not groups(problems={})
    assert groups(row=sender(spam_level="中"), problems={})


@case("问题弹幕和刷屏人工误报均扣除，撤销可恢复")
def false_positive():
    assert not groups(fp={("dm", "问题原文")})
    assert not groups(row=sender(spam_level="高"), problems={}, fp={("spam", H)})
    assert groups(row=sender(spam_level="高"), fp={("dm", "问题原文")})


@case("原文必须属于当前匿名hash，不能从其他发送者或候选评论拼接")
def attribution():
    assert not groups(problems={"他人原文": problem("他人原文")})
    assert groups()[0]["problems"][0]["content"] == "问题原文"


@case("目标模式列出显式匹配的纯候选，标明匹配不等于问题")
def target_selection():
    out = groups(problems={}, target_matches=rows()[:1])
    html = crc.render_crc_groups(out, "/endpoint", False, str, str, target_mode=True)
    assert len(out) == 1 and "命中筛选不等于问题判定" in html
    assert not groups(row=sender(selected=False), target_matches=rows())


@case("解析时保存的存在性状态可复用，过期后保留为待复核")
def recorded_status():
    row = sender()
    row["resolution"]["candidate_status"] = {str(A): "exists", str(B): "not_exists"}
    assert [c["status"] for c in groups(row=row)[0]["candidates"]] == ["exists", "not_exists"]
    row["resolution"]["checked_at"] = time.time() - 8 * 86400
    assert all(c["status"] == "unknown" for c in groups(row=row)[0]["candidates"])


@case("新解析保留全部候选的验证状态，不改变原置信度规则")
def resolver_status():
    with patch.object(resolver, "crack_crc32", return_value=[A, B]), patch.object(resolver, "_batch_verify_uids", return_value=({A: {"uid": A}, B: {"uid": B}}, set())):
        result = resolver.resolve_all_senders({H: {"count": 2, "contents": ["问题原文"]}}, {}, object())[H]
    assert result["uid"] is None and result["method"] == "候选歧义"
    assert result["resolution"]["candidate_status"] == {str(A): "exists", str(B): "exists"}


@case("匿名问题证据独立落库，无UID或画像也可查看")
def standalone_evidence():
    storage.save_danmaku_problems(BV, {"问题原文": problem()}, {"问题原文"})
    assert storage.load_danmaku_problems(BV)["问题原文"]["category"] == "人身攻击"
    assert storage.load_user_data(A) is None


@case("成功覆盖范围原子替换，失败和未覆盖内容保持旧标注")
def partial_replace():
    storage.save_danmaku_problems(BV, {"甲": problem("甲"), "乙": problem("乙")}, {"甲", "乙"})
    storage.save_danmaku_problems(BV, {}, {"甲"})
    assert set(storage.load_danmaku_problems(BV)) == {"乙"}


@case("部分批次成功空判定清旧标注，失败批次证据保留")
def partial_judge():
    dms = rows()
    items = cd._dedup_contents(dms)
    storage.save_danmaku_problems(BV, {it["content"]: problem(it["content"]) for it in items}, {it["content"] for it in items})
    sender_groups = {H: {"contents": [d["content"] for d in dms]}}
    with patch.object(cd, "LLM_API_KEY", "离线占位"), patch.object(cd, "_judge_batches", return_value=(cd.BatchVerdicts([], {0}), 1, 2)):
        cd.detect_cringe_danmaku(dms, sender_groups, {"bvid": BV})
    assert set(storage.load_danmaku_problems(BV)) == {items[1]["content"]}


@case("整段缓存命中补齐匿名证据，无须重调LLM")
def aggregate_cache():
    dms = rows()
    sender_groups = {H: {"contents": [d["content"] for d in dms]}}
    key = cd._danmaku_cache_key(cd._dedup_contents(dms), sender_groups, BV)
    storage.save_llm_cache(key, json.dumps({H: {"items": [problem()], "count": 1}}))
    with patch.object(cd, "LLM_API_KEY", "离线占位"), patch.object(cd, "_judge_batches", side_effect=AssertionError("不调用LLM")):
        cd.detect_cringe_danmaku(dms, sender_groups, {"bvid": BV})
    assert "问题原文" in storage.load_danmaku_problems(BV)


@case("旧缓存只读恢复问题证据，其他视频或不同内容不能串用")
def legacy_cache():
    setup_video(with_problem=False)
    dms = rows()
    sender_groups = {H: {"contents": [d["content"] for d in dms]}}
    key = cd._danmaku_cache_key(cd._dedup_contents(dms), sender_groups, BV)
    storage.save_llm_cache(key, json.dumps({H: {"items": [problem()]}}))
    assert cd.load_cached_danmaku_problems(dms, sender_groups, BV)
    assert not cd.load_cached_danmaku_problems(dms, sender_groups, "BVOTHER")
    assert not cd.load_cached_danmaku_problems(dms[:1], {H: {"contents": ["问题原文"]}}, BV)
    html, count = web._crc_candidate_tab_html(BV)
    assert count == 1 and "问题原文" in html and storage.load_danmaku_problems(BV) == {}


@case("常规报告纯破解页在低置信度后，无画像仍展示全部候选")
def normal_route():
    setup_video()
    with patch.object(web, "_get_client", side_effect=AssertionError("报告不登录")):
        response = web.app.test_client().get(f"/video/{BV}")
    html = response.text
    assert response.status_code == 200 and html.index('data-tab="lowconf"') < html.index('data-tab="crc"')
    assert str(A) in html and str(B) in html and "问题原文" in html and 'id="tab-crc"' in html


@case("目标报告也有纯破解页，薄快照字段缺失时正常降级")
def target_route():
    item = target_record()
    item["result"]["matches"] = [{"mid_hash": H, "content": "问题原文", "time": 1}]
    storage.save_target_analysis(item["id"], BV, item["selector"], item["result"])
    with patch.object(web, "_get_client", side_effect=AssertionError("目标报告不采集")):
        response = web.app.test_client().get("/target/crc-offline")
    assert response.status_code == 200 and 'data-tab="crc"' in response.text
    assert str(A) in response.text and str(B) in response.text and "目标命中原文" in response.text


@case("候选页原文防XSS，遮蔽开关作用于候选昵称和可见UID")
def escaping():
    out = groups(verifications={A: ("exists", {"name": "候选昵称甲"})})
    out[0]["problems"][0]["content"] = '<script>攻击</script>'
    html = crc.render_crc_groups(out, "/endpoint", True, web._mask_name, web._mask_uid)
    from lxml import html as parser
    text = parser.fromstring(html).text_content()
    assert "&lt;script&gt;攻击&lt;/script&gt;" in html and "<script>攻击</script>" not in html
    assert str(A) not in text and "候选昵称甲" not in text


@case("正常报告接口拒绝无关UID、无问题记录及外站Origin")
def api_validation():
    setup_video()
    client = web.app.test_client()
    path = f"/api/video/{BV}/candidate-space"
    with patch.object(web, "_get_client", side_effect=AssertionError("错误输入不采集")):
        assert client.post(path, json={"mid_hash": H, "uid": 456789}).status_code == 400
        assert client.post(path, json={"mid_hash": H, "uid": A}, headers={"Origin": "https://outside.example"}).status_code == 403
        assert client.post("/api/video/unknown/candidate-space", json={}).status_code == 404
        storage.save_danmaku_problems(BV, {}, {"问题原文"})
        assert client.post(path, json={"mid_hash": H, "uid": A}).status_code == 400


@case("数学候选和问题原文展示不污染明文证据库、画像或屏蔽列表")
def isolation():
    setup_video(sender(spam_level="高"))
    html, count = web._crc_candidate_tab_html(BV)
    assert count == 1 and str(A) in html and str(B) in html
    assert storage.load_global_uid_map() == {} and storage.load_user_data(A) is None
    assert web._build_blocklist(BV, {"spam"}, 100, include_lowconf=True)["entries"] == []


@case("常规候选页核验接口复用独立空间缓存，不选中某个UID或创建画像")
def cached_space_api():
    setup_video()
    sample = {"version": 1, "uid": A, "checked_at": time.time(), "complete": True,
              "sources": {kind: {"status": "ok", "items": []} for kind in ("card", "videos", "dynamics", "followings")}}
    sample["sources"]["card"]["items"] = [{"name": "离线候选甲", "text": "公开签名"}]
    storage.save_uid_space_cache(A, sample)
    with patch.object(web, "_get_client", side_effect=AssertionError("缓存命中不登录")):
        response = web.app.test_client().post(f"/api/video/{BV}/candidate-space", json={"mid_hash": H, "uid": A})
    assert response.status_code == 200 and "离线候选甲" in response.get_json()["html"]
    updated = storage.load_senders(BV)[0]
    assert updated["uid"] is None and updated["method"] == "待验证" and storage.load_user_data(A) is None
    assert updated["resolution"]["candidate_status"] == {str(A): "exists", str(B): "unknown"}


@case("旧候选集合可从数学缓存补齐，读取报告不重算CRC或联网")
def mathematical_fallback():
    row = sender()
    row["resolution"]["candidates"] = []
    setup_video(row)
    storage.save_uid_candidates(H, config.MITM_MAX_UID, resolver.CANDIDATE_VERSION, [A, B])
    with patch.object(resolver, "crack_crc32", side_effect=AssertionError("报告不破解")):
        html, count = web._crc_candidate_tab_html(BV)
    assert count == 1 and str(A) in html and str(B) in html


@case("旧的成功批次缓存可恢复，越界和错误类别不生成问题证据")
def legacy_batch():
    import hashlib
    dms = rows()
    items = cd._dedup_contents(dms)
    digest = hashlib.sha256(json.dumps([it["content"] for it in items], ensure_ascii=False).encode()).hexdigest()[:16]
    key = f"cringe:{BV}:{cd._DM_CACHE_VERSION}@batch:v2:{cd.LLM_MODEL}:{digest}"
    storage.save_llm_cache(key, json.dumps([{"i": 0, "category": "广告引流", "severity": 2},
                                          {"i": 99, "category": "人身攻击"}, {"i": 1, "category": "未知分类"}]))
    result = cd.load_cached_danmaku_problems(dms, {H: {"contents": [d["content"] for d in dms]}}, BV)
    assert set(result) == {items[0]["content"]}


@case("问题标注或候选存在性改变时，即使条数不变也使页面缓存失效")
def revisions():
    setup_video()
    first = web._page_fingerprint(BV)
    storage.save_danmaku_problems(BV, {"问题原文": problem(category="广告引流")}, {"问题原文"})
    second = web._page_fingerprint(BV)
    storage.save_uid_verify_cache(A, "exists", {"uid": A, "name": "离线昵称"})
    assert first != second != web._page_fingerprint(BV)


@case("明确删除和强制清理均清除匿名问题证据")
def cleanup():
    setup_video()
    storage.clear_video_cache(BV)
    assert storage.load_danmaku_problems(BV) == {}
    setup_video()
    storage.delete_video_data(BV)
    assert storage.load_danmaku_problems(BV) == {}


class CardClient:
    def __init__(self, values=None):
        self.values = values or {}
        self.calls = []

    def get(self, url, params):
        self.calls.append((url, params))
        uid = params["mid"]
        value = self.values.get(uid, {"code": 0, "data": {"card": {
            "mid": str(uid), "name": "离线昵称甲", "sign": "公开签名", "face": "https://i0.hdslb.com/test.png"}}})
        if isinstance(value, Exception):
            raise value
        return value


def card_post(uid, fake=None, target=False):
    path = f"/api/target/crc-offline/candidate-space" if target else f"/api/video/{BV}/candidate-space"
    with patch.object(web, "_get_client", return_value=fake or CardClient()):
        return web.app.test_client().post(path, json={"mid_hash": H, "uid": uid, "mode": "card"})


@case("候选是直接可点击的长条名片，头像昵称签名UID完整且按钮不嵌套在链接中")
def long_card():
    from lxml import html as parser
    out = groups(verifications={A: ("exists", {"name": "离线昵称甲", "face": "http://i0.hdslb.com/test.png", "sign": "公开签名"})})
    root = parser.fromstring(crc.render_crc_groups(out, "/endpoint", False, str, str))
    anchors = root.xpath('//a[@class="candidate-card"]')
    assert len(anchors) == 2 and anchors[0].get("href") == f"https://space.bilibili.com/{A}"
    assert "公开签名" in anchors[0].text_content() and str(A) in anchors[0].text_content()
    assert anchors[0].xpath('.//img')[0].get("src") == "https://i0.hdslb.com/test.png"
    assert not anchors[0].xpath('.//button') and not root.xpath('//details[@class="space-candidate"]')


@case("危险头像协议及名片XSS被剔除或转义，空签名与待读取区分")
def card_escaping():
    row = {"uid": A, "name": '<img onerror="x">', "face": "javascript:alert(1)", "sign": "", "card_ready": True}
    text = crc.render_candidate_card(row, H, "/endpoint", False, str, str)
    assert "javascript:" not in text and "<img onerror" not in text and "未设置签名" in text
    row["card_ready"] = False
    assert "签名待读取" in crc.render_candidate_card(row, H, "/endpoint", False, str, str)


@case("自动名片模式只请求一个限速名片接口，不采评论动态投稿关注或画像")
def card_only_api():
    setup_video()
    fake = CardClient()
    response = card_post(A, fake)
    assert response.status_code == 200 and response.json["status"] == "exists"
    assert fake.calls == [(config.USER_CARD_URL, {"mid": A})]
    assert "公开签名" in response.json["group_html"] and "candidate-avatar" in response.json["group_html"]
    assert storage.load_user_data(A) is None and storage.load_global_uid_map() == {}


@case("完整名片和明确阴性命中缓存，不需要Cookie或API客户端")
def card_cache_api():
    setup_video()
    storage.save_uid_verify_cache(A, "exists", {"uid": A, "name": "缓存昵称", "face": "", "sign": ""})
    storage.save_uid_verify_cache(B, "not_exists", {})
    with patch.object(web, "_get_client", side_effect=AssertionError("完整缓存不登录")):
        for uid in (A, B):
            response = web.app.test_client().post(f"/api/video/{BV}/candidate-space", json={"uid": uid, "mid_hash": H, "mode": "card"})
            assert response.status_code == 200


@case("批量名片只有昵称头像时，补一次单点名片读取签名")
def incomplete_card():
    setup_video()
    storage.save_uid_verify_cache(A, "exists", {"uid": A, "name": "旧昵称", "face": ""})
    fake = CardClient()
    assert card_post(A, fake).json["status"] == "exists" and len(fake.calls) == 1
    assert storage.load_uid_verify_cache([A], 100, 100)[A][1]["sign"] == "公开签名"


@case("明确-404排除其他全部候选，常规发送者收敛唯一存在UID并标高（存在性排除）")
def normal_convergence():
    setup_video()
    card_post(A)
    response = card_post(B, CardClient({B: {"code": -404}}))
    row = storage.load_senders(BV)[0]
    assert row["uid"] == A and row["confidence"] == "高" and row["method"] == "CRC32破解"
    assert row["resolution"]["candidates"] == [A, B] and row["resolution"]["collision_risk"]
    assert response.json["group_removed"] and response.json["candidate_count"] == 0
    assert not response.json["group_html"] and "暂无待复核" in response.json["empty_html"]
    assert storage.load_global_uid_map() == {} and storage.load_user_data(A) is None


@case("目标快照收敛后UID昵称与统计同步，保留命中原文且不自动产生画像")
def target_convergence():
    item = target_record()
    storage.save_target_analysis(item["id"], BV, item["selector"], item["result"])
    card_post(A, target=True)
    response = card_post(B, CardClient({B: {"code": -404}}), target=True)
    result = storage.load_target_analysis(item["id"])["result"]
    assert response.status_code == 200 and result["senders"][0]["uid"] == A
    assert result["senders"][0]["name"] == "离线昵称甲" and result["senders"][0]["confidence"] == "高"
    assert result["stats"]["low_confidence_count"] == 0 and result["stats"]["unresolved_count"] == 0
    assert not result["profiles"] and result["matches"] == item["result"]["matches"]
    assert storage.list_target_analyses(BV)[0]["summary"]["low_confidence_count"] == 0


@case("一名存在而另一名核验未知，仍不分配UID，不把404网页或异常当成不存在")
def unknown_no_convergence():
    for value in ({"code": -403}, {"code": -400}, {"code": 0, "data": {}}, "404网页", RuntimeError("请求失败")):
        setup_video()
        with contextlib.closing(storage.get_db()) as conn:
            conn.execute("DELETE FROM uid_verify_cache")
            conn.commit()
        card_post(A)
        response = card_post(B, CardClient({B: value}))
        row = storage.load_senders(BV)[0]
        assert response.json["status"] == "unknown" and row["uid"] is None and row["method"] == "待验证"
        assert row["resolution"]["candidate_status"][str(B)] == "unknown"


@case("两名真实存在候选保留歧义，不能按昵称签名或UID大小挑选")
def multiple_existing():
    setup_video()
    card_post(A)
    card_post(B)
    row = storage.load_senders(BV)[0]
    assert row["uid"] is None and row["method"] == "候选歧义"


@case("全部候选明确不存在仍保留匿名及排除清单，不产生最终UID")
def all_missing():
    setup_video()
    for uid in (A, B):
        response = card_post(uid, CardClient({uid: {"code": -404}}))
    row = storage.load_senders(BV)[0]
    assert row["uid"] is None and row["method"] == "CRC32碰撞" and row["confidence"] == "无"
    assert "候选均不存在" in response.json["group_html"] and "candidate-card\"" not in response.json["group_html"]


@case("名片风控响应和异常向前端传递停止信号，未知不写阴性缓存")
def card_risk():
    setup_video()
    for value in ({"code": -412}, RiskControlError("离线风控")):
        response = card_post(A, CardClient({A: value}))
        assert response.json["risk"] and response.json["status"] == "unknown"
        assert not storage.load_uid_verify_cache([A], 100, 100)


@case("名片UID不匹配不能写入存在缓存或为别人的头像签名归属")
def card_mismatch():
    setup_video()
    response = card_post(A, CardClient({A: {"code": 0, "data": {"card": {"mid": B, "name": "错误用户"}}}}))
    assert response.json["status"] == "unknown" and "错误用户" not in response.json["group_html"]
    assert not storage.load_uid_verify_cache([A], 100, 100)


@case("回写候选时沿用每个原核验时间，不延长即将过期的阴性结论")
def negative_age():
    setup_video()
    at = time.time() - cards.UID_VERIFY_MISSING_TTL + 3
    storage.save_uid_verify_cache(B, "not_exists", {}, at)
    card_post(A)
    row = storage.load_senders(BV)[0]
    assert row["resolution"]["candidate_checked_at"][str(B)] == at
    out = groups(row=row, verifications={A: ("exists", {"uid": A})})
    with patch.object(crc.time, "time", return_value=at + cards.UID_VERIFY_MISSING_TTL + 1):
        out = groups(row=row, verifications={A: ("exists", {"uid": A})})
    assert out[0]["candidates"][1]["status"] == "unknown"


@case("旧空间样本不覆盖较新的-404存在性结论")
def older_space():
    at = time.time()
    storage.save_uid_verify_cache(A, "not_exists", {}, at)
    cards.cache_space_card({"uid": A, "checked_at": at - 100, "sources": {"card": {
        "status": "ok", "items": [{"name": "旧昵称", "text": "签名"}]}}})
    assert storage.load_uid_verify_cache([A], 1000, 1000)[A][0] == "not_exists"


@case("空间辅助核验得到-404时同样回写排除并收敛，不局限自动名片模式")
def space_missing_convergence():
    setup_video()
    card_post(A)
    with patch.object(web, "_get_client", return_value=CardClient({B: {"code": -404}})):
        response = web.app.test_client().post(f"/api/video/{BV}/candidate-space", json={"mid_hash": H, "uid": B})
    assert response.status_code == 200 and storage.load_senders(BV)[0]["uid"] == A
    assert storage.load_uid_verify_cache([B], 100, 100)[B][0] == "not_exists"


@case("长网络核验期间最新快照变成明文解析，旧纯破解不能覆盖最新结果")
def latest_snapshot():
    item = target_record()
    storage.save_target_analysis(item["id"], BV, item["selector"], item["result"])
    class ChangingClient(CardClient):
        def get(self, url, params):
            current = storage.load_target_analysis(item["id"])
            current["result"]["senders"][0].update(uid=B, confidence="高", method="评论区验证", name="新结果")
            current["result"]["comments"] = [{"uid": B, "content": "新找到的评论"}]
            storage.save_target_analysis(item["id"], BV, current["selector"], current["result"])
            return super().get(url, params)
    assert card_post(A, ChangingClient(), target=True).status_code == 200
    result = storage.load_target_analysis(item["id"])["result"]
    assert result["senders"][0]["uid"] == B and result["senders"][0]["confidence"] == "高"
    assert result["comments"][0]["content"] == "新找到的评论"


@case("旧误分配UID明确不存在后撤回，错误画像仅留审计不继续展示")
def withdraw_bad_uid():
    item = target_record()
    item["result"]["senders"][0].update(uid=B, confidence="低", method="CRC32破解")
    item["result"]["profiles"] = [{"uid": B, "target_mid_hash": H, "name": "旧误归属"}]
    storage.save_target_analysis(item["id"], BV, item["selector"], item["result"])
    card_post(B, CardClient({B: {"code": -404}}), target=True)
    result = storage.load_target_analysis(item["id"])["result"]
    assert result["senders"][0]["uid"] is None and result["senders"][0]["resolution"]["previous_uid"] == B
    assert not result["profiles"] and result["excluded_profiles"][0]["uid"] == B


@case("只有旧单UID缺少候选集合时不虚构唯一结论，已有本地数学集合可补齐")
def incomplete_candidates():
    row = sender(uid=A, method="CRC32破解", confidence="低", resolution={})
    setup_video(row)
    storage.save_uid_verify_cache(A, "not_exists", {})
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("DELETE FROM uid_candidate_cache"); conn.commit()
    assert cards.reconcile_crc_sender(BV, H) is None
    storage.save_uid_candidates(H, config.MITM_MAX_UID, "mitm-v1", [A, B])
    storage.save_uid_verify_cache(B, "exists", {"uid": B, "name": "幸存候选", "sign": "", "face": ""})
    updated = cards.reconcile_crc_sender(BV, H)
    assert updated["uid"] == B and updated["confidence"] == "高"


@case("候选核验后正常报告刷新能显示缓存名片，GET不登录也不再次调用API")
def refreshed_report():
    setup_video()
    card_post(A)
    with patch.object(web, "_get_client", side_effect=AssertionError("GET只读缓存")):
        response = web.app.test_client().get(f"/video/{BV}")
    assert response.status_code == 200 and "离线昵称甲" in response.text and "公开签名" in response.text


@case("核验后的目标CSV和JSON即时下载采用最新UID，保留完整候选审计")
def latest_export():
    import csv
    item = target_record()
    storage.save_target_analysis(item["id"], BV, item["selector"], item["result"])
    card_post(A, target=True)
    card_post(B, CardClient({B: {"code": -404}}), target=True)
    client = web.app.test_client()
    with patch.object(web, "_get_client", side_effect=AssertionError("下载不联网")):
        response = client.get('/api/target/crc-offline/export/json')
        assert response.status_code == 200 and response.json["senders"][0]["uid"] == A
        response = client.get('/api/target/crc-offline/export/csv')
        row = next(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
        assert row["UID"] == str(A) and row["置信度"] == "高" and str(B) in row["候选UID"]


@case("即时导出拒绝不存在报告及未知格式，CSV单元格仍防公式注入")
def safe_export():
    item = target_record()
    item["result"]["senders"][0]["name"] = "=SUM(1,2)"
    storage.save_target_analysis(item["id"], BV, item["selector"], item["result"])
    client = web.app.test_client()
    assert client.get('/api/target/crc-offline/export/csv').status_code == 200
    assert "'=SUM(1,2)" in client.get('/api/target/crc-offline/export/csv').text
    assert client.get('/api/target/unknown/export/json').status_code == 404
    assert client.get('/api/target/crc-offline/export/html').status_code == 404


@case("另一报告共享的完整存在缓存可收敛本报告，缓存同步不登录或发起B站请求")
def shared_cache_reconciliation():
    setup_video()
    storage.save_uid_verify_cache(A, "exists", {"uid": A, "name": "缓存昵称", "sign": "", "face": ""})
    storage.save_uid_verify_cache(B, "not_exists", {})
    html, _ = web._crc_candidate_tab_html(BV)
    assert 'data-reconcile="1"' in html
    with patch.object(web, "_get_client", side_effect=AssertionError("缓存同步不登录")):
        response = web.app.test_client().post(f'/api/video/{BV}/candidate-space', json={"mid_hash": H, "uid": A, "mode": "reconcile"})
    assert response.status_code == 200 and storage.load_senders(BV)[0]["uid"] == A
    assert response.json["group_removed"] and response.json["candidate_count"] == 0


@case("缓存已确认全部候选不存在时，无可见用户卡仍可同步撤回旧UID")
def shared_missing_cache():
    setup_video(sender(uid=B, confidence="低", method="CRC32破解"))
    for uid in (A, B):
        storage.save_uid_verify_cache(uid, "not_exists", {})
    html, _ = web._crc_candidate_tab_html(BV)
    assert 'data-reconcile="1"' in html and 'class="candidate-card"' not in html
    with patch.object(web, "_get_client", side_effect=AssertionError("同步排除不登录")):
        response = web.app.test_client().post(f'/api/video/{BV}/candidate-space', json={"mid_hash": H, "uid": A, "mode": "reconcile"})
    assert response.status_code == 200 and storage.load_senders(BV)[0]["uid"] is None


@case("高置信度存在性排除徽标与明文交叉验证分开，仍提示碰撞风险")
def high_crc_label():
    from report import generate_user_card
    html = generate_user_card({"uid": A, "name": "测试", "resolve_confidence": "高",
                               "resolve_method": "CRC32破解", "collision_risk": True})
    assert "高（存在性排除）" in html and "可能误识别" in html and "高（交叉验证）" not in html
    item = target_record()
    storage.save_target_analysis(item["id"], BV, item["selector"], item["result"])
    card_post(A, target=True)
    card_post(B, CardClient({B: {"code": -404}}), target=True)
    assert "CRC32破解 · 高（存在性排除）" in web.app.test_client().get('/target/crc-offline').text


@case("旧低置信度完整已核验候选同步升高，版本指纹与原核验时刻保持不变")
def upgrade_old_crc():
    row = sender(uid=A, confidence="低", method="CRC32破解")
    row["resolution"].update(version="evidence-v3", fingerprint="旧指纹")
    setup_video(row)
    item = target_record()
    item["result"]["senders"][0].update(uid=A, confidence="低", method="CRC32破解", resolution=row["resolution"])
    storage.save_target_analysis(item["id"], BV, item["selector"], item["result"])
    storage.save_uid_verify_cache(A, "exists", {"uid": A})
    at = time.time() - 500
    storage.save_uid_verify_cache(B, "not_exists", {}, at)
    storage.init_db()
    out = storage.load_senders(BV)[0]
    result = storage.load_target_analysis(item["id"])["result"]
    assert out["confidence"] == result["senders"][0]["confidence"] == "高"
    assert out["resolution"]["version"] == "evidence-v3" and out["resolution"]["fingerprint"] == "旧指纹"
    assert out["resolution"]["candidate_checked_at"][str(B)] == at and storage.load_global_uid_map() == {}
    assert result["stats"]["low_confidence_count"] == 0


@case("旧低分级缺少有效阴性缓存或候选集不完整时不得迁移为高")
def incomplete_upgrade():
    row = sender(uid=A, confidence="低", method="CRC32破解")
    setup_video(row)
    storage.save_uid_verify_cache(A, "exists", {"uid": A})
    storage.save_uid_verify_cache(B, "not_exists", {}, time.time() - cards.UID_VERIFY_MISSING_TTL - 1)
    storage.init_db()
    assert storage.load_senders(BV)[0]["confidence"] == "低"
    row["resolution"]["candidates"] = [A]
    setup_video(row)
    storage.save_uid_verify_cache(B, "not_exists", {})
    storage.init_db()
    assert storage.load_senders(BV)[0]["confidence"] == "低"


@case("遗漏的数学候选从完整缓存补回，未验证者阻止只凭一个已存UID升高")
def restore_missing_candidate():
    row = sender(uid=A, confidence="低", method="CRC32破解")
    row["resolution"]["candidates"] = [A]
    setup_video(row)
    storage.save_uid_verify_cache(A, "exists", {"uid": A, "face": "", "sign": ""})
    updated = cards.reconcile_crc_sender(BV, H)
    assert updated["candidates"] == [A, B] and updated["uid"] is None and updated["method"] == "待验证"


@case("高分级解析缓存有一个阴性结论过期即失效，不沿用默认一天的解析缓存")
def high_cache_expiry():
    storage.save_uid_verify_cache(A, "exists", {"uid": A})
    storage.save_uid_verify_cache(B, "not_exists", {})
    verdict = (A, "高", "CRC32破解", {}, True, [A, B])
    row = sender(uid=A, confidence="高", method="CRC32破解", resolution=resolver.resolution_record(H, verdict, {}))
    assert resolver.resolution_is_current(row, H, {})
    storage.save_uid_verify_cache(B, "not_exists", {}, time.time() - cards.UID_VERIFY_MISSING_TTL - 1)
    # 新结论不能被旧写入回滚；模拟时间推进让既有阴性到期。
    with patch.object(resolver.time, "time", return_value=time.time() + cards.UID_VERIFY_MISSING_TTL + 1):
        assert not resolver.resolution_is_current(row, H, {})


@case("解析命中存在性缓存保留真实核验时间，不能刷新阴性有效期")
def resolver_keeps_verification_time():
    at = time.time() - cards.UID_VERIFY_MISSING_TTL + 2
    storage.save_uid_verify_cache(A, "exists", {"uid": A})
    storage.save_uid_verify_cache(B, "not_exists", {}, at)
    with patch.object(resolver, "crack_crc32", return_value=[A, B]):
        out = resolver.resolve_all_senders({H: {"count": 1, "contents": ["测试"]}}, {}, None)[H]
    assert out["confidence"] == "高" and out["resolution"]["candidate_checked_at"][str(B)] == at


@case("较新的阴性已过期时不能复活更旧的阳性快照，继续保留未知")
def expired_negative_does_not_revive_positive():
    old_at = time.time() - 10000
    row = sender(uid=A, confidence="低", method="CRC32破解")
    row["resolution"].update(candidate_status={str(A): "exists", str(B): "not_exists"},
                             candidate_checked_at={str(A): old_at, str(B): time.time()})
    setup_video(row)
    latest = time.time() - cards.UID_VERIFY_MISSING_TTL - 1
    storage.save_uid_verify_cache(A, "not_exists", {}, latest)
    storage.save_uid_verify_cache(B, "not_exists", {})
    out = groups(row=row, verifications={B: ("not_exists", {})}, verification_times={str(A): latest})
    assert out[0]["candidates"][0]["status"] == "unknown"
    updated = cards.reconcile_crc_sender(BV, H)
    assert updated["uid"] is None and updated["method"] == "待验证"


@case("非标准存在性状态也视为未知，不能将其冒充明确不存在并升高")
def invalid_verification_status():
    out = resolver._finalize(H, 1, None, [A, B], None,
                             lambda u: "exists" if u == A else "接口异常", lambda u: {})
    assert out[0] is None and out[2] == "待验证"


@case("高分级且唯一有效的CRC记录移出候选页，接口校验仍可读取其原始候选")
def resolved_hidden():
    row = sender(uid=A, confidence="高", method="CRC32破解")
    row["resolution"]["candidate_version"] = "mitm-v1"
    verified = {A: ("exists", {"uid": A}), B: ("not_exists", {})}
    assert not groups(row=row, verifications=verified)
    assert groups(row=row, verifications=verified, include_resolved=True)[0]["candidates"][1]["uid"] == B


@case("高分级但仍有未知或旧核验过期的记录保留复核，不仅凭高字样隐藏")
def stale_high_retained():
    row = sender(uid=A, confidence="高", method="CRC32破解")
    row["resolution"].update(candidate_version="mitm-v1", candidate_status={str(A): "exists", str(B): "not_exists"},
                             candidate_checked_at={str(A): time.time(), str(B): time.time() - cards.UID_VERIFY_MISSING_TTL - 1})
    out = groups(row=row, verifications={A: ("exists", {"uid": A})})
    assert len(out) == 1 and out[0]["candidates"][1]["status"] == "unknown"


@case("已有完整缓存但尚未同步高分级的条目仍显示，回写完成才移除")
def only_remove_after_assignment():
    setup_video()
    storage.save_uid_verify_cache(A, "exists", {"uid": A, "name": "缓存昵称", "face": "", "sign": ""})
    storage.save_uid_verify_cache(B, "not_exists", {})
    assert web._crc_candidate_tab_html(BV)[1] == 1
    response = card_post(A)
    assert response.json["group_removed"] and web._crc_candidate_tab_html(BV)[1] == 0
    assert storage.load_senders(BV)[0]["confidence"] == "高"


@case("移出候选页不删除已解析结果、画像、数学候选、弹幕原文或导出审计")
def resolved_routes_keep_data():
    from lxml import html as parser
    setup_video()
    item = target_record()
    storage.save_target_analysis(item["id"], BV, item["selector"], item["result"])
    card_post(A)
    card_post(B, CardClient({B: {"code": -404}}))
    card_post(A, target=True)
    storage.save_user_data(A, "已解析昵称", 6, {"uid": A, "name": "已解析昵称"},
                           {"uid": A, "name": "已解析昵称"}, bvid=BV)
    before = storage.load_senders(BV)
    client = web.app.test_client()
    with patch.object(web, "_get_client", side_effect=AssertionError("渲染不采集")):
        for path in (f'/video/{BV}', '/target/crc-offline'):
            response = client.get(path)
            assert response.status_code == 200
            pane = parser.fromstring(response.text).get_element_by_id('tab-crc')
            assert not pane.xpath('.//section[@class="crc-sender"]') and "暂无待复核" in pane.text_content()
        target_html = client.get('/target/crc-offline').text
        assert "CRC32破解 · 高（存在性排除）" in target_html
        exported = client.get('/api/target/crc-offline/export/json').json
        assert exported["senders"][0]["uid"] == A and exported["senders"][0]["resolution"]["candidates"] == [A, B]
    assert storage.load_senders(BV) == before and len(storage.load_danmaku(BV)) == 2
    assert storage.load_video_profile(BV, A)["name"] == "已解析昵称"
    assert storage.load_uid_candidates(H, config.MITM_MAX_UID, "mitm-v1") == [A, B]


@case("隐藏后的有效候选接口可安全读取缓存，不因列表过滤丢失授权检查或额外采集")
def hidden_api_cached():
    setup_video()
    card_post(A)
    card_post(B, CardClient({B: {"code": -404}}))
    with patch.object(web, "_get_client", side_effect=AssertionError("已解析缓存不采集")):
        response = web.app.test_client().post(f'/api/video/{BV}/candidate-space', json={"uid": A, "mid_hash": H, "mode": "card"})
    assert response.status_code == 200 and response.json["group_removed"]
    assert response.json["candidate_count"] == 0 and not response.json["group_html"]


@case("一个高分级条目移除后仍准确保留其他未解析条目，返回剩余数量")
def partial_removal():
    setup_video()
    other = crc32_uid(9)
    storage.append_danmaku(BV, [{**rows()[0], "mid_hash": other, "dmid": "other"}], {d["dmid"] for d in rows()})
    storage.save_sender(BV, other, None, "无", "待验证", 1, ["问题原文"], "低", 0,
                        resolution={"candidates": [9]})
    storage.save_uid_candidates(other, config.MITM_MAX_UID, "mitm-v1", [9])
    assert web._crc_candidate_tab_html(BV)[1] == 2
    card_post(A)
    response = card_post(B, CardClient({B: {"code": -404}}))
    assert response.json["group_removed"] and response.json["candidate_count"] == 1 and not response.json["empty_html"]
    html, count = web._crc_candidate_tab_html(BV)
    assert count == 1 and f'data-hash="{other}"' in html and f'data-hash="{H}"' not in html


def main():
    requests.sessions.Session.request = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("禁止真实网络"))
    passed = failed = 0
    with tempfile.TemporaryDirectory(prefix="bili_crc_candidates_") as tmp:
        for index, (name, fn) in enumerate(CASES):
            storage.DB_PATH = config.DB_PATH = str(Path(tmp) / f"{index}.db")
            web.JOBS.clear()
            web._PAGE_CACHE.clear()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    storage.init_db()
                    fn()
                passed += 1
                print(f"  ✔ {name}")
            except Exception as exc:
                failed += 1
                print(f"  ✘ {name}：{type(exc).__name__}: {exc}")
    print(f"==== 纯破解候选: {passed} 项通过, {failed} 项失败 ====")
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
