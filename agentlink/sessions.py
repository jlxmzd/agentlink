"""Discover the conversations each host has, so the panel can offer them for binding.

Only metadata is read (id, title, working directory, last activity) and it stays on this machine:
  Claude Code  ~/.claude/projects/<encoded cwd>/<sessionId>.jsonl  (custom-title / agent-name / last-prompt rows)
  Codex        ~/.codex/session_index.jsonl (id, thread_name, updated_at) + rollout session_meta for the cwd
  DeepSeek     the Harness session.list call through the loopback bridge
"""
import datetime
import json
import os
from pathlib import Path
import re

from .project import contains, key
from .util import user_home


def iso(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec='seconds')


def claude_dir_name(path):
    return re.sub(r'[^A-Za-z0-9]', '-', os.path.abspath(str(path)))


def claude_execution(cwd, sid, since):
    """True=unfinished turn, False=explicit end after claim, None=unknown.

    Only inspect the bound session's transcript metadata. Never classify from message wording, file age,
    missing files or a transport error. A later user/tool-use row supersedes an earlier end_turn.
    """
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', sid or ''): return None
    try: started = datetime.datetime.fromisoformat(since).timestamp()
    except (TypeError, ValueError): return None
    base = user_home() / '.claude' / 'projects'
    wanted = claude_dir_name(cwd)
    if not base.is_dir(): return None
    files = [d / (sid + '.jsonl') for d in base.iterdir()
             if d.is_dir() and (d.name == wanted or d.name.startswith(wanted + '-')) and (d / (sid + '.jsonl')).is_file()]
    if len(files) != 1: return None
    status = None
    try:
        with files[0].open(encoding='utf-8') as stream:
            for line in stream:
                if not line.strip(): continue
                row = json.loads(line)
                if not isinstance(row, dict): return None
                if row.get('isSidechain'): continue
                if row.get('type') not in ('user', 'assistant'): continue
                if row.get('sessionId') and row['sessionId'] != sid: continue
                if row.get('cwd') and not contains(cwd, row['cwd']): return None
                try: stamp = datetime.datetime.fromisoformat(str(row.get('timestamp') or '').replace('Z', '+00:00')).timestamp()
                except (TypeError, ValueError): return None
                if stamp < started: continue
                if row['type'] == 'user':
                    status = True
                else:
                    message = row.get('message') or {}
                    if not isinstance(message, dict): return None
                    stop = message.get('stop_reason')
                    if stop in ('end_turn', 'stop_sequence'): status = False
                    elif stop in (None, 'tool_use'): status = True
                    else: status = None
    except (OSError, ValueError): return None
    return status


