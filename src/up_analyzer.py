"""
UP主分析器 — 被关注 UP 主的投稿词云懒加载

报告页悬停关注 chip 时经 /api/up/<uid>/wordcloud 按需采集（fetch_up_wordcloud
轻量路径：单请求 + immediate 免限速），结果按 `up:{uid}` 键入 llm_cache 缓存。
采集阶段只存关注名单本身，不再逐个深度分析被关注 UP 主（阶段5 提速）。
"""
import re
from config import USER_VIDEOS_LEGACY_URL
from api_client import BiliAPIClient


def _tokenize(text: str) -> list[str]:
    """简单中文分词：提取2字及以上连续中文字符"""
    words = re.findall(r'[\u4e00-\u9fff]{2,}', text)
    # 过滤常见停用词
    stop = {'一个','一下','可以','什么','没有','不是','这个','那个','还是','不要','已经','知道','觉得','真的','就是'}
    return [w for w in words if w not in stop]


def fetch_up_wordcloud(uid: int, client: BiliAPIClient) -> dict:
    """轻量词云采集（报告页悬停懒加载专用）：只发 1 个投稿列表请求，且走
    immediate 交互式免限速通道（悬停是单次交互，不属批量采集）——
    对比旧的名片+投稿双请求路径，出词延迟从「2 次限速+2 次网络」降到 1 次网络。
    UP 主昵称从投稿列表响应顺带取（vlist[0].author），名片接口不再调。
    返回 {"name": str, "word_freq": {词: 次}}；失败返回空词频。"""
    result = {"name": "", "word_freq": {}}
    try:
        data = client.get(USER_VIDEOS_LEGACY_URL, params={
            "mid": uid, "ps": 50, "pn": 1,
            "order": "pubdate", "order_avoided": "true",
        }, immediate=True)
        if data.get("code") == 0:
            vlist = data["data"]["list"]["vlist"]
            if vlist:
                result["name"] = vlist[0].get("author", "")
            wf: dict[str, int] = {}
            for v in vlist:
                for w in _tokenize(v.get("title", "")):
                    wf[w] = wf.get(w, 0) + 1
            result["word_freq"] = wf
    except Exception as e:
        print(f"  [UP] 警告: 词云轻量采集异常（UID:{uid}）: {e}")
    return result
