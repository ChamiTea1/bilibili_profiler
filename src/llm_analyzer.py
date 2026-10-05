"""
通用 LLM 分析器 — 兼容所有 OpenAI 协议的 API（仅保留重点深掘；全员粗筛已砍，省 token）

两套厂商配置（config.py），环境变量切换，零代码改动:
    export LLM_PROVIDER="mimo"      # 小米 MiMo（mimo-v2.5）；缺省 deepseek
"""
import hashlib
import json
import time

import openai
from openai import OpenAI
from config import (LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_MAX_TOKENS, LLM_DEEP_TOP_K,
                    LLM_FALLBACK, LLM_DEEP_TIMEOUT)
from storage import load_llm_cache, save_llm_cache

# 深掘结果缓存口径版本号（prompt/证据包结构变更时递增，使旧缓存自动失效）
_DEEP_CACHE_VERSION = "v2"
# 深掘的致命错误（鉴权/权限/参数/模型不存在）：重试与换厂商都无意义，直接跳过该用户
_FATAL_DEEP_ERRORS = (openai.AuthenticationError, openai.PermissionDeniedError,
                      openai.BadRequestError, openai.NotFoundError)


class LLMAnalyzer:
    """通用 LLM 分析器，兼容所有 OpenAI 协议 API"""

    def __init__(self):
        self.api_key = LLM_API_KEY
        self.base_url = LLM_BASE_URL
        self.model = LLM_MODEL
        self.max_tokens = LLM_MAX_TOKENS
        # OpenAI client 复用（一次创建全程复用，避免每用户重建 TCP/TLS 连接）；
        # 备用厂商第二套（LLM_FALLBACK，key 为空则不建），主用失败重试时切换
        self.clients = [OpenAI(api_key=self.api_key, base_url=self.base_url)]
        self.models = [self.model]
        if LLM_FALLBACK[0]:
            self.clients.append(OpenAI(api_key=LLM_FALLBACK[0], base_url=LLM_FALLBACK[1]))
            self.models.append(LLM_FALLBACK[2])

    def _build_evidence(self, p: dict, video_info: dict) -> dict:
        """深掘证据包（缓存 hash 与 prompt 构建共用同一份数据，保证 hash 能反映证据变化）"""
        dm = p.get("danmaku", {})
        cringe = p.get("cringe", {})
        return {
            "uid": p.get("uid"),
            "video_title": video_info.get("title", "未知视频"),  # 进 hash：改标题后缓存正确失效
            "name": p.get("name", "未知"),
            "level": p.get("level", 0),
            "sign": p.get("sign", ""),
            "vip": p.get("vip_status", 0) == 1,
            "follower": p.get("follower", 0),
            "archive_count": p.get("archive_count", 0),
            "tags": p.get("tags", []),
            "following_summary": p.get("following_summary", {}),
            "favorite_folders": p.get("favorite", {}).get("names", []),
            "danmaku_count": dm.get("count", 0),
            "danmaku_contents": dm.get("contents", [])[:50],  # 刷屏用户截断防超长
            "spam": {"level": dm.get("spam_level", "低"), "score": dm.get("spam_score", 0.0),
                     "reason": dm.get("spam_reason", "")},
            "cringe": {"count": cringe.get("count", 0),
                       "categories": cringe.get("categories", []),
                       "examples": cringe.get("examples", [])},
            "comments_in_video": [{"content": c.get("content", ""), "like": c.get("like", 0)}
                                  for c in p.get("comments", [])[:10]],
        }

    def _build_deep_prompt(self, p: dict, video_info: dict) -> str:
        """重点人员单人深掘 prompt：证据包（弹幕原文/评论/问题弹幕判定/刷屏分析/四维度数据）"""
        title = video_info.get("title", "未知视频")
        evidence = self._build_evidence(p, video_info)
        return f"""你是一位熟悉二次元游戏（二游）社区生态的资深网络行为分析师。请对以下这位B站用户做**单人深度行为画像**。
他/她曾在二游视频《{title}》中发送弹幕，是本视频中值得重点关注的人物（刷屏得分高或存在问题弹幕）。
评估时注意社区语境：发癫文学、角色应援、强度/剧情吐槽是二游社区的正常行为；重点关注拉踩引战、人身攻击、恶意带节奏等对社区氛围有实质破坏的行为。

## 证据包（JSON）
{json.dumps(evidence, ensure_ascii=False, indent=2)}

## 输出要求（严格按以下四节输出，结论必须引用证据包原文作为论据）

**行为定性**: （2-3句：这是个什么样的人，在本视频中扮演什么角色）
**动机分析**: （2-3句：他/她为什么发这些弹幕/评论，想获得什么）
**证据引用**: （列出2-4条最能支撑结论的弹幕或评论原文，并各配一句解读）
**风险等级**: （高/中/低 + 一句理由：对社区氛围的潜在影响）"""

    def _analyze_one_deep(self, p: dict, video_info: dict, ci: int = 0) -> str:
        """单人深掘调用，返回分析文本。ci 为厂商下标（0=主用，1=备用）"""
        response = self.clients[ci].chat.completions.create(
            model=self.models[ci],
            messages=[{"role": "user", "content": self._build_deep_prompt(p, video_info)}],
            max_tokens=self.max_tokens,
            temperature=1.0,
            top_p=0.95,
            stream=False,
            timeout=LLM_DEEP_TIMEOUT,
        )
        return response.choices[0].message.content or ""

    def analyze_deep(self, profiles: list[dict], video_info: dict,
                     top_k: int = LLM_DEEP_TOP_K) -> dict[int, str]:
        """重点深掘：兴趣分 top K 单人单调用。兴趣分 = (spam_score, 问题弹幕最高严重度, 弹幕数)。
        证据包未变时命中 llm_cache 直接复用（零 LLM 调用）"""
        def interest(p: dict):
            dm = p.get("danmaku", {})
            return (dm.get("spam_score", 0.0), p.get("cringe", {}).get("max_severity", 0),
                    dm.get("count", 0))

        targets = sorted(profiles, key=interest, reverse=True)[:top_k]
        results = {}
        for i, p in enumerate(targets, 1):
            uid = p.get("uid")
            if not uid:
                # 无 uid 的画像无法归属深掘结果（报告按 uid 取用），且会让 results[None] 互相覆盖
                print(f"  深掘 {i}/{len(targets)}: 跳过（画像缺 uid）")
                continue
            evidence = self._build_evidence(p, video_info)
            digest = hashlib.sha256(
                json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            cache_key = f"deep:{uid}:{_DEEP_CACHE_VERSION}:{LLM_MODEL}:{digest}"
            cached = load_llm_cache(cache_key)
            if cached:
                results[uid] = cached
                print(f"  深掘 {i}/{len(targets)}: UID:{uid} {p.get('name', '')}（缓存命中，跳过 LLM）")
                continue
            print(f"  深掘 {i}/{len(targets)}: UID:{uid} {p.get('name', '')}"
                  f"（LLM请求中，可能需要数十秒）...")
            try:
                text = self._analyze_one_deep(p, video_info)
            except _FATAL_DEEP_ERRORS as e:
                # 鉴权/参数/模型不存在等致命错误：换厂商与重试都无意义，直接跳过该用户
                print(f"  警告: UID:{uid} 深掘致命错误（{type(e).__name__}: {e}），跳过该用户")
                continue
            except Exception as e:
                # 超时等多为瞬态 API 波动（实测曾整批连续超时后自行恢复），
                # 退避后重试一次（有备用厂商则换备用），仍失败才降级跳过，不中断整体深掘
                ci = 1 if len(self.clients) > 1 else 0
                retry_tag = "，换备用厂商" if ci else ""
                print(f"  警告: UID:{uid} 深掘失败（{e}），20 秒后重试一次{retry_tag}...")
                time.sleep(20)
                try:
                    text = self._analyze_one_deep(p, video_info, ci)
                except _FATAL_DEEP_ERRORS as e2:
                    print(f"  警告: UID:{uid} 深掘致命错误（{type(e2).__name__}: {e2}），跳过该用户")
                    continue
                except Exception as e2:
                    print(f"  警告: UID:{uid} 重试仍失败（{e2}），跳过")
                    continue
            if text.strip():
                results[uid] = text
                save_llm_cache(cache_key, text)
            else:
                print(f"  警告: UID:{uid} 深掘响应为空，跳过")
        return results
