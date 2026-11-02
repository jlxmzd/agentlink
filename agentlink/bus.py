"""Task bus for one project: tasks, path locks, state machine, event cursors, wakes and DeepSeek dispatch.

Ported from the MLUQOMS tri bus and made project-independent: every project keeps its own state in
<project>/.agentlink/bus/, and the bound sessions in <project>/.agentlink/config.json decide who is woken.
Hooks print nothing unless something changed for the calling agent, so idle agents spend no tokens.
"""
import datetime
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
import uuid

from .util import FileLock, now, read_json, user_home, write_json

AGENTS = ('claude', 'codex', 'deepseek')
ACTORS = AGENTS + ('user',)                              # the person, acting from the panel
LEVELS = ('L0', 'L1', 'L2', 'L3')
OPEN = ('posted', 'claimed', 'rework', 'blocked', 'ai_review')   # states that hold file locks
TERMINAL = ('accepted', 'cancelled')
REWORK_LIMIT = 2
GUIDED = ('L2', 'L3')                                    # DeepSeek gets these only with an implementation guide
KINDS = ('code', 'design')                               # what a reject is about; decides who takes over
LEAD_ACTIONS = ('block', 'escalate', 'stalled')          # events the lead must act on, whoever the task belongs to
STALL_SECONDS = 120
ALIVE_SECONDS = 300      # an agent whose plugin polled within this long counts as online
CLAIM_SECONDS = 600      # an escalation Claude has not claimed by then goes to the lead
AI_REVIEW_LABEL = {'pending': '待投递', 'sending': '投递中', 'sent': '审查中', 'accepted': '审查通过',
                   'rejected': '审查驳回', 'failed': '未送达', 'uncertain': '投递不确定'}
GATE_RETRIES = 3         # failing acceptance commands bounce back to the deliverer up to this many times minus one
WAKE_QUIET = 45          # a burst of changes must settle this long before an agent is woken: one turn, not one per event
WAKE_DEBOUNCE = 120      # at least this long between two wakes of the same agent
WAKE_PER_HOUR = 12
WAKE_PREFIX = 'AgentLink：'
WAKE_TEXT = WAKE_PREFIX + '%s 需要你处理。详情已附在本轮上下文，无需再 list / show；处理完只回一行。'
UUID = re.compile(r'\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z')
CLI_PATH = Path(__file__).resolve().parents[1] / 'agentlink.py'


def cli():
    """How agents call this program, as text they can paste into a shell."""
    return 'python "%s"' % CLI_PATH.as_posix()


def norm(path):
    p = path.replace('\\', '/').strip().strip('/')
    if not p or p.startswith('..') or re.match(r'^[A-Za-z]:', p): raise ValueError('Paths must be project-relative: ' + path)
    return p


def overlaps(a, b):
    return a == b or a.startswith(b + '/') or b.startswith(a + '/')


def line(t, short=False):
    """One task line. `list` prints the short form (no level or direction) because a caller that needs those
    is about to `show` that task anyway."""
    if short: return '%s %s %s' % (t['id'], t['state'], t['title'][:40])
    return '%s %s→%s %s %s：%s' % (t['id'], t['from'], t['to'], t['level'], t['state'], t['title'][:60])


def seen_at(seat):
    """When the agent's plugin last polled, as ISO text; seats store `seen` as epoch seconds."""
    value = (seat or {}).get('seen')
    return datetime.datetime.fromtimestamp(value).astimezone().isoformat(timespec='seconds') if value else None


