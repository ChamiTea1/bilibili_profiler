# -*- coding: utf-8 -*-
"""LLM 客户端构造：统一决定是否继承环境代理。

为什么单独抽一个模块：openai SDK 底层用 httpx，而 httpx 默认 trust_env=True，会读
shell 里的 http_proxy/https_proxy/all_proxy。代理客户端（Clash 系、FastLinkCore 等）
常写入 all_proxy=socks://127.0.0.1:xxxx，未安装 socks 支持时 httpx 直接抛
ValueError: Unknown scheme for proxy URL —— 表现为「所有 LLM 调用 0.0 秒秒失败」，
判定与深掘整段崩掉，而 api_client 那条链路却是好的（它本来就 trust_env=False）。

策略（对齐项目既有原则「直连/代理由程序显式控制」）：默认不继承环境代理；
三家内置厂商（deepseek / bigmodel / mimo）均境内直连可达。需要经代理访问厂商时，
把 config.LLM_TRUST_ENV_PROXY 置 1（或设同名环境变量）。
"""
import httpx
from openai import OpenAI

from config import LLM_TRUST_ENV_PROXY


def make_client(api_key: str, base_url: str, **kwargs) -> OpenAI:
    """构造 OpenAI 兼容客户端；http_client 显式声明 trust_env（默认不继承环境代理）。

    kwargs 透传给 OpenAI（如 max_retries=0）。单次调用超时仍由调用方在
    chat.completions.create(timeout=...) 传入，不受此处的 httpx 客户端默认值影响。"""
    return OpenAI(api_key=api_key, base_url=base_url,
                  http_client=httpx.Client(trust_env=LLM_TRUST_ENV_PROXY), **kwargs)
