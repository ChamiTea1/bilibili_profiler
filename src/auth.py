"""
B站扫码登录 + Cookie自动刷新

核心改进（参考 bilibili-api）：
- 登录时保存 refresh_token，用于后续刷新
- cookie 过期时自动 RSA 刷新，无需重新扫码
"""
import json
import time
import os
import re
import tempfile
import uuid
import qrcode
from io import BytesIO
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256

from api_client import BiliAPIClient
from config import COOKIE_PATH, COOKIES_DIR, QRCODE_GEN_URL, QRCODE_POLL_URL, NAV_URL

# 账号名合法性（作为 cookies/<名字>.json 文件名，防路径注入）
_ACCOUNT_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def account_cookie_path(name: str) -> str:
    """小号 cookie 路径：data/cookies/<名字>.json；主号为 None 时用 COOKIE_PATH"""
    if not _ACCOUNT_NAME_RE.fullmatch(name or ""):
        raise ValueError(f"账号名不合法（仅限字母/数字/-/_，≤32字符）: {name!r}")
    os.makedirs(COOKIES_DIR, exist_ok=True)
    return os.path.join(COOKIES_DIR, f"{name}.json")

# B站 cookie 刷新用的 RSA 公钥
REFRESH_PUBKEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""


def generate_qrcode_image(url: str, filepath: str = "qrcode.png") -> str:
    qr_ascii = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=2)
    qr_ascii.add_data(url)
    qr_ascii.make(fit=True)
    qr_ascii.print_ascii(invert=True, tty=False)

    qr_img = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr_img.add_data(url)
    qr_img.make(fit=True)
    img = qr_img.make_image(fill_color="black", back_color="white")
    img.save(filepath)
    return filepath


def get_qrcode() -> tuple[str, str]:
    client = BiliAPIClient()
    data = client.get(QRCODE_GEN_URL)
    if data.get("code") != 0:
        raise Exception(f"获取二维码失败: {data}")
    return data["data"]["url"], data["data"]["qrcode_key"]


def poll_qrcode(qrcode_key: str, client: BiliAPIClient) -> dict:
    return client.get(QRCODE_POLL_URL, params={"qrcode_key": qrcode_key})


def save_cookie(client: BiliAPIClient, path: str | None = None) -> None:
    path = path or COOKIE_PATH
    cookie_dict = client.get_cookies_dict()
    # 同时保存 refresh_token
    if hasattr(client, "_refresh_token"):
        cookie_dict["_refresh_token"] = client._refresh_token
    # 原子写入：先写临时文件再 os.replace，避免中断留下截断的 JSON
    dir_ = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cookie_dict, f, ensure_ascii=False, indent=2)
        # cookie 即账号凭证，收紧权限为仅当前用户可读写
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f"[Auth] Cookie已保存到 {path}")


def load_cookie(path: str | None = None) -> dict | None:
    path = path or COOKIE_PATH
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        # cookie 文件损坏（如上次写入被中断或编码异常）或读取失败（权限/IO 错误），
        # 均降级为重新登录而非崩溃
        print(f"[Auth] 警告: Cookie文件损坏或读取失败（{path}），请重新登录")
        return None
    if not isinstance(data, dict):
        # 合法 JSON 但不是 cookie dict（如被误写成数组/字符串），同样按损坏处理
        print(f"[Auth] 警告: Cookie文件格式异常（{path}），请重新登录")
        return None
    return data


def verify_cookie(client: BiliAPIClient) -> bool:
    try:
        data = client.get(NAV_URL)
        if data.get("code") == 0 and (data.get("data") or {}).get("isLogin"):
            uname = data["data"].get("uname", "未知")
            print(f"[Auth] Cookie有效，当前用户: {uname}")
            return True
    except Exception as e:
        print(f"[Auth] Cookie验证异常: {e}")
    return False


def _get_correspond_path() -> str:
    """RSA-OAEP 加密生成 correspondPath"""
    key = RSA.importKey(REFRESH_PUBKEY)
    cipher = PKCS1_OAEP.new(key, SHA256)
    ts = round(time.time() * 1000)
    encrypted = cipher.encrypt(f"refresh_{ts}".encode())
    return encrypted.hex()


