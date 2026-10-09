"""
配置模板 —— 使用方式：
    cp config.example.py src/config.py        # macOS / Linux / Git Bash
    copy config.example.py src\\config.py     # Windows cmd / PowerShell（路径用反斜杠）

然后打开 src/config.py 修改。**最少只要改两处，在文件里搜「替换这里」可直接定位**：

    ① 【替换这里①】LLM_API_KEY —— 你的大模型 API Key
       不填也能跑：问题弹幕/问题评论判定与 AI 深掘会自动跳过，采集/破解/画像/报告全部可用。
    ② 【替换这里②】SUB_URLS   —— 你的机场订阅链接
       不填也能跑：不走代理、直连，其余功能完全照常（只是少了对风控换 IP 的能力）。

其余常量都有合理默认值，不用动。src/config.py 已被 .gitignore 排除，不会被提交。

改完想确认没写错，可跑一句自检（只打印"订阅条数 / Key 是否已填"，不会打印链接或 Key 本身）：
    python -c "import sys;sys.path.insert(0,'src');import config;print('订阅条数',len(config.SUB_URLS),'| LLM Key 已填',bool(config.LLM_API_KEY))"
"""
import os

# ========== 路径配置 ==========
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "profiler.db")
COOKIE_PATH = os.path.join(DATA_DIR, "cookie.json")
# 小号池目录：python login.py <名字> 扫码后存为 cookies/<名字>.json，run.py 自动发现
COOKIES_DIR = os.path.join(DATA_DIR, "cookies")
REPORT_DIR = os.path.join(DATA_DIR, "reports")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

# ========== API 端点 ==========
VIDEO_INFO_URL = "https://api.bilibili.com/x/web-interface/view"
DANMAKU_XML_URL = "https://api.bilibili.com/x/v1/dm/list.so"
DANMAKU_HISTORY_INDEX_URL = "https://api.bilibili.com/x/v2/dm/history/index"
DANMAKU_HISTORY_SEG_URL = "https://api.bilibili.com/x/v2/dm/web/history/seg.so"
DANMAKU_VIEW_URL = "https://api.bilibili.com/x/v2/dm/web/view"  # 弹幕元数据（含互动弹幕明文mid，需SESSDATA）
COMMENT_MAIN_WBI_URL = "https://api.bilibili.com/x/v2/reply/wbi/main"  # 主评论新接口（wbi签名+游标翻页）
COMMENT_MAIN_URL = "https://api.bilibili.com/x/v2/reply/main"  # 旧版主评论接口（wbi/main 失败时降级用）
COMMENT_REPLY_URL = "https://api.bilibili.com/x/v2/reply/reply"
CHARGE_LIST_URL = "https://api.bilibili.com/x/web-interface/elec/show"  # 视频充电鸣谢名单（含明文pay_mid）

USER_CARD_URL = "https://api.bilibili.com/x/web-interface/card"
USER_CARDS_BATCH_URL = "https://api.bilibili.com/x/polymer/pc-electron/v1/user/cards"  # 批量名片（≤50人/请求，仅需登录）
USER_SPACE_URL = "https://api.bilibili.com/x/space/wbi/acc/info"  # wbi 版空间信息（旧 acc/info 已废弃）
# 投稿列表主接口：文档注明"暂未发现风控校验"，无需 wbi 签名；keywords 为空取全部
USER_VIDEOS_URL = "https://api.bilibili.com/x/series/recArchivesByKeywords"
# 投稿列表降级接口：wbi 签名，2023-11 起需 dm_img_* 指纹参数（实测当前未强制），但返回 typeid 分区信息
USER_VIDEOS_LEGACY_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
USER_DYNAMICS_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
USER_FOLLOWINGS_URL = "https://api.bilibili.com/x/relation/followings"
USER_FOLLOWERS_URL = "https://api.bilibili.com/x/relation/followers"
USER_FAV_FOLDERS_URL = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
USER_FAV_CONTENTS_URL = "https://api.bilibili.com/x/v3/fav/resource/list"
USER_BANGUMI_URL = "https://api.bilibili.com/x/space/bangumi/follow/list"
USER_LIVE_URL = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld"
USER_LIKE_URL = "https://api.bilibili.com/x/space/like/video"

QRCODE_GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QRCODE_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

# ========== 请求配置 ==========
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}

