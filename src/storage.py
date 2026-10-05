"""
数据持久化模块（SQLite）

支持：
- 视频/弹幕/用户数据存储
- 基于 senders/users 缓存的断点续采
- 避免重复采集
"""
import sqlite3
import json
import time
from contextlib import closing
from datetime import datetime

from config import DB_PATH

# uid/content 为空的评论被 save_comments 静默丢弃时的累计计数（debug 统计用）
_dropped_comment_count = 0

# 全局映射来源覆盖优先级：明文来源（评论区验证/充电名单/互动弹幕/视频信息）
# 可靠度高于 CRC32 破解；冲突时仅当新来源优先级 >= 旧来源才覆盖 uid/source。
# 未列出的来源（如旧版遗留值）按最低档处理，保证明文凭据永不被未知来源冲掉
_GLOBAL_UID_SOURCE_PRIORITY = {
    "评论区验证": 2,
    "充电名单": 2,
    "互动弹幕": 2,
    "视频信息": 2,
    "评论收割": 2,     # harvest_comment_uids 翻评论区收集的明文 UID，证据等级同评论
    "CRC32破解": 1,
}


def _like_escape(s: str) -> str:
    """LIKE 模式串转义（配合 ESCAPE '\\'）：\\ % _ 前置反斜杠，防 bvid 含通配符误匹配"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_db() -> sqlite3.Connection:
    """获取数据库连接（每连接设置并发相关 PRAGMA）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # 并发友好：写入遇锁等待 10s 而非立即报 database is locked（web.py 后台 job 与
    # 前台渲染会并发访问同一库）；WAL 允许读写并发，属库级持久设置，此处幂等执行
    # 以覆盖现网仍为 delete 模式的旧库；synchronous=NORMAL 在 WAL 下安全且更快
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """初始化数据库表结构"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        # 库级持久设置：WAL 日志模式（get_db 中已幂等执行，此处建表时显式设置一次）
        cursor.execute("PRAGMA journal_mode=WAL")

        # 视频信息表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS videos (
                bvid TEXT PRIMARY KEY,
                title TEXT,
                aid INTEGER,
                cid INTEGER,
                duration INTEGER,
                view_count INTEGER,
                danmaku_count INTEGER,
                reply_count INTEGER,
                video_info_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 弹幕发送者表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS senders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bvid TEXT,
                mid_hash TEXT,
                uid INTEGER,
                confidence TEXT,
                method TEXT,
                danmaku_count INTEGER,
                contents_json TEXT,
                spam_level TEXT,
                spam_score REAL,
                UNIQUE(bvid, mid_hash)
            )
        ''')

        # 用户深度数据表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                uid INTEGER PRIMARY KEY,
                name TEXT,
                level INTEGER,
                data_json TEXT,
                profile_json TEXT,
                collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 全局 mid_hash→UID 映射表（跨视频复用，只增不删）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS global_uid_map (
                mid_hash TEXT PRIMARY KEY,
                uid INTEGER NOT NULL,
                source TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 1
            )
        ''')

        # LLM 结果缓存表（问题弹幕判定 + 重点深掘，跨运行复用省 token）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_key   TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
        ''')

        # 全量弹幕表（Web 弹幕浏览器数据源；mode/color/pool/dmid 入库支撑弹幕属性统计）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS danmaku (
                bvid TEXT NOT NULL,
                mid_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                time REAL NOT NULL,        -- 视频内出现时间(秒)
                timestamp INTEGER NOT NULL, -- 发送时间戳
                mode INTEGER NOT NULL DEFAULT 1,   -- 弹幕模式（1-3滚动/4底部/5顶部）
                color TEXT NOT NULL DEFAULT '',    -- 弹幕颜色（#rrggbb）
                pool INTEGER NOT NULL DEFAULT 0,   -- 弹幕池
                dmid INTEGER NOT NULL DEFAULT 0    -- 弹幕ID（历史合并去重键）
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_danmaku_bvid ON danmaku(bvid)")
        # 复合索引：弹幕浏览器按发送者+时间排序、报告按 bvid 聚合发送者用
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_danmaku_bvid_hash_time ON danmaku(bvid, mid_hash, time)")

        # 旧库迁移：danmaku 表补 mode/color/pool/dmid 列（弹幕属性统计：滚动占比/颜色分布/顶底弹幕）
        dm_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(danmaku)").fetchall()}
        if "mode" not in dm_cols:
            cursor.execute("ALTER TABLE danmaku ADD COLUMN mode INTEGER NOT NULL DEFAULT 1")
        if "color" not in dm_cols:
            cursor.execute("ALTER TABLE danmaku ADD COLUMN color TEXT NOT NULL DEFAULT ''")
        if "pool" not in dm_cols:
            cursor.execute("ALTER TABLE danmaku ADD COLUMN pool INTEGER NOT NULL DEFAULT 0")
        if "dmid" not in dm_cols:
            cursor.execute("ALTER TABLE danmaku ADD COLUMN dmid INTEGER NOT NULL DEFAULT 0")
        # 旧库迁移：danmaku 表补 page 列（分P序号，断点续采从库读回后 group_by_sender 要用）
        if "page" not in dm_cols:
            cursor.execute("ALTER TABLE danmaku ADD COLUMN page INTEGER NOT NULL DEFAULT 1")

        # 评论表（跨视频足迹 + 高回复评论页数据源；reply_count 只对主评论有意义，
        # root_rpid 记录子评论所属主评论的 rpid，供「高回复评论」页关联争议主楼与回复）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bvid TEXT NOT NULL,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL DEFAULT '',  -- 评论者昵称（高回复评论树直接显示用户名用）
                rpid INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL,
                ctime INTEGER NOT NULL DEFAULT 0,   -- 评论发送时间戳
                like INTEGER NOT NULL DEFAULT 0,
                is_sub INTEGER NOT NULL DEFAULT 0,  -- 1=子评论 0=主评论
                reply_count INTEGER NOT NULL DEFAULT 0,  -- 主评论的回复总数（rcount）
                root_rpid INTEGER NOT NULL DEFAULT 0,    -- 子评论所属主评论 rpid
                parent_rpid INTEGER NOT NULL DEFAULT 0,  -- 子评论的直接父级 rpid（回复树缩进用；0=直接回复主楼）
                problem TEXT NOT NULL DEFAULT '',        -- LLM 问题评论类别（空=正常/未判定）
                UNIQUE(bvid, rpid, uid)
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_bvid ON comments(bvid)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_uid ON comments(uid)")
        # 复合索引：问题评论榜/问题作者直引按 (bvid, problem) 过滤用
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_bvid_problem ON comments(bvid, problem)")

        # 旧库迁移：comments 表补 reply_count / root_rpid 列（高回复评论功能）
        comment_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(comments)").fetchall()}
        if "reply_count" not in comment_cols:
            cursor.execute("ALTER TABLE comments ADD COLUMN reply_count INTEGER NOT NULL DEFAULT 0")
        if "root_rpid" not in comment_cols:
            cursor.execute("ALTER TABLE comments ADD COLUMN root_rpid INTEGER NOT NULL DEFAULT 0")
        # 旧库迁移：comments 表补 parent_rpid（回复树缩进）与 problem（LLM 问题评论标注）列
        if "parent_rpid" not in comment_cols:
            cursor.execute("ALTER TABLE comments ADD COLUMN parent_rpid INTEGER NOT NULL DEFAULT 0")
        if "problem" not in comment_cols:
            cursor.execute("ALTER TABLE comments ADD COLUMN problem TEXT NOT NULL DEFAULT ''")
        # 旧库迁移：comments 表补 uname（高回复评论树直接显示用户名）
        if "uname" not in comment_cols:
            cursor.execute("ALTER TABLE comments ADD COLUMN uname TEXT NOT NULL DEFAULT ''")
        # 旧库迁移：comments 表补 location（IP 属地，断点续采从库读回评论时画像地域维度用）
        if "location" not in comment_cols:
            cursor.execute("ALTER TABLE comments ADD COLUMN location TEXT NOT NULL DEFAULT ''")

        # 误报标记表（P2-a）：人工标注 LLM 误判的问题弹幕/评论。
        # kind: dm=问题弹幕（target=弹幕内容，判定按内容去重故同内容同源同罪）
        #       cmt=问题评论（target=rpid 字符串）
        # 展示层加载后从聚合中扣除，可再点撤销；llm_cache 不动，标记跨重跑保留
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS false_positive (
                bvid TEXT NOT NULL,
                kind TEXT NOT NULL,
                target TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (bvid, kind, target)
            )
        ''')

        # 头像缓存表：为争执焦点关系图等展示层补采头像（uid 未进 users 表的用户）。
        # 行存在即表示已查过（face 可能为空串：封号/无头像），避免每次渲染重复请求
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS face_cache (
                uid INTEGER PRIMARY KEY,
                face TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            )
        ''')

        # 阶段检查点表（断点续采）：弹幕历史逐日进度、评论翻页游标等，
        # 任意位置中断（Ctrl+C/崩溃）后重跑从检查点继续
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS phase_state (
                bvid TEXT NOT NULL,
                phase TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (bvid, phase, key)
            )
        ''')

        conn.commit()

        # 清理旧版本的 progress 表（断点续采已改为纯 senders/users 缓存机制）
        cursor.execute("DROP TABLE IF EXISTS progress")
        conn.commit()
    print("[Storage] 数据库初始化完成")


def get_phase_state(bvid: str, phase: str, key: str) -> str | None:
    """读检查点；无记录返回 None"""
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT value FROM phase_state WHERE bvid=? AND phase=? AND key=?",
            (bvid, phase, key)).fetchone()
    return row["value"] if row else None


def set_phase_state(bvid: str, phase: str, key: str, value: str):
    """写检查点（幂等覆盖）"""
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO phase_state (bvid, phase, key, value) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(bvid, phase, key) DO UPDATE SET value=excluded.value",
            (bvid, phase, key, value))
        conn.commit()


# ========== 视频数据 ==========

def save_video_info(bvid: str, video_info: dict):
    """保存视频信息"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        stat = video_info.get("stat", {})
        cursor.execute('''
            INSERT INTO videos (bvid, title, aid, cid, duration, view_count, danmaku_count, reply_count, video_info_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bvid) DO UPDATE SET
                title=excluded.title, aid=excluded.aid, cid=excluded.cid, duration=excluded.duration,
                view_count=excluded.view_count, danmaku_count=excluded.danmaku_count,
                reply_count=excluded.reply_count, video_info_json=excluded.video_info_json
        ''', (
            bvid,
            video_info.get("title", ""),
            video_info.get("aid", 0),
            video_info.get("cid", 0),
            video_info.get("duration", 0),
            stat.get("view", 0),
            stat.get("danmaku", 0),
            stat.get("reply", 0),
            json.dumps(video_info, ensure_ascii=False)
        ))
        conn.commit()


