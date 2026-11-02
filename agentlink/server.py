"""The management panel: a loopback-only HTTP server with a per-launch token.

Only 127.0.0.1 is bound. Every /api call must carry the token injected into the page, and a request whose
Origin or Host is not this server is refused, so other web pages cannot drive the panel.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import urllib.parse
import webbrowser

from . import install, project as projects_mod
from .sessions import discover

WEB = Path(__file__).resolve().parent / 'web'
TOKEN = secrets.token_urlsafe(24)
MAX_BODY = 256 * 1024


def project_of(body):
    root = (body.get('root') or '').strip()
    if not root: raise ValueError('需要项目目录')
    p = projects_mod.Project(root)
    if not p.root.is_dir(): raise ValueError('目录不存在：%s' % p.root)
    return p


def tasks_of(p):
    if not p.exists(): return {'tasks': [], 'events': [], 'wake': None}
    bus = p.bus()
    tasks = bus.all()
    for t in tasks:
        t['spec'], t['guide'] = bus.spec(t['id']), bus.guide(t['id'])
        review = t.get('aiReview')
        if isinstance(review, dict) and 'token' in review:
            t['aiReview'] = {k: v for k, v in review.items() if k != 'token'}   # the review token stays server-side
    return {'tasks': tasks, 'events': bus.read_events(max(0, bus.last_seq() - 60)), 'wake': bus.wake_status(),
            'locks': bus.locks()}


def api(method, path, query, body):
    if method == 'GET' and path == '/api/projects':
        return [p.summary() for p in projects_mod.projects()]
    if method == 'GET' and path == '/api/project':
        return projects_mod.Project(query.get('root', '')).summary()
    if method == 'GET' and path == '/api/sessions':
        return discover(query.get('root', ''), query.get('all') == '1')
    if method == 'GET' and path == '/api/tasks':
        return tasks_of(projects_mod.Project(query.get('root', '')))
    if method == 'GET' and path == '/api/hosts':
        return install.status()
    if method == 'GET' and path == '/api/migration/export':
        p = projects_mod.Project(query.get('root', ''))
        if not p.exists(): raise ValueError('项目尚未确认绑定，无法导出')
        return p.migration_export(query.get('sessions') == '1')
    if method != 'POST': raise LookupError(path)
    if path == '/api/project/add':
        p = project_of(body)
        projects_mod.register(p.root)
        return p.summary()
    if path == '/api/project/remove':
        p = project_of(body)
        projects_mod.unregister(p.root)
        return {'removed': str(p.root), 'blocks': p.remove_blocks() if body.get('cleanup') else []}
    if path == '/api/project/apply':
        p = project_of(body)
        files = p.apply(body.get('bindings') or {}, body.get('roles'), body.get('options'), body.get('name'))
        return {'project': p.summary(), 'files': files}
    if path == '/api/project/settings':
        p = project_of(body)
        if not p.exists(): raise ValueError('项目尚未确认绑定，请先在「绑定」页确认')
        return p.save_settings(body.get('options') or {})
    if path == '/api/migration/import':
        p = project_of(body)
        return p.migration_apply(body.get('package'))
    if path == '/api/task/post':
        p = project_of(body)
        return p.bus().post('user', body.get('to'), body.get('title'), body.get('spec'), body.get('level') or 'L1',
                            body.get('paths') or [], bool(body.get('build')), False, bool(body.get('hold')),
                            body.get('guide') or '', body.get('checks') or [])
    if path == '/api/task/act':
        p = project_of(body)
        bus, action, tid, note = p.bus(), body.get('action'), body.get('id'), body.get('note') or ''
        if action in ('accept', 'reject', 'cancel'): out = bus.review('user', tid, action, note, kind=body.get('kind') or 'code')
        elif action == 'reassign': out = bus.reassign('user', tid, body.get('to'), note)
        elif action == 'dispatch': out = bus.release(tid)
        else: raise ValueError('未知操作 %s' % action)
        bus.wake_all()
        return out
    if path == '/api/wake':
        bus = project_of(body).bus()
        bus.set_wake(bool(body.get('enabled')))
        return bus.wake_status()
    if path == '/api/install':
        return install.run({k: bool(body.get(k)) for k in ('claude', 'codex', 'harness')}, bool(body.get('uninstall')))
    raise LookupError(path)


class Handler(BaseHTTPRequestHandler):
    server_version = 'AgentLink'

    def log_message(self, *_):
        pass                                             # keep the console quiet; nothing sensitive is logged

    def send(self, code, payload, kind='application/json; charset=utf-8'):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def local_request(self):
        port = self.server.server_address[1]
        allowed = {'127.0.0.1:%d' % port, 'localhost:%d' % port}
        if self.headers.get('Host') not in allowed: return False              # blocks DNS rebinding
        origin = self.headers.get('Origin')
        return origin is None or urllib.parse.urlsplit(origin).netloc in allowed

    def handle_any(self, method):
        url = urllib.parse.urlsplit(self.path)
        if not self.local_request(): return self.send(403, {'error': 'forbidden'})
        if not url.path.startswith('/api/'): return self.static(url.path)
        if not secrets.compare_digest(self.headers.get('X-AgentLink-Token', ''), TOKEN):
            return self.send(403, {'error': 'bad token'})
        body = {}
        if method == 'POST':
            length = int(self.headers.get('Content-Length') or 0)
            if length > MAX_BODY: return self.send(413, {'error': 'too large'})
            try: body = json.loads(self.rfile.read(length) or b'{}')
            except ValueError: return self.send(400, {'error': '请求不是 JSON'})
            if not isinstance(body, dict): return self.send(400, {'error': '请求不是对象'})
        query = dict(urllib.parse.parse_qsl(url.query))
        try:
            self.send(200, api(method, url.path, query, body))
        except LookupError:
            self.send(404, {'error': 'not found'})
        except ValueError as error:
            self.send(400, {'error': str(error)})
        except Exception as error:   # the class only: messages may carry paths or connection details
            self.send(500, {'error': type(error).__name__})

    def static(self, path):
        names = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}
        name = names.get(path)
        if not name: return self.send(404, b'not found', 'text/plain')
        data = (WEB / name).read_bytes()
        if name == 'index.html': data = data.replace(b'__AGENTLINK_TOKEN__', TOKEN.encode())
        kind = {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css'}[name.rsplit('.', 1)[1]]
        self.send(200, data, kind + '; charset=utf-8')

    def do_GET(self):
        self.handle_any('GET')

    def do_POST(self):
        self.handle_any('POST')


def make_server(port):
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def serve(port=8765, open_browser=True):
    server = make_server(port)
    url = 'http://127.0.0.1:%d/' % server.server_address[1]
    print('AgentLink 管理面板：' + url + '（Ctrl+C 退出）')
    if open_browser: threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
