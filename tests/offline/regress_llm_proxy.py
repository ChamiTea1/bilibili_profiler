# -*- coding: utf-8 -*-
"""LLM 客户端不得继承 shell 里的环境代理（5 项）

真实故障：终端被代理客户端写入 all_proxy=socks://127.0.0.1:7892（以及 http(s)_proxy），
httpx 默认 trust_env=True 会读它，而没装 socks 支持时直接抛
ValueError: Unknown scheme for proxy URL —— 所有 LLM 调用 0.0 秒秒失败，
问题弹幕/问题评论判定与 AI 深掘整段崩掉（api_client 那条链路却正常，因为它本来就
trust_env=False，所以现象很迷惑）。

本用例用 127.0.0.1 上的假 OpenAI 兼容服务 + 指向死端口的代理环境验证：
裸 SDK 客户端必失败（测试前提），而项目的 llm_client.make_client 必须成功。

离线回归脚本：只用回环地址，**不联网、不消耗任何 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_llm_proxy.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# 先布置"有毒"代理环境，再导入被测模块（复刻用户现场）
os.environ.update({
    "http_proxy": "http://127.0.0.1:9/", "HTTP_PROXY": "http://127.0.0.1:9/",
    "https_proxy": "http://127.0.0.1:9/", "HTTPS_PROXY": "http://127.0.0.1:9/",
    "all_proxy": "socks://127.0.0.1:9/", "ALL_PROXY": "socks://127.0.0.1:9/",
    "no_proxy": "127.*,localhost", "NO_PROXY": "127.*,localhost",
})

from openai import OpenAI

import config as C
from llm_client import make_client

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

RESP = {"id": "chatcmpl-test", "object": "chat.completion", "created": 1, "model": "fake",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

class _H(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps(RESP).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d/v1" % port

def _ask(client):
    resp = client.chat.completions.create(
        model="fake", timeout=10, max_tokens=8,
        messages=[{"role": "user", "content": "hi"}])
    return resp.choices[0].message.content

# 1) 前提：这套环境下裸 SDK 客户端确实会被代理搞挂（否则本用例无意义）
bare_err = None
try:
    _ask(OpenAI(api_key="x", base_url=BASE, max_retries=0))
except Exception as e:
    bare_err = "%s: %s" % (type(e).__name__, str(e)[:60])
check("前提校验：裸 OpenAI 客户端在有毒代理环境下失败（本地假服务被代理劫持）",
      bare_err is not None, "(%s)" % (bare_err or "竟然成功了"))

# 2) 配置默认不继承环境代理
check("config.LLM_TRUST_ENV_PROXY 默认为 False", C.LLM_TRUST_ENV_PROXY is False,
      "(=%r)" % (C.LLM_TRUST_ENV_PROXY,))

# 3) make_client 必须绕开环境代理，直连本地假服务
got, err = None, None
try:
    got = _ask(make_client("x", BASE, max_retries=0))
except Exception as e:
    err = "%s: %s" % (type(e).__name__, str(e)[:80])
check("make_client 构造的客户端在有毒代理环境下仍能调通（判定/深掘链路）",
      got == "OK", "(返回 %r, 异常 %s)" % (got, err))

# 4) 底层 httpx 客户端显式声明了 trust_env（结构断言，SDK 版本不同则跳过）
inner = getattr(make_client("x", BASE), "_client", None)
trust = getattr(inner, "trust_env", None)
check("make_client 的底层 httpx 客户端 trust_env=False", trust is False, "(trust_env=%r)" % (trust,))

# 5) 判定批次的 thinking 参数路径同样可用（真实构造方式：extra_body 透传）
import cringe_detector as cd
extra = cd._batch_thinking_extra("https://api.deepseek.com", "deepseek-v4-flash")
try:
    resp = make_client("x", BASE, max_retries=0).chat.completions.create(
        model="fake", timeout=10, max_tokens=8, temperature=0.3,
        messages=[{"role": "user", "content": "hi"}], **extra)
    ok5 = resp.choices[0].message.content == "OK"
except Exception as e:
    ok5 = False
    print("    （异常 %s: %s）" % (type(e).__name__, str(e)[:60]))
check("带判定批次 thinking 参数调用同样成功（extra_body 透传不影响连接）", ok5,
      "(extra=%s)" % json.dumps(extra, ensure_ascii=False))

# 6) 守护：离线测试靠替换 llm_client.OpenAI 注入假客户端，这条通道必须始终有效
#    （曾因客户端构造从模块级 OpenAI 改为 make_client 而失效，导致测试真连厂商网络）
from unittest.mock import patch
import llm_client as lc

class _Fake:
    def __init__(self, **kw):
        self.kw = kw

with patch.object(lc, "OpenAI", _Fake):
    injected = lc.make_client("k", "http://127.0.0.1:1/v1")
check("替换 llm_client.OpenAI 能生效（离线假客户端注入通道畅通）",
      isinstance(injected, _Fake) and "http_client" in injected.kw,
      "(类型 %s)" % type(injected).__name__)

srv.shutdown()
print("")
print("==== LLM 客户端不继承环境代理: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
