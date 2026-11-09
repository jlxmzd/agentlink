"""Command line: the bus commands agents run, the hooks, and the panel server."""
import argparse
import json
import os
import sys

from . import project as projects_mod
from .bus import ACTORS, AGENTS, LEVELS
from .util import agentlink_home, now


def resolve(a):
    """The project from --project, else the configured project containing --cwd / the working directory."""
    if getattr(a, 'project', None):
        p = projects_mod.Project(a.project)
        if not p.exists(): raise ValueError('Not an AgentLink project (bind it in the panel first): %s' % p.root)
        return p
    p = projects_mod.find(getattr(a, 'cwd', None) or os.getcwd())
    if p is None: raise ValueError('No AgentLink project here; bind one in the panel (agentlink.py panel)')
    return p


def hook_payload():
    """The host hook's JSON payload when stdin delivers one within a second; {} otherwise."""
    if sys.stdin is None or sys.stdin.isatty(): return {}
    import threading
    box = []
    reader = threading.Thread(target=lambda: box.append(sys.stdin.read()), daemon=True)
    reader.start()
    reader.join(1.0)                                     # some hosts never close stdin; do not hang on them
    try:
        value = json.loads(box[0]) if box and box[0].strip() else {}
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def cmd_hook(a):
    # Hooks are installed globally: stay silent outside AgentLink projects and in unbound conversations.
    payload = hook_payload() if a.agent == 'codex' else {}
    cwd = a.cwd or payload.get('cwd') or os.getcwd()
    session = a.session or (os.environ.get('CODEX_THREAD_ID') or payload.get('session_id') if a.agent == 'codex' else None)
    p = projects_mod.find(cwd)
    if p is None: return None
    bus = p.bus()
    if not p.binding(a.agent) and a.agent == 'deepseek': return None
    try: bus.register_seat(a.agent, session)
    except Exception: pass
    if not bus.seat_matches(a.agent, session): return None
    try:
        if not bus.watcher_alive(): bus.pump()           # the Harness watcher owns dispatch while it runs
    except Exception: pass
    text = bus.brief(a.agent, a.event)
    if not text: return None
    if a.plain: return text
    return {'hookSpecificOutput': {'hookEventName': a.event, 'additionalContext': text}}


def cmd_pump_all(a):
    """Called by the Harness watcher every 15 s: dispatch, stall checks and Codex wakes for every project.
    One run at a time: an overlapping tick skips."""
    from .util import FileLock
    try:
        with FileLock(agentlink_home() / 'pump.lock', timeout=0.1, stale=120):
            return pump_projects()
    except ValueError:
        return None                                      # the other runner is on this tick


def pump_projects():
    for p in projects_mod.projects():
        if not p.exists(): continue
        beat = {}
        try:
            bus = p.bus()
            bus.pump()
        except Exception as error:   # report the class only; transport errors may carry connection details
            beat['error'] = str(error) if isinstance(error, ValueError) else type(error).__name__
            bus = None
        if bus:
            beat['codexWake'] = bus.wake_all()
            bus.heartbeat(**beat)
    return None


def cmd_panel(a):
    from .server import serve
    serve(a.port, open_browser=not a.no_browser)


