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
from uid_evidence import PLAINTEXT_SOURCES, crc32_uid, candidates_of

# uid/content 为空的评论被 save_comments 静默丢弃时的累计计数（debug 统计用）
_dropped_comment_count = 0



def _like_escape(s: str) -> str:
    """LIKE 模式串转义（配合 ESCAPE '\\'）：\\ % _ 前置反斜杠，防 bvid 含通配符误匹配"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _migrate_col(cursor, col: str, existing: set, ddl: str):
    """旧库补列迁移：两进程（web 常驻 + 新起 run.py）并发首迁同一旧库时，
    PRAGMA 检查与 ALTER 之间无锁，后执行者会撞 duplicate column name——
    容忍该错误（说明另一进程已把列补好），其余异常正常上抛。"""
    if col in existing:
        return
    try:
        cursor.execute(ddl)
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


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
        # uid 索引：跨视频重叠面板/低置信度页/global 沉淀判重按 uid 查 senders
        # （无索引则每次全表扫；clear_video_cache 的逐 uid 判重是 O(uids×N)）
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_senders_uid ON senders(uid)")
        sender_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(senders)")}
        _migrate_col(cursor, "resolution_json", sender_cols,
                     "ALTER TABLE senders ADD COLUMN resolution_json TEXT NOT NULL DEFAULT '{}'")

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

        # 全局 mid_hash→UID 映射表（跨视频沉淀复用；正常只增——删除视频报告时
        # delete_video_data 会连带删除该视频涉及的映射（用户明确选择"彻底删除"，
        # 含身份痕迹），跨视频解析率的小幅损失属可接受的代价）
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
        # 旧单值表保留作审计；新证据按 UID、来源、视频保存，不覆盖碰撞候选。
        cursor.execute('''CREATE TABLE IF NOT EXISTS uid_evidence (
            mid_hash TEXT NOT NULL, uid INTEGER NOT NULL, source TEXT NOT NULL,
            bvid TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(mid_hash, uid, source, bvid))''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_uid_evidence_video ON uid_evidence(bvid)")
        cursor.execute('''CREATE TABLE IF NOT EXISTS uid_verify_cache (
            uid INTEGER PRIMARY KEY, status TEXT NOT NULL, info_json TEXT NOT NULL,
            verified_at REAL NOT NULL)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS uid_space_cache (
            uid INTEGER PRIMARY KEY, sample_json TEXT NOT NULL)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS uid_candidate_cache (
            mid_hash TEXT NOT NULL, max_uid INTEGER NOT NULL, version TEXT NOT NULL,
            candidates_json TEXT NOT NULL, PRIMARY KEY(mid_hash, max_uid, version))''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS uid_harvest_seen (
            bvid TEXT NOT NULL, rpid INTEGER NOT NULL, PRIMARY KEY(bvid, rpid))''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS uid_schema_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)''')

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

        # 旧库迁移：danmaku 表补 mode/color/pool/dmid/page 列（弹幕属性统计：滚动占比/颜色分布/顶底弹幕；
        # page 为分P序号，断点续采从库读回后 group_by_sender 要用）
        dm_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(danmaku)").fetchall()}
        for col, ddl in (
            ("mode", "ALTER TABLE danmaku ADD COLUMN mode INTEGER NOT NULL DEFAULT 1"),
            ("color", "ALTER TABLE danmaku ADD COLUMN color TEXT NOT NULL DEFAULT ''"),
            ("pool", "ALTER TABLE danmaku ADD COLUMN pool INTEGER NOT NULL DEFAULT 0"),
            ("dmid", "ALTER TABLE danmaku ADD COLUMN dmid INTEGER NOT NULL DEFAULT 0"),
            ("page", "ALTER TABLE danmaku ADD COLUMN page INTEGER NOT NULL DEFAULT 1"),
        ):
            _migrate_col(cursor, col, dm_cols, ddl)

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

        # 旧库迁移：comments 表补 reply_count / root_rpid / parent_rpid / problem /
        # uname / location 列（高回复评论功能 + 回复树 + LLM 问题标注 + IP属地）
        comment_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(comments)").fetchall()}
        for col, ddl in (
            ("reply_count", "ALTER TABLE comments ADD COLUMN reply_count INTEGER NOT NULL DEFAULT 0"),
            ("root_rpid", "ALTER TABLE comments ADD COLUMN root_rpid INTEGER NOT NULL DEFAULT 0"),
            ("parent_rpid", "ALTER TABLE comments ADD COLUMN parent_rpid INTEGER NOT NULL DEFAULT 0"),
            ("problem", "ALTER TABLE comments ADD COLUMN problem TEXT NOT NULL DEFAULT ''"),
            ("uname", "ALTER TABLE comments ADD COLUMN uname TEXT NOT NULL DEFAULT ''"),
            ("location", "ALTER TABLE comments ADD COLUMN location TEXT NOT NULL DEFAULT ''"),
            ("problem_severity", "ALTER TABLE comments ADD COLUMN problem_severity INTEGER NOT NULL DEFAULT 0"),
        ):
            _migrate_col(cursor, col, comment_cols, ddl)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_bvid_problem ON comments(bvid, problem)")
        if not cursor.execute("SELECT 1 FROM uid_schema_state WHERE key='evidence_v1'").fetchone():
            conn.create_function("uid_crc32", 1, crc32_uid)
            now = datetime.now().isoformat()
            sources = sorted(PLAINTEXT_SOURCES)
            cursor.execute(f'''INSERT OR IGNORE INTO uid_evidence
                SELECT LOWER(mid_hash), uid, source, '', first_seen, last_seen, hit_count
                FROM global_uid_map WHERE source IN ({','.join('?' for _ in sources)})
                AND uid > 0 AND LOWER(mid_hash)=uid_crc32(uid)''', sources)
            cursor.execute('''INSERT OR IGNORE INTO uid_evidence
                SELECT DISTINCT uid_crc32(uid), uid, '评论区验证', bvid, ?, ?, 1
                FROM comments WHERE uid > 0''', (now, now))
            cursor.execute("INSERT OR IGNORE INTO uid_schema_state VALUES('evidence_v1', '1')")

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

        # 基础采集按 UID 共享；弹幕、评论和 AI 证据必须按视频隔离。
        cursor.execute('''CREATE TABLE IF NOT EXISTS video_profiles (
            bvid TEXT NOT NULL, uid INTEGER NOT NULL, profile_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(bvid, uid))''')
        # 目标分析独立存档；匹配条件和画像均不写入常规报告的 senders/video_profiles。
        cursor.execute('''CREATE TABLE IF NOT EXISTS target_analyses (
            id TEXT PRIMARY KEY, bvid TEXT NOT NULL, selector_json TEXT NOT NULL,
            result_json TEXT NOT NULL, summary_json TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        cursor.execute("CREATE INDEX IF NOT EXISTS target_analyses_video ON target_analyses(bvid, updated_at)")
        # 单视频旧画像可直接迁移；多视频旧画像无法确定归属，读取时用本视频证据重建。
        cursor.execute('''INSERT OR IGNORE INTO video_profiles(bvid, uid, profile_json)
            SELECT MIN(s.bvid), u.uid, u.profile_json FROM users u JOIN senders s ON s.uid=u.uid
            WHERE u.profile_json NOT IN ('', '{}') GROUP BY u.uid HAVING COUNT(DISTINCT s.bvid)=1
            AND NOT EXISTS(SELECT 1 FROM video_profiles p WHERE p.uid=u.uid)''')
        # 数据修订号也覆盖同条数更新，避免热度、UID、头像和深掘变更命中旧页面。
        cursor.execute('''CREATE TABLE IF NOT EXISTS data_revision (
            scope TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)''')
        for table in ("videos", "senders", "users", "video_profiles", "target_analyses", "comments", "danmaku", "false_positive", "face_cache"):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                ref = "OLD" if operation == "DELETE" else "NEW"
                scope = f"{ref}.bvid" if table not in ("users", "face_cache") else "'*'"
                cursor.execute(f'''CREATE TRIGGER IF NOT EXISTS revision_{table}_{operation}
                    AFTER {operation} ON {table} BEGIN
                    INSERT INTO data_revision(scope, revision) VALUES({scope}, 1)
                    ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
                    END''')
        conn.commit()

        # 清理旧版本的 progress 表（断点续采已改为纯 senders/users 缓存机制）
        cursor.execute("DROP TABLE IF EXISTS progress")
        conn.commit()
    print("[Storage] 数据库初始化完成")


def save_target_analysis(target_id: str, bvid: str, selector: dict, result: dict) -> None:
    """原子保存目标分析快照；列表只读摘要，不加载全部命中弹幕。"""
    summary = {"title": result.get("video_info", {}).get("title", bvid),
               "status": result.get("status", "running"), **result.get("stats", {})}
    with closing(get_db()) as conn:
        conn.execute('''INSERT INTO target_analyses VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET selector_json=excluded.selector_json,
            result_json=excluded.result_json, summary_json=excluded.summary_json,
            updated_at=excluded.updated_at''',
            (target_id, bvid, json.dumps(selector, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False, default=str), json.dumps(summary, ensure_ascii=False),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()


def load_target_analysis(target_id: str) -> dict | None:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM target_analyses WHERE id=?", (target_id,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "bvid": row["bvid"], "selector": json.loads(row["selector_json"]),
            "result": json.loads(row["result_json"]), "updated_at": row["updated_at"]}


def load_uid_space_cache(uids: list[int]) -> dict[int, dict]:
    """独立的公开空间样本缓存，不作为 UID 明文证据或完整画像。"""
    result = {}
    values = sorted(candidates_of(uids))
    with closing(get_db()) as conn:
        for start in range(0, len(values), 500):
            chunk = values[start:start + 500]
            rows = conn.execute(f"SELECT uid, sample_json FROM uid_space_cache WHERE uid IN ({','.join('?' for _ in chunk)})",
                                chunk).fetchall()
            for row in rows:
                try:
                    sample = json.loads(row["sample_json"])
                    if isinstance(sample, dict) and sample.get("uid") == row["uid"]:
                        result[row["uid"]] = sample
                except (ValueError, TypeError):
                    continue
    return result


def save_uid_space_cache(uid: int, sample: dict):
    """只保存辅助核验样本；不改 users、senders 或可靠 UID 映射。"""
    if sample.get("uid") != uid:
        raise ValueError("空间样本 UID 不符")
    with closing(get_db()) as conn:
        conn.execute("INSERT OR REPLACE INTO uid_space_cache VALUES (?, ?)",
                     (uid, json.dumps(sample, ensure_ascii=False)))
        conn.commit()


def list_target_analyses(bvid: str | None = None, limit: int = 50) -> list[dict]:
    with closing(get_db()) as conn:
        rows = conn.execute('''SELECT id, bvid, selector_json, summary_json, updated_at
            FROM target_analyses WHERE (? IS NULL OR bvid=?) ORDER BY updated_at DESC, id LIMIT ?''',
            (bvid, bvid, max(1, limit))).fetchall()
    return [{"id": r["id"], "bvid": r["bvid"], "selector": json.loads(r["selector_json"]),
             "summary": json.loads(r["summary_json"]), "updated_at": r["updated_at"]} for r in rows]


# videos 中也保存目标分析共用的元信息与弹幕缓存，不能据此声称已有常规报告。
# 兼容既有目标快照：常规发送者/画像/评论记录或完成标记才代表另有常规分析。
NORMAL_REPORT_PREDICATE = """(
    NOT EXISTS (SELECT 1 FROM target_analyses t WHERE t.bvid=v.bvid)
    OR EXISTS (SELECT 1 FROM senders s WHERE s.bvid=v.bvid)
    OR EXISTS (SELECT 1 FROM video_profiles p WHERE p.bvid=v.bvid)
    OR EXISTS (SELECT 1 FROM comments c WHERE c.bvid=v.bvid)
    OR EXISTS (SELECT 1 FROM phase_state ps WHERE ps.bvid=v.bvid
               AND ps.phase='report' AND ps.key='ready' AND ps.value='1')
)"""


def has_normal_report(bvid: str) -> bool:
    with closing(get_db()) as conn:
        return conn.execute(f"SELECT 1 FROM videos v WHERE v.bvid=? AND {NORMAL_REPORT_PREDICATE}",
                            (bvid,)).fetchone() is not None


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
                spam_level: str, spam_score: float, resolution: dict | None = None):
    """保存发送者解析结果"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO senders
            (bvid, mid_hash, uid, confidence, method, danmaku_count, contents_json, spam_level, spam_score, resolution_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bvid, mid_hash) DO UPDATE SET
                uid=excluded.uid, confidence=excluded.confidence, method=excluded.method,
                danmaku_count=excluded.danmaku_count, contents_json=excluded.contents_json,
                spam_level=excluded.spam_level, spam_score=excluded.spam_score,
                resolution_json=excluded.resolution_json
        ''', (bvid, mid_hash, uid, confidence, method, danmaku_count,
              json.dumps(contents, ensure_ascii=False), spam_level, spam_score,
              json.dumps(resolution or {}, ensure_ascii=False)))
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
            SELECT mid_hash, uid, confidence, method, danmaku_count, contents_json, spam_level, spam_score, resolution_json
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
        "resolution": json.loads(r["resolution_json"] or "{}"),
    } for r in rows]


# ========== 用户数据 ==========

def _put_video_profile(conn, bvid: str, uid: int, profile: dict):
    profile = dict(profile, _bvid=bvid)
    conn.execute('''INSERT INTO video_profiles(bvid, uid, profile_json) VALUES(?, ?, ?)
        ON CONFLICT(bvid, uid) DO UPDATE SET profile_json=excluded.profile_json,
        revision=video_profiles.revision+1''', (bvid, uid, json.dumps(profile, ensure_ascii=False)))


def save_user_data(uid: int, name: str, level: int, user_data: dict, profile: dict,
                   bvid: str | None = None):
    """共享基础采集数据，画像按视频保存；空画像不覆盖既有画像。"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO users (uid, name, level, data_json, profile_json, collected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET name=excluded.name, level=excluded.level,
                data_json=excluded.data_json, collected_at=excluded.collected_at,
                profile_json=CASE WHEN excluded.profile_json='{}' THEN users.profile_json
                    ELSE excluded.profile_json END
        ''', (uid, name, level,
              json.dumps(user_data, ensure_ascii=False),
              json.dumps(profile, ensure_ascii=False),
              datetime.now().isoformat()))
        if profile:
            # 兼容旧调用：只有单视频引用才能推断归属，绝不向多个视频复制证据。
            if not bvid:
                refs = conn.execute("SELECT DISTINCT bvid FROM senders WHERE uid=?", (uid,)).fetchall()
                bvid = refs[0][0] if len(refs) == 1 else None
            if bvid:
                _put_video_profile(conn, bvid, uid, profile)
        conn.commit()


