"""Install or remove the AgentLink host plugins for Claude Code, Codex and the DeepSeek Harness.

Each part is opt-in, idempotent and backs up the file it changes. Files are written as UTF-8 without
BOM; configuration values are never printed. Restart each changed host afterwards.
"""
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .util import read_text, user_home, write_text

APP = Path(__file__).resolve().parents[1]
HOSTS = APP / 'hosts'
CLAUDE_MOD = HOSTS / 'claude' / 'agentlink'
CODEX_DIR = HOSTS / 'codex'
BRIDGE_ID = 'agentlink-bridge'
MARKET = 'agentlink'
CODEX_HEADERS = (r'\[marketplaces\.%s\]' % MARKET, r'\[plugins\."agentlink@%s"\]' % MARKET)
LEGACY = {'claude': 'tri-collab', 'codex': 'mluqoms-tri', 'harness': 'tri-collaboration-bridge'}


def stamp():
    return datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')


def backup(path):
    if path.exists():
        target = path.with_name('%s.bak-%s' % (path.name, stamp()))
        shutil.copy2(str(path), str(target))
        return target.name
    return None


def change(path, new_text):
    """Write only when content changes; returns a short report."""
    if path.exists() and read_text(path) == new_text and not path.read_bytes().startswith(b'\xef\xbb\xbf'):
        return 'unchanged'
    saved = backup(path)
    write_text(path, new_text)
    return 'updated' + (' (backup %s)' % saved if saved else ' (new file)')


def python_cmd():
    return sys.executable or 'python'


# -- Claude: CLAUDE_CODE_PLUGIN_DIRS in ~/.claude/settings.json (user settings only) ----------
def claude_settings():
    return user_home() / '.claude' / 'settings.json'


def claude_dirs():
    text = read_text(claude_settings())
    data = json.loads(text) if text.strip() else {}
    return data, [d for d in data.get('env', {}).get('CLAUDE_CODE_PLUGIN_DIRS', '').split(os.pathsep) if d]


