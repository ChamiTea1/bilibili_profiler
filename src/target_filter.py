"""目标弹幕筛选：普通文本完整匹配，含正则元字符时自动使用正则搜索。"""
import hashlib
import json
import multiprocessing
import re

import config

_REGEX_META = re.compile(r"[\\.^$*+?{}\[\]()|]")


def make_selector(pattern: str, mode: str = "auto") -> dict:
    """在登录前校验输入；不剥离空白，保留用户指定的原文。"""
    if not pattern:
        raise ValueError("目标弹幕不能为空")
    if len(pattern) > getattr(config, "TARGET_PATTERN_MAX_LENGTH", 2048):
        raise ValueError("目标表达式过长")
    if mode not in ("auto", "literal", "regex"):
        raise ValueError("未知匹配方式")
    effective = ("regex" if _REGEX_META.search(pattern) else "literal") if mode == "auto" else mode
    if effective == "regex":
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"正则表达式无效：{e}；若要匹配原文，请加 --literal") from e
    return {"pattern": pattern, "mode": effective, "version": 1}


def analysis_id(bvid: str, selector: dict) -> str:
    """相同视频及实际匹配条件复用同一存档，不在 URL/文件名中暴露表达式。"""
    key = json.dumps([bvid, selector], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _regex_worker(pattern: str, contents: list[str], pipe):
    """在独立进程执行正则，避免病态回溯卡死整个分析流程。"""
    try:
        regex = re.compile(pattern)
        pipe.send((True, [i for i, content in enumerate(contents) if regex.search(content)]))
    except Exception as e:
        pipe.send((False, str(e)))
    finally:
        pipe.close()


def match_danmaku(danmaku: list[dict], selector: dict, timeout: float | None = None) -> list[dict]:
    if selector["mode"] == "literal":
        return [dm for dm in danmaku if dm.get("content", "") == selector["pattern"]]
    if not danmaku:
        return []
    budget = timeout if timeout is not None else getattr(config, "TARGET_REGEX_TIMEOUT", 30)
    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_regex_worker,
                         args=(selector["pattern"], [str(d.get("content", "")) for d in danmaku], writer),
                         daemon=True)
    started = False
    try:
        worker.start()
        started = True
        writer.close()
        if not reader.poll(max(0, budget)):
            raise ValueError("正则匹配超时，请简化表达式；匹配原文可加 --literal")
        try:
            success, result = reader.recv()
        except EOFError as e:
            raise ValueError("正则匹配进程异常退出") from e
        if not success:
            raise ValueError(f"正则匹配失败：{result}")
        return [danmaku[i] for i in result]
    finally:
        reader.close()
        writer.close()
        if started:
            worker.join(timeout=0.1)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=2)
            worker.close()
