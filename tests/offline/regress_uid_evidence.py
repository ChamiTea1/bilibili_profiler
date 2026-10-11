#!/usr/bin/env python3
"""UID 证据、歧义、缓存和有预算补采回归；隔离数据库并禁止真实网络。"""
import contextlib
import io
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
import config
import storage
import requests
import uid_resolver as ur
import uid_evidence as ue
import comment
import danmaku
import main
import web

TMP = tempfile.TemporaryDirectory(prefix='bili_uid_evidence_')
requests.sessions.Session.request = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('禁止真实网络'))
A, B = 12345678, 7533489035
H = ue.crc32_uid(A)
ok = fail = 0


def case(name):
    def decorator(fn):
        global ok, fail
        storage.DB_PATH = config.DB_PATH = str(Path(TMP.name) / f'{ok + fail}.db')
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                storage.init_db()
                fn()
            ok += 1
            print(f'  ✔ {name}')
        except Exception as exc:
            fail += 1
            print(f'  ✘ {name}: {type(exc).__name__}: {exc}')
        return fn
    return decorator


def client(fn):
    calls = []
    def get(url, params=None, **kw):
        calls.append((url, params))
        return fn(url, params)
    return SimpleNamespace(get=get, calls=calls)


def fake_cards(existing=(), unknown=()):
    def response(url, params):
        if url == ur.USER_CARDS_BATCH_URL:
            return {'code': 0, 'data': {str(u): {'name': '测试'} for u in map(int, params['uids'].split(',')) if u in existing}}
        uid = params['mid']
        return {'code': -500} if uid in unknown else {'code': 0, 'data': {'card': {'mid': uid}}} if uid in existing else {'code': -404}
    return client(response)


def group(h=H, n=2):
    return {h: {'mid_hash': h, 'count': n, 'contents': ['测试'] * n, 'timestamps': [1] * n,
                'video_times': [1] * n, 'colors': ['#ffffff'] * n, 'pages': [1] * n}}


def raw(rpid, uid, children=None):
    return {'rpid': rpid, 'member': {'mid': str(uid)}, 'replies': children or []}


@case('已知 CRC32 碰撞及明文映射同时保留两名用户')
def collision_map():
    assert H == ue.crc32_uid(B)
    assert comment.build_comment_uid_map([{'uid': A}, {'uid': B}])[H] == {A, B}
    assert danmaku.build_command_uid_map([{'mid': A}, {'mid': B}])[H] == {A, B}
    assert main.build_video_meta_uid_map({'owner': {'mid': A}, 'staff': [{'mid': B}]})[H] == {A, B}


@case('同 UID 多来源及不同视频证据分别持久化')
def evidence_context():
    storage.save_global_uid(H, A, '评论区验证', 'BV甲')
    storage.save_global_uid(H, A, '视频信息', 'BV乙')
    entry = storage.load_global_uid_map()[H]
    assert entry['uids'] == [A] and {e['bvid'] for e in entry['evidence']} == {'BV甲', 'BV乙'}


@case('不同明文 UID 冲突不覆盖任何候选')
def global_collision():
    for u in (A, B):
        storage.save_global_uid(H, u, '评论收割', 'BV甲')
    entry = storage.load_global_uid_map()[H]
    assert entry['uid'] is None and set(entry['uids']) == {A, B}


@case('旧库迁移保留明文候选并隔离纯 CRC 破解审计记录')
def legacy_migration():
    path = str(Path(TMP.name) / 'legacy.db')
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE global_uid_map(mid_hash TEXT PRIMARY KEY,uid INTEGER,source TEXT,first_seen TEXT,last_seen TEXT,hit_count INTEGER)')
        conn.executemany('INSERT INTO global_uid_map VALUES(?,?,?,?,?,?)', [(H, A, 'CRC32破解', '旧', '旧', 1), (ue.crc32_uid(9), 9, '评论区验证', '旧', '旧', 1)])
        conn.execute('CREATE TABLE comments(id INTEGER PRIMARY KEY,bvid TEXT,uid INTEGER,rpid INTEGER,content TEXT,ctime INTEGER,like INTEGER,is_sub INTEGER,UNIQUE(bvid,rpid,uid))')
        conn.execute('INSERT INTO comments VALUES(1,?, ?, 1, ?,0,0,0)', ('BV旧', B, '旧评论'))
    with patch.object(storage, 'DB_PATH', path):
        storage.init_db()
        storage.init_db()
        entry = storage.load_global_uid_map()[H]
        assert entry['uids'] == [B] and entry['evidence'][0]['bvid'] == 'BV旧'
        with storage.get_db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM global_uid_map WHERE source='CRC32破解'").fetchone()[0] == 1
        assert storage.load_global_uid_map()[ue.crc32_uid(9)]['uid'] == 9


