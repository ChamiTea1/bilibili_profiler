"""内置核心启动回归：假时钟/假进程/假控制器，验证超时和脱敏，不联网、不读 Cookie。"""
from contextlib import contextmanager, redirect_stdout
import io
import itertools
import json
from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import requests
import clash_ctl as cc
import config
import proxy_core as pc

CASES = []


def case(name):
    def register(fn):
        CASES.append((name, fn))
        return fn
    return register


def response(body, code=200):
    r = requests.Response()
    r.status_code = code
    r._content = json.dumps(body).encode()
    return r


def nodes_body(group="账号组", nodes=None, delay=100):
    nodes = ["节点甲"] if nodes is None else nodes
    proxies = {group: {"type": "Selector", "all": nodes + ["DIRECT", "COMPATIBLE"], "now": "节点甲"}}
    proxies.update({n: {"history": [{"delay": delay}]} for n in nodes})
    return {"proxies": proxies}


@contextmanager
def controller():
    cc._STATES.clear()
    ctl = cc.ClashCtl("http://127.0.0.1:19090", "测试密钥", "账号组")
    yield ctl
    cc._STATES.clear()


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, duration):
        assert 0 < duration <= 1
        self.now += duration


class Process:
    def __init__(self, logs="", exit_code=None):
        self.stdout = io.StringIO(logs)
        self.returncode = exit_code
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


@contextmanager
def startup(logs="", exit_code=None):
    clock, proc, output = Clock(), Process(logs, exit_code), io.StringIO()
    ports = itertools.count(18080)
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(pc, "_RUNTIME_DIR", tmp), \
            patch.object(pc, "_free_port", side_effect=lambda: next(ports)), \
            patch.object(pc, "ensure_binary", return_value="测试核心"), \
            patch.object(pc.ProxyCore, "_pdeathsig_kwargs", return_value={}), \
            patch.object(pc.subprocess, "Popen", return_value=proc), \
            patch.object(pc.atexit, "register"), \
            patch.object(pc.time, "monotonic", side_effect=lambda: clock.now), \
            patch.object(pc.time, "sleep", side_effect=clock.sleep), \
            patch.object(config, "PROXY_CORE_STARTUP_TIMEOUT", 4, create=True), \
            patch.object(config, "PROXY_CORE_STARTUP_LOG_INTERVAL", 1, create=True), \
            redirect_stdout(output):
        core = pc.ProxyCore(["https://测试订阅.invalid/subscribe?token=不可输出的订阅令牌"])
        try:
            yield core, clock, proc, output
        finally:
            core.stop()


@case("一次请求完成就绪检查，复用过滤伪节点及健康检查口径")
def _():
    with controller() as ctl, patch.object(cc._SESSION, "get", return_value=response(nodes_body())) as get:
        status = ctl.startup_status(timeout=(0.2, 0.3))
        assert status == {"state": "ready", "nodes": ["节点甲"]}
        assert ctl._nodes == ["节点甲"] and get.call_count == 1
        assert get.call_args.kwargs["timeout"] == (0.2, 0.3)
        assert cc._SESSION.trust_env is False


@case("控制器连通与组未加载、空订阅、全死节点分别诊断")
def _():
    for body, state in [({"proxies": {}}, "missing_group"),
                        (nodes_body(nodes=[]), "empty"),
                        (nodes_body(delay=0), "all_dead")]:
        with controller() as ctl, patch.object(cc._SESSION, "get", return_value=response(body)):
            assert ctl.startup_status() == {"state": state, "nodes": []}


@case("认证失败和 HTTP 异常不当成订阅空节点")
def _():
    for code, state in [(401, "unauthorized"), (403, "unauthorized"), (500, "http_error")]:
        with controller() as ctl, patch.object(cc._SESSION, "get", return_value=response({}, code)):
            assert ctl.startup_status()["state"] == state


@case("控制器网络失败与坏 JSON 各自降级，异常原文不输出")
def _():
    for error, state in [(requests.Timeout("不可输出的令牌"), "unreachable"),
                         (requests.JSONDecodeError("不可输出的令牌", "bad", 0), "invalid_response")]:
        output = io.StringIO()
        with controller() as ctl, patch.object(cc._SESSION, "get", side_effect=error), redirect_stdout(output):
            assert ctl.startup_status()["state"] == state
        assert "不可输出的令牌" not in output.getvalue()


@case("坏结构不触发 AttributeError，账号节点组非列表也安全降级")
def _():
    for body, state in [([], "invalid_response"), ({"proxies": []}, "invalid_response"),
                        ({"proxies": {"账号组": {"all": 123}}}, "empty")]:
        with controller() as ctl, patch.object(cc._SESSION, "get", return_value=response(body)):
            assert ctl.startup_status()["state"] == state