def update_user_profile(uid: int, profile: dict, bvid: str | None = None):
    """回写指定视频画像并递增修订号；基础采集时间保持不变。"""
    with closing(get_db()) as conn:
        if not bvid:
            refs = conn.execute("SELECT DISTINCT bvid FROM senders WHERE uid=?", (uid,)).fetchall()
            bvid = refs[0][0] if len(refs) == 1 else None
        if not bvid:
            raise ValueError("更新画像必须指定所属视频")
        _put_video_profile(conn, bvid, uid, profile)
        conn.execute("UPDATE users SET profile_json = ? WHERE uid = ?",
                     (json.dumps(profile, ensure_ascii=False), uid))
        conn.commit()


def load_video_profile(bvid: str, uid: int) -> dict | None:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT profile_json FROM video_profiles WHERE bvid=? AND uid=?",
                           (bvid, uid)).fetchone()
    return json.loads(row[0]) if row else None


def ensure_video_profiles(bvid: str):
    """为旧库缺少归属的画像重建本视频证据，不复用归属不明的 AI 结果。"""
    from profile_analyzer import analyze_profile
    with closing(get_db()) as conn:
        missing = conn.execute('''SELECT s.*, u.data_json, u.name AS collected_name,
            u.level AS collected_level FROM senders s
            JOIN users u ON u.uid=s.uid LEFT JOIN video_profiles p ON p.bvid=s.bvid AND p.uid=s.uid
            WHERE s.bvid=? AND p.uid IS NULL GROUP BY s.uid''', (bvid,)).fetchall()
    if not missing:
        return
    dms = load_danmaku(bvid)
    comments = load_comments(bvid)
    video = load_video_info(bvid) or {}
    dm_by_hash, comments_by_uid = {}, {}
    for d in dms:
        dm_by_hash.setdefault(d["mid_hash"], []).append(d)
    for c in comments:
        comments_by_uid.setdefault(c["uid"], []).append(c)
    # 完整判定缓存有视频前缀，可恢复该视频证据；整轮只读一次，无网络调用。
    cringe_by_hash = {}
    with closing(get_db()) as conn:
        cached = conn.execute("SELECT result_json FROM llm_cache WHERE cache_key LIKE ? ESCAPE '\\' "
            "AND cache_key NOT LIKE '%@batch:%' ORDER BY created_at DESC",
            (f"cringe:{_like_escape(bvid)}:%",)).fetchall()
    for cr in cached:
        try:
            for h, val in json.loads(cr[0]).items():
                if isinstance(val, dict):
                    cringe_by_hash.setdefault(h, val)
        except (ValueError, AttributeError, TypeError):
            continue
    for row in missing:
        raw = json.loads(row["data_json"])
        raw.update(uid=row["uid"])
        raw.setdefault("name", row["collected_name"] or "未知")
        raw.setdefault("level", row["collected_level"] or 0)
        raw.pop("ip_location", None)
        mine = comments_by_uid.get(row["uid"], [])
        if mine:
            raw["ip_location"] = next((c["location"] for c in mine if c.get("location")), "")
        dm = dm_by_hash.get(row["mid_hash"], [])
        contents = [d["content"] for d in dm] or json.loads(row["contents_json"] or "[]")
        p = analyze_profile(raw, {"count": row["danmaku_count"], "contents": contents,
            "video_times": [d["time"] for d in dm], "video_pages": [d.get("page", 1) for d in dm],
            "multi_page": len(video.get("pages") or []) > 1},
            {"spam_level": row["spam_level"], "spam_score": row["spam_score"]})
        p["comments"] = sorted(mine, key=lambda c: -c["like"])[:10]
        p["cringe"] = cringe_by_hash.get(row["mid_hash"], {})
        p["collision_risk"] = json.loads(row["resolution_json"] or "{}").get("collision_risk", row["method"] == "CRC32破解")
        problem = [c for c in mine if c["problem"]]
        if problem:
            p["comment_problem"] = {"hits": len(problem),
                "max_severity": max(c.get("problem_severity") or 1 for c in problem),
                "categories": list(dict.fromkeys(c["problem"] for c in problem))}
        with closing(get_db()) as conn:
            _put_video_profile(conn, bvid, row["uid"], p)
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
    new_dmids = set()
    for dm in danmaku_list:
        dmid = dm.get("dmid", 0)
        if dmid and (dmid in seen_dmids or dmid in new_dmids):
            continue
        if dmid:
            new_dmids.add(dmid)
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
    seen_dmids.update(new_dmids)
    return len(rows)


