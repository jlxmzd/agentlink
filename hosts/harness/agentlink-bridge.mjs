import { execFile } from 'node:child_process';
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'agentlink-bridge';
export const inject = ['connection', 'webServer'];

// DeepSeek Harness side of AgentLink. It never talks to the model and opens no port:
// 1. every 2 s it publishes the host's existing authenticated loopback URL to connection.json
//    (directory restricted to this user by the installer), so the panel can list sessions and
//    the bus can dispatch tasks;
// 2. every 15 s it runs `agentlink.py pump-all`: pending DeepSeek dispatches, stall checks and
//    Codex wakes for every registered project.
export function apply(ctx) {
  const here = dirname(fileURLToPath(import.meta.url));
  // Windows PowerShell 5.1 writes a BOM; JSON.parse rejects it.
  const config = JSON.parse(readFileSync(join(here, 'agentlink-bridge.json'), 'utf8').replace(/^﻿/, ''));
  const file = join(here, 'connection.json');
  let previous;
  function publish() {
    if (!ctx.webServer.port) return;
    const url = ctx.connection.authenticatedUrl(`http://127.0.0.1:${ctx.webServer.port}`);
    if (url === previous) return;
    mkdirSync(here, { recursive: true, mode: 0o700 });
    writeFileSync(file + '.tmp', JSON.stringify({ url, pid: process.pid, version: 1 }), { mode: 0o600 });
    renameSync(file + '.tmp', file);
    previous = url;
  }
  let busy = false;
  function tick() {
    if (busy) return;
    busy = true;
    execFile(config.python || 'python', [config.cli, 'pump-all'],
      { cwd: dirname(config.cli), timeout: 60000, windowsHide: true }, () => { busy = false; });
  }
  const publisher = setInterval(publish, 2000);
  const pump = setInterval(tick, 15000);
  publisher.unref();
  pump.unref();
  ctx.on('dispose', () => {
    clearInterval(publisher);
    clearInterval(pump);
    rmSync(file, { force: true });
  });
  publish();
  tick();
}
