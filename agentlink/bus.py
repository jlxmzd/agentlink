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
OPEN = ('posted', 'claimed', 'rework', 'blocked', 'ai_review', 'paused', 'recalling')
TERMINAL = ('accepted', 'cancelled')
REWORK_LIMIT = 2
GUIDED = ('L2', 'L3')                                    # DeepSeek gets these only with an implementation guide
KINDS = ('code', 'design')                               # what a reject is about; decides who takes over
LEAD_ACTIONS = ('block', 'escalate', 'stalled')          # events the lead must act on, whoever the task belongs to
STALL_SECONDS = 120
ALIVE_SECONDS = 300      # an agent whose plugin polled within this long counts as online
CLAIM_SECONDS = 600      # an escalation Claude has not claimed by then goes to the lead
AI_REVIEW_LABEL = {'pending': '待投递', 'sending': '投递中', 'sent': '已入队（未确认启动）', 'started': '审查中',
                   'accepted': '审查通过', 'rejected': '审查驳回', 'failed': '未送达', 'uncertain': '投递不确定',
                   'stalled': '审查停滞'}
# The review prompt is a queue write: `sent` only means the bridge accepted it, never that the reviewer began.
# A reviewer confirms with `reviewer --verdict started` before its verdict counts, and every resend stays inside
# a bounded backoff with the same requestId, so the bridge cannot deliver the same prompt twice.
AI_START_TIMEOUT = 180   # 秒：入队后仍未确认 started，才允许同 ID 有界重投
AI_STALL_SECONDS = 1800  # 秒：已 started 却迟迟无结论，标记停滞交人工
AI_RETRY_BASE = 60       # 秒：首次重投等待，之后按 2 的幂增长
AI_RETRY_MAX = 900       # 秒：退避上限
AI_RETRY_LIMIT = 5       # 连续无进展的重投次数上限，超过即 stalled
AI_LEASE_SECONDS = 120   # 秒：发送租约，避免并发进程重复投递
GATE_RETRIES = 3         # failing acceptance commands bounce back to the deliverer up to this many times minus one
WAKE_QUIET = 45          # a burst of changes must settle this long before an agent is woken: one turn, not one per event
WAKE_DEBOUNCE = 120      # at least this long between two wakes of the same agent
WAKE_PER_HOUR = 12
WAKE_LEASE_SECONDS = 120   # a reserved wake that never finishes (crash) may be reclaimed after this
RULES_ROUNDS_DEFAULT = 20  # re-show the short rules after this many completed rounds (0 disables)
FINAL_REVIEW_SECONDS = 1800  # one reminder per 30 minutes, bounded; never auto-accept waiting work
FINAL_REVIEW_REMINDERS = 3
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


def route(t):
    """Who is really doing this task, and where did it come from.

    After a hand-over the poster is still the poster, so `from → to` alone reads "Codex → Codex" for a task
    DeepSeek had been doing. The executor chain is what people mean, so it is shown instead."""
    history = t.get('handoverFrom') or []
    if t.get('escalation'):
        origin = t.get('origin') or (history[0]['from'] if history else 'deepseek')
        return '%s→%s' % (origin, t['to'])
    return '%s→%s' % (t['from'], t['to'])


