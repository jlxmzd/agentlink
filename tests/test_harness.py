"""Unit checks for the DeepSeek / Harness side of AgentLink. Nothing here talks to a real Harness: the
loopback client is replaced with fakes and every file lives in a temporary home.

  python -I tests/test_harness.py
"""
import json
import os
from pathlib import Path
import sys
import tempfile

TMP = Path(tempfile.mkdtemp(prefix='agentlink-harness-test-'))
HOME = TMP / 'home'
os.environ['AGENTLINK_USER_HOME'] = str(HOME)
# AGENTLINK_HOME wins over AGENTLINK_USER_HOME for the project registry; scope it to this sandbox too.
os.environ['AGENTLINK_HOME'] = str(HOME / '.agentlink')
os.environ['AGENTLINK_NO_WAKE'] = '1'
os.environ.pop('AGENTLINK_NO_DISPATCH', None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentlink import harness, project as P, sessions  # noqa: E402
from agentlink.bus import AI_START_TIMEOUT, AI_STALL_SECONDS, AI_RETRY_MAX, AI_RETRY_LIMIT  # noqa: E402

DSH = HOME / '.dsh'
RESULTS = []


def check(name):
    def wrap(fn):
        try:
            fn(); RESULTS.append((name, True, ''))
        except Exception as error:
            RESULTS.append((name, False, '%s: %s' % (type(error).__name__, error)))
        return fn
    return wrap


def raises(fn, text=''):
    try: fn()
    except ValueError as error:
        assert text in str(error), 'wrong error: %s' % error
        return
    raise AssertionError('expected ValueError')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def descriptor(url, folder='agentlink'):
    path = DSH / folder / 'connection.json'
    write_json(path, {'url': url, 'pid': 1, 'version': 1})
    return path


def forget_descriptors():
    for folder in ('agentlink', 'codex-bridge'):
        path = DSH / folder / 'connection.json'
        if path.exists(): path.unlink()


class FakeClient:
    """Answers the only two Harness calls AgentLink makes and records the prompts it was sent."""
    items = []
    prompts = []

    def __init__(self): pass

    def call(self, method, request):
        if method == 'session.list': return {'items': list(FakeClient.items)}
        if method == 'session.prompt':
            FakeClient.prompts.append(request)
            return {'accepted': True}
        raise AssertionError('unexpected Harness call: ' + method)


class FailingPromptClient(FakeClient):
    """The bridge is up but the prompt send fails: the dispatch must stay uncertain, never reap."""

    def call(self, method, request):
        if method == 'session.prompt': raise OSError('bridge down')
        return FakeClient.call(self, method, request)


@check('descriptor_path prefers ~/.dsh/agentlink, falls back to codex-bridge, then raises')
def _():
    forget_descriptors()
    ours = descriptor('http://127.0.0.1:8765/#token=1')
    legacy = descriptor('http://127.0.0.1:8766/', 'codex-bridge')
    assert harness.descriptor_path() == ours
    ours.unlink()
    assert harness.descriptor_path() == legacy
    legacy.unlink()
    raises(lambda: harness.descriptor_path(), 'connection.json')


@check('Client refuses any descriptor URL that is not loopback HTTP')
def _():
    descriptor('http://example.com:8080/api')
    raises(lambda: harness.Client(), 'loopback')
    descriptor('https://127.0.0.1:8765/')
    raises(lambda: harness.Client(), 'loopback')
    forget_descriptors()


@check('archived_sessions reads global.archivedSessionIds and rejects malformed metadata')
def _():
    path = DSH / 'storages' / 'workspace.json'
    write_json(path, {'global': {'archivedSessionIds': ['old-1', 'old-2']}})
    assert harness.archived_sessions() == {'old-1', 'old-2'}
    write_json(path, {'global': {'archivedSessionIds': 'old-1'}})
    raises(lambda: harness.archived_sessions(), 'archive metadata')


ROOT = TMP / 'proj'
(ROOT / 'src').mkdir(parents=True)


def workspace_file(archived=(), workspaces=None):
    """Write the Harness workspace store in its real shape: global.archivedSessionIds + tables.workspaces.

    `workspaces` maps an absolute directory path to the session ids that workspace accounts for."""
    rows = workspaces if workspaces is not None else {ROOT.as_posix(): []}
    if isinstance(rows, list):                     # convenience: a flat member list for one workspace
        rows = {ROOT.as_posix(): list(rows)}
    tables = {'workspaces': {('ws-%d' % n): {'path': path, 'title': path.rsplit('/', 1)[-1] or path,
                                             'sessionIds': list(ids), 'createdAt': '', 'updatedAt': ''}
                             for n, (path, ids) in enumerate(rows.items())}}
    write_json(DSH / 'storages' / 'workspace.json',
               {'unit': 1, 'global': {'initialized': True, 'archivedSessionIds': list(archived)}, 'tables': tables})


def item(sid, cwd, title=None, updated=None, prompt=None, parent=None, blank=False, running=False):
    values = {}
    if title is not None: values['title'] = title
    values['sessionListMetadata'] = {'blank': blank, 'lastPromptAt': prompt}
    row = {'sessionId': sid, 'cwd': cwd, 'running': running, 'updatedAt': updated,
           'projections': {'values': values}}
    if parent: row['parentSessionId'] = parent
    return row


@check('deepseek_sessions follows the UI workspace membership, keeping forked but not stray sub-sessions')
def _():
    workspace_file(archived=['s-archived'], workspaces={ROOT.as_posix(): ['s-main', 's-fork', 's-archived', 's-dup']})
    FakeClient.items = [
        item('s-main', str(ROOT), '主会话', 1793000000000, prompt=1793000000000),
        item('s-fork', str(ROOT), '保留的 fork', 1793100000000, prompt=1793100000000, parent='s-main'),
        item('s-stray', str(ROOT), '自动子会话', 1793200000000, prompt=1793200000000, parent='s-main'),
        item('s-archived', str(ROOT), '已归档', 1793300000000, prompt=1793300000000),
        item('s-stub', str(ROOT), None, 1793400000000, blank=True, prompt=None),
        item('s-elsewhere', str(TMP / 'elsewhere'), '别处', 1793500000000, prompt=1793500000000),
        item('s-dup', str(ROOT), '旧记录', 1792900000000, prompt=1792900000000),
        item('s-dup', str(ROOT), '新记录', 1793600000000, prompt=1793600000000, running=True),
    ]
    original = harness.Client
    harness.Client = FakeClient
    try:
        found = sessions.deepseek_sessions(str(ROOT))
        everything = sessions.deepseek_sessions(str(ROOT), include_all=True)
    finally:
        harness.Client = original
    ids = [i['id'] for i in found]
    assert ids == ['s-dup', 's-fork', 's-main'], ids      # sorted by last prompt, newest first
    assert 's-stray' not in ids                           # a child no workspace accounts for
    assert 's-archived' not in ids and 's-stub' not in ids
    assert found[0]['title'] == '新记录' and found[0]['running'] is True   # dedupe keeps the newest record
    assert found[1]['fork'] is True and found[1]['workspace'] is True
    assert all(i['archived'] is False for i in found)
    every = [i['id'] for i in everything]
    assert 's-elsewhere' in every                         # show-all widens the directory filter only
    assert 's-stray' not in every and 's-archived' not in every


@check('deepseek_sessions keeps a workspace session even when it is blank, and hides a blank stranger')
def _():
    workspace_file(workspaces={ROOT.as_posix(): ['s-blank-member']})
    FakeClient.items = [
        item('s-blank-member', str(ROOT), '新对话', 1793000000000, blank=True, prompt=None),
        item('s-blank-stranger', str(ROOT), None, 1793100000000, blank=True, prompt=None),
    ]
    original = harness.Client
    harness.Client = FakeClient
    try: found = sessions.deepseek_sessions(str(ROOT))
    finally: harness.Client = original
    assert [i['id'] for i in found] == ['s-blank-member'], found


@check('deepseek_sessions prefers the UI title projection over the list RPC title field')
def _():
    workspace_file(workspaces={ROOT.as_posix(): ['s-proj', 's-top', 's-none']})
    FakeClient.items = [
        dict(item('s-proj', str(ROOT), '投影标题', 1793000000000, prompt=1793000000000), title='顶层旧标题'),
        dict(item('s-top', str(ROOT), None, 1793100000000, prompt=1793100000000), title='只有顶层标题'),
        item('s-none', str(ROOT), None, 1793200000000, prompt=1793200000000),
    ]
    original = harness.Client
    harness.Client = FakeClient
    try: found = sessions.deepseek_sessions(str(ROOT))
    finally: harness.Client = original
    assert found[2]['title'] == '投影标题', found[2]            # projections.values.title wins
    assert found[1]['title'] == '只有顶层标题', found[1]        # documented list field is the fallback
    assert found[0]['title'] == '', found[0]                    # neither: empty, never a guess


@check('deepseek_sessions without a matching workspace still drops stray child sessions')
def _():
    OTHER = TMP / 'elsewhere-ws'
    workspace_file(workspaces={OTHER.as_posix(): ['s-other']})
    FakeClient.items = [
        item('s-top', str(ROOT), '顶层', 1793000000000, prompt=1793000000000),
        item('s-child', str(ROOT), '内部子对话', 1793100000000, prompt=1793100000000, parent='s-top'),
        item('s-child2', str(ROOT), None, 1793200000000, parent='s-top', blank=True),
    ]
    original = harness.Client
    harness.Client = FakeClient
    try: found = sessions.deepseek_sessions(str(ROOT))
    finally: harness.Client = original
    assert [i['id'] for i in found] == ['s-top'], found      # no parent-based exclusion escapes here


@check('deepseek_sessions with show-all keeps a fork listed by another workspace, not its stray sibling')
def _():
    OTHER = TMP / 'elsewhere-ws'
    workspace_file(workspaces={ROOT.as_posix(): ['s-top'], OTHER.as_posix(): ['s-fork']})
    FakeClient.items = [
        item('s-top', str(ROOT), '顶层', 1793000000000, prompt=1793000000000),
        item('s-fork', str(OTHER), '别处的 fork', 1793100000000, prompt=1793100000000, parent='s-root'),
        item('s-stray', str(OTHER), '自动子会话', 1793200000000, prompt=1793200000000, parent='s-root'),
    ]
    original = harness.Client
    harness.Client = FakeClient
    try:
        here = sessions.deepseek_sessions(str(ROOT))
        every = sessions.deepseek_sessions(str(ROOT), include_all=True)
    finally: harness.Client = original
    assert [i['id'] for i in here] == ['s-top'], here        # another project's sessions stay out by default
    ids = [i['id'] for i in every]
    assert 's-fork' in ids and 's-stray' not in ids, ids     # a kept fork yes, an unlisted child no


@check('deepseek_sessions keeps an explicit member whose cwd moved to another directory')
def _():
    OTHER = TMP / 'elsewhere-ws'
    workspace_file(workspaces={ROOT.as_posix(): ['s-top', 's-moved']})
    FakeClient.items = [
        item('s-top', str(ROOT), '本地', 1793000000000, prompt=1793000000000),
        item('s-moved', str(OTHER), '成员已迁走', 1793100000000, prompt=1793100000000),
    ]
    original = harness.Client
    harness.Client = FakeClient
    try: found = sessions.deepseek_sessions(str(ROOT))
    finally: harness.Client = original
    assert [i['id'] for i in found] == ['s-moved', 's-top'], found   # membership outranks the directory


@check('a nested sub-project keeps its own sessions and does not inherit its parent project')
def _():
    SUB = ROOT / 'sub'
    SUB.mkdir(parents=True)
    workspace_file(workspaces={ROOT.as_posix(): ['s-parent'], SUB.as_posix(): ['s-sub']})
    FakeClient.items = [
        item('s-parent', str(ROOT), '父项目', 1793000000000, prompt=1793000000000),
        item('s-sub', str(SUB), '子项目', 1793100000000, prompt=1793100000000),
    ]
    original = harness.Client
    harness.Client = FakeClient
    try:
        child_view = sessions.deepseek_sessions(str(SUB))
        parent_view = sessions.deepseek_sessions(str(ROOT))
    finally: harness.Client = original
    assert [i['id'] for i in child_view] == ['s-sub'], child_view              # its own workspace wins
    assert 's-sub' not in [i['id'] for i in parent_view], parent_view           # not dragged into the parent


@check('every visible session stays offerable: no hidden 40-row cap')
def _():
    workspace_file(workspaces={ROOT.as_posix(): ['s-%02d' % n for n in range(45)]})
    FakeClient.items = [item('s-%02d' % n, str(ROOT), '会话 %d' % n,
                             1793000000000 + n * 1000, prompt=1793000000000 + n * 1000)
                        for n in range(45)]
    original = harness.Client
    harness.Client = FakeClient
    try: found = sessions.deepseek_sessions(str(ROOT))
    finally: harness.Client = original
    assert len(found) == 45, len(found)                       # nothing is truncated
    assert found[0]['id'] == 's-44'                           # still newest-first


@check('unreadable workspace metadata is reported and never silently unfiltered')
def _():
    FakeClient.items = [item('s-main', str(ROOT), '主会话', 1793000000000, prompt=1793000000000)]
    original = harness.Client
    harness.Client = FakeClient
    try:
        write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': [], 'initialized': True},
                                                         'tables': {'workspaces': 'not-a-table'}})
        found = sessions.discover(str(ROOT))
        assert found['deepseek']['items'] == [], found['deepseek']
        assert '工作区元数据' in found['deepseek']['error'], found['deepseek']

        write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': 'not-a-list'}})
        found = sessions.discover(str(ROOT))
        assert found['deepseek']['items'] == [] and 'archive' in found['deepseek']['error'].lower()

        (DSH / 'storages' / 'workspace.json').unlink()
        found = sessions.discover(str(ROOT))
        assert found['deepseek']['items'] == [] and '工作区元数据' in found['deepseek']['error']
    finally:
        harness.Client = original