@case("慢控制器消耗实际等待预算，不能再把每次请求超时叠加 60 轮")
def _():
    with startup() as (core, clock, proc, output):
        budgets = []
        def slow(_ctl, timeout):
            assert sum(timeout) <= 4 - clock.now
            budgets.append(timeout)
            clock.now += sum(timeout)
            return {"state": "unreachable", "nodes": []}
        with patch.object(cc.ClashCtl, "startup_status", slow):
            assert core.start(allow_download=False) is None
        assert clock.now == 4 and len(budgets) == 1 and proc.terminated
        assert not Path(core._runtime_dir).exists()
        assert "核心就绪超时（4s）" in output.getvalue()


@case("控制器可达但订阅空时输出等待进度和具体终态")
def _():
    with startup() as (core, clock, proc, output):
        with patch.object(cc.ClashCtl, "startup_status", return_value={"state": "empty", "nodes": []}):
            assert core.start(False) is None
        text = output.getvalue()
        assert clock.now == 4 and proc.terminated
        assert "等待中（1/4s）" in text and "订阅尚未加载有效节点" in text
        assert "不可输出的订阅令牌" not in text


@case("等待期间订阅恢复可正常就绪，已有核心幂等复用")
def _():
    with startup() as (core, clock, proc, output):
        with patch.object(cc.ClashCtl, "startup_status", side_effect=[
                {"state": "empty", "nodes": []}, {"state": "ready", "nodes": ["节点甲"]}]) as probe:
            ctl = core.start(False)
            assert ctl is not None and core.generation == 1 and clock.now == 1
            assert core.start(False).api_url == ctl.api_url and probe.call_count == 2
            assert not proc.terminated and "内置核心就绪" in output.getvalue()


@case("超过截止时刻的就绪响应不误报启动成功")
def _():
    with startup() as (core, clock, proc, _output):
        def late(*args, **kwargs):
            clock.now = 5
            return {"state": "ready", "nodes": ["节点甲"]}
        with patch.object(cc.ClashCtl, "startup_status", side_effect=late):
            assert core.start(False) is None and core.generation == 0 and proc.terminated


@case("本机控制器认证拒绝立即停止，不无意义等待完整超时")
def _():
    with startup() as (core, clock, proc, output):
        with patch.object(cc.ClashCtl, "startup_status", return_value={"state": "unauthorized", "nodes": []}):
            assert core.start(False) is None
        assert clock.now == 0 and proc.terminated and "拒绝认证" in output.getvalue()


@case("核心提前退出回收配置，并输出脱敏的端口冲突诊断")
def _():
    with startup("level=fatal msg=\"listen: address already in use token=秘密\"\n", 1) as (core, _clock, _proc, output):
        with patch.object(cc.ClashCtl, "startup_status") as probe:
            assert core.start(False) is None and not probe.called
        assert "本机监听端口被占用" in output.getvalue()
        assert "秘密" not in output.getvalue() and not Path(core._runtime_dir).exists()


@case("核心原文无论携带 URL、节点密码或配置密钥都不会出现在诊断")
def _():
    diagnostics = pc._CoreDiagnostics()
    lines = [
        'level=error msg="initial proxy provider sub0 error: HTTP status code: 403 token=订阅秘密"',
        'level=error msg="initial proxy provider sub0 error: parse error: yaml: password: 节点秘密"',
        'level=error msg="initial proxy provider sub1 error: Get https://测试.invalid?token=订阅秘密: no such host"',
        'level=error msg="provider sub0: x509: unknown authority; context deadline exceeded"',
        'level=fatal msg="secret: 控制器秘密"',
    ]
    for line in lines:
        diagnostics.feed(line)
    summary = diagnostics.summary()
    for secret in ("订阅秘密", "节点秘密", "控制器秘密", "https://"):
        assert secret not in summary
    for reason in ("HTTP 401/403", "格式不受支持", "DNS 解析失败", "TLS 证书校验失败", "超时", "其他错误"):
        assert reason in summary


@case("核心输出持续排空，仅保留有限类别，不保存原文和凭证")
def _():
    with startup() as (core, _clock, _proc, _output):
        stream = io.StringIO(('level=error msg="provider sub0: connection refused token=秘密"\n') * 1000)
        core._read_core_output(stream, core._diagnostics)
        assert stream.closed and "秘密" not in core._diagnostics.summary()
        assert len(core._diagnostics._reasons) == 2