class Bus:
    """File-backed store for one project; every mutation holds one exclusive lock file."""

    def __init__(self, project):
        self.project = project
        self.base = project.bus_dir
        self.tasks = self.base / 'tasks'
        self.cursors = self.base / 'cursors'
        self.seats = self.base / 'seats'
        self.events = self.base / 'events.jsonl'
        for d in (self.tasks, self.cursors, self.seats): d.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(self.base / 'bus.lock')

    # -- storage -------------------------------------------------------------
    def load(self, task_id):
        path = self.tasks / (task_id + '.json')
        if not re.fullmatch(r'T-\d{8}-\d{2,3}', task_id or '') or not path.exists():
            raise ValueError('Unknown task %s' % task_id)
        return json.loads(path.read_text(encoding='utf-8'))

    def save(self, task):
        task['updated'] = now()
        write_json(self.tasks / (task['id'] + '.json'), task)

    def all(self):
        return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(self.tasks.glob('T-*.json'))
                if re.fullmatch(r'T-\d{8}-\d{2,3}', p.stem)]

    def spec(self, task_id):
        path = self.tasks / (task_id + '.md')
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def guide(self, task_id):
        path = self.tasks / (task_id + '.guide.md')
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def lead(self):
        return self.project.option('lead')

    @staticmethod
    def needs_audit(t):
        """DeepSeek deliveries the lead audits even when someone else posted them: L2/L3 and build-lock work."""
        return t['to'] == 'deepseek' and (t['level'] in GUIDED or bool(t.get('build')))

    def concerns(self, agent, e):
        """Whether an event is the agent's business (a party to the task, or cc'd as the lead)."""
        return agent in (e['from'], e['to']) or agent in e.get('cc', ())

    def last_seq(self):
        if not self.events.exists(): return 0
        lines = self.events.read_text(encoding='utf-8').splitlines()
        return json.loads(lines[-1])['seq'] if lines else 0

    def emit(self, task, by, action, note=''):
        event = {'seq': self.last_seq() + 1, 't': now(), 'task': task['id'], 'by': by,
                 'action': action, 'from': task['from'], 'to': task['to'], 'note': (note or '')[:300]}
        lead = self.lead()
        if (action in LEAD_ACTIONS or (action == 'deliver' and self.needs_audit(task))) \
                and lead not in (task['from'], task['to'], by):
            event['cc'] = [lead]
        with self.events.open('a', encoding='utf-8') as f: f.write(json.dumps(event, ensure_ascii=False) + '\n')
        task.setdefault('history', []).append({k: event[k] for k in ('t', 'by', 'action', 'note')})
        return event

    def read_events(self, after=0):
        if not self.events.exists(): return []
        return [e for e in map(json.loads, self.events.read_text(encoding='utf-8').splitlines()) if e['seq'] > after]

    def cursor(self, agent):
        value = read_json(self.cursors / (agent + '.json'), {})
        return value.get('seq', 0)

    def set_cursor(self, agent, seq):
        write_json(self.cursors / (agent + '.json'), {'seq': seq})

    def next_id(self):
        day = datetime.date.today().strftime('%Y%m%d')
        used = [int(p.stem.rsplit('-', 1)[1]) for p in self.tasks.glob('T-%s-*.json' % day)
                if re.fullmatch(r'T-\d{8}-[0-9]+', p.stem)]
        return 'T-%s-%02d' % (day, max(used, default=0) + 1)

    def git_head(self):
        try:
            return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=str(self.project.root),
                                  capture_output=True, text=True, timeout=5).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    def conflicts(self, paths, build, skip=None):
        found = []
        for t in self.all():
            if t['id'] == skip or t['state'] not in OPEN: continue
            for p in paths:
                for q in t.get('paths', []):
                    if overlaps(p, q): found.append('%s 持有 %s（%s）' % (t['id'], q, t['to']))
            if build and t.get('build'): found.append('%s 持有构建锁（%s）' % (t['id'], t['to']))
        return found

    def require(self, task, agent, roles, states, steer=False):
        # The lead (and the person, from the panel) may steer any task, not only the ones they posted.
        steering = steer and agent in (self.lead(), 'user')
        if agent not in [task[r] for r in roles] and not steering:
            raise ValueError('%s cannot act on %s (needs %s)' % (agent, task['id'], '/'.join(task[r] for r in roles)))
        if task['state'] not in states:
            raise ValueError('%s is %s; expected %s' % (task['id'], task['state'], '/'.join(states)))

    # -- commands ------------------------------------------------------------
    def post(self, agent, to, title, spec, level='L1', paths=(), build=False, force=False, no_dispatch=False, guide='',
             checks=()):
        if agent not in ACTORS: raise ValueError('Unknown actor ' + str(agent))
        if to not in AGENTS: raise ValueError('Post to claude, codex or deepseek')
        if level not in LEVELS: raise ValueError('Level must be L0~L3')
        if not (title or '').strip(): raise ValueError('A task needs a title')
        if not (spec or '').strip(): raise ValueError('A task needs a spec')
        if to == agent: raise ValueError('Post to another agent; do your own work directly')
        if to == 'deepseek' and level in GUIDED and agent != 'user' and not (guide or '').strip():
            raise ValueError('%s 任务交给 DeepSeek 需要实现指引（--guide / --guide-file）：大致方向、推荐写法、'
                             '必须保持的接口与行为、风险点' % level)
        paths = sorted({norm(p) for p in paths if p.strip()})
        with self.lock:
            clash = self.conflicts(paths, build)
            if clash and not force: raise ValueError('Lock conflict: ' + '; '.join(clash))
            task = {'id': self.next_id(), 'title': title.strip(), 'from': agent, 'to': to, 'level': level,
                    'state': 'posted', 'paths': paths, 'build': bool(build), 'base': self.git_head(),
                    'created': now(), 'rejects': 0, 'checks': [c.strip() for c in checks if c.strip()][:10]}
            (self.tasks / (task['id'] + '.md')).write_text(spec.strip() + '\n', encoding='utf-8')
            if (guide or '').strip():
                (self.tasks / (task['id'] + '.guide.md')).write_text(guide.strip() + '\n', encoding='utf-8')
                task['guided'] = True
            self.emit(task, agent, 'post', task['title'])
            self.save(task)
        self.take_snapshot(task['id'])
        out = {'id': task['id'], 'state': 'posted'}
        if to == 'deepseek':
            if no_dispatch:
                self.mark(task['id'], state='held', attempt=0, reason='--no-dispatch')
                out['dispatch'] = 'held'
            else:
                out['dispatch'] = self.dispatch(task['id'])
        return out

    def claim(self, agent, task_id):
        with self.lock:
            t = self.load(task_id)
            self.require(t, agent, ['to'], ('posted', 'rework', 'blocked'))
            t['state'] = 'claimed'
            self.emit(t, agent, 'claim'); self.save(t)
        return {'id': t['id'], 'state': t['state']}

    def deliver(self, agent, task_id, summary, checks='', evidence=(), limits=''):
        """Hand the work in. The program checks it first, in the delivering agent's own turn: scope (what changed
        against the allowed paths) and the task's acceptance commands. A failure is answered to the deliverer at
        once so it fixes it without anyone else being woken; simple DeepSeek work that passes is accepted here."""
        if not (summary or '').strip(): raise ValueError('Delivery needs a summary')
        t = self.load(task_id)
        self.require(t, agent, ['to'], ('posted', 'claimed', 'rework'))
        result = self.run_gate(t)
        if result.get('checks') and not result['checks']['ok'] and t.get('gateFails', 0) + 1 < GATE_RETRIES:
            with self.lock:
                t = self.load(task_id)
                t['gateFails'] = t.get('gateFails', 0) + 1
                self.save(t)
            c = result['checks']
            raise ValueError('验收命令未通过，未交付（第 %d/%d 次）：`%s` 退出码 %s\n%s\n修复后重新 deliver；完整日志：%s' % (
                t['gateFails'], GATE_RETRIES - 1, c['failed'], c['exit'], c['tail'].strip(), result['log']))
        with self.lock:
            t = self.load(task_id)
            self.require(t, agent, ['to'], ('posted', 'claimed', 'rework'))
            t['state'] = 'delivered'
            t['receipt'] = {'summary': summary[:1200], 'checks': (checks or '')[:600],
                            'evidence': [e[:200] for e in list(evidence)[:10]], 'limits': (limits or '')[:400], 'at': now()}
            t['gate'] = result
            t.pop('gateFails', None)
            self.emit(t, agent, 'deliver', summary)
            auto = self.auto_accept(t)
            if auto:
                t['state'] = 'accepted'
                t['review'] = {'verdict': 'accept', 'note': auto, 'kind': 'code', 'at': now(), 'by': 'agentlink'}
                self.emit(t, 'agentlink', 'accept', auto)
            elif self.ai_review_applies(t):
                attempt = t.get('rejects', 0)
                t['state'] = 'ai_review'
                t['aiReview'] = {'status': 'pending', 'attempt': attempt, 'session': self.ai_review_session(),
                                 'requestId': self.ai_review_request(task_id, attempt),
                                 'token': secrets.token_urlsafe(18), 'at': now()}
                self.emit(t, 'agentlink', 'ai_review', '程序验收通过，等待 DeepSeek AI 验收')
            self.save(t)
        out = {'id': t['id'], 'state': t['state']}
        if result.get('outside'): out['outside'] = result['outside']
        if auto: out['auto'] = auto
        if t['state'] == 'ai_review': out['aiReview'] = self.ai_review_send(t['id'])
        return out

    # -- program checks in place of an AI reviewer ------------------------------
    def snap_path(self, task_id):
        # Kept out of tasks/: its glob must see task records only.
        return self.base / 'snaps' / (task_id + '.json')

    def take_snapshot(self, task_id):
        """What was already dirty when work started; only taken once per task, so rework keeps the whole diff."""
        from . import gate
        path = self.snap_path(task_id)
        if path.exists(): return
        try: gate.save_snapshot(path, gate.snapshot(self.project.root))
        except (OSError, subprocess.SubprocessError): pass

    def expand_checks(self, commands):
        """Make wildcard acceptance commands shell-independent: `python -m py_compile agentlink/*.py` runs as
        the same program in cmd (which does not expand globs), sh (which does) and the panel."""
        import glob
        out = []
        for command in commands:
            parts, changed = [], False
            for part in command.split():
                pathish = ('/' in part or '\\' in part or part[:1] in ('*', '?'))
                if pathish and ('*' in part or '?' in part) and not part.startswith(('"', "'", '-')) \
                        and ';' not in part and '=' not in part:
                    hits = sorted(glob.glob(str(self.project.root / part)))
                    if hits:
                        parts.extend(self.project.rel(h) for h in hits); changed = True; continue
                parts.append(part)
            out.append(' '.join(parts) if changed else command)
        return out

    def run_gate(self, t):
        from . import gate
        root = self.project.root
        result = {'at': now()}
        snap = gate.load_snapshot(self.snap_path(t['id']))
        try:
            if snap is not None:
                files = gate.changed_since(root, snap)
                allowed = t.get('paths') or []
                inside = [f for f in files if not allowed or gate.inside(f, allowed)]
                # Files outside the allowed paths may be another agent's concurrent work: they block automatic
                # acceptance and are shown to the reviewer, but never reject on their own.
                result['outside'] = [f for f in files if allowed and not gate.inside(f, allowed)][:20]
                result['files'] = inside[:40]
                result['stat'] = gate.stat(root, inside[:40]) if inside else []
                # A large diff is never inlined: the reviewer gets the stat plus the command to fetch it.
                diff, size = gate.diff_summary(root, inside[:40]) if inside else (None, 0)
                if diff: result['diff'] = diff
                elif size: result['diffSize'] = size
        except (OSError, subprocess.SubprocessError) as error:
            result['scopeError'] = str(error)[:200]
        commands = t.get('checks') or []
        if commands:
            log = self.base / 'logs' / ('%s-%d.log' % (t['id'], t.get('rejects', 0)))
            result['checks'] = gate.run_checks(root, self.expand_checks(commands), log,
                                               int(self.project.option('checkTimeout')))
            result['log'] = str(log)
        return result

    def auto_accept(self, t):
        """Accept without waking anyone: DeepSeek work at an auto-accept level, inside its paths, checks passed
        (L0 needs none). Returns the note, or None when a person or the poster has to look."""
        if self.ai_review_applies(t): return None
        g = t.get('gate') or {}
        if t['to'] != 'deepseek' or t['level'] not in (self.project.option('autoAccept') or []): return None
        if g.get('outside') or g.get('scopeError') or 'files' not in g: return None
        c = g.get('checks')
        if c is None and t['level'] != 'L0': return None
        if c is not None and not c['ok']: return None
        return '程序自动验收：%s；改动 %d 个文件，均在允许路径内' % (
            '验收命令全部通过（%ds）' % c['seconds'] if c else 'L0 无需验收命令', len(g['files']))

    # -- DeepSeek AI review stage ---------------------------------------------
    def ai_review_applies(self, t):
        """When enabled, DeepSeek implementation work goes through the configured AI review session after the
        program gate and before the poster's own acceptance. L0 mechanical work keeps its old path."""
        return bool(self.project.option('aiReviewEnabled')) and t['to'] == 'deepseek' and t['level'] != 'L0'

    def ai_review_session(self):
        value = self.project.option('aiReviewSession')
        return dict(value) if isinstance(value, dict) and value.get('id') else None

    def ai_review_request(self, task_id, attempt):
        return str(uuid.uuid5(uuid.NAMESPACE_URL,
                              '%s/agentlink/%s/ai-review/%d' % (self.project.root.as_posix(), task_id, attempt)))

    def _ai_review_mark(self, task_id, status, error=None):
        """Record the stage status; a failure also becomes an event, so it is visible instead of silent."""
        with self.lock:
            t = self.load(task_id)
            r = t.get('aiReview') or {}
            r['status'] = status
            if error: r['error'] = str(error)[:300]
            else: r.pop('error', None)
            t['aiReview'] = r
            if status in ('failed', 'uncertain'):
                self.emit(t, 'agentlink', 'ai_review_failed', r.get('error') or status)
            self.save(t)
        return status

    def _ai_review_reset(self, task_id):
        with self.lock:
            t = self.load(task_id)
            r = t.get('aiReview') or {}
            r.update(status='pending'); r.pop('error', None)
            t['aiReview'] = r
            self.save(t)

    def ai_review_send(self, task_id):
        """Send the review prompt once per delivery attempt; a failure stays visible and is never auto-accepted."""
        t = self.load(task_id)
        if t['state'] != 'ai_review': return 'skipped'
        r = t.get('aiReview') or {}
        if r.get('attempt') != t.get('rejects', 0): return 'stale'
        if r.get('status') in ('sent', 'accepted', 'rejected'): return r['status']
        session = self.ai_review_session()
        sid = (session or {}).get('id') or (r.get('session') or {}).get('id')
        if not sid: return self._ai_review_mark(task_id, 'failed', '未配置 AI 验收会话')
        if os.environ.get('AGENTLINK_NO_DISPATCH'):
            return self._ai_review_mark(task_id, 'failed', 'AGENTLINK_NO_DISPATCH')
        try:
            from . import harness
            c = harness.Client()
            item = harness.find_session(c, sid)
            if item is None: raise ValueError('AI 验收会话不存在：%s' % sid)
            if sid in harness.archived_sessions(): raise ValueError('AI 验收会话已归档')
        except Exception as error:
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            return self._ai_review_mark(task_id, 'failed', reason)
        with self.lock:
            t = self.load(task_id)
            r = t.get('aiReview') or {}
            if r.get('status') in ('sent', 'accepted', 'rejected'): return r['status']
            r.update(status='sending', session=session or r.get('session'), at=r.get('at') or now())
            t['aiReview'] = r
            self.save(t)
        prompt = self.ai_review_prompt(t, r)
        try:
            answer = c.call('session.prompt', {'sessionId': sid, 'requestId': r['requestId'], 'mode': 'queue',
                                               'content': [{'type': 'text', 'text': prompt}],
                                               'clientTimeZone': self.project.option('timeZone')})
            status = 'sent' if answer.get('accepted') else 'uncertain'
        except Exception:
            status = 'uncertain'
        return self._ai_review_mark(task_id, status, None if status == 'sent' else '审查提示发送结果不确定')

    def ai_review_prompt(self, t, review):
        gate = t.get('gate') or {}
        checks = gate.get('checks')
        if checks and checks.get('ok'): verified = '验收命令全部通过（%ss）' % checks['seconds']
        elif checks: verified = '验收命令未通过：%s' % checks.get('failed')
        else: verified = '任务未设验收命令'
        token, attempt, cmd = review['token'], review['attempt'], cli()
        return ('AgentLink AI 验收：任务 %s（%s→%s %s，第 %d 次交付）。你只负责审查，不要修改任何文件、也不要运行会'
                '改动文件的命令。\n允许路径：%s\n程序验收：%s\n改动文件：%s\n\n--- 规格 ---\n%s\n\n'
                '审查通过：`%s reviewer --as deepseek --task %s --attempt %d --verdict accept --token %s`\n'
                '审查驳回：同上命令改 --verdict reject --note "<具体问题>"（代码问题 --kind code，设计问题 design）。\n'
                '令牌只对本次交付有效，不要写进任何文件。审查完只回一行。') % (
            t['id'], t['from'], t['to'], t['level'], attempt + 1, ', '.join(t['paths']) or '（未限定）',
            verified, '、'.join(gate.get('files') or []) or '（无）', self.spec(t['id'])[:1500],
            cmd, t['id'], attempt, token)

    def ai_review(self, agent, task_id, attempt, token, verdict, note='', kind='code', no_dispatch=False):
        """The reviewer's explicit verdict: identity, token, attempt and current state are checked, so no
        natural-language reply is ever parsed and no other DeepSeek session can close a task."""
        if agent != 'deepseek': raise ValueError('AI 验收只接受 DeepSeek 审查会话')
        if verdict not in ('accept', 'reject'): raise ValueError('verdict 只能是 accept 或 reject')
        if kind not in KINDS: raise ValueError('kind must be code or design')
        with self.lock:
            t = self.load(task_id)
            r = t.get('aiReview') or {}

            def refuse(message):
                r['error'] = message[:300]; t['aiReview'] = r
                self.emit(t, 'agentlink', 'ai_review_error', message)
                self.save(t)
                raise ValueError(message)

            if (t['state'] == 'delivered' and r.get('status') == 'accepted' and r.get('attempt') == attempt
                    and r.get('verdict') == 'accept'):
                return {'id': t['id'], 'state': t['state'], 'duplicate': True}   # a repeated call changes nothing
            if t['state'] != 'ai_review': refuse('%s 不在 AI 审查阶段（当前 %s）' % (task_id, t['state']))
            if attempt != t.get('rejects', 0) or attempt != r.get('attempt'):
                refuse('交付序号不匹配：任务当前第 %d 次交付' % (t.get('rejects', 0) + 1))
            stored = str(r.get('token') or '')
            if not isinstance(token, str) or not stored or not secrets.compare_digest(stored, token):
                refuse('审查令牌无效')
            r.pop('token', None)
            r.pop('error', None)
            if verdict == 'accept':
                t['state'] = 'delivered'
                r.update(status='accepted', verdict='accept', at=now())
                t['aiReview'] = r
                self.emit(t, 'ai-reviewer', 'ai_accept', 'AI 验收通过，交发起方最终验收')
                self.save(t)
                return {'id': t['id'], 'state': t['state']}
            if not (note or '').strip(): raise ValueError('AI 验收驳回需要写清具体问题')
            t['rejects'] = t.get('rejects', 0) + 1
            r.update(status='rejected', verdict='reject', note=note[:600], at=now())
            t['aiReview'] = r
            t['review'] = {'verdict': 'reject', 'note': note[:600], 'kind': kind, 'at': now(), 'by': 'ai-reviewer'}
            t['state'] = 'rework'
            self.emit(t, 'ai-reviewer', 'ai_reject', note)
            escalated = self.escalate(t, kind)
            self.save(t)
        out = {'id': t['id'], 'state': t['state'], 'to': t['to']}
        if escalated: out['escalated'] = escalated
        elif not no_dispatch: out['dispatch'] = self.dispatch(task_id)
        return out

    def review(self, agent, task_id, verdict, note='', no_dispatch=False, kind='code'):
        if kind not in KINDS: raise ValueError('kind must be code or design')
        with self.lock:
            t = self.load(task_id)
            self.require(t, agent, ['from'], OPEN + ('delivered',) if verdict == 'cancel' else ('delivered',),
                         steer=verdict == 'cancel')
            if verdict == 'accept': t['state'] = 'accepted'
            elif verdict == 'cancel': t['state'] = 'cancelled'
            elif verdict == 'reject':
                if not (note or '').strip(): raise ValueError('Rejecting needs a note with the concrete problem')
                t['rejects'] = t.get('rejects', 0) + 1
                t['state'] = 'rework'
            else: raise ValueError('Unknown verdict ' + str(verdict))
            t['review'] = {'verdict': verdict, 'note': (note or '')[:600], 'kind': kind, 'at': now()}
            self.emit(t, agent, verdict, note)
            escalated = self.escalate(t, kind) if verdict == 'reject' else None
            self.save(t)
        out = {'id': t['id'], 'state': t['state'], 'to': t['to']}
        if escalated:
            out['escalated'] = escalated
            return out
        if t['state'] == 'rework' and t['rejects'] >= REWORK_LIMIT:
            out['advice'] = '已返工%d次：按约定收回或转交，勿继续返工' % t['rejects']
        if t['state'] == 'rework' and t['to'] == 'deepseek' and not no_dispatch:
            out['dispatch'] = self.dispatch(t['id'])
        return out

    def escalate(self, t, kind):
        """DeepSeek failed twice: hand the task to Claude (code) or Codex (design) for rectification.

        Runs under the bus lock. Falls back to the other primary when the preferred one is not bound."""
        if t['to'] != 'deepseek' or t['rejects'] < REWORK_LIMIT or not self.project.option('autoEscalate'): return None
        from .project import ESCALATION
        # One automatic hand-over per task: a task that came back to DeepSeek and failed again goes to the lead.
        ruling = bool(t.get('escalation'))
        target = self.lead() if ruling else ESCALATION[kind]
        # Claude only takes work while its session runs (its plugin polls every 30 s); otherwise the lead does.
        if not self.project.binding(target) or (target == 'claude' and not self.alive('claude')):
            target = next((a for a in (self.lead(), 'codex', 'claude')
                           if self.project.binding(a) and (a != 'claude' or self.alive('claude'))), target)
        t['escalations'] = t.get('escalations', 0) + 1
        t['escalation'] = {'from': 'deepseek', 'to': target, 'kind': kind, 'rejects': t['rejects'], 'at': now(),
                           'ruling': ruling}
        t.pop('dispatch', None)
        t['to'], t['rejects'] = target, 0
        # A poster taking over its own task works on it directly; anyone else claims it like a new task.
        t['state'] = 'claimed' if target == t['from'] else 'posted'
        self.emit(t, 'agentlink', 'escalate', ('已自动转交过一次，DeepSeek 再次未解决，交总控 %s 裁决' % target) if ruling else
                  'DeepSeek 返工两次未解决（%s问题），转 %s 整改' % ('代码' if kind == 'code' else '设计', target))
        return target

    def block(self, agent, task_id, note):
        if not (note or '').strip(): raise ValueError('Blocking needs a note')
        with self.lock:
            t = self.load(task_id)
            self.require(t, agent, ['to'], ('posted', 'claimed', 'rework'))
            t['state'] = 'blocked'
            self.emit(t, agent, 'block', note); self.save(t)
        return {'id': t['id'], 'state': t['state']}

    def reassign(self, agent, task_id, to, note='', no_dispatch=False):
        """Take a task back (to yourself) or hand it to another agent, keeping history and locks."""
        if to not in ACTORS: raise ValueError('Unknown agent ' + str(to))
        with self.lock:
            t = self.load(task_id)
            self.require(t, agent, ['from'], OPEN + ('delivered',), steer=True)
            if to == agent and agent != t['from']:          # the lead takes another's task over itself
                t['to'], t['state'], t['rejects'] = agent, 'claimed', 0
                t.pop('dispatch', None)
                self.emit(t, agent, 'reassign', note or '总控接手')
            elif to == t['from']:
                t['state'] = 'cancelled'                 # taken back: the poster now does it directly
                self.emit(t, agent, 'takeback', note)
            elif to == 'user':
                raise ValueError('Tasks go to claude, codex or deepseek')
            else:
                t['to'], t['state'], t['rejects'] = to, 'posted', 0
                t.pop('dispatch', None)
                self.emit(t, agent, 'reassign', note)
            self.save(t)
        out = {'id': t['id'], 'state': t['state'], 'to': t['to']}
        if t['state'] == 'posted' and t['to'] == 'deepseek' and not no_dispatch: out['dispatch'] = self.dispatch(t['id'])
        return out

    def show(self, task_id, full=False):
        t = self.load(task_id)
        head = '%s [%s→%s %s %s] %s\n允许路径：%s%s\n基线：%s' % (
            t['id'], t['from'], t['to'], t['level'], t['state'], t['title'], ', '.join(t['paths']) or '（未限定）',
            '；持有构建锁' if t.get('build') else '', t.get('base'))
        review = t.get('review') or {}
        if review.get('verdict') == 'reject' and t['state'] in ('rework', 'posted', 'claimed'):
            head += '\n返工意见：' + review['note']
        if t.get('escalation'):
            e = t['escalation']
            head += '\n整改：DeepSeek 返工 %d 次未解决（%s问题），已转交 %s；先读返工意见和历史回执' % (
                e['rejects'], '代码' if e['kind'] == 'code' else '设计', e['to'])
        ai = t.get('aiReview') or {}
        if ai:
            session = ai.get('session') or {}
            head += '\nAI 验收：' + AI_REVIEW_LABEL.get(ai.get('status'), str(ai.get('status') or '-'))
            if session.get('id'): head += '（会话 %s）' % (session.get('title') or session['id'])
            if ai.get('note'): head += '\nAI 审查意见：' + str(ai['note'])[:300]
            if ai.get('error'): head += '\nAI 验收错误：' + str(ai['error'])[:200]
            if t['state'] == 'ai_review' and ai.get('status') in ('failed', 'uncertain'):
                head += '\n重试 AI 验收：dispatch ' + t['id']
        if full and t.get('receipt'): head += '\n回执：' + json.dumps(t['receipt'], ensure_ascii=False)
        text = head + '\n---\n' + self.spec(t['id'])
        guide = self.guide(t['id'])
        if guide: text += '--- 实现指引\n' + guide
        return text

    def listing(self, agent=None, include_all=False, full=False):
        rows = [t for t in self.all() if include_all or t['state'] not in TERMINAL]
        # The lead sees every open task: it keeps the overall picture.
        if agent and not include_all and agent != self.lead(): rows = [t for t in rows if agent in (t['from'], t['to'])]
        return '\n'.join(line(t, short=not full) for t in rows) or '（无）'

    def locks(self):
        rows = ['%s %s：%s%s' % (t['id'], t['to'], ', '.join(t['paths']) or '-', ' +build' if t.get('build') else '')
                for t in self.all() if t['state'] in OPEN]
        return '\n'.join(rows) or '（无锁）'

    # -- hook output: nothing unless something changed -----------------------
    def actionable(self, agent):
        out, lead = [], self.lead()
        for t in self.all():
            if t['to'] == agent and t['state'] in ('posted', 'rework'):
                out.append('待办 ' + line(t) + ('（返工：%s）' % t['review']['note'][:80] if t['state'] == 'rework' else ''))
            elif t['from'] == agent and t['state'] == 'delivered':
                out.append('待验收 ' + line(t) + '：' + t['receipt']['summary'][:120].replace('\n', ' '))
            elif agent == lead and t['state'] == 'delivered' and self.needs_audit(t):
                out.append('待审计 ' + line(t) + '：发起方验收，总控审计方向与集成影响')
            elif t['state'] == 'blocked' and agent in (t['from'], lead):
                out.append('受阻 ' + line(t) + '：' + t['history'][-1]['note'][:120])
            elif t['state'] == 'ai_review' and agent in (t['from'], lead) \
                    and (t.get('aiReview') or {}).get('status') in ('failed', 'uncertain'):
                out.append('AI 验收未送达 ' + line(t) + '：' + str((t.get('aiReview') or {}).get('error') or '-')[:100])
            elif (t.get('dispatch') or {}).get('stalled') and t['state'] not in TERMINAL and agent in (t['from'], lead):
                out.append('疑似停滞 ' + line(t) + '：DS已空闲但未交付，核实后 reassign 或收回')
        return out

    def context_items(self, agent):
        """What the agent should read: what it must act on, plus the lead's audit duties."""
        items = self.wake_items(agent, quiet_ok=True)
        if agent == self.lead():
            for t in self.all():
                stalled = (t.get('dispatch') or {}).get('stalled') and t['state'] not in TERMINAL
                if t['from'] == agent or t['to'] == agent: continue
                if t['state'] == 'delivered' and self.needs_audit(t): why = 'audit'
                elif t['state'] == 'blocked': why = 'blocked'
                elif t['state'] == 'ai_review' and (t.get('aiReview') or {}).get('status') in ('failed', 'uncertain'):
                    why = 'ai_review'
                elif stalled: why = 'stalled'
                else: continue
                items['%s:%s:%d:%s' % (t['id'], t['state'], t.get('rejects', 0), why)] = t
        return items

    def headline(self, agent, t, why):
        """One line per open item, for a session that has just started: enough to decide what to do, cheap to
        read. The spec, receipt and diff are fetched with `show` only for the item it actually works on."""
        head = '%s %s（%s→%s %s）' % (t['id'], t['title'][:40], t['from'], t['to'], t['level'])
        if why == 'todo':
            state = '返工中' if (t.get('review') or {}).get('verdict') == 'reject' else '待领取'
            return '【待办】%s：%s；做法：show %s → claim %s' % (head, state, t['id'], t['id'])
        if why in ('delivered', 'audit'):
            summary = ((t.get('receipt') or {}).get('summary') or '')[:80].replace('\n', ' ')
            return '【%s】%s：%s' % ('待验收' if why == 'delivered' else '待审计（发起方验收）', head, summary)
        if why == 'blocked':
            return '【受阻】%s：%s' % (head, t['history'][-1]['note'][:120])
        if why in ('ai_review', 'ai_review_failed'):
            ai = t.get('aiReview') or {}
            return '【AI 验收未送达】%s：%s' % (head, str(ai.get('error') or ai.get('status') or '-')[:100])
        return '【疑似停滞】%s：DeepSeek 已空闲但未交付' % head

    def detail(self, agent, t, why):
        """Everything the agent needs to act, inline: a tool call to `show` would cost it another full round."""
        head = '%s %s（%s→%s %s）' % (t['id'], t['title'][:60], t['from'], t['to'], t['level'])
        clip = lambda text, n: text if len(text) <= n else text[:n] + '…（完整：show %s）' % t['id']
        if why == 'todo':
            lines = ['【待办】' + head, '允许路径：' + (', '.join(t['paths']) or '（未限定）') + ('；构建锁' if t.get('build') else '')]
            review = t.get('review') or {}
            if review.get('verdict') == 'reject': lines.append('返工意见：' + review['note'][:300])
            if t.get('escalation'): lines.append('整改：DeepSeek 两次未解决，交你处理')
            lines.append('规格：' + clip(self.spec(t['id']).strip(), 900))
            guide = self.guide(t['id']).strip()
            if guide: lines.append('指引：' + clip(guide, 500))
            lines.append('做法：claim %s → 实现 → deliver %s --summary …' % (t['id'], t['id']))
        elif why in ('delivered', 'audit'):
            r = t.get('receipt') or {}
            lines = ['【%s】' % ('待验收' if why == 'delivered' else '待审计（发起方验收）') + head,
                     '回执：' + (r.get('summary') or '')[:400].replace('\n', ' ')]
            if r.get('checks'): lines.append('检查：' + r['checks'][:200].replace('\n', ' '))
            if r.get('limits'): lines.append('边界：' + r['limits'][:200].replace('\n', ' '))
            lines.extend(self.gate_lines(t))
            if why == 'delivered':
                lines.append('做法：看 diff 后 accept %s，或 reject %s --note "<具体问题>"%s' % (
                    t['id'], t['id'], ' --kind code|design' if t['to'] == 'deepseek' else ''))
        elif why == 'blocked':
            lines = ['【受阻】' + head + '：' + t['history'][-1]['note'][:200], '做法：补充信息后 reassign，或 cancel']
        elif why in ('ai_review', 'ai_review_failed'):
            ai = t.get('aiReview') or {}
            session = ai.get('session') or {}
            lines = ['【AI 验收未送达】' + head,
                     '状态：' + AI_REVIEW_LABEL.get(ai.get('status'), str(ai.get('status') or '-')) +
                     ('；会话 ' + (session.get('title') or session['id']) if session.get('id') else '；未配置会话'),
                     '错误：' + str(ai.get('error') or '-')[:200],
                     '做法：在设置页确认 AI 验收会话后 `%s dispatch %s` 重试；或直接人工验收' % (cli(), t['id'])]
        else:
            lines = ['【疑似停滞】' + head + '：DeepSeek 已空闲但未交付', '做法：确认后 dispatch 重投、reassign 或收回']
        return '\n'.join(lines)

    @staticmethod
    def gate_lines(t):
        """What the program already verified, so the reviewer reads it instead of re-running git and tests."""
        g = t.get('gate') or {}
        out = []
        c = g.get('checks')
        if c:
            out.append('程序验收：' + ('全部通过（%ds）' % c['seconds'] if c['ok'] else
                                     '未通过：`%s` 退出码 %s，日志 %s' % (c['failed'], c['exit'], g.get('log'))))
        elif not t.get('checks'):
            out.append('程序验收：任务未设验收命令')
        if g.get('outside'): out.append('注意：允许路径外有改动（可能是他人并行工作，需确认）：' + ', '.join(g['outside'][:8]))
        if g.get('stat'): out.append('改动：' + '；'.join(g['stat'][:12]))
        elif 'files' in g: out.append('改动：允许路径内没有文件变化')
        if g.get('diff'): out.append('diff：\n' + g['diff'])
        elif g.get('stat'):
            # Not inlined to save the reviewer's context: one command fetches it, with the exact paths already
            # known from the gate, so the reviewer does not need a round-trip to discover them.
            files = g.get('files') or []
            shown = ' '.join('"%s"' % f if ' ' in f else f for f in files[:8])
            more = ' …' if len(files) > 8 else ''
            out.append('diff 未内联（约 %d 字符）：按需 `git diff HEAD -- %s%s`' % (g.get('diffSize') or 0, shown, more))
        return out

    def brief(self, agent, event):
        """Compact context for an agent hook: only items it has not been told about yet ('' when quiet).

        A session that is just starting gets one headline per open item (cheap to scan, and it decides what to
        work on before paying for the spec/receipt/diff of a single task with `show`). A turn already under way
        gets the full inline detail, because the agent is about to act and a `show` round-trip would cost more.
        Telling the agent also counts as waking it: it is in a turn already."""
        items = self.context_items(agent)
        seat = self.read_seat(agent)
        start = event == 'SessionStart'
        told = set() if start else set(seat.get('told', []))
        fresh = [k for k in sorted(items) if k not in told]
        budget = 1200 if start else 3600          # headlines are short: a session start stays a few hundred tokens
        text = ''
        if fresh:
            body = []
            for k in fresh:
                why = k.rsplit(':', 1)[1]
                t = items[k]
                # An L0 receipt is only read, never studied: the poster gets the one-liner and the accept hint,
                # not the receipt/gate dump (the same rule that keeps L0 receipts from waking anyone).
                cheap = start or (why == 'delivered' and t['level'] == 'L0' and t['to'] == 'deepseek')
                part = (self.headline if cheap else self.detail)(agent, t, why)
                if cheap and why == 'delivered' and t['level'] == 'L0' and t['to'] == 'deepseek':
                    part += '；accept %s 即可（详情 show %s --full）' % (t['id'], t['id'])
                if budget - len(part) < 0:
                    body.append('另有 %d 项：%s --as %s list' % (len(fresh) - len(body), cli(), agent)); break
                body.append(part); budget -= len(part)
            hint = '（开工前 show <ID> 读完整规格）' if start else ''
            text = 'AgentLink（项目 %s，命令 %s --as %s <cmd>）%s：\n%s' % (
                self.project.name, cli(), agent, hint, ('\n' if start else '\n\n').join(body))
        keys = sorted(items)
        wake_keys = sorted(self.wake_items(agent))
        if seat.get('told') != keys or seat.get('woken') != wake_keys:
            with self.lock:
                seat = self.read_seat(agent)
                seat['told'], seat['woken'] = keys, wake_keys
                write_json(self.seat_path(agent), seat)
        return text

    def bound(self, agent):
        return (self.project.binding(agent) or {}).get('id')

    def seat_matches(self, agent, session):
        """A bound agent hears only its bound conversation; an unbound one hears any session here."""
        bound = self.bound(agent)
        return not bound or (session is not None and session == bound)

    # -- waking idle agents --------------------------------------------------
    # DeepSeek is woken by dispatch(). Codex is woken with `codex queue` into its bound thread.
    # Claude wakes itself: its plugin polls `pending` and submits a prompt. Every wake is deduplicated
    # by event sequence, debounced and capped per hour, and can be switched off from the panel.
    def seat_path(self, agent):
        return self.seats / (agent + '.json')

    def read_seat(self, agent):
        return read_json(self.seat_path(agent), {}) or {}

    def register_seat(self, agent, session):
        """Remember an unbound Codex thread from its hook so it can be woken; a binding always wins."""
        if agent != 'codex' or self.bound('codex'): return
        if not isinstance(session, str) or not UUID.match(session): return
        with self.lock:
            seat = self.read_seat(agent)
            if seat.get('thread') != session:
                seat.update(thread=session, registered=now())
                seat.pop('failed', None)
                write_json(self.seat_path(agent), seat)

    def wake_enabled(self):
        if os.environ.get('AGENTLINK_NO_WAKE'): return False
        return read_json(self.base / 'wake.json', {}).get('enabled', True)

    def set_wake(self, enabled):
        write_json(self.base / 'wake.json', {'enabled': bool(enabled), 't': now()})

    # A wake costs Claude or Codex a whole turn over its whole conversation, so only work that needs that agent
    # wakes it, each piece once, and a burst of changes settles into a single turn.
    def wake_items(self, agent, quiet_ok=False):
        """What only this agent can move forward, keyed by task, state and attempt. Claims, accepts and other
        agents' progress never wake anyone; the lead reads those in its prompt context instead."""
        out = {}
        for t in self.all():
            stalled = (t.get('dispatch') or {}).get('stalled') and t['state'] not in TERMINAL
            if t['to'] == agent and t['state'] in ('posted', 'rework'): why = 'todo'
            elif t['from'] == agent and t['state'] in ('delivered', 'blocked'):
                # A DeepSeek L0 receipt is only read: it waits for the poster's next turn instead of starting one.
                if t['state'] == 'delivered' and t['to'] == 'deepseek' and t['level'] == 'L0' and not quiet_ok: continue
                why = t['state']
            elif t['from'] == agent and t['state'] == 'ai_review' \
                    and (t.get('aiReview') or {}).get('status') in ('failed', 'uncertain'):
                why = 'ai_review_failed'
            elif t['from'] == agent and stalled: why = 'stalled'
            else: continue
            out['%s:%s:%d:%s' % (t['id'], t['state'], t.get('rejects', 0), why)] = t
        return out

    @staticmethod
    def changed_at(t):
        try: return datetime.datetime.fromisoformat(t.get('updated') or t['created']).timestamp()
        except (KeyError, ValueError): return 0

    def pending_for(self, agent):
        """(ids needing a wake, whether the burst has settled). A key the agent was woken for or already
        saw in a prompt it was working on never wakes it again."""
        items = self.wake_items(agent)
        seen = set(self.read_seat(agent).get('woken', []))
        fresh = [k for k in items if k not in seen]
        newest = max((self.changed_at(items[k]) for k in fresh), default=0)
        settled = bool(fresh) and time.time() - newest >= WAKE_QUIET
        return sorted({items[k]['id'] for k in fresh}), settled

    def wake_allowed(self, seat):
        if not self.wake_enabled(): return False
        t = time.time()
        recent = [w for w in seat.get('wakes', []) if t - w < 3600]
        return len(recent) < WAKE_PER_HOUR and (not recent or t - recent[-1] >= WAKE_DEBOUNCE)

    def record_wake(self, agent, delivered=True, **extra):
        keys = sorted(self.wake_items(agent))
        with self.lock:
            seat = self.read_seat(agent)
            t = time.time()
            seat['wakes'] = [w for w in seat.get('wakes', []) if t - w < 3600] + [t]
            if delivered: seat['woken'] = keys
            seat.update(wokeAt=now(), **extra)
            write_json(self.seat_path(agent), seat)

    def pending(self, agent, session=None, claim=False):
        """For the Claude plugin's poll: whether to start a turn now, and for which tasks."""
        if not self.seat_matches(agent, session): return {'ids': [], 'wake': False}
        self.mark_alive(agent)
        ids, settled = self.pending_for(agent)
        wake = settled and self.wake_allowed(self.read_seat(agent))
        if wake and claim: self.record_wake(agent)
        return {'ids': ids, 'wake': wake}

    def mark_alive(self, agent):
        """The agent's session is running now (written at most once a minute)."""
        seat = self.read_seat(agent)
        if time.time() - seat.get('seen', 0) < 60: return
        with self.lock:
            seat = self.read_seat(agent)
            seat['seen'] = time.time()
            write_json(self.seat_path(agent), seat)

    def alive(self, agent):
        """Online: its plugin polled the bus within ALIVE_SECONDS."""
        return time.time() - self.read_seat(agent).get('seen', 0) < ALIVE_SECONDS

    def unclaimed_fallback(self):
        """An escalation Claude has not picked up in time goes to the lead, so it never sits waiting."""
        lead = self.lead()
        for t in self.all():
            e = t.get('escalation') or {}
            if t['state'] != 'posted' or t['to'] != 'claude' or not e or lead == 'claude': continue
            age = time.time() - datetime.datetime.fromisoformat(e['at']).timestamp()
            if age < CLAIM_SECONDS and self.alive('claude'): continue
            with self.lock:
                t = self.load(t['id'])
                if t['state'] != 'posted' or t['to'] != 'claude': continue
                t['to'] = lead
                t['escalation'].update(to=lead, fallback=now())
                self.emit(t, 'agentlink', 'escalate', 'Claude 未在线或 %d 分钟内未领取，转总控 %s' % (CLAIM_SECONDS // 60, lead))
                self.save(t)

    def wake_codex(self):
        ids, settled = self.pending_for('codex')
        seat = self.read_seat('codex')
        thread = self.bound('codex') or seat.get('thread')
        if not settled or not thread or not self.wake_allowed(seat): return None
        command = codex_command()
        if not command: return None
        # Only task IDs and fixed text reach the message; the details arrive through the hook's context.
        message = WAKE_TEXT % '、'.join(ids[:6])
        try:
            ran = subprocess.run(command + ['queue', '--thread', thread, '--message', message],
                                 cwd=str(self.project.root), capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
            ok = ran.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        # A failed wake still counts toward the cap, so a stale thread id cannot cause a retry storm.
        self.record_wake('codex', delivered=ok, failed=None if ok else now())
        return 'queued' if ok else 'failed'

    def wake_all(self):
        try: return self.wake_codex()
        except Exception: return None

    def wake_status(self):
        codex, claude = self.read_seat('codex'), self.read_seat('claude')
        return {'enabled': self.wake_enabled(), 'codexThread': self.bound('codex') or codex.get('thread'),
                'codexLastWake': codex.get('wokeAt'), 'codexFailed': codex.get('failed'),
                'claudeLastWake': claude.get('wokeAt'), 'watcher': read_json(self.base / 'harness-watcher.json'),
                'claudeSeen': seen_at(claude), 'codexSeen': seen_at(codex), 'claudeOnline': self.alive('claude')}

    # -- DeepSeek dispatch through the Harness bridge -------------------------
    def mark(self, task_id, **dispatch_state):
        with self.lock:
            t = self.load(task_id)
            t['dispatch'] = dispatch_state
            self.save(t)
            return t

    def dispatch(self, task_id):
        """One short pointer prompt per task attempt; uncertain sends are never retried automatically."""
        t = self.load(task_id)
        attempt = t.get('rejects', 0)
        d = t.get('dispatch') or {}
        if d.get('attempt') == attempt and d.get('state') in ('sending', 'accepted', 'uncertain'):
            return d['state']
        if os.environ.get('AGENTLINK_NO_DISPATCH'):
            self.mark(task_id, state='held', attempt=attempt, reason='AGENTLINK_NO_DISPATCH')
            return 'held'
        sid = self.bound('deepseek')
        # Network preflight runs outside the bus lock: Harness calls can take seconds.
        try:
            if not sid: raise ValueError('未绑定 DeepSeek 会话')
            from . import harness
            c = harness.Client()
            item = harness.find_session(c, sid)
            if item is None: raise ValueError('bound DeepSeek session missing')
            if sid in harness.archived_sessions(): raise ValueError('bound DeepSeek session archived')
            if item.get('running'): raise ValueError('busy')
        except Exception as error:
            self.mark(task_id, state='pending', attempt=attempt,
                      reason=str(error) if isinstance(error, ValueError) else type(error).__name__)
            return 'pending'
        request = str(uuid.uuid5(uuid.NAMESPACE_URL, '%s/agentlink/%s/%d' % (self.project.root.as_posix(), task_id, attempt)))
        with self.lock:
            t = self.load(task_id)
            current = t.get('dispatch') or {}
            if current.get('attempt') == attempt and current.get('state') in ('sending', 'accepted', 'uncertain'):
                return current['state']                  # another process won the race
            t['dispatch'] = {'state': 'sending', 'attempt': attempt, 'requestId': request, 'at': now()}
            self.save(t)
        # Keep the pointer short: the full protocol (commands, guide, receipt format) arrives through `show`
        # and AGENTLINK.md, so the CLI path is spelled out once instead of in every command.
        prompt = ('AgentLink 任务 %s（%s 派发，%s）：%s show %s --as deepseek 读规格（含实现指引）并 claim；'
                  '只改允许路径，可拆给子智能体后汇总成一份回执；完成后 deliver --as deepseek --summary/--checks/--evidence，'
                  '受阻 block --note；回执写进命令，会话里只回一行。') % (task_id, t['from'], t['level'], cli(), task_id)
        try:
            r = c.call('session.prompt', {'sessionId': sid, 'requestId': request, 'mode': 'queue',
                                          'content': [{'type': 'text', 'text': prompt}],
                                          'clientTimeZone': self.project.option('timeZone')})
            state = 'accepted' if r.get('accepted') else 'uncertain'
        except Exception:
            state = 'uncertain'
        with self.lock:
            t = self.load(task_id)
            t['dispatch']['state'] = state
            self.emit(t, 'agentlink', 'dispatch', state); self.save(t)
        return state

    def release(self, task_id):
        """Explicitly release a held task, retry an uncertain dispatch, or retry a failed AI review send."""
        t = self.load(task_id)
        if t['state'] == 'ai_review':
            if (t.get('aiReview') or {}).get('status') in ('failed', 'uncertain', 'sending'):
                self._ai_review_reset(task_id)
            return {'id': task_id, 'aiReview': self.ai_review_send(task_id)}
        if (t.get('dispatch') or {}).get('state') in ('held', 'uncertain'):
            self.mark(task_id, state='pending', attempt=t.get('rejects', 0), reason='released')
        return {'id': task_id, 'dispatch': self.dispatch(task_id)}

    def pump(self):
        """Retry pending DeepSeek dispatches, hand unclaimed escalations on and flag stalls. Cheap."""
        try: self.unclaimed_fallback()
        except Exception: pass
        for t in self.all():
            if t['to'] != 'deepseek' or t['state'] not in ('posted', 'claimed', 'rework'): continue
            try: self.pump_one(t)
            except Exception: continue           # one unreachable task must not starve the rest

    def pump_one(self, t):
        d = t.get('dispatch') or {}
        if d.get('state') == 'held': return
        stale = d.get('attempt') != t.get('rejects', 0) and t['state'] in ('posted', 'rework')
        if d.get('state') == 'pending' or (not d and t['state'] in ('posted', 'rework')) or stale:
            self.dispatch(t['id'])
        elif d.get('state') == 'accepted' and not d.get('stalled'):
            age = time.time() - datetime.datetime.fromisoformat(d['at']).timestamp()
            if age > STALL_SECONDS and not self.deepseek_running():
                with self.lock:
                    t = self.load(t['id'])
                    t['dispatch']['stalled'] = True
                    self.emit(t, 'agentlink', 'stalled', 'DeepSeek 空闲但未交付'); self.save(t)

    def deepseek_running(self):
        from . import harness
        item = harness.find_session(harness.Client(), self.bound('deepseek'))
        return bool(item and item.get('running'))

    def watcher_alive(self):
        try: return time.time() - (self.base / 'harness-watcher.json').stat().st_mtime < 30
        except FileNotFoundError: return False

    def heartbeat(self, **extra):
        write_json(self.base / 'harness-watcher.json', dict({'pid': os.getpid(), 't': now()}, **extra))

    def wait(self, task_id, timeout=3600):
        """Block without model calls until the task leaves the working states (zero tokens while waiting)."""
        deadline = time.monotonic() + timeout
        while True:
            t = self.load(task_id)
            if t['state'] not in ('posted', 'claimed', 'rework'): return {'id': t['id'], 'state': t['state']}
            if time.monotonic() > deadline: return {'id': t['id'], 'state': 'waiting'}
            time.sleep(2)


def codex_command():
    if os.environ.get('AGENTLINK_CODEX_CMD'): return json.loads(os.environ['AGENTLINK_CODEX_CMD'])
    found = user_home() / '.codex' / 'plugins' / '.plugin-appserver' / 'codex.exe'
    if found.exists(): return [str(found)]
    which = shutil.which('codex')
    return [which] if which else None