@check('epoch-millisecond updatedAt is normalised to ISO text and sorts newest first')
def _():
    workspace_file(workspaces={ROOT.as_posix(): ['s-old', 's-new']})
    FakeClient.items = [
        item('s-old', str(ROOT), '旧', 1700000000000, prompt=1700000000000),
        item('s-new', str(ROOT), '新', 1790000000000, prompt=1790000000000),
    ]
    original = harness.Client
    harness.Client = FakeClient
    try: found = sessions.deepseek_sessions(str(ROOT))
    finally: harness.Client = original
    assert [i['id'] for i in found] == ['s-new', 's-old'], found
    assert found[0]['updated'].startswith('2026-') and 'T' in found[0]['updated'], found[0]['updated']
    assert found[1]['updated'].startswith('2023-')


DROOT = TMP / 'dispatch-proj'
(DROOT / 'src').mkdir(parents=True)
PROJ = P.Project(DROOT)
PROJ.apply({'claude': {'id': 'c1111111-1111-1111-1111-111111111111'},
            'codex': {'id': '01a143dc-fea9-7491-934e-95b1c08226af'}})
BUS = PROJ.bus()


@check('dispatch is pending with an unbound reason when DeepSeek has no bound session')
def _():
    out = BUS.post('claude', 'deepseek', 'Unbound', 'spec', no_dispatch=True)
    assert out['state'] == 'posted' and out['dispatch'] == 'held', out
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    assert BUS.dispatch(out['id']) == 'pending'
    d = BUS.load(out['id'])['dispatch']
    assert d['state'] == 'pending' and '未绑定' in d['reason'], d


