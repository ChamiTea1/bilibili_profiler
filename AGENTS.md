# AGENTS.md

## 项目概述

**B站弹幕发送者用户画像分析系统**：输入视频 BV 号，采集该视频的全部弹幕（实时弹幕池 + 历史快照），先做本地刷屏检测 + LLM 问题弹幕检测（八类：中二抒情/尬夸捧杀/引战阴阳/人身攻击/恶意剧透/广告引流/键政敏感/批评吐槽，二游社区语境口径，结果按 llm_cache 缓存）+ LLM 问题评论检测（同八类口径，回写 comments.problem 列，`cmt:{bvid}:*` 缓存），按兴趣分（中/高刷屏或问题弹幕命中）阈值制动态定员，再破解入选发送者的匿名 `mid_hash`（MITM 中间相遇 CRC32 反查 + 评论/充电名单/互动弹幕/视频元信息（UP主/联合投稿staff/简介@提及 desc_v2）明文 UID 交叉验证 + 全局映射库）；问题评论达阈值（严重度≥COMMENT_AUTHOR_MIN_SEVERITY 或 命中≥COMMENT_AUTHOR_MIN_HITS 条）的作者凭明文 UID 以合成键 `cmt:{uid}` 直引画像名单（senders 表 method="问题评论"、danmaku_count=0），对每个发送者做四维度深度画像（主页信息、互动足迹、社交关系、行为模式），可选调用 LLM 对兴趣分 top K 重点深掘生成 AI 画像（llm_cache 缓存，全员粗筛已砍），最终输出交互式 Web 报告（根目录 `web.py`，Flask 本地服务 127.0.0.1:8001，八标签页：概览/用户画像/弹幕浏览器/问题弹幕榜/争执焦点（问题回复按 parent_rpid 还原 A→B 攻击边、挑事者/被围攻者双榜附代表原文（挑事者成对展示被攻击者原评+攻击原文；被围攻者受害原评按条去重只展示一次、各攻击者攻击原文挂在其下；名额随攻击边数动态浮动：保底 ATTACK_FOCUS_TOP_N=5、每10边+1、封顶 ATTACK_FOCUS_MAX_N=20；顶部关系图画布（多圆簇布局：每个连通分量一个独立小圆——受害者居中、其攻击者环绕，1:1对成小簇，无边节点独立环簇，簇按半径网格排布；SVG 箭头线随攻击者分色、线粗映射攻击次数，头像节点大小映射攻击/被攻击次数（face_cache 后台补采），环上标签沿切线旋转、中心受害者标签上下交错，同一 uid 只画一个节点（既攻击又被攻击的链条节点标签显示 攻×n 被×n、同时有进出边），悬停节点高亮其全部关系边，点击节点定位到下方明细条目）））/问题评论榜（全部问题评论按 点赞+回复数×权重 热度降序、楼中楼附父评原文）/高回复评论（潜在争执热点：回复数达阈值的评论单独成页，完整回复树按 parent_rpid 嵌套缩进、默认折叠可展开，评论者直接显示用户名（comments.uname），楼主/UP主身份徽标，问题评论分色标注；悬停评论组静止600ms弹该组讨论主题词云（data-wc 服务端注入词频，复用 wc-popup））/低置信度（confidence="低" 的已解析发送者集中复核页：UID+解析方式+可能误识别徽标+mid_hash+弹幕内容样本，提示可经弹幕浏览器勾选强制画像；概览语句含低置信度人数），原"完整报告"标签页已移除、由 @media print 打印样式替代；概览页含操作条（返回首页/重新生成/删除/导出下载/B站屏蔽列表导出（操作条下拉面板：按 问题弹幕/高中风险刷屏/问题评论作者 三标准勾选 + 条数上限，生成播放器「弹幕屏蔽列表」可导入的用户屏蔽 JSON（type=2+filter=uid，/api/video/<bvid>/blocklist），误报标记口径同各榜单扣除、低置信度 UID 默认排除防误屏蔽、未解析发送者无 UID 可导出；首页另有跨视频合并屏蔽榜（/api/blocklist/cross：在 ≥N 个视频都命中标准者优先，排序以命中视频数为首位——B站用户屏蔽全站生效，惯犯屏蔽性价比最高））/隐藏信息开关——Cookie mask=1 全局生效，开启后昵称只留末字其余用颜文字（￣▽￣）代替、UID 除前三位替换为 *，服务端各数据装配收口点遮蔽，页缓存按开关分版本）、弹幕密度时间轴（按视频内时间分桶直方图；多分P视频的弹幕 `time` 是各分P内部时间、与 `videos.duration`（各P时长之和）不可混用，故按分P分别建桶 + 分P选择器（默认选中弹幕最多的那P），点击柱条以带 `p` 参数的链接跳转该分P对应时段；分P时长元信息缺失时降级单轴并在卡片内标注；画像样本/弹幕浏览器/互动时间线的 mm:ss 在多分P时标 P{n} 并按「先分P 再分P内时间」排序，旧画像由 web 渲染期按 (mid_hash,content,time) 回填分P）、群体复读事件区块（接龙/+1队列检测：同内容 60s 窗内 ≥5 人 ≥8 条；按「视频内时间」分P 开窗为主、兼容「发送时间」集中刷屏，事件行给出视频内时刻并可点跳转到该分P 该秒核验，主写法旁标「＋N 变体」、命中数达展示上限时明示只列前 N 起，附全池分布自检行）、解析质量区块（解析方式/置信度分布 + 碰撞风险人数）；问题弹幕/问题评论条目带「误报」人工纠偏按钮（false_positive 表持久化，弹幕按内容、评论按 rpid、刷屏判定按发送者 mid_hash（展示层降级为低风险），标记后不计入聚合与用户疑似分、可撤销，跨重跑保留，删除报告时清除）；首页含跨视频重叠用户面板（≥2 个已分析视频都出现过的发送者，视频条目可展开查看该用户在其中的弹幕/评论明细样本）；用户卡片含「其他视频足迹」区块（该用户在其他已分析视频中的弹幕与评论样本）、毕业院校徽标（school）与关注偏好 chips（采集阶段只存全部关注名单，被关注 UP 主的投稿词云在悬停 chip 时经 /api/up/<uid>/wordcloud 懒加载，`up:{uid}` 缓存），并可经高回复评论作者进入「用户互动时间线」页（/user/<uid>，该用户在全部已分析视频中的弹幕/评论按最近互动倒序）；标签页/弹幕... (line truncated to 2000 chars)

