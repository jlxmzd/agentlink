"""受阻上报协议与漏报检测：全部在临时沙箱里，用假 RPC / 假时钟，不碰真实任务与真实宿主。

    python -I tests/test_blockers.py

覆盖：双执行方上报总控、结构化字段、幂等与"报告变更再通知"、未解决不可交付/领取、
解阻授权与续跑意见（含新执行轮次）、漏报检测与去重、仍在运行/未知不误报、跨轮次 CAS、
Claude 转录判定（end_turn / 新 user / tool_use / 截断）。
"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

SANDBOX = tempfile.TemporaryDirectory(prefix='agentlink-blockers-')
# Both homes must be pinned before importing agentlink: AGENTLINK_HOME outranks AGENTLINK_USER_HOME for the
# project registry, so leaving it unset would let this suite touch a developer's real projects.json.
os.environ['AGENTLINK_USER_HOME'] = str(Path(SANDBOX.name) / 'home')
os.environ['AGENTLINK_HOME'] = str(Path(SANDBOX.name) / 'home' / '.agentlink')
os.environ['AGENTLINK_NO_WAKE'] = '1'
os.environ['AGENTLINK_NO_DISPATCH'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentlink import bus as B, harness, project, sessions

RESULTS = []


def check(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ''))
    except Exception as error:                       # noqa: BLE001 - the point is to report every failure
        RESULTS.append((name, False, '%s: %s' % (type(error).__name__, error)))


def eq(actual, expected, what='value'):
    if actual != expected:
        raise AssertionError('%s expected %r but got %r' % (what, expected, actual))


def ok(value, what='expected truthy'):
    if not value:
        raise AssertionError(what + ' was falsy')


class Fixture(object):
    """One temporary project with three bound agents and a fake Harness."""

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        root = Path(self.temp.name)
        self.p = project.Project(root)
        self.p.apply({'codex': {'id': '00000000-0000-0000-0000-000000000001'},
                      'claude': {'id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'claude-' + root.name))},
                      'deepseek': {'id': 'session-ds'}},
                     options={'autoAccept': [], 'lead': 'codex'})
        self.bus = self.p.bus()
        self.root = root

    def close(self):
        self.temp.cleanup()

    def task(self, poster='codex', to='deepseek', level='L1'):
        allowed = ['src/task%d' % len(self.bus.all())]
        return self.bus.post(poster, to, 'task', 'spec', level=level, paths=allowed, no_dispatch=True)['id']


class FakeHarness(object):
    """Stand-in for harness.Client; `running` drives the DeepSeek idle detection."""

    running = False

    def __init__(self): pass

    def call(self, method, payload):
        return {'items': []}


def find_session(_client, sid):
    return {'sessionId': sid, 'cwd': '/p', 'running': FakeHarness.running}


# -- 上报：字段、负责人、幂等 ---------------------------------------------------
def test_an_executor_blocks_with_the_structured_report():
    f = Fixture()
    try:
        tid = f.task('codex', 'deepseek')
        f.bus.claim('deepseek', tid)
        out = f.bus.block('deepseek', tid, '缺少配置样例，无法继续',
                          missing='config/sample.yml 不存在', attempts='试过复制默认值，仍缺字段',
                          help_needed='请提供一份样例', evidence=['logs/blocker-1.txt'])
        eq(out['state'], 'blocked', 'state')
        eq(out['owner'], 'codex', 'the lead owns it')
        t = f.bus.load(tid)
        r = t['blocker']
        eq(r['reason'], '缺少配置样例，无法继续', 'reason')
        eq(r['missing'], 'config/sample.yml 不存在', 'missing')
        eq(r['attempts'], '试过复制默认值，仍缺字段', 'attempts')
        eq(r['helpNeeded'], '请提供一份样例', 'helpNeeded')
        eq(r['evidence'], ['logs/blocker-1.txt'], 'evidence')
        eq(r['by'], 'deepseek', 'reporter')
        eq(r['owner'], 'codex', 'owner')
        eq(r['status'], 'open', 'open while unresolved')
        ok(r.get('at'), 'timestamps the report')
        ok(tid in f.bus.locks(), 'path locks are kept while blocked')
    finally:
        f.close()


def test_both_executors_report_to_the_lead():
    f = Fixture()
    try:
        a = f.task('codex', 'deepseek')
        b = f.task('codex', 'claude')
        f.bus.claim('deepseek', a)
        f.bus.claim('claude', b)
        f.bus.block('deepseek', a, 'DS cannot finish', missing='lib')
        f.bus.block('claude', b, 'claude cannot finish', missing='tool')
        for tid in (a, b):
            t = f.bus.load(tid)
            eq(t['state'], 'blocked', 'blocked ' + tid)
            eq(t['blocker']['owner'], 'codex', 'both reach the lead')
        # and a user-posted task still reaches the lead
        c = f.task('user', 'deepseek')
        f.bus.claim('deepseek', c)
        f.bus.block('deepseek', c, 'user task blocked')
        eq(f.bus.load(c)['blocker']['owner'], 'codex', 'a user-posted block still reaches the lead')
    finally:
        f.close()


def test_an_identical_report_is_idempotent_and_a_changed_one_notifies_again():
    f = Fixture()
    try:
        tid = f.task('codex', 'deepseek')
        f.bus.claim('deepseek', tid)
        f.bus.block('deepseek', tid, 'need access', missing='token')
        first = f.bus.load(tid)
        again = f.bus.block('deepseek', tid, 'need access', missing='token')
        ok(again.get('duplicate'), 'an identical report is a duplicate')
        second = f.bus.load(tid)
        eq(second.get('updated'), first.get('updated'), 'no rewrite for an identical report')
        # a changed report must be recorded again
        changed = f.bus.block('deepseek', tid, 'need access', missing='token and scope')
        ok(not changed.get('duplicate'), 'a changed report is not a duplicate')
        eq(f.bus.load(tid)['blocker']['missing'], 'token and scope', 'the report was updated')
    finally:
        f.close()


def test_blocking_needs_a_note_and_a_permitted_reporter():
    f = Fixture()
    try:
        tid = f.task('codex', 'deepseek')
        f.bus.claim('deepseek', tid)
        try:
            f.bus.block('deepseek', tid, '   ')
        except ValueError as error:
            ok('note' in str(error).lower() or '需要' in str(error), 'empty note refused')
        else:
            raise AssertionError('an empty note must be refused')
        try:
            f.bus.block('claude', tid, 'not my task')          # claude is not the executor
        except ValueError:
            pass
        else:
            raise AssertionError('only the current executor may report')
    finally:
        f.close()


# -- 未解决：不可交付、不可领取 -----------------------------------------------
def test_an_unresolved_block_cannot_be_delivered_or_claimed():
    f = Fixture()
    try:
        tid = f.task('codex', 'deepseek')
        f.bus.block('deepseek', tid, 'cannot start')
        for call, label in ((lambda: f.bus.deliver('deepseek', tid, 'done'), 'deliver'),
                            (lambda: f.bus.claim('deepseek', tid), 'claim')):
            try:
                call()
            except ValueError:
                pass
            else:
                raise AssertionError(label + ' must be refused while blocked is unresolved')
        eq(f.bus.load(tid)['state'], 'blocked', 'still blocked afterwards')
    finally:
        f.close()


# -- 解阻：授权、意见、新轮次 ------------------------------------------------
def test_unblock_is_authorised_and_records_the_resolution():
    f = Fixture()
    try:
        tid = f.task('codex', 'deepseek')
        f.bus.claim('deepseek', tid)
        f.bus.block('deepseek', tid, 'need the sample config', missing='config/sample.yml')
        try:
            f.bus.unblock('deepseek', tid, 'resolved myself')
        except ValueError:
            pass
        else:
            raise AssertionError('the executor must not resolve its own block')
        try:
            f.bus.unblock('codex', tid, '   ')
        except ValueError:
            ok(True, 'a note is required')
        else:
            raise AssertionError('an empty resolution must be refused')
        before = f.bus.dispatch_generation(f.bus.load(tid))
        out = f.bus.unblock('codex', tid, '已补充 config/sample.yml，请从第 3 步继续')
        eq(out['state'], 'posted', 'back to the queue')
        eq(out['to'], 'deepseek', 'the original executor keeps the task')
        t = f.bus.load(tid)
        r = t['blocker']
        eq(r['status'], 'resolved', 'resolved')
        eq(r['resolution'], '已补充 config/sample.yml，请从第 3 步继续', 'resolution recorded')
        eq(r['resolvedBy'], 'codex', 'resolver recorded')
        eq(f.bus.dispatch_generation(t), before + 1, 'a new execution round')
        ok(tid in f.bus.locks(), 'locks never moved')
    finally:
        f.close()


def test_the_original_executor_gets_the_resolution_opinion():
    f = Fixture()
    try:
        tid = f.task('codex', 'deepseek')
        f.bus.claim('deepseek', tid)
        f.bus.block('deepseek', tid, 'need the sample')
        f.bus.unblock('codex', tid, '已补齐依赖，继续实现第 2 步')
        text = f.bus.brief('deepseek', 'UserPromptSubmit')
        ok(text, 'the executor is told about the resumed task')
        ok('已补齐依赖' in text or '继续实现' in text, 'the resolution reaches the executor: ' + text[:120])
    finally:
        f.close()


# -- 漏报检测 ---------------------------------------------------------------
def _aged_execution(bus, tid, seconds=0, session='session-ds'):
    """Pretend this task's execution started `seconds` ago."""
    with bus.lock:
        t = bus.load(tid)
        stamp = (datetime.datetime.now().astimezone() - datetime.timedelta(seconds=seconds)).isoformat()
        t['execution'] = {'sessionId': session, 'startedAt': stamp,
                          'generation': bus.dispatch_generation(t)}
        bus.save(t)