def save_comments(bvid: str, comments: list[dict], checkpoint: dict | None = None):
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
    if not rows and not checkpoint:
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
        for uid in {int(row[1]) for row in rows if int(row[1]) > 0}:
            _put_uid_evidence(conn, crc32_uid(uid), uid, "评论区验证", bvid)
        if checkpoint:
            conn.executemany("INSERT INTO phase_state(bvid, phase, key, value) VALUES(?, 'comment', ?, ?) "
                "ON CONFLICT(bvid, phase, key) DO UPDATE SET value=excluded.value",
                [(bvid, key, str(value)) for key, value in checkpoint.items()])
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


def load_comments(bvid: str, uids: list[int] | None = None) -> list[dict]:
    """从库读回某视频的全部评论（断点续采用：跳过阶段3网络重采）。
    返回与采集侧同形的 dict 列表（problem 列带出 LLM 标注；location 旧行可能为空）。"""
    with closing(get_db()) as conn:
        query = (
            "SELECT uid, uname, rpid, content, ctime, like, is_sub, reply_count, "
            "root_rpid, parent_rpid, problem, problem_severity, location "
            "FROM comments WHERE bvid = ?")
        if uids is None:
            rows = conn.execute(query, (bvid,)).fetchall()
        else:
            # 目标模式只加载已关联发送者的正文；按 SQLite 实际变量上限分块。
            wanted = sorted(set(uids))
            chunk_size = max(1, conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - 1)
            rows = []
            for offset in range(0, len(wanted), chunk_size):
                chunk = wanted[offset:offset + chunk_size]
                rows.extend(conn.execute(query + f" AND uid IN ({','.join('?' for _ in chunk)})",
                                         (bvid, *chunk)).fetchall())
    return [{"uid": r["uid"], "uname": r["uname"], "rpid": r["rpid"],
             "content": r["content"], "ctime": r["ctime"], "like": r["like"],
             "is_sub": bool(r["is_sub"]), "reply_count": r["reply_count"],
             "root_rpid": r["root_rpid"], "parent_rpid": r["parent_rpid"],
             "problem": r["problem"], "problem_severity": r["problem_severity"],
             "location": r["location"]} for r in rows]


