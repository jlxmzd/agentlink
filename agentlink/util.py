"""Small shared helpers: time stamps, atomic writes and an exclusive lock file."""
import datetime
import json
import os
from pathlib import Path
import time


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def user_home():
    # Tests point this at a temporary directory so real host stores are never read or written.
    return Path(os.environ.get('AGENTLINK_USER_HOME') or Path.home())


def agentlink_home():
    return Path(os.environ.get('AGENTLINK_HOME') or user_home() / '.agentlink')


def read_text(path):
    # Tolerate a BOM left by older tools; never write one back.
    path = Path(path)
    return path.read_text(encoding='utf-8-sig') if path.exists() else ''


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_bytes(text.encode('utf-8'))
    os.replace(str(temp), str(path))


def read_json(path, default=None):
    text = read_text(path)
    if not text.strip(): return default
    return json.loads(text)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=1) + '\n')


class FileLock:
    """Exclusive lock file; a holder that crashed is ignored after `stale` seconds."""

    def __init__(self, path, timeout=10, stale=30):
        self.path, self.timeout, self.stale = Path(path), timeout, stale

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode()); os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale: self.path.unlink()
                except FileNotFoundError: pass
                if time.monotonic() > deadline: raise ValueError('Locked by another process: ' + self.path.name)
                time.sleep(0.05)

    def __exit__(self, *_):
        try: self.path.unlink()
        except FileNotFoundError: pass