def test_an_idle_deepseek_without_a_delivery_is_reported_once():
    f = Fixture()
    try:
        FakeHarness.running = False
        tid = f.task('codex', 'deepseek')
        f.bus.claim('deepseek', tid)
        with f.bus.lock:
            t = f.bus.load(tid)
            t['dispatch'] = {'state': 'accepted', 'attempt': 0, 'at': t['created'], 'sessionId': 'session-ds'}
            f.bus.save(t)
        _aged_execution(f.bus, tid, B.STALL_SECONDS + 60)
        with patch.object(harness, 'Client', FakeHarness), \
             patch.object(harness, 'find_session', side_effect=find_session):
            f.bus.pump_execution(f.bus.load(tid))
        t = f.bus.load(tid)
        eq((t.get('executionIssue') or {}).get('status'), 'unreported', 'an issue is recorded')
        eq(t['state'], 'claimed', 'the real state is preserved')
        ok(tid in f.bus.locks(), 'locks are preserved')
        ok(t['executionIssue']['note'].find('没有交付或受阻回执') != -1, 'it does not infer the reason')
        # a second pump must not report again
        with patch.object(harness, 'Client', FakeHarness), \
             patch.object(harness, 'find_session', side_effect=find_session):
            first_at = t['executionIssue']['at']
            f.bus.pump_execution(f.bus.load(tid))
            eq(f.bus.load(tid)['executionIssue']['at'], first_at, 'the second pump is deduplicated')
    finally:
        f.close()