def load_comment_uids(bvid: str) -> set[int]:
    """该视频已入库评论的 uid 集合（UID 收割的已知集合）。只取 uid 列——
    大评论区（数万行）全列拉取（含正文数 MB）纯属浪费。"""
    with closing(get_db()) as conn:
        return {r[0] for r in conn.execute(
            "SELECT uid FROM comments WHERE bvid = ?", (bvid,))}


def update_comment_problems(bvid: str, verdicts: dict, judged_rpids=None):
    """回写类别和严重度，只替换成功判定范围；失败和截断范围保留旧标注。

    幂等可重跑；rpid 在库中不存在（评论被删/未落库）时 UPDATE 命中 0 行，静默跳过。
    """
    if judged_rpids is None:
        judged_rpids = getattr(verdicts, "judged_rpids", None)
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        if judged_rpids is None:
            cursor.execute("UPDATE comments SET problem = '', problem_severity=0 WHERE bvid = ?", (bvid,))
        else:
            cursor.executemany("UPDATE comments SET problem='', problem_severity=0 WHERE bvid=? AND rpid=?",
                               [(bvid, rpid) for rpid in judged_rpids])
        for rpid, verdict in verdicts.items():
            category = verdict["category"] if isinstance(verdict, dict) else verdict
            severity = verdict.get("severity", 1) if isinstance(verdict, dict) else 1
            cursor.execute("UPDATE comments SET problem = ?, problem_severity=? WHERE bvid = ? AND rpid = ?",
                           (category, severity, bvid, rpid))
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
        cursor.execute("DELETE FROM uid_harvest_seen WHERE bvid = ?", (bvid,))
        cursor.execute("DELETE FROM video_profiles WHERE bvid = ?", (bvid,))
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

        # 目标分析的低置信度候选不在普通 senders 中，删除时也清除其空间样本。
        space_uids = set(uids)
        for row in cursor.execute("SELECT result_json FROM target_analyses WHERE bvid=?", (bvid,)).fetchall():
            try:
                snapshot = json.loads(row["result_json"])
                for sender in snapshot.get("senders", []):
                    space_uids |= candidates_of(sender.get("uid"))
                    space_uids |= candidates_of(sender.get("candidates", []))
                    space_uids |= candidates_of(sender.get("resolution", {}).get("candidates", []))
            except (ValueError, TypeError, AttributeError):
                continue

        counts = {}
        counts["senders"] = cursor.execute("DELETE FROM senders WHERE bvid = ?", (bvid,)).rowcount
        counts["video_profiles"] = cursor.execute("DELETE FROM video_profiles WHERE bvid = ?", (bvid,)).rowcount
        counts["target_analyses"] = cursor.execute("DELETE FROM target_analyses WHERE bvid = ?", (bvid,)).rowcount
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
        counts["uid_evidence"] = cursor.execute("DELETE FROM uid_evidence WHERE bvid=?", (bvid,)).rowcount
        cursor.execute("DELETE FROM uid_harvest_seen WHERE bvid=?", (bvid,))
        counts["global_uid_map"] = 0
        for mh in mid_hashes:
            counts["global_uid_map"] += cursor.execute(
                "DELETE FROM global_uid_map WHERE mid_hash = ?", (mh,)).rowcount
            counts["uid_evidence"] += cursor.execute(
                "DELETE FROM uid_evidence WHERE mid_hash=? AND bvid=''", (mh,)).rowcount
        counts["users"] = 0
        for uid in uids:
            counts["users"] += cursor.execute("DELETE FROM users WHERE uid = ?", (uid,)).rowcount
        counts["uid_space_cache"] = 0
        for uid in space_uids:
            counts["uid_space_cache"] += cursor.execute("DELETE FROM uid_space_cache WHERE uid=?", (uid,)).rowcount
        conn.commit()
    return counts


