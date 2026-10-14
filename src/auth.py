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
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256

from api_client import BiliAPIClient
import config
from config import COOKIE_PATH, COOKIES_DIR, QRCODE_GEN_URL, QRCODE_POLL_URL, NAV_URL

# 本地伪字段（保存在 cookie 文件里、**绝不注入 session 发给 B站**）：
#   _refresh_token 一次性刷新令牌
#   _refreshed_at  上次成功刷新的 unix 时间戳（看不到服务端到期时间，靠它做续期节流）
_LOCAL_FIELDS = ("_refresh_token", "_refreshed_at")


def _pop_local_fields(cookie_dict: dict) -> tuple:
    """弹出本地伪字段，返回 (refresh_token, 上次刷新时间戳)。

    所有「读文件 → 注入 session」的地方都必须先过这里，否则 _refreshed_at 会被当成
    cookie 发给 B站（回归用例锁定了这一点）。"""
    token = cookie_dict.pop("_refresh_token", "") or ""
    try:
        stamp = float(cookie_dict.pop("_refreshed_at", 0) or 0)
    except (TypeError, ValueError):
        stamp = 0.0
    return token, stamp


def _replace_cookie_jar(client, cookies: dict) -> None:
    """用去重后的 dict 清空并重建 jar，domain 统一为 .bilibili.com。

    刷新成功后空域旧 SESSDATA 会和 .bilibili.com 的新 SESSDATA 并存。
    本地伪字段（refresh_token 等）绝不写进 session。没有真实 jar 的替身客户端直接跳过。
    """
    session = getattr(client, "session", None)
    jar = getattr(session, "cookies", None) if session is not None else None
    if jar is None or not hasattr(jar, "clear") or not hasattr(jar, "set"):
        return
    clean = {}
    for name, value in cookies.items():
        if name in _LOCAL_FIELDS or value is None:
            continue
        clean[str(name)] = str(value)
    lock = getattr(client, "_lock", None)

    def _rebuild():
        jar.clear()
        for name, value in clean.items():
            jar.set(name, value, domain=".bilibili.com")

    if lock is not None:
        with lock:
            _rebuild()
    else:
        _rebuild()


def _clear_refresh_state(client) -> None:
    """重扫成功前丢掉旧令牌，避免响应没下发新令牌时把失效令牌写回。"""
    for attr in _LOCAL_FIELDS:
        if hasattr(client, attr):
            delattr(client, attr)


class RefreshResult(str):
    """cookie 刷新结果三态。

    仅 REFRESH_OK 为真（既有的 if _try_refresh_cookie(...) 用法不受影响），其余为假但可
    精确区分——网络/风控失败应保留旧登录态继续用，只有 token 失效才需要重扫。"""

    def __bool__(self) -> bool:
        return str(self) == "ok"


REFRESH_OK = RefreshResult("ok")          # 刷新成功（含落盘）
REFRESH_RETRY = RefreshResult("retry")    # 网络/风控冷却：保留现有登录态，下次再试
REFRESH_DEAD = RefreshResult("dead")      # refresh_token 失效（疑似被风控下线）：必须重扫
REFRESH_NONE = RefreshResult("none")      # 没有 refresh_token，未尝试

# 刷新接口返回这些 code 视为 refresh_token 已废（需重扫）；其余按可重试处理
_REFRESH_DEAD_CODES = (-101, -111, -400)


def refresh_result_text(result) -> str:
    """三态 → 面向用户的原因说明"""
    return {
        "retry": "刷新失败（网络异常或风控冷却中）",
        "dead": "刷新令牌已失效（疑似被风控下线）",
        "none": "缺少刷新令牌，无法自动续期",
    }.get(str(result), "刷新失败")

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


class LoginRequiredError(Exception):
    """需要人工扫码登录（二维码过期/扫码超时）：无人值守场景（批量分析、web job）
    捕获后应中止/终止而非继续——逐个空等 3 分钟扫码超时纯属浪费"""


# 跨进程 cookie 刷新互斥（refresh_token 一次性，两进程并发刷新同一账号会互相
# 烧掉对方的登录态）：POSIX 用 fcntl.flock，Windows 用 msvcrt.locking（都在持有进程
# 退出时由内核释放，不会死锁）。两者都不可用时才退化为无锁，并明确告警一次。
try:
    import fcntl
except ImportError:      # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:      # pragma: no cover - POSIX
    msvcrt = None
_LOCK_WARNED = False