def run(a):
    if a.cmd == 'hook': return cmd_hook(a)
    if a.cmd == 'pump-all': return cmd_pump_all(a)
    if a.cmd == 'panel': return cmd_panel(a)
    if a.cmd == 'projects':
        return [p.summary() for p in projects_mod.projects()]
    if a.cmd == 'sessions':
        from .sessions import discover
        return discover(a.cwd or os.getcwd(), a.all)
    # Claude's plugin polls from every directory: outside an AgentLink project this is a quiet no-op, not
    # an error worth a hook-errors.log line every 30 s.
    if a.cmd == 'pending' and not getattr(a, 'project', None) \
            and projects_mod.find(getattr(a, 'cwd', None) or os.getcwd()) is None:
        return {'ids': [], 'wake': False}
    p = resolve(a)
    bus = p.bus()
    if a.cmd == 'post':
        spec = open(a.spec_file, encoding='utf-8-sig').read() if a.spec_file else (a.spec or '')
        guide = open(a.guide_file, encoding='utf-8-sig').read() if a.guide_file else (a.guide or '')
        return bus.post(a.agent, a.to, a.title, spec, a.level, a.paths, a.build, a.force, a.no_dispatch, guide, a.check)
    if a.cmd == 'claim': return bus.claim(a.agent, a.id)
    if a.cmd == 'deliver': return bus.deliver(a.agent, a.id, a.summary, a.checks, a.evidence, a.limits)
    if a.cmd in ('accept', 'reject', 'cancel'):
        return bus.review(a.agent, a.id, a.cmd, a.note, getattr(a, 'no_dispatch', False), getattr(a, 'kind', 'code'))
    if a.cmd == 'block':
        return bus.block(a.agent, a.id, a.note, a.missing, a.attempts, a.help_needed, a.evidence)
    if a.cmd == 'unblock': return bus.unblock(a.agent, a.id, a.note, a.no_dispatch)
    if a.cmd in ('pause', 'resume'): return bus.control(a.agent, a.id, a.cmd, a.note)
    if a.cmd in ('takeover', 'remind'): return bus.review_attention(a.agent, a.id, a.cmd, a.note)
    if a.cmd == 'reassign': return bus.reassign(a.agent, a.id, a.to, a.note, a.no_dispatch)
    if a.cmd == 'stop-ack': return bus.acknowledge_stop(a.agent, a.id, a.token, a.note, a.no_dispatch)
    if a.cmd == 'show': return bus.show(a.id, a.full)
    if a.cmd == 'list': return bus.listing(a.agent, a.all, a.full)
    if a.cmd == 'locks': return bus.locks()
    if a.cmd == 'dispatch': return bus.release(a.id)
    if a.cmd == 'wait': return bus.wait(a.id, a.timeout)
    if a.cmd == 'pump':
        bus.pump(); bus.heartbeat(codexWake=bus.wake_all()); return None
    if a.cmd == 'pending': return bus.pending(a.agent, a.session, a.claim)
    if a.cmd == 'ack-wake': return bus.ack_wake(a.agent, a.lease, not a.failed)
    if a.cmd == 'reviewer':
        return bus.ai_review(a.agent, a.task, a.attempt, a.token, a.verdict, a.note, a.kind, a.no_dispatch)
    if a.cmd == 'wake':
        if a.mode in ('on', 'off'): bus.set_wake(a.mode == 'on')
        return bus.wake_status()
    raise ValueError('Unknown command ' + a.cmd)