@case('新增纯破解或脏 hash 不写明文证据库')
def reject_unsafe():
    storage.save_global_uid(H, A, 'CRC32破解', 'BV甲')
    storage.save_global_uid('ffffffff', A, '评论区验证', 'BV甲')
    assert storage.load_global_uid_map() == {}


@case('评论与游标原子落库时同时保存 UID 来源')
def comment_atomic():
    storage.save_comments('BV甲', [{'uid': A, 'rpid': 1, 'content': '测试'}], checkpoint={'page': 1})
    assert storage.load_global_uid_map()[H]['evidence'][0]['bvid'] == 'BV甲'
    assert storage.get_phase_state('BV甲', 'comment', 'page') == '1'


@case('本视频明文关联不随弹幕条数升级且始终保留碰撞提示')
def count_not_evidence():
    for count in (1, 2, 100):
        out = ur.resolve_sender(H, ['测试'] * count, {H: A}, None)
        assert out[:3] == (A, '中', '评论区验证') and out[4]


@case('仅跨视频候选压为低置信度')
def global_only():
    storage.save_global_uid(H, A, '评论区验证', 'BV其他')
    mapping, methods = ur.build_evidence_maps('BV本', [], storage.load_global_uid_map())
    out = ur.resolve_sender(H, ['测试'], mapping, None, method_map=methods)
    assert out[0] == A and out[1] == '低' and out[4]


@case('本视频与跨视频明文碰撞保留歧义而非覆盖')
def local_global_conflict():
    storage.save_global_uid(H, A, '评论区验证', 'BV其他')
    mapping, methods = ur.build_evidence_maps('BV本', [('视频信息', {H: B})], storage.load_global_uid_map())
    out = ur.resolve_sender(H, ['测试'], mapping, None, method_map=methods)
    assert out[0] is None and out[2] == ur.METHOD_AMBIGUOUS and set(out[-1]) == {A, B}


@case('旧破解映射即使直接传入也不能冒充明文证据')
def legacy_input():
    mapping, methods = ur.build_evidence_maps('BV本', [], {H: {'uid': A, 'source': 'CRC32破解'}})
    assert mapping == {} and methods == {}


@case('一个存在一个未知不得提前认定唯一候选')
def partial_verification():
    with patch.object(ur, 'crack_crc32', return_value=[A, B]):
        out = ur.resolve_sender(H, ['测试'] * 5, {}, fake_cards([A], [B]))
    assert out[0] is None and out[2] == ur.METHOD_PENDING


@case('多个候选同时存在不取最小 UID')
def multiple_exist():
    with patch.object(ur, 'crack_crc32', return_value=[A, B]):
        out = ur.resolve_sender(H, ['测试'], {}, fake_cards([A, B]))
    assert out[0] is None and out[2] == ur.METHOD_AMBIGUOUS


@case('唯一存在且其他明确 -404 仅给低置信度候选')
def unique_exists():
    with patch.object(ur, 'crack_crc32', return_value=[A, B]):
        out = ur.resolve_sender(H, ['测试'] * 100, {}, fake_cards([B]))
    assert out[0] == B and out[1] == '低' and out[4]


@case('所有候选明确 -404 才判定搜索范围内碰撞')
def all_missing():
    with patch.object(ur, 'crack_crc32', return_value=[A, B]):
        out = ur.resolve_sender(H, ['测试'], {}, fake_cards())
    assert out[0] is None and out[2] == ur.METHOD_CRC32_COLLISION