def line(t, short=False):
    """One task line. `list` prints the short form (no level or direction) because a caller that needs those
    is about to `show` that task anyway."""
    if short: return '%s %s %s' % (t['id'], t['state'], t['title'][:40])
    return '%s %s %s %s：%s' % (t['id'], route(t), t['level'], t['state'], t['title'][:60])


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
        if action in ('reassign', 'takeback', 'deliver', 'reject', 'escalate', 'block', 'unblock'):
            task['notificationGeneration'] = task.get('notificationGeneration', 0) + 1
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
            # A hand-over the bus itself placed in `claimed` is still unclaimed: the agent must be able to take
            # it (otherwise that todo can never be opened and would wake it forever).
            states = ('posted', 'rework')
            if t.get('escalation') and not t.get('escalationClaimed'): states += ('claimed',)
            self.require(t, agent, ['to'], states)
            t['escalationClaimed'] = True
            t['state'] = 'claimed'
            t['execution'] = {'agent': agent, 'sessionId': self.bound(agent),
                              'generation': self.dispatch_generation(t),
                              'startedAt': datetime.datetime.now().astimezone().isoformat()}
            t.pop('executionIssue', None)
            if t.get('dispatch'): t['dispatch'].pop('stalled', None)
            self.emit(t, agent, 'claim'); self.save(t)
        return {'id': t['id'], 'state': t['state']}

    def deliver(self, agent, task_id, summary, checks='', evidence=(), limits=''):
        """Hand the work in. The program checks it first, in the delivering agent's own turn: scope (what changed
        against the allowed paths) and the task's acceptance commands. A failure is answered to the deliverer at
        once so it fixes it without anyone else being woken; simple DeepSeek work that passes is accepted here."""
        if not (summary or '').strip(): raise ValueError('Delivery needs a summary')
        t = self.load(task_id)
        self.require(t, agent, ['to'], ('posted', 'claimed', 'rework'))
        completed = False
        result = self.run_gate(t)
        if result.get('checks') and not result['checks']['ok'] and t.get('gateFails', 0) + 1 < GATE_RETRIES:
            with self.lock:
                t = self.load(task_id)
                self.require(t, agent, ['to'], ('posted', 'claimed', 'rework'))
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
            t.pop('finalReview', None)
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
                generation = self.dispatch_generation(t)
                t['aiReview'] = {'status': 'pending', 'attempt': attempt, 'session': self.ai_review_session(),
                                 'requestId': self.ai_review_request(task_id, attempt, generation),
                                 'generation': generation,
                                 'token': 'review_' + secrets.token_urlsafe(18), 'at': now(),
                                 'attempts': 0, 'nextRetryAt': None, 'startedAt': None,
                                 'sentAt': None, 'lease': None, 'leaseId': None}
                self.emit(t, 'agentlink', 'ai_review', '程序验收通过，等待 DeepSeek AI 验收')
            if t['state'] == 'delivered': self.queue_final_review(t)
            self.save(t)
            completed = True                                     # the gate passed and the work is handed in
        if completed: self.count_round(agent)
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

    def ai_review_request(self, task_id, attempt, generation=None):
        """The requestId for one review attempt, scoped to the execution round that produced it."""
        record = task_id if isinstance(task_id, dict) else self.load(task_id)
        if generation is None:
            generation = self.dispatch_generation(record)
        task_id = record['id']
        base = '%s/agentlink/%s/ai-review' % (self.project.root.as_posix(), task_id)
        key = '%s/%d' % (base, attempt) if (generation or 0) <= 0 else '%s/g%d/%d' % (base, generation, attempt)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, key))

    def _ai_review_reset(self, task_id, reason='manual'):
        """Clear a stall so one manual attempt is allowed. Never touches a live lease or a started review."""
        with self.lock:
            t = self.load(task_id)
            r = t.get('aiReview') or {}
            if t['state'] != 'ai_review': return False
            status = r.get('status')
            if r.get('startedAt') or status == 'started': return False                     # already running: a reset would lose that proof
            if status == 'sending' and not self._ai_lease_free(r): return False   # someone is sending right now
            if r.get('attempt') != t.get('rejects', 0): return False
            if status == 'stalled' and int(r.get('attempts') or 0) >= int(r.get('sendLimit') or AI_RETRY_LIMIT):
                r['sendLimit'] = int(r.get('attempts') or 0) + 1
                self.emit(t, 'agentlink', 'ai_review_resume', reason)
            r.update(status='pending', nextRetryAt=None, lease=None, leaseId=None)
            r.pop('error', None); r.pop('stalled', None)
            t['aiReview'] = r
            self.save(t)
            return True

    @staticmethod
    def _ts(value):
        """Parse an ISO stamp defensively: old records may miss or mangle it."""
        if not isinstance(value, str) or not value: return 0
        try: return datetime.datetime.fromisoformat(value).timestamp()
        except ValueError: return 0

    def _ai_review_age(self, r, field):
        """Seconds since a stored stamp. A missing stamp counts as 'long ago' so legacy records are retryable,
        never as 'just now' (which would park a recovered task forever)."""
        stamp = self._ts((r or {}).get(field))
        return (time.time() - stamp) if stamp else float('inf')

    def _ai_lease_free(self, r):
        """A send lease older than AI_LEASE_SECONDS belongs to a crashed process and may be taken."""
        return self._ai_review_age(r, 'lease') >= AI_LEASE_SECONDS

    def _ai_backoff_waiting(self, r):
        """True while the failure backoff has not elapsed. An absent/legacy nextRetryAt never waits."""
        stamp = self._ts((r or {}).get('nextRetryAt'))
        return bool(stamp) and time.time() - stamp < 0

    def _ai_lease_holds(self, r, lease_id):
        """Does this record still belong to the send that is reporting? Every write after an RPC must prove it."""
        return bool(lease_id) and r.get('leaseId') == lease_id and r.get('status') == 'sending'

    def ai_review_claim(self, task_id, attempt):
        """Take the send lease under the lock. Returns (leaseId, requestId) or (None, reason).

        The lease carries a unique id, not just a timestamp: an expired lease may be taken by a new sender,
        and the old one then fails the CAS in `_ai_review_*` instead of overwriting the newer delivery.
        The attempt budget is charged **here**, inside the lock, so crashes still count and alternating
        failures cannot reset it. The RPC itself never runs under this lock."""
        with self.lock:
            t = self.load(task_id)
            if t['state'] != 'ai_review': return None, 'not-ai-review'
            r = t.get('aiReview') or {}
            if r.get('attempt') != t.get('rejects', 0) or r.get('attempt') != attempt: return None, 'stale'
            status = r.get('status')
            if status in ('accepted', 'rejected', 'started'): return None, status
            if status == 'stalled': return None, 'stalled'
            if status == 'sending' and not self._ai_lease_free(r): return None, 'busy'
            if status == 'sent' and self._ai_review_age(r, 'sentAt') < AI_START_TIMEOUT: return None, 'waiting'
            if status in ('failed', 'uncertain') and self._ai_backoff_waiting(r): return None, 'backoff'
            attempts = int(r.get('attempts') or 0) + 1               # monotonic: never derived from successes
            if attempts > int(r.get('sendLimit') or AI_RETRY_LIMIT):
                return None, 'exhausted'
            lease_id = uuid.uuid4().hex
            r.update(status='sending', lease=now(), leaseId=lease_id, lastTryAt=now(), attempts=attempts)
            r.pop('error', None)
            t['aiReview'] = r
            self.save(t)
            return lease_id, r.get('requestId')

    def ai_review_send(self, task_id, attempt=None, manual=False):
        """Send the review prompt for one attempt, at most once per lease.

        `sent` means the bridge queued the prompt; only the reviewer's `started` confirmation proves it began.
        A queued prompt that is never confirmed may be resent **with the same requestId** after AI_START_TIMEOUT,
        inside a bounded budget; past AI_RETRY_LIMIT the task is marked stalled for a human.
        Every write below compares the lease id, so a reply that arrives after the reviewer already confirmed
        `started`, after a cancel, or after a newer send cannot overwrite that newer state."""
        t = self.load(task_id)
        if t['state'] != 'ai_review': return 'skipped'
        attempt = t.get('rejects', 0) if attempt is None else attempt
        r = t.get('aiReview') or {}
        status = r.get('status')
        if status in ('accepted', 'rejected'): return status
        if r.get('startedAt') or status == 'started': return 'started'                 # already running: never re-dispatch
        if status == 'stalled' and not manual: return 'stalled'
        if int(r.get('attempts') or 0) >= int(r.get('sendLimit') or AI_RETRY_LIMIT) and status != 'sending':
            self._ai_review_stall(task_id, attempt, '重投 %d 次仍无进展' % int(r.get('attempts') or 0))
            return 'stalled'
        if not manual:
            if status == 'sent' and self._ai_review_age(r, 'sentAt') < AI_START_TIMEOUT: return 'sent'
            if status in ('failed', 'uncertain') and self._ai_backoff_waiting(r): return status
        lease_id, request = self.ai_review_claim(task_id, attempt)
        if lease_id is None:
            if request == 'exhausted': self._ai_review_stall(task_id, attempt, '重投次数已达上限')
            return request or 'skipped'
        session = self.ai_review_session()
        sid = (session or {}).get('id') or (r.get('session') or {}).get('id')
        if not sid:
            self._ai_review_fail(task_id, attempt, lease_id, 'failed', '未配置 AI 验收会话')
            return 'failed'
        if os.environ.get('AGENTLINK_NO_DISPATCH'):
            self._ai_review_fail(task_id, attempt, lease_id, 'failed', 'AGENTLINK_NO_DISPATCH')
            return 'failed'
        try:
            from . import harness
            c = harness.Client()
            item = harness.find_session(c, sid)
            if item is None: raise ValueError('AI 验收会话不存在：%s' % sid)
            if sid in harness.archived_sessions(): raise ValueError('AI 验收会话已归档')
        except Exception as error:
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            self._ai_review_fail(task_id, attempt, lease_id, 'failed', reason)
            return 'failed'
        with self.lock:
            current = self.load(task_id)
            review = current.get('aiReview') or {}
            if (current['state'] != 'ai_review' or review.get('attempt') != attempt
                    or review.get('requestId') != request or not self._ai_lease_holds(review, lease_id)):
                return 'stale'
            prompt = self.ai_review_prompt(current, review)
        try:
            answer = c.call('session.prompt', {'sessionId': sid, 'requestId': request, 'mode': 'queue',
                                               'content': [{'type': 'text', 'text': prompt}],
                                               'clientTimeZone': self.project.option('timeZone')})
            queued = bool(answer.get('accepted'))
        except Exception:
            queued = None                                        # unknown: treat as uncertain, never as success
        if queued is True:
            ok = self._ai_review_mark(task_id, attempt, lease_id, 'sent',
                                      '审查提示已入队（等待 reviewer 确认 started）')
            return 'sent' if ok else 'stale'
        status = 'uncertain' if queued is None else 'failed'
        self._ai_review_fail(task_id, attempt, lease_id, status,
                             '审查提示发送结果不确定' if queued is None else '审查提示未被接受')
        return status

    def _ai_review_mark(self, task_id, attempt, lease_id, status, note=None):
        """CAS write for a send result: only the lease holder of the current attempt may write it."""
        with self.lock:
            t = self.load(task_id)
            r = t.get('aiReview') or {}
            if t['state'] != 'ai_review' or r.get('attempt') != attempt: return False
            if not self._ai_lease_holds(r, lease_id): return False    # a newer send, a started review or a cancel won
            r.update(status=status, lease=None, leaseId=None)
            if status == 'sent': r['sentAt'] = now(); r.pop('error', None)
            t['aiReview'] = r
            if note: self.emit(t, 'agentlink', 'ai_review', note)
            self.save(t)
            return True

    def _ai_review_fail(self, task_id, attempt, lease_id, status, error):
        """Record a failed/uncertain send and schedule the next permitted retry (exponential, capped).

        The attempt budget was already charged by the claim, so a later success cannot reset it."""
        with self.lock:
            t = self.load(task_id)
            r = t.get('aiReview') or {}
            if t['state'] != 'ai_review' or r.get('attempt') != attempt: return False
            if not self._ai_lease_holds(r, lease_id): return False
            attempts = int(r.get('attempts') or 1)
            wait = min(AI_RETRY_BASE * (2 ** max(0, attempts - 1)), AI_RETRY_MAX)
            r.update(status=status, error=str(error)[:300], lease=None, leaseId=None,
                     nextRetryAt=(datetime.datetime.now().astimezone() +
                                  datetime.timedelta(seconds=wait)).isoformat(timespec='seconds'))
            if attempts >= int(r.get('sendLimit') or AI_RETRY_LIMIT):
                r.update(status='stalled', stalled=True)
                self.emit(t, 'agentlink', 'ai_review_stalled', '重投 %d 次仍未送达，交总控处理' % attempts)
            else:
                self.emit(t, 'agentlink', 'ai_review_failed', str(error)[:200])
            t['aiReview'] = r
            self.save(t)
            return True

    def _ai_review_stall(self, task_id, attempt, note):
        """Mark a stalled review. Only for this attempt and only while it is still under review: a finished,
        cancelled or newer attempt is never rewritten to stalled."""
        with self.lock:
            t = self.load(task_id)
            r = t.get('aiReview') or {}
            if t['state'] != 'ai_review' or r.get('attempt') != attempt: return False
            if r.get('status') in ('accepted', 'rejected') or r.get('stalled'): return False
            status = r.get('status')
            if status == 'started' or r.get('startedAt'):
                if self._ai_review_age(r, 'startedAt') <= AI_STALL_SECONDS: return False
            else:
                if status == 'sending' and not self._ai_lease_free(r): return False
                if status == 'sent' and self._ai_review_age(r, 'sentAt') < AI_START_TIMEOUT: return False
                if status in ('failed', 'uncertain') and self._ai_backoff_waiting(r): return False
                if int(r.get('attempts') or 0) < int(r.get('sendLimit') or AI_RETRY_LIMIT): return False
            r.update(status='stalled', stalled=True, lease=None, leaseId=None)
            t['aiReview'] = r
            self.emit(t, 'agentlink', 'ai_review_stalled', note)
            self.save(t)
            return True

    def ai_review_prompt(self, t, review):
        gate = t.get('gate') or {}
        checks = gate.get('checks')
        if checks and checks.get('ok'): verified = '验收命令全部通过（%ss）' % checks['seconds']
        elif checks: verified = '验收命令未通过：%s' % checks.get('failed')
        else: verified = '任务未设验收命令'
        token, attempt, cmd = review['token'], review['attempt'], cli()
        common = '%s reviewer --as deepseek --task %s --attempt %d --token=%s' % (cmd, t['id'], attempt, token)
        return ('AgentLink AI 验收：任务 %s（%s→%s %s，第 %d 次交付）。你只负责审查，不要修改任何文件、也不要运行会'
                '改动文件的命令。\n允许路径：%s\n程序验收：%s\n改动文件：%s\n\n--- 规格 ---\n%s\n\n'
                '第一步（必须先做）：`%s --verdict started` —— 确认你已开始审查；没有这一步，系统会认为提示只是入队了，'
                '可能用同一个 requestId 重投。\n'
                '审查通过：`%s --verdict accept`\n'
                '审查驳回：`%s --verdict reject --note "<具体问题>"`（代码问题 --kind code，设计问题 design）。\n'
                '令牌只对本次交付有效，不要写进任何文件。审查完只回一行。') % (
            t['id'], t['from'], t['to'], t['level'], attempt + 1, ', '.join(t['paths']) or '（未限定）',
            verified, '、'.join(gate.get('files') or []) or '（无）', self.spec(t['id'])[:1500],
            common, common, common)

    @staticmethod
    def _token_matches(review, token):
        """Constant-time token check that also treats a missing/consumed token as a mismatch."""
        stored = str((review or {}).get('token') or '')
        return isinstance(token, str) and bool(stored) and secrets.compare_digest(stored, token)

    def ai_review(self, agent, task_id, attempt, token, verdict, note='', kind='code', no_dispatch=False):
        """The reviewer's explicit callback: `started`, then `accept` or `reject`.

        Identity, token, attempt and current state are checked, so no natural-language reply is ever parsed
        and no other DeepSeek session can close a task. A stale callback (an old clip after cancel, accept or
        a new attempt) is refused instead of overwriting the newer state, and repeated calls are idempotent."""
        if agent != 'deepseek': raise ValueError('AI 验收只接受 DeepSeek 审查会话')
        if verdict not in ('started', 'accept', 'reject'): raise ValueError('verdict 只能是 started / accept / reject')
        if kind not in KINDS: raise ValueError('kind must be code or design')
        with self.lock:
            t = self.load(task_id)
            r = t.get('aiReview') or {}

            def refuse(message):
                r['error'] = message[:300]; t['aiReview'] = r
                self.emit(t, 'agentlink', 'ai_review_error', message)
                self.save(t)
                raise ValueError(message)

            # Idempotent repeats are recognised only after identity and the attempt are proven: a wrong token
            # or a callback for a cancelled task must never be reported as a successful duplicate.
            same_attempt = r.get('attempt') == attempt
            if verdict == 'started' and same_attempt and r.get('status') == 'started' \
                    and t['state'] == 'ai_review' and self._token_matches(r, token):
                return {'id': t['id'], 'state': t['state'], 'duplicate': True}
            if (t['state'] == 'delivered' and r.get('status') == 'accepted' and same_attempt
                    and r.get('verdict') == 'accept' and verdict == 'accept'):
                # The token is consumed when the verdict lands, so a repeat is proved by the stored outcome:
                # same attempt, same verdict, already accepted. It changes nothing.
                return {'id': t['id'], 'state': t['state'], 'duplicate': True}
            if (t['state'] == 'rework' and r.get('status') == 'rejected' and same_attempt
                    and verdict in ('accept', 'reject')):
                # The attempt already ended in a rejection; another accept after that is a stale clip, not a
                # new decision, so it must not undo the rework the poster already started.
                return {'id': t['id'], 'state': t['state'], 'duplicate': True}
            if t['state'] != 'ai_review':
                # A callback that arrives after the task moved on is stale, not a new error to be recorded.
                raise ValueError('%s 不在 AI 审查阶段（当前 %s），此回调已过期' % (task_id, t['state']))
            # The attempt is checked before the verdict: a callback for another delivery is a mismatched
            # attempt (the reviewer got a stale prompt), not a duplicate of the one that just finished.
            if attempt != t.get('rejects', 0) or attempt != r.get('attempt'):
                refuse('交付序号不匹配：任务当前第 %d 次交付' % (t.get('rejects', 0) + 1))
            if r.get('status') in ('accepted', 'rejected'):
                refuse('本次交付已有结论（%s），忽略过期回调' % r['status'])
            if not self._token_matches(r, token):
                refuse('审查令牌无效')
            if verdict == 'started':
                # The explicit proof that the reviewer began; `sent`/`running` never count as that proof.
                r.update(status='started', startedAt=now(), lease=None, leaseId=None, nextRetryAt=None)
                r.pop('error', None)
                t['aiReview'] = r
                self.emit(t, 'ai-reviewer', 'ai_started', 'AI 审查已确认开始')
                self.save(t)
                return {'id': t['id'], 'state': t['state'], 'status': 'started'}
            r.pop('token', None)
            r.pop('error', None)
            r.pop('lease', None)
            r.pop('nextRetryAt', None)
            if verdict == 'accept':
                t['state'] = 'delivered'
                r.update(status='accepted', verdict='accept', at=now(), finishedAt=now())
                t['aiReview'] = r
                self.queue_final_review(t)
                self.emit(t, 'ai-reviewer', 'ai_accept', 'AI 验收通过，交发起方最终验收')
                self.save(t)
                return {'id': t['id'], 'state': t['state']}
            if not (note or '').strip(): raise ValueError('AI 验收驳回需要写清具体问题')
            t['rejects'] = t.get('rejects', 0) + 1
            r.update(status='rejected', verdict='reject', note=note[:600], at=now(), finishedAt=now())
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
        if verdict == 'cancel': return self.cancel_task(agent, task_id, note, no_dispatch)
        with self.lock:
            t = self.load(task_id)
            self.require(dict(t, reviewer=self.final_review_owner(t)), agent, ['from', 'reviewer'],
                         ('delivered',), steer=True)
            if verdict == 'accept': t['state'] = 'accepted'
            elif verdict == 'cancel': t['state'] = 'cancelled'
            elif verdict == 'reject':
                if not (note or '').strip(): raise ValueError('Rejecting needs a note with the concrete problem')
                t['rejects'] = t.get('rejects', 0) + 1
                t['state'] = 'rework'
            else: raise ValueError('Unknown verdict ' + str(verdict))
            t['review'] = {'verdict': verdict, 'note': (note or '')[:600], 'kind': kind, 'at': now(), 'by': agent}
            self.emit(t, agent, verdict, note)
            accepted = verdict == 'accept'
            escalated = self.escalate(t, kind) if verdict == 'reject' else None
            self.save(t)
        if accepted: self.count_round(agent)                 # outside the lock: count_round takes it again
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
        ruling = bool(t.get('escalations') or t.get('escalation'))
        target = self.lead() if ruling else ESCALATION[kind]
        # Claude only takes work while its session runs (its plugin polls every 30 s); otherwise the lead does.
        if not self.project.binding(target) or (target == 'claude' and not self.alive('claude')):
            target = next((a for a in (self.lead(), 'codex', 'claude')
                           if self.project.binding(a) and (a != 'claude' or self.alive('claude'))), target)
        t['escalations'] = t.get('escalations', 0) + 1
        t['escalation'] = {'from': 'deepseek', 'to': target, 'kind': kind, 'rejects': t['rejects'], 'at': now(),
                           'ruling': ruling, 'poster': t['from']}
        t.pop('dispatch', None)
        self.begin_execution_round(t)
        # The poster stays the poster: only the executor changes. `origin` / `handoverFrom` record who was doing
        # the work before this hand-over, so the panel can say "DeepSeek → Codex" instead of the misleading
        # "Codex → Codex" that a task posted by Codex itself would otherwise show.
        t['origin'] = t.get('origin') or 'deepseek'
        t.setdefault('handoverFrom', []).append({'from': 'deepseek', 'to': target, 'kind': kind, 'at': now()})
        t['to'], t['rejects'] = target, 0
        # A poster taking over its own task works on it directly; anyone else claims it like a new task.
        # `escalationClaimed` is set only when the agent really runs `claim`: a state the hand-over wrote
        # itself must still count as an unclaimed todo, otherwise nobody is ever woken for it.
        t.pop('escalationClaimed', None)
        t['state'] = 'claimed' if target == t['from'] else 'posted'
        self.emit(t, 'agentlink', 'escalate', ('已自动转交过一次，DeepSeek 再次未解决，交总控 %s 裁决' % target) if ruling else
                  'DeepSeek 返工两次未解决（%s问题），转 %s 整改' % ('代码' if kind == 'code' else '设计', target))
        return target

    def final_review_owner(self, t):
        """DeepSeek cannot be woken to sign off: route its receipts to a supported reviewer."""
        owner = (t.get('finalReview') or {}).get('owner') or t.get('from')
        if owner in ('codex', 'claude', 'user'): return owner
        lead = self.lead()
        return lead if lead in ('codex', 'claude') else 'codex'

    @staticmethod
    def age_seconds(stamp):
        try: return max(0, int(time.time() - datetime.datetime.fromisoformat(stamp).timestamp()))
        except (TypeError, ValueError, OverflowError): return 0

    def final_review_info(self, t):
        if t['state'] != 'delivered': return None
        f = t.get('finalReview') or {}
        ai = t.get('aiReview') or {}
        since = f.get('waitingSince') or ai.get('finishedAt') or (t.get('receipt') or {}).get('at') or t.get('updated') or t.get('created')
        age = self.age_seconds(since)
        return {'owner': self.final_review_owner(t), 'waitingSince': since, 'ageSeconds': age,
                'overdue': age >= FINAL_REVIEW_SECONDS, 'reminders': f.get('reminders', 0),
                'maxReminders': FINAL_REVIEW_REMINDERS,
                'reason': f.get('reason') or ('DeepSeek 发起的任务由总控验收' if t.get('from') == 'deepseek' else '等待负责人最终验收')}

    def queue_final_review(self, t):
        t['finalReview'] = {'owner': self.final_review_owner(t), 'waitingSince': now(), 'reminders': 0,
                            'round': 0, 'reason': 'DeepSeek 发起的任务由总控验收' if t['from'] == 'deepseek' else '等待负责人最终验收'}

    def review_attention(self, agent, task_id, action, note=''):
        if action not in ('takeover', 'remind'): raise ValueError('Unknown review action')
        with self.lock:
            t = self.load(task_id)
            self.require(t, agent, ['from'], ('delivered',), steer=True)
            info = self.final_review_info(t)
            f = t.setdefault('finalReview', dict(info, round=0))
            if action == 'remind' and self.age_seconds(f.get('lastManualAt')) < WAKE_DEBOUNCE and f.get('lastManualAt'):
                raise ValueError('刚刚已提醒，请稍后重试')
            if action == 'takeover':
                lead = self.lead()
                f['owner'] = lead if lead in ('codex', 'claude') else 'codex'
                f['reason'] = '人工交给总控验收'
            else:
                f['manualReminders'] = f.get('manualReminders', 0) + 1
            f['lastManualAt'] = now()
            f['round'] = f.get('round', 0) + 1
            f['lastReminderAt'] = now()
            self.emit(t, agent, 'final_' + action, note or ('交总控最终验收' if action == 'takeover' else '人工提醒最终验收'))
            self.save(t)
        return {'id': task_id, 'state': t['state'], 'finalReview': self.final_review_info(t)}

    def pump_final_review(self, task_id):
        """Recover legacy queues, then remind at a bounded cadence. No acceptance is inferred."""
        with self.lock:
            t = self.load(task_id)
            if t['state'] != 'delivered': return
            info = self.final_review_info(t)
            f = t.get('finalReview')
            if not f:
                f = t['finalReview'] = dict(info, round=0)
            if info['owner'] == 'user':
                return  # the dashboard is the human's inbox; no model wake on their behalf
            elapsed = self.age_seconds(f.get('lastReminderAt') or info['waitingSince'])
            if elapsed < FINAL_REVIEW_SECONDS or f.get('reminders', 0) >= FINAL_REVIEW_REMINDERS: return
            if info['owner'] != self.lead() and info['owner'] == 'claude' and not self.alive('claude'):
                f['owner'] = self.lead() if self.lead() in ('codex', 'claude') else 'codex'
                f['reason'] = '原验收方离线，超时转总控'
            f['reminders'] = f.get('reminders', 0) + 1
            f['round'] = f.get('round', 0) + 1
            f['lastReminderAt'] = now()
            self.emit(t, 'agentlink', 'final_reminder', '最终验收超时，提醒 %s（%d/%d）' % (
                self.final_review_owner(t), f['reminders'], FINAL_REVIEW_REMINDERS))
            self.save(t)

    def task_actions(self, t):
        """Actions available to the human in the panel, evaluated again under lock on mutation."""
        state = t['state']
        if state in TERMINAL: return []
        if state == 'recalling': return []  # only the original executor can acknowledge; never force-release locks
        if state == 'delivered': return ['accept', 'reject', 'takeover', 'remind', 'cancel']
        if state == 'paused': return ['resume', 'cancel']
        actions = ['pause', 'cancel'] if state in OPEN else []
        d, r = t.get('dispatch') or {}, t.get('aiReview') or {}
        if (state == 'ai_review' and not r.get('startedAt')) or (t['to'] == 'deepseek' and state in ('posted', 'rework') and d.get('state') in ('held', 'pending', 'uncertain')):
            actions.append('dispatch')
        return actions

    @staticmethod
    def needs_stop(t):
        previous = (t.get('control') or {}).get('previousState') if t['state'] == 'paused' else t['state']
        return previous in ('claimed', 'blocked') and not (t.get('escalation') and not t.get('escalationClaimed'))

    def start_recall(self, t, agent, to, note='', intent='reassign'):
        """Called under the task lock: no new owner is allowed until the old one acknowledges."""
        t['recallGeneration'] = t.get('recallGeneration', 0) + 1
        t['recall'] = {'status': 'pending', 'from': t['to'], 'to': to, 'requestedBy': agent,
                       'at': now(), 'note': note[:600], 'token': 'recall_' + secrets.token_urlsafe(18),
                       'generation': t['recallGeneration'], 'notification': 'pending', 'intent': intent}
        t['state'] = 'recalling'
        self.emit(t, agent, 'recall', note or '请求原执行方停止并确认；原文件锁保留')
        self.save(t)

    def cancel_task(self, agent, task_id, note='', no_dispatch=False):
        with self.lock:
            t = self.load(task_id)
            self.require(t, agent, ['from'], tuple(s for s in OPEN if s != 'recalling') + ('delivered',), steer=True)
            if self.needs_stop(t):
                self.start_recall(t, agent, None, note, intent='cancel')
            else:
                t['state'] = 'cancelled'
                t['review'] = {'verdict': 'cancel', 'note': note[:600], 'at': now(), 'by': agent}
                self.emit(t, agent, 'cancel', note)
                self.save(t)
        out = {'id': task_id, 'state': t['state'], 'to': t['to']}
        if t['state'] == 'recalling' and t['to'] == 'deepseek' and not no_dispatch:
            out['notification'] = self.notify_recall(task_id)
        return out

    def control(self, agent, task_id, action, note=''):
        """Cooperative scheduling control, never an OS/session kill. Pauses keep path locks."""
        if action not in ('pause', 'resume'): raise ValueError('Unknown control action')
        with self.lock:
            t = self.load(task_id)
            self.require(t, agent, ['from'], ('paused',) if action == 'resume' else tuple(s for s in OPEN if s not in ('paused', 'recalling')), steer=True)
            if action == 'pause':
                t['control'] = {'previousState': t['state'], 'at': now(), 'by': agent, 'note': note[:600],
                                'runtime': 'unconfirmed'}
                t['state'] = 'paused'
            else:
                previous = (t.get('control') or {}).get('previousState')
                if previous not in OPEN or previous == 'paused': raise ValueError('暂停记录缺少原状态，无法恢复')
                t['state'] = previous
                t['control']['resumedAt'] = now()
                # Pausing scheduling does not stop a running reviewer. Preserve its token,
                # started evidence, lease, request ID and retry budget on resume.
            self.emit(t, agent, action, note or ('暂停 AgentLink 调度；宿主执行状态未确认' if action == 'pause' else '恢复 AgentLink 调度'))
            self.save(t)
        return {'id': task_id, 'state': t['state'], 'control': t['control']}

    def block(self, agent, task_id, note, missing='', attempts='', help_needed='', evidence=()):
        if not (note or '').strip(): raise ValueError('Blocking needs a note')
        with self.lock:
            t = self.load(task_id)
            self.require(t, agent, ['to'], ('posted', 'claimed', 'rework', 'blocked'))
            report = {'reason': note.strip()[:1200], 'missing': (missing or '').strip()[:800],
                      'attempts': (attempts or '').strip()[:800], 'helpNeeded': (help_needed or '').strip()[:800],
                      'evidence': [str(e)[:200] for e in list(evidence)[:10]], 'by': agent,
                      'owner': self.lead(), 'status': 'open'}
            previous = t.get('blocker') or {}
            if t['state'] == 'blocked' and all(previous.get(k) == v for k, v in report.items()):
                return {'id': task_id, 'state': 'blocked', 'owner': self.lead(), 'duplicate': True}
            report['at'] = now()
            t['blocker'] = report
            t['state'] = 'blocked'
            self.emit(t, agent, 'block', note); self.save(t)
        return {'id': t['id'], 'state': t['state'], 'owner': self.lead(),
                'next': '总控先尝试解决；解决后 unblock 续跑，仍无法解决则由总控向用户提问。任务与原文件锁保留。'}

    def unblock(self, agent, task_id, note, no_dispatch=False):
        """Resume the original executor only after the lead/poster records its resolution.

        This is not a transfer or a stop acknowledgement: the original executor retains its locks.
        """
        if not (note or '').strip(): raise ValueError('Unblocking needs a resolution note')
        with self.lock:
            t = self.load(task_id)
            self.require(t, agent, ['from'], ('blocked',), steer=True)
            report = t.setdefault('blocker', {})
            report.update(status='resolved', resolution=note.strip()[:1200], resolvedBy=agent, resolvedAt=now())
            t['state'] = 'posted'
            t.pop('dispatch', None)
            self.begin_execution_round(t)
            self.emit(t, agent, 'unblock', note)
            self.save(t)
        out = {'id': task_id, 'state': 'posted', 'to': t['to']}
        if t['to'] == 'deepseek' and not no_dispatch: out['dispatch'] = self.dispatch(task_id)
        return out

    @staticmethod
    def execution_attention(t):
        return t['state'] in ('posted', 'claimed', 'rework') and bool(
            (t.get('executionIssue') or {}).get('status') == 'unreported' or
            (t.get('dispatch') or {}).get('stalled'))

    @staticmethod
    def blocker_reason(t):
        report = t.get('blocker') or {}
        if report.get('reason'): return report['reason']
        return next((h.get('note', '') for h in reversed(t.get('history') or []) if h.get('action') == 'block'),
                    '执行方报告受阻，需总控核查')

    def reassign(self, agent, task_id, to, note='', no_dispatch=False):
        """Take a task back (to yourself) or hand it to another agent, keeping history and locks."""
        if to not in AGENTS: raise ValueError('Tasks go to claude, codex or deepseek')
        with self.lock:
            t = self.load(task_id)
            self.require(t, agent, ['from'], tuple(s for s in OPEN if s != 'recalling') + ('delivered',), steer=True)
            if to == t['to']: raise ValueError('任务已由该执行方持有')
            # A queued prompt is not a claim. An unclaimed escalation is safe to take back immediately.
            running = self.needs_stop(t)
            if running:
                self.start_recall(t, agent, to, note)
                out = {'id': task_id, 'state': 'recalling', 'to': t['to'], 'target': to}
            else:
                out = None
            if running:
                pass
            elif to == agent and agent != t['from']:          # unclaimed or already delivered
                t['to'], t['state'], t['rejects'] = agent, 'claimed', 0
                t.pop('dispatch', None)
                self.begin_execution_round(t)                 # new round: a new dispatch identity from here on
                self.emit(t, agent, 'reassign', note or '总控接手')
            elif to == t['from']:
                t['to'], t['state'], t['rejects'] = to, 'claimed', 0
                t.pop('dispatch', None)
                self.begin_execution_round(t)
                self.emit(t, agent, 'takeback', note)
            else:
                t['to'], t['state'], t['rejects'] = to, 'posted', 0
                t.pop('dispatch', None)
                self.begin_execution_round(t)
                self.emit(t, agent, 'reassign', note)
            if not running:
                t.pop('escalation', None)
                t.pop('escalationClaimed', None)
                t.pop('finalReview', None)
                t.pop('control', None)
                if t.get('aiReview'): t['aiReview'].pop('token', None)
            self.save(t)
        if out is not None:
            if t['to'] == 'deepseek' and not no_dispatch: out['notification'] = self.notify_recall(task_id)
            return out
        out = {'id': t['id'], 'state': t['state'], 'to': t['to']}
        if t['state'] == 'posted' and t['to'] == 'deepseek' and not no_dispatch: out['dispatch'] = self.dispatch(t['id'])
        return out

    def recall_prompt(self, t):
        r = t['recall']
        target = '终止任务' if r.get('intent') == 'cancel' else '交给 ' + r['to']
        return ('AgentLink 回收请求 %s：总控/发起方要求停止本任务并%s。停止编辑允许路径，'
                '等待你为本任务启动的工具及子智能体全部停止后，执行 %s --as %s stop-ack %s --token=%s '
                '--note "已停止本任务全部写入、工具及子智能体"。未确认前任务锁仍归你；不要继续实现或交付。'
                '如不能保证停止，不要确认，说明原因。') % (t['id'], target, cli(), r['from'], t['id'], r['token'])

    def notify_recall(self, task_id):
        """A queued stop request is not proof of stopping. Retry uncertain sends with the same request ID."""
        t = self.load(task_id)
        if t['state'] != 'recalling' or t['to'] != 'deepseek': return 'skipped'
        r = t['recall']
        if r.get('notification') == 'sent' or self.age_seconds(r.get('lastNotifyAt')) < 60 and r.get('lastNotifyAt'):
            return r.get('notification', 'pending')
        if r.get('notifyAttempts', 0) >= 3: return 'failed'
        if os.environ.get('AGENTLINK_NO_DISPATCH'): return 'held'
        generation = r['generation']
        try:
            from . import harness
            c, sid = harness.Client(), self.bound('deepseek')
            if not sid: raise ValueError('DeepSeek 未绑定，等待原执行方确认停止')
            current = self.load(task_id)
            if current['state'] != 'recalling' or current['recall']['generation'] != generation: return 'skipped'
            result = c.call('session.prompt', {'sessionId': sid, 'requestId': 'agentlink-recall-%s-%s-%d' % (
                uuid.uuid5(uuid.NAMESPACE_URL, self.project.root.as_posix()), task_id, generation), 'mode': 'queue',
                'content': [{'type': 'text', 'text': self.recall_prompt(current)}],
                'clientTimeZone': self.project.option('timeZone')})
            status = 'sent' if result.get('accepted') else 'uncertain'
        except Exception:
            status = 'uncertain'
        with self.lock:
            t = self.load(task_id)
            if t['state'] != 'recalling' or t['recall']['generation'] != generation: return 'skipped'
            t['recall'].update(notification=status, lastNotifyAt=now(), notifyAttempts=r.get('notifyAttempts', 0) + 1)
            self.save(t)
        return status

    def acknowledge_stop(self, agent, task_id, token, note='', no_dispatch=False):
        with self.lock:
            t = self.load(task_id)
            self.require(t, agent, ['to'], ('recalling',))
            r = t.get('recall') or {}
            if not isinstance(token, str) or not secrets.compare_digest(r.get('token') or '', token):
                raise ValueError('回收确认令牌无效或已过期')
            if not note.strip(): raise ValueError('请说明本任务的工具、子智能体与文件写入均已停止')
            r.pop('token', None)
            r.update(status='confirmed', confirmedAt=now(), confirmedBy=agent, confirmation=note[:600])
            t['origin'] = t.get('origin') or agent
            t.setdefault('handoverFrom', []).append({'from': agent, 'to': r['to'], 'at': now(), 'kind': 'recall'})
            cancelled = r.get('intent') == 'cancel'
            if cancelled:
                t['state'] = 'cancelled'
                t['review'] = {'verdict': 'cancel', 'note': r.get('note') or '', 'at': now(), 'by': r['requestedBy']}
            else:
                t['to'], t['state'], t['rejects'] = r['to'], 'posted', 0
                t.pop('dispatch', None)
                self.begin_execution_round(t)                 # the receiver is different: a fresh round
            if cancelled: t.pop('dispatch', None)
            t.pop('escalation', None)
            t.pop('escalationClaimed', None)
            t.pop('finalReview', None)
            if t.get('aiReview'): t['aiReview'].pop('token', None)
            self.emit(t, agent, 'stop_ack', note)
            self.emit(t, r['requestedBy'], 'cancel' if cancelled else 'reassign',
                      '原执行方确认停止，任务已终止' if cancelled else '原执行方确认停止，现已移交 ' + r['to'])
            self.save(t)
        out = {'id': task_id, 'state': t['state'], 'to': t['to']}
        if t['state'] == 'posted' and t['to'] == 'deepseek' and not no_dispatch: out['dispatch'] = self.dispatch(task_id)
        return out

    def show(self, task_id, full=False):
        t = self.load(task_id)
        head = '%s [%s %s %s] %s\n允许路径：%s%s\n基线：%s' % (
            t['id'], route(t), t['level'], t['state'], t['title'], ', '.join(t['paths']) or '（未限定）',
            '；持有构建锁' if t.get('build') else '', t.get('base'))
        if t['state'] == 'recalling': head += '\n' + self.recall_prompt(t)
        blocker = t.get('blocker') or {}
        if t['state'] == 'blocked': head += '\n' + self.detail(self.lead(), t, 'blocked')
        elif blocker.get('status') == 'resolved':
            head += '\n总控处置及续跑指引：' + (blocker.get('resolution') or '')[:1200]
        if self.execution_attention(t): head += '\n' + self.detail(self.lead(), t, 'stalled')
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
            if t['state'] == 'paused': continue
            if t['state'] == 'recalling':
                if agent == t['to']: out.append('停止确认 ' + line(t))
                continue
            waiting = t['to'] == agent and t['state'] == 'claimed' and t.get('escalation') \
                and not t.get('escalationClaimed')
            if t['to'] == agent and t['state'] in ('posted', 'rework') or waiting:
                note = ''
                if t['state'] == 'rework': note = '（返工：%s）' % t['review']['note'][:80]
                elif waiting: note = '（转交给你，未领取）'
                out.append('待办 ' + line(t) + note)
            elif self.final_review_owner(t) == agent and t['state'] == 'delivered':
                out.append('待验收 ' + line(t) + '：' + t['receipt']['summary'][:120].replace('\n', ' '))
            elif agent == lead and t['state'] == 'delivered' and self.needs_audit(t):
                out.append('待审计 ' + line(t) + '：发起方验收，总控审计方向与集成影响')
            elif t['state'] == 'blocked' and agent in (t['from'], lead):
                out.append('受阻 ' + line(t) + '：' + self.blocker_reason(t)[:120])
            elif t['state'] == 'ai_review' and agent in (t['from'], lead) \
                    and (t.get('aiReview') or {}).get('status') in ('failed', 'uncertain'):
                out.append('AI 验收未送达 ' + line(t) + '：' + str((t.get('aiReview') or {}).get('error') or '-')[:100])
            elif self.execution_attention(t) and agent in (t['from'], lead):
                out.append('缺少回执 ' + line(t) + '：执行方可能已结束但未交付或上报受阻，由总控核查')
        return out

    def context_items(self, agent):
        """What the agent should read: what it must act on, plus the lead's audit duties."""
        items = self.wake_items(agent, quiet_ok=True)
        if agent == self.lead():
            for t in self.all():
                if t['state'] == 'paused': continue
                stalled = self.execution_attention(t)
                if t['from'] == agent or t['to'] == agent: continue
                if t['state'] == 'delivered' and self.needs_audit(t) and self.final_review_owner(t) != agent: why = 'audit'
                elif t['state'] == 'blocked': why = 'blocked'
                elif t['state'] == 'ai_review' and (t.get('aiReview') or {}).get('status') in ('failed', 'uncertain'):
                    why = 'ai_review'
                elif stalled: why = 'stalled'
                else: continue
                items[self.wake_key(t, why)] = t
        return items

    def headline(self, agent, t, why):
        """One line per open item, for a session that has just started: enough to decide what to do, cheap to
        read. The spec, receipt and diff are fetched with `show` only for the item it actually works on."""
        head = '%s %s（%s→%s %s）' % (t['id'], t['title'][:40], t['from'], t['to'], t['level'])
        if why == 'todo':
            state = '返工中' if (t.get('review') or {}).get('verdict') == 'reject' else '待领取'
            return '【待办】%s：%s；做法：show %s → claim %s' % (head, state, t['id'], t['id'])
        if why == 'recall': return '【回收请求】' + self.recall_prompt(t)
        if why in ('delivered', 'audit'):
            summary = ((t.get('receipt') or {}).get('summary') or '')[:80].replace('\n', ' ')
            return '【%s】%s：%s' % ('待验收' if why == 'delivered' else '待审计（发起方验收）', head, summary)
        if why == 'blocked':
            return '【受阻，交总控处理】%s：%s；先尝试解决，仍无法解决才由总控询问用户' % (head, self.blocker_reason(t)[:120])
        if why in ('ai_review', 'ai_review_failed'):
            ai = t.get('aiReview') or {}
            return '【AI 验收未送达】%s：%s' % (head, str(ai.get('error') or ai.get('status') or '-')[:100])
        return '【缺少执行回执】%s：由总控核查完成情况或缺失条件，不能视为任务结束' % head

    def detail(self, agent, t, why):
        """Everything the agent needs to act, inline: a tool call to `show` would cost it another full round."""
        head = '%s %s（%s %s）' % (t['id'], t['title'][:60], route(t), t['level'])
        clip = lambda text, n: text if len(text) <= n else text[:n] + '…（完整：show %s）' % t['id']
        if why == 'todo':
            lines = ['【待办】' + head, '允许路径：' + (', '.join(t['paths']) or '（未限定）') + ('；构建锁' if t.get('build') else '')]
            review = t.get('review') or {}
            if review.get('verdict') == 'reject': lines.append('返工意见：' + review['note'][:300])
            if t.get('escalation'): lines.append('整改：DeepSeek 两次未解决，交你处理')
            lines.append('规格：' + clip(self.spec(t['id']).strip(), 900))
            guide = self.guide(t['id']).strip()
            if guide: lines.append('指引：' + clip(guide, 500))
            blocker = t.get('blocker') or {}
            if blocker.get('status') == 'resolved':
                lines.append('受阻处理意见（续跑）：' + clip(blocker.get('resolution') or '', 600))
            lines.append('做法：claim %s → 实现 → deliver %s --summary …' % (t['id'], t['id']))
            lines.append('缺文件/依赖/权限或无法完成：必须 block %s --note "原因" --missing "缺项" '
                         '--attempts "已尝试" --help-needed "需要总控解决什么"；不能仅聊天留言结束或虚假交付。' % t['id'])
        elif why == 'recall':
            lines = ['【停止并确认】' + self.recall_prompt(t)]
        elif why in ('delivered', 'audit'):
            r = t.get('receipt') or {}
            lines = ['【%s】' % ('待验收' if why == 'delivered' else '待审计（发起方验收）') + head,
                     '回执：' + (r.get('summary') or '')[:400].replace('\n', ' ')]
            if r.get('checks'): lines.append('检查：' + r['checks'][:200].replace('\n', ' '))
            if r.get('limits'): lines.append('边界：' + r['limits'][:200].replace('\n', ' '))
            lines.extend(self.gate_lines(t))
            owner = self.final_review_owner(t)
            if owner != t['from']:
                lines.append('最终验收负责人：%s（原发起方 %s；已路由或接管，有权 accept/reject）' % (owner, t['from']))
            if why == 'delivered':
                lines.append('做法：看 diff 后 accept %s，或 reject %s --note "<具体问题>"%s' % (
                    t['id'], t['id'], ' --kind code|design' if t['to'] == 'deepseek' else ''))
        elif why == 'blocked':
            report = t.get('blocker') or {}
            lines = ['【受阻，交总控处理】' + head, '原因：' + clip(self.blocker_reason(t), 600)]
            for key, label in (('missing', '缺少条件'), ('attempts', '已尝试'), ('helpNeeded', '需要帮助')):
                if report.get(key): lines.append(label + '：' + clip(report[key], 400))
            if report.get('evidence'): lines.append('证据：' + ', '.join(report['evidence'])[:400])
            lines.append('规格：' + clip(self.spec(t['id']).strip(), 500))
            lines.append('总控职责：先检查可用文件、依赖与替代办法并尝试解决；解决后 '
                         'unblock %s --note "处置和续跑指引"。仍无法解决时，仅由总控向用户提出具体问题，保持受阻。'
                         '换人编辑必须 reassign 并等待原执行方 stop-ack，不得擅自 cancel 或接管文件锁。' % t['id'])
        elif why in ('ai_review', 'ai_review_failed'):
            ai = t.get('aiReview') or {}
            session = ai.get('session') or {}
            lines = ['【AI 验收未送达】' + head,
                     '状态：' + AI_REVIEW_LABEL.get(ai.get('status'), str(ai.get('status') or '-')) +
                     ('；会话 ' + (session.get('title') or session['id']) if session.get('id') else '；未配置会话'),
                     '错误：' + str(ai.get('error') or '-')[:200],
                     '做法：在设置页确认 AI 验收会话后 `%s dispatch %s` 重试；或直接人工验收' % (cli(), t['id'])]
        else:
            issue = t.get('executionIssue') or {}
            lines = ['【缺少执行回执，交总控核查】' + head,
                     '观测：' + (issue.get('note') or '执行方空闲但未交付或上报受阻'),
                     '任务原状态和文件锁保留；此观测不能证明工具已停、任务成功或具体受阻原因。',
                     '总控先核查可用文件、执行结果及缺项，尝试解决；仍不能解决才由总控向用户提问。'
                     '换人写入必须走 reassign / stop-ack；不要直接重投或把等待当作完成。']
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
        emitted = []
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
                emitted.append(k)
            hint = '（开工前 show <ID> 读完整规格）' if start else ''
            text = 'AgentLink（项目 %s，命令 %s --as %s <cmd>）%s：\n%s' % (
                self.project.name, cli(), agent, hint, ('\n' if start else '\n\n').join(body))
        keys = sorted((set(seat.get('told', [])) | set(emitted)) & set(items))
        wake_items = self.wake_items(agent)
        wake_keys = sorted((set(seat.get('woken', [])) | set(emitted)) & set(wake_items))
        if seat.get('told') != keys or seat.get('woken') != wake_keys:
            with self.lock:
                seat = self.read_seat(agent)
                # merge, never overwrite: another process may have told the agent about other keys meanwhile
                seat['told'] = sorted(set(seat.get('told', [])) | set(keys))
                seat['woken'] = sorted(set(seat.get('woken', [])) | set(wake_keys))
                write_json(self.seat_path(agent), seat)
        # The rules reminder rides along with a turn that is happening anyway. `active` is decided from the real
        # open work of this agent, so a session with nothing to do stays silent and never starts a turn for it.
        active = bool(items) or self._has_active_work(agent)
        rules = self.rules_block(agent, event, seat, active)
        if rules: text = (text + '\n\n' + rules) if text else rules
        return text

    def _has_active_work(self, agent):
        """Does this agent have any live task it is party to? A claimed task counts as active work but is not a
        todo, so it must not be re-announced as something to act on."""
        for t in self.all():
            if t['state'] in TERMINAL: continue
            if agent in (t['from'], t['to']): return True
        return False

    # -- the short rules ------------------------------------------------------
    # A long conversation forgets how this bus works, so the hook re-showed the rules anyway. Keep this text
    # short and factual: it is paid for on every reminder, and it must not read like a task list.
    RULES_TEXT = (
        'AgentLink 规则（提醒）：\n'
        '· 开工前先 locks，不动别人持有的路径；claim 后再改文件。\n'
        '· 缺文件/依赖/权限或无法完成必须 block --note --missing --attempts --help-needed 上报总控，不能只留言结束。\n'
        '· 总控先尝试解决，解决后 unblock 续跑；仍无法解决才由总控向用户提问，任务保持受阻。\n'
        '· 交付用 deliver --summary/--checks/--evidence，回执写进命令。\n'
        '· 收到停止请求先停写入/工具/子智能体，再回 stop-ack；确认前不要再改文件。\n'
        '· 只回一行结论；详细内容放回执，别复述规格。'
    )

    def count_round(self, agent):
        """One completed round for this agent: a successful delivery or an acceptance it performed.

        Failed gate checks, duplicate callbacks and refusals never reach this call, so they never inflate the
        counter. The counter rides in the seat next to `roundsSinceRules`, so it survives restarts and stays
        per agent."""
        if not agent: return
        with self.lock:
            seat = self.read_seat(agent)
            seat['roundsDone'] = int(seat.get('roundsDone') or 0) + 1
            seat['roundsSinceRules'] = int(seat.get('roundsSinceRules') or 0) + 1
            write_json(self.seat_path(agent), seat)

    def rules_every(self):
        """How many completed rounds between rule reminders. Old configs may lack the option: default to 20.

        The reminder is counted in work done, not in wall-clock time: a conversation that finishes twenty rounds
        is exactly the conversation that has drifted far enough to need the rules again, while a session that has
        been open for an hour without doing anything needs nothing."""
        value = self.project.option('rulesReminderEvery')
        if value is None: return RULES_ROUNDS_DEFAULT          # legacy config without the field
        try: return max(0, int(value))
        except (TypeError, ValueError): return RULES_ROUNDS_DEFAULT

    def should_show_rules(self, agent, event, seat):
        """Whether this hook turn should carry the rules block.

        A session start always gets them (it is the moment the agent has no context). A running conversation gets
        them once `rulesReminderEvery` rounds have been completed since the last reminder; the counter is
        persisted in the seat, so it survives restarts and is per agent."""
        every = self.rules_every()
        if not every: return False
        if not seat.get('rulesAt'): return True               # this conversation never got them
        done = int(seat.get('roundsSinceRules') or 0)
        return done >= every

    def rules_block(self, agent, event, seat, active):
        """The rendered reminder, plus the seat write that records it.

        `active` decides whether a mid-conversation reminder is worth paying for at all: a session with no open
        work stays silent, because a rules reminder must never be the reason a model starts a turn."""
        if event != 'SessionStart' and not active: return ''   # a reminder must never be why a model starts a turn
        if not self.should_show_rules(agent, event, seat): return ''
        with self.lock:
            fresh = self.read_seat(agent)                     # merge under the lock: another process may have told it
            if not self.should_show_rules(agent, event, fresh): return ''
            fresh['rulesAt'] = now()
            fresh['roundsSinceRules'] = 0                     # this reminder is the new baseline
            fresh['rulesShownAtRound'] = fresh.get('roundsDone', 0)
            write_json(self.seat_path(agent), fresh)
        return self.RULES_TEXT

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
            if t['state'] == 'paused': continue
            if t['state'] == 'recalling':
                if t['to'] == agent: out[self.wake_key(t, 'recall')] = t
                continue
            stalled = self.execution_attention(t)
            if t['to'] == agent and t['state'] in ('posted', 'rework'): why = 'todo'
            elif t['to'] == agent and t['state'] == 'claimed' and t.get('escalation') \
                    and t['escalation'].get('to') == agent and not t.get('escalationClaimed'):
                # A hand-over that the taking agent still has to pick up counts as its todo. Without this a
                # task escalated to Codex sits in `claimed` (nobody claimed it, the escalation wrote that state)
                # and never wakes it: the poster sees "转交 Codex" while Codex is told nothing.
                why = 'todo'
            elif (t['state'] == 'delivered' and self.final_review_owner(t) == agent) or (t['state'] == 'blocked' and agent in (t['from'], self.lead())):
                # A DeepSeek L0 receipt is only read: it waits for the poster's next turn instead of starting one.
                if t['state'] == 'delivered' and t['to'] == 'deepseek' and t['level'] == 'L0' and not quiet_ok \
                        and not (t.get('finalReview') or {}).get('round'): continue
                why = t['state']
            elif agent in (t['from'], self.lead()) and t['state'] == 'ai_review' \
                    and ((t.get('aiReview') or {}).get('status') in ('failed', 'uncertain', 'stalled') or (t.get('aiReview') or {}).get('stalled')):
                why = 'ai_review_failed'
            elif agent in (t['from'], self.lead()) and stalled: why = 'stalled'
            else: continue
            out[self.wake_key(t, why)] = t
        return out

    @staticmethod
    def wake_key(t, why):
        revision = ':r%d' % (t.get('finalReview') or {}).get('round', 0) if t['state'] == 'delivered' else ''
        if t['state'] == 'recalling': revision = ':g%d' % (t.get('recall') or {}).get('generation', 0)
        if t.get('notificationGeneration'): revision += ':n%d' % t['notificationGeneration']
        return '%s:%s:%d%s:%s' % (t['id'], t['state'], t.get('rejects', 0), revision, why)

    @staticmethod
    def changed_at(t):
        try: return datetime.datetime.fromisoformat(t.get('updated') or t['created']).timestamp()
        except (KeyError, ValueError): return 0

    def pending_for(self, agent):
        """(ids needing a wake, whether the burst has settled). A key the agent was woken for or already
        saw in a prompt it was working on never wakes it again; neither does one whose wake another process has
        already reserved (unless that lease went stale)."""
        items = self.wake_items(agent)
        seat = self.read_seat(agent)
        seen = set(seat.get('woken', []))
        leased = self._lease_keys(self._live_leases(seat))
        pending = {k: t for k, t in items.items() if k not in seen and k not in leased}
        newest = max((self.changed_at(t) for t in pending.values()), default=0)
        settled = bool(pending) and time.time() - newest >= WAKE_QUIET
        return sorted({t['id'] for t in pending.values()}), settled

    # -- wake reservation -----------------------------------------------------
    # Two processes ticking at the same time used to both see the same pending item and both queue a prompt
    # into the same conversation. The reservation below makes "this key is being woken right now" a fact other
    # processes can see, before any RPC happens.
    #
    # It is NOT exactly-once: the host may already have queued a message that cannot be recalled, so a lease that
    # expires mid-flight can still produce a duplicate. What it guarantees is one reservation per key at a time,
    # and that a reply from a superseded send never marks the newer batch as delivered.

    def _live_leases(self, seat):
        """Lease ids that still hold a key: expired ones are ignored (a crashed process must recover)."""
        t = time.time()
        out = []
        for lease in (seat.get('wakeLeases') or []):
            if isinstance(lease, dict) and t - (lease.get('at') or 0) < WAKE_LEASE_SECONDS:
                out.append(lease)
        return out

    @staticmethod
    def _lease_keys(leases):
        keys = set()
        for lease in leases:
            keys.update(lease.get('keys') or [])
        return keys

    def reserve_wake(self, agent, keys=None, limit=6):
        """Atomically reserve the wake for this agent. Returns {lease, keys, ids} or None.

        Runs entirely under the bus lock: it re-reads the seat, the pending set, the cooldown and the per-hour
        cap, so N concurrent callers can only produce one waiver. The RPC that follows happens outside the lock.
        """
        with self.lock:
            if not self.wake_enabled(): return None
            seat = self.read_seat(agent)
            live = self._live_leases(seat)
            if live: return None  # one sender per recipient, including batches with different keys
            items = self.wake_items(agent)
            seen = set(seat.get('woken', []))
            leased = self._lease_keys(live)
            wanted = {k: t for k, t in items.items() if k not in seen and k not in leased}
            if keys is not None:
                wanted = {k: t for k, t in wanted.items() if k in set(keys)}
            if not wanted: return None
            newest = max((self.changed_at(t) for t in wanted.values()), default=0)
            # 同一批事项要等它安静下来再叫，否则每个事件都会拉起一整轮
            if time.time() - newest < WAKE_QUIET: return None
            now_t = time.time()
            recent = [w for w in seat.get('wakes', []) if now_t - w < 3600]
            if len(recent) >= WAKE_PER_HOUR: return None
            if recent and now_t - recent[-1] < WAKE_DEBOUNCE: return None
            ids = []
            chosen = []
            for k in sorted(wanted, key=lambda k: (self.changed_at(wanted[k]), k)):
                tid = wanted[k]['id']
                if tid in ids: chosen.append(k); continue
                if len(ids) >= limit: continue
                ids.append(tid); chosen.append(k)
            lease_id = uuid.uuid4().hex
            lease = {'id': lease_id, 'keys': chosen, 'at': now_t, 'agent': agent}
            seat['wakeLeases'] = live + [lease]
            write_json(self.seat_path(agent), seat)
            return {'lease': lease_id, 'keys': chosen, 'ids': ids}

    def finish_wake(self, agent, lease_id, delivered, **extra):
        """Consume one's own lease. Only the holder can finish it; a superseded lease changes nothing.

        Success marks exactly the keys actually sent, so an item that the host never received stays pending.
        Failure counts against the cooldown and the hourly cap (a stale thread must not cause a retry storm).
        """
        if not lease_id: return False
        with self.lock:
            seat = self.read_seat(agent)
            leases = self._live_leases(seat)
            mine = next((l for l in leases if isinstance(l, dict) and l.get('id') == lease_id), None)
            if mine is None: return False                    # expired and reclaimed, or already finished
            seat['wakeLeases'] = [l for l in leases if l is not mine]
            t = time.time()
            seat['wakes'] = [w for w in seat.get('wakes', []) if t - w < 3600] + [t]
            if delivered:
                items = self.wake_items(agent)
                # 只消费本次真正送出的键：准备阶段才终结的任务不会被误标已通知
                seat['woken'] = sorted(set(seat.get('woken', [])) | (set(mine.get('keys') or []) & set(items)))
            seat.update(wokeAt=now(), **extra)
            write_json(self.seat_path(agent), seat)
            return True

    def validate_wake(self, agent, lease_id):
        """Recheck the reserved batch immediately before invoking the host; RPC never holds the bus lock."""
        with self.lock:
            if not self.wake_enabled(): return None
            seat = self.read_seat(agent)
            mine = next((l for l in self._live_leases(seat) if l.get('id') == lease_id), None)
            if mine is None: return None
            items = self.wake_items(agent)
            seen = set(seat.get('woken') or [])
            keys = [k for k in mine.get('keys', []) if k in items and k not in seen]
            if not keys: return None
            mine['keys'] = keys
            seat['wakeLeases'] = self._live_leases(seat)
            write_json(self.seat_path(agent), seat)
            return {'ids': sorted({items[k]['id'] for k in keys}), 'keys': keys}

    def wake_allowed(self, seat):
        if not self.wake_enabled(): return False
        t = time.time()
        recent = [w for w in seat.get('wakes', []) if t - w < 3600]
        return len(recent) < WAKE_PER_HOUR and (not recent or t - recent[-1] >= WAKE_DEBOUNCE)

    def record_wake(self, agent, delivered=True, keys=None, **extra):
        items = self.wake_items(agent)
        keys = list(items) if keys is None else [k for k in keys if k in items]
        with self.lock:
            seat = self.read_seat(agent)
            t = time.time()
            seat['wakes'] = [w for w in seat.get('wakes', []) if t - w < 3600] + [t]
            if delivered: seat['woken'] = sorted((set(seat.get('woken', [])) | set(keys)) & set(items))
            seat.update(wokeAt=now(), **extra)
            write_json(self.seat_path(agent), seat)

    def pending(self, agent, session=None, claim=False):
        """For the Claude plugin's poll: whether to start a turn now, and for which tasks.

        `claim=True` reserves the wake atomically. The reservation is NOT consumed here: the plugin's poll is
        just a question, and consuming it before the turn exists would silently eat items the agent never saw.
        The turn itself finishes the lease through `pending --ack` (or the lease expires and retries)."""
        if not self.seat_matches(agent, session): return {'ids': [], 'wake': False}
        self.mark_alive(agent)
        ids, settled = self.pending_for(agent)
        if not claim:
            return {'ids': ids, 'wake': bool(settled and self.wake_allowed(self.read_seat(agent)))}
        # claim=True: the reservation is the single decision point — it re-checks the quiet window, the cooldown,
        # the hourly cap and the lease under the lock, so trusting a stale `settled` here would be wrong.
        reserved = self.reserve_wake(agent)
        if reserved is None:
            return {'ids': ids, 'wake': False}
        return {'ids': reserved['ids'], 'wake': True, 'lease': reserved['lease']}

    def ack_wake(self, agent, lease_id, delivered=True):
        """The poll actually started a turn (or the host refused): finish the reservation it holds."""
        return self.finish_wake(agent, lease_id, delivered=delivered)

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
        """Wake Codex at most once per batch: the reservation is atomic, the queue call is outside the lock.

        Two processes ticking together can no longer both queue the same item: the second one finds the key
        reserved and stands down. The lease is still not exactly-once for the host, which may already hold a
        queued message it cannot recall."""
        seat = self.read_seat('codex')
        thread = self.bound('codex') or seat.get('thread')
        if not thread: return None
        command = codex_command()
        if not command: return None
        reserved = self.reserve_wake('codex')
        if not reserved: return None
        lease = reserved['lease']
        checked = self.validate_wake('codex', lease)
        if not checked:
            self.finish_wake('codex', lease, delivered=False)
            return None
        ids = checked['ids']
        # Only task IDs and fixed text reach the message; the details arrive through the hook's context.
        message = WAKE_TEXT % '、'.join(ids)
        ok = False
        try:
            ran = subprocess.run(command + ['queue', '--thread', thread, '--message', message],
                                 cwd=str(self.project.root), capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
            ok = ran.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        # A failed wake still counts toward the cap, so a stale thread id cannot cause a retry storm.
        self.finish_wake('codex', lease, delivered=ok, failed=None if ok else now())
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
            if t['state'] not in ('posted', 'claimed', 'rework'): return
            t['dispatch'] = dispatch_state
            self.save(t)
            return t

    def mark_identity(self, task_id, dispatch_state, generation=None, attempt=None, requestId=None,
                      reason=None, sessionId=None):
        """Write a dispatch state only while the round identity still matches.

        A failed preflight, or any write that happens after an RPC, may land in a task that changed hands meanwhile
        (reassign / escalate / stop-ack). Writing then would erase the new receiver's round, so the whole identity
        is compared under the lock before the record is replaced."""
        with self.lock:
            t = self.load(task_id)
            if t['state'] not in ('posted', 'claimed', 'rework'): return None
            if t['to'] != 'deepseek': return None
            if generation is not None and self.dispatch_generation(t) != generation: return None
            if attempt is not None and t.get('rejects', 0) != attempt: return None
            if sessionId is not None and self.bound('deepseek') != sessionId: return None
            current = t.get('dispatch') or {}
            if requestId is not None and current.get('requestId') not in (None, requestId): return None
            # Another preflight can finish sending while this one is still checking the host.
            # Its successful/reserved/uncertain send owns the identity; a late preflight cannot reset it.
            if current.get('state') in ('sending', 'accepted', 'uncertain'): return None
            state = dict(dispatch_state)
            if reason is not None: state['reason'] = reason
            t['dispatch'] = state
            self.save(t)
            return t

    # A generation bump marks a NEW execution round: a different receiver, or a hand-over that completed.
    # Every identity derived from the generation therefore differs from anything already sent.
    def dispatch_generation(self, t):
        try: return int(t.get('dispatchGeneration') or 0)
        except (TypeError, ValueError): return 0

    def begin_execution_round(self, t):
        """Start a new execution round. Call this wherever the task changes hands or a recall completes."""
        t['dispatchGeneration'] = self.dispatch_generation(t) + 1
        t.pop('execution', None)
        t.pop('executionIssue', None)
        return t['dispatchGeneration']

    def dispatch_request(self, t, attempt):
        """The requestId for one dispatch attempt.

        The generation is part of the identity only when it is non-zero, so records written before this feature
        keep the exact requestId they already had (legacy compatibility), while every new round is distinct.
        The attempt is the rework counter: retries within one round reuse the same id on purpose, which is what
        keeps the bridge's dedupe from delivering the same prompt twice."""
        generation = self.dispatch_generation(t)
        base = '%s/agentlink/%s' % (self.project.root.as_posix(), t['id'])
        if generation <= 0:
            key = '%s/%d' % (base, attempt)                       # exact legacy derivation
        else:
            key = '%s/g%d/%d' % (base, generation, attempt)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, key))

    def dispatch(self, task_id):
        """One short pointer prompt per task attempt; uncertain sends are never retried automatically."""
        t = self.load(task_id)
        if t['to'] != 'deepseek' or t['state'] not in ('posted', 'claimed', 'rework'): return 'skipped'
        attempt = t.get('rejects', 0)
        generation = self.dispatch_generation(t)
        request = self.dispatch_request(t, attempt)
        sid = self.bound('deepseek')
        d = t.get('dispatch') or {}
        if d.get('sessionId') and d['sessionId'] != sid:
            return 'skipped'                         # a changed binding needs an explicit new execution round
        # Same round, same attempt, same identity: a duplicate call must not send twice.
        if d.get('attempt') == attempt and d.get('requestId') == request \
                and d.get('state') in ('sending', 'accepted', 'uncertain'):
            return d['state']
        if os.environ.get('AGENTLINK_NO_DISPATCH'):
            wrote = self.mark_identity(task_id, dict(state='held', attempt=attempt, reason='AGENTLINK_NO_DISPATCH',
                                       generation=generation, requestId=request, sessionId=sid),
                                       generation=generation, attempt=attempt, requestId=request, sessionId=sid)
            return 'held' if wrote else self.dispatch_outcome(task_id, generation, attempt, sid)
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
            wrote = self.mark_identity(task_id, dict(state='pending', attempt=attempt, generation=generation,
                                       requestId=request, sessionId=sid),
                               generation=generation, attempt=attempt, requestId=request,
                               sessionId=sid, reason=str(error) if isinstance(error, ValueError) else type(error).__name__)
            return 'pending' if wrote else self.dispatch_outcome(task_id, generation, attempt, sid)
        with self.lock:
            t = self.load(task_id)
            if t['to'] != 'deepseek' or t['state'] not in ('posted', 'claimed', 'rework'): return 'skipped'
            # A hand-over could have happened during the preflight: the round, receiver and attempt must all match.
            if self.dispatch_generation(t) != generation or t.get('rejects', 0) != attempt or self.bound('deepseek') != sid:
                return 'skipped'
            current = t.get('dispatch') or {}
            if current.get('attempt') == attempt and current.get('requestId') == request \
                    and current.get('state') in ('sending', 'accepted', 'uncertain'):
                return current['state']                  # another process won the race
            t['dispatch'] = {'state': 'sending', 'attempt': attempt, 'requestId': request,
                             'generation': generation, 'to': 'deepseek', 'sessionId': sid, 'at': now()}
            self.save(t)
        # Keep the pointer short: the full protocol (commands, guide, receipt format) arrives through `show`
        # and AGENTLINK.md, so the CLI path is spelled out once instead of in every command.
        prompt = ('AgentLink 任务 %s（%s 派发，%s）：%s show %s --as deepseek 读规格（含实现指引）并 claim；'
                  '只改允许路径，可拆给子智能体后汇总成一份回执；完成后 deliver --as deepseek --summary/--checks/--evidence，'
                  '缺文件/依赖/权限或无法完成必须 block --note "原因" --missing "缺项" --attempts "已尝试" '
                  '--help-needed "需总控帮助" 上报总控，不能只留言结束或伪装交付；回执写进命令，会话里只回一行。') % (task_id, t['from'], t['level'], cli(), task_id)
        blocker = t.get('blocker') or {}
        if blocker.get('status') == 'resolved': prompt += '\n总控处置及续跑指引：' + (blocker.get('resolution') or '')[:1200]
        try:
            # Binding is independent of the task lock. Check again immediately before the host call.
            if self.bound('deepseek') != sid:
                with self.lock:
                    current = self.load(task_id)
                    saved = current.get('dispatch') or {}
                    if saved.get('requestId') == request and saved.get('state') == 'sending' \
                            and self.dispatch_generation(current) == generation and saved.get('sessionId') == sid:
                        saved.update(state='pending', reason='发送前绑定已变更，请重新交接任务')
                        self.save(current)
                return 'skipped'
            r = c.call('session.prompt', {'sessionId': sid, 'requestId': request, 'mode': 'queue',
                                          'content': [{'type': 'text', 'text': prompt}],
                                          'clientTimeZone': self.project.option('timeZone')})
            state = 'accepted' if r.get('accepted') else 'uncertain'
        except Exception:
            state = 'uncertain'
        with self.lock:
            t = self.load(task_id)
            current = t.get('dispatch') or {}
            if t['state'] not in ('posted', 'claimed', 'rework') or current.get('requestId') != request \
                    or current.get('generation') != generation or self.dispatch_generation(t) != generation \
                    or t['to'] != 'deepseek' or t.get('rejects', 0) != attempt or current.get('sessionId') != sid:
                return 'skipped'                         # a stale answer for a superseded round
            if self.bound('deepseek') != sid:
                current.update(state='uncertain', reason='发送期间绑定已变更，请确认旧会话停止后重新交接任务')
                self.emit(t, 'agentlink', 'dispatch', 'uncertain: binding changed'); self.save(t)
                return 'skipped'
            t['dispatch']['state'] = state
            self.emit(t, 'agentlink', 'dispatch', state); self.save(t)
        return state

    def dispatch_outcome(self, task_id, generation, attempt, session_id):
        """Report a competing sender's result, never the stale preflight's attempted write."""
        t = self.load(task_id)
        if t['to'] != 'deepseek' or t['state'] not in ('posted', 'claimed', 'rework') \
                or self.dispatch_generation(t) != generation or t.get('rejects', 0) != attempt \
                or self.bound('deepseek') != session_id:
            return 'skipped'
        return (t.get('dispatch') or {}).get('state', 'skipped')

    def release(self, task_id):
        """Explicitly release a held task, retry an uncertain dispatch, or retry an AI review send.

        A manual retry is not a bypass: the reset refuses to touch a started review or a live send lease, and
        the resend goes through the same claim, the same requestId and the same attempt budget."""
        t = self.load(task_id)
        if t['state'] in TERMINAL or t['state'] in ('paused', 'recalling'): raise ValueError('任务已结束、暂停或等待停止确认，不能投递')
        if t['state'] == 'ai_review':
            r = t.get('aiReview') or {}
            if r.get('status') in ('failed', 'uncertain', 'stalled'):
                self._ai_review_reset(task_id, 'manual retry')     # clears the stall, only when no lease/running
            status = self.ai_review_send(task_id, manual=True)
            return {'id': task_id, 'aiReview': status,
                    'aiReviewStatus': (self.load(task_id).get('aiReview') or {}).get('status')}
        with self.lock:
            current = self.load(task_id)
            if current['state'] in TERMINAL or current['state'] in ('paused', 'recalling'):
                raise ValueError('任务已结束、暂停或等待停止确认，不能投递')
            if current['to'] != t['to'] or self.dispatch_generation(current) != self.dispatch_generation(t) \
                    or current.get('rejects', 0) != t.get('rejects', 0):
                return {'id': task_id, 'dispatch': 'skipped'}
            d = current.get('dispatch') or {}
            if current['to'] == 'deepseek' and d.get('state') in ('held', 'uncertain'):
                d.update(state='pending', reason='released')
                current['dispatch'] = d
                self.save(current)
        return {'id': task_id, 'dispatch': self.dispatch(task_id)}

    def pump(self):
        """Retry pending DeepSeek dispatches, nudge stalled AI reviews and flag stalls. Cheap."""
        try: self.unclaimed_fallback()
        except Exception: pass
        for t in self.all():
            if t['state'] == 'recalling':
                try: self.notify_recall(t['id'])
                except Exception: pass
                continue
            if t['state'] == 'delivered':
                try: self.pump_final_review(t['id'])
                except Exception: pass
                continue
            if t['state'] == 'ai_review':
                try: self.pump_ai_review(t)
                except Exception: continue
                continue
            if t['to'] == 'claude' and t['state'] == 'claimed':
                try: self.pump_execution(t)
                except Exception: pass
                continue
            if t['to'] != 'deepseek' or t['state'] not in ('posted', 'claimed', 'rework'): continue
            try: self.pump_one(t)
            except Exception: continue           # one unreachable task must not starve the rest

    def pump_ai_review(self, t):
        """Resume an AI review that a crash, a lost queue entry or a slow reviewer left behind.

        Every branch is bounded: `sent` without a `started` confirmation waits AI_START_TIMEOUT, the resend
        reuses the same requestId under a lease, and after AI_RETRY_LIMIT the task is marked stalled for a
        human instead of being retried forever or accepted silently."""
        r = t.get('aiReview') or {}
        if r.get('status') in ('accepted', 'rejected'): return
        if r.get('attempt') != t.get('rejects', 0): return          # an old attempt's review never runs again
        status = r.get('status')
        attempt = r.get('attempt')
        if status == 'started':
            if not r.get('stalled') and self._ai_review_age(r, 'startedAt') > AI_STALL_SECONDS:
                self._ai_review_stall(t['id'], attempt, '审查已开始但 %d 分钟无结论' % (AI_STALL_SECONDS // 60))
            return
        if status == 'stalled':
            if not r.get('stalled'):                                  # legacy record missing the flag
                self._ai_review_stall(t['id'], attempt, '重投无进展，交总控处理')
            return
        if status == 'sending' and not self._ai_lease_free(r): return  # another process is sending
        if status == 'sent' and self._ai_review_age(r, 'sentAt') < AI_START_TIMEOUT: return
        if status in ('failed', 'uncertain') and self._ai_backoff_waiting(r): return
        if int(r.get('attempts') or 0) >= int(r.get('sendLimit') or AI_RETRY_LIMIT):
            self._ai_review_stall(t['id'], attempt, '重投 %d 次仍无进展' % int(r.get('attempts') or 0))
            return
        self.ai_review_send(t['id'])

    def pump_one(self, t):
        d = t.get('dispatch') or {}
        if d.get('state') == 'held': return
        stale = d.get('attempt') != t.get('rejects', 0) and t['state'] in ('posted', 'rework')
        if d.get('state') == 'pending' or (not d and t['state'] in ('posted', 'rework')) or stale:
            self.dispatch(t['id'])
        if t['state'] == 'claimed' or d.get('state') == 'accepted': self.pump_execution(t)

    def pump_execution(self, t):
        """Notice an ended/idle executor without inferring its reason, completion or stopped tools.

        Host reads run outside the lock; the identity, state and execution timestamps are compared again
        before recording an issue, so a late observation cannot overwrite a new claim/delivery/round.
        """
        if t['state'] not in ('posted', 'claimed', 'rework') or self.execution_attention(t): return
        agent = t['to']
        if agent not in ('deepseek', 'claude'): return
        execution, dispatch = t.get('execution') or {}, t.get('dispatch') or {}
        generation, attempt = self.dispatch_generation(t), t.get('rejects', 0)
        if execution and execution.get('generation') != generation: return
        if agent == 'deepseek':
            if dispatch.get('state') != 'accepted' and not execution: return
            sid = execution.get('sessionId') or dispatch.get('sessionId')
            # Legacy dispatch records without a captured session cannot be tied to the current binding.
            if not sid: return
        else:
            if t['state'] != 'claimed' or not execution: return
            sid = execution.get('sessionId')
        if not sid or self.bound(agent) != sid: return
        since = execution.get('startedAt') or dispatch.get('at')
        stamp = self._ts(since)
        if not stamp or time.time() - stamp < STALL_SECONDS: return
        try:
            if agent == 'deepseek':
                from . import harness
                item = harness.find_session(harness.Client(), sid)
                ended = isinstance(item, dict) and item.get('running') is False
            else:
                from .sessions import claude_execution
                ended = claude_execution(str(self.project.root), sid, since) is False
        except Exception:
            return  # disconnected/unknown does not mean idle
        if not ended: return
        with self.lock:
            current = self.load(t['id'])
            if (current['state'] != t['state'] or current['to'] != agent or
                    self.dispatch_generation(current) != generation or current.get('rejects', 0) != attempt or
                    self.bound(agent) != sid or current.get('execution') != t.get('execution') or
                    current.get('dispatch') != t.get('dispatch') or self.execution_attention(current)):
                return
            note = ('DeepSeek 会话当前空闲' if agent == 'deepseek' else 'Claude 转录显示本轮已结束') + '，但任务没有交付或受阻回执；由总控核查'
            current['executionIssue'] = {'status': 'unreported', 'agent': agent, 'sessionId': sid,
                                         'generation': generation, 'at': now(), 'note': note}
            if agent == 'deepseek' and current.get('dispatch'): current['dispatch']['stalled'] = True
            self.emit(current, 'agentlink', 'stalled', note)
            self.save(current)

    def deepseek_running(self):
        from . import harness
        item = harness.find_session(harness.Client(), self.bound('deepseek'))
        return item.get('running') if isinstance(item, dict) and isinstance(item.get('running'), bool) else None

    def watcher_alive(self):
        try: return time.time() - (self.base / 'harness-watcher.json').stat().st_mtime < 30
        except FileNotFoundError: return False

    def heartbeat(self, **extra):
        write_json(self.base / 'harness-watcher.json', dict({'pid': os.getpid(), 't': now()}, **extra))

    def wait(self, task_id, timeout=3600):
        """Block without model calls until the task leaves the working states (zero tokens while waiting)."""
        started = time.monotonic()
        deadline = started + max(0, timeout)
        while True:
            t = self.load(task_id)
            out = {'id': t['id'], 'state': t['state'], 'timedOut': False,
                   'waitedSeconds': round(time.monotonic() - started, 2)}
            if t['state'] not in ('posted', 'claimed', 'rework', 'ai_review', 'recalling'): return out
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                out['timedOut'] = True
                return out
            time.sleep(min(2, remaining))


def codex_command():
    if os.environ.get('AGENTLINK_CODEX_CMD'): return json.loads(os.environ['AGENTLINK_CODEX_CMD'])
    found = user_home() / '.codex' / 'plugins' / '.plugin-appserver' / 'codex.exe'
    if found.exists(): return [str(found)]
    which = shutil.which('codex')
    return [which] if which else None