def load_video_info(bvid: str) -> dict | None:
    """加载视频信息"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT video_info_json FROM videos WHERE bvid = ?", (bvid,))
        row = cursor.fetchone()
    if row:
        return json.loads(row["video_info_json"])
    return None


# ========== 发送者数据 ==========

def save_sender(bvid: str, mid_hash: str, uid: int | None, confidence: str,
                method: str, danmaku_count: int, contents: list[str],
                spam_level: str, spam_score: float):
    """保存发送者解析结果"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO senders
            (bvid, mid_hash, uid, confidence, method, danmaku_count, contents_json, spam_level, spam_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bvid, mid_hash) DO UPDATE SET
                uid=excluded.uid, confidence=excluded.confidence, method=excluded.method,
                danmaku_count=excluded.danmaku_count, contents_json=excluded.contents_json,
                spam_level=excluded.spam_level, spam_score=excluded.spam_score
        ''', (bvid, mid_hash, uid, confidence, method, danmaku_count,
              json.dumps(contents, ensure_ascii=False), spam_level, spam_score))
        conn.commit()


def update_sender_spam(bvid: str, mid_hash: str, spam_level: str, spam_score: float):
    """回写发送者的刷屏检测结果（仅 UPDATE 已存在的行；未落库的行不受影响）"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE senders SET spam_level = ?, spam_score = ?
            WHERE bvid = ? AND mid_hash = ?
        ''', (spam_level, spam_score, bvid, mid_hash))
        conn.commit()


