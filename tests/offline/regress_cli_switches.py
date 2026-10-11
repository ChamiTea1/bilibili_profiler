# -*- coding: utf-8 -*-
"""命令行开关：--skip-deep 的判定与接线（5 项）

--skip-deep 与 config.LLM_DEEP_ENABLED 取与：前者"这次不跑"，后者"永久关"。
本用例锁住真值表、参数确实透传到 run_analysis/run_batch，以及 --help 里能看到它
（避免再次出现"参数加了但没接线"）。

离线回归脚本：只做纯函数与 --help 子进程，**不联网、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_cli_switches.py
"""
import inspect
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="cli_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
import main

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

saved = main.LLM_DEEP_ENABLED
try:
    rows = []
    for global_on, skip in ((True, False), (True, True), (False, False), (False, True)):
        main.LLM_DEEP_ENABLED = global_on
        rows.append(main._deep_enabled(skip))
finally:
    main.LLM_DEEP_ENABLED = saved
check("_deep_enabled 真值表（配置开/关 × --skip-deep 用/不用 取与）",
      rows == [True, False, False, False], "(得到 %s)" % rows)

sig_analysis = inspect.signature(main.run_analysis)
sig_batch = inspect.signature(main.run_batch)
check("run_analysis 接受 skip_deep", "skip_deep" in sig_analysis.parameters,
      "(%s)" % list(sig_analysis.parameters)[:8])
check("run_batch 接受 skip_deep（批量模式同享）", "skip_deep" in sig_batch.parameters)
check("skip_deep 默认 False（不传即保持原行为）",
      sig_analysis.parameters["skip_deep"].default is False)

proc = subprocess.run([sys.executable, str(ROOT / "run.py"), "--help"],
                      cwd=str(ROOT), capture_output=True, text=True)
out = (proc.stdout or "") + (proc.stderr or "")
check("run.py --help 列出 --skip-deep 与 --skip-collect",
      proc.returncode == 0 and "--skip-deep" in out and "--skip-collect" in out,
      "(rc=%s)" % proc.returncode)

print("")
print("==== 命令行开关: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