- **Python 3.12+**（使用 PEP 701 嵌套 f-string，不兼容 3.10/3.11）；纯 Python 项目，无构建系统（无 pyproject.toml / setup.py / package.json），依赖通过 `requirements.txt` 管理。
- 主要依赖：`requests`（HTTP）、`lxml`（弹幕 XML 解析）、`qrcode` + `pillow`（扫码登录）、`openai`（LLM 客户端）、`pycryptodome`、`flask`（Web 报告服务）。
- 所有代码注释、打印输出、文档均使用**中文**，回复和代码注释请沿用中文。

## 运行与安装

需要 Python 3.12+（`python --version` 自检）。

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 主流程（登录→弹幕→刷屏检测→问题弹幕检测→评论→兴趣分UID解析→用户采集→画像分析→LLM深掘→报告）
python run.py BV1vu4y1b7Y9                # 分析视频
python run.py BV1vu4y1b7Y9 --force        # 忽略缓存强制重新分析
python run.py BV1vu4y1b7Y9 --max-users 50 # 手动覆盖动态定员（默认阈值命中者全进，上限随发送者规模浮动：保底300/发送者数×5%/封顶1000）
python run.py BV1vu4y1b7Y9 --skip-collect # 跳过阶段5采集的网络请求：只用库内已采数据刷新报告（未采用户本轮不出画像，去掉参数重跑自动续采）
python run.py --batch videos.txt          # 批量分析（逐行读取BV号，忽略空行与 # 注释行）