BILI_TICKET_ENABLED = True  # 申请bili_ticket降低风控概率

# 请求间隔为区间值：每次请求在区间内随机取时长，消除固定节奏的机器人特征
REQUEST_DELAY = (0.8, 1.6)       # 基础请求间隔区间（秒）
REQUEST_DELAY_LONG = (2.0, 4.0)  # 高风险API间隔区间（秒）
MAX_RETRY = 3                # 最大重试次数
RETRY_BACKOFF = 2.0          # 重试退避基数（秒）
RISK_COOLDOWN = 600          # 触发-412风控后的长冷却秒数

# 自适应降速：触发风控（-412/HTTP412/重签无效的-352/-403）时间隔倍率上调，
# 连续成功请求后缓慢回落；"撞一次墙就老实一点"，避免冷却结束又原速撞墙
ADAPTIVE_THROTTLE_FACTOR = 1.5   # 触发风控后间隔倍率增幅（×该值）
ADAPTIVE_THROTTLE_MAX = 5.0      # 间隔倍率上限
ADAPTIVE_THROTTLE_DECAY = 0.99   # 每次业务成功请求后倍率衰减（约40次成功回落一半）

# ========== IP 池（外部 Clash / 内置 mihomo 核心） ==========
# 三档来源自动择优（都不配置则降级直连，程序照常运行）：
#   1. 外部控制器自动探测：本机已有运行中的 Clash/ShellCrash（9090/9999/9097）即直接接管；
#   2. SUB_URLS 内置核心：填机场订阅链接后自动下载/拉起内置 mihomo 核心（仅监听 127.0.0.1 随机端口）；
#   3. 都不可用 → 无 IP 池，账号轮转 + 长冷却兜底直连。
#
# 【替换这里②】机场订阅链接（可选。属凭证，勿提交、勿打印、勿截图）：
#   把下面这行 os.environ.get("SUB_URLS", "") 的**第二个参数**（现在是两个紧挨的空引号 ""）
#   改成你的机场订阅链接。**引号要保留，只把链接贴在两个引号中间**，改完长这样：
#       SUB_URLS = [u.strip() for u in os.environ.get("SUB_URLS",
#           "https://example.com/api/v1/client/subscribe?token=你的令牌").split(",") if u.strip()]
#   · ⚠️ 引号不能删：写成 ...("SUB_URLS", https://example.com/...).split(...) 会直接语法错误
#   · 除引号中间的内容外，其余字符（方括号、split、逗号等）都不要动
#   · 多个订阅：写在**同一对引号之间**，用英文逗号分隔
#   · 不填（保持 ""）：不使用代理，直连运行，其余功能完全照常
#   · 也可以用环境变量覆盖（优先级更高）：cmd 用 set、PowerShell 用 $env:、Linux/mac 用 export
SUB_URLS = [u.strip() for u in os.environ.get("SUB_URLS", "").split(",") if u.strip()]
#                                                      ↑↑↑ 把 "" 中间填上你的订阅链接（引号保留）↑↑↑
# 覆盖内置 mihomo 二进制定位（默认依次找 vendor/mihomo、data/mihomo，都没有则自动下载）
MIHOMO_PATH = os.environ.get("MIHOMO_PATH", "")
# GitHub 加速前缀列表（下载 mihomo 核心用，按序尝试、全部失败回退直连；
# 环境变量 GH_PROXIES 逗号分隔可覆盖，置空则只直连）
GH_PROXIES = [u.strip() for u in os.environ.get(
    "GH_PROXIES",
    "https://gh-proxy.cn/,https://gh-proxy.com/,https://ghfast.top/,"
    "https://mirror.ghproxy.com/,https://hub.gitmirror.com/",
).split(",") if u.strip()]
# 外部 Clash/ShellCrash 控制器（CLASH_ENABLED=1 显式启用；未启用也会自动探测本机常见端口）
CLASH_ENABLED = os.environ.get("CLASH_ENABLED", "") == "1"
CLASH_API_URL = os.environ.get("CLASH_API_URL", "http://127.0.0.1:9090")
CLASH_SECRET = os.environ.get("CLASH_SECRET", "")
CLASH_GROUP = os.environ.get("CLASH_GROUP", "")            # 轮换节点组名（内置核心默认 profiler）
CLASH_PROXY_URL = os.environ.get("CLASH_PROXY_URL", "http://127.0.0.1:7890")
# 单任务单元允许的兜底冷却圈数（整圈账号全风控 → 长冷却为最后手段）
MAX_RISK_ROUNDS = 3

