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
    "tests/offline/regress_target_analysis.py", # 目标筛选、存档隔离、多分P原文和命令行接线
    "tests/offline/regress_target_lookup.py",   # 目标轻量取数、找到即停、原文留存与独立续页
    "tests/offline/regress_candidate_space.py", # 候选空间按需核验、缓存隔离、关联边界与路由防护
    "tests/offline/regress_uid_evidence.py",    # 多候选、证据迁移、存在性缓存与 UID 定向补采
    "tests/offline/regress_proxy_pool.py",      # 账号出口隔离、共享 IP 限速与故障降级
    "tests/offline/regress_proxy_startup.py",   # 核心启动实际超时、阶段状态与错误脱敏
    "tests/offline/regress_comprehensive_fixes.py", # 全库审查：存储隔离、事务、缓存、失败覆盖和界面
    "tests/offline/regress_core.py",            # 主回归 13 项
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
    "tests/offline/regress_review_fixes.py",    # 本轮 code review 修复定点验证 10 项
    "tests/offline/regress_blocklist.py",       # B站屏蔽列表导出（选人/误报/低置信度/路由/跨视频）17 项
    "tests/offline/regress_density_peaks.py",   # 弹幕高能点 AI 标注（峰值/缓存/降级）7 项
    "tests/offline/regress_long_image.py",      # 分享长图导出（payload/渲染护栏/路由/遮蔽）12 项
    "tests/offline/regress_web_stop.py",        # --stop 兜底与 pidfile 归属 8 项
    "tests/offline/regress_web_autostart.py",   # 目标路由就绪、旧服务识别及自动打开404防护
    "tests/offline/regress_env_proxy.py",       # 环境代理不得劫持本机调用 6 项
    "tests/offline/regress_llm_proxy.py",       # LLM 客户端不继承环境代理 6 项
    "tests/offline/regress_cli_switches.py",    # 命令行开关 --skip-deep 判定与接线 5 项
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
    proc = subprocess.run([PY, "-m", "pyflakes", "src", "web.py", "run.py",
                           "quick_test.py", "login.py", "tests"],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    # 模块缺失时 pyflakes 以非零码退出并在 stderr 报 "No module named"——
    # （PY 一定是可执行的解释器，FileNotFoundError 分支实际到不了）
    if proc.returncode != 0 and "No module named" in out:
        print("  未安装 pyflakes，跳过（pip install pyflakes）")
        return True
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