@case('批量返回缺席必须单点确认，静默子集不能排除用户')
def missing_batch():
    cli = client(lambda url, p: {'code': 0, 'data': {}} if url == ur.USER_CARDS_BATCH_URL else {'code': 0, 'data': {'card': {'mid': p['mid']}}})
    found, unknown = ur._batch_verify_uids([A], cli)
    assert A in found and not unknown and len(cli.calls) == 2


@case('空名片、错误 mid、非字典返回均保持未知')
def malformed_single():
    for response in ({'code': 0, 'data': {'card': {}}}, {'code': 0, 'data': {'card': {'mid': B}}}, [], {'code': 0, 'data': []}):
        assert ur.verify_uid_exists(A, client(lambda *args: response))[0] == ur.VERIFY_UNKNOWN


@case('批量名片字段损坏不外抛也不缓存不存在')
def malformed_batch():
    cli = client(lambda url, p: {'code': 0, 'data': {str(A): '损坏'}} if url == ur.USER_CARDS_BATCH_URL else {'code': -500})
    assert ur._batch_verify_uids([A], cli) == ({}, {A})
    assert storage.load_uid_verify_cache([A], 100, 100) == {}


@case('批量串包按有预算单点降级')
def foreign_batch():
    cli = client(lambda url, p: {'code': 0, 'data': {str(B): {'name': '串包'}}} if url == ur.USER_CARDS_BATCH_URL else {'code': -404})
    assert ur._batch_verify_uids([A], cli) == ({}, set()) and len(cli.calls) == 2


@case('风控立即停止后续批次且不展开单点请求')
def risk_stop():
    cli = client(lambda *args: {'code': -412})
    assert ur._batch_verify_uids(list(range(1, 101)), cli)[1] == set(range(1, 101))
    assert len(cli.calls) == 1


@case('单点降级预算耗尽的候选保留未知')
def fallback_budget():
    cli = fake_cards()
    with patch.object(ur, 'UID_VERIFY_FALLBACK_BUDGET', 2):
        found, unknown = ur._batch_verify_uids([1, 2, 3, 4], cli)
    assert not found and unknown == {3, 4} and len(cli.calls) == 3


@case('存在与 -404 缓存跨解析复用，未知不缓存')
def cache_reuse():
    cli = fake_cards([A], [3])
    ur._batch_verify_uids([A, B, 3], cli)
    before = len(cli.calls)
    ur._batch_verify_uids([A, B], cli)
    assert len(cli.calls) == before
    assert 3 not in storage.load_uid_verify_cache([A, B, 3], 100, 100)


@case('存在性缓存过期自动重新请求')
def cache_expiry():
    storage.save_uid_verify_cache(A, 'exists', {'uid': A})
    with storage.get_db() as conn:
        conn.execute('UPDATE uid_verify_cache SET verified_at=?', (time.time() - 100,))
    cli = fake_cards([A])
    with patch.object(ur, 'UID_VERIFY_EXISTS_TTL', 10):
        ur._batch_verify_uids([A], cli)
    assert len(cli.calls) == 1


@case('数学候选缓存按上限和算法版本隔离')
def candidate_cache():
    with patch.object(ur, 'lookup', return_value=[A]) as lookup:
        assert ur.crack_crc32(H) == [A] and ur.crack_crc32(H) == [A]
        assert lookup.call_count == 1
        ur.crack_crc32(H, max_search=A)
        assert lookup.call_count == 2
    assert storage.load_uid_candidates(H, A, '其他版本') is None


@case('候选缓存损坏或正向校验不符自动重算')
def candidate_corruption():
    storage.save_uid_candidates(H, ur.MITM_MAX_UID, ur.CANDIDATE_VERSION, [9])
    with patch.object(ur, 'lookup', return_value=[A]) as lookup:
        assert ur.crack_crc32(H) == [A] and lookup.call_count == 1