# ========== 破解配置 ==========
MITM_MAX_UID = 10_000_000_000   # MITM 反查覆盖上限：全部 ≤10 位 UID（16位随机长UID不可解）

# ========== 采集配置 ==========
MAX_COMMENT_PAGES = 100      # 评论最大翻页数（约20条/页，上限~2000条主评论）
COMMENT_REPLY_MAX_PAGES = 25  # 每条主评论的子评论最多补采页数（pn分页，每页20条，上限500条）

# 跨视频足迹（用户卡片「其他视频足迹」区块：该用户在其他已分析视频中的弹幕/评论）
MAX_FOOTPRINT_VIDEOS = 5              # 每张卡片最多展示的其他视频数
MAX_FOOTPRINT_DANMAKU_SAMPLES = 5     # 每个其他视频的弹幕样本条数
MAX_FOOTPRINT_COMMENT_SAMPLES = 5     # 每个其他视频的评论样本条数

# 高回复评论页（单独成页的潜在争执热点：回复数达阈值的评论独立展示，回复树完整展示不截断）
HOT_COMMENT_MIN_REPLIES = 20          # 入选门槛：主评论回复数 >= 该值
HOT_COMMENT_MAX_SHOW = 50             # 页面最多展示的高回复评论条数

# 用户互动时间线页（/user/<uid>：该用户在已分析视频中的弹幕/评论足迹，按最近互动倒序）
USER_TIMELINE_MAX_VIDEOS = 50         # 时间线最多展示的视频数（超出显示「另有 N 个」）
USER_TIMELINE_SAMPLES = 3             # 每个视频展示的弹幕/评论样本条数

# 跨视频重叠分析（首页面板：在多个已分析视频中都出现过的发送者，找水军/带节奏用户）
CROSS_VIDEO_MIN_VIDEOS = 2            # 入选门槛：出现过的已分析视频数 >= 该值
CROSS_VIDEO_MAX_USERS = 50            # 面板最多展示的用户数

# 概览页弹幕密度时间轴
DENSITY_BUCKETS = 60                  # 按视频内时间分桶的桶数上限

HISTORY_DANMAKU_ENABLED = True   # 是否采集全量历史弹幕（需登录）
HISTORY_MAX_MONTHS = 24          # 历史弹幕最多回溯月数
HISTORY_MAX_DAYS = 400           # 历史弹幕最多采集天数（逐日接口，每日1次请求）
# 分P视频的历史弹幕：B站历史接口只认 cid（=各分P 各一个弹幕池），只采分P 1 会让
# 其余分P 永久只有实时池数据（密度图/画像样本严重偏低）。各分P 独立检查点、独立续采。
HISTORY_MULTIPAGE_ENABLED = True  # False=退回只采分P 1（旧行为，请求量最省）
HISTORY_MULTIPAGE_MAX_PAGES = 4   # 最多采前 N 个分P的历史弹幕（每P 各 HISTORY_MAX_DAYS 天请求量）
MAX_VIDEO_PAGES = 3          # 用户视频最大翻页
MAX_DYNAMIC_PAGES = 5        # 动态最大翻页（对画像而言近 5 页足够，减少高风险请求）
# 调研实证：他人关注列表接口仅能查看前 100 个（5页×20），超出返回空列表但 code=0
MAX_FOLLOWING_PAGES = 5       # 关注列表最大翻页（每页20，他人最多100）
MAX_FOLLOWER_PAGES = 2       # 粉丝列表最大翻页
MAX_FAV_CONTENTS = 20        # 收藏夹内容采样数
# 并发模型说明：阶段5 用户采集走 combo_pool 按账号分片（每账号一子池、限速按号独立、
# 线程↔分片绑定），线程数不再由单独的 COLLECT_WORKERS 配置控制（该死配置已移除，
# 旧注释"限速为全局共享"与现行按号独立架构矛盾）