@check('dispatch waits while the bound session is busy, then prompts once with a stable requestId')
def _():
    PROJ.apply({'deepseek': {'id': 'session-ds', 'title': 'DS'}})
    write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': []}})
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    FakeClient.items = [{'sessionId': 'session-ds', 'cwd': str(DROOT), 'running': True,
                         'projections': {'values': {'title': 'DS'}}}]
    FakeClient.prompts = []
    original = harness.Client
    harness.Client = FakeClient
    try:
        busy = BUS.post('claude', 'deepseek', 'Busy', 'spec', no_dispatch=True)
        assert BUS.dispatch(busy['id']) == 'pending'
        d = BUS.load(busy['id'])['dispatch']
        assert d['state'] == 'pending' and 'busy' in d['reason'], d
        assert FakeClient.prompts == []

        FakeClient.items = [{'sessionId': 'session-ds', 'cwd': str(DROOT), 'running': False,
                             'projections': {'values': {'title': 'DS'}}}]
        t = BUS.post('claude', 'deepseek', 'Run', 'spec', no_dispatch=True)
        assert BUS.dispatch(t['id']) == 'accepted'
        first = BUS.load(t['id'])['dispatch']['requestId']
        assert isinstance(first, str) and first
        assert len(FakeClient.prompts) == 1
        p = FakeClient.prompts[0]
        assert p['sessionId'] == 'session-ds' and p['requestId'] == first and p['mode'] == 'queue', p
        assert p['content'][0]['type'] == 'text' and t['id'] in p['content'][0]['text']

        assert BUS.dispatch(t['id']) == 'accepted'                 # accepted: never sent twice
        assert len(FakeClient.prompts) == 1
        BUS.mark(t['id'], state='pending', attempt=0)              # an explicit release of the same attempt
        assert BUS.dispatch(t['id']) == 'accepted'
        assert len(FakeClient.prompts) == 2
        assert FakeClient.prompts[-1]['requestId'] == first        # requestId is stable per task/attempt
    finally:
        harness.Client = original


