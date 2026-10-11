# -*- coding: utf-8 -*-
"""--stop 的兜底与 pidfile 归属（7 项）

复现的故障：服务在跑、端口被占，但 pidfile 缺失（老实例退出时无条件删了它），
此时 --stop 只会说「未发现服务记录」，用户被卡住。现在 --stop 会按监听端口反查，
但仍须验明是本项目 web.py 才动手；同时 _clear_pid 只删属于自己的记录。

离线回归脚本：只测纯函数与文件读写，**不起服务、不占端口、不联网**。
从仓库任意目录均可运行：  python tests/offline/regress_web_stop.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

TMP = tempfile.mkdtemp(prefix="wstop_")
import web

web.DATA_DIR = TMP          # 隔离 pidfile 目录，绝不碰真实 data/

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

# ---------- 1) 监听进程解析（ss / lsof / netstat） ----------
ss_out = 'LISTEN 0 128 127.0.0.1:8000 0.0.0.0:* users:(("python",pid=1060664,fd=4))\n'
check("解析 ss 输出里的监听 PID", web._parse_listener_pids(ss_out, 8000) == [1060664],
      "(%s)" % web._parse_listener_pids(ss_out, 8000))

check("解析 lsof -ti 输出（每行一个 PID）", web._parse_listener_pids("4321\n8765\n", 8000) == [4321, 8765],
      "(%s)" % web._parse_listener_pids("4321\n8765\n", 8000))

netstat_out = ("  TCP    127.0.0.1:8001         0.0.0.0:0              LISTENING       99999\n"
               "  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       12345\n")
got = web._parse_listener_pids(netstat_out, 8000)
check("解析 netstat 输出且只取目标端口（8001 的行不误收）", got == [12345], "(%s)" % got)

# ---------- 2) _clear_pid 归属守卫 ----------
def write_pid(pid: int, port: int = 65533):
    with open(web._pid_file(port), "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "port": port, "script": os.path.realpath(web.__file__),
                   "started": "1", "cwd": os.getcwd()}, f)

P = 65533
write_pid(1, P)                       # 别人的记录（模拟老实例遗留/新实例记录）
web._clear_pid(P)
check("_clear_pid 不删非本进程的记录（防误删新实例 pidfile）",
      os.path.exists(web._pid_file(P)), web._pid_file(P))

web._clear_pid(P, force=True)
check("_clear_pid(force=True) 可清理已确认失效的记录",
      not os.path.exists(web._pid_file(P)))

write_pid(os.getpid(), P)             # 自己的记录
web._clear_pid(P)
check("_clear_pid 会删属于本进程的记录（atexit 正常清理路径）",
      not os.path.exists(web._pid_file(P)))

# ---------- 3) 身份校验：不认识的进程绝不动手 ----------
check("_pid_is_webpy 对「脚本不是 web.py 的进程」返回 False（本项目自己的测试进程）",
      web._pid_is_webpy(os.getpid()) is False, "(本进程 pid=%d)" % os.getpid())
check("_pid_is_webpy 对不存在的 PID 返回 False",
      web._pid_is_webpy(999_999_999) is False)

print("")
print("==== --stop 兜底与 pidfile 归属: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
