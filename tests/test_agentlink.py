"""Regression checks for AgentLink. Every check runs against a temporary home and project: real host
stores and configs are never read or written, nothing is dispatched and no agent is woken.

  python -I tests/test_agentlink.py
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import urllib.request

TMP = Path(tempfile.mkdtemp(prefix='agentlink-test-'))
os.environ['AGENTLINK_USER_HOME'] = str(TMP / 'home')
os.environ['AGENTLINK_NO_DISPATCH'] = '1'
os.environ['AGENTLINK_NO_WAKE'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentlink import install, project as P, server, sessions  # noqa: E402
from agentlink.cli import main  # noqa: E402

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


ROOT = TMP / 'proj'
(ROOT / 'src').mkdir(parents=True)
(ROOT / 'AGENTS.md').write_text('# Rules\n\nKeep me.\n', encoding='utf-8')
proj = P.Project(ROOT)
BIND = {'claude': {'id': 'c1111111-1111-1111-1111-111111111111', 'title': 'Claude chat'},
        'codex': {'id': '01a143dc-fea9-7491-934e-95b1c08226af', 'title': 'Codex thread'},
        'deepseek': {'id': 'session-abc', 'title': 'DS'}}


@check('apply writes AGENTLINK.md, managed blocks, gitignore and registers the project')
def _():
    files = proj.apply(BIND, roles={'claude': '审查'}, options={'integrator': 'claude'})
    assert {'AGENTLINK.md', 'AGENTS.md', 'CLAUDE.md', '.gitignore', '.agentlink/config.json'} <= set(files), files
    doc = (ROOT / 'AGENTLINK.md').read_text(encoding='utf-8')
    assert BIND['codex']['id'] in doc and '审查' in doc and '**集成人**' in doc and '**总控**' in doc
    agents = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
    assert 'Keep me.' in agents and agents.count(P.BLOCK_START) == 1
    assert '.agentlink/bus/' in (ROOT / '.gitignore').read_text(encoding='utf-8')
    assert any(p.root == ROOT for p in P.projects())


@check('re-applying replaces the managed block instead of duplicating it')
def _():
    proj.apply(BIND)
    proj.apply(BIND)
    for name in ('AGENTS.md', 'CLAUDE.md'):
        assert (ROOT / name).read_text(encoding='utf-8').count(P.BLOCK_START) == 1
    assert (ROOT / '.gitignore').read_text(encoding='utf-8').count('.agentlink/bus/') == 1


@check('unbinding an agent removes it; find() resolves subdirectories')
def _():
    proj.apply({'deepseek': None})
    assert proj.binding('deepseek') is None and proj.binding('claude')
    assert P.find(ROOT / 'src').root == ROOT
    assert P.find(TMP) is None
    proj.apply(BIND)


bus = proj.bus()


@check('post / claim / deliver / accept round trip')
def _():
    t = bus.post('codex', 'claude', 'Fix', 'do it', paths=['src/a.py'])
    assert t['state'] == 'posted'
    bus.claim('claude', t['id'])
    bus.deliver('claude', t['id'], 'done', 'ran tests')
    assert bus.review('codex', t['id'], 'accept')['state'] == 'accepted'


@check('path locks conflict on overlapping prefixes and release when finished')
def _():
    t = bus.post('codex', 'claude', 'Lock', 'x', paths=['src/pkg'])
    raises(lambda: bus.post('claude', 'codex', 'Other', 'y', paths=['src/pkg/mod.py']), 'Lock conflict')
    bus.review('codex', t['id'], 'cancel')
    bus.post('claude', 'codex', 'Other', 'y', paths=['src/pkg/mod.py'])


@check('absolute and parent paths are refused')
def _():
    raises(lambda: bus.post('codex', 'claude', 'Bad', 'x', paths=['C:/x']), 'project-relative')
    raises(lambda: bus.post('codex', 'claude', 'Bad', 'x', paths=['../x']), 'project-relative')


@check('only the right party can act')
def _():
    t = bus.post('codex', 'claude', 'Roles', 'x')
    raises(lambda: bus.claim('codex', t['id']), 'cannot act')
    bus.deliver('claude', t['id'], 'ok')
    raises(lambda: bus.review('claude', t['id'], 'accept'), 'cannot act')
    raises(lambda: bus.review('codex', t['id'], 'reject'), 'concrete problem')


@check('two rejects produce take-back advice')
def _():
    t = bus.post('claude', 'codex', 'Rework', 'x')
    for _ in range(2):
        bus.deliver('codex', t['id'], 'try')
        out = bus.review('claude', t['id'], 'reject', 'still wrong')
    assert 'advice' in out


@check('user can post from the panel and review; DeepSeek tasks are held without a dispatch')
def _():
    t = bus.post('user', 'deepseek', 'Package', 'pack it', level='L0')
    assert t['dispatch'] == 'held'
    bus.deliver('deepseek', t['id'], 'packed')
    assert bus.review('user', t['id'], 'accept')['state'] == 'accepted'


@check('L2/L3 tasks for DeepSeek need an implementation guide; show includes it')
def _():
    raises(lambda: bus.post('codex', 'deepseek', 'Hard', 'x', level='L2'), '实现指引')
    t = bus.post('codex', 'deepseek', 'Hard', 'spec body', level='L2', guide='用适配器模式，保持 run() 签名')
    text = bus.show(t['id'])
    assert '实现指引' in text and '适配器模式' in text
    bus.post('codex', 'deepseek', 'Easy', 'x', level='L1')                       # simple tasks need none
    bus.post('user', 'deepseek', 'From panel', 'x', level='L3')                   # the person decides for themselves


@check('two code rejects escalate a DeepSeek task to Claude; design rejects go to Codex')
def _():
    bus.mark_alive('claude')                                                    # Claude's plugin is polling
    t = bus.post('codex', 'deepseek', 'Escalate code', 'x')
    for i in range(2):
        bus.deliver('deepseek', t['id'], 'try %d' % i)
        out = bus.review('codex', t['id'], 'reject', 'still broken', kind='code')
    assert out['escalated'] == 'claude' and out['to'] == 'claude' and out['state'] == 'posted', out
    assert '整改' in bus.show(t['id'])
    bus.claim('claude', t['id'])
    t2 = bus.post('claude', 'deepseek', 'Escalate design', 'x')
    for i in range(2):
        bus.deliver('deepseek', t2['id'], 'try')
        out = bus.review('claude', t2['id'], 'reject', 'wrong approach', kind='design')
    assert out['escalated'] == 'codex', out
    events = [e for e in bus.read_events(0) if e['task'] == t['id'] and e['action'] == 'escalate']
    assert events and bus.concerns('codex', events[0])                            # the lead is told


@check('DeepSeek L2/L3 deliveries remind the lead to audit even when Claude posted them')
def _():
    t = bus.post('claude', 'deepseek', 'Audit me', 'x', level='L2', guide='方向')
    bus.deliver('deepseek', t['id'], 'done')
    assert any(item.startswith('待审计 ' + t['id']) for item in bus.actionable('codex'))
    event = [e for e in bus.read_events(0) if e['task'] == t['id'] and e['action'] == 'deliver'][0]
    assert event.get('cc') == ['codex']
    simple = bus.post('claude', 'deepseek', 'No audit', 'x', level='L1')
    bus.deliver('deepseek', simple['id'], 'done')
    assert not any(simple['id'] in item for item in bus.actionable('codex'))
    bus.review('claude', t['id'], 'accept'); bus.review('claude', simple['id'], 'accept')


@check('automatic hand-over happens once; a second failure goes to the lead for a ruling')
def _():
    bus.mark_alive('claude')                                                    # Claude's plugin is polling
    t = bus.post('claude', 'deepseek', 'Cap', 'x')
    for _ in range(2):
        bus.deliver('deepseek', t['id'], 'try')
        out = bus.review('claude', t['id'], 'reject', 'bad', kind='code')
    assert out['escalated'] == 'claude' and out['state'] == 'claimed'           # poster takes it over directly
    bus.reassign('claude', t['id'], 'deepseek', 'try again with my notes')
    for _ in range(2):
        bus.deliver('deepseek', t['id'], 'try')
        out = bus.review('claude', t['id'], 'reject', 'still bad', kind='code')
    assert out['escalated'] == 'codex', out
    task = bus.load(t['id'])
    assert task['escalation']['ruling'] and task['escalations'] == 2


@check('the lead sees every task and can steer tasks it did not post')
def _():
    t = bus.post('claude', 'deepseek', 'Claude task', 'x', level='L2', guide='方向')
    assert t['id'] in bus.listing('codex')                                       # lead: full picture on demand
    bus.brief('codex', 'UserPromptSubmit')
    bus.deliver('deepseek', t['id'], 'done')
    assert '待审计' in bus.brief('codex', 'UserPromptSubmit')                    # audit duty reaches the lead
    assert bus.reassign('codex', t['id'], 'codex')['to'] == 'codex'               # lead takes it over
    raises(lambda: bus.reassign('deepseek', t['id'], 'claude'), 'cannot act')


@check('blocked tasks reach the lead even when it is not a party')
def _():
    t = bus.post('claude', 'deepseek', 'Blocker', 'x')
    bus.block('deepseek', t['id'], 'missing API')
    assert '受阻' in bus.brief('codex', 'UserPromptSubmit') and 'missing API' in bus.brief('codex', 'SessionStart')


@check('hook brief is change-only and scoped to the bound conversation')
def _():
    bus.brief('claude', 'UserPromptSubmit')                          # drain
    assert bus.brief('claude', 'UserPromptSubmit') == ''
    bus.post('codex', 'claude', 'Brief', 'spec text here')
    assert bus.seat_matches('claude', BIND['claude']['id'])
    assert not bus.seat_matches('claude', 'someone-else')
    assert 'Brief' in bus.brief('claude', 'UserPromptSubmit')
    assert bus.brief('claude', 'UserPromptSubmit') == ''


@check('pending answers only the bound Claude session and respects the wake switch')
def _():
    import agentlink.bus as B
    B.WAKE_QUIET = 0
    bus.post('codex', 'claude', 'Wake me', 'x')
    assert bus.pending('claude', 'other')['wake'] is False
    assert bus.pending('claude', BIND['claude']['id'])['ids']
    assert bus.pending('claude', BIND['claude']['id'])['wake'] is False     # AGENTLINK_NO_WAKE


@check('token saving: progress events never wake; a told or woken item never wakes again')
def _():
    import agentlink.bus as B
    B.WAKE_QUIET = 0
    bus.brief('codex', 'UserPromptSubmit'); bus.brief('claude', 'UserPromptSubmit')
    t = bus.post('codex', 'deepseek', 'Quiet', 'x')
    bus.claim('deepseek', t['id'])                                              # DeepSeek progress: nobody woken
    assert t['id'] not in bus.pending_for('codex')[0]
    bus.deliver('deepseek', t['id'], 'done')                                    # now Codex must review
    ids, settled = bus.pending_for('codex')
    assert ids == [t['id']] and settled
    text = bus.brief('codex', 'UserPromptSubmit')                               # Codex sees it in a turn it had anyway
    assert '待验收' in text and 'done' in text and 'accept ' + t['id'] in text  # everything needed, inline
    assert bus.pending_for('codex')[0] == []                                    # so no wake for it later
    bus.review('codex', t['id'], 'accept')
    assert t['id'] not in bus.pending_for('deepseek')[0] and bus.brief('codex', 'UserPromptSubmit') == ''


@check('token saving: a session start gets headlines, an in-turn brief gets the full detail')
def _():
    import agentlink.bus as B
    B.WAKE_QUIET = 0
    long_spec, long_receipt = '规格正文。' * 40, '回执正文。' * 40
    t = bus.post('codex', 'deepseek', 'Headline', long_spec, level='L1')
    bus.claim('deepseek', t['id'])
    bus.deliver('deepseek', t['id'], long_receipt)
    start = bus.brief('codex', 'SessionStart')
    assert t['id'] in start and '待验收' in start                                  # the item itself is visible
    assert long_spec not in start and long_receipt not in start                    # without the payloads
    assert 'show <ID>' in start                                                    # and it knows how to get them
    seat = bus.read_seat('codex'); seat['told'] = []
    from agentlink.util import write_json
    write_json(bus.seat_path('codex'), seat)
    turn = bus.brief('codex', 'UserPromptSubmit')
    assert '程序验收' in turn and '回执：' in turn                                  # a working turn still gets everything
    assert len(start) < len(turn)                                                  # and is the cheaper of the two
    bus.review('codex', t['id'], 'accept')


@check('token saving: an L0 DeepSeek receipt is one line, not the full gate dump')
def _():
    low = bus.post('codex', 'deepseek', 'L0 receipt', '机械活', level='L0')
    bus.claim('deepseek', low['id'])
    bus.deliver('deepseek', low['id'], 'picked and packed ' + 'x' * 200)
    text = bus.brief('codex', 'UserPromptSubmit')
    assert '待验收' in text and low['id'] in text and 'accept ' + low['id'] in text
    assert '程序验收' not in text, text                                            # no gate block for a read-only receipt
    assert len(text) < 400, len(text)
    bus.review('codex', low['id'], 'accept')
    high = bus.post('codex', 'deepseek', 'L2 receipt', '实现', level='L2', guide='方向')
    bus.claim('deepseek', high['id'])
    bus.deliver('deepseek', high['id'], 'done')
    assert '程序验收' in bus.brief('codex', 'UserPromptSubmit')                     # higher levels keep the detail
    bus.review('codex', high['id'], 'accept')


@check('token saving: a burst settles into one wake, and rework counts as new work')
def _():
    import agentlink.bus as B
    B.WAKE_QUIET = 3600
    t = bus.post('claude', 'codex', 'Burst', 'x')
    assert bus.pending_for('codex') == ([t['id']], False)                       # still settling: no wake yet
    B.WAKE_QUIET = 0
    assert bus.pending_for('codex')[1]
    bus.record_wake('codex')
    assert bus.pending_for('codex')[0] == []
    bus.deliver('codex', t['id'], 'v1'); bus.review('claude', t['id'], 'reject', 'fix it')
    assert bus.pending_for('codex')[0] == [t['id']]                             # rework 1 is a new item
    spec = bus.brief('codex', 'UserPromptSubmit')
    assert '返工意见：fix it' in spec and '规格：x' in spec
    bus.reassign('claude', t['id'], 'claude')


@check('CLI hook prints nothing outside a project or for an unbound session')
def _():
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(['hook', '--as', 'claude', '--plain', '--cwd', str(TMP), '--session', BIND['claude']['id']])
        main(['hook', '--as', 'claude', '--plain', '--cwd', str(ROOT), '--session', 'not-bound'])
    assert out.getvalue() == '', out.getvalue()


@check('CLI resolves the project from --cwd and lists tasks')
def _():
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(['list', '--as', 'claude', '--cwd', str(ROOT / 'src')])
    assert 'T-' in out.getvalue()


@check('pending outside a project answers quietly and writes no hook-errors.log line')
def _():
    import contextlib, io
    from agentlink.util import agentlink_home
    log = agentlink_home() / 'hook-errors.log'
    before = log.read_text(encoding='utf-8') if log.exists() else ''
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(['pending', '--as', 'claude', '--cwd', str(TMP), '--session', 'x'])
    assert code == 0, code
    assert json.loads(out.getvalue()) == {'ids': [], 'wake': False}, out.getvalue()
    after = log.read_text(encoding='utf-8') if log.exists() else ''
    assert after == before, after


@check('wake_status reports when Claude was last seen and whether it is online')
def _():
    import datetime
    bus.mark_alive('claude')
    status = bus.wake_status()
    assert status['claudeOnline'] is True, status
    assert 'codexSeen' in status, status
    seen = datetime.datetime.fromisoformat(status['claudeSeen'])
    assert seen.tzinfo is not None, status['claudeSeen']
    assert status['claudeLastWake'] is None or isinstance(status['claudeLastWake'], str), status


@check('session discovery reads Claude and Codex metadata for the project only')
def _():
    home = Path(os.environ['AGENTLINK_USER_HOME'])
    cdir = home / '.claude' / 'projects' / sessions.claude_dir_name(ROOT)
    cdir.mkdir(parents=True)
    (cdir / 'abc.jsonl').write_text('\n'.join(json.dumps(x) for x in [
        {'type': 'user', 'cwd': str(ROOT), 'message': {'content': 'secret text'}},
        {'type': 'custom-title', 'customTitle': '我的对话'}]) + '\n', encoding='utf-8')
    sdir = home / '.codex' / 'sessions' / '2026' / '10'
    sdir.mkdir(parents=True)
    for sid, cwd in (('11111111-1111-1111-1111-111111111111', ROOT), ('22222222-2222-2222-2222-222222222222', TMP)):
        (sdir / ('rollout-x-%s.jsonl' % sid)).write_text(
            json.dumps({'type': 'session_meta', 'payload': {'id': sid, 'cwd': str(cwd)}}) + '\n', encoding='utf-8')
    (home / '.codex' / 'session_index.jsonl').write_text(
        json.dumps({'id': '11111111-1111-1111-1111-111111111111', 'thread_name': '线程A'}) + '\n', encoding='utf-8')
    found = sessions.discover(str(ROOT))
    assert [i['title'] for i in found['claude']['items']] == ['我的对话']
    assert 'secret' not in json.dumps(found)
    assert [i['title'] for i in found['codex']['items']] == ['线程A']
    assert found['deepseek']['error']                                         # no Harness here: reported, not raised


@check('installer edits temp-home configs idempotently and uninstalls cleanly')
def _():
    home = Path(os.environ['AGENTLINK_USER_HOME'])
    (home / '.codex').mkdir(parents=True, exist_ok=True)
    (home / '.codex' / 'config.toml').write_text('model = "x"\n', encoding='utf-8')
    (home / '.dsh' / 'profiles' / 'desktop').mkdir(parents=True)
    (home / '.dsh' / 'profiles' / 'desktop' / 'cordis.patch.yml').write_text('- insert:\n    - id: other\n', encoding='utf-8')
    (home / '.claude' / 'settings.json').write_text('{"env": {"KEEP": "1"}}', encoding='utf-8')
    parts = {'claude': True, 'codex': True, 'harness': True}
    install.run(parts)
    second = install.run(parts)['report']
    assert all('unchanged' in r for r in second[:2]), second
    s = install.status()
    assert s['claude']['installed'] and s['codex']['installed'] and s['harness']['installed']
    settings = json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))
    assert settings['env']['KEEP'] == '1' and settings['env']['AGENTLINK_CLI'].endswith('agentlink.py')
    manifest = json.loads((install.CODEX_DIR / '.codex-plugin' / 'plugin.json').read_text(encoding='utf-8'))
    assert 'hook --as codex --event SessionStart' in manifest['hooks']['hooks']['SessionStart'][0]['hooks'][0]['command']
    install.run(parts, remove=True)
    s = install.status()
    assert not (s['claude']['installed'] or s['codex']['installed'] or s['harness']['installed'])
    settings = json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))
    assert settings == {'env': {'KEEP': '1'}}, settings
    assert 'id: other' in (home / '.dsh' / 'profiles' / 'desktop' / 'cordis.patch.yml').read_text(encoding='utf-8')


@check('panel server: token required, foreign Origin refused, API works with token')
def _():
    srv = server.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = 'http://127.0.0.1:%d' % port

        def call(path, body=None, headers=None):
            req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                                         dict({'Content-Type': 'application/json'}, **(headers or {})))
            try:
                with urllib.request.urlopen(req, timeout=10) as r: return r.status, json.loads(r.read() or b'null')
            except urllib.error.HTTPError as e: return e.code, None
        page = urllib.request.urlopen(base + '/', timeout=10).read().decode()
        assert server.TOKEN in page
        assert call('/api/projects')[0] == 403
        tok = {'X-AgentLink-Token': server.TOKEN}
        assert call('/api/projects', headers=dict(tok, Origin='http://evil.example'))[0] == 403
        code, data = call('/api/projects', headers=tok)
        assert code == 200 and any(p['root'] == str(ROOT) for p in data)
        code, data = call('/api/task/post', {'root': str(ROOT), 'to': 'codex', 'title': 'From panel', 'spec': 'x'}, tok)
        assert code == 200 and data['state'] == 'posted'
        code, data = call('/api/tasks?root=' + urllib.request.quote(str(ROOT)), headers=tok)
        assert code == 200 and any(t['title'] == 'From panel' for t in data['tasks'])
        assert call('/api/task/post', {'root': str(ROOT), 'to': 'codex', 'title': '', 'spec': 'x'}, tok)[0] == 400
    finally:
        srv.shutdown(); srv.server_close()


@check('program gate: checks bounce failures to DeepSeek, passing L1 work is accepted without any AI review')
def _():
    import subprocess as sp
    groot = TMP / 'gitproj'
    (groot / 'src').mkdir(parents=True)
    (groot / 'src' / 'a.txt').write_text('one\n', encoding='utf-8')
    (groot / 'other.txt').write_text('x\n', encoding='utf-8')
    g = lambda *args: sp.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=str(groot),
                             capture_output=True, check=True)
    g('init', '-q'); g('add', '-A'); g('commit', '-q', '-m', 'base')
    gp = P.Project(groot)
    gp.apply({'codex': {'id': 'c'}, 'deepseek': {'id': 'd'}})
    gb = gp.bus()
    check = 'python -c "import sys; sys.exit(0 if open(\'src/a.txt\').read().strip() == \'two\' else 1)"'
    t = gb.post('codex', 'deepseek', 'Gate', 'make it two', paths=['src'], checks=[check])
    gb.claim('deepseek', t['id'])
    raises(lambda: gb.deliver('deepseek', t['id'], 'done'), '验收命令未通过')        # bounced, nobody woken
    assert gb.load(t['id'])['state'] == 'claimed'
    assert t['id'] not in gb.pending_for('codex')[0]
    (groot / 'src' / 'a.txt').write_text('two\n', encoding='utf-8')
    out = gb.deliver('deepseek', t['id'], 'done')
    assert out['state'] == 'accepted' and '程序自动验收' in out['auto'], out
    task = gb.load(t['id'])
    assert task['gate']['files'] == ['src/a.txt'] and task['gate']['checks']['ok']
    assert t['id'] not in gb.pending_for('codex')[0]                              # Codex spends nothing on it


@check('program gate: out-of-scope changes and repeated failures still go to the poster, with the facts inline')
def _():
    groot = TMP / 'gitproj'
    gb = P.Project(groot).bus()
    t = gb.post('codex', 'deepseek', 'Scope', 'touch src only', paths=['src/b.txt'], checks=['python -c "pass"'])
    (groot / 'src' / 'b.txt').write_text('new\n', encoding='utf-8')
    (groot / 'other.txt').write_text('changed\n', encoding='utf-8')
    out = gb.deliver('deepseek', t['id'], 'done')
    assert out['state'] == 'delivered' and out['outside'] == ['other.txt'], out
    text = gb.brief('codex', 'UserPromptSubmit')
    assert '程序验收：全部通过' in text and 'other.txt' in text and 'src/b.txt +1' in text, text
    gb.review('codex', t['id'], 'accept')
    t2 = gb.post('codex', 'deepseek', 'Always fails', 'x', paths=['src/c.txt'], checks=['python -c "raise SystemExit(3)"'])
    for _ in range(2): raises(lambda: gb.deliver('deepseek', t2['id'], 'try'), '验收命令未通过')
    out = gb.deliver('deepseek', t2['id'], 'try')                                # third time: the poster decides
    assert out['state'] == 'delivered'
    assert '未通过' in gb.brief('codex', 'UserPromptSubmit')


@check('token saving: a large diff is not inlined, and the hint names the exact paths')
def _():
    import subprocess as sp
    from agentlink import gate as G
    groot = TMP / 'bidiffproj'
    (groot / 'big').mkdir(parents=True)
    (groot / 'big' / 'b.txt').write_text('base\n', encoding='utf-8')
    g = lambda *args: sp.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=str(groot),
                             capture_output=True, check=True)
    g('init', '-q'); g('add', '-A'); g('commit', '-q', '-m', 'base')
    gp = P.Project(groot); gp.apply({'codex': {'id': 'c'}, 'deepseek': {'id': 'd'}})
    gb = gp.bus()
    t = gb.post('codex', 'deepseek', 'Big diff', 'x', paths=['big'])
    (groot / 'big' / 'b.txt').write_text('\n'.join('line %d' % i for i in range(300)) + '\n', encoding='utf-8')
    gb.deliver('deepseek', t['id'], 'rewrote it')
    staged = gb.load(t['id'])['gate']
    assert 'diff' not in staged and staged['diffSize'] > G.INLINE_DIFF, staged.get('diffSize')
    lines = gb.gate_lines(gb.load(t['id']))
    joined = '\n'.join(lines)
    assert '未内联' in joined and 'big/b.txt' in joined, joined
    assert '-- - ' not in joined                                                   # no stray dash before the paths
    assert len(joined) < 400, len(joined)                                          # the reviewer pays a few lines
    gb.review('codex', t['id'], 'accept')


@check('token saving: list prints one short line per task, --full keeps the old detail')
def _():
    t = bus.post('codex', 'deepseek', 'Short line', 'x', level='L1', paths=['src/short.txt'])
    short = bus.listing('codex')
    full = bus.listing('codex', full=True)
    mine = [r for r in short.splitlines() if t['id'] in r][0]
    assert 'codex→deepseek' not in mine and ' L1 ' not in mine, mine               # no direction, no level
    assert 'L1' in full and 'codex→deepseek' in full                               # --full still has them
    assert len(mine) <= 80, mine
    bus.review('codex', t['id'], 'cancel')


@check('escalation skips an offline Claude; an unclaimed one falls back to the lead')
def _():
    import time as _t
    # Claude offline: a code-level failure goes straight to the lead.
    seat = bus.read_seat('claude'); seat['seen'] = 0
    from agentlink.util import write_json
    write_json(bus.seat_path('claude'), seat)
    t = bus.post('codex', 'deepseek', 'Offline', 'x')
    for _ in range(2):
        bus.deliver('deepseek', t['id'], 'try')
        out = bus.review('codex', t['id'], 'reject', 'bad', kind='code')
    assert out['escalated'] == 'codex', out
    # Claude online but never claims: the watcher hands it on after CLAIM_SECONDS.
    bus.mark_alive('claude')
    t2 = bus.post('codex', 'deepseek', 'Online', 'x')
    for _ in range(2):
        bus.deliver('deepseek', t2['id'], 'try')
        out = bus.review('codex', t2['id'], 'reject', 'bad', kind='code')
    assert out['escalated'] == 'claude', out
    task = bus.load(t2['id'])
    task['escalation']['at'] = '2000-01-01T00:00:00+00:00'
    bus.save(task)
    bus.unclaimed_fallback()
    assert bus.load(t2['id'])['to'] == 'codex'


@check('migration export carries only whitelisted settings, never tasks, paths or the root')
def _():
    mroot = TMP / 'migrate-src'
    (mroot / 'src').mkdir(parents=True)
    mp = P.Project(mroot)
    mp.apply(BIND, options={'aiReviewEnabled': True, 'aiReviewSession': {'id': 'review-1', 'title': '审查'}})
    mb = mp.bus()
    mb.post('codex', 'deepseek', 'Secret task title', 'spec body must not leak', level='L1')
    data = mp.migration_export()
    blob = json.dumps(data, ensure_ascii=False)
    assert data['agentlinkMigration'] == 1 and data['name'] == mp.name
    assert 'bindings' not in data and 'root' not in data
    assert str(mroot) not in blob and str(TMP) not in blob
    assert 'Secret task title' not in blob and 'spec body must not leak' not in blob and 'T-' not in blob
    assert data['options']['lead'] == 'codex' and data['collaboration']['agentsMd'] is True
    with_sessions = mp.migration_export(include_sessions=True)
    assert with_sessions['bindings']['codex']['id'] == BIND['codex']['id']
    assert with_sessions['aiReview']['session']['id'] == 'review-1'
    assert 'root' not in with_sessions


@check('migration import applies settings, ignores unknown fields and leaves tasks and the root alone')
def _():
    mroot = TMP / 'migrate-dst'
    (mroot / 'src').mkdir(parents=True)
    mp = P.Project(mroot)
    mp.apply(BIND)
    mb = mp.bus()
    before = mb.post('codex', 'claude', 'Existing', 'x', level='L1')
    package = {'agentlinkMigration': 1, 'name': '迁入项目', 'roles': {'claude': '迁入分工'},
               'options': {'lead': 'claude', 'autoAccept': ['L0']},
               'collaboration': {'agentsMd': False}, 'unknown': {'ignore': True},
               'bindings': {'codex': {'id': 'ghost-session', 'title': '幽灵'}}}
    out = mp.migration_apply(package)
    assert mp.name == '迁入项目' and mp.role('claude') == '迁入分工'
    assert mp.option('lead') == 'claude' and mp.option('autoAccept') == ['L0'] and mp.option('agentsMd') is False
    assert out['pending'].get('codex') == 'ghost-session'
    assert mp.summary()['pendingBindings']['codex']['id'] == 'ghost-session'
    assert str(mp.root) == str(mroot) and mb.load(before['id'])['state'] == 'posted'
    assert len(mb.all()) == 1
    raises(lambda: mp.migration_apply({'agentlinkMigration': 99}), '版本')
    raises(lambda: mp.migration_apply('not a dict'), 'JSON 对象')


@check('migration import keeps a session binding only when that session is visible here')
def _():
    mroot = TMP / 'migrate-visible'
    (mroot / 'src').mkdir(parents=True)
    mp = P.Project(mroot)
    mp.apply(BIND)
    original = sessions.discover
    sessions.discover = lambda cwd, include_all=False: {
        'claude': {'items': [{'id': 'visible-claude'}]}, 'codex': {'items': []}, 'deepseek': {'items': []}}
    try:
        out = mp.migration_apply({'agentlinkMigration': 1,
                                  'bindings': {'claude': {'id': 'visible-claude', 'title': '可见'},
                                               'codex': {'id': 'gone-codex', 'title': '不在'}}})
    finally:
        sessions.discover = original
    assert mp.binding('claude')['id'] == 'visible-claude'
    assert 'claude' not in (mp.summary()['pendingBindings'] or {})
    assert out['pending'].get('codex') == 'gone-codex'
    assert mp.binding('codex')['id'] == BIND['codex']['id']      # an invisible id never replaces a live binding


@check('AI review: an enabled review with no reachable Harness stays visible, never auto-accepted')
def _():
    aroot = TMP / 'ai-review-proj'
    (aroot / 'src').mkdir(parents=True)
    ap = P.Project(aroot)
    ap.apply({'codex': {'id': 'c2', 'title': 'C'}, 'deepseek': {'id': 'd2', 'title': 'D'}},
             options={'aiReviewEnabled': True, 'aiReviewSession': {'id': 'review-1', 'title': '审查'}})
    ab = ap.bus()
    t = ab.post('codex', 'deepseek', 'Review me', 'x', level='L1', paths=['src'])
    ab.claim('deepseek', t['id'])
    out = ab.deliver('deepseek', t['id'], 'done')
    assert out['state'] == 'ai_review' and out.get('auto') is None, out
    assert out['aiReview'] in ('failed', 'uncertain'), out
    task = ab.load(t['id'])
    assert task['state'] == 'ai_review' and task['aiReview']['status'] in ('failed', 'uncertain')
    assert task['aiReview'].get('token')
    assert 'AI 验收未送达' in '\n'.join(ab.actionable('codex'))
    raises(lambda: ab.review('codex', t['id'], 'accept'), 'ai_review')
    raises(lambda: ab.ai_review('deepseek', t['id'], 0, 'bad-token', 'accept'), '令牌')
    assert ab.load(t['id'])['aiReview']['error']
    token = ab.load(t['id'])['aiReview']['token']
    done = ab.ai_review('deepseek', t['id'], 0, token, 'accept')
    assert done['state'] == 'delivered'
    assert ab.load(t['id'])['aiReview']['status'] == 'accepted'
    assert '待验收' in '\n'.join(ab.actionable('codex'))


@check('AI review never touches work that is not a DeepSeek delivery, and stays off by default')
def _():
    nroot = TMP / 'ai-review-off'
    (nroot / 'src').mkdir(parents=True)
    np = P.Project(nroot)
    np.apply({'codex': {'id': 'c3', 'title': 'C'}, 'deepseek': {'id': 'd3', 'title': 'D'}})
    nb = np.bus()
    assert np.option('aiReviewEnabled') is False
    t = nb.post('deepseek', 'codex', 'Codex work', 'x', level='L1')
    out = nb.deliver('codex', t['id'], 'done')
    assert out['state'] == 'delivered' and 'aiReview' not in nb.load(t['id'])
    np.save_settings({'aiReviewEnabled': True, 'aiReviewSession': {'id': 'review-1', 'title': '审查'}})
    t2 = nb.post('deepseek', 'codex', 'Codex work 2', 'x', level='L1')
    assert nb.deliver('codex', t2['id'], 'done')['state'] == 'delivered'
    assert 'aiReview' not in nb.load(t2['id'])


@check('panel API: migration endpoints are token-guarded and never expose task text or the review token')
def _():
    proot = TMP / 'panel-api-proj'
    (proot / 'src').mkdir(parents=True)
    pp = P.Project(proot)
    pp.apply(BIND)
    pb = pp.bus()
    rt = pb.post('codex', 'deepseek', 'Token task', 'secret spec body', level='L1')
    task = pb.load(rt['id'])
    task['aiReview'] = {'status': 'sent', 'attempt': 0, 'token': 'review-secret-token'}
    pb.save(task)
    srv = server.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = 'http://127.0.0.1:%d' % port
        tok = {'X-AgentLink-Token': server.TOKEN}
        quoted = urllib.request.quote(str(proot))

        def call(path, body=None):
            req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                                         dict({'Content-Type': 'application/json'}, **tok))
            try:
                with urllib.request.urlopen(req, timeout=10) as r: return r.status, json.loads(r.read() or b'null')
            except urllib.error.HTTPError as e: return e.code, None

        code, data = call('/api/migration/export?root=' + quoted)
        blob = json.dumps(data, ensure_ascii=False)
        assert code == 200 and 'root' not in blob and str(proot) not in blob and 'secret spec body' not in blob
        code, out = call('/api/migration/import', {'root': str(proot), 'package': {'agentlinkMigration': 1, 'name': '面板迁入'}})
        assert code == 200 and out['applied'] == ['name'], out
        assert call('/api/migration/import', {'root': str(proot), 'package': {'agentlinkMigration': 7}})[0] == 400
        code, tasks = call('/api/tasks?root=' + quoted)
        raw = json.dumps(tasks, ensure_ascii=False)
        assert code == 200 and 'review-secret-token' not in raw
        staged = [t for t in tasks['tasks'] if t['id'] == rt['id']][0]
        assert staged['aiReview']['status'] == 'sent' and 'token' not in staged['aiReview']
        req = urllib.request.Request(base + '/api/migration/export?root=' + quoted)
        try:
            urllib.request.urlopen(req, timeout=10); raise AssertionError('expected 403')
        except urllib.error.HTTPError as error:
            assert error.code == 403
    finally:
        srv.shutdown(); srv.server_close()


@check('remove_blocks restores AGENTS.md / CLAUDE.md content outside the block')
def _():
    proj.remove_blocks()
    assert P.BLOCK_START not in (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
    assert 'Keep me.' in (ROOT / 'AGENTS.md').read_text(encoding='utf-8')


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