def claude_sessions(cwd, include_all=False, limit=40):
    base = user_home() / '.claude' / 'projects'
    if not base.is_dir(): return []
    if include_all: dirs = [d for d in base.iterdir() if d.is_dir()]
    else:
        wanted = claude_dir_name(cwd)
        # Sessions started in a subdirectory of the project belong to it too.
        dirs = [d for d in base.iterdir() if d.is_dir() and (d.name == wanted or d.name.startswith(wanted + '-'))]
    files = sorted((f for d in dirs for f in d.glob('*.jsonl')), key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        title, prompt, where = '', '', None
        try:
            with f.open(encoding='utf-8', errors='replace') as fh:
                for ln in fh:
                    # Cheap substring checks first: transcripts can be large.
                    if where is None and '"cwd"' in ln:
                        try: where = json.loads(ln).get('cwd')
                        except ValueError: pass
                    if '"custom-title"' in ln or '"agent-name"' in ln or '"last-prompt"' in ln:
                        try: d = json.loads(ln)
                        except ValueError: continue
                        if d.get('type') == 'custom-title': title = d.get('customTitle') or title
                        elif d.get('type') == 'agent-name' and not title: title = d.get('agentName') or ''
                        elif d.get('type') == 'last-prompt': prompt = d.get('lastPrompt') or prompt
        except OSError:
            continue
        if not include_all and where and not contains(cwd, where): continue
        out.append({'id': f.stem, 'title': (title or prompt or '（无标题）')[:80], 'cwd': where,
                    'updated': iso(f.stat().st_mtime), 'preview': prompt[:120]})
    return out


def codex_sessions(cwd, include_all=False, limit=40):
    home = user_home() / '.codex'
    index = home / 'session_index.jsonl'
    names = {}
    if index.exists():
        for ln in index.read_text(encoding='utf-8', errors='replace').splitlines():
            try: d = json.loads(ln)
            except ValueError: continue
            if d.get('id'): names[d['id']] = d
    archived = set()
    for f in (home / 'archived_sessions').glob('**/*.jsonl') if (home / 'archived_sessions').is_dir() else []:
        m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$', f.name)
        if m: archived.add(m.group(1))
    files = sorted((home / 'sessions').glob('**/rollout-*.jsonl') if (home / 'sessions').is_dir() else [],
                   key=lambda f: f.stat().st_mtime, reverse=True)
    out, seen = [], set()
    for f in files:
        if len(out) >= limit: break
        try:
            with f.open(encoding='utf-8', errors='replace') as fh: meta = json.loads(fh.readline())
        except (OSError, ValueError):
            continue
        p = meta.get('payload') if meta.get('type') == 'session_meta' else None
        if not isinstance(p, dict): continue
        sid = p.get('id') or p.get('session_id')
        if not sid or sid in seen or sid in archived: continue
        seen.add(sid)
        where = p.get('cwd')
        if not include_all and not (where and contains(cwd, where)): continue
        n = names.get(sid, {})
        out.append({'id': sid, 'title': (n.get('thread_name') or '（无标题）')[:80], 'cwd': where,
                    'updated': n.get('updated_at') or iso(f.stat().st_mtime)})
    return out


def deepseek_sessions(cwd, include_all=False):
    """The DeepSeek conversations the Harness UI itself lists for this project, newest first.

    Membership follows the UI, which is the workspace store and not `parentSessionId`:
    `workspace.json` → `tables.workspaces[*].sessionIds`. Three rules keep it aligned with what the UI
    actually shows, and each has its own test:

    * A session the current project's workspace lists explicitly is always shown, whatever its `cwd`
      (moving a session to another directory must not make it vanish from its own project).
    * A child session (a fork) is shown only when some workspace lists it. An automatic sub-session that
      no workspace accounts for is internal and never appears — not even where no workspace matches.
    * A stub that never held a turn is hidden, unless its workspace lists it (the UI shows titled empty
      conversations there).

    Archived sessions never appear, show-all widens the directory range only, and unreadable metadata is
    reported instead of silently widening the list. The RPC returns every session at once (no cursor), so
    the list is not truncated: a conversation that exists must stay bindable."""
    from . import harness
    c = harness.Client()
    try: meta = harness.workspace_meta()
    except Exception as error:
        # Fail closed: without workspace metadata every child session would come back and archived ones with it.
        raise ValueError('无法读取 Harness 工作区元数据：%s' % (error if isinstance(error, ValueError) else type(error).__name__))
    archived, members = meta['archived'], harness.workspace_members(meta)
    explicit_anywhere = set(members)                       # every session any workspace accounts for
    # Parent/child rule: the deepest workspace whose path *contains* this project owns it. A project nested
    # inside another project therefore uses its own workspace, not the ancestor's.
    owning = [w for w in meta['workspaces'] if w['path'] and contains(w['path'], cwd)]
    primary = max(owning, key=lambda w: len(w['path'])) if owning else None
    explicit_here = set(primary['sessions']) if primary is not None else set()
    rows = []
    for item in c.call('session.list', {})['items']:
        sid = item.get('sessionId')
        if not sid or sid in archived: continue
        in_project = bool(item.get('cwd')) and contains(cwd, item['cwd'])
        exact = bool(item.get('cwd')) and key(item['cwd']) == key(cwd)
        member_here, member_any = sid in explicit_here, sid in explicit_anywhere
        stub = (item.get('projections', {}).get('values', {}) or {}).get('sessionListMetadata') or {}
        empty_stub = bool(stub.get('blank')) and not stub.get('lastPromptAt')
        if include_all:
            # Every project: a workspace member anywhere, or a plain top-level conversation; never a stray child.
            if not (member_any or not harness.child_session(item)) or (empty_stub and not member_any): continue
        else:
            # This project only. A session filed under a workspace that owns this directory stays. A session
            # that merely lives somewhere below is taken only when its own directory is this one — a nested
            # sub-project's conversations belong to that sub-project, not to its parent.
            if not (member_here or in_project): continue
            if not member_here:
                if not exact: continue
                if harness.child_session(item) and not member_any: continue
                if empty_stub and not member_any: continue
        rows.append(harness.compact(item, archived, members))
    rows.sort(key=lambda row: (row.get('promptAt') or 0, row.get('updated') or ''), reverse=True)
    out, seen = [], set()
    for row in rows:                                       # de-duplicate; the newest record wins
        if row['id'] in seen: continue
        seen.add(row['id']); out.append(row)
    return out


def discover(cwd, include_all=False):
    """Sessions per agent; a host that cannot be read reports an error instead of failing the rest."""
    out = {}
    for agent, fn in (('claude', claude_sessions), ('codex', codex_sessions), ('deepseek', deepseek_sessions)):
        try: out[agent] = {'items': fn(cwd, include_all)}
        except Exception as error:
            out[agent] = {'items': [], 'error': str(error) if isinstance(error, ValueError) else type(error).__name__}
    return out