@case("订阅非鉴权 HTTP 失败、连接中断和不支持的协议也给出具体类别")
def _():
    for line, reason in [
            ('provider sub0 error: HTTP status code: 404 token=秘密', "HTTP 404"),
            ('provider sub0 error: HTTP status code: 400 token=秘密', "HTTP 400"),
            ('provider sub0 error: unexpected status 502 token=秘密', "HTTP 502"),
            ('provider sub0 error: 403 Forbidden token=秘密', "HTTP 401/403"),
            ('provider sub0 error: network is unreachable token=秘密', "网络路由不可达"),
            ('provider sub0 error: permission denied token=秘密', "访问权限"),
            ('initial proxy provider sub0 error: file must have a `proxies` field token=秘密', "格式不受支持"),
            ('initial proxy provider sub0 error: provider need one proxy at least token=秘密', "内容为空"),
            ('provider sub0 error: unexpected EOF token=秘密', "远端连接中断"),
            ('provider sub0 error: unsupported proxy type 秘密', "不支持的节点协议")]:
        diagnostics = pc._CoreDiagnostics()
        diagnostics.feed(line)
        assert reason in diagnostics.summary() and "秘密" not in diagnostics.summary()
    with startup() as (core, _clock, _proc, output):
        core._diagnostics.feed('provider sub0 error: 400 Bad Request token=秘密')
        core._print_diagnostics()
        assert "先关闭后重试" in output.getvalue() and "秘密" not in output.getvalue()


@case("订阅默认发送 mihomo 客户端标识，自定义请求头不能注入 YAML 配置")
def _():
    with startup() as (core, _clock, _proc, _output):
        with patch.dict(pc.os.environ, {"SUB_USER_AGENT": ""}):
            assert 'User-Agent: ["clash.meta"]' in core._gen_config()
        ua = 'mihomo/1.19.30"\nexternal-controller: 0.0.0.0:9090'
        with patch.dict(pc.os.environ, {"SUB_USER_AGENT": ua}):
            text = core._gen_config()
            assert f"User-Agent: [{json.dumps(ua)}]" in text
            assert "\nexternal-controller: 0.0.0.0:9090" not in text


@case("Popen 发生在常驻线程，而不是发起启动的临时线程")
def _():
    import os
    import threading
    seen = {}

    class FakePopen:
        def __init__(self, *args, **kwargs):
            seen["name"] = threading.current_thread().name
            self.stdout = io.StringIO("")
            self.returncode = 1

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 1

        def kill(self):
            pass

    def launch():
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(pc, "_RUNTIME_DIR", tmp), \
                patch.object(pc, "ensure_binary", return_value=os.path.join(tmp, "mihomo")), \
                patch.object(pc.subprocess, "Popen", FakePopen), \
                patch.object(pc.atexit, "register"), \
                redirect_stdout(io.StringIO()):
            core = pc.ProxyCore(["https://测试订阅.invalid/subscribe"])
            core.start(allow_download=False)

    worker = threading.Thread(target=launch, name="临时启动线程")
    worker.start()
    worker.join(10)
    assert not worker.is_alive()
    assert seen.get("name") == pc._SPAWN_THREAD_NAME
    assert seen["name"] != "临时启动线程"


@case("已退出进程的运行目录会被清理，存活进程目录保留且不打印凭证")
def _():
    import os
    with tempfile.TemporaryDirectory() as tmp, patch.object(pc, "_RUNTIME_DIR", tmp):
        dead_pid = next(cand for cand in range(1_000_000, 1_002_000) if not pc._pid_alive(cand))
        dead = Path(tmp) / f"{dead_pid}-abcd1234"
        dead.mkdir()
        (dead / "config.yaml").write_text("订阅凭证不可输出", encoding="utf-8")
        live = Path(tmp) / f"{os.getpid()}-eeeeffff"
        live.mkdir()
        (live / "config.yaml").write_text("保留", encoding="utf-8")
        other = Path(tmp) / "notes"
        other.mkdir()
        output = io.StringIO()
        with redirect_stdout(output):
            pc._reap_stale_runtimes()
        text = output.getvalue()
        assert not dead.exists() and live.exists() and other.exists()
        assert "订阅凭证不可输出" not in text and "abcd1234" not in text


@case("有摘要则必须一致才执行，无摘要的手动二进制允许并警告")
def _():
    import hashlib
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "mihomo"
        binary.write_bytes(b"offline-core")
        output = io.StringIO()
        with redirect_stdout(output):
            assert pc._binary_digest_ok(str(binary))
        assert "警告" in output.getvalue()
        side = Path(str(binary) + ".sha256")
        side.write_text("00" * 32 + "\n", encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            assert pc._binary_digest_ok(str(binary)) is False
        assert "重新下载" in output.getvalue()
        side.write_text(hashlib.sha256(b"offline-core").hexdigest() + "\n", encoding="utf-8")
        assert pc._binary_digest_ok(str(binary)) is True


def main():
    passed = failed = 0
    for name, fn in CASES:
        try:
            fn()
            passed += 1
            print(f"  ✔ {name}")
        except Exception:
            failed += 1
            print(f"  ✘ {name}")
            traceback.print_exc()
    print(f"\n==== 核心启动回归: {passed} 项通过, {failed} 项失败 ====")
    return int(failed > 0)


if __name__ == "__main__":
    sys.exit(main())