@case('解析缓存版本或证据指纹变化立即失效')
def resolution_invalidation():
    verdict = ur.resolve_sender(H, ['测试'], {H: A}, None)
    record = ur.resolution_record(H, verdict, {H: A})
    row = {'method': verdict[2], 'resolution': record}
    assert ur.resolution_is_current(row, H, {H: A})
    assert not ur.resolution_is_current(row, H, {H: {A, B}})
    record['version'] = '旧版'
    assert not ur.resolution_is_current(row, H, {H: A})


@case('新增明文关联复核旧缓存，低兴趣用户也不会升高置信度')
def phase_upgrade():
    storage.save_sender('BV升级', H, A, '低', 'CRC32破解', 1, ['旧'], '低', 0)
    out = main.phase_resolve('BV升级', group(n=1), {H: B}, None)[H]
    assert out['uid'] == B and out['confidence'] == '中' and out['collision_risk']
    assert storage.load_senders('BV升级')[0]['resolution']['previous_uid'] == A


@case('缓存低置信度不再沿用新出现的明文冲突 UID')
def phase_conflict():
    first = main.phase_resolve('BV冲突', group(), {H: A}, None, spam_results={H: {'spam_level': '中'}})
    assert first[H]['uid'] == A
    second = main.phase_resolve('BV冲突', group(), {H: B}, None)
    assert second[H]['uid'] is None and second[H]['method'] == ur.METHOD_AMBIGUOUS
    assert set(storage.load_senders('BV冲突')[0]['resolution']['candidates']) == {A, B}


@case('失效旧猜测退出归属，但原始弹幕及历史 UID 保留')
def retire_old_guess():
    storage.save_sender('BV旧猜测', H, A, '高', 'CRC32破解', 1, ['旧'], '低', 0)
    out = main.phase_resolve('BV旧猜测', group(), {}, None)[H]
    assert out['uid'] is None and out['resolution']['previous_uid'] == A
    assert out['contents'] == ['测试', '测试']


@case('已完成收割刷新不会因全局已知 UID 提前终止')
def harvest_known_global():
    storage.save_global_uid(ue.crc32_uid(7), 7, '评论区验证', 'BV其他')
    storage.set_phase_state('BV刷新', 'uid_harvest', 'done', '1')
    pages = [raw(101, 7), raw(102, B)]
    cli = client(lambda url, p: {'code': 0, 'data': {'replies': [pages[len(cli.calls)-1]],
        'cursor': {'is_end': len(cli.calls) == 2, 'pagination_reply': {'next_offset': '第二页'}}}})
    out = comment.harvest_comment_uids(1, cli, 'BV刷新')
    assert len(cli.calls) == 2 and out[H] == {B}
    assert any(e['bvid'] == 'BV刷新' for e in storage.load_global_uid_map()[ue.crc32_uid(7)]['evidence'])


@case('收割刷新遇到本视频已见评论 ID 才停且处理该页')
def harvest_boundary():
    storage.save_comments('BV边界', [{'uid': A, 'rpid': 101, 'content': '旧'}])
    storage.set_phase_state('BV边界', 'uid_harvest', 'done', '1')
    cli = client(lambda *args: {'code': 0, 'data': {'replies': [raw(101, A, [raw(102, B)])],
                  'cursor': {'pagination_reply': {'next_offset': '下一页'}}}})
    assert comment.harvest_comment_uids(1, cli, 'BV边界')[H] == {A, B}
    assert len(cli.calls) == 1 and 102 in storage.load_uid_harvest_seen('BV边界')


@case('收割请求失败不前移游标或写完成')
def harvest_failure():
    cli = client(lambda *args: {'code': -500})
    assert comment.harvest_comment_uids(1, cli, 'BV失败') == {}
    assert storage.get_phase_state('BV失败', 'uid_harvest', 'done') is None


