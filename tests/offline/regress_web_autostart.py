#!/usr/bin/env python3
"""报告打开回归：模拟旧服务与 404，不起进程、不联网、不读取 Cookie。"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import web_autostart as launch

TARGET = "a" * 24
passed = failed = 0


def case(name):
    def run(fn):
        global passed, failed
        try:
            fn()
            passed += 1
            print(f"  ✔ {name}")
        except Exception as exc:
            failed += 1
            print(f"  ✘ {name}：{type(exc).__name__}: {exc}")
        return fn
    return run


class Response(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status


@contextlib.contextmanager
def context(**overrides):
    with tempfile.TemporaryDirectory(prefix="bili_web_start_") as folder:
        output = io.StringIO()
        settings = {"WEB_AUTOSTART": True, "DATA_DIR": folder, **overrides}
        with patch.dict(os.environ, {"PROFILER_PORT": "9000"}), patch.multiple(launch, **settings), \
             patch.object(launch.subprocess, "Popen") as popen, patch.object(launch.webbrowser, "open", return_value=True) as browser, \
             patch.object(launch.time, "sleep"), patch.object(launch, "_service_is_current", return_value=True), contextlib.redirect_stdout(output):
            yield output, popen, browser, folder


@case("读取本机JSON并保留HTTP状态")
def good_json():
    response = Response(json.dumps({"rows": [], "total": 0}).encode())
    with patch.object(launch._LOCAL_OPENER, "open", return_value=response) as get:
        assert launch._probe_json("http://127.0.0.1:9000/test") == (200, {"rows": [], "total": 0})
    assert response.closed and get.call_args.kwargs["timeout"] == 1


@case("404错误体用于识别缺失存档，响应句柄关闭")
def error_json():
    body = io.BytesIO(json.dumps({"error": "未知目标分析"}).encode())
    error = urllib.error.HTTPError("http://127.0.0.1/test", 404, "未找到", {}, body)
    with patch.object(launch._LOCAL_OPENER, "open", side_effect=error):
        assert launch._probe_json("http://127.0.0.1/test") == (404, {"error": "未知目标分析"})
    assert body.closed


@case("HTML、坏UTF8与非对象JSON不能伪装成报告接口")
def bad_payload():
    for data in (b"<html>404</html>", b"\xff", b"[]"):
        with patch.object(launch._LOCAL_OPENER, "open", return_value=Response(data)):
            assert launch._probe_json("http://127.0.0.1/test") == (200, None)


@case("目标接口200且存档可读才就绪，零命中报告也能打开")
def target_ready():
    with patch.object(launch, "_probe_json", return_value=(200, {"rows": [], "total": 0})) as probe:
        assert launch._server_state(9000, "BVTEST", TARGET) == "ready"
    assert probe.call_args.args[0] == f"http://127.0.0.1:9000/api/target/{TARGET}/danmaku?page=1"
    with patch.object(launch, "_probe_json", return_value=(200, {"rows": "伪响应", "total": 1})):
        assert launch._server_state(9000, "BVTEST", TARGET) == "occupied"


@case("旧服务视频接口可用但目标路由404，不打开错误页或重复起进程")
def outdated():
    def old_server(url):
        return (200, {"rows": [], "total": 0}) if "/api/video/" in url else (404, None)
    with context(), patch.object(launch, "_probe_json", side_effect=old_server):
        assert launch._server_state(9000, "BVTEST", TARGET) == "outdated"
    with context() as (output, popen, browser, _), patch.object(launch, "_probe_json", side_effect=old_server):
        launch.maybe_launch_web("BVTEST", TARGET)
    popen.assert_not_called()
    browser.assert_not_called()
    assert "旧版报告服务" in output.getvalue() and "无需重新分析" in output.getvalue()
    assert "--stop --port 9000" in output.getvalue()


@case("新服务缺失目标存档时提示目录问题，不自动打开404")
def missing():
    with context() as (output, popen, browser, _), patch.object(launch, "_probe_json", return_value=(404, {"error": "未知目标分析"})):
        launch.maybe_launch_web("BVTEST", TARGET)
    popen.assert_not_called()
    browser.assert_not_called()
    assert "同一项目目录" in output.getvalue()


@case("其它HTTP服务占端口不判就绪，也不再启动冲突的服务")
def occupied():
    with context() as (output, popen, browser, _), patch.object(launch, "_probe_json", return_value=(200, None)):
        launch.maybe_launch_web("BVTEST", TARGET)
    popen.assert_not_called()
    browser.assert_not_called()
    assert "端口的服务未能读取报告" in output.getvalue()


@case("常规模式仍支持视频不存在的JSON响应识别本项目服务")
def normal():
    with context() as (_, popen, browser, _), patch.object(launch, "_probe_json", return_value=(404, {"error": "未知视频"})):
        launch.maybe_launch_web("BVTEST")
    popen.assert_not_called()
    browser.assert_called_once_with("http://127.0.0.1:9000/video/BVTEST")


@case("已有新版服务直接打开目标报告，不启动第二个服务")
def reuse():
    with context() as (_, popen, browser, _), patch.object(launch, "_probe_json", return_value=(200, {"rows": [], "total": 0})) as probe:
        launch.maybe_launch_web("BVTEST", TARGET)
    popen.assert_not_called()
    browser.assert_called_once_with(f"http://127.0.0.1:9000/target/{TARGET}")
    assert all("/api/target/" in call.args[0] for call in probe.call_args_list)


@case("无服务时显式传入端口并等待目标接口可读，父进程关闭日志")
def start():
    with context() as (_, popen, browser, folder), patch.object(launch, "_server_state", side_effect=["down", "ready", "ready", "ready"]):
        launch.maybe_launch_web("BVTEST", TARGET)
        assert Path(folder, "web.log").exists()
    assert popen.call_args.args[0][-2:] == ["--port", "9000"]
    assert popen.call_args.kwargs["stdout"].closed
    browser.assert_called_once_with(f"http://127.0.0.1:9000/target/{TARGET}")


@case("启动超时不打开浏览器，保存可手动访问的链接")
def startup_timeout():
    with context() as (output, popen, browser, _), patch.object(launch, "_server_state", return_value="down"):
        launch.maybe_launch_web("BVTEST", TARGET)
    assert popen.call_count == 1
    browser.assert_not_called()
    assert "启动超时" in output.getvalue() and f"/target/{TARGET}" in output.getvalue()


@case("进程启动异常只打印提示，不影响分析结果且关闭日志句柄")
def start_failure():
    with context() as (output, popen, browser, _), patch.object(launch, "_server_state", return_value="down"):
        popen.side_effect = OSError("模拟启动失败")
        launch.maybe_launch_web("BVTEST", TARGET)
    assert popen.call_args.kwargs["stdout"].closed
    browser.assert_not_called()
    assert "自动启动失败" in output.getvalue()


@case("关闭自动启动仅打印目标链接，不探测服务或启动浏览器")
def disabled():
    with context(WEB_AUTOSTART=False) as (output, popen, browser, _), patch.object(launch, "_server_state") as probe:
        launch.maybe_launch_web("BVTEST", TARGET)
    probe.assert_not_called()
    popen.assert_not_called()
    browser.assert_not_called()
    assert f"/target/{TARGET}" in output.getvalue()


@case("非法端口在本机探测前拒绝，避免启动到默认端口后打开错误地址")
def invalid_port():
    for port in ("0", "65536", "abc"):
        with context() as (output, popen, browser, _), patch.dict(os.environ, {"PROFILER_PORT": port}), patch.object(launch, "_server_state") as probe:
            launch.maybe_launch_web("BVTEST", TARGET)
        probe.assert_not_called()
        popen.assert_not_called()
        browser.assert_not_called()
        assert "PROFILER_PORT" in output.getvalue()


@case("常规接口可用但源码指纹旧时提示重启，不打开旧报告")
def stale_source():
    with context() as (output, popen, browser, _), patch.object(launch, "_server_state", return_value="ready"), patch.object(launch, "_service_is_current", return_value=False):
        launch.maybe_launch_web("BVTEST")
    browser.assert_not_called()
    popen.assert_not_called()
    assert "旧版报告服务" in output.getvalue() and "无需重新分析" in output.getvalue()


@case("源码指纹接口缺失或不匹配不能视为新版，匹配才可复用")
def source_match():
    revision = launch.source_revision()
    for response in [(404, None), (200, {"source_revision": "old"}), (200, {"rows": []})]:
        with patch.object(launch, "_probe_json", return_value=response):
            assert not launch._service_is_current(9000)
    with patch.object(launch, "_probe_json", return_value=(200, {"source_revision": revision})):
        assert launch._service_is_current(9000)


print(f"==== Web自动打开: {passed} 项通过, {failed} 项失败 ====")
raise SystemExit(int(failed > 0))
