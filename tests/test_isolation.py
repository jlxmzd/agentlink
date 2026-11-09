"""隔离回归：测试脚本绝不可写进开发者真实的项目注册表（projects.json）。

    python -I tests/test_isolation.py

做法：在外层临时目录造一个假的「用户 home」和一个假的 `AGENTLINK_HOME`，在其中写入哨兵
`projects.json`；然后把这些环境变量交给各测试脚本的子进程继承，运行它们，并断言：

  1. 每个脚本都以 0 退出、且报告全部通过；
  2. 外层哨兵 `projects.json` **字节完全未变**；
  3. 外层假 home 内没有新增任何真实配置文件（没有 `.agentlink`、`.claude`、`.codex`、`.dsh` 等被创建）。

哨兵只存在于临时目录里，因此这个回归本身不会碰真实注册表，也不会发起真实 RPC/通知。
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ENTRIES = ['test_agentlink.py', 'test_harness.py', 'test_notifications.py', 'test_task_control.py',
           'test_blockers.py']

# What a leaked run would create under the outer home. The registry is the one that matters most.
FORBIDDEN_IN_OUTER_HOME = ['.agentlink', '.claude', '.codex', '.dsh']

SUMMARY = []


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_entry(entry, sandbox_home, extra_env):
    """Run one test script with the outer fake homes inherited, exactly as a user would."""
    env = dict(os.environ)
    # The child inherits the sentinel locations: if any script fails to isolate itself, it will write here.
    env['AGENTLINK_USER_HOME'] = str(sandbox_home)
    env['AGENTLINK_HOME'] = str(extra_env)
    for key in ('AGENTLINK_NO_DISPATCH', 'AGENTLINK_NO_WAKE'):
        env.pop(key, None)                       # let each script choose its own dispatch/wake isolation
    done = subprocess.run([sys.executable, '-I', str(ROOT / 'tests' / entry)],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding='utf-8', errors='replace', env=env, timeout=600)
    return done


def outer_state(home, registry):
    """Everything a leaked test run could have changed under the outer fake home."""
    snapshot = {'registrySha': digest(registry), 'created': []}
    for name in sorted(os.listdir(str(home))):
        snapshot['created'].append(name)
    return snapshot


def main():
    failures = []
    with tempfile.TemporaryDirectory(prefix='agentlink-isolation-') as tmp:
        outer = Path(tmp)
        outer_home = outer / 'user-home'
        outer_home.mkdir()
        outer_agentlink = outer / 'agentlink-home'
        outer_agentlink.mkdir()
        registry = outer_agentlink / 'projects.json'
        # A sentinel registry: clearly not a real one, and byte-compared before/after every child run.
        sentinel = {'projects': [{'root': 'SENTINEL-DO-NOT-TRUST', 'name': '哨兵项目'}], 'version': 99}
        registry.write_text(json.dumps(sentinel, ensure_ascii=False), encoding='utf-8')

        before_sha = digest(registry)
        before_entries = outer_state(outer_home, registry)['created']

        for entry in ENTRIES:
            done = run_entry(entry, outer_home, outer_agentlink)
            out = (done.stdout or '') + (done.stderr or '')
            passed = done.returncode == 0 and ('passed' in out or 'OK' in out)
            # The registry must be byte-identical after EVERY child, not merely at the end.
            after_sha = digest(registry)
            if after_sha != before_sha:
                failures.append('%s 修改了外层 projects.json（%s → %s）' % (entry, before_sha[:8], after_sha[:8]))
            if not passed:
                tail = out.strip().splitlines()[-6:]
                failures.append('%s 未全部通过（exit %s）：%s' % (entry, done.returncode, ' / '.join(tail)))
            SUMMARY.append('%s: %s (exit %s)' % (entry, 'ok' if passed else 'FAILED', done.returncode))

        # Nothing may have been created under the outer fake home.
        after_entries = outer_state(outer_home, registry)['created']
        leaked = sorted(set(after_entries) - set(before_entries)) + \
                 sorted(set(FORBIDDEN_IN_OUTER_HOME) & set(after_entries))
        leaked = sorted(set(leaked))
        if leaked:
            failures.append('外层假 home 被写入：%s' % ', '.join(leaked))

        final_sha = digest(registry)
        if final_sha != before_sha:
            failures.append('外层 projects.json 最终被改动')
        if registry.read_text(encoding='utf-8') != json.dumps(sentinel, ensure_ascii=False):
            failures.append('哨兵内容被替换')

    print('\n'.join(SUMMARY))
    if failures:
        print('\n隔离失败：')
        for item in failures:
            print(' -', item)
        return 1
    print('隔离验证通过：全部入口都没有触碰外层注册表或外层用户 home')
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding='utf-8')
        except AttributeError: pass
    sys.exit(main())