def _try_refresh_cookie(client: BiliAPIClient, path: str | None = None) -> bool:
    """尝试刷新 cookie，返回是否成功；path 为小号 cookie 路径（默认主号）"""
    refresh_token = getattr(client, "_refresh_token", None)
    if not refresh_token:
        return False

    try:
        print("[Auth] 尝试刷新Cookie...")

        # Step 1: 获取 correspondPath
        correspond_path = _get_correspond_path()

        # Step 2: 获取 refresh_csrf（该页面是 HTML，需要原始响应用正则提取；
        # get_raw 重试耗尽会 raise，这里接住降级为刷新失败）
        buvid = str(uuid.uuid1())
        try:
            resp = client.get_raw(
                f"https://www.bilibili.com/correspond/1/{correspond_path}",
                cookies={"buvid3": buvid},
            )
        except Exception as e:
            print(f"[Auth] 获取 refresh_csrf 请求失败: {e}")
            return False
        match = re.search(r'<div id="1-name">(.+?)</div>', resp.text)
        if not match:
            print("[Auth] refresh_csrf 提取失败")
            return False
        refresh_csrf = match.group(1)

        # Step 3: 执行刷新（requests.Session 会自动把响应 Set-Cookie 写入 cookie jar）
        data = client.post(
            "https://passport.bilibili.com/x/passport-login/web/cookie/refresh",
            data={
                "csrf": client.get_cookies_dict().get("bili_jct", ""),
                "refresh_csrf": refresh_csrf,
                "refresh_token": refresh_token,
                "source": "main_web",
            },
            cookies={"buvid3": str(uuid.uuid1())},
        )
        if data.get("code") != 0:
            print(f"[Auth] 刷新失败: {data.get('message', '')}")
            return False

        # Step 4: 更新 cookies（Step 3 的新 cookie 已自动写入 session，直接读取）
        new_cookies = client.get_cookies_dict()
        new_refresh_token = (data.get("data") or {}).get("refresh_token", "")

        client._refresh_token = new_refresh_token or refresh_token
        # 先落盘再 confirm：窗口期崩溃也不丢登录态。
        # save_cookie 单独 try：落盘失败时服务端轮换已完成、内存态有效，
        # 仍按刷新成功返回 True，只醒目告警（不兜成 False 让调用方误判重扫）
        try:
            save_cookie(client, path)
        except Exception as e:
            print(f"[Auth] !!!警告!!! Cookie刷新成功但落盘失败（{path or COOKIE_PATH}）: {e}；"
                  f"本次会话不受影响，重启后需重新扫码登录")

        # Step 5: 确认刷新（按B站协议用旧 refresh_token 确认，保持不变）
        confirm = client.post(
            "https://passport.bilibili.com/x/passport-login/web/confirm/refresh",
            data={
                "csrf": new_cookies.get("bili_jct", ""),
                "refresh_token": refresh_token,
            },
        )
        if confirm.get("code") != 0:
            print(f"[Auth] 警告: 刷新确认接口返回异常: code={confirm.get('code')} "
                  f"{confirm.get('message', '')}")

        print("[Auth] Cookie刷新成功!")
        return True

    except Exception as e:
        print(f"[Auth] Cookie刷新异常: {e}")
        return False


def login_by_qrcode() -> BiliAPIClient:
    """扫码登录，返回带登录态的客户端（支持自动刷新）"""
    client = BiliAPIClient()

    # 尝试加载已有cookie
    cookie_dict = load_cookie()
    if cookie_dict:
        refresh_token = cookie_dict.pop("_refresh_token", None)
        client.update_cookies(cookie_dict)
        if refresh_token:
            client._refresh_token = refresh_token

        if verify_cookie(client):
            return client

        # Cookie 验证失败但 refresh_token 可能仍有效：无条件先尝试一次刷新，
        # 成功后再重新验证；仍失败才走重新扫码
        if refresh_token and _try_refresh_cookie(client):
            if verify_cookie(client):
                return client

        print("[Auth] Cookie已过期，需要重新扫码登录...")

    # 扫码登录
    url, qrcode_key = get_qrcode()
    qr_path = os.path.join(os.path.dirname(COOKIE_PATH), "qrcode.png")
    generate_qrcode_image(url, qr_path)

    print("\n" + "=" * 50)
    print("请使用B站APP扫描二维码登录")
    print("=" * 50)

    max_wait = 180
    start_time = time.time()
    last_status = None

    while time.time() - start_time < max_wait:
        result = poll_qrcode(qrcode_key, client)
        # get() 降级返回 {"code": -1} 时 data 字段缺失或为 None，用 or {} 防御
        code = (result.get("data") or {}).get("code", -1)

        if code == 0:
            # 从扫码响应中提取 refresh_token
            refresh_token = (result.get("data") or {}).get("refresh_token", "")
            if refresh_token:
                client._refresh_token = refresh_token
            print("\n[Auth] 登录成功!")
            save_cookie(client)
            return client
        elif code == 86101:
            pass
        elif code == 86090:
            if last_status != 86090:
                print("\n[Auth] 已扫码，等待APP确认...")
                last_status = 86090
        elif code == 86038:
            print("\n[Auth] 二维码已过期，请重新运行程序")
            raise Exception("二维码过期")
        else:
            msg = (result.get("data") or {}).get("message", "未知状态")
            if last_status != code:
                print(f"\n[Auth] 登录状态: {msg} (code={code})")
                last_status = code

        time.sleep(2)

    raise Exception("登录超时（3分钟）")


def get_auth_client() -> BiliAPIClient:
    return login_by_qrcode()


def load_extra_clients() -> list[tuple[str, BiliAPIClient]]:
    """发现并校验小号池（data/cookies/*.json，由 python login.py <名字> 扫码写入）。

    每个小号独立的 BiliAPIClient（限速/冷却/自适应降速天然隔离）；失效小号尝试
    刷新一次，仍失败则跳过并提示重扫。返回 [(账号名, client), ...]，无小号为空列表。
    """
    import glob as _glob
    clients: list[tuple[str, BiliAPIClient]] = []
    for path in sorted(_glob.glob(os.path.join(COOKIES_DIR, "*.json"))):
        if not os.path.isfile(path):
            continue  # 同名目录等非文件项直接跳过，避免 open 时炸穿整个小号发现
        name = os.path.splitext(os.path.basename(path))[0]
        cookie_dict = load_cookie(path)
        if not cookie_dict or not cookie_dict.get("SESSDATA"):
            print(f"[Auth] 小号 {name}: cookie 缺失/无效（python login.py {name} 重扫），跳过")
            continue
        refresh_token = cookie_dict.pop("_refresh_token", None)
        client = BiliAPIClient()
        client.update_cookies(cookie_dict)
        if refresh_token:
            client._refresh_token = refresh_token
        if verify_cookie(client) or (_try_refresh_cookie(client, path) and verify_cookie(client)):
            clients.append((name, client))
        else:
            print(f"[Auth] 小号 {name}: cookie 已失效（python login.py {name} 重扫），跳过")
    return clients
