#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键跑全部离线回归（秒级、无需网络/Cookie/LLM 额度），可选附带静态检查。

用法:
    python tests/run_all.py           # 只跑离线回归
    python tests/run_all.py --lint    # 附带 pyflakes 静态检查（需 pip install pyflakes）

退出码：全部通过为 0，任一脚本失败为非 0（可直接接 CI / pre-commit）。
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pick_python() -> str:
    """挑选能导入项目依赖的解释器：当前解释器 → 仓库 .venv。

    这样无论用系统 python 还是 venv python 调用本脚本，离线回归都能跑起来。"""
    probe = "import openai, flask, requests"

    def ok(exe: str) -> bool:
        try:
            return subprocess.run([exe, "-c", probe], capture_output=True).returncode == 0
        except OSError:
            return False

    if ok(sys.executable):
        return sys.executable
    for cand in (ROOT / ".venv/bin/python", ROOT / ".venv/Scripts/python.exe",
                 ROOT / "venv/bin/python"):
        if cand.exists() and ok(str(cand)):
            return str(cand)
    return sys.executable


PY = pick_python()
SCRIPTS = [
    "tests/offline/regress_core.py",            # 主回归 14 项
    "tests/offline/regress_comment_path.py",    # 评论采集路径 10 项
    "tests/offline/regress_judge_danmaku.py",   # 问题弹幕判定聚合 4 项
    "tests/offline/regress_judge_comment.py",   # 问题评论判定聚合 3 项
    "tests/offline/regress_config_template.py", # 配置模板与真实配置同步 3 项
    "tests/offline/regress_danmaku_stats_source.py", # 弹幕统计同源与报告补齐 5 项
    "tests/offline/regress_skip_collect.py",    # --skip-collect 零网络只读缓存 4 项
    "tests/offline/regress_density_multip.py",  # 弹幕密度时间轴分P口径 9 项
    "tests/offline/regress_multipart_pages.py", # 多分P 历史采集与样本标注 14 项
    "tests/offline/regress_repeat_events.py",   # 群体复读事件检测口径 17 项
    "tests/offline/regress_port_config.py",     # 端口配置（--port/环境变量/回退）5 项
]
SUMMARY_RE = re.compile(r"(\d+) 项通过,\s*(\d+) 项失败")


def ensure_local_config() -> None:
    """全新克隆没有 src/config.py（已被 .gitignore 排除）：从模板生成一份，零配置即可跑回归。"""
    real, example = ROOT / "src" / "config.py", ROOT / "config.example.py"
    if real.exists() or not example.exists():
        return
    real.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"（已从 config.example.py 生成 {real.relative_to(ROOT)}，仅本地使用、不入库）")


def run_offline() -> tuple[int, int, list[str]]:
    passed = failed = 0
    bad: list[str] = []
    for rel in SCRIPTS:
        print(f"\n=== {rel} ===")
        # 显式 UTF-8：用例打印 ✔/✘ 与中文，Windows 默认 cp1252 会 UnicodeEncodeError
        proc = subprocess.run([PY, str(ROOT / rel)], cwd=str(ROOT),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
        hits = SUMMARY_RE.findall(out)
        if hits:
            p, f = int(hits[-1][0]), int(hits[-1][1])
            passed += p
            failed += f
            print(f"  {p} 项通过, {f} 项失败")
        else:
            print("  未取到结果汇总，疑似脚本异常：")
        for line in out.splitlines():
            if line.strip().startswith("✘"):
                print("  " + line.strip())
        if proc.returncode != 0 or not hits or int(hits[-1][1]) > 0:
            bad.append(rel)
            if not hits:
                print("  " + "\n  ".join(out.strip().splitlines()[-8:]))
    return passed, failed, bad


def run_lint() -> bool:
    """pyflakes 静态检查：抓未定义名/语法类回归（比运行时踩 NameError 便宜得多）"""
    print("\n=== pyflakes 静态检查 ===")
    try:
        proc = subprocess.run([PY, "-m", "pyflakes", "src", "web.py", "run.py",
                               "quick_test.py", "login.py", "tests"],
                              cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("  未安装 pyflakes，跳过（pip install pyflakes）")
        return True
    out = (proc.stdout or "") + (proc.stderr or "")
    serious = [l for l in out.splitlines()
               if l.strip() and "imported but unused" not in l and "f-string is missing" not in l]
    if serious:
        print("  发现需处理项：")
        for line in serious:
            print("   " + line)
        return False
    print("  无未定义名/语法问题（仅剩 unused import、无占位符 f-string 等既有 cosmetic 项）")
    return True


def main() -> int:
    print(f"使用解释器: {PY}")
    ensure_local_config()
    passed, failed, bad = run_offline()
    lint_ok = run_lint() if "--lint" in sys.argv else True
    print("\n" + "=" * 52)
    print(f"离线回归合计: {passed} 项通过, {failed} 项失败")
    if bad:
        print("失败脚本: " + ", ".join(bad))
    print(f"静态检查: {'通过' if lint_ok else '有需处理项'}")
    print("=" * 52)
    return 0 if (not bad and failed == 0 and lint_ok) else 1


if __name__ == "__main__":
    sys.exit(main())