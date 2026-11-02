"""Loopback client for the DeepSeek Harness (dsh). Never prints the connection URL or cookies.

The Harness side (hosts/harness/agentlink-bridge.mjs) publishes the host's existing authenticated
loopback URL to ~/.dsh/agentlink/connection.json; no new port is opened. For compatibility the
descriptor written by the MLUQOMS codex-collaboration-bridge is used when ours is absent.
"""
import datetime
import http.cookiejar
import json
import urllib.parse
import urllib.request
import uuid

from .util import read_text, user_home


def dsh_home():
    return user_home() / '.dsh'


def descriptor_path():
    for name in ('agentlink', 'codex-bridge'):
        path = dsh_home() / name / 'connection.json'
        if path.exists(): return path
    raise ValueError('DeepSeek Harness 未连接：未找到 connection.json（安装 Harness 插件并重启 Harness）')


class Client:
    def __init__(self):
        descriptor = json.loads(read_text(descriptor_path()))
        url = urllib.parse.urlsplit(descriptor['url'])
        if url.scheme != 'http' or url.hostname != '127.0.0.1':
            raise ValueError('Harness bridge must use loopback HTTP')
        self.origin = 'http://' + url.netloc
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies),
                                                  urllib.request.ProxyHandler({}))
        self.opener.open(descriptor['url'], timeout=10).close()

    def call(self, method, request):
        method = method.replace('.', '/')
        rpc = str(uuid.uuid4())
        arg = '_request' if method == 'session/list' else 'request'
        body = {'type': 'client-request', 'rpcId': rpc, 'method': method, 'payload': {'args': {arg: request}}}
        req = urllib.request.Request(self.origin + '/api/' + method, json.dumps(body).encode(),
                                     {'Content-Type': 'application/json'})
        result = json.load(self.opener.open(req, timeout=30))
        if result.get('rpcId') != rpc: raise ValueError('RPC correlation mismatch')
        result = result['result']
        if not result['ok']: raise ValueError(result['error']['code'] + ': ' + result['error']['message'])
        return result['value']


def find_session(client, sid):
    if not sid: return None
    return next((i for i in client.call('session.list', {})['items'] if i['sessionId'] == sid), None)


def archived_sessions():
    # Fail closed if lifecycle metadata cannot be read: session.list includes archived sessions.
    return workspace_meta()['archived']


def workspace_meta():
    """The Harness's own workspace store: archived ids plus which sessions each workspace accounts for.

    The UI's session list is driven by `tables.workspaces[*].sessionIds`, not by `parentSessionId`, so a
    session the person forked into a workspace is shown even though it has a parent. Raises ValueError
    when the file is missing or shaped differently — the caller decides whether to degrade."""
    path = dsh_home() / 'storages' / 'workspace.json'
    if not path.exists(): raise ValueError('未找到 Harness 工作区元数据（workspace.json）')
    try: data = json.loads(read_text(path))
    except ValueError as error: raise ValueError('Harness 工作区元数据不是合法 JSON：%s' % error)
    if not isinstance(data, dict): raise ValueError('Invalid Harness workspace metadata')
    archived = (data.get('global') or {}).get('archivedSessionIds')
    if not isinstance(archived, list): raise ValueError('Invalid Harness archive metadata')
    tables = data.get('tables') or {}
    workspaces = tables.get('workspaces') if isinstance(tables, dict) else None
    if workspaces is None: workspaces = {}          # older stores had only global.bookkeeping
    if not isinstance(workspaces, dict): raise ValueError('Invalid Harness workspace metadata')
    rows = []
    for wid, row in workspaces.items():
        if not isinstance(row, dict): continue
        ids = row.get('sessionIds')
        if not isinstance(ids, list): continue
        rows.append({'id': wid, 'path': row.get('path') or '', 'title': row.get('title') or '',
                     'sessions': [s for s in ids if isinstance(s, str) and s]})
    return {'archived': set(archived), 'workspaces': rows}


def workspace_members(meta):
    """session id -> the workspace paths that account for it (a session can sit in more than one)."""
    out = {}
    for row in meta.get('workspaces') or []:
        for sid in row['sessions']: out.setdefault(sid, []).append(row['path'])
    return out


def compact(item, archived=(), members=None):
    values = item.get('projections', {}).get('values', {}) if isinstance(item.get('projections'), dict) else {}
    meta = values.get('sessionListMetadata') if isinstance(values.get('sessionListMetadata'), dict) else {}
    return {'id': item['sessionId'], 'cwd': item.get('cwd'), 'running': bool(item.get('running')),
            'title': title_of(item), 'archived': item['sessionId'] in archived,
            'updated': updated_at(item), 'promptAt': prompt_at(item),
            'blank': bool(meta.get('blank')), 'fork': child_session(item),
            'workspace': bool(members and members.get(item['sessionId']))}


def title_of(item):
    """The title the Harness UI shows: the latest `session/title` event folded into projections.values.title.
    The list RPC's top-level title is documented but never populated, so it is only a fallback."""
    values = item.get('projections', {}).get('values', {}) if isinstance(item.get('projections'), dict) else {}
    for value in (values.get('title'), item.get('title')):
        if isinstance(value, str) and value.strip(): return value.strip()
    return ''


def prompt_at(item):
    """Latest human-authored prompt time (epoch ms) from the persisted list metadata; '' when absent."""
    values = item.get('projections', {}).get('values', {}) if isinstance(item.get('projections'), dict) else {}
    meta = values.get('sessionListMetadata') if isinstance(values.get('sessionListMetadata'), dict) else {}
    value = meta.get('lastPromptAt')
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else ''


def child_session(item):
    """A conversation started from another one (a DeepSeek sub-session): never offered for binding."""
    return bool(item.get('parentSessionId') or item.get('parent_session_id'))


def updated_at(item):
    """The session's last-activity stamp as ISO text. The Harness sends epoch milliseconds (int); a plain
    ISO string is passed through, so both shapes work and sorting stays comparable."""
    values = item.get('projections', {}).get('values', {}) if isinstance(item.get('projections'), dict) else {}
    for source in (item, values):
        if not isinstance(source, dict): continue
        for key in ('updatedAt', 'updated_at', 'updated'):
            value = source.get(key)
            if isinstance(value, bool) or value is None: continue
            if isinstance(value, (int, float)) and value > 0:
                seconds = value / 1000.0 if value > 1e11 else float(value)     # ms since the epoch, else seconds
                try: return datetime.datetime.fromtimestamp(seconds).astimezone().isoformat(timespec='seconds')
                except (OverflowError, OSError, ValueError): return ''
            if isinstance(value, str) and value.strip(): return value.strip()
    return ''