def claude(remove):
    path = claude_settings()
    if remove and not path.exists(): return 'Claude: nothing to remove'
    data, dirs = claude_dirs()
    dirs = [d for d in dirs if os.path.normcase(d) != os.path.normcase(str(CLAUDE_MOD))]
    if not remove: dirs.append(str(CLAUDE_MOD))
    env = data.setdefault('env', {})
    if dirs: env['CLAUDE_CODE_PLUGIN_DIRS'] = os.pathsep.join(dirs)
    else: env.pop('CLAUDE_CODE_PLUGIN_DIRS', None)
    # The plugin cannot see its own folder: it reads the interpreter and the CLI from these two variables.
    if remove:
        env.pop('AGENTLINK_PYTHON', None); env.pop('AGENTLINK_CLI', None)
    else:
        env['AGENTLINK_PYTHON'] = python_cmd()
        env['AGENTLINK_CLI'] = str(APP / 'agentlink.py')
    if not env: data.pop('env')
    return 'Claude: ' + change(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


# -- Codex: local marketplace + enabled plugin in ~/.codex/config.toml ------------------------
def strip_tables(text, headers):
    for header in headers:
        # A table runs from its header to the next table header or the end of file.
        text = re.sub(r'(?ms)^\s*' + header + r'\s*$.*?(?=^\s*\[|\Z)', '', text)
    return text.rstrip('\n') + '\n' if text.strip() else ''


def codex_hook_command(event):
    cli = str(APP / 'agentlink.py').replace('\\', '/')
    return '"%s" "%s" hook --as codex --event %s' % (python_cmd().replace('\\', '/'), cli, event)


def write_codex_manifest():
    manifest = json.loads(read_text(CODEX_DIR / '.codex-plugin' / 'plugin.template.json'))
    for event, groups in manifest['hooks']['hooks'].items():
        for group in groups:
            for hook in group['hooks']: hook['command'] = codex_hook_command(event)
    write_text(CODEX_DIR / '.codex-plugin' / 'plugin.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')


def codex(remove):
    path = user_home() / '.codex' / 'config.toml'
    if not path.exists(): return 'Codex: config.toml not found; skipped'
    text = strip_tables(read_text(path), CODEX_HEADERS)
    if not remove:
        write_codex_manifest()
        text += ("\n[marketplaces.%s]\nsource_type = \"local\"\nsource = '%s'\n"
                 "\n[plugins.\"agentlink@%s\"]\nenabled = true\n") % (MARKET, HOSTS, MARKET)
    note = '' if remove else '; then run: codex plugin add agentlink@agentlink, and trust its hooks when Codex asks'
    return 'Codex: ' + change(path, text) + note


# -- Harness: copy the module and insert it into the desktop profile --------------------------
def harness_dir():
    return user_home() / '.dsh' / 'agentlink'


def harness_patch():
    return user_home() / '.dsh' / 'profiles' / 'desktop' / 'cordis.patch.yml'


def restrict(directory):
    """The connection descriptor carries a session credential: only this user and SYSTEM may read it."""
    if os.name != 'nt' or os.environ.get('AGENTLINK_USER_HOME'): return
    owner = os.environ.get('USERDOMAIN', '') + '\\' + os.environ.get('USERNAME', '')
    subprocess.run(['icacls', str(directory), '/inheritance:r', '/grant:r', owner + ':(OI)(CI)F',
                    '*S-1-5-18:(OI)(CI)F'], capture_output=True, timeout=30)


def harness(remove):
    patch = harness_patch()
    if not patch.exists(): return 'Harness: desktop profile not found; skipped'
    target = harness_dir()
    block = re.compile(r'\n?- insert:\n    - id: %s\n      name: "[^"\n]*"\n?' % re.escape(BRIDGE_ID))
    text = block.sub('\n', read_text(patch)).rstrip('\n') + '\n'
    if remove:
        report = change(patch, text)
        for name in ('agentlink-bridge.mjs', 'agentlink-bridge.json', 'connection.json'):
            try: (target / name).unlink()
            except FileNotFoundError: pass
        return 'Harness: profile ' + report + '; module files removed (backups kept)'
    target.mkdir(parents=True, exist_ok=True)
    restrict(target)
    shutil.copy2(str(HOSTS / 'harness' / 'agentlink-bridge.mjs'), str(target / 'agentlink-bridge.mjs'))
    write_text(target / 'agentlink-bridge.json',
               json.dumps({'python': python_cmd(), 'cli': str(APP / 'agentlink.py')}, ensure_ascii=False, indent=2) + '\n')
    text += '\n- insert:\n    - id: %s\n      name: "%s"\n' % (BRIDGE_ID, (target / 'agentlink-bridge.mjs').as_uri())
    return 'Harness: profile ' + change(patch, text)


def run(parts, remove=False):
    out = []
    if parts.get('claude'): out.append(claude(remove))
    if parts.get('codex'): out.append(codex(remove))
    if parts.get('harness'): out.append(harness(remove))
    if not out: raise ValueError('选择至少一个宿主')
    out.append('改动后请在各宿主当前工作结束时重启它。')
    return {'report': out, 'status': status()}


def status():
    """What is installed, plus legacy MLUQOMS tri plugins that would run alongside."""
    try: _, dirs = claude_dirs()
    except ValueError: dirs = []
    codex_cfg = read_text(user_home() / '.codex' / 'config.toml')
    patch = read_text(harness_patch())
    conn = harness_dir() / 'connection.json'
    return {
        'claude': {'installed': any(os.path.normcase(d) == os.path.normcase(str(CLAUDE_MOD)) for d in dirs),
                   'legacy': any(LEGACY['claude'] in d for d in dirs)},
        'codex': {'installed': ('[marketplaces.%s]' % MARKET) in codex_cfg,
                  'found': bool(codex_cfg), 'legacy': LEGACY['codex'] in codex_cfg},
        'harness': {'installed': ('id: %s' % BRIDGE_ID) in patch, 'found': bool(patch),
                    'connected': conn.exists() or (user_home() / '.dsh' / 'codex-bridge' / 'connection.json').exists(),
                    'legacy': LEGACY['harness'] in patch},
        'python': python_cmd(),
    }