# ========== 全局 mid_hash→UID 映射库（跨视频复用） ==========

def _put_uid_evidence(conn, mid_hash: str, uid: int, source: str, bvid: str = ""):
    """只记录校验相符的明文 UID；同一 hash 的其他候选绝不覆盖。"""
    if source not in PLAINTEXT_SOURCES or uid <= 0 or crc32_uid(uid) != mid_hash:
        return
    now = datetime.now().isoformat()
    conn.execute("INSERT INTO uid_evidence VALUES(?, ?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(mid_hash, uid, source, bvid) DO UPDATE SET "
        "last_seen=excluded.last_seen, hit_count=uid_evidence.hit_count+1",
        (mid_hash, uid, source, bvid, now, now))


def save_global_uid(mid_hash: str, uid: int, source: str, bvid: str = ""):
    """保存候选来源与视频；纯 CRC32 破解及未知来源不进入明文证据库。"""
    with closing(get_db()) as conn:
        _put_uid_evidence(conn, mid_hash, uid, source, bvid)
        conn.commit()


def save_uid_evidence_maps(bvid: str, sources: list[tuple[str, dict]]):
    """一批明文来源原子入库，避免逐候选新建连接。"""
    with closing(get_db()) as conn:
        for source, mapping in sources:
            for h, value in mapping.items():
                for uid in candidates_of(value):
                    _put_uid_evidence(conn, h, uid, source, bvid)
        conn.commit()