def load_senders(bvid: str) -> list[dict]:
    """加载已解析的发送者"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT mid_hash, uid, confidence, method, danmaku_count, contents_json, spam_level, spam_score
            FROM senders WHERE bvid = ?
        ''', (bvid,))
        rows = cursor.fetchall()
    return [{
        "mid_hash": r["mid_hash"],
        "uid": r["uid"],
        "confidence": r["confidence"],
        "method": r["method"],
        "danmaku_count": r["danmaku_count"],
        "contents": json.loads(r["contents_json"]),
        "spam_level": r["spam_level"],
        "spam_score": r["spam_score"],
    } for r in rows]


# ========== 用户数据 ==========

def save_user_data(uid: int, name: str, level: int, user_data: dict, profile: dict):
    """保存用户深度数据和画像"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (uid, name, level, data_json, profile_json, collected_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (uid, name, level,
              json.dumps(user_data, ensure_ascii=False),
              json.dumps(profile, ensure_ascii=False),
              datetime.now().isoformat()))
        conn.commit()


def update_user_profile(uid: int, profile: dict):
    """只更新 users.profile_json（阶段7 LLM 深掘结果注入后回写用；
    不重写 data_json/collected_at——深掘在阶段6落库之后运行）"""
    with closing(get_db()) as conn:
        conn.execute("UPDATE users SET profile_json = ? WHERE uid = ?",
                     (json.dumps(profile, ensure_ascii=False), uid))
        conn.commit()


