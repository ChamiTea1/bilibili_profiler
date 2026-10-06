#!/usr/bin/env python3
"""
B站弹幕发送者用户画像分析系统 — 入口脚本

用法:
    python run.py BVxxxxxxxx [--force]
    python run.py BVxxxxxxxx --skip-collect   # 阶段5只读库内已采数据（不发采集请求）
    python run.py BVxxxxxxxx --port 9000      # 报告服务端口（默认 8001，跨平台）
    python run.py --batch videos.txt   # 批量模式：逐行读取BV号（忽略空行与 # 注释行）
    --force: 强制重新分析
"""
import sys
import os

if sys.version_info < (3, 12):
    sys.exit(f"本项目需要 Python 3.12 或更高版本（当前 {sys.version.split()[0]}）："
             "代码使用了 Python 3.12 的嵌套 f-string 语法。")

# 将 src 目录加入模块搜索路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

# 强制行缓冲：输出被重定向/管道时也能实时看到进度（默认块缓冲会长时间无输出）
sys.stdout.reconfigure(line_buffering=True)

from main import main

if __name__ == "__main__":
    main()
