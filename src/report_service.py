"""报告服务源码指纹：区别运行中的旧进程与磁盘上的新代码，不读取配置或凭证。"""
import hashlib
from pathlib import Path


def source_revision() -> str:
    root = Path(__file__).resolve().parents[1]
    paths = [root / "web.py", *sorted((root / "src").glob("*.py"))]
    digest = hashlib.sha256()
    for path in paths:
        if path.name == "config.py":
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