def load_user_data(uid: int) -> tuple[dict, dict] | None:
    """加载用户数据和画像"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT data_json, profile_json FROM users WHERE uid = ?", (uid,))
        row = cursor.fetchone()
    if row:
        return json.loads(row["data_json"]), json.loads(row["profile_json"])
    return None


def has_user_data(uid: int) -> bool:
    """检查是否已采集过该用户"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE uid = ?", (uid,))
        row = cursor.fetchone()
    return row is not None


# ========== 全量弹幕（Web 弹幕浏览器数据源） ==========

def append_danmaku(bvid: str, danmaku_list: list[dict], seen_dmids: set) -> int:
    """增量落库（断点续采用）：按 dmid 去重——已见过的 dmid（含库中已有与本次新插入）
    跳过，dmid=0 无法判重直接插入（历史快照 dmid 恒非 0，实时池个别为 0）。
    seen_dmids 由调用方持有并在调用间复用；返回实际插入条数。"""
    rows = []
    for dm in danmaku_list:
        dmid = dm.get("dmid", 0)
        if dmid and dmid in seen_dmids:
            continue
        if dmid:
            seen_dmids.add(dmid)
        rows.append((bvid, dm["mid_hash"], dm["content"], dm["time"], dm["timestamp"],
                     dm.get("mode", 1), dm.get("color", ""), dm.get("pool", 0), dmid,
                     dm.get("page", 1)))
    if rows:
        with closing(get_db()) as conn:
            conn.executemany(
                "INSERT INTO danmaku (bvid, mid_hash, content, time, timestamp, mode, color, pool, dmid, page) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows)
            conn.commit()
    return len(rows)


def save_comments(bvid: str, comments: list[dict]):
    """阶段3评论采集后批量落库（跨视频足迹数据源）。

    UNIQUE(bvid, rpid, uid) + INSERT OR IGNORE 幂等去重：同一次分析重复落库、
    --force 重采（清缓存后重采同一批评论）都不会产生重复行。
    只存基础列；sign/avatar 等仅用于当次运行的内存流程，不入库。
    location（IP属地）入库：断点续采从库读回评论时画像地域维度要用。
    problem 列由 LLM 问题评论检测后经 update_comment_problems 回写。
    uname/location 在冲突时回填（旧行缺时补上），like/reply_count 冲突时刷新
    （热度榜不停在首采快照），其余列冲突不覆盖（首采快照为准）。
    """
    global _dropped_comment_count
    rows = [(bvid, c["uid"], c.get("uname", ""), c.get("rpid", 0), c.get("content", ""),
             c.get("ctime", 0), c.get("like", 0), 1 if c.get("is_sub") else 0,
             c.get("reply_count", 0), c.get("root_rpid", 0), c.get("parent_rpid", 0),
             c.get("location", ""))
            for c in comments if c.get("uid") and c.get("content")]
    # uid/content 为空的评论静默丢弃，累计计数并打印一条 debug 统计（不中断采集）
    dropped = len(comments) - len(rows)
    if dropped:
        _dropped_comment_count += dropped
        print(f"[Storage] 评论落库：跳过 {dropped} 条 uid/content 为空的评论（累计 {_dropped_comment_count} 条）")
    if not rows:
        return
    with closing(get_db()) as conn:
        conn.executemany(
            "INSERT INTO comments "
            "(bvid, uid, uname, rpid, content, ctime, like, is_sub, reply_count, root_rpid, parent_rpid, location) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bvid, rpid, uid) DO UPDATE SET "
            "uname = CASE WHEN excluded.uname != '' THEN excluded.uname ELSE comments.uname END, "
            "location = CASE WHEN excluded.location != '' THEN excluded.location ELSE comments.location END, "
            "like = excluded.like, reply_count = excluded.reply_count",
            rows)
        conn.commit()


def load_danmaku(bvid: str) -> list[dict]:
    """从库读回某视频的全部弹幕（断点续采用：跳过阶段2网络重采）。
    返回与采集侧同形的 dict 列表。"""
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT mid_hash, content, time, timestamp, mode, color, pool, dmid, page "
            "FROM danmaku WHERE bvid = ?", (bvid,)).fetchall()
    return [{"mid_hash": r["mid_hash"], "content": r["content"], "time": r["time"],
             "timestamp": r["timestamp"], "mode": r["mode"], "color": r["color"],
             "pool": r["pool"], "dmid": r["dmid"], "page": r["page"]} for r in rows]


