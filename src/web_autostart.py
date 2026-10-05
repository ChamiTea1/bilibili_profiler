"""
分析完毕自动启动 Web 报告（run.py 与 quick_test.py 共用入口）

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

# bvid 合法性校验（拼 URL 前防御）
_BVID_RE = re.compile(r"^BV[A-Za-z0-9]+$")


def maybe_launch_web(bvid: str):
    """分析结束后自动起 web.py 并用浏览器打开报告页。

    WEB_AUTOSTART=False 时只打印手动提示；已有服务在跑则跳过启动直接开页。
    """
    try:
        port = int(os.environ.get("PROFILER_PORT", "8000"))
    except ValueError:
        # 自动启动是锦上添花：环境变量写错也不能影响分析结果与退出码
        print(f"  [Web] PROFILER_PORT 不是合法端口（{os.environ.get('PROFILER_PORT')!r}），"
              f"跳过自动启动，请手动运行 python web.py")
        return
    if not _BVID_RE.fullmatch(bvid or ""):
        print(f"  [Web] bvid 不合法（{bvid!r}），跳过自动启动，请手动运行 python web.py")
        return
    safe_bvid = urllib.parse.quote(bvid)
    url = f"http://127.0.0.1:{port}/video/{safe_bvid}"
    if not WEB_AUTOSTART:
        print(f"  运行 python web.py 后访问报告页: {url}")
        return

    # 1 秒超时探测：无服务时快速失败，不阻塞主流程收尾。
    # 探测 web.py 专有的弹幕 JSON API 并校验响应特征（含 rows/error 键的 JSON），
    # 非"任意 HTTP 200 即判就绪"——避免端口被其他服务占用时误判
    def _alive() -> bool:
        probe = f"http://127.0.0.1:{port}/api/video/{safe_bvid}/danmaku?page=1&page_size=50"
        try:
            with urllib.request.urlopen(probe, timeout=1) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            # 4xx/5xx 同样说明有 HTTP 服务在应答，仍需校验是否为 web.py 的 JSON 错误体
            payload = e.read()
        except Exception:
            return False
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:
            return False
        return isinstance(data, dict) and ("rows" in data or "error" in data)

    try:
        if not _alive():
            # 分离启动：start_new_session 脱离本进程生命周期，日志重定向到 data/web.log
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_path = os.path.join(DATA_DIR, "web.log")
            log_f = open(log_path, "a", encoding="utf-8")
            try:
                subprocess.Popen([sys.executable, os.path.join(root, "web.py")],
                                 cwd=root, stdout=log_f, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            except Exception:
                # Popen 抛异常时子进程未持有句柄，父进程必须自行关闭，防文件描述符泄漏
                log_f.close()
                raise
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
                print("  停止后台 web 服务: python web.py --stop")
            else:
                print(f"  浏览器打开失败，请手动访问: {url}")
        else:
            print(f"  请手动运行 python web.py 后访问: {url}")
    except Exception as e:
        # 自动启动是锦上添花：任何异常都不影响分析结果与退出码
        print(f"  [Web] 自动启动失败（{e}），请手动运行 python web.py 后访问: {url}")