def test_a_running_or_unknown_executor_is_not_reported():
    f = Fixture()
    try:
        tid = f.task('codex', 'deepseek')
        f.bus.claim('deepseek', tid)
        with f.bus.lock:
            t = f.bus.load(tid)
            t['dispatch'] = {'state': 'accepted', 'attempt': 0, 'at': t['created'], 'sessionId': 'session-ds'}
            f.bus.save(t)
        _aged_execution(f.bus, tid, B.STALL_SECONDS + 60)
        # still running -> no report
        FakeHarness.running = True
        with patch.object(harness, 'Client', FakeHarness), \
             patch.object(harness, 'find_session', side_effect=find_session):
            f.bus.pump_execution(f.bus.load(tid))
        eq(f.bus.load(tid).get('executionIssue'), None, 'a running executor is not reported')
        # transport failure -> unknown, so no report either
        FakeHarness.running = False
        with patch.object(harness, 'Client', side_effect=OSError('bridge down')):
            f.bus.pump_execution(f.bus.load(tid))
        eq(f.bus.load(tid).get('executionIssue'), None, 'an unreachable host is unknown, not idle')
        # not idle long enough -> no report
        _aged_execution(f.bus, tid, 5)
        with patch.object(harness, 'Client', FakeHarness), \
             patch.object(harness, 'find_session', side_effect=find_session):
            f.bus.pump_execution(f.bus.load(tid))
        eq(f.bus.load(tid).get('executionIssue'), None, 'the quiet window has not elapsed')
    finally:
        f.close()