@case('楼中楼补采命中长 UID 后停止并保留来源及检查点')
def targeted_reply():
    long_uid = 1234567890123456
    target = ue.crc32_uid(long_uid)
    cli = client(lambda *args: {'code': 0, 'data': {'replies': [raw(301, long_uid)], 'page': {'count': 100}}})
    out = comment.supplement_uid_replies(1, cli, 'BV长UID', [{'uid': 8, 'rpid': 300, 'reply_count': 100}], {target}, {})
    assert out[target] == {long_uid} and len(cli.calls) == 1
    assert storage.get_phase_state('BV长UID', 'uid_reply:300', 'page') == '1'
    assert storage.load_global_uid_map()[target]['evidence'][0]['bvid'] == 'BV长UID'


@case('楼中楼补采遵守总请求预算并可续页')
def targeted_budget():
    cli = client(lambda *args: {'code': 0, 'data': {'replies': [raw(301, 8)], 'page': {'count': 100}}})
    roots = [{'uid': 8, 'rpid': 300, 'reply_count': 100}]
    with patch.object(config, 'UID_REPLY_TOTAL_PAGES', 2, create=True):
        comment.supplement_uid_replies(1, cli, 'BV预算', roots, {H}, {})
    assert len(cli.calls) == 2 and storage.get_phase_state('BV预算', 'uid_reply:300', 'page') == '2'
    with patch.object(config, 'UID_REPLY_TOTAL_PAGES', 1, create=True):
        comment.supplement_uid_replies(1, cli, 'BV预算', roots, {H}, {})
    assert cli.calls[-1][1]['pn'] == 3


@case('歧义和待验证条目展示全部候选并遵守遮蔽开关')
def pending_ui():
    verdict = (None, '无', ur.METHOD_AMBIGUOUS, {}, True, [A, B])
    storage.save_sender('BV复核', H, None, '无', ur.METHOD_AMBIGUOUS, 2, ['测试'], '低', 0,
                        resolution=ur.resolution_record(H, verdict, {H: {A, B}}))
    with web.app.test_request_context('/'):
        html, count = web._low_conf_tab_html('BV复核')
        assert count == 1 and str(A) in html and str(B) in html and '尚未确定 UID' in html
    with web.app.test_request_context('/', headers={'Cookie': 'mask=1'}):
        html, count = web._low_conf_tab_html('BV复核')
        assert str(A) not in html and str(B) not in html
    assert web._build_blocklist('BV复核', {'dm', 'spam'}, 100)['entries'] == []


@case('解析质量将歧义与待验证单列，评论直引不冒充弹幕解析率')
def quality_counts():
    storage.save_sender('BV质量', H, None, '无', ur.METHOD_PENDING, 1, ['测试'], '低', 0)
    storage.save_sender('BV质量', 'cmt:9', 9, '高', '问题评论', 0, [], '低', 0)
    quality = web._resolve_quality('BV质量')
    assert quality['resolved'] == 0 and quality['pending'] == 1 and quality['comment_authors'] == 1


@case('删除视频清除本视频证据与收割 ID，保留其他视频来源')
def delete_evidence():
    storage.save_global_uid(H, A, '评论区验证', 'BV删')
    storage.save_global_uid(H, B, '评论区验证', 'BV留')
    storage.save_uid_harvest_page('BV删', {A}, {101}, {'page': 1})
    storage.delete_video_data('BV删')
    entry = storage.load_global_uid_map()[H]
    assert entry['uids'] == [B] and storage.load_uid_harvest_seen('BV删') == set()



@case('收割成功码但空结构不能被当成完成')
def harvest_empty_structure():
    cli = client(lambda *args: {'code': 0, 'data': {}})
    assert comment.harvest_comment_uids(1, cli, 'BV空结构') == {}
    assert storage.get_phase_state('BV空结构', 'uid_harvest', 'done') is None


@case('收割事务失败时证据、已见 ID 和游标全部回滚')
def harvest_rollback():
    with contextlib.closing(storage.get_db()) as conn:
        conn.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON phase_state BEGIN SELECT RAISE(ABORT, '模拟失败'); END")
        conn.commit()
    try:
        storage.save_uid_harvest_page('BV回滚', {A}, {1}, {'page': 1})
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError('应触发事务失败')
    assert storage.load_global_uid_map() == {}
    assert storage.load_uid_harvest_seen('BV回滚') == set()
    assert storage.get_phase_state('BV回滚', 'uid_harvest', 'page') is None