def _refresh_lock(path: str | None):
    """cookie 刷新的跨进程文件锁（上下文管理器）：拿锁期间其它进程的刷新请求阻塞
    等待（内核保证持有进程退出自动释放，不会死锁）"""
    import contextlib

    lock_path = (path or COOKIE_PATH) + ".refresh.lock"

    @contextlib.contextmanager
    def _noop():
        yield

    if fcntl is None and msvcrt is None:
        global _LOCK_WARNED
        if not _LOCK_WARNED:
            _LOCK_WARNED = True
            print("[Auth] 警告: 无文件锁可用（fcntl/msvcrt 均缺失），"
                  "多进程同时刷新同一账号可能互相作废 refresh_token")
        return _noop()
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        # 用原始 fd：fcntl 与 msvcrt 都能直接作用其上
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        if msvcrt is not None and fcntl is None and os.fstat(fd).st_size == 0:
            os.write(fd, b"0")      # msvcrt 只能锁已存在的字节区间
    except OSError:
        return _noop()   # 锁文件都建不了（只读目录等）：退化为无锁，刷新本身仍会失败降级

    @contextlib.contextmanager
    def _locked():
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)      # 阻塞等锁：持有者退出内核自动释放
            else:                                   # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                while True:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)   # 约 10 次重试后抛错，继续等
                        break
                    except OSError:
                        time.sleep(1)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:                               # pragma: no cover - Windows
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(fd)

    return _locked()


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
    # 同时保存本地伪字段：刷新令牌 + 上次成功刷新时间（续期节流的依据）
    if hasattr(client, "_refresh_token"):
        cookie_dict["_refresh_token"] = client._refresh_token
    if hasattr(client, "_refreshed_at"):
        cookie_dict["_refreshed_at"] = str(client._refreshed_at)
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

def _due_for_refresh(path: str | None = None, now: float | None = None) -> bool:
    """距上次成功刷新是否已超过 COOKIE_REFRESH_INTERVAL_DAYS；从未刷新过 → 需要。

    客户端看不到 SESSDATA 的服务端到期时间，只能按"上次成功刷新的时间戳"记账，
    提前续期；窗口内幂等，不会每次启动都刷（刷新会旋转 token，过频会踩风控）。"""
    raw = (load_cookie(path) or {}).get("_refreshed_at")
    try:
        last = float(raw)
    except (TypeError, ValueError):
        return True
    days = getattr(config, "COOKIE_REFRESH_INTERVAL_DAYS", 7)
    return (now or time.time()) - last >= days * 86400


def save_cookie_if_current(client, path: str | None = None) -> bool:
    """运行结束回写登录态：仅当磁盘上的 refresh_token 仍是本客户端持有的那个才写。

    B站刷新会同时旋转 SESSDATA 与 refresh_token：若另一个进程（如 web.py）刚刷新过，
    直接回写会把新登录态覆盖回旧值，等于把账号写死。故先比对再写，并整段拿刷新锁
    与刷新动作串行化。返回是否真的写了。"""
    with _refresh_lock(path):
        disk = load_cookie(path) or {}
        current = getattr(client, "_refresh_token", None) or ""
        disk_token = disk.get("_refresh_token") or ""
        # 磁盘有令牌且与本进程不同（含本进程为空）就跳过，避免抹掉别的进程刚写的令牌
        if disk_token and disk_token != current:
            print("[Auth] 跳过回写: 磁盘上的登录态比本进程新（另一进程已刷新）")
            return False
        save_cookie(client, path)
        return True


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


def _try_refresh_cookie(client: BiliAPIClient, path: str | None = None) -> RefreshResult:
    """尝试刷新 cookie，返回三态（REFRESH_OK/RETRY/DEAD/NONE）；path 为小号 cookie 路径

    跨进程互斥（_refresh_lock）：refresh_token 一次性，run.py 与 web.py 两进程
    并发刷新同一账号时后者必失败且旧 token 作废；拿锁后先重读磁盘——等锁期间
    别的进程可能已刷新成功落盘，直接采用其结果，不再消耗自己手里的旧 token。
    """
    refresh_token = getattr(client, "_refresh_token", None)
    if not refresh_token:
        return REFRESH_NONE

    with _refresh_lock(path):
        # 拿到锁后重读磁盘：等锁期间其它进程已完成刷新并落盘（token 与内存值不同），
        # 直接采用新登录态（调用方随后会重新 verify_cookie 验证）
        disk = load_cookie(path)
        if disk:
            disk_rt, disk_stamp = _pop_local_fields(disk)   # 伪字段不进 session
            if disk_rt and disk_rt != refresh_token:
                client.update_cookies(disk)
                client._refresh_token = disk_rt
                if disk_stamp:
                    client._refreshed_at = disk_stamp
                print("[Auth] 检测到其它进程已完成刷新（文件锁串行化），采用其新登录态")
                return REFRESH_OK

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
                return REFRESH_RETRY
            match = re.search(r'<div id="1-name">(.+?)</div>', resp.text)
            if not match:
                print("[Auth] refresh_csrf 提取失败")
                return REFRESH_RETRY
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
                max_attempts=1,
            )
            if data.get("code") != 0:
                code = data.get("code")
                print(f"[Auth] 刷新失败: code={code} {data.get('message', '')}")
                # 鉴权类错误说明 refresh_token 已废（多为被风控下线），重扫才能恢复；
                # 其余（风控冷却/服务端异常）保留旧登录态下次再试
                return REFRESH_DEAD if code in _REFRESH_DEAD_CODES else REFRESH_RETRY

            # Step 4: 更新 cookies（Step 3 的新 cookie 已自动写入 session，直接读取）
            new_cookies = client.get_cookies_dict()
            new_refresh_token = (data.get("data") or {}).get("refresh_token", "")
            # 去重重建 jar。refresh_token 只留在客户端属性上，不进 session。
            _replace_cookie_jar(client, new_cookies)

            client._refresh_token = new_refresh_token or refresh_token
            client._refreshed_at = time.time()      # 续期节流依据（随文件落盘）
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
                max_attempts=1,
            )
            if confirm.get("code") != 0:
                print(f"[Auth] 警告: 刷新确认接口返回异常: code={confirm.get('code')} "
                      f"{confirm.get('message', '')}")

            print("[Auth] Cookie刷新成功!")
            return REFRESH_OK

        except Exception as e:
            print(f"[Auth] Cookie刷新异常: {e}")
            return REFRESH_RETRY