# ========== 画像配置 ==========
SPAM_HIGH_THRESHOLD = (10, 0.7)    # (弹幕数, 重复率)
SPAM_MEDIUM_THRESHOLD = (3, 0.5)   # (弹幕数, 重复率)：>2 条且半数重复即中风险
# 动态定员（兴趣命中者全进，仅超上限才按兴趣分截断）：上限随视频发送者规模浮动——
# 小视频保底 FLOOR，大视频按独立发送者数 × RATIO 上浮，HARD_CAP 为防爆绝对天花板
ANALYZE_USERS_FLOOR = 300        # 保底名额（小视频的默认上限）
ANALYZE_USERS_RATIO = 0.05       # 上限 = max(FLOOR, 独立发送者数 × 该比例)
MAX_ANALYZE_USERS_HARD_CAP = 1000  # 绝对上限（保险丝；采集成本与风控暴露的硬约束）
LLM_DEEP_TOP_K = 20                # LLM 重点深掘人数（兴趣分 top K 单人单调用）
LLM_DEEP_ENABLED = os.environ.get("LLM_DEEP_ENABLED", "1") not in ("0", "false", "False")
                                   # 阶段7 LLM 重点深掘总开关（False 或环境变量 LLM_DEEP_ENABLED=0 关闭）
CRINGE_BATCH_SIZE = 200            # 问题弹幕检测每批弹幕条数
COMMENT_CRINGE_BATCH_SIZE = 100    # 问题评论检测每批条数（评论比弹幕长，批次减半）
COMMENT_CRINGE_MAX_ITEMS = 2000    # 问题评论检测去重后最大条数（按点赞降序截断）

# 问题评论作者直引画像：问题评论达阈值的作者凭明文 UID 直接并入画像名单（无需 mid_hash 破解）
COMMENT_AUTHOR_MIN_SEVERITY = 2    # 入选条件一：问题评论最高严重度 >= 该值
COMMENT_AUTHOR_MIN_HITS = 2        # 入选条件二：问题评论命中条数 >= 该值（两条件满足其一）

# 问题评论榜（高回复评论页顶部：全部问题评论按热度加权排序，高热度优先展示）
COMMENT_HEAT_REPLY_WEIGHT = 10     # 热度 = 点赞 + 回复数 × 该权重（回复比点赞更能体现争执烈度）
PROBLEM_COMMENT_TOP_N = 30         # 榜单最多展示条数

# 争执焦点区块（高回复评论页顶部：问题回复按 parent_rpid 还原「谁攻击谁」）
ATTACK_FOCUS_TOP_N = 5             # 挑事者/被围攻者双榜保底名额
ATTACK_FOCUS_MAX_N = 20            # 名额上限；实际名额随攻击边数浮动：每10条攻击边+1

# B站屏蔽列表导出（概览页操作条：把问题发送者导出为播放器「弹幕屏蔽列表」可导入的 JSON）
BLOCKLIST_MAX_UIDS = 200           # 单次导出上限；B站屏蔽列表容量约 200 条，超出按严重度截断

# 弹幕高能点 AI 标注（概览页密度时间轴：峰值时段取弹幕样本让 LLM 判断爆发原因）
DENSITY_PEAK_MAX = 5               # 每视频最多标注的峰值时段数（跨全部分P取弹幕量最高的 N 个）
DENSITY_PEAK_MIN_COUNT = 10        # 峰值桶最少弹幕数（低于此不成「爆发」，防小视频噪声）
DENSITY_PEAK_SAMPLE = 30           # 每个峰值时段取多少条弹幕样本喂给 LLM

# ========== Web 报告配置 ==========
WEB_AUTOSTART = True   # run.py/quick_test.py 分析完毕自动启动 web.py 并用浏览器打开报告页（False 关闭）