@check('a failed send is uncertain and is not retried automatically')
def _():
    FakeClient.items = [{'sessionId': 'session-ds', 'cwd': str(DROOT), 'running': False,
                         'projections': {'values': {'title': 'DS'}}}]
    original = harness.Client
    harness.Client = FailingPromptClient
    try:
        t = BUS.post('claude', 'deepseek', 'Flaky', 'spec', no_dispatch=True)
        assert BUS.dispatch(t['id']) == 'uncertain'
        assert BUS.load(t['id'])['dispatch']['state'] == 'uncertain'
        assert BUS.dispatch(t['id']) == 'uncertain'                # uncertain sends wait for the person
    finally:
        harness.Client = original


AIROOT = TMP / 'ai-review'
(AIROOT / 'src').mkdir(parents=True)
APROJ = P.Project(AIROOT)
APROJ.apply({'codex': {'id': '01a143dc-fea9-7491-934e-95b1c08226af'},
             'deepseek': {'id': 'session-exec', 'title': '执行'}},
            options={'aiReviewEnabled': True, 'aiReviewSession': {'id': 'session-review', 'title': 'AI 审查'}})
ABUS = APROJ.bus()
SESSIONS = [
    {'sessionId': 'session-exec', 'cwd': str(AIROOT), 'running': False,
     'projections': {'values': {'title': '执行'}}},
    {'sessionId': 'session-review', 'cwd': str(AIROOT), 'running': False,
     'projections': {'values': {'title': 'AI 审查'}}},
]


def fresh_review_bus(name):
    """An isolated project for one AI-review check: a failure in one check can never leak path locks into
    the next (a task left in ai_review or rework holds its paths until it is cancelled)."""
    root = TMP / ('ai-' + name)
    (root / 'src').mkdir(parents=True)
    proj = P.Project(root)
    proj.apply({'codex': {'id': '01a143dc-fea9-7491-934e-95b1c08226af'},
                'deepseek': {'id': 'session-exec', 'title': '执行'}},
               options={'aiReviewEnabled': True, 'aiReviewSession': {'id': 'session-review', 'title': 'AI 审查'}})
    sessions = [
        {'sessionId': 'session-exec', 'cwd': str(root), 'running': False,
         'projections': {'values': {'title': '执行'}}},
        {'sessionId': 'session-review', 'cwd': str(root), 'running': False,
         'projections': {'values': {'title': 'AI 审查'}}},
    ]
    return proj.bus(), sessions


@check('AI review: a passing DeepSeek delivery goes to ai_review and prompts the review session once')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': []}})
    bus, sessions = fresh_review_bus('prompt-once')
    FakeClient.items = list(sessions)
    FakeClient.prompts = []
    original = harness.Client
    harness.Client = FakeClient
    try:
        t = bus.post('codex', 'deepseek', 'Review flow', '实现 src 里的功能', level='L1', paths=['src'], no_dispatch=True)
        bus.claim('deepseek', t['id'])
        out = bus.deliver('deepseek', t['id'], 'done')
        assert out['state'] == 'ai_review' and out.get('auto') is None, out
        task = bus.load(t['id'])
        assert task['aiReview']['status'] == 'sent', task['aiReview']
        assert task['aiReview']['session']['id'] == 'session-review', task['aiReview']
        prompts = [p for p in FakeClient.prompts if p['sessionId'] == 'session-review']
        assert len(prompts) == 1, FakeClient.prompts
        text = prompts[0]['content'][0]['text']
        for needle in (t['id'], 'src', '只负责审查', '不要修改任何文件', 'reviewer', '程序验收'):
            assert needle in text, needle
        assert prompts[0]['requestId'] == task['aiReview']['requestId']
        raises(lambda: bus.deliver('deepseek', t['id'], 'again'), 'ai_review')
        bus.ai_review_send(t['id'])                                     # never sent twice per delivery
        assert len([p for p in FakeClient.prompts if p['sessionId'] == 'session-review']) == 1
    finally:
        harness.Client = original
        bus.review('codex', t['id'], 'cancel', 'test cleanup')


