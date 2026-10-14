"""
分析完毕自动启动 Web 报告（run.py 使用；quick_test.py 打开独立采样报告）

spec C：分析结束后探测本地服务→没有则分离启动 web.py→浏览器打开报告页；
任何一步失败都只打印 URL 提示手动访问，绝不影响主流程退出码。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from config import WEB_AUTOSTART, DATA_DIR
from report_service import source_revision

# 本机探测必须绕开环境代理：代理客户端常把 http_proxy 写进 shell，而 Python 的
# no_proxy 不支持 127.* 这类通配（只做后缀匹配），否则本地探测会被送进代理、
# 拿到 502 而误判「服务没起来」。ProxyHandler({}) 显式声明无代理。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# bvid 合法性校验（拼 URL 前防御）
_BVID_RE = re.compile(r"^BV[A-Za-z0-9]+$")


def _probe_json(url: str) -> tuple[int | None, dict | None]:
    """仅探测本机报告接口；保留 HTTP 状态，避免把 HTML 404 当作就绪。"""
    try:
        with _LOCAL_OPENER.open(url, timeout=1) as resp:
            status, payload = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        with e:
            status, payload = e.code, e.read()
    except Exception:
        return None, None
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeError):
        return status, None
    return status, data if isinstance(data, dict) else None


def _server_state(port: int, bvid: str, target_id: str | None) -> str:
    """目标模式须确认目标接口及存档可读，常规视频接口不能证明新功能已加载。"""
    base = f"http://127.0.0.1:{port}"
    video_probe = f"{base}/api/video/{urllib.parse.quote(bvid)}/danmaku?page=1&page_size=50"
    if target_id is None:
        status, data = _probe_json(video_probe)
        if data is not None and ("rows" in data or "error" in data):
            return "ready"
        return "down" if status is None else "occupied"
    status, data = _probe_json(f"{base}/api/target/{target_id}/danmaku?page=1")
    if status == 200 and data is not None and isinstance(data.get("rows"), list) and "total" in data:
        return "ready"
    if status == 404 and data is not None and data.get("error") == "未知目标分析":
        return "missing"
    if status == 404 and data is None:
        _, legacy = _probe_json(video_probe)
        if legacy is not None and ("rows" in legacy or "error" in legacy):
            return "outdated"
    return "down" if status is None else "occupied"


def maybe_launch_web(bvid: str, target_id: str | None = None):
    """分析结束后自动起 web.py 并用浏览器打开报告页。

    WEB_AUTOSTART=False 时只打印手动提示；目标报告须确认当前服务已加载目标路由。
    """
    try:
        port = int(os.environ.get("PROFILER_PORT", "8001"))
    except ValueError:
        # 自动启动是锦上添花：环境变量写错也不能影响分析结果与退出码
        print(f"  [Web] PROFILER_PORT 不是合法端口（{os.environ.get('PROFILER_PORT')!r}），"
              f"跳过自动启动，请手动运行 python web.py")
        return
    if not 1 <= port <= 65535:
        print("  [Web] PROFILER_PORT 超出有效端口范围，跳过自动启动，请手动运行 python web.py")
        return
    if not _BVID_RE.fullmatch(bvid or ""):
        print(f"  [Web] bvid 不合法（{bvid!r}），跳过自动启动，请手动运行 python web.py")
        return
    safe_bvid = urllib.parse.quote(bvid)
    url = f"http://127.0.0.1:{port}/video/{safe_bvid}"
    if target_id is not None:
        if not re.fullmatch(r"[0-9a-f]{24}", target_id):
            print("  [Web] 目标分析编号不合法，跳过自动启动")
            return
        url = f"http://127.0.0.1:{port}/target/{target_id}"
    if not WEB_AUTOSTART:
        print(f"  运行 python web.py 后访问报告页: {url}")
        return


    def _alive() -> bool:
        return _server_state(port, bvid, target_id) == "ready"

    try:
        state = _server_state(port, bvid, target_id)
        if state == "ready" and not _service_is_current(port):
            state = "outdated"
        if state == "outdated":
            print(f"  [Web] {port} 端口运行的是旧版报告服务，尚未加载当前报告代码；数据已保存，无需重新分析")
            print(f"  请依次运行 python web.py --stop --port {port} 和 python web.py --port {port}，再访问: {url}")
            return
        if state == "missing":
            print(f"  [Web] 当前服务未找到这份目标存档，请确认服务来自同一项目目录并重启；报告链接: {url}")
            return
        if state == "occupied":
            print(f"  [Web] {port} 端口的服务未能读取报告，请检查该端口服务与 data/web.log；报告链接: {url}")
            return
        if state == "down":
            # 分离启动：start_new_session 脱离本进程生命周期，日志重定向到 data/web.log
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_path = os.path.join(DATA_DIR, "web.log")
            log_f = open(log_path, "a", encoding="utf-8")
            try:
                subprocess.Popen([sys.executable, os.path.join(root, "web.py"), "--port", str(port)],
                                 cwd=root, stdout=log_f, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            except Exception:
                # Popen 抛异常时子进程未持有句柄，父进程必须自行关闭，防文件描述符泄漏
                log_f.close()
                raise
            finally:
                # Popen 成功后句柄已由子进程继承，父进程自身的引用同样要关闭——
                # 常驻父进程（run.py 批量模式）不关会逐次泄漏一个 fd
                log_f.close()
            # 等服务就绪（最多约 5 秒）
            for _ in range(10):
                if _alive():
                    break
                time.sleep(0.5)
            if not _alive():
                print(f"  [Web] web.py 启动超时，日志见 {log_path}")
        if _alive():
            # 终端下拉起 GTK 系浏览器会打印 "Not loading module atk-bridge" 警告
            # （at-spi 无障碍桥未加载），无害但脏输出；GTK 读到该环境变量即跳过模块
            os.environ.setdefault("NO_AT_BRIDGE", "1")
            # webbrowser.open 在无图形环境返回 False/抛异常，降级打印 URL
            if webbrowser.open(url):
                print(f"  报告页已在浏览器打开: {url}")
                print(f"  停止后台 web 服务: python web.py --stop --port {port}")
            else:
                print(f"  浏览器打开失败，请手动访问: {url}")
        else:
            print(f"  请手动运行 python web.py 后访问: {url}")
    except Exception as e:
        # 自动启动是锦上添花：任何异常都不影响分析结果与退出码
        print(f"  [Web] 自动启动失败（{e}），请手动运行 python web.py 后访问: {url}")


def _service_is_current(port: int) -> bool:
    """接口存在但代码未更新的旧服务也须提示重启，不直接打开旧报告。"""
    status, data = _probe_json(f"http://127.0.0.1:{port}/api/report-service")
    return status == 200 and isinstance(data, dict) and data.get("source_revision") == source_revision()
