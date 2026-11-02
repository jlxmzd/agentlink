"""Program checks that stand in for an AI reviewer: what changed, whether it stayed in scope, and whether the
task's acceptance commands pass. They run inside the delivering agent's own `deliver` call, so a failure goes
back to DeepSeek (whose tokens are cheap) instead of waking Claude or Codex.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import time

from .util import read_json, write_json

IGNORED = ('.agentlink/', '.git/')
LOG_TAIL = 1500
INLINE_DIFF = 600        # a bigger diff is fetched with git only when a reviewer actually needs it


def git(root, *args, timeout=30):
    ran = subprocess.run(['git', '-c', 'core.quotepath=off', *args], cwd=str(root), capture_output=True,
                         timeout=timeout, stdin=subprocess.DEVNULL)
    if ran.returncode != 0: raise OSError(ran.stderr.decode('utf-8', 'replace').strip()[:200])
    return ran.stdout.decode('utf-8', 'replace')


def is_repo(root):
    try: return git(root, 'rev-parse', '--is-inside-work-tree').strip() == 'true'
    except (OSError, subprocess.SubprocessError): return False


def dirty(root):
    """Project-relative paths that differ from HEAD (modified, staged, untracked, deleted)."""
    out = set()
    for line in git(root, 'status', '--porcelain', '-z', '--untracked-files=all').split('\0'):
        if len(line) > 3:
            path = line[3:].replace('\\', '/')
            if not path.startswith(IGNORED): out.add(path)
    return out


def digest(root, path):
    p = Path(root) / path
    try: return hashlib.sha1(p.read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError): return 'missing'
    except OSError: return 'unreadable'


def snapshot(root):
    """Hashes of every dirty file, taken when work starts, so later changes can be told apart from earlier ones."""
    if not is_repo(root): return None
    return {p: digest(root, p) for p in sorted(dirty(root))}


def changed_since(root, snap):
    """Files the work touched: dirty now and new or different since the snapshot."""
    return sorted(p for p in dirty(root) if snap.get(p) != digest(root, p))


def inside(path, allowed):
    return any(path == a or path.startswith(a.rstrip('/') + '/') for a in allowed)


def stat(root, files):
    """`+added -removed` per file; untracked files count as all added."""
    rows = {}
    try:
        for line in git(root, 'diff', 'HEAD', '--numstat', '--', *files).splitlines():
            parts = line.split('\t')
            if len(parts) == 3: rows[parts[2]] = (parts[0], parts[1])
    except (OSError, subprocess.SubprocessError):
        pass
    out = []
    for f in files:
        if f in rows: out.append('%s +%s -%s' % (f, rows[f][0], rows[f][1]))
        else:
            try: n = sum(1 for _ in (Path(root) / f).open(encoding='utf-8', errors='replace'))
            except OSError: n = 0
            out.append('%s +%d（新文件）' % (f, n) if n else '%s（已删除或二进制）' % f)
    return out


def diff_summary(root, files):
    """(inline diff or None, total characters). Only a small diff is worth its context; a large one is
    cheaper to fetch on demand than to inline into every reviewer prompt."""
    try: text = git(root, 'diff', 'HEAD', '--unified=2', '--', *files)
    except (OSError, subprocess.SubprocessError): return None, 0
    if not text: return None, 0
    return (text if len(text) <= INLINE_DIFF else None), len(text)


def small_diff(root, files):
    """The diff itself when it is short enough to read inline; None otherwise."""
    return diff_summary(root, files)[0]


def run_checks(root, commands, log_path, timeout):
    """Run each acceptance command in the project root; the log keeps everything, the result the tail."""
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('w', encoding='utf-8') as log:
        for command in commands:
            log.write('$ %s\n' % command); log.flush()
            try:
                # The command was written by the agent or the person who posted the task, the same as one they
                # would type into their own shell in this project.
                ran = subprocess.run(command, shell=True, cwd=str(root), capture_output=True, timeout=timeout,
                                     stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONIOENCODING='utf-8'))
                text = (ran.stdout + ran.stderr).decode('utf-8', 'replace')
                code = ran.returncode
            except subprocess.TimeoutExpired:
                text, code = '（超时 %d 秒）' % timeout, -1
            log.write(text + '\n[exit %d]\n' % code)
            if code != 0:
                return {'ok': False, 'failed': command, 'exit': code, 'tail': text[-LOG_TAIL:],
                        'seconds': round(time.monotonic() - started)}
    return {'ok': True, 'seconds': round(time.monotonic() - started)}


def save_snapshot(path, snap):
    if snap is not None: write_json(path, snap)


def load_snapshot(path):
    return read_json(path, None)