def load_global_uid_map() -> dict:
    """返回多候选证据；旧 CRC32 破解记录仅留在旧表审计，不参与解析。"""
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT * FROM uid_evidence ORDER BY mid_hash, uid, source, bvid").fetchall()
    result = {}
    for row in rows:
        h, uid = row["mid_hash"], row["uid"]
        if row["source"] not in PLAINTEXT_SOURCES or uid <= 0 or crc32_uid(uid) != h:
            continue
        entry = result.setdefault(h, {"uids": [], "evidence": [], "hit_count": 0})
        if uid not in entry["uids"]:
            entry["uids"].append(uid)
        entry["evidence"].append({"uid": uid, "source": row["source"], "bvid": row["bvid"]})
        entry["hit_count"] += row["hit_count"]
    for entry in result.values():
        entry["uid"] = entry["uids"][0] if len(entry["uids"]) == 1 else None
        entry["source"] = entry["evidence"][0]["source"] if entry["uid"] else "明文冲突"
    return result


def load_uid_verify_cache(uids: list[int], exists_ttl: float, missing_ttl: float) -> dict:
    """存在性仅缓存有效结论；过期、未知和损坏记录均重新验证。"""
    result, now = {}, time.time()
    with closing(get_db()) as conn:
        for start in range(0, len(uids), 500):
            chunk = uids[start:start + 500]
            rows = conn.execute(f"SELECT * FROM uid_verify_cache WHERE uid IN ({','.join('?' for _ in chunk)})",
                                chunk).fetchall()
            for row in rows:
                ttl = exists_ttl if row["status"] == "exists" else missing_ttl
                if row["status"] not in ("exists", "not_exists") or not 0 <= now - row["verified_at"] < ttl:
                    continue
                try:
                    info = json.loads(row["info_json"])
                except (TypeError, ValueError):
                    continue
                if isinstance(info, dict) and (row["status"] != "exists" or info.get("uid") == row["uid"]):
                    result[row["uid"]] = (row["status"], info)
    return result


