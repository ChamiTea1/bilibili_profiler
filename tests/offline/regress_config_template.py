# -*- coding: utf-8 -*-
"""配置模板一致性：config.example.py 必须与 src/config.py 的常量名同步（3 项）

离线回归脚本：使用隔离临时库 + 假 HTTP/假 OpenAI，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_config_template.py

为什么需要它：src/config.py 被 .gitignore 排除，新克隆者只能从 config.example.py 复制；
一旦模板漏了某个常量（例如新增功能时只改了 src/config.py），全新克隆会在 import 阶段
直接报 ImportError（web.py 起不来），而本地因为有旧 config.py 完全无感。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

CONST_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)\s*=", re.M)
LEAK_RE = re.compile(r"sk-[A-Za-z0-9]{20,}|SESSDATA=[A-Za-z0-9%]|(?:token|sub)=[A-Za-z0-9]{16,}")

example = ROOT / "config.example.py"
real = ROOT / "src" / "config.py"
check("config.example.py 存在", example.is_file(), str(example.relative_to(ROOT)))

txt = example.read_text(encoding="utf-8") if example.is_file() else ""
leak = LEAK_RE.search(txt)
check("模板不含密钥/订阅凭证", leak is None,
      ("疑似命中: %s" % leak.group(0)[:40]) if leak else "")

if real.is_file():
    ex = set(CONST_RE.findall(txt))
    rd = set(CONST_RE.findall(real.read_text(encoding="utf-8")))
    missing = sorted(rd - ex)     # 真实配置有、模板没有 → 全新克隆 ImportError
    extra = sorted(ex - rd)       # 模板有、真实配置没有 → 模板残留过时项
    check("模板未漏掉 src/config.py 的常量", not missing,
          ("缺失: %s" % "、".join(missing)) if missing else "")
    if extra:
        print("  （提示：模板中多出 %s，如属已删除常量请同步清理）" % "、".join(extra))
else:
    print("  （跳过常量比对：src/config.py 不存在，先 cp config.example.py src/config.py）")

print("")
print("==== 配置模板一致性: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