def test_paused_recalling_terminal_and_old_rounds_are_never_reported():
    f = Fixture()
    try:
        FakeHarness.running = False
        tid = f.task('codex', 'deepseek')
        f.bus.claim('deepseek', tid)
        with f.bus.lock:
            t = f.bus.load(tid)
            t['dispatch'] = {'state': 'accepted', 'attempt': 0, 'at': t['created'], 'sessionId': 'session-ds'}
            f.bus.save(t)
        _aged_execution(f.bus, tid, B.STALL_SECONDS + 60)
        # a stale generation must be skipped
        with f.bus.lock:
            t = f.bus.load(tid)
            t['execution']['generation'] = f.bus.dispatch_generation(t) + 1
            f.bus.save(t)
        with patch.object(harness, 'Client', FakeHarness), \
             patch.object(harness, 'find_session', side_effect=find_session):
            f.bus.pump_execution(f.bus.load(tid))
        eq(f.bus.load(tid).get('executionIssue'), None, 'an old round is not reported')
        # paused is likewise skipped
        _aged_execution(f.bus, tid, B.STALL_SECONDS + 60)
        f.bus.control('user', tid, 'pause')
        with patch.object(harness, 'Client', FakeHarness), \
             patch.object(harness, 'find_session', side_effect=find_session):
            f.bus.pump_execution(f.bus.load(tid))
        eq(f.bus.load(tid).get('executionIssue'), None, 'paused is not reported')
    finally:
        f.close()


def test_a_late_observation_cannot_write_after_the_task_moved_on():
    f = Fixture()
    try:
        FakeHarness.running = False
        tid = f.task('codex', 'deepseek')
        f.bus.claim('deepseek', tid)
        with f.bus.lock:
            t = f.bus.load(tid)
            t['dispatch'] = {'state': 'accepted', 'attempt': 0, 'at': t['created'], 'sessionId': 'session-ds'}
            f.bus.save(t)
        _aged_execution(f.bus, tid, B.STALL_SECONDS + 60)
        snapshot = f.bus.load(tid)
        # meanwhile the executor delivers: the old snapshot must not produce an issue
        f.bus.deliver('deepseek', tid, 'done')
        with patch.object(harness, 'Client', FakeHarness), \
             patch.object(harness, 'find_session', side_effect=find_session):
            f.bus.pump_execution(snapshot)                   # the stale snapshot
        t = f.bus.load(tid)
        eq(t.get('executionIssue'), None, 'a stale observation writes nothing')
        ok(t['state'] in ('delivered', 'accepted'), 'the newer state survives')
    finally:
        f.close()


# -- Claude 转录判定 -------------------------------------------------------
def _claude_fixture(f):
    """Write a transcript for the bound Claude session under this sandbox's fake home."""
    sid = f.p.binding('claude')['id']
    cwd = str(f.root)
    folder = B.user_home() / '.claude' / 'projects' / sessions.claude_dir_name(cwd)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (sid + '.jsonl')
    return sid, cwd, path


def _row(kind, stamp, stop=None, sid=None, cwd=None):
    row = {'type': kind, 'timestamp': stamp.isoformat()}
    if sid: row['sessionId'] = sid
    if cwd: row['cwd'] = cwd
    if kind == 'assistant': row['message'] = {'stop_reason': stop}
    return json.dumps(row) + '\n'