@check('AI review: accept hands the task to the poster, reject sends it back with the note')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': []}})
    bus, sessions = fresh_review_bus('accept-reject')
    FakeClient.items = list(sessions)
    FakeClient.prompts = []
    original = harness.Client
    harness.Client = FakeClient
    created = []                                                   # every task this check opens, for cleanup
    try:
        t = bus.post('codex', 'deepseek', 'Accept flow', 'x', level='L1', paths=['accept-flow'], no_dispatch=True)
        created.append(t['id'])
        bus.claim('deepseek', t['id'])
        bus.deliver('deepseek', t['id'], 'done')
        token = bus.load(t['id'])['aiReview']['token']
        raises(lambda: bus.ai_review('claude', t['id'], 0, token, 'accept'), 'DeepSeek')
        raises(lambda: bus.ai_review('deepseek', t['id'], 0, 'wrong-token', 'accept'), '令牌')
        raises(lambda: bus.ai_review('deepseek', t['id'], 3, token, 'accept'), '序号')
        out = bus.ai_review('deepseek', t['id'], 0, token, 'accept')
        assert out['state'] == 'delivered', out
        assert bus.load(t['id'])['aiReview']['status'] == 'accepted'
        assert bus.ai_review('deepseek', t['id'], 0, token, 'accept').get('duplicate') is True
        assert bus.review('codex', t['id'], 'accept')['state'] == 'accepted'

        t2 = bus.post('codex', 'deepseek', 'Reject flow', 'x', level='L1', paths=['reject-flow'], no_dispatch=True)
        created.append(t2['id'])
        bus.claim('deepseek', t2['id'])
        bus.deliver('deepseek', t2['id'], 'done')
        token2 = bus.load(t2['id'])['aiReview']['token']
        out2 = bus.ai_review('deepseek', t2['id'], 0, token2, 'reject', '实现不符合规格', no_dispatch=True)
        assert out2['state'] == 'rework', out2
        task = bus.load(t2['id'])
        assert task['review']['note'] == '实现不符合规格' and task['aiReview']['status'] == 'rejected'
        assert task['rejects'] == 1 and 'token' not in task['aiReview']
        # A late callback for a finished attempt is stale, not a new error: it must not overwrite the verdict.
        late = bus.ai_review('deepseek', t2['id'], 0, token2, 'accept')
        assert late.get('duplicate') is True, late
        assert bus.load(t2['id'])['state'] == 'rework'
        # A stale callback for a finished attempt is refused without touching the state: it lands in
        # aiReview.error for the panel, but the task itself stays where it is.
        try:
            late_start = bus.ai_review('deepseek', t2['id'], 0, token2, 'started')
        except ValueError as error:
            assert '过期' in str(error), error
        else:
            raise AssertionError('expected ValueError, got %r' % (late_start,))
        assert bus.load(t2['id'])['state'] == 'rework'
        bus.claim('deepseek', t2['id'])                               # rework is worked on again
        out3 = bus.deliver('deepseek', t2['id'], 'fixed')
        again = bus.load(t2['id'])
        assert out3['state'] == 'ai_review' and again['aiReview']['attempt'] == 1, again['aiReview']
        assert again['aiReview']['token'] != token2 and again['aiReview']['status'] == 'sent'
        bus.review('codex', t2['id'], 'cancel', 'test cleanup')        # ai_review holds a lock: release it
    finally:
        harness.Client = original
        for tid in created:                                        # a mid-check failure must not leak path locks
            try: bus.review('codex', tid, 'cancel', 'test cleanup')
            except ValueError: pass


@check('AI review: a failed send stays visible and a retry reuses the same requestId')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': []}})
    FakeClient.items = list(SESSIONS)
    FakeClient.prompts = []
    original = harness.Client
    harness.Client = FailingPromptClient
    try:
        t = ABUS.post('codex', 'deepseek', 'Flaky review', 'x', level='L1', paths=['flaky-flow'], no_dispatch=True)
        ABUS.claim('deepseek', t['id'])
        out = ABUS.deliver('deepseek', t['id'], 'done')
        assert out['state'] == 'ai_review' and out['aiReview'] == 'uncertain', out
        task = ABUS.load(t['id'])
        assert task['aiReview']['status'] == 'uncertain', task['aiReview']
        assert 'AI 验收未送达' in '\n'.join(ABUS.actionable('codex'))
        request = task['aiReview']['requestId']
    finally:
        harness.Client = FakeClient
    try:
        retried = ABUS.release(t['id'])
        assert retried['aiReview'] == 'sent', retried
        assert ABUS.load(t['id'])['aiReview']['requestId'] == request
        ABUS.review('codex', t['id'], 'cancel', 'test cleanup')      # ai_review holds a lock: release it
    finally:
        harness.Client = original