# ========== LLM 配置（可选：不填 Key 就自动跳过 AI 判定与 AI 深掘，其余功能照常） ==========
# 【替换这里①】大模型 API Key。三家厂商**任选一家填、且只填一家**：
#
#   用 DeepSeek（默认，推荐）：
#     1) 打开 https://platform.deepseek.com 注册/登录
#     2) 左侧「API keys」→ 创建 → 复制 sk- 开头的那一串
#     3) 把下面 LLM_API_KEY 那行的**第二个参数**（现在是两个紧挨的空引号 ""）改成你的 Key，
#        **引号要保留，只把 Key 贴在两个引号中间**，改完长这样：
#            LLM_API_KEY = os.environ.get("LLM_API_KEY", "sk-你的key")
#        ⚠️ 引号不能删：写成 ...("LLM_API_KEY", sk-你的key) 会直接报错
#        注意：不是新增一行，而是把 "" 中间的内容补上，其余字符都不要动
#
#   用小米 MiMo / 智谱 GLM：把下面 MIMO_API_KEY / GLM_API_KEY 按同样方式填上，
#   再把 LLM_PROVIDER 改成 "mimo" / "glm"（没填 Key 的厂商会被自动忽略，不会报错）
#
#   不想把 Key 写进文件（更安全，且优先级高于文件里的值）就用环境变量：
#       Windows cmd:        set LLM_API_KEY=sk-你的key      然后  python run.py BVxxxxxxxx
#       Windows PowerShell: $env:LLM_API_KEY="sk-你的key"; python run.py BVxxxxxxxx
#       macOS / Linux:      export LLM_API_KEY=sk-你的key
#
# DeepSeek 官方 OpenAI 兼容端点（https://api-docs.deepseek.com），非必要不用改
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")   # ←【替换这里①】把 "" 中间填上你的 Key（引号保留，sk- 开头）
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "16384"))
# LLM 判定并发上限（问题弹幕/问题评论；实际路数=min(批次数, 此值)；触发 429 限速自动退避重试）
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "16"))