def parser():
    p = argparse.ArgumentParser(prog='agentlink', description='AgentLink: Claude Code · Codex · DeepSeek collaboration')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--as', dest='agent', choices=ACTORS, default=argparse.SUPPRESS)
    common.add_argument('--project', default=argparse.SUPPRESS, help='project root (default: the one containing --cwd)')
    common.add_argument('--cwd', default=argparse.SUPPRESS)
    p.add_argument('--as', dest='agent', choices=ACTORS, default=os.environ.get('AGENTLINK_AGENT'))
    p.add_argument('--project', default=None)
    p.add_argument('--cwd', default=None)
    sub = p.add_subparsers(dest='cmd', required=True)

    def add(name, *args):
        s = sub.add_parser(name, parents=[common])
        for spec in args: s.add_argument(*spec[0], **spec[1])
        return s

    ident = (['id'], {})
    nodisp = (['--no-dispatch'], {'action': 'store_true'})
    add('post', (['--to'], {'choices': AGENTS, 'required': True}), (['--title'], {'required': True}),
        (['--level'], {'choices': LEVELS, 'default': 'L1'}), (['--paths'], {'nargs': '*', 'default': []}),
        (['--spec'], {}), (['--spec-file'], {}), (['--build'], {'action': 'store_true'}),
        (['--force'], {'action': 'store_true'}), (['--guide'], {}), (['--guide-file'], {}),
        (['--check'], {'action': 'append', 'default': [],
                       'help': 'acceptance command the program runs at deliver, in the project root (repeatable)'}),
        nodisp)
    add('claim', ident)
    add('deliver', ident, (['--summary'], {'required': True}), (['--checks'], {'default': ''}),
        (['--evidence'], {'nargs': '*', 'default': []}), (['--limits'], {'default': ''}))
    add('accept', ident, (['--note'], {'default': ''}))
    add('reject', ident, (['--note'], {'required': True}), (['--kind'], {'choices': ('code', 'design'), 'default': 'code'}), nodisp)
    add('cancel', ident, (['--note'], {'default': ''}))
    for action in ('pause', 'resume', 'takeover', 'remind'):
        add(action, ident, (['--note'], {'default': ''}))
    add('block', ident, (['--note'], {'required': True}), (['--missing'], {'default': ''}),
        (['--attempts'], {'default': ''}), (['--help-needed'], {'default': ''}),
        (['--evidence'], {'nargs': '*', 'default': []}))
    add('unblock', ident, (['--note'], {'required': True}), nodisp)
    add('reassign', ident, (['--to'], {'choices': ACTORS, 'required': True}), (['--note'], {'default': ''}), nodisp)
    add('stop-ack', ident, (['--token'], {'required': True}), (['--note'], {'required': True}), nodisp)
    add('show', ident, (['--full'], {'action': 'store_true'}))
    add('list', (['--all'], {'action': 'store_true'}),
        (['--full'], {'action': 'store_true', 'help': 'also show level and direction (longer output)'}))
    add('locks')
    add('dispatch', ident)
    add('wait', ident, (['--timeout'], {'type': int, 'default': 3600}))
    add('hook', (['--event'], {'default': 'UserPromptSubmit'}), (['--plain'], {'action': 'store_true'}),
        (['--session'], {'default': None}))
    add('pump')
    add('pump-all')
    add('pending', (['--claim'], {'action': 'store_true'}), (['--session'], {'default': None}))
    # 预留唤醒之后由真正开始的那一轮结束它；失败也走这里，计入冷却而不是假装成功。
    add('ack-wake', (['--lease'], {'required': True}), (['--failed'], {'action': 'store_true'}))
    add('reviewer', (['--task'], {'required': True}), (['--attempt'], {'type': int, 'required': True}),
        (['--token'], {'required': True}),
        (['--verdict'], {'choices': ('started', 'accept', 'reject'), 'required': True,
                         'help': 'started = reviewer began; accept/reject = the verdict after it began'}),
        (['--note'], {'default': ''}), (['--kind'], {'choices': ('code', 'design'), 'default': 'code'}), nodisp)
    add('wake', (['mode'], {'nargs': '?', 'choices': ['on', 'off', 'status'], 'default': 'status'}))
    add('projects')
    add('sessions', (['--all'], {'action': 'store_true'}))
    add('panel', (['--port'], {'type': int, 'default': 8765}), (['--no-browser'], {'action': 'store_true'}))
    return p


ANONYMOUS = {'list', 'locks', 'show', 'wait', 'pump', 'pump-all', 'wake', 'projects', 'sessions', 'panel', 'dispatch'}
MUTATING = {'post', 'deliver', 'accept', 'reject', 'cancel', 'block', 'unblock', 'reassign', 'reviewer',
            'pause', 'resume', 'takeover', 'remind', 'stop-ack'}


def log_hook_error(agent, error):
    try:
        path = agentlink_home() / 'hook-errors.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as f: f.write('%s %s %s\n' % (now(), agent, type(error).__name__))
    except OSError: pass


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.agent and args.cmd not in ANONYMOUS: raise ValueError('Identify yourself with --as or AGENTLINK_AGENT')
    if args.cmd in ('hook', 'pending', 'pump-all'):
        # A hook must never break the host agent's prompt: failures are logged, output stays empty.
        try: result = run(args)
        except Exception as error:
            log_hook_error(args.agent, error)
            result = {'ids': [], 'wake': False} if args.cmd == 'pending' else None
    else:
        result = run(args)
        if args.agent == 'deepseek' and args.cmd in MUTATING and isinstance(result, dict):
            bus = resolve(args).bus()
            rules = bus.rules_block('deepseek', 'CommandResult', bus.read_seat('deepseek'), True)
            if rules: result['rulesReminder'] = rules
        if args.cmd in MUTATING:
            try: resolve(args).bus().wake_all()          # wake the other side now, not at the next watcher tick
            except Exception: pass
    if result is None: return 0
    print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=None))
    return 0


def entry():
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding='utf-8')
        except AttributeError: pass
    try: sys.exit(main())
    except ValueError as error:
        print('agentlink: ' + str(error), file=sys.stderr)
        sys.exit(2)