@check('AI review: a missing review session is visible as failed and never accepted')
def _():
    mroot = TMP / 'ai-review-missing'
    (mroot / 'src').mkdir(parents=True)
    mp = P.Project(mroot)
    mp.apply({'codex': {'id': '01a143dc-fea9-7491-934e-95b1c08226af'}, 'deepseek': {'id': 'session-exec'}},
             options={'aiReviewEnabled': True, 'aiReviewSession': None})
    mb = mp.bus()
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    FakeClient.items = list(SESSIONS)
    original = harness.Client
    harness.Client = FakeClient
    try:
        t = mb.post('codex', 'deepseek', 'No session', 'x', level='L1', paths=['src'], no_dispatch=True)
        mb.claim('deepseek', t['id'])
        out = mb.deliver('deepseek', t['id'], 'done')
        assert out['state'] == 'ai_review' and out['aiReview'] == 'failed', out
        assert '未配置' in mb.load(t['id'])['aiReview']['error']
        assert mb.load(t['id'])['state'] not in ('accepted', 'delivered')
    finally:
        harness.Client = original


def _fake_clock(bus, seconds):
    """Shift every stored review timestamp back by `seconds`, so timeout/backoff branches run without
    sleeping. Uses the bus socket so the refreshed records are written the same way the code writes them."""
    import datetime
    with bus.lock:
        for task in bus.all():
            r = task.get('aiReview') or {}
            for field in ('at', 'sentAt', 'startedAt', 'lease', 'lastTryAt', 'nextRetryAt'):
                value = r.get(field)
                if not isinstance(value, str) or not value: continue
                try: stamp = datetime.datetime.fromisoformat(value)
                except ValueError: continue
                r[field] = (stamp - datetime.timedelta(seconds=seconds)).isoformat(timespec='seconds')
            if r: task['aiReview'] = r
            bus.save(task)


def _review_task(bus, sessions, paths, title='AI flow'):
    FakeClient.items = list(sessions)
    FakeClient.prompts = []
    t = bus.post('codex', 'deepseek', title, 'x', level='L1', paths=paths, no_dispatch=True)
    bus.claim('deepseek', t['id'])
    out = bus.deliver('deepseek', t['id'], 'done')
    assert out['state'] == 'ai_review', out
    return t['id']


@check('AI review: started must be confirmed with the right token and is idempotent')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': []}})
    bus, sessions = fresh_review_bus('started-auth')
    original = harness.Client
    harness.Client = FakeClient
    try:
        tid = _review_task(bus, sessions, ['started'])
        token = bus.load(tid)['aiReview']['token']
        raises(lambda: bus.ai_review('claude', tid, 0, token, 'started'), 'DeepSeek')
        raises(lambda: bus.ai_review('deepseek', tid, 0, 'wrong', 'started'), '令牌')
        raises(lambda: bus.ai_review('deepseek', tid, 5, token, 'started'), '序号')
        first = bus.ai_review('deepseek', tid, 0, token, 'started')
        assert first['status'] == 'started', first
        r = bus.load(tid)['aiReview']
        assert r['status'] == 'started' and r['startedAt'], r
        again = bus.ai_review('deepseek', tid, 0, token, 'started')
        assert again.get('duplicate') is True, again                  # repeated confirmation changes nothing
        # `sent` alone never means started: the stored status is the proof, and it is 'started' only now.
        before = bus.load(tid)['aiReview']
        assert before['status'] == 'started' and 'sentAt' in before
    finally:
        harness.Client = original
        bus.review('codex', tid, 'cancel', 'test cleanup')


@check('AI review: a sent prompt that is never confirmed retries on a bounded backoff, then stalls')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': []}})
    bus, sessions = fresh_review_bus('backoff')
    original = harness.Client
    harness.Client = FakeClient
    try:
        tid = _review_task(bus, sessions, ['backoff'])
        r = bus.load(tid)['aiReview']
        assert r['status'] == 'sent' and r['attempts'] == 1, r      # the budget is charged when the lease is taken
        # Inside AI_START_TIMEOUT a pending prompt is left alone, even if a tick runs.
        bus.pump()
        assert bus.load(tid)['aiReview']['status'] == 'sent'
        assert len(FakeClient.prompts) == 1
        # Past the timeout the same requestId is resent once, never a fresh one.
        _fake_clock(bus, AI_START_TIMEOUT + 5)
        bus.pump()
        r = bus.load(tid)['aiReview']
        assert r['status'] == 'sent' and r['attempts'] == 2, r     # charged at claim time, monotonic
        assert len(FakeClient.prompts) == 2
        assert {p['requestId'] for p in FakeClient.prompts} == {r['requestId']}
        # A resend right away waits for the next window: a fresh sentAt means the timeout has to pass again.
        bus.pump()
        assert bus.load(tid)['aiReview']['attempts'] == 2
        assert len(FakeClient.prompts) == 2
        # Drive it to the limit: attempts keep growing, then the task is stalled for a human, never accepted.
        for _ in range(AI_RETRY_LIMIT + 2):
            _fake_clock(bus, AI_START_TIMEOUT + AI_RETRY_MAX + 5)
            bus.pump()
            if bus.load(tid)['aiReview']['status'] == 'stalled': break
        r = bus.load(tid)['aiReview']
        assert r['status'] == 'stalled' and r.get('stalled') is True, r
        assert r['attempts'] == AI_RETRY_LIMIT, r          # the cap is exact: the counter stops at the limit
        assert bus.load(tid)['state'] == 'ai_review'                  # never silently accepted
        assert all(p['requestId'] == r['requestId'] for p in FakeClient.prompts)
    finally:
        harness.Client = original
        bus.review('codex', tid, 'cancel', 'test cleanup')


