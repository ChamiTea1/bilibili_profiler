# -*- coding: utf-8 -*-
"""验证主号/小号登录脚本统一使用 refresh_token 自动续期（不联网、不读取真实 Cookie）。"""
import contextlib
import io
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

import login

TMP = tempfile.mkdtemp(prefix="auth_refresh_")
COOKIE_PATH = os.path.join(TMP, "alt1.json")
login.account_cookie_path = lambda name: COOKIE_PATH
login.COOKIE_PATH = os.path.join(TMP, "cookie.json")

ok = fail = 0


def check(name, condition, extra=""):
    global ok, fail
    if condition:
        ok += 1
        print("  ✔ %s %s" % (name, extra))
    else:
        fail += 1
        print("  ✘ %s %s" % (name, extra))


class FakeClient:
    def __init__(self):
        self.cookies = {}

    def update_cookies(self, cookies):
        self.cookies.update(cookies)


print("=== 1. 小号 Cookie 失效时先自动刷新，不直接扫码 ===")
client = FakeClient()
verify_results = iter([False, True])
refresh_calls = []
qr_calls = []
login.BiliAPIClient = lambda: client
login.load_cookie = lambda path: {"SESSDATA": "masked", "_refresh_token": "refresh"}
login.verify_cookie = lambda c: next(verify_results)
login._try_refresh_cookie = lambda c, path: refresh_calls.append(path) or True
login.get_qrcode = lambda: qr_calls.append(True) or (_ for _ in ()).throw(AssertionError("不应扫码"))
old_argv = sys.argv
sys.argv = ["login.py", "alt1"]
try:
    with contextlib.redirect_stdout(io.StringIO()):
        login.main()
finally:
    sys.argv = old_argv
check("失效小号调用自动刷新", refresh_calls == [COOKIE_PATH])
check("刷新成功不获取二维码", not qr_calls)


print("=== 2. 没有刷新令牌或刷新失败时才进入扫码 ===")
client = FakeClient()
qr_calls = []
saved = []
login.BiliAPIClient = lambda: client
login.load_cookie = lambda path: {"SESSDATA": "masked"}
login.verify_cookie = lambda c: False
login._try_refresh_cookie = lambda c, path: (_ for _ in ()).throw(AssertionError("无令牌不应刷新"))
login.get_qrcode = lambda: qr_calls.append(True) or ("https://qrcode.invalid", "key")
login.generate_qrcode_image = lambda url, path: None
login.poll_qrcode = lambda key, c: {"data": {"code": 0, "refresh_token": "new"}}
login.save_cookie = lambda c, path: saved.append(path)
sys.argv = ["login.py", "alt1"]
try:
    with contextlib.redirect_stdout(io.StringIO()):
        login.main()
finally:
    sys.argv = old_argv
check("刷新令牌缺失时进入扫码", bool(qr_calls))
check("扫码成功保存对应小号文件", saved == [COOKIE_PATH])


print("=== 3. 重扫成功先清旧令牌，响应没下发新令牌就不写回 ===")
client = FakeClient()
client._refresh_token = "expired"
client._refreshed_at = 1.0
saved_clients = []
login.BiliAPIClient = lambda: client
login.load_cookie = lambda path: {
    "SESSDATA": "masked", "_refresh_token": "expired", "_refreshed_at": "1"}
login.verify_cookie = lambda c: False
login._try_refresh_cookie = lambda c, path: False
login.get_qrcode = lambda: ("https://qrcode.invalid", "key")
login.generate_qrcode_image = lambda url, path: None
login.poll_qrcode = lambda key, c: {"data": {"code": 0}}
login.save_cookie = lambda c, path: saved_clients.append((path, hasattr(c, "_refresh_token"), hasattr(c, "_refreshed_at")))
sys.argv = ["login.py", "alt1"]
try:
    with contextlib.redirect_stdout(io.StringIO()):
        login.main()
finally:
    sys.argv = old_argv
check("无新令牌时旧刷新令牌和时间戳都不再写回",
      saved_clients == [(COOKIE_PATH, False, False)])

client = FakeClient()
client._refresh_token = "expired"
client._refreshed_at = 1.0
login.BiliAPIClient = lambda: client
login.poll_qrcode = lambda key, c: {"data": {"code": 0, "refresh_token": "fresh"}}
captured = {}
login.save_cookie = lambda c, path: captured.update(
    token=getattr(c, "_refresh_token", None), stamp=getattr(c, "_refreshed_at", None))
sys.argv = ["login.py", "alt1"]
try:
    with contextlib.redirect_stdout(io.StringIO()):
        login.main()
finally:
    sys.argv = old_argv
check("响应有新令牌时按本次响应重新赋值",
      captured.get("token") == "fresh" and captured.get("stamp"))

print("\n回归结果: %d 项通过, %d 项失败" % (ok, fail))
sys.exit(1 if fail else 0)
