"""候选空间辅助核验：按需采一页公开内容，仅展示关联线索，不确认弹幕身份。"""
from datetime import datetime
import re
import time
import unicodedata

import config
from api_client import RiskControlError
from storage import load_uid_space_cache, save_uid_space_cache
from uid_evidence import candidates_of, crc32_uid

SOURCE_NAMES = {"card": "公开名片", "videos": "投稿首页", "dynamics": "动态首页", "followings": "关注首页"}
LIMIT_NOTE = "只检查公开首页样本；兴趣或措辞相似不等于身份确认，未发现关联也不能排除候选。保留原解析置信度，不提供未经校准的身份概率。"


def candidate_uids(sender: dict) -> list[int]:
    """只能核验当前发送者的实际碰撞候选，且必须正向 CRC32 相符。"""
    if sender.get("selected") is False:
        return []
    values = candidates_of(sender.get("candidates", []))
    values |= candidates_of(sender.get("uid"))
    values |= candidates_of(sender.get("resolution", {}).get("candidates", []))
    return sorted(uid for uid in values if crc32_uid(uid) == sender.get("mid_hash"))


def sample_is_fresh(sample: dict, now: float | None = None) -> bool:
    if sample.get("version") != 1:
        return False
    sources = sample.get("sources")
    if not isinstance(sources, dict):
        return False
    for kind in SOURCE_NAMES:
        source = sources.get(kind)
        if (not isinstance(source, dict) or source.get("status") not in
                ("ok", "unavailable", "risk", "skipped", "not_exists") or
                not isinstance(source.get("items"), list) or
                any(not isinstance(item, dict) for item in source["items"])):
            return False
    ttl = (getattr(config, "UID_SPACE_CACHE_TTL", 21600) if sample.get("complete")
           else getattr(config, "UID_SPACE_RETRY_AFTER", 60))
    try:
        age = (time.time() if now is None else now) - float(sample["checked_at"])
        return 0 <= age < ttl
    except (KeyError, TypeError, ValueError):
        return False


def _text(value) -> str:
    return value[:500] if isinstance(value, str) else ""


def _video_url(bvid) -> str:
    return f"https://www.bilibili.com/video/{bvid}" if isinstance(bvid, str) and re.fullmatch(r"BV[0-9A-Za-z]+", bvid) else ""


def _normalize(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def _parse_source(kind: str, data: dict, uid: int, limit: int) -> list[dict]:
    """缺失字段或错误账号不能伪装成已检查的空空间。"""
    if kind == "card":
        card = data.get("card")
        if not isinstance(card, dict) or str(card.get("mid")) != str(uid):
            raise ValueError("名片 UID 不符或资料缺失")
        return [{"text": _text(card.get("sign")), "name": _text(card.get("name")), "face": _text(card.get("face")),
                 "url": f"https://space.bilibili.com/{uid}"}]
    field = {"videos": "archives", "dynamics": "items", "followings": "list"}[kind]
    raw = data.get(field)
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw[:limit]):
        raise ValueError("公开列表格式异常")
    items = []
    for item in raw[:limit]:
        if kind == "videos":
            bvid = item.get("bvid", "")
            items.append({"text": _text(item.get("title")), "bvid": bvid, "url": _video_url(bvid)})
        elif kind == "followings":
            values = candidates_of(item.get("mid"))
            if not values:
                continue
            followed = next(iter(values))
            items.append({"text": _text(item.get("uname")), "uid": followed,
                          "url": f"https://space.bilibili.com/{followed}"})
        else:
            # 转发原文属于他人：可证明分享关联，但不能当作候选自己的措辞。
            modules = item.get("modules") or {}
            dynamic = modules.get("module_dynamic") or {}
            archive = (dynamic.get("major") or {}).get("archive") or {}
            original = item.get("orig") or {}
            orig_dynamic = (original.get("modules") or {}).get("module_dynamic") or {}
            orig_archive = (orig_dynamic.get("major") or {}).get("archive") or {}
            ident = str(item.get("id_str") or "")
            items.append({"text": _text((dynamic.get("desc") or {}).get("text")),
                          "title": _text(archive.get("title") or orig_archive.get("title")),
                          "bvid": archive.get("bvid") or orig_archive.get("bvid") or "",
                          "url": f"https://t.bilibili.com/{ident}" if ident.isdecimal() else "",
                          "forwarded": bool(original)})
    return items