def load_comments(bvid: str) -> list[dict]:
    """从库读回某视频的全部评论（断点续采用：跳过阶段3网络重采）。
    返回与采集侧同形的 dict 列表（problem 列带出 LLM 标注；location 旧行可能为空）。"""
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT uid, uname, rpid, content, ctime, like, is_sub, reply_count, "
            "root_rpid, parent_rpid, problem, location "
            "FROM comments WHERE bvid = ?", (bvid,)).fetchall()
    return [{"uid": r["uid"], "uname": r["uname"], "rpid": r["rpid"],
             "content": r["content"], "ctime": r["ctime"], "like": r["like"],
             "is_sub": bool(r["is_sub"]), "reply_count": r["reply_count"],
             "root_rpid": r["root_rpid"], "parent_rpid": r["parent_rpid"],
             "problem": r["problem"], "location": r["location"]} for r in rows]


def update_comment_problems(bvid: str, verdicts: dict):
    """回写 LLM 问题评论判定：verdicts = {rpid: 类别}；先清该 bvid 旧标注再逐条 UPDATE。

    幂等可重跑；rpid 在库中不存在（评论被删/未落库）时 UPDATE 命中 0 行，静默跳过。
    """
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE comments SET problem = '' WHERE bvid = ?", (bvid,))
        for rpid, category in verdicts.items():
            cursor.execute("UPDATE comments SET problem = ? WHERE bvid = ? AND rpid = ?",
                           (category, bvid, rpid))
        conn.commit()


