# -*- coding: utf-8 -*-
"""环境代理不得劫持本机调用（6 项）

真实故障：用户终端里被代理客户端写了 http_proxy/https_proxy/all_proxy=127.0.0.1:7892，
no_proxy 里写着 127.*。但 Python 的 no_proxy **只做后缀匹配**，127.* 不匹配 127.0.0.1，
于是 clash_ctl 对本地 mihomo 控制器（127.0.0.1:33487）的 requests 调用被送进代理，
拿到 502 → ProxyCore.start() 的就绪轮询 60 次全失败 → 启动阶段看起来像卡死。

本用例把环境设成同样的形态（代理指向死端口），再验证本地调用仍能打通。

离线回归脚本：只用 127.0.0.1 上的临时 HTTP 服务，**不联网、不消耗任何额度**。
从仓库任意目录均可运行：  python tests/offline/regress_env_proxy.py
"""
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# 先布置"有毒"的代理环境（指向必然连不上的死端口），再导入被测模块
os.environ.update({
    "http_proxy": "http://127.0.0.1:9/", "HTTP_PROXY": "http://127.0.0.1:9/",
    "https_proxy": "http://127.0.0.1:9/", "HTTPS_PROXY": "http://127.0.0.1:9/",
    "all_proxy": "socks://127.0.0.1:9/", "ALL_PROXY": "socks://127.0.0.1:9/",
    "no_proxy": "127.*,localhost", "NO_PROXY": "127.*,localhost",
})

import requests
import clash_ctl
import web_autostart

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

# ---------- 起一个"假 Clash 控制器" ----------
class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = {"proxies": {
            "profiler": {"type": "Selector", "all": ["节点A", "DIRECT"], "now": "节点A"},
            "节点A": {"type": "Shadowsocks", "history": [{"delay": 120}]},
            "DIRECT": {"type": "Direct"},
        }} if self.path.startswith("/proxies") else {"version": "v1.19.30"}
        body = json.dumps(payload).encode("utf-8")
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
api = "http://127.0.0.1:%d" % port

# ---------- 1) 确认测试前提：这套环境确实会劫持本地调用 ----------
check("前提校验：Python 不认 no_proxy 里的 127.*（本地调用本会被代理劫持）",
      urllib.request.proxy_bypass("127.0.0.1") is False)

# ---------- 2) clash_ctl 必须绕开环境代理 ----------
check("clash_ctl 的会话已关闭环境代理（trust_env=False）",
      clash_ctl._SESSION.trust_env is False)

ctl = clash_ctl.ClashCtl(api, "", "")
check("控制器 available() 在有毒代理环境下仍为 True（控制器可达）",
      ctl.available() is True, "(%s)" % api)

nodes = ctl.list_nodes()
check("节点列表能正常读出（_fetch_group 同样绕开了代理）",
      nodes == ["节点A"], "(nodes=%r)" % (nodes,))

check("refresh_nodes() 生效且过滤掉 DIRECT 伪节点",
      ctl.refresh_nodes() == ["节点A"], "(nodes=%r)" % (ctl._nodes,))

# ---------- 3) web_autostart 的本机探测同样绕开代理 ----------
probe = "%s/proxies" % api
try:
    with web_autostart._LOCAL_OPENER.open(probe, timeout=2) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    reached = "proxies" in data
except Exception as e:
    reached = False
    print("    （探测异常: %s）" % e)
check("web_autostart 的本机探测绕开环境代理（能拿到 200 与响应体）", reached)

srv.shutdown()
print("")
print("==== 环境代理不劫持本机调用: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