def save_uid_verify_cache(uid: int, status: str, info: dict):
    if status not in ("exists", "not_exists"):
        return
    with closing(get_db()) as conn:
        conn.execute("INSERT INTO uid_verify_cache VALUES(?, ?, ?, ?) "
            "ON CONFLICT(uid) DO UPDATE SET status=excluded.status, info_json=excluded.info_json, "
            "verified_at=excluded.verified_at", (uid, status, json.dumps(info, ensure_ascii=False), time.time()))
        conn.commit()


def load_uid_candidates(mid_hash: str, max_uid: int, version: str) -> list[int] | None:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT candidates_json FROM uid_candidate_cache "
                           "WHERE mid_hash=? AND max_uid=? AND version=?", (mid_hash, max_uid, version)).fetchone()
    if not row:
        return None
    try:
        values = json.loads(row[0])
        if isinstance(values, list) and all(isinstance(u, int) and 0 < u <= max_uid and crc32_uid(u) == mid_hash for u in values):
            return sorted(set(values))
    except (TypeError, ValueError):
        pass
    return None


def save_uid_candidates(mid_hash: str, max_uid: int, version: str, candidates: list[int]):
    with closing(get_db()) as conn:
        conn.execute("INSERT OR REPLACE INTO uid_candidate_cache VALUES(?, ?, ?, ?)",
                     (mid_hash, max_uid, version, json.dumps(candidates)))
        conn.commit()


def load_uid_harvest_seen(bvid: str) -> set[int]:
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT rpid FROM uid_harvest_seen WHERE bvid=? UNION "
                            "SELECT rpid FROM comments WHERE bvid=? AND rpid>0", (bvid, bvid)).fetchall()
    return {r[0] for r in rows}


def save_uid_harvest_page(bvid: str, uids: set[int], rpids: set[int], checkpoint: dict, phase: str = "uid_harvest"):
    """UID 证据、该视频已见评论及游标一页原子提交，防止续采跳页。"""
    with closing(get_db()) as conn:
        for uid in uids:
            _put_uid_evidence(conn, crc32_uid(uid), uid, "评论收割", bvid)
        conn.executemany("INSERT OR IGNORE INTO uid_harvest_seen VALUES(?, ?)", [(bvid, r) for r in rpids])
        conn.executemany("INSERT INTO phase_state VALUES(?, ?, ?, ?) "
            "ON CONFLICT(bvid, phase, key) DO UPDATE SET value=excluded.value",
            [(bvid, phase, k, str(v)) for k, v in checkpoint.items()])
        conn.commit()


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