def login_by_qrcode() -> BiliAPIClient:
    """扫码登录，返回带登录态的客户端（支持自动刷新）"""
    client = BiliAPIClient()

    # 尝试加载已有cookie
    cookie_dict = load_cookie()
    if cookie_dict:
        refresh_token, stamp = _pop_local_fields(cookie_dict)
        client.update_cookies(cookie_dict)
        if refresh_token:
            client._refresh_token = refresh_token
        if stamp:
            client._refreshed_at = stamp

        if verify_cookie(client):
            # 主号同样到期前主动续期（失败不影响本次使用）
            if refresh_token and getattr(config, "COOKIE_PROACTIVE_REFRESH", True) and _due_for_refresh():
                result = _try_refresh_cookie(client)
                if result:
                    print("[Auth] 主号登录态已续期")
                elif verify_cookie(client):
                    print(f"[Auth] 主号{refresh_result_text(result)}，继续使用现有登录态")
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
            # 先清掉加载阶段留下的旧令牌，再按本次响应赋值
            _clear_refresh_state(client)
            refresh_token = (result.get("data") or {}).get("refresh_token", "")
            if refresh_token:
                client._refresh_token = refresh_token
                client._refreshed_at = time.time()
            else:
                # 没有 refresh_token 就等于"一次性 cookie"，到期只能重扫——必须让用户知道
                print("[Auth] 警告: 本次登录未获得 refresh_token（B站未下发），"
                      "该账号到期后无法自动续期，需要重新扫码")
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
            raise LoginRequiredError("二维码过期，需重新扫码登录")
        else:
            msg = (result.get("data") or {}).get("message", "未知状态")
            if last_status != code:
                print(f"\n[Auth] 登录状态: {msg} (code={code})")
                last_status = code

        time.sleep(2)

    raise LoginRequiredError("登录超时（3分钟未确认扫码）")


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
        refresh_token, stamp = _pop_local_fields(cookie_dict)   # 伪字段不进 session
        client = BiliAPIClient()
        client.update_cookies(cookie_dict)
        if refresh_token:
            client._refresh_token = refresh_token
        if stamp:
            client._refreshed_at = stamp
        elif refresh_token:
            # 老文件里没有时间戳：说明这份 token 从没被用来刷新过，首轮就续期一次
            print(f"[Auth] 小号 {name}: 无续期记录，本轮将主动续期")

        ok = verify_cookie(client)
        proactive = getattr(config, "COOKIE_PROACTIVE_REFRESH", True)
        if refresh_token and (not ok or (proactive and _due_for_refresh(path))):
            result = _try_refresh_cookie(client, path)
            if result:
                ok = verify_cookie(client)
                if ok:
                    days = getattr(config, "COOKIE_REFRESH_INTERVAL_DAYS", 7)
                    print(f"[Auth] 小号 {name}: 登录态已续期（{days} 天后再续）")
            elif not ok:
                print(f"[Auth] 小号 {name}: {refresh_result_text(result)}，"
                      f"需重扫（python login.py {name}），跳过")
                continue
            else:
                print(f"[Auth] 小号 {name}: {refresh_result_text(result)}，"
                      "继续使用现有登录态")
        if ok:
            clients.append((name, client))
        else:
            extra = "" if refresh_token else "（该账号无刷新令牌，无法自动续期）"
            print(f"[Auth] 小号 {name}: cookie 已失效{extra}，跳过"
                  f"（python login.py {name} 重扫）")
    return clients