@case('定向补采遇风控整轮停止，不继续请求其他主楼')
def supplement_risk():
    cli = client(lambda *args: {'code': -412})
    roots = [{'uid': 8, 'rpid': r, 'reply_count': 100} for r in (300, 400)]
    assert comment.supplement_uid_replies(1, cli, 'BV风控', roots, {H}, {}) == {}
    assert len(cli.calls) == 1
    assert storage.get_phase_state('BV风控', 'uid_reply:300', 'page') is None


@case('风控异常也终止单点验证和补采，不缓存候选不存在')
def risk_exception():
    def response(*args):
        raise ur.RiskControlError('模拟风控')
    cli = client(response)
    assert ur._batch_verify_uids([A, B], cli) == ({}, {A, B})
    assert len(cli.calls) == 1
    cli.calls.clear()
    comment.supplement_uid_replies(1, cli, 'BV异常风控', [{'uid': 8, 'rpid': 300, 'reply_count': 100}], {H}, {})
    assert len(cli.calls) == 1


@case('手动分析已有旧画像也必须先复核，歧义时不采集候选')
def manual_job_rechecks():
    storage.save_sender('BV手动', H, A, '高', 'CRC32破解', 1, ['旧'], '低', 0)
    storage.save_user_data(A, '旧用户', 1, {'uid': A}, {'uid': A}, bvid='BV手动')
    storage.save_comments('BV手动', [{'uid': A, 'rpid': 1, 'content': '甲'}, {'uid': B, 'rpid': 2, 'content': '乙'}])
    web.JOBS['offline_uid'] = {'errors': [], 'results': [], 'done': 0, 'finished': False}
    with patch.object(web, '_get_client', return_value=None), patch.object(web, '_get_pool', return_value=None), \
         patch.object(web, 'LLM_API_KEY', ''), patch.object(web, 'collect_user_data') as collect:
        web._run_analysis_job('offline_uid', 'BV手动', [H])
    assert not collect.called and web.JOBS['offline_uid']['errors']
    assert web.JOBS['offline_uid']['finished'] and not web.JOBS['offline_uid']['results']
    assert storage.load_senders('BV手动')[0]['uid'] is None


@case('候选来源从其他视频转为本视频时缓存失效并升为关联中置信度')
def context_upgrade():
    storage.save_global_uid(H, A, '评论区验证', 'BV其他')
    mapping, methods = ur.build_evidence_maps('BV本', [], storage.load_global_uid_map())
    verdict = ur.resolve_sender(H, ['测试'], mapping, None, method_map=methods)
    row = {'method': verdict[2], 'resolution': ur.resolution_record(H, verdict, mapping, methods)}
    storage.save_global_uid(H, A, '评论收割', 'BV本')
    new_map, new_methods = ur.build_evidence_maps('BV本', [], storage.load_global_uid_map())
    assert not ur.resolution_is_current(row, H, new_map, new_methods)
    assert ur.resolve_sender(H, ['测试'], new_map, None, method_map=new_methods)[1] == '中'



@case('逐人解析共享整轮补验预算，不按发送者重新计数')
def shared_budget():
    with patch.object(ur, 'UID_VERIFY_FALLBACK_BUDGET', 1):
        budget = ur.VerificationBudget()
    cli = fake_cards()
    assert ur._batch_verify_uids([A], cli, budget) == ({}, set())
    assert ur._batch_verify_uids([B], cli, budget) == ({}, {B})
    assert budget.remaining == 0 and len(cli.calls) == 3


@case('一个发送者触发风控后本轮其他发送者不继续补验')
def shared_risk_stop():
    budget = ur.VerificationBudget()
    cli = client(lambda *args: {'code': -412})
    assert ur._batch_verify_uids([A], cli, budget) == ({}, {A})
    assert ur._batch_verify_uids([B], cli, budget) == ({}, {B})
    assert budget.stopped and len(cli.calls) == 1


print(f'\nUID证据回归: {ok} 项通过, {fail} 项失败')
TMP.cleanup()
sys.exit(1 if fail else 0)