# 辅助脚本
python login.py        # 扫码登录主号（全自动轮询：终端字符码+图片，APP确认后自动落库，无需按键）
python login.py alt1   # 扫码登录小号 alt1（存 data/cookies/alt1.json，run.py 阶段5自动发现轮转分摊采集）
python quick_test.py [BV号] [--top N]  # 快速分析：只分析刷屏得分最高的前 N 个发送者
python web.py       # 交互式 Web 报告（默认 127.0.0.1:8001）
python web.py --port 9000            # 换端口（跨平台推荐写法；优先级 --port > PROFILER_PORT > 8001）
python web.py --stop [--port 9000]   # 停止后台 web 服务并释放端口（换过端口要带同样的 --port）
                                     # pidfile: data/web_{端口}.pid，防 PID 复用误杀
```

注意：`run.py`/`quick_test.py` 分析完毕会自动启动 web.py 并打开报告页（`config.py` 中 `WEB_AUTOSTART=False` 关闭；批量模式不自动启动）。

首次运行会提示用 B站 APP 扫码登录；Cookie 自动保存到 `data/cookie.json` 并复用。

## 代码结构

入口 `run.py` 将 `src/` 加入 `sys.path` 后调用 `main.main()`。模块间以**扁平的非包方式导入**（`from config import ...`，不带 `src.` 前缀），修改时务必保持这一约定。

```
web.py               # 交互式 Web 报告服务（Flask，首页视频列表 + 五标签页报告 + 弹幕 JSON API + 手动分析/删除/重新生成 job API（job 原子注册互斥防并发重入）；非 GET 请求强制校验 Origin/Host 为本机回环否则 403（防 CSRF/DNS rebinding）；Cookie 失效后可经 /api/reload_client 热复位；CSS/JS 在 static/，只留路由与数据装配）
static/              # Web 报告静态资源（report.css/report.js/index.css/index.js + 本地化的 chart.umd.min.js/wordcloud2.min.js，Flask 默认伺服 /static/；数据经内联 window.__DATA__ 注入）
src/
├── main.py              # 主控流程：登录→弹幕(实时+历史)→刷屏检测→问题弹幕检测→评论→兴趣分UID解析（+问题评论作者直引 select_problem_comment_authors）→用户采集→画像分析→LLM深掘→报告
├── web_autostart.py     # 分析完毕自动启动 web.py 并打开报告页（maybe_launch_web，WEB_AUTOSTART 开关）
├── config.py            # 全部配置常量：API 端点、限速/重试、采集翻页上限、LLM 配置（含 API Key，已被 .gitignore 排除）
├── api_client.py        # BiliAPIClient：HTTP 封装（线程安全限速（区间内随机：基础0.8–1.6s/高风险2–4s）、自适应降速（触发风控×1.5、成功缓慢回落）、重试退避、-412及重签无效的-352/-403风控全局冷却（仅 WBI 端点走重签）、Cookie、WBI签名（密钥获取失败 60s 负缓存）、bili_ticket/buvid3（失败 300s 后可重试）；post() 与 get() 同风控语义；get(immediate=True) 为交互式单次请求免限速通道——仅报告页悬停词云用，风控冷却仍生效）
├── clash_ctl.py         # Clash/mihomo 控制器封装（列节点/切节点换出口 IP，跨地区跳跃轮换，失败静默降级；死节点名单：健康检查历史判死 + 代理故障即时上报 mark_dead，TTL 复活，全灭回退不过滤）
├── proxy_core.py        # 内置 mihomo 核心生命周期（SUB_URLS 多订阅→127.0.0.1 随机端口代理+控制器，零安装；自动下载锁定版本且经官方 SHA256 校验不匹配拒绝执行、github.com 直连优先；PDEATHSIG 防孤儿驻留，stop 时清理含凭证 config.yaml）
├── combo_pool.py        # 账号×IP 组合池（鸭子类型模拟 BiliAPIClient；风控换"新号+新IP"重试，冷却按截止时刻锁外等待（单账号池冷却缩至 SINGLE_ACCOUNT_RISK_COOLDOWN=120s），IP 池故障摘代理降级直连、每 PROXY_RETRY_AFTER=600s 重探恢复；注意内置核心为单 mixed-port 单 select 组，IP 维度全局单点：所有账号共享同一出口 IP；风控「整圈」账本按使用方（`pool_owner()`，web job 即 job_id）隔离，账号轮换与长冷却仍是全局状态）
├── auth.py              # 扫码登录、Cookie 保存/加载/校验/自动刷新、小号池发现（data/cookies/*.json → load_extra_clients，失效自动尝试刷新）
├── danmaku.py           # 实时弹幕 XML 解析，按 mid_hash 聚合发送者
├── danmaku_history.py   # 历史弹幕采集（逐日弹幕池快照，protobuf wire 手写解析+0x0A 特征校验防错误页误判；失败日记账 failed_dates 优先补采、截断写 truncated、done=1 后重跑滚动补采最近 3 天；每日快照为独立请求无游标链，组合池多号分片时按账号并发采多天，落库/检查点推进在主线程串行；多分P 视频按分P 各采一份（page 参数落库标记 + 检查点键 :pN 隔离，分P 1 沿用原键名故既有报告不重采））
├── comment.py           # 评论区采集（wbi/main 游标 + 子评论补采 + IP属地），建立 UID→CRC32 映射；页内各主楼的子评论补采互相独立，组合池多号分片时按账号并发补采（主评论游标翻页为链式依赖无法分片，楼中楼补采才是时间大头）；done=1 视频重跑时 refresh_comments 按时间序（mode=2）增量刷新新评论（撞整页已见即停，旧评论顺带刷新热度）；harvest_comment_uids 纯 UID 收割（翻评论区只取 uid 建 CRC32 映射、不落评论内容；正文未被截断且评论表非空时直接跳过，正文截断时接续其热度序游标续翻不重扫已采页；累计上限 UID_HARVEST_MAX_PAGES=500 页，断点续翻，done 后重跑转刷新模式，沉淀全局库）
├── uid_resolver.py      # mid_hash 破解：评论/充电名单/互动弹幕/视频元信息（main.build_video_meta_uid_map）/全局库交叉验证 + MITM 反查碰撞消歧
├── crc_rainbow.py       # MITM 中间相遇 CRC32 反查（10万条内存小表，覆盖全部 ≤10 位 UID；惰性建表加双检锁线程安全，预计算 adv5 表，约 49ms/hash）
├── spam_detector.py     # 刷屏检测：只标记风险等级（高/中/低），绝不删除弹幕数据；四规则组合计分（最高分+每多触发一条加成 SPAM_COMBO_BONUS）、高频爆发按滑动窗口口径、全池 P95 相对离群补强、群体复读事件检测（detect_repeat_events：同内容 60s 窗内 ≥5 人 ≥8 条的接龙/队列，全视频维度；**双时间轴**——视频内时间轴按分P 分别开窗（接龙发生在同一画面，观众可横跨数月，是主形态），发送时间轴抓真实时间短窗集中刷屏，两轴各自达标才写进结果；内容先按 `_content_key` 归一化合并写法变体——全半角/空白/首尾标点/连续重复字符（？？？→？、666→6、哈哈哈→哈，非 ASCII 字母才折叠以免 good→god），变体并入同一事件后人数取并集、条数求和，事件带 variants/variant_count 供 tooltip）、分布自检（P50/P90/P95 供阈值校准）
├── cringe_detector.py   # LLM 问题弹幕检测（八类判定+发送者聚合+llm_cache缓存，key 带口径版本号 v4，批次响应先解析校验成功才落缓存）+ 问题评论检测（同口径，按内容缓存判定、回映 rpid 回写 comments.problem，未配置 LLM_API_KEY 自动跳过；致命 4xx 直接上抛由 phase 层降级，瞬态错误同厂商短退避，整轮重试 LLM_RETRY_BUDGET_SECONDS=1800s 熔断）
├── user_collector.py    # 四维度用户数据采集（主页/动态/关注/收藏等；采集全程走账号×IP 组合池（combo_pool，鸭子类型透明接管：多号并行分片（每账号一子池、限速按号独立、线程↔分片绑定、吞吐≈账号数倍）、风控换号+切节点重试，冷却锁外等待，IP 池故障自动降级直连并定时重探恢复）；单用户日志行缓冲、完成时原子输出，多线程不交错）
├── profile_analyzer.py  # 规则式画像分析与标签生成
├── llm_analyzer.py      # LLMAnalyzer：重点深掘（兴趣分 top K 单人单调用+llm_cache缓存，OpenAI client 初始化时复用、单次调用超时 LLM_DEEP_TIMEOUT=120s；结果注入 profile 并由阶段7回写 users.profile_json（web 报告读库展示，不落库则报告页不可见）；全员粗筛已砍，未配置 Key 自动跳过）
├── up_analyzer.py       # UP 主词云懒加载（fetch_up_wordcloud：单请求+immediate 免限速轻量路径；采集阶段只存全部关注名单，被关注 UP 主的投稿词云在悬停 chip 时经 /api/up/<uid>/wordcloud 懒加载，`up:{uid}` 缓存；web 启动时后台热身鉴权威胁，前端视口内 chip 慢速队列预热+在途去重）
├── report.py            # 报告渲染函数库（用户卡片/问题弹幕榜/图表统计/基础CSS，被 web.py 复用）
├── exporter.py          # CSV/JSON 数据导出（report_{BV号}_{时间} 前缀，Web 报告页提供下载链接）
└── storage.py           # SQLite 持久化（data/profiler.db，get_db 统一 WAL + busy_timeout=10000 + synchronous=NORMAL），支撑断点续采与 LLM 结果缓存（llm_cache 表、danmaku 全量弹幕表（含 mode/color/pool/dmid/page 属性列）、comments 评论表（含 uname 昵称、parent_rpid 回复树、problem 问题标注、location IP属地）、false_positive 误报标记表（kind: dm=弹幕内容/cmt=评论rpid/spam=发送者mid_hash，展示层扣除聚合）、phase_state 阶段检查点表（弹幕历史 last_date/fetched_dates/failed_dates/truncated、评论游标，中断续采））
```

数据流：`run.py` → `main.run_analysis(bvid, force, max_users)`，全阶段断点续采（任意位置 Ctrl+C/崩溃后重跑可续）：弹幕逐日增量落库+phase_state 检查点（danmaku 表 last_date/fetched_dates/failed_dates/truncated/done，历史快照中断后逐日续采、失败日记账优先补采；多分P 视频逐分P 各一套检查点（分P 1 原键名、其余 :pN），各P 独立续采互不影响；月份/日期降序遍历、上限耗尽保留最新日期；done=1 后重跑仍滚动补采最近 HISTORY_RECENT_REFRESH_DAYS=3 天，dmid 幂等去重；仅时间窗完整无失败才写 done）、评论逐页落库+游标检查点（comments 表 mode/page/offset/done/truncated，wbi/legacy 双路径续页，模式切换页码归零、截断不写 done；done=1 后重跑按时间序增量刷新新评论，上限 COMMENT_REFRESH_MAX_PAGES=10 页）、LLM 判定批次级缓存（llm_cache 批次 key=稳定前缀`cringe:{bvid}:版本`/`cmt:{bvid}:版本`@batch:模型:内容指纹，先解析校验成功才落缓存、坏响应进重试链，已完成批次重跑零调用）、senders 逐条落库、users 每人落库；非 --force 重跑时各阶段按库内数据存在性独立跳过（完整判据：done=1 或本功能前的旧版完整落库），`--force` 清除该视频全部缓存与检查点强制重采（llm_cache 中仅清该视频的问题弹幕/问题评论判定缓存 `cringe:{bvid}:*` 与 `cmt:{bvid}:*`，深掘缓存 `deep:{uid}:*` 跨视频复用、保留）。全局映射库 `global_uid_map` 跨视频沉淀可靠 mid_hash→UID 映射（仅评论/充电/互动/元信息等明文来源可沉淀，纯 CRC32 破解结果不沉淀防误归因跨视频放大；冲突覆盖按来源优先级，明文>破解），解析率随使用次数累积提升。

## 开发约定

- **限速是硬约束**：B站 API 有风控，`config.py` 中 `REQUEST_DELAY` / `REQUEST_DELAY_LONG` 为区间值（基础 0.8–1.6s、高风险 2–4s，区间内随机取时长以消除固定节奏特征），触发风控（-412/HTTP412/重签无效的-352/-403）时自适应倍率 ×1.5 上调（上限 5.0，由 `ADAPTIVE_THROTTLE_*` 控制）、业务成功后缓慢回落，重试最多 3 次指数退避。新增 API 调用必须走 `BiliAPIClient`，不要绕过限速直接发请求。
- **失败要降级而非中断**：例如评论采集失败时回退为仅用 CRC32 破解；LLM 分析失败只打印警告。单个用户采集异常不得中断整体流水线。
- **不删除数据**：刷屏检测只标记 `spam_level`，不删除任何弹幕。
- 采集规模由 `config.py` 中的 `MAX_*` 常量控制（评论 100 页、子评论补采 25 页/条、动态定员上限 ANALYZE_USERS_FLOOR=300 / ANALYZE_USERS_RATIO=0.05 / MAX_ANALYZE_USERS_HARD_CAP=1000（保底/按比例上浮/绝对封顶）、深掘 LLM_DEEP_TOP_K=20、问题弹幕/问题评论判定并发上限 LLM_CONCURRENCY=16（实际路数=min(批次数,上限)，429 限速自动退避重试）、问题弹幕批大小 CRINGE_BATCH_SIZE=200、问题评论批次/上限 COMMENT_CRINGE_BATCH_SIZE=100 / COMMENT_CRINGE_MAX_ITEMS=2000、问题评论作者直引阈值 COMMENT_AUTHOR_MIN_SEVERITY=2 / COMMENT_AUTHOR_MIN_HITS=2、评论 UID 收割上限 UID_HARVEST_MAX_PAGES=500、问题评论榜 COMMENT_HEAT_REPLY_WEIGHT=10 / PROBLEM_COMMENT_TOP_N=30、争执焦点 ATTACK_FOCUS_TOP_N=5 / ATTACK_FOCUS_MAX_N=20（保底/封顶，按攻击边数浮动）、密度时间轴桶数 DENSITY_BUCKETS=60、跨视频面板 CROSS_VIDEO_MIN_VIDEOS=2 / CROSS_VIDEO_MAX_USERS=50、LLM 深掘超时 LLM_DEEP_TIMEOUT=120、LLM 判定整轮重试熔断 LLM_RETRY_BUDGET_SECONDS=1800 / 瞬态重试 LLM_TRANSIENT_RETRIES=2、历史弹幕滚动补采 HISTORY_RECENT_REFRESH_DAYS=3 / 分P历史采集 HISTORY_MULTIPAGE_ENABLED / HISTORY_MULTIPAGE_MAX_PAGES=4、代理恢复重探 PROXY_RETRY_AFTER=600、单账号池冷却 SINGLE_ACCOUNT_RISK_COOLDOWN=120、WBI 密钥负缓存 WBI_KEY_FAIL_TTL=60、buvid3/bili_ticket 重试 CRED_FAIL_TTL=300、回复树深度 REPLY_TREE_MAX_DEPTH=50、web job 淘汰 WEB_JOB_MAX_KEPT=100 、手动分析上限 ANALYZE_MAX_TARGETS=200、刷屏检测 SPAM_BURST_*/SPAM_VARIANT_*/SPAM_COMBO_BONUS/SPAM_RELATIVE_*/REPEAT_EVENT_* 窗口阈值与组合/相对离群/复读事件参数等），调优时改这里而不是散落在代码里的数字。
- 输出文件：`data/reports/report_{BV号}_{时间}.csv/.json`（CSV/JSON 导出，web.py 报告页提供下载链接）、`data/profiler.db`（数据库）、`data/cookie.json`（登录态）。

## 测试说明

项目**没有单元测试框架**（无 pytest/unittest 目录）。改动后按"从便宜到贵"三层验证：

```bash
# 1) 离线回归（94 项，秒级，不联网/不用 Cookie/不消耗 LLM 额度，使用隔离临时库）
python tests/run_all.py            # 主回归 + 评论路径 + 弹幕/评论判定聚合 + 多分P 口径（密度轴/历史采集/样本标注）
python tests/run_all.py --lint     # 附带 pyflakes（需 pip install pyflakes）

