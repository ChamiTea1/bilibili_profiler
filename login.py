#!/usr/bin/env python3
"""
B站扫码登录工具（全自动：二维码生成后程序自动轮询登录状态，无需按 Enter 确认）

用法:
    python login.py          # 主号（data/cookie.json）
    python login.py alt1     # 小号 alt1（data/cookies/alt1.json，run.py 自动发现并分摊采集）

流程: 检查已有 Cookie → 有效则直接退出；失效/没有则生成二维码
（终端字符码 + data/qrcode.png 图片），APP 扫码并确认后程序自动落库 Cookie。
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

# 强制行缓冲：输出被重定向/管道时也能实时看到进度（默认块缓冲会长时间无输出）
sys.stdout.reconfigure(line_buffering=True)

from auth import (get_qrcode, poll_qrcode, save_cookie, verify_cookie, load_cookie,
                  account_cookie_path, generate_qrcode_image, COOKIE_PATH)
from api_client import BiliAPIClient

MAX_WAIT_SECONDS = 300   # 二维码轮询总时长上限（5 分钟，覆盖扫码+确认）


def main():
    # 位置参数即账号名：python login.py alt1 → data/cookies/alt1.json
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        cookie_path = account_cookie_path(name) if name else COOKIE_PATH
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)

    print("=" * 50)
    print(f"  B站扫码登录工具" + (f"（小号: {name}）" if name else "（主号）"))
    print("=" * 50)

    # 检查已有 cookie
    client = BiliAPIClient()
    cookie_dict = load_cookie(cookie_path)
    if cookie_dict:
        # _refresh_token 是本地保存的伪 cookie，需先弹出，避免注入 session 发给B站
        refresh_token = cookie_dict.pop("_refresh_token", None)
        client.update_cookies(cookie_dict)
        if refresh_token:
            client._refresh_token = refresh_token
        if verify_cookie(client):
            print("\n[✓] 已有有效Cookie，无需重新登录")
            print(f"    Cookie文件: {cookie_path}")
            return
        print("\n[!] 已有Cookie已过期，重新登录...")

    # 生成二维码（路径按账号名区分，主号保持 data/qrcode.png，小号 qrcode_{name}.png）
    print("\n[1] 正在获取登录二维码...")
    url, qrcode_key = get_qrcode()
    qr_name = f"qrcode_{name}.png" if name else "qrcode.png"
    qr_path = os.path.join(os.path.dirname(COOKIE_PATH), qr_name)
    generate_qrcode_image(url, qr_path)

    print("\n" + "=" * 50)
    print("  请使用B站APP扫描上方二维码（终端字符码或图片）")
    print("=" * 50)
    print(f"  二维码图片: {qr_path}")
    print(f"  备用链接: {url}")
    print("\n[2] 等待扫码确认（全自动轮询，无需任何按键操作）...")

    start_time = time.time()
    last_code = None
    while time.time() - start_time < MAX_WAIT_SECONDS:
        result = poll_qrcode(qrcode_key, client)
        # get() 降级返回 {"code": -1} 时 data 字段缺失或为 None，用 or {} 防御
        code = (result.get("data") or {}).get("code", -1)

        if code == 0:
            # 从扫码响应中提取 refresh_token（用于后续 cookie 自动刷新）
            refresh_token = (result.get("data") or {}).get("refresh_token", "")
            if refresh_token:
                client._refresh_token = refresh_token
            print("\n[✓] 登录成功!")
            save_cookie(client, cookie_path)
            print(f"\n  Cookie已保存到: {cookie_path}")
            print("  现在可以运行: python run.py BVxxxxxxxx")
            # 登录成功后顺手删除临时二维码图片（避免残留过期二维码造成混淆）
            try:
                os.remove(qr_path)
            except OSError:
                pass
            return
        if code == 86038:
            print("\n[✗] 二维码已过期，请重新运行本程序")
            return
        if code != last_code:
            if code == 86090:
                print("[!] 已扫码但未确认，请先在APP上点击确认")
            elif code == 86101:
                print("[!] 尚未扫码，请先扫码")
            else:
                print(f"[!] 登录状态 (code={code}): {(result.get('data') or {}).get('message', '未知错误')}")
            last_code = code
        time.sleep(3)

    print(f"\n[✗] 等待超时（{MAX_WAIT_SECONDS // 60}分钟），请重新运行本程序")


if __name__ == "__main__":
    main()