# ========== 误报标记（P2-a：人工纠偏 LLM 判定，展示层扣除聚合，可撤销） ==========

def toggle_false_positive(bvid: str, kind: str, target: str) -> bool:
    """切换误报标记，返回切换后的状态（True=已标记）。kind: dm=弹幕内容 / cmt=评论rpid

    原子操作：INSERT OR IGNORE 命中已有行时 rowcount=0，据此判定原状态再 DELETE，
    避免先查后插在并发点击下的竞态（主键冲突不再抛异常）。"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO false_positive (bvid, kind, target, created_at) VALUES (?, ?, ?, ?)",
            (bvid, kind, target, int(time.time())))
        if cursor.rowcount:
            marked = True
        else:
            cursor.execute(
                "DELETE FROM false_positive WHERE bvid = ? AND kind = ? AND target = ?",
                (bvid, kind, target))
            marked = False
        conn.commit()
    return marked


def load_false_positives(bvid: str) -> set[tuple[str, str]]:
    """加载该视频的全部误报标记：{(kind, target)}"""
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT kind, target FROM false_positive WHERE bvid = ?", (bvid,)).fetchall()
    return {(r["kind"], r["target"]) for r in rows}


# ========== 头像缓存（face_cache，争执焦点关系图节点用） ==========

def load_faces(uids: list[int]) -> dict[int, str]:
    """查询 uid 列表的头像缓存。返回 {uid: face_url}（只含非空头像）；
    无行的 uid 表示从未查过（供补采筛选），有行但 face 为空表示查过但没有头像。"""
    if not uids:
        return {}
    out: dict[int, str] = {}
    with closing(get_db()) as conn:
        for i in range(0, len(uids), 500):
            chunk = uids[i:i + 500]
            qm = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT uid, face FROM face_cache WHERE uid IN ({qm})", chunk):
                if r["face"]:
                    out[r["uid"]] = r["face"]
    return out


def load_face_cached_uids(uids: list[int]) -> set[int]:
    """返回其中已查过（face_cache 有行，无论 face 是否为空）的 uid 集合"""
    if not uids:
        return set()
    out: set[int] = set()
    with closing(get_db()) as conn:
        for i in range(0, len(uids), 500):
            chunk = uids[i:i + 500]
            qm = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT uid FROM face_cache WHERE uid IN ({qm})", chunk):
                out.add(r["uid"])
    return out


def save_face(uid: int, face: str):
    """写入头像缓存（face 可为空串表示查过无头像）"""
    with closing(get_db()) as conn:
        conn.execute("INSERT OR REPLACE INTO face_cache (uid, face, created_at) VALUES (?, ?, ?)",
                     (uid, face or "", int(time.time())))
        conn.commit()


# ========== 缓存清理 ==========

def clear_video_cache(bvid: str):
    """
    清除指定视频的缓存（供 --force 强制重采使用）

    - 删除该 bvid 的全部 senders 记录
    - users 表无 bvid 列，按 uid 关联：仅删除"该 bvid 的 senders 引用、
      且不再被其他 bvid 的 senders 引用"的用户数据，避免误删共享缓存
    - 删除该 bvid 的全部 danmaku 弹幕行
    - 删除该 bvid 的全部 comments 评论行（跨视频足迹数据源）
    - 删除 videos 表中该 bvid 的视频信息记录
    - 删除 llm_cache 中该 bvid 的问题弹幕判定缓存（cringe:{bvid}:*），深掘缓存（deep:*）保留
    """
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT uid FROM senders WHERE bvid = ? AND uid IS NOT NULL", (bvid,))
        uids = [r["uid"] for r in cursor.fetchall()]

        cursor.execute("DELETE FROM senders WHERE bvid = ?", (bvid,))
        cursor.execute("DELETE FROM videos WHERE bvid = ?", (bvid,))
        cursor.execute("DELETE FROM danmaku WHERE bvid = ?", (bvid,))
        cursor.execute("DELETE FROM comments WHERE bvid = ?", (bvid,))
        cursor.execute("DELETE FROM phase_state WHERE bvid = ?", (bvid,))
        # 该视频的问题弹幕/问题评论判定缓存一并清除（key 前缀 cringe:/cmt: + {bvid}:）；
        # 深掘缓存 key 为 deep:{uid}:...，按用户跨视频复用，不清
        cursor.execute("DELETE FROM llm_cache WHERE cache_key LIKE ? ESCAPE '\\'", (f"cringe:{_like_escape(bvid)}:%",))
        cursor.execute("DELETE FROM llm_cache WHERE cache_key LIKE ? ESCAPE '\\'", (f"cmt:{_like_escape(bvid)}:%",))

        for uid in uids:
            cursor.execute("SELECT 1 FROM senders WHERE uid = ? LIMIT 1", (uid,))
            if cursor.fetchone() is None:
                cursor.execute("DELETE FROM users WHERE uid = ?", (uid,))

        conn.commit()


def delete_video_data(bvid: str) -> dict:
    """彻底删除指定视频的全部分析数据（Web 报告页「删除」按钮，spec 9）。

    与 clear_video_cache 的区别——共享缓存一并清除（用户明确选择）：
    - users 画像行无条件删除（即使仍被其他视频的 senders 引用）
    - llm_cache 深掘缓存 deep:{uid}:* 删除
    - global_uid_map 中该视频涉及的 mid_hash 条目删除
    返回各项删除行数 dict。"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT uid, mid_hash FROM senders WHERE bvid = ?", (bvid,))
        rows = cursor.fetchall()
        uids = {r["uid"] for r in rows if r["uid"] is not None}
        mid_hashes = {r["mid_hash"] for r in rows}

        counts = {}
        counts["senders"] = cursor.execute("DELETE FROM senders WHERE bvid = ?", (bvid,)).rowcount
        counts["danmaku"] = cursor.execute("DELETE FROM danmaku WHERE bvid = ?", (bvid,)).rowcount
        counts["comments"] = cursor.execute("DELETE FROM comments WHERE bvid = ?", (bvid,)).rowcount
        counts["false_positive"] = cursor.execute(
            "DELETE FROM false_positive WHERE bvid = ?", (bvid,)).rowcount
        counts["videos"] = cursor.execute("DELETE FROM videos WHERE bvid = ?", (bvid,)).rowcount
        counts["phase_state"] = cursor.execute(
            "DELETE FROM phase_state WHERE bvid = ?", (bvid,)).rowcount
        counts["cringe_cache"] = cursor.execute(
            "DELETE FROM llm_cache WHERE cache_key LIKE ? ESCAPE '\\'",
            (f"cringe:{_like_escape(bvid)}:%",)).rowcount
        counts["cmt_cache"] = cursor.execute(
            "DELETE FROM llm_cache WHERE cache_key LIKE ? ESCAPE '\\'",
            (f"cmt:{_like_escape(bvid)}:%",)).rowcount
        counts["deep_cache"] = 0
        for uid in uids:
            counts["deep_cache"] += cursor.execute(
                "DELETE FROM llm_cache WHERE cache_key LIKE ? ESCAPE '\\'",
                (f"deep:{uid}:%",)).rowcount
        counts["global_uid_map"] = 0
        for mh in mid_hashes:
            counts["global_uid_map"] += cursor.execute(
                "DELETE FROM global_uid_map WHERE mid_hash = ?", (mh,)).rowcount
        counts["users"] = 0
        for uid in uids:
            counts["users"] += cursor.execute("DELETE FROM users WHERE uid = ?", (uid,)).rowcount
        conn.commit()
    return counts