@check('AI review: a started review that goes quiet is flagged stalled, and manual retry is allowed')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': []}})
    bus, sessions = fresh_review_bus('stall-started')
    original = harness.Client
    harness.Client = FakeClient
    try:
        tid = _review_task(bus, sessions, ['stall'])
        token = bus.load(tid)['aiReview']['token']
        bus.ai_review('deepseek', tid, 0, token, 'started')
        _fake_clock(bus, AI_STALL_SECONDS + 60)
        bus.pump()
        r = bus.load(tid)['aiReview']
        assert r['status'] == 'stalled' and r['stalled'] is True, r     # started but no verdict for too long
        assert bus.load(tid)['state'] == 'ai_review'
        # A manual retry clears the stall and resends once with the same requestId.
        out = bus.release(tid)
        assert out['aiReview'] == 'started', out
        assert len(FakeClient.prompts) == 1
        assert bus.load(tid)['aiReview']['requestId'] == r['requestId']
    finally:
        harness.Client = original
        bus.review('codex', tid, 'cancel', 'test cleanup')


@check('AI review: crash recovery picks up sending/sent/failed records left behind')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': []}})
    bus, sessions = fresh_review_bus('recovery')
    original = harness.Client
    harness.Client = FakeClient
    try:
        # A process died mid-send: the record says sending with an expired lease.
        tid = _review_task(bus, sessions, ['recover-sending'])
        task = bus.load(tid)
        task['aiReview'].update(status='sending', lease='2000-01-01T00:00:00+00:00')
        bus.save(task)
        FakeClient.prompts = []
        bus.pump()
        assert bus.load(tid)['aiReview']['status'] == 'sent', bus.load(tid)['aiReview']
        assert len(FakeClient.prompts) == 1

        # A legacy failure record with no nextRetryAt must be retried, not dropped.
        tid2 = _review_task(bus, sessions, ['recover-failed'], 'Legacy')
        task = bus.load(tid2)
        task['aiReview'].update(status='failed', error='old record')
        task['aiReview'].pop('nextRetryAt', None)
        bus.save(task)
        FakeClient.prompts = []
        bus.pump()
        assert bus.load(tid2)['aiReview']['status'] in ('sent', 'failed'), bus.load(tid2)['aiReview']
        assert len(FakeClient.prompts) == 1
    finally:
        harness.Client = original
        bus.review('codex', tid, 'cancel', 'test cleanup')
        bus.review('codex', tid2, 'cancel', 'test cleanup')


@check('AI review: a lease blocks concurrent sends, and an old reply cannot overwrite a newer state')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': []}})
    bus, sessions = fresh_review_bus('race')
    original = harness.Client
    harness.Client = FakeClient
    try:
        tid = _review_task(bus, sessions, ['race'])
        task = bus.load(tid)
        # Simulate a second process holding a fresh lease.
        task['aiReview'].update(status='sending', lease=__import__('agentlink.util', fromlist=['now']).now())
        bus.save(task)
        FakeClient.prompts = []
        claim, why = bus.ai_review_claim(tid, 0)
        assert claim is None and why == 'busy', (claim, why)          # the loser of the race must stand down
        bus.ai_review_send(tid)
        assert len(FakeClient.prompts) == 0, FakeClient.prompts

        # An old clip for a cancelled task is refused and the task stays cancelled.
        token = bus.load(tid)['aiReview']['token']
        bus.review('codex', tid, 'cancel', 'poster cancelled')
        try:
            bus.ai_review('deepseek', tid, 0, token, 'accept')
        except ValueError as error:
            assert '过期' in str(error), error
        else:
            raise AssertionError('expected the stale reply to be refused')
        assert bus.load(tid)['state'] == 'cancelled'
    finally:
        harness.Client = original


@check('AI review: a started reply that lands inside the RPC wins over the send result')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': []}})
    bus, sessions = fresh_review_bus('inrpc-started')
    original = harness.Client
    started_inside = []

    class StartedInsideRpc(FakeClient):
        """The reviewer answers on the same tick: `started` lands while the send call is still in flight."""
        def call(self, method, request):
            if method == 'session.prompt':
                tid = started_inside[0]
                rec = bus.load(tid)['aiReview']
                bus.ai_review('deepseek', tid, rec['attempt'], rec['token'], 'started')
                return {'accepted': True}
            return FakeClient.call(self, method, request)

    harness.Client = StartedInsideRpc
    try:
        tid = _review_task(bus, sessions, ['inrpc'])
        started_inside.append(tid)
        # Re-drive the send so the fake reviewer can confirm `started` during the RPC.
        task = bus.load(tid)
        task['aiReview'].update(status='failed', nextRetryAt=None, lease=None, leaseId=None)
        bus.save(task)
        bus.ai_review_send(tid)
        r = bus.load(tid)['aiReview']
        assert r['status'] == 'started', r                             # the send result must not overwrite it
        assert r['startedAt'] and not r.get('leaseId'), r
    finally:
        harness.Client = original
        bus.review('codex', tid, 'cancel', 'test cleanup')