def fetch_space_sample(uid: int, client, card_info: dict | None = None, card_checked_at: float | None = None) -> dict:
    """最多调用四个空间接口，各取首页；风控后停止后续请求，所有调用遵守客户端限速。"""
    limit = max(1, min(20, int(getattr(config, "UID_SPACE_SAMPLE_ITEMS", 20))))
    sample = {"version": 1, "uid": uid, "checked_at": time.time(), "sources": {}, "complete": False}
    calls = [("card", config.USER_CARD_URL, {"mid": uid}),
             ("videos", config.USER_VIDEOS_URL, {"mid": uid, "keywords": "", "ps": limit, "pn": 1}),
             ("dynamics", config.USER_DYNAMICS_URL, {"host_mid": uid}),
             ("followings", config.USER_FOLLOWINGS_URL, {"vmid": uid, "ps": limit, "pn": 1})]
    stopped = False
    for kind, url, params in calls:
        source = {"status": "unavailable", "items": []}
        sample["sources"][kind] = source
        if stopped:
            source.update(status="skipped", note="前序核验中止，未请求")
            continue
        if kind == "card" and card_info is not None:
            source.update(status="ok" if "sign" in card_info and "face" in card_info else "unavailable", items=[{"name": _text(card_info.get("name")),
                          "text": _text(card_info.get("sign")), "face": _text(card_info.get("face")),
                          "url": f"https://space.bilibili.com/{uid}"}])
            sample["card_checked_at"] = card_checked_at if card_checked_at is not None else sample["checked_at"]
            if source["status"] != "ok":
                source["note"] = "命令行名片字段未取得完整，保留缺失标记"
            continue
        try:
            response = client.get(url, params=params)
            if kind == "card":
                sample["card_checked_at"] = time.time()
            code = response.get("code") if isinstance(response, dict) else None
            if code in (-412, -352, -403):
                raise RiskControlError("空间接口风控")
            if kind == "card" and code == -404:
                source.update(status="not_exists", note="名片接口明确返回账号不存在")
                stopped = True
                continue
            if code != 0 or not isinstance(response.get("data"), dict):
                source["note"] = "资料不可见或接口未成功，不能据此排除候选"
                if kind == "card":
                    stopped = True
                continue
            source["items"] = _parse_source(kind, response["data"], uid, limit)
            source["status"] = "ok"
        except RiskControlError:
            source.update(status="risk", note="触发风控，已停止后续核验")
            stopped = True
        except Exception:
            # 不回显异常正文，避免代理凭证或请求参数随第三方错误泄露。
            source["note"] = "接口异常或返回资料无法核对，不能据此排除候选"
            if kind == "card":
                stopped = True
        if kind == "card" and source["status"] != "ok":
            stopped = True
    sample["complete"] = all(s["status"] == "ok" for s in sample["sources"].values())
    sample["checked_at"] = time.time()
    save_uid_space_cache(uid, sample)
    return sample


def assess_space(sample: dict, bvid: str, video_info: dict, contents: list[str]) -> dict:
    """原文和公开关联独立呈现；不将空间线索写进身份解析或全局证据库。"""
    evidence, examples, coverage = [], [], []
    example_counts = {}
    sources = sample.get("sources", {})
    minimum = max(8, int(getattr(config, "UID_SPACE_MIN_TEXT_LENGTH", 8)))
    phrases = {_normalize(c) for c in contents if isinstance(c, str)}
    phrases = {c for c in phrases if len(c) >= minimum and len(set(c)) >= 4}
    up_uids = candidates_of((video_info.get("owner") or {}).get("mid"))
    for kind, label in SOURCE_NAMES.items():
        source = sources.get(kind, {})
        items = source.get("items", [])
        coverage.append({"source": label, "status": source.get("status", "unavailable"),
                         "count": len(items), "note": source.get("note", "")})
        if source.get("status") != "ok":
            continue
        for item in items:
            text, title = item.get("text", ""), item.get("title", "")
            snippet = text or title
            url = item.get("url", "")
            if kind in ("videos", "dynamics") and item.get("bvid") == bvid:
                evidence.append({"kind": "video", "label": "同视频投稿或分享", "source": label,
                                 "text": snippet or bvid, "url": url})
            if kind == "followings" and item.get("uid") in up_uids:
                evidence.append({"kind": "following", "label": "关注目标视频 UP 主（弱线索）", "source": label,
                                 "text": text, "url": url})
            # 标题及转发原文可能由第三人撰写，不计为候选本人的相同措辞。
            if kind in ("card", "dynamics") and text and any(p in _normalize(text) for p in phrases):
                evidence.append({"kind": "text", "label": "出现相同长发言片段（可能为引用）", "source": label,
                                 "text": text, "url": url})
            if kind in ("card", "videos", "dynamics") and snippet and example_counts.get(kind, 0) < 3:
                examples.append({"source": label, "text": snippet, "url": url})
                example_counts[kind] = example_counts.get(kind, 0) + 1
    exists_status = sources.get("card", {}).get("status")
    if exists_status == "not_exists":
        verdict = "账号不存在，建议重新核验解析结果"
    elif any(e["kind"] == "video" for e in evidence):
        verdict = "有同视频关联线索，身份仍未确认"
    elif evidence:
        verdict = "有弱关联线索，身份仍未确认"
    elif sample.get("complete"):
        verdict = "公开样本未发现关联，不能排除候选"
    else:
        verdict = "资料不足，暂无法判断关联"
    card = sources.get("card", {}).get("items", [])
    return {"uid": sample["uid"], "name": card[0].get("name", "") if card else "",
            "verdict": verdict, "evidence": evidence, "examples": examples, "coverage": coverage,
            "checked_at": datetime.fromtimestamp(sample["checked_at"]).strftime("%Y-%m-%d %H:%M:%S"),
            "limit_note": LIMIT_NOTE}


def cached_assessments(record: dict) -> dict:
    """报告 GET 仅查缓存；尚未点击的候选不发起任何采集。"""
    senders = record["result"].get("senders", [])
    samples = load_uid_space_cache([u for s in senders for u in candidate_uids(s)])
    output = {}
    for sender in senders:
        for uid in candidate_uids(sender):
            sample = (sender.get("resolution", {}).get("space_samples") or {}).get(str(uid)) or samples.get(uid)
            if sample and isinstance(sample, dict) and sample.get("uid") == uid and sample_is_fresh(sample, now=sample.get("checked_at")):
                contents = sender_contents(record, sender)
                output[(sender["mid_hash"], uid)] = assess_space(sample, record["bvid"],
                                                              record["result"].get("video_info", {}), contents)
    return output


def sender_contents(record: dict, sender: dict) -> list[str]:
    return [dm.get("content", "") for dm in record["result"].get("matches", [])
            if dm.get("mid_hash") == sender["mid_hash"]] or sender.get("samples", [])