# ========== 全局 mid_hash→UID 映射库（跨视频复用） ==========

def save_global_uid(mid_hash: str, uid: int, source: str):
    """
    upsert 全局映射：新条目 hit_count=1；重复命中 hit_count+1 并刷新 last_seen
    source: 评论区验证 / CRC32破解 / 充电名单 / 互动弹幕 / 视频信息

    冲突覆盖按来源优先级（见 _GLOBAL_UID_SOURCE_PRIORITY）：明文来源 > CRC32破解，
    仅当新来源优先级 >= 旧来源才覆盖 uid/source，防止破解结果被低置信来源反复冲掉；
    先查旧 source 再决定，同一连接事务内完成。
    """
    now = datetime.now().isoformat()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        new_pri = _GLOBAL_UID_SOURCE_PRIORITY.get(source, 0)
        # INSERT OR IGNORE 先行占位：先查后插在并发写（web job × run.py）下会撞主键
        # 抛 IntegrityError，占位后该行必存在，后续只需按来源优先级决定是否覆盖
        cursor.execute(
            "INSERT OR IGNORE INTO global_uid_map (mid_hash, uid, source, first_seen, last_seen, hit_count)"
            " VALUES (?, ?, ?, ?, ?, 0)", (mid_hash, uid, source, now, now))
        row = cursor.execute(
            "SELECT source FROM global_uid_map WHERE mid_hash = ?", (mid_hash,)).fetchone()
        old_pri = _GLOBAL_UID_SOURCE_PRIORITY.get(row["source"], 0)
        if new_pri >= old_pri:
            cursor.execute('''
                UPDATE global_uid_map SET uid=?, source=?, last_seen=?, hit_count=hit_count+1
                WHERE mid_hash=?
            ''', (uid, source, now, mid_hash))
        else:
            # 低优先级来源不覆盖 uid/source，只累计命中并刷新 last_seen
            cursor.execute('''
                UPDATE global_uid_map SET last_seen=?, hit_count=hit_count+1
                WHERE mid_hash=?
            ''', (now, mid_hash))
        conn.commit()