def test_claude_transcript_states_are_classified():
    f = Fixture()
    try:
        sid, cwd, path = _claude_fixture(f)
        since = datetime.datetime.now().astimezone().isoformat()
        base = datetime.datetime.now().astimezone()
        # end_turn after the claim = explicitly finished
        path.write_text(_row('user', base, sid=sid, cwd=cwd) + _row('assistant', base, 'end_turn', sid, cwd),
                        encoding='utf-8')
        eq(sessions.claude_execution(cwd, sid, since), False, 'end_turn means finished')
        # a later user message supersedes it
        path.write_text(_row('user', base, sid=sid, cwd=cwd) +
                        _row('assistant', base, 'end_turn', sid, cwd) +
                        _row('user', base, sid=sid, cwd=cwd), encoding='utf-8')
        eq(sessions.claude_execution(cwd, sid, since), True, 'a new user turn means still working')
        # tool_use means the turn is still running
        path.write_text(_row('user', base, sid=sid, cwd=cwd) +
                        _row('assistant', base, 'tool_use', sid, cwd), encoding='utf-8')
        eq(sessions.claude_execution(cwd, sid, since), True, 'tool_use is still working')
        # a truncated line cannot be classified
        path.write_text(_row('user', base, sid=sid, cwd=cwd) + '{"type": "assist', encoding='utf-8')
        eq(sessions.claude_execution(cwd, sid, since), None, 'a truncated transcript is unknown')
        # nothing after the claim is unknown, not finished
        path.write_text(_row('assistant', base + datetime.timedelta(hours=-2), 'end_turn', sid, cwd),
                        encoding='utf-8')
        eq(sessions.claude_execution(cwd, sid, since), None, 'an older end_turn is not this round')
    finally:
        f.close()


def test_a_claude_silent_finish_is_reported_to_the_lead():
    f = Fixture()
    try:
        sid, cwd, path = _claude_fixture(f)
        tid = f.task('codex', 'claude')
        f.bus.claim('claude', tid)
        base = datetime.datetime.now().astimezone()
        path.write_text(_row('user', base, sid=sid, cwd=cwd) + _row('assistant', base, 'end_turn', sid, cwd),
                        encoding='utf-8')
        _aged_execution(f.bus, tid, B.STALL_SECONDS + 60, session=sid)
        f.bus.pump_execution(f.bus.load(tid))
        t = f.bus.load(tid)
        eq((t.get('executionIssue') or {}).get('status'), 'unreported', 'the silent finish is reported')
        eq(t['state'], 'claimed', 'state preserved')
        eq(t['blocker'] if t.get('blocker') else None, None, 'a stall is not turned into a blocker')
    finally:
        f.close()


# -- CLI 参数 --------------------------------------------------------------
def test_cli_block_and_unblock_arguments_round_trip():
    f = Fixture()
    try:
        tid = f.task('codex', 'deepseek')
        f.bus.claim('deepseek', tid)
        cli = [sys.executable, '-I', str(Path(__file__).resolve().parents[1] / 'agentlink.py'),
               '--as', 'deepseek', '--project', str(f.root)]
        done = subprocess.run(cli + ['block', tid, '--note', 'CLI 受阻', '--missing', 'CLI 缺件',
                                     '--attempts', 'CLI 试过', '--help-needed', 'CLI 需要帮助',
                                     '--evidence', 'cli.log'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        eq(done.returncode, 0, 'block exits 0: ' + done.stderr[:200])
        r = f.bus.load(tid)['blocker']
        eq(r['reason'], 'CLI 受阻', 'reason via CLI')
        eq(r['missing'], 'CLI 缺件', 'missing via CLI')
        eq(r['attempts'], 'CLI 试过', 'attempts via CLI')
        eq(r['helpNeeded'], 'CLI 需要帮助', 'helpNeeded via CLI')
        eq(r['evidence'], ['cli.log'], 'evidence via CLI')
        done = subprocess.run(cli[:3] + ['--as', 'codex', '--project', str(f.root)] +
                              ['unblock', tid, '--note', 'CLI 已解决'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        eq(done.returncode, 0, 'unblock exits 0: ' + done.stderr[:200])
        eq(f.bus.load(tid)['blocker']['resolution'], 'CLI 已解决', 'resolution via CLI')
    finally:
        f.close()


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding='utf-8')
        except AttributeError: pass
    # unittest is convenient for the pieces that need real isolation of module state
    class Suite(unittest.TestCase):
        pass

    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            check(name, fn)
    for name, passed, why in RESULTS:
        print(('PASS ' if passed else 'FAIL ') + name + (('  — ' + why) if why else ''))
    failed = sum(1 for r in RESULTS if not r[1])
    print('%d/%d passed' % (len(RESULTS) - failed, len(RESULTS)))
    sys.exit(1 if failed else 0)
