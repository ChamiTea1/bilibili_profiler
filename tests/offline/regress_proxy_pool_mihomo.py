"""可选真实 mihomo 回归：临时 provider、三条本地 HTTP 代理和 HTTPS 目标。

全程只连接 127.0.0.1，不使用真实订阅、Cookie 或 B站/LLM 服务。
由 regress_proxy_pool.py --mihomo 调用；未安装核心时明确跳过。
"""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import select
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import traceback
from unittest.mock import patch

import urllib3
import api_client as ac
import combo_pool as cp
import config
import proxy_core as pc
import proxy_pool as pp


class Server(ThreadingHTTPServer):
    daemon_threads = True


def serve(stack, handler, tls=None):
    server = Server(("127.0.0.1", 0), handler)
    if tls:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def stop():
        server.shutdown()
        server.server_close()
        thread.join(2)
    stack.callback(stop)
    return server.server_address[1]


def target_handler(marker, ip):
    class Target(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            body = (ip if self.path == "/ip" else json.dumps({"code": 0, "marker": marker})).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Target


def proxy_handler(target_port):
    class Proxy(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            with socket.create_connection(("127.0.0.1", target_port), timeout=3) as upstream:
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                channels = [self.connection, upstream]
                while True:
                    readable, _, _ = select.select(channels, [], [], 5)
                    if not readable:
                        return
                    for src in readable:
                        data = src.recv(65536)
                        if not data:
                            return
                        dst = upstream if src is self.connection else self.connection
                        dst.sendall(data)

        def log_message(self, *args):
            pass
    return Proxy


def run():
    binary = pc.ensure_binary(allow_download=False)
    if not binary or not shutil.which("openssl"):
        print("  （跳过真实核心回归：需要已安装 mihomo 和 openssl，不会自动联网下载）")
        return 0, 0
    passed = failed = 0
    def check(name, fn):
        nonlocal passed, failed
        try:
            fn()
            passed += 1
            print(f"  ✔ 真实核心：{name}")
        except Exception:
            failed += 1
            print(f"  ✘ 真实核心：{name}")
            traceback.print_exc()
    with tempfile.TemporaryDirectory(prefix="profiler_proxy_") as tmp, ExitStack() as stack:
        cert, key = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "1",
                        "-subj", "/CN=localhost"], check=True, capture_output=True)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        ports = {}
        for index, marker in enumerate(["A", "B", "C"], 1):
            target_port = serve(stack, target_handler(marker, f"203.0.113.{index}"), tls)
            ports[marker] = serve(stack, proxy_handler(target_port))
        provider = "proxies:\n" + "\n".join(
            f"  - name: 节点{marker}\n    type: http\n    server: 127.0.0.1\n    port: {port}"
            for marker, port in ports.items())
        class Provider(BaseHTTPRequestHandler):
            def do_GET(self):
                body = provider.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        provider_port = serve(stack, Provider)
        class LocalCore(pc.ProxyCore):
            def _gen_config(self):
                # 禁用默认外部 DNS/健康探测，订阅和所有隧道都只访问本地服务。
                return super()._gen_config().replace("enable: true", "enable: false")
        with patch.object(pc, "_RUNTIME_DIR", tmp):
            core = LocalCore([f"http://127.0.0.1:{provider_port}/provider"], account_names=["主号", "alt1"])
        stack.callback(core.stop)
        if not core.start(allow_download=False):
            print("  ✘ 真实核心：本地 provider 配置启动失败")
            return 0, 1
        bindings = core.account_routes()
        routes, clients = {}, {}
        for name, (ctl, url) in bindings.items():
            assert ctl.switch_node("节点A" if name == "主号" else "节点B")
            c = ac.BiliAPIClient()
            # 自签名证书仅用于这个本地测试；不改变生产客户端的验证设置。
            c.session.verify = False
            c._ensure_buvid3 = lambda: None
            c._ensure_bili_ticket = lambda: None
            stack.callback(c.session.close)
            r = pp.get_route((ctl.api_url, ctl.group), url, ctl, managed=True)
            r.node = ctl.current_node()
            r.ready = True
            routes[name], clients[name] = r, c
        pool = cp.ComboPool([(n, clients[n]) for n in clients], routes=routes)
        shards = pool.shard_pools()
        url = "https://127.0.0.1:1/marker"
        def marker(client):
            return client.get(url, timeout=3)["marker"]
        def assert_markers(a, b):
            assert marker(clients["主号"]) == a and marker(clients["alt1"]) == b
        with patch.dict("os.environ", {"PROXY_IP_CHECK_URL": ""}), patch.object(ac, "REQUEST_DELAY", (0, 0)), patch.object(config, "NAV_URL", url):
            with urllib3.warnings.catch_warnings():
                urllib3.warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                def independent():
                    with patch.dict("os.environ", {"PROXY_IP_CHECK_URL": "https://127.0.0.1:1/ip"}):
                        for name, r in routes.items():
                            r._probe_ip(clients[name], force=True)
                    assert routes["主号"].ip == "203.0.113.1"
                    assert routes["alt1"].ip == "203.0.113.2"
                    assert routes["主号"].gate is not routes["alt1"].gate
                    assert [s.get(url, timeout=3)["marker"] for s in shards] == ["A", "B"]
                check("两个账号端口实际走不同节点并测得不同出口", independent)
                def keepalive():
                    ctl = bindings["主号"][0]
                    assert ctl.switch_node("节点B")
                    assert marker(clients["主号"]) == "A", "测试前提：应复用旧 CONNECT 隧道"
                    clients["主号"].reset_connections()
                    assert marker(clients["主号"]) == "B"
                    assert ctl.switch_node("节点A")
                    clients["主号"].reset_connections()
                    assert marker(clients["主号"]) == "A"
                check("仅改选择组会复用旧隧道，清连接池后才换出口", keepalive)
                def rotate():
                    assert routes["主号"]._rotate()
                    assert routes["主号"].node == "节点C"
                    assert [s.get(url, timeout=3)["marker"] for s in shards] == ["C", "B"]
                check("出口层自动清旧隧道且另一账号保持原出口", rotate)
                def reload():
                    old_ports = {n: u for n, (_, u) in bindings.items()}
                    assert core.ensure_accounts(["主号", "alt1", "alt2"])
                    updated = core.account_routes()
                    assert all(updated[n][1] == u for n, u in old_ports.items())
                    assert updated["主号"][0].current_node() == "节点C"
                    assert updated["alt1"][0].current_node() == "节点B"
                    for r in routes.values():
                        r.sync_generation(1)
                        r.sync_generation(core.generation)
                    assert_markers("C", "B")
                check("热增账号成功且已有端口与节点选择保持", reload)
                def cleanup():
                    own = Path(core._runtime_dir)
                    assert own.exists()
                    core.stop()
                    assert not own.exists() and Path(tmp).exists()
                check("退出清理独立运行目录及 provider 缓存", cleanup)
    return passed, failed