def load_global_uid_map() -> dict:
    """读取全局映射库：{mid_hash: {"uid": int, "source": str, "hit_count": int}}"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT mid_hash, uid, source, hit_count FROM global_uid_map")
        rows = cursor.fetchall()
    return {r["mid_hash"]: {"uid": r["uid"], "source": r["source"], "hit_count": r["hit_count"]}
            for r in rows}


# ========== LLM 结果缓存（省 token：重跑同视频/同证据包零调用） ==========

def load_llm_cache(cache_key: str) -> str | None:
    """读取 LLM 缓存；不存在或读取异常均返回 None（视为未命中，不中断流水线）"""
    try:
        with closing(get_db()) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT result_json FROM llm_cache WHERE cache_key = ?", (cache_key,))
            row = cursor.fetchone()
        return row["result_json"] if row else None
    except Exception:
        return None


def save_llm_cache(cache_key: str, result_json: str) -> bool:
    """写入 LLM 缓存，返回是否成功；失败只告警不中断主流程（调用方按返回值决定是否提示）"""
    try:
        with closing(get_db()) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO llm_cache (cache_key, result_json, created_at)
                VALUES (?, ?, ?)
            ''', (cache_key, result_json, datetime.now().isoformat()))
            conn.commit()
        return True
    except Exception as e:
        print(f"[Storage] 警告: LLM 缓存写入失败（key={cache_key[:40]}，{e}），"
              f"缓存未写入，重跑将重复消耗 LLM token")
        return False
