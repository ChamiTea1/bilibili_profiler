# 贡献指南

感谢你有兴趣改进本项目！提交 Issue 与 Pull Request 前，请先阅读以下约定。

## 开发环境

需要 **Python 3.12+**（项目使用 PEP 701 嵌套 f-string，3.10 / 3.11 无法运行）。

```bash
git clone https://github.com/ChamiTea1/bilibili_profiler.git && cd bilibili_profiler
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp config.example.py src/config.py                 # 仓库不含真实配置，只有模板
```

`src/config.py` 与 `data/` 均已在 `.gitignore` 中，**永远不要提交它们**（含 API Key 与账号 Cookie）。

## 三层验证（改动后按"从便宜到贵"跑）

```bash
# 1) 离线回归：88 项，秒级，不联网、不用 Cookie、不消耗 LLM 额度（隔离临时库）
python tests/run_all.py            # 主回归 + 评论路径 + 弹幕/评论判定聚合 + 配置模板一致性
python tests/run_all.py --lint     # 附带 pyflakes 静态检查
python tests/offline/regress_core.py   # 也可单独跑某一个

# 2) 真实端到端冒烟（需有效 Cookie 与网络，会真实请求 B站 API）
python quick_test.py [BV号] [--top N]
```

新增或修改**易错路径**（断点续采、翻页与降级、LLM 判定聚合、并发状态机等）时，请在
`tests/offline/` 补一条离线用例：这类缺陷往往只在真实运行中暴露，而离线用例能把它们锁死。

## 代码约定

- **中文优先**：注释、日志、文档一律中文。
- **扁平导入**：模块间用 `from config import ...`，不带 `src.` 前缀。
- **限速是硬约束**：所有 B站 API 调用必须走 `BiliAPIClient`（自动限速/重试/风控冷却），不要直接用 `requests`。
- **失败要降级而非中断**：单个用户/单条数据异常不得中断整体流水线（打警告、跳过、留检查点）。
- **不删除数据**：刷屏检测只标记 `spam_level`，任何情况下不删弹幕。
- **数值进 `src/config.py`**：不要在业务代码里散落魔数（翻页上限、阈值、超时等）。

## PR 检查清单

- [ ] `python tests/run_all.py --lint` 全绿
- [ ] 涉及真实链路的改动跑过一次 `quick_test.py`
- [ ] 未提交 `src/config.py`、`data/`、任何 Cookie / 密钥 / 订阅链接
- [ ] 新增配置常量时**同步更新 `config.example.py`**（模板漏项会让全新克隆直接 ImportError）
- [ ] 新增易错路径时有对应的 `tests/offline/` 用例

## 报告问题

- 功能缺陷 / 功能建议：走仓库的 Issue 模板。
- **安全漏洞：请勿公开提交 Issue**，按 `SECURITY.md` 走 GitHub 私密报告通道。

## 免责声明

本项目涉及对第三方用户的画像分析，请确保你的贡献不会被用于人肉搜索、网络暴力、商业推销或其他侵权用途；
详见 `README.md` 的免责声明与「局限性与已知边界」。