@check('AI review: a send failure after the reviewer started cannot roll the review back')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': []}})
    bus, sessions = fresh_review_bus('inrpc-fail')
    original = harness.Client
    started_inside = []

    class FailAfterStarted(FakeClient):
        """`started` lands first, then the same RPC raises: the failure must not mark it failed/uncertain."""
        def call(self, method, request):
            if method == 'session.prompt':
                tid = started_inside[0]
                rec = bus.load(tid)['aiReview']
                bus.ai_review('deepseek', tid, rec['attempt'], rec['token'], 'started')
                raise OSError('bridge dropped after accepting')
            return FakeClient.call(self, method, request)

    harness.Client = FailAfterStarted
    try:
        tid = _review_task(bus, sessions, ['inrpc-fail'])
        started_inside.append(tid)
        task = bus.load(tid)
        task['aiReview'].update(status='failed', nextRetryAt=None, lease=None, leaseId=None)
        bus.save(task)
        bus.ai_review_send(tid)
        r = bus.load(tid)['aiReview']
        assert r['status'] == 'started', r
        assert not r.get('error'), r                                   # no failure recorded over a running review
    finally:
        harness.Client = original
        bus.review('codex', tid, 'cancel', 'test cleanup')


@check('AI review: a manual retry cannot clear a live lease or a started review')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    write_json(DSH / 'storages' / 'workspace.json', {'global': {'archivedSessionIds': []}})
    bus, sessions = fresh_review_bus('manual-guard')
    original = harness.Client
    harness.Client = FakeClient
    try:
        from agentlink.util import now as stamp
        # 1. A live lease belongs to another sender: a manual retry must not clear it or send again.
        tid = _review_task(bus, sessions, ['guard-lease'])
        task = bus.load(tid)
        task['aiReview'].update(status='sending', lease=stamp(), leaseId='other-process')
        bus.save(task)
        FakeClient.prompts = []
        out = bus.release(tid)
        assert out['aiReview'] in ('busy', 'sending'), out
        r = bus.load(tid)['aiReview']
        assert r['status'] == 'sending' and r['leaseId'] == 'other-process', r
        assert len(FakeClient.prompts) == 0, FakeClient.prompts

        # 2. A started review is running: a manual retry must not reset it to pending or re-send.
        token = bus.load(tid)['aiReview']['token']
        bus.ai_review('deepseek', tid, 0, token, 'started')
        FakeClient.prompts = []
        out = bus.release(tid)
        assert out['aiReview'] == 'started', out
        assert bus.load(tid)['aiReview']['status'] == 'started'
        assert len(FakeClient.prompts) == 0, FakeClient.prompts
    finally:
        harness.Client = original
        bus.review('codex', tid, 'cancel', 'test cleanup')


@check('AI review: stale stall snapshots preserve new leases and started callbacks; exhausted manual resume is bounded')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    workspace_file()
    bus, sessions = fresh_review_bus('final-races')
    original = harness.Client
    harness.Client = FakeClient
    try:
        tid = _review_task(bus, sessions, ['races'])
        task = bus.load(tid)
        task['aiReview'].update(attempts=AI_RETRY_LIMIT, sentAt='2000-01-01T00:00:00+00:00')
        bus.save(task)
        stale = bus.load(tid)
        token = task['aiReview']['token']
        bus.ai_review('deepseek', tid, 0, token, 'started')
        bus.pump_ai_review(stale)
        assert bus.load(tid)['aiReview']['status'] == 'started'
        bus.review('codex', tid, 'cancel', 'cleanup')
        tid = _review_task(bus, sessions, ['resume'])
        task = bus.load(tid)
        task['aiReview'].update(attempts=AI_RETRY_LIMIT, sentAt='2000-01-01T00:00:00+00:00')
        bus.save(task)
        bus.pump()
        assert bus.load(tid)['aiReview']['status'] == 'stalled'
        assert bus.release(tid)['aiReview'] == 'sent'
        assert bus.load(tid)['aiReview']['attempts'] == AI_RETRY_LIMIT + 1
        bus.pump()
        assert bus.load(tid)['aiReview']['status'] == 'sent'
        task = bus.load(tid)
        task['aiReview'].update(status='sending', leaseId='new-owner', lease=__import__('agentlink.util', fromlist=['now']).now())
        bus.save(task)
        assert not bus._ai_review_stall(tid, 0, 'stale snapshot')
        assert not bus._ai_review_mark(tid, 0, 'old-owner', 'sent')
        assert bus.load(tid)['aiReview']['leaseId'] == 'new-owner'
        bus.review('codex', tid, 'cancel', 'cleanup')
    finally:
        harness.Client = original


@check('AI review: cancellation during session preflight prevents prompt delivery')
def _():
    os.environ.pop('AGENTLINK_NO_DISPATCH', None)
    workspace_file()
    bus, sessions = fresh_review_bus('preflight-cancel')
    original = harness.Client
    harness.Client = FakeClient
    try:
        tid = _review_task(bus, sessions, ['preflight'])
        task = bus.load(tid)
        task['aiReview'].update(status='failed', nextRetryAt=None)
        bus.save(task)
        class CancelClient(FakeClient):
            def call(self, method, request):
                if method == 'session.list':
                    bus.review('codex', tid, 'cancel', 'cancel during preflight')
                return super().call(method, request)
        harness.Client = CancelClient
        FakeClient.prompts = []
        assert bus.ai_review_send(tid) == 'stale'
        assert FakeClient.prompts == []
        assert bus.load(tid)['state'] == 'cancelled'
    finally:
        harness.Client = original


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding='utf-8')
        except AttributeError: pass
    for name, ok, why in RESULTS: print(('PASS ' if ok else 'FAIL ') + name + ('  — ' + why if why else ''))
    failed = sum(1 for r in RESULTS if not r[1])
    print('%d/%d passed' % (len(RESULTS) - failed, len(RESULTS)))
    import shutil
    shutil.rmtree(str(TMP), ignore_errors=True)
    sys.exit(1 if failed else 0)