# 第二/三套 LLM：小米 MiMo、智谱 GLM（默认主用 deepseek——判定批次已按厂商关思考，
# 推理 token 不再计费，成本可控；GLM 免费档限流激进、判定偏触发快乐，作末位兜底）。
# LLM_PROVIDER 选择主用厂商，备用厂商按 mimo→glm→deepseek 序自动取下一个 key 非空的；
# 各厂商缓存按模型名隔离（cache key 含模型名），可同视频 A/B 对比判定质量
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "deepseek")   # 主用厂商 deepseek | glm | mimo（填了哪家的 Key 就用哪家）
MIMO_API_KEY = os.environ.get("MIMO_API_KEY", "")   # ← 用小米 MiMo 就把 "" 中间填上它的 Key（引号保留），并把上面改成 "mimo"
MIMO_BASE_URL = os.environ.get("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_MODEL = os.environ.get("MIMO_MODEL", "mimo-v2.5")
GLM_API_KEY = os.environ.get("GLM_API_KEY", "")        # ← 用智谱就把 "" 中间填上 bigmodel.cn 的 Key（引号保留），并把 LLM_PROVIDER 改成 "glm"
GLM_BASE_URL = os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
GLM_MODEL = os.environ.get("GLM_MODEL", "glm-5.3-flash")
_PROVIDERS = {"deepseek": (LLM_API_KEY, LLM_BASE_URL, LLM_MODEL),
              "mimo": (MIMO_API_KEY, MIMO_BASE_URL, MIMO_MODEL),
              "glm": (GLM_API_KEY, GLM_BASE_URL, GLM_MODEL)}
_primary = LLM_PROVIDER if LLM_PROVIDER in _PROVIDERS else "glm"
_fb = next((n for n in ("mimo", "glm", "deepseek") if n != _primary and _PROVIDERS[n][0]), "")
LLM_FALLBACK = (*_PROVIDERS[_fb], _fb) if _fb else ("", "", "", "")
LLM_API_KEY, LLM_BASE_URL, LLM_MODEL = _PROVIDERS[_primary]

# === 审查修复新增（2026-08-29） ===
# 以下常量原分散在各模块内的私有定义，统一迁移至配置文件便于调优
LLM_DEEP_TIMEOUT = 120             # 深掘单次 LLM 调用超时秒数（原 llm_analyzer._DEEP_TIMEOUT）
LLM_RETRY_BUDGET_SECONDS = 1800    # 问题弹幕/评论判定整轮重试总耗时熔断（原 cringe_detector._RETRY_BUDGET_SECONDS）
LLM_TRANSIENT_RETRIES = 2          # 瞬态连接/超时错误同厂商短退避重试次数（原 cringe_detector._TRANSIENT_RETRIES）
HISTORY_RECENT_REFRESH_DAYS = 3    # 历史弹幕 done=1 后重跑滚动补采最近天数（原 danmaku_history._RECENT_REFRESH_DAYS）
PROXY_RETRY_AFTER = 600            # IP 池摘代理降级后恢复重探间隔秒数（原 combo_pool._PROXY_RETRY_AFTER）
SINGLE_ACCOUNT_RISK_COOLDOWN = 120  # 单账号子池风控冷却基准秒数（原 combo_pool._SINGLE_ACCOUNT_COOLDOWN）
WBI_KEY_FAIL_TTL = 60              # WBI 密钥获取失败负缓存秒数（原 api_client._WBI_KEY_FAIL_TTL）
CRED_FAIL_TTL = 300                # buvid3/bili_ticket 获取失败重试间隔秒数（原 api_client._CRED_FAIL_TTL）
REPLY_TREE_MAX_DEPTH = 50          # 高回复评论树渲染递归深度上限（原 web.py _REPLY_TREE_MAX_DEPTH）
WEB_JOB_MAX_KEPT = 100             # web 内存 job 表淘汰上限（原 web.py _JOB_MAX_KEPT）
WEB_PAGE_CACHE_MAX_ENTRIES = 32    # 报告页 HTML 缓存最多保留的（视频, 遮蔽状态）条目数
WEB_PAGE_CACHE_MAX_BYTES = 64 * 1024 * 1024  # 报告页 HTML 缓存总字节上限（超出即整体清空重建）
ANALYZE_MAX_TARGETS = 200          # /api/analyze 单次 mid_hashes 上限（原 web.py _ANALYZE_MAX_TARGETS）
SPAM_BURST_WINDOW_SECONDS = 10     # 刷屏突发检测的滑动窗口长度（秒，原 spam_detector._BURST_WINDOW_SECONDS）
SPAM_BURST_HIGH_COUNT = 5          # 窗口内 ≥N 条判高强度突发（原 spam_detector._BURST_HIGH_COUNT）
SPAM_BURST_MEDIUM_COUNT = 3        # 窗口内 ≥N 条判疑似突发（原 spam_detector._BURST_MEDIUM_COUNT）
SPAM_VARIANT_SIMILARITY = 0.8      # 变种刷屏的平均相似度阈值（原 spam_detector._VARIANT_SPAM_SIMILARITY）
SPAM_VARIANT_MIN_COUNT = 5         # 变种刷屏判定的最小弹幕数（原 spam_detector._VARIANT_SPAM_MIN_COUNT）
SPAM_PAIRWISE_UNIQUE_CAP = 200     # 两两相似度计算的唯一内容数上限（超出确定性抽样，防 O(u²) 卡死）
SPAM_BURST_MIN_COUNT = 10          # 高频爆发判定：滑动窗口内最大条数阈值（原 spam_detector._BURST_SPAM_MIN_COUNT）
SPAM_COMBO_BONUS = 0.10            # 组合评分：每多触发一条刷屏规则的加分（封顶 1.0，替代旧的 max 取极值）
SPAM_RELATIVE_MIN_POOL = 10        # 相对离群判定：全池至少 N 个达标发送者才启用（防小样本噪声）
SPAM_RELATIVE_REPEAT_FLOOR = 0.4   # 相对离群：重复率绝对下限
SPAM_RELATIVE_COUNT_FLOOR = 5      # 相对离群：弹幕数绝对下限
SPAM_RELATIVE_SCORE = 0.5          # 相对离群命中后的保底分（中档）
REPEAT_EVENT_WINDOW_SECONDS = 60   # 群体复读事件：时间窗口（秒；视频内时间轴与发送时间轴同用该值）
REPEAT_EVENT_MIN_SENDERS = 5       # 群体复读事件：窗口内不同发送者下限
REPEAT_EVENT_MIN_TOTAL = 8         # 群体复读事件：窗口内总条数下限
REPEAT_EVENT_TOP_N = 20            # 群体复读事件：展示上限
UID_HARVEST_MAX_PAGES = 500        # 评论 UID 收割（纯翻页取 uid 建 CRC32 映射）的累计页数上限
PROXY_NODE_DEAD_TTL = 660          # 节点死名单 TTL（秒，略大于订阅健康检查间隔 600s，到期允许复活重试）
COMMENT_REFRESH_MAX_PAGES = 10     # 评论增量刷新（done=1 重跑）按时间序回翻的页数上限
LLM_BATCH_THINKING = os.environ.get("LLM_BATCH_THINKING", "off")  # 判定批次思考强度：off(关)/low(低档)/default(厂商原生)
