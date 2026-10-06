# -*- coding: utf-8 -*-
"""端口配置：--port 跨平台可用、优先级正确、非法值回退（5 项）

背景：文档此前只给 `PROFILER_PORT=9000 python web.py`（bash 前置换值），
Windows 的 cmd/PowerShell 不支持该写法，换端口说明在 Windows 上等于失效。
现在 `--port` 优先于环境变量，且两个入口都暴露该参数。

离线回归脚本：纯函数 + --help 子进程，**不联网、不占端口、不用 Cookie**。
从仓库任意目录均可运行：  python tests/offline/regress_port_config.py
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="port_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
import web

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

check("--port 优先于 PROFILER_PORT 环境变量",
      web._resolve_port(9000, {"PROFILER_PORT": "7000"}) == 9000,
      "(得到 %s)" % web._resolve_port(9000, {"PROFILER_PORT": "7000"}))
check("未传 --port 时环境变量生效",
      web._resolve_port(None, {"PROFILER_PORT": "7000"}) == 7000,
      "(得到 %s)" % web._resolve_port(None, {"PROFILER_PORT": "7000"}))
check("既无参数也无环境变量时默认 8001",
      web._resolve_port(None, {}) == 8001,
      "(得到 %s)" % web._resolve_port(None, {}))
check("非法/越界值回退 8001（abc / 0 / 99999）",
      web._resolve_port(None, {"PROFILER_PORT": "abc"}) == 8001
      and web._resolve_port(0, {}) == 8001 and web._resolve_port(99999, {}) == 8001,
      "(得到 %s/%s/%s)" % (web._resolve_port(None, {"PROFILER_PORT": "abc"}),
                           web._resolve_port(0, {}), web._resolve_port(99999, {})))

helps = {}
for entry in ("web.py", "run.py"):
    proc = subprocess.run([sys.executable, str(ROOT / entry), "--help"],
                          cwd=str(ROOT), capture_output=True, text=True)
    helps[entry] = (proc.returncode, (proc.stdout or "") + (proc.stderr or ""))
check("web.py 与 run.py 的 --help 都列出 --port",
      all(code == 0 and "--port" in out for code, out in helps.values()),
      "(%s)" % {k: ("rc=%s" % v[0], "--port" in v[1]) for k, v in helps.items()})

print("")
print("==== 端口配置: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