# 2) 静态检查（抓未定义名/语法类回归，比运行时踩 NameError 便宜得多）
python -m pyflakes src/*.py web.py run.py quick_test.py login.py

# 3) 真实端到端冒烟（需有效 Cookie 与网络）
python quick_test.py [BV号] [--top N]
```

新增/修改易错路径（断点续采、翻页降级、LLM 判定聚合等）时，请在 `tests/offline/` 补一条离线用例——本轮全库审查中三处缺陷（评论 NameError、判定聚合被清零）正是靠这类用例才被锁住。

## 安全注意事项

- `src/config.py` 含 LLM API Key 默认值、`data/cookie.json` 与 `data/cookies/`（小号池）含 B站登录凭证，以上连同 `data/profiler.db`、`data/reports/`、`data/qrcode.png` 均已在 `.gitignore` 中排除——**不要把它们提交进仓库或打印到日志**。
- LLM 配置走环境变量覆盖：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_MAX_TOKENS`，优先用环境变量而非改 `config.py` 里的硬编码 Key。三厂商（`LLM_PROVIDER` 选主用：默认 `deepseek`、`glm` 智谱（`GLM_API_KEY` / `GLM_BASE_URL` / `GLM_MODEL`，免费档限流激进、判定偏触发快乐，末位兜底；5.x 恒思考勿用）、`mimo` 小米 MiMo（`MIMO_*`，默认 mimo-v2.5）；备用厂商按 mimo→glm→deepseek 序自动取下一个 key 非空的），llm_cache key 含模型名，各厂商缓存互不污染可 A/B 对比；**判定批次（问题弹幕/问题评论共用 _judge_batches）按厂商自动关闭/压低思考**（deepseek 与 GLM-4.x 用 thinking:disabled、GLM-5.x 恒思考压 low，推理 token 不计费且不占 max_tokens 预算），深掘保留默认思考档；判定批次失败会自动换备用厂商（`LLM_FALLBACK`）兜底重试；仍失败则整轮等待重试（60s×轮次递增、封顶 300s，总耗时超 LLM_RETRY_BUDGET_SECONDS=1800s 熔断放弃剩余批次并告警；认证失败/模型不存在等致命 4xx 直接报错降级，厂商内容审核拦截（如 GLM 1301）按批次跳过不中止整轮，不死循环）。
- Cookie 等于账号登录态，泄露即等于账号被盗，处理 `data/` 目录文件时保持谨慎。
- 机场订阅链接（config.py SUB_URLS / 环境变量）与 data/mihomo_runtime/config.yaml 含凭证，均不入库、不打印（mihomo stop 时自动删除该文件）；内置核心二进制在 data/ 或 vendor/（gitignore），自动下载强制官方 SHA256 校验。注意 config.py 中既有明文 Key 建议尽快轮换并改用环境变量注入。