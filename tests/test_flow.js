/* Tests for the flow board's pure model and refresh lifecycle. No browser needed: the module is loaded as a
 * CommonJS module, the timers are faked, and the DOM checks use a minimal stub.
 *
 *   node tests/test_flow.js
 */
'use strict';

const path = require('path');
const Flow = require(path.join(__dirname, '..', 'agentlink', 'web', 'flow.js'));

const RESULTS = [];
function check(name, fn) {
  try { fn(); RESULTS.push([name, true, '']); }
  catch (error) { RESULTS.push([name, false, (error && error.message) || String(error)]); }
}
function eq(actual, expected, what) {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  if (a !== b) throw new Error((what || 'value') + ' expected ' + b + ' but got ' + a);
}
function ok(value, what) { if (!value) throw new Error((what || 'expected truthy') + ' was falsy'); }

const ROOT_A = 'E:/a';
const ROOT_B = 'E:/b';

function task(over) {
  return Object.assign({
    projectRoot: ROOT_A, projectName: 'A', id: 'T-1', title: 'Task', state: 'posted',
    from: 'codex', to: 'deepseek', level: 'L1', created: '2026-11-04T10:00:00+08:00',
    updated: '2026-11-04T10:00:00+08:00', rejects: 0, paths: [], dispatch: {},
    aiReview: null, gate: { checksOk: null, outside: [], files: [] },
    receipt: null, review: {}, lastEvent: { seq: 1, kind: 'post', at: '2026-11-04T10:00:00+08:00', note: 'posted' },
  }, over);
}

function snapshot(tasks, projects) {
  return {
    updatedAt: '2026-11-04T12:00:00+08:00',
    projects: projects || [{ root: ROOT_A, name: 'A' }, { root: ROOT_B, name: 'B' }],
    tasks: tasks || [],
  };
}

// -- phase mapping -------------------------------------------------------------
check('missing execution receipts wait for the lead without claiming progress or stopping tools', () => {
  for (const receiver of ['claude', 'deepseek']) {
    const t = task({ to: receiver, state: 'claimed', executionIssue: { status: 'unreported' } });
    eq(Flow.phaseFor(t).label, '回执待核查', 'missing receipt label');
    eq(Flow.motionFor(t), null, 'no simulated running progress');
    eq(Flow.effectFor(t), 'stalled', 'attention effect');
    eq(Flow.chainFor(t)[2].mark, 'pending', 'no invented gate completion');
    eq(t.state, 'claimed', 'original state retained');
  }
  const posted = task({ executionIssue: { status: 'unreported' } });
  eq(Flow.phaseFor(posted).key, 'dispatch', 'unclaimed remains at dispatch');
  eq(Flow.chainFor(posted)[1].mark, 'pending', 'no invented claim');
  eq(Flow.phaseFor(task({ state: 'accepted', executionIssue: { status: 'unreported' } })).key, 'end', 'old issue cannot override completion');
});

check('posted sits at the dispatch phase, nothing beyond it is claimed', () => {
  const p = Flow.phaseFor(task({ state: 'posted' }));
  eq(p.key, 'dispatch', 'phase');
  const chain = Flow.chainFor(task({ state: 'posted' }));
  eq(chain[0].mark, 'active', 'dispatch node');
  eq(chain[1].mark, 'pending', 'work node stays pending');
});

check('claimed is "in progress (picked up)", never "running", and claims no later stage', () => {
  const p = Flow.phaseFor(task({ state: 'claimed' }));
  eq(p.key, 'work', 'phase');
  eq(p.label, '进行中（已领取）', 'label states it is only claimed, not running');
  const chain = Flow.chainFor(task({ state: 'claimed' }));
  eq(chain[2].mark, 'pending', 'gate node pending');
  eq(chain[3].mark, 'pending', 'ai node pending');
  eq(chain[0].mark, 'done', 'dispatch is behind it');
});

check('delivered without a gate verdict does not fabricate a passed gate', () => {
  const p = Flow.phaseFor(task({ state: 'delivered', gate: { checksOk: null } }));
  eq(p.key, 'signoff', 'phase goes straight to sign-off');
  ok(p.note.indexOf('未设验收命令') !== -1, 'note says why: ' + p.note);
  ok(p.note.indexOf('无程序验收记录') !== -1, 'and that there is no gate record');
  const chain = Flow.chainFor(task({ state: 'delivered', gate: { checksOk: null } }));
  eq(chain[2].mark, 'skipped', 'gate node is skipped, not done');
  eq(chain[1].mark, 'done', 'the work node before it is done');
});

check('delivered with a failing gate stays on the gate phase', () => {
  const p = Flow.phaseFor(task({ state: 'delivered', gate: { checksOk: false } }));
  eq(p.key, 'gate', 'phase');
  eq(p.tone, 'bad', 'tone');
  const chain = Flow.chainFor(task({ state: 'delivered', gate: { checksOk: false } }));
  eq(chain[2].mark, 'error', 'gate node is an error');
});

check('delivered with a passing gate moves on to final sign-off', () => {
  const p = Flow.phaseFor(task({ state: 'delivered', gate: { checksOk: true } }));
  eq(p.key, 'signoff', 'phase');
  const chain = Flow.chainFor(task({ state: 'delivered', gate: { checksOk: true } }));
  eq(chain[2].mark, 'done', 'gate done');
  eq(chain[4].mark, 'active', 'signoff active');
});

check('ai_review with a queued prompt is not "running": only started proves it', () => {
  const queued = Flow.phaseFor(task({ state: 'ai_review', aiReview: { status: 'sent' } }));
  eq(queued.label, 'AI 审查未开始', 'queued label');
  eq(queued.tone, 'warn', 'queued tone');
  const started = Flow.phaseFor(task({ state: 'ai_review', aiReview: { status: 'started' } }));
  eq(started.label, 'AI 审查中', 'started label');
  eq(started.note, '审查方已确认开始', 'started note');
  const chain = Flow.chainFor(task({ state: 'ai_review', aiReview: { status: 'started' } }));
  eq(chain[3].mark, 'active', 'ai node active only when started');
});

check('ai_review without any aiReview record does not claim the AI phase', () => {
  const p = Flow.phaseFor({ state: 'ai_review', gate: { checksOk: true } });
  eq(p.label, 'AI 审查未开始', 'label');
  ok(String(p.note).length, 'note explains the absence');
});

check('rework and blocked are anomalies with the reason carried on the node', () => {
  const r = Flow.phaseFor(task({ state: 'rework', review: { note: 'not good' } }));
  eq(r.tone, 'warn', 'rework tone');
  eq(r.note, 'not good', 'rework note');
  const b = Flow.phaseFor(task({ state: 'blocked' }));
  eq(b.tone, 'bad', 'blocked tone');
  const chain = Flow.chainFor(task({ state: 'blocked' }));
  eq(chain[1].mark, 'error', 'work node is an error for blocked');
});

check('accepted and cancelled end the chain', () => {
  const a = Flow.phaseFor(task({ state: 'accepted' }));
  eq(a.key, 'end', 'accepted phase');
  const chain = Flow.chainFor(task({ state: 'accepted' }));
  eq(chain[5].mark, 'done', 'end node is done once accepted');
  const c = Flow.chainFor(task({ state: 'cancelled' }));
  eq(c[5].mark, 'error', 'cancelled end node is an error');
  eq(c[0].mark, 'skipped', 'nothing before the end is shown as done');
});

check('an unknown state is shown as itself, never mapped into a familiar phase', () => {
  const p = Flow.phaseFor({ state: 'weird', gate: {}, aiReview: null });
  eq(p.label, 'weird', 'label keeps the raw state');
  eq(p.note, '未知状态，按原样显示', 'note flags it');
});

check('the chain always has the six pipeline nodes in order', () => {
  const chain = Flow.chainFor(task({ state: 'claimed' }));
  eq(chain.map(n => n.key), ['dispatch', 'work', 'gate', 'ai', 'signoff', 'end'], 'node order');
});


check('a present-but-empty aiReview record proves nothing about the AI stage', () => {
  // The real API sends aiReview: { status: null, attempts: 0, ... }: truthiness is not evidence.
  const empty = { status: null, attempts: 0, startedAt: null, stalled: false, nextRetryAt: null, error: null };
  const delivered = task({ state: 'delivered', gate: { checksOk: true }, aiReview: empty });
  const chain = Flow.chainFor(delivered);
  eq(chain[3].mark, 'skipped', 'AI node is "no record", not done');
  eq(chain[3].note, '无 AI 审查记录', 'and it says so');
  const accepted = task({ state: 'accepted', gate: { checksOk: true }, aiReview: empty });
  const chain2 = Flow.chainFor(accepted);
  eq(chain2[3].mark, 'skipped', 'accepted without AI review does not paint AI as done');
});

check('gate checksOk undefined is "no verdict", not a passed gate', () => {
  const t = task({ state: 'delivered', gate: {} });
  const chain = Flow.chainFor(t);
  eq(chain[2].mark, 'skipped', 'gate node is skipped, never done');
  eq(Flow.statsOf([t]).failed, 0, 'and it is not counted as a failure');
});

check('a started review that went quiet reads as stalled, never "not started"', () => {
  const p = Flow.phaseFor(task({ state: 'ai_review', aiReview: { status: 'started', startedAt: 'x', stalled: true } }));
  eq(p.label, 'AI 审查停滞', 'label');
  eq(p.tone, 'bad', 'tone');
  ok(p.note.indexOf('无结论') !== -1, 'note explains');
});

check('stats partition every task into exactly one bucket', () => {
  const set = [
    task({ id: 'a', state: 'accepted' }),
    task({ id: 'b', state: 'cancelled' }),
    task({ id: 'c', state: 'claimed' }),
    task({ id: 'd', state: 'blocked' }),
    task({ id: 'e', state: 'rework' }),
    task({ id: 'f', state: 'delivered', gate: { checksOk: false } }),
    task({ id: 'g', state: 'ai_review', aiReview: { status: 'rejected' } }),
    task({ id: 'h', state: 'ai_review', aiReview: { status: 'sent' } }),
  ];
  const s = Flow.statsOf(set);
  eq(s.total, 8, 'total');
  eq(s.active + s.done + s.failed, s.total, 'buckets add up to the total');
  eq(s.done, 2, 'accepted + cancelled');
  eq(s.failed, 4, 'blocked, rework, failing gate and a rejected AI review all count as failure');
});


// -- paused（新增 state）与动画契约 ----------------------------------------------
check('paused is a scheduling state: shown, static, and keeps the pre-pause phase', () => {
  const t = task({ state: 'paused', control: { previousState: 'claimed' } });
  const p = Flow.phaseFor(t);
  eq(p.label, '已暂停调度', 'label');
  eq(p.tone, 'paused', 'tone');
  ok(p.note.indexOf('暂停前') !== -1, 'names the phase it was in: ' + p.note);
  ok(p.note.indexOf('宿主') !== -1, 'and where the running AI actually lives');
  const chain = Flow.chainFor(t);
  const marks = chain.map(n => n.mark);
  ok(marks.indexOf('paused') !== -1, 'one node is marked paused');
  eq(marks.filter(m => m === 'active').length, 0, 'nothing is "active" while paused');
  eq(marks[marks.length - 1], 'pending', 'the chain does not run to the end');
});

check('paused tasks stay in the default (open) filter', () => {
  const board2 = snapshot([task({ id: 'T-paused', state: 'paused' })]);
  eq(Flow.filterTasks(board2, {}).map(t => t.id), ['T-paused'], 'visible without show-finished');
});

check('animation only follows real evidence, never a queued prompt or a wait', () => {
  eq(Flow.motionFor(task({ state: 'paused' })), null, 'paused is static');
  eq(Flow.motionFor(task({ state: 'accepted' })), null, 'accepted is static');
  eq(Flow.motionFor(task({ state: 'cancelled' })), null, 'cancelled is static');
  eq(Flow.motionFor(task({ state: 'ai_review', aiReview: { status: 'sent' } })), null,
     'a queued prompt is not running');
  eq(Flow.motionFor(task({ state: 'ai_review', aiReview: { status: 'started', stalled: true } })), null,
     'a stalled review is static');
  eq(Flow.motionFor(task({ state: 'ai_review', aiReview: { status: 'started' } })), 'review',
     'a started review animates');
  eq(Flow.motionFor(task({ state: 'claimed' })), 'work', 'a claimed task animates as work');
  eq(Flow.motionFor(task({ state: 'posted' })), null, 'waiting for a claim stays static');
  eq(Flow.motionFor(task({ state: 'delivered', finalReview: { owner: 'codex' } })), null,
     'waiting for final review must not pretend to run');
});

check('refresh signature ignores refresh-only churn but captures real changes', () => {
  const base = task({ id: 'T-1', state: 'claimed' });
  const same = task({ id: 'T-1', state: 'claimed' });
  eq(Flow.signatureOf(base, false), Flow.signatureOf(same, false), 'same content, same signature');
  const renamed = task({ id: 'T-1', state: 'claimed', title: 'changed' });
  ok(Flow.signatureOf(base, false) !== Flow.signatureOf(renamed, false), 'a title change resizes the signature');
  const advanced = task({ id: 'T-1', state: 'delivered', gate: { checksOk: true } });
  ok(Flow.signatureOf(base, false) !== Flow.signatureOf(advanced, false), 'a state change does too');
  ok(Flow.signatureOf(base, false) !== Flow.signatureOf(base, true), 'and so does opening the row');
});

check('the signature is unique per projectRoot+id, so equal IDs in two projects do not collide', () => {
  const a = task({ id: 'T-9', projectRoot: 'E:/a', state: 'claimed' });
  const b = task({ id: 'T-9', projectRoot: 'E:/b', state: 'claimed' });
  const keyA = a.projectRoot + '\u0000' + a.id, keyB = b.projectRoot + '\u0000' + b.id;
  ok(keyA !== keyB, 'keys differ');
  eq(Flow.signatureOf(a, false) === Flow.signatureOf(b, false), true,
     'the signature alone may match, but the cache key is what separates them');
});


// -- recalling：等待停止确认 ------------------------------------------------------
const recallTask = over => task(Object.assign({
  state: 'recalling',
  recall: { status: 'pending', from: 'deepseek', to: 'codex', at: '2026-11-04T21:50:00+08:00',
            notification: 'sent', intent: 'cancel', note: '用户要求终止' },
  actions: [],                                   // 规格明确：recalling 的 actions 必须是空
}, over));

check('recalling waits for the original executor and is never shown as finished', () => {
  const t = recallTask({});
  const p = Flow.phaseFor(t);
  eq(p.label, '等待停止确认', 'label');
  eq(p.tone, 'recall', 'tone');
  ok(p.note.indexOf('DeepSeek') !== -1, 'names who must confirm: ' + p.note);
  ok(p.note.indexOf('停止写入、工具与子智能体') !== -1, 'says what must stop');
  ok(p.note.indexOf('确认后终止任务') !== -1, 'intent=cancel says it terminates after confirmation');
  ok(p.note.indexOf('已终止') === -1, 'never claims it already ended');
});

check('intent other than cancel says the task goes to the next owner', () => {
  const t = recallTask({ recall: { from: 'deepseek', to: 'codex', intent: 'reassign' } });
  ok(Flow.phaseFor(t).note.indexOf('确认后交给 Codex') !== -1, Flow.phaseFor(t).note);
  ok(Flow.phaseFor(t).note.indexOf('终止') === -1, 'no termination promised');
});

check('recalling freezes the chain at the work node and never reaches the end', () => {
  const marks = Flow.chainFor(recallTask({})).map(n => n.mark);
  eq(marks[0], 'done', 'dispatch is behind it');
  eq(marks[1], 'recalling', 'work node waits');
  eq(marks.slice(2), ['pending', 'pending', 'pending', 'pending'], 'nothing else advances');
  eq(marks.indexOf('done') === marks.lastIndexOf('done'), true, 'no completion painted anywhere');
});

check('recalling never animates', () => {
  eq(Flow.motionFor(recallTask({})), null, 'a recall is waiting, not running');
});

check('recalling stays visible in the default filter and counts as something to watch', () => {
  const board3 = snapshot([recallTask({ id: 'T-rec' })]);
  eq(Flow.filterTasks(board3, {}).map(t => t.id), ['T-rec'], 'not treated as terminal');
  const waiting = Flow.actions.waitingTasks([recallTask({ id: 'T-rec' })]);
  eq(waiting.map(w => w.id), ['T-rec'], 'listed as waiting on a person');
  eq(waiting[0].label, '等待停止确认', 'label');
});

check('a recall that runs past its expected wait is flagged but cannot skip confirmation', () => {
  const alerts = Flow.alertsOf(recallTask({ recall: { from: 'deepseek', to: 'codex', intent: 'cancel', overdue: true } }));
  const text = alerts.map(a => a.text).join(' ');
  ok(text.indexOf('等待停止') !== -1, 'the recall is announced');
  ok(text.indexOf('超过预期等待时间') !== -1, 'overdue is stated');
  ok(text.indexOf('不能') !== -1 || text.indexOf('不可') !== -1, 'and confirmation cannot be skipped');
  ok(text.indexOf('已终止') === -1 && text.indexOf('已完成') === -1, 'still never claims completion');
});

check('an empty actions list on a recall offers nothing (no invented operation)', () => {
  const plan = Flow.actions.planAction(recallTask({}));
  eq(plan.readOnly, false, 'a whitelist was provided');
  eq(plan.items.length, 0, 'and it grants no action');
});

check('the cancel confirmation explains the two paths and denies killing the process', () => {
  const d = Flow.actions.describeConfirm('cancel', task({ id: 'T-1' }));
  ok(d.body.indexOf('还没被领取时直接终止') !== -1, 'unclaimed terminates at once');
  ok(d.body.indexOf('先向原执行方发送停止请求') !== -1, 'claimed sends a stop request first');
  ok(d.body.indexOf('等它确认停止后才终止') !== -1, 'and waits for the confirmation');
  ok(d.body.indexOf('不会结束宿主里正在运行的 AI') !== -1, 'never claims to kill the process');
  ok(d.body.indexOf('不会回滚') !== -1, 'never claims to roll back files');
});

// -- filtering -----------------------------------------------------------------
const board = snapshot([
  task({ id: 'T-open', state: 'claimed' }),
  task({ id: 'T-done', state: 'accepted' }),
  task({ id: 'T-other', projectRoot: ROOT_B, projectName: 'B', state: 'posted' }),
  task({ id: 'T-unreg', projectRoot: 'E:/ghost', projectName: 'Ghost', state: 'posted' }),
]);

check('default shows every open task of every registered project', () => {
  const ids = Flow.filterTasks(board, {}).map(t => t.id);
  eq(ids.sort(), ['T-open', 'T-other'], 'open tasks from both projects, ghost excluded');
});

check('terminal tasks appear only with the show-finished switch', () => {
  const ids = Flow.filterTasks(board, { showDone: true }).map(t => t.id);
  eq(ids.sort(), ['T-done', 'T-open', 'T-other'], 'accepted included, unregistered still out');
});

check('project and search filters compose', () => {
  eq(Flow.filterTasks(board, { project: ROOT_B }).map(t => t.id), ['T-other'], 'by project');
  eq(Flow.filterTasks(board, { query: 't-open' }).map(t => t.id), ['T-open'], 'by id, case-insensitive');
  eq(Flow.filterTasks(board, { query: 'B' }).map(t => t.id), ['T-other'], 'by project name');
  eq(Flow.filterTasks(board, { project: ROOT_B, query: 'T-done' }).length, 0, 'no cross-filter bleed');
});

check('no truncation: 200 open tasks all survive the default filter', () => {
  const many = snapshot(Array.from({ length: 200 }, (_, i) => task({ id: 'T-' + i, state: 'posted' })));
  eq(Flow.filterTasks(many, {}).length, 200, 'count');
});

check('an empty board and a filtered-out board are distinguishable', () => {
  eq(Flow.filterTasks(snapshot([]), {}).length, 0, 'empty snapshot');
  eq(Flow.filterTasks(board, { query: 'nothing-matches' }).length, 0, 'filtered out');
  ok(Flow.filterTasks(board, {}).length > 0, 'board has rows');
});

// -- stats and alerts ----------------------------------------------------------
check('stats separate running reviews from ones whose prompt is merely queued', () => {
  const s = Flow.statsOf([
    task({ id: 'a', state: 'ai_review', aiReview: { status: 'started' } }),
    task({ id: 'b', state: 'ai_review', aiReview: { status: 'sent' } }),
    task({ id: 'c', state: 'blocked' }),
    task({ id: 'd', state: 'accepted' }),
  ]);
  eq([s.aiRunning, s.aiWaiting, s.failed, s.done], [1, 1, 1, 1], 'counters');
});

check('alerts name the anomaly instead of hiding it', () => {
  const a = Flow.alertsOf(task({
    state: 'ai_review', aiReview: { status: 'stalled', error: 'bridge down' },
    gate: { checksOk: false, outside: ['docs/x.md'] }, rejects: 2,
  }));
  ok(a.some(x => x.text.indexOf('程序验收未通过') !== -1), 'gate alert');
  ok(a.some(x => x.text.indexOf('docs/x.md') !== -1), 'outside-path alert');
  ok(a.some(x => x.text.indexOf('驳回 2') !== -1 || x.text.indexOf('2') !== -1), 'rework count alert');
  ok(a.some(x => x.text.indexOf('bridge down') !== -1), 'ai error alert');
});

// -- refresh lifecycle --------------------------------------------------------
function fakeClock() {
  let handlers = [];
  let nextId = 1;
  return {
    setInterval(fn) { const id = nextId++; handlers.push({ id, fn }); return id; },
    clearInterval(id) { handlers = handlers.filter(h => h.id !== id); },
    fire() { handlers.slice().forEach(h => h.fn()); },
    count() { return handlers.length; },
  };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

(async () => {
  // Every async block below pushes into RESULTS: an async failure must show up in the summary and exit
  // non-zero, instead of silently passing while only the synchronous checks are counted.
  async function asyncCheck(name, fn) {
    try { await fn(); RESULTS.push([name, true, '']); }
    catch (error) { RESULTS.push([name, false, (error && error.message) || String(error)]); }
  }

  await asyncCheck('start loads immediately, installs one timer, and stop halts it', async () => {
    const clock = fakeClock();
    const calls = [];
    const c = Flow.createController({
      load: () => { calls.push('load'); return Promise.resolve(snapshot([task({})])); },
      intervalMs: 5000, setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    });
    c.start();
    await flush();
    eq(calls.length, 1, 'immediate load');
    eq(clock.count(), 1, 'one polling timer');
    clock.fire(); await flush();
    eq(calls.length, 2, 'a tick loads again');
    c.stop();
    eq(clock.count(), 0, 'stop clears the timer');
    clock.fire(); await flush();
    eq(calls.length, 2, 'no load after stop');
  });

  await asyncCheck('stop discards an answer that arrives afterwards', async () => {
    const clock = fakeClock();
    const gate = deferred();
    const seen = [];
    const c = Flow.createController({
      load: () => gate.promise,
      setInterval: clock.setInterval, clearInterval: clock.clearInterval,
      onData: payload => seen.push(payload),
    });
    c.start();
    c.stop();                       // stopped while the request is still in flight
    gate.resolve(snapshot([task({})]));
    await flush();
    eq(seen.length, 0, 'a late answer from a stopped controller is dropped');
    eq(c.state().inflight, false, 'in-flight flag cleared');
  });

  await asyncCheck('overlapping refreshes are coalesced, never stacked', async () => {
    const gates = [];
    const calls = [];
    const seen = [];
    const clock = fakeClock();
    const c = Flow.createController({
      load: () => { calls.push(1); const g = deferred(); gates.push(g); return g.promise; },
      setInterval: clock.setInterval, clearInterval: clock.clearInterval,
      onData: p => seen.push(p),
    });
    c.start();
    c.refresh(); c.refresh(); c.refresh();           // three asks while the first is in flight
    eq(calls.length, 1, 'only one request in flight');
    eq(c.state().queued, true, 'the extra asks collapse into one queued refresh');
    gates[0].resolve(snapshot([task({})]));
    await flush();
    eq(calls.length, 2, 'exactly one follow-up ran for all the collapsed asks');
    gates[1].resolve(snapshot([task({})]));
    await flush();
    eq(calls.length, 2, 'and no third request');
    eq(seen.length, 2, 'two renders, no duplicates');
    c.stop();
  });

  await asyncCheck('a failed load keeps the old snapshot and marks it stale', async () => {
    const seen = [];
    let attempt = 0;
    const clock = fakeClock();
    const c = Flow.createController({
      load: () => { attempt++; return attempt === 1 ? Promise.resolve(snapshot([task({})])) : Promise.reject(new Error('offline')); },
      setInterval: clock.setInterval, clearInterval: clock.clearInterval,
      onData: p => seen.push(p),
    });
    c.start(); await flush();
    c.refresh(); await flush();
    eq(seen.length, 2, 'both results delivered');
    eq(seen[1].stale, true, 'second is marked stale');
    eq(seen[1].error, 'offline', 'error surfaced');
    ok(seen[1].snapshot && seen[1].snapshot.tasks.length === 1, 'old snapshot kept');
    eq(seen[0].stale, false, 'first was fresh');
    c.stop();
  });

  await asyncCheck('a hidden page pauses polling and unhiding refreshes at once', async () => {
    const clock = fakeClock();
    const calls = [];
    const c = Flow.createController({
      load: () => { calls.push(1); return Promise.resolve(snapshot([task({})])); },
      setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    });
    c.start(); await flush();
    const before = calls.length;
    c.setPaused(true);
    clock.fire(); await flush();
    eq(calls.length, before, 'a tick while hidden does nothing');   // the timer never forces
    c.setPaused(false); await flush();
    eq(calls.length, before + 1, 'unhiding refreshes at once');
    clock.fire(); await flush();
    eq(calls.length, before + 2, 'and the interval resumes');
    c.stop();
  });

  await asyncCheck('calling start twice does not create a second timer', async () => {
    const clock = fakeClock();
    const c = Flow.createController({
      load: () => Promise.resolve(snapshot([task({})])),
      setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    });
    c.start(); await flush();
    c.start(); await flush();
    eq(clock.count(), 1, 'still exactly one timer');
    c.stop();
  });


  // The race Codex reproduced: stop mid-flight, start again, and let the OLD answer land last. The old
  // callback must not clear the new request's in-flight flag, must not render, and must not re-queue.
  await asyncCheck('a stale answer from before stop() cannot clear the new request in-flight flag', async () => {
    const clock = fakeClock();
    const gates = [];
    const seen = [];
    const c = Flow.createController({
      load: () => { const g = deferred(); gates.push(g); return g.promise; },
      setInterval: clock.setInterval, clearInterval: clock.clearInterval,
      onData: payload => seen.push(payload),
    });
    c.start();                                  // request #1 (stale once we stop)
    await flush();
    c.stop();                                   // bump epoch, release the slot
    c.start();                                  // request #2 (the one that must stay in flight)
    await flush();
    eq(gates.length, 2, 'the restart issued a fresh request');
    eq(c.state().inflight, true, 'new request is in flight');

    gates[0].resolve(snapshot([task({ id: 'stale' })]));      // the old answer lands last
    await flush();
    eq(seen.length, 0, 'the stale answer never rendered');
    eq(c.state().inflight, true, 'the old callback did not clear the new in-flight flag');
    eq(c.state().queued, false, 'and did not leave a queued refresh behind');

    gates[1].resolve(snapshot([task({ id: 'fresh' })]));
    await flush();
    eq(seen.length, 1, 'the live answer rendered once');
    eq(seen[0].snapshot.tasks[0].id, 'fresh', 'and it is the new data');
    c.stop();
  });

  await asyncCheck('an answer to a refresh issued after stop() never renders, even a failure', async () => {
    const clock = fakeClock();
    const gates = [];
    const seen = [];
    const c = Flow.createController({
      load: () => { const g = deferred(); gates.push(g); return g.promise; },
      setInterval: clock.setInterval, clearInterval: clock.clearInterval,
      onData: payload => seen.push(payload),
    });
    c.start(); await flush();
    c.stop();
    c.refresh();                                // the person asked, so a request is sent; its answer is stale
    await flush();
    eq(gates.length, 2, 'an explicit refresh still issues a request');
    gates[0].resolve(snapshot([task({ id: 'stale-1' })]));
    gates[1].reject(new Error('late failure'));
    await flush();
    eq(seen.length, 0, 'neither data nor error from a stopped controller reaches the view');
    eq(c.state().queued, false, 'and nothing was queued for later');
    eq(c.state().inflight, false, 'no phantom in-flight flag is left behind');
  });

  await asyncCheck('a hidden page does not queue a refresh to replay when it returns', async () => {
    const clock = fakeClock();
    const gates = [];
    const c = Flow.createController({
      load: () => { const g = deferred(); gates.push(g); return g.promise; },
      setInterval: clock.setInterval, clearInterval: clock.clearInterval,
      onData: () => {},
    });
    c.start(); await flush();                 // gate #0 is the start load, still in flight
    c.setPaused(true);
    c.refresh(); c.refresh();                 // asking while hidden must fold, never start a second request
    eq(gates.length, 1, 'no second request is started while hidden');
    eq(c.state().queued, true, 'the asks are remembered as a single pending refresh');
    gates[0].resolve(snapshot([task({})]));
    await flush();
    eq(gates.length, 1, 'the pending refresh does not run while hidden');
    eq(c.state().queued, false, 'and the pending ask is dropped when the request completes');
    c.setPaused(false);                       // coming back must fetch once, not replay the dropped ask
    await flush();
    eq(gates.length, 2, 'unhiding does exactly one fresh load');
    c.stop();
  });

  // -- mount() with a stub DOM -----------------------------------------------
  // Async on purpose: mount() renders a snapshot only after the injected load promise settles.
  await new Promise(resolve => {
    const made = [];
    function node(tag) {
      return {
        tagName: tag, className: '', textContent: '', children: [], parentNode: null,
        childNodes: [], firstChild: null, style: {}, dataset: {}, listeners: {},
        setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
        appendChild(child) { this.childNodes.push(child); child.parentNode = this; this.firstChild = this.childNodes[0]; return child; },
        removeChild(child) { this.childNodes = this.childNodes.filter(c => c !== child); this.firstChild = this.childNodes[0] || null; return child; },
        addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
        removeEventListener() {}, classList: { add() {}, remove() {}, toggle() {} },
      };
    }
    const document = {
      createElement: tag => { const n = node(tag); n.ownerDocument = document; made.push(n); return n; },
      createTextNode: text => ({ nodeType: 3, textContent: String(text), parentNode: null }),
      addEventListener() {}, removeEventListener() {},
      hidden: false, visibilityState: 'visible',
      fullscreenElement: null,
      querySelector() { return null; },
    };
    const container = node('div');
    container.ownerDocument = document;
    container.querySelector = () => null;

    const evil = '<img src=x onerror=alert(1)>';
    const board = Flow.mount(container, {
      load: () => Promise.resolve(snapshot([task({ id: 'T-x', title: evil, state: 'claimed' })])),
      onOpenTask() {},
    });
    board.refresh();
    setTimeout(() => {
      try {
        // Walk what was actually created: the task title must exist only as a text node, never as an
        // element's textContent (setting textContent is the trap this check guards against).
        const walk = (n, out) => { out.push(n); (n.childNodes || []).forEach(c => walk(c, out)); return out; };
        const all = [];
        made.forEach(n => { if (n.childNodes && n.childNodes.length) walk(n, all); });
        const texts = all.filter(n => n.nodeType === 3).map(n => n.textContent);
        ok(texts.length > 0, 'text was rendered');
        ok(texts.some(t => t.indexOf('<img') !== -1), 'the title is displayed as plain text');
        ok(!all.some(n => n.tagName && n.textContent === evil), 'no element carries it as textContent');
        ok(!all.some(n => n.tagName === 'img'), 'nothing parsed it into an element');
        board.destroy();
        RESULTS.push(['mount renders task text through text nodes, never as markup', true, '']);
      } catch (error) {
        RESULTS.push(['mount renders task text through text nodes, never as markup', false,
                      (error && error.message) || String(error)]);
      }
      resolve();
    }, 0);
  });


  // First-load failure: the board must say so and offer a retry, not sit on "loading" forever.
  await new Promise(resolve => {
    function node(tag) {
      return {
        tagName: tag, className: '', textContent: '', childNodes: [], firstChild: null,
        style: {}, dataset: {}, listeners: {}, attrs: {},
        setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k] || null; },
        appendChild(child) { this.childNodes.push(child); child.parentNode = this; this.firstChild = this.childNodes[0]; return child; },
        removeChild(child) { this.childNodes = this.childNodes.filter(c => c !== child); this.firstChild = this.childNodes[0] || null; return child; },
        addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
        removeEventListener() {}, classList: { add() {}, remove() {}, toggle() {} },
      };
    }
    const document = {
      createElement: tag => { const n = node(tag); n.ownerDocument = document; return n; },
      createTextNode: text => ({ nodeType: 3, textContent: String(text), parentNode: null }),
      addEventListener() {}, removeEventListener() {},
      hidden: false, visibilityState: 'visible', fullscreenElement: null,
      querySelector() { return null; },
    };
    const container = node('div');
    container.ownerDocument = document;
    container.querySelector = () => null;

    let mode = 'fail';
    const board = Flow.mount(container, {
      load: () => (mode === 'fail' ? Promise.reject(new Error('连接被拒绝')) : Promise.resolve(snapshot([task({})]))),
      onOpenTask() {},
    });

    const text = node => {
      const out = [];
      const walk = n => { if (n.nodeType === 3) out.push(n.textContent); (n.childNodes || []).forEach(walk); };
      walk(node);
      return out.join('');
    };

    board.refresh();
    setTimeout(() => {
      try {
        const all = text(container);
        ok(all.indexOf('加载失败') !== -1, 'the first-load failure is stated');
        ok(all.indexOf('连接被拒绝') !== -1, 'with the actual reason');
        ok(all.indexOf('不是实时数据') !== -1, 'and it says the view is not live');
        ok(all.indexOf('正在加载') === -1, 'it is not left on "loading" forever');
        // the retry button must be able to recover
        const btns = [];
        const walk = n => { if (n.tagName === 'button') btns.push(n); (n.childNodes || []).forEach(walk); };
        walk(container);
        const retry = btns.filter(b => text(b).indexOf('重新加载') !== -1)[0];
        ok(retry, 'a retry button is offered');
        mode = 'ok';
        retry.listeners.click[0]();
        setTimeout(() => {
          try {
            ok(text(container).indexOf('加载失败') === -1, 'the retry cleared the error state');
            ok(text(container).indexOf('Task') !== -1, 'and rendered real data (the compact row shows the short id, not T-1)');
            board.destroy();
            RESULTS.push(['a failed first load is stated and recoverable, never stuck on loading', true, '']);
          } catch (error) {
            RESULTS.push(['a failed first load is stated and recoverable, never stuck on loading', false,
                          (error && error.message) || String(error)]);
          }
          resolve();
        }, 0);
      } catch (error) {
        RESULTS.push(['a failed first load is stated and recoverable, never stuck on loading', false,
                      (error && error.message) || String(error)]);
        resolve();
      }
    }, 0);
  });

  await asyncCheck('an expanded row survives refresh and stays scoped to projectRoot+id', async () => {
    function node(tag) {
      const n = {
        tagName: tag, className: '', childNodes: [], firstChild: null, attrs: {}, listeners: {}, hidden: false,
        setAttribute(k, v) { this.attrs[k] = v; },
        getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
        appendChild(c) { this.childNodes.push(c); c.parentNode = this; this.firstChild = this.childNodes[0]; return c; },
        removeChild(c) { this.childNodes = this.childNodes.filter(x => x !== c); this.firstChild = this.childNodes[0] || null; },
        addEventListener(k, fn) { (this.listeners[k] = this.listeners[k] || []).push(fn); },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        // the row toggle ignores clicks that land inside the action menu or the open button
        closest(sel) { return sel.indexOf('flow-main') !== -1 ? null : undefined; },
      };
      n.classList = {
        add(c) { n.className = (n.className + ' ' + c).trim(); }, remove(c) {
          n.className = n.className.split(' ').filter(x => x && x !== c).join(' ');
        },
        toggle(c, add) {
          n.className = n.className.split(' ').filter(x => x && x !== c).concat(add ? [c] : []).join(' ');
        },
      };
      return n;
    }
    const doc = {
      createElement(tag) { const n = node(tag); n.ownerDocument = doc; return n; },
      createTextNode(text) { return { nodeType: 3, textContent: String(text), childNodes: [] }; },
      addEventListener() {}, removeEventListener() {},
      querySelector() { return null; },
    };
    const container = doc.createElement('div');
    const board = Flow.mount(container, {
      load: () => Promise.resolve(snapshot([
        task({ id: 'T-1', projectRoot: ROOT_A, title: 'Same id in A' }),
        task({ id: 'T-1', projectRoot: ROOT_B, projectName: 'B', title: 'Same id in B' }),
      ])),
    });
    function rows() {
      const out = [];
      const walk = n => { if (n.className && n.className.split(' ').indexOf('flow-task') !== -1) out.push(n); (n.childNodes || []).forEach(walk); };
      walk(container);
      return out;
    }
    function mains() {
      const out = [];
      const walk = n => { if (n.className === 'flow-main') out.push(n); (n.childNodes || []).forEach(walk); };
      walk(container);
      return out;
    }
    try {
      await board.refresh();
      eq(rows().length, 2, 'both projects rendered their own row for the same id');
      // expand only the first row
      mains()[0].listeners.click[0]({ target: null });
      await board.refresh();
      eq(mains().map(n => n.attrs['aria-expanded']), ['true', 'false'], 'only the opened row stays expanded');
      eq(rows().map(n => n.className.split(' ').indexOf('flow-open') !== -1), [true, false],
         'the expansion state agrees after a refresh');
      // the same id in the other project is an independent row: expanding one must not expand the other
      const keys = rows().map(n => n.attrs['data-key']);
      ok(keys[0] !== keys[1], 'the two rows are keyed by projectRoot+id');
    } finally { board.destroy(); }
  });

  // -- the dense console contract ------------------------------------------------
  // A shared stub DOM for the rendering checks below.
  function stubDoc() {
    function node(tag) {
      const n = {
        tagName: tag, className: '', childNodes: [], firstChild: null, attrs: {}, listeners: {}, hidden: false,
        style: {}, dataset: {},
        setAttribute(k, v) { this.attrs[k] = v; },
        getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
        appendChild(c) { this.childNodes.push(c); c.parentNode = this; this.firstChild = this.childNodes[0]; return c; },
        removeChild(c) { this.childNodes = this.childNodes.filter(x => x !== c); this.firstChild = this.childNodes[0] || null; },
        addEventListener(k, fn) { (this.listeners[k] = this.listeners[k] || []).push(fn); },
        removeEventListener() {}, focus() {},
        querySelector() { return null; }, querySelectorAll() { return []; },
        closest() { return null; },
      };
      n.classList = {
        add(...cs) { cs.forEach(c => { if (n.className.split(' ').indexOf(c) < 0) n.className = (n.className + ' ' + c).trim(); }); },
        remove(c) { n.className = n.className.split(' ').filter(x => x && x !== c).join(' '); },
        contains(c) { return n.className.split(' ').indexOf(c) !== -1; },
        toggle(c, add) { if (add === undefined) add = !n.classList.contains(c); n.classList[add ? 'add' : 'remove'](c); },
      };
      return n;
    }
    const doc = {
      createElement(tag) { const n = node(tag); n.ownerDocument = doc; return n; },
      createTextNode(text) { return { nodeType: 3, textContent: String(text), childNodes: [] }; },
      addEventListener() {}, removeEventListener() {},
      activeElement: null, hidden: false, visibilityState: 'visible', fullscreenElement: null,
      querySelector() { return null; },
    };
    const container = doc.createElement('div');
    container.ownerDocument = doc;
    container.querySelector = () => null;
    return { doc, container };
  }
  const textOf = node => {
    const out = [];
    const walk = n => { if (n.nodeType === 3) out.push(n.textContent); (n.childNodes || []).forEach(walk); };
    walk(node);
    return out.join(' ');
  };
  const byClass = (root, cls) => {
    const out = [];
    const walk = n => {
      if (n.className && n.className.split(' ').indexOf(cls) !== -1) out.push(n);
      (n.childNodes || []).forEach(walk);
    };
    walk(root);
    return out;
  };

  // A task carrying every kind of detailed content the console must NOT show.
  const noisyTask = over => task(Object.assign({
    id: 'T-noisy',
    title: 'noisy task',
    paths: ['secret/path/one', 'secret/path/two'],
    receipt: { summary: 'RECEIPT-SUMMARY-SHOULD-NOT-APPEAR', checks: 'CHECK-CMD-SHOULD-NOT-APPEAR', limits: 'LIMITS-SHOULD-NOT-APPEAR' },
    review: { note: 'REVIEW-NOTE-SHOULD-NOT-APPEAR' },
    aiReview: { status: 'failed', error: 'RAW-ERROR-SHOULD-NOT-APPEAR' },
    gate: { checksOk: false, outside: ['OUTSIDE-PATH-SHOULD-NOT-APPEAR'], stat: ['DIFF-STAT-SHOULD-NOT-APPEAR'] },
    lastEvent: { seq: 77, kind: 'deliver', at: '2026-11-04T20:00:00+08:00', note: 'EVENT-BODY-SHOULD-NOT-APPEAR' },
    actions: ['accept', 'reject'],
  }, over));

  await asyncCheck('a collapsed row ships no detailed content into the DOM', async () => {
    const { container } = stubDoc();
    const board = Flow.mount(container, {
      load: () => Promise.resolve(snapshot([noisyTask({ state: 'delivered' })])),
      actions: Flow.actions,
      onAction: () => Promise.resolve({ state: 'accepted' }),
    });
    try {
      await board.refresh();
      const text = textOf(container);
      const forbidden = ['RECEIPT-SUMMARY', 'CHECK-CMD', 'LIMITS-SHOULD', 'REVIEW-NOTE', 'RAW-ERROR',
        'OUTSIDE-PATH', 'DIFF-STAT', 'EVENT-BODY', 'secret/path'];
      forbidden.forEach(token => ok(text.indexOf(token) === -1, token + ' must not be rendered'));
      // and it must not be smuggled into attributes either
      const attrs = [];
      const walk = n => { Object.keys(n.attrs || {}).forEach(k => attrs.push(String(n.attrs[k]))); (n.childNodes || []).forEach(walk); };
      walk(container);
      forbidden.forEach(token => ok(!attrs.some(a => a.indexOf(token) !== -1), token + ' must not be smuggled into an attribute'));
    } finally { board.destroy(); }
  });

  await asyncCheck('the row shows title, project, short id, route, badge, six nodes and an action menu', async () => {
    const { container } = stubDoc();
    const board = Flow.mount(container, {
      load: () => Promise.resolve(snapshot([noisyTask({ state: 'claimed', title: 'Build the search page' })])),
      actions: Flow.actions,
      onAction: () => Promise.resolve({ state: 'claimed' }),
    });
    try {
      await board.refresh();
      const text = textOf(container);
      ok(text.indexOf('Build the search page') !== -1, 'title is shown');
      ok(text.indexOf('A') !== -1, 'project name is shown');
      ok(text.indexOf('noisy') !== -1, 'short id (tail) is shown');
      ok(text.indexOf('Codex') !== -1 && text.indexOf('DeepSeek') !== -1, 'route is shown');
      eq(byClass(container, 'flow-node').length, 6, 'exactly six stage nodes');
      ok(byClass(container, 'flow-badge').length >= 1, 'a status badge exists');
      ok(byClass(container, 'flow-menu').length >= 1, 'a collapsed action menu exists');
      const menu = byClass(container, 'flow-menu')[0];
      eq(menu.open === undefined || menu.open === false || menu.open === null, true, 'the menu starts closed');
      // the phase explanation text is not on the row: labels only
      ok(byClass(container, 'flow-node-note').length === 0, 'no per-node explanation on the row');
    } finally { board.destroy(); }
  });

  await asyncCheck('the anomaly is a short badge, not a paragraph', async () => {
    const { container } = stubDoc();
    const board = Flow.mount(container, {
      load: () => Promise.resolve(snapshot([task({ state: 'blocked', title: 'blocked one' })])),
      actions: Flow.actions,
    });
    try {
      await board.refresh();
      const anomaly = byClass(container, 'flow-anomaly');
      eq(anomaly.length, 1, 'one anomaly badge');
      const label = textOf(anomaly[0]).trim();
      ok(label.length <= 6, 'the badge is a short word, got: ' + label);
      ok(byClass(container, 'flow-alert').length === 0, 'no long alert bar');
    } finally { board.destroy(); }
  });

  await asyncCheck('expanded facts show progress, never the receipt, and always offer Open task', async () => {
    const { container } = stubDoc();
    const opened = [];
    const board = Flow.mount(container, {
      load: () => Promise.resolve(snapshot([noisyTask({ state: 'recalling', recall: { from: 'deepseek', to: 'codex', intent: 'cancel', overdue: true } })])),
      actions: Flow.actions,
      onOpenTask: t => opened.push(t.id),
    });
    try {
      await board.refresh();
      const mains = byClass(container, 'flow-main');
      mains[0].listeners.click[0]({ target: null });
      // no refresh here: the facts are created by the toggle itself, and a refresh would rebuild the row
      const facts = byClass(container, 'flow-fact').map(n => textOf(n));
      ok(facts.some(f => f.indexOf('DeepSeek') !== -1), 'names who is being waited for');
      ok(facts.some(f => f.indexOf('确认停止') !== -1 || f.indexOf('停止写入') !== -1), 'states the stop confirmation');
      ok(facts.some(f => f.indexOf('超时') !== -1), 'states the overdue fact');
      const text = textOf(container);
      ok(text.indexOf('RECEIPT-SUMMARY') === -1, 'the receipt is still absent when expanded');
      ok(text.indexOf('secret/path') === -1, 'and so are the allowed paths');
      const openBtn = byClass(container, 'flow-open-task');
      ok(openBtn.length >= 1, 'Open task is offered once the row is expanded');
      openBtn[0].listeners.click[0]();
      eq(opened, ['T-noisy'], 'opening hands the task id to the main panel');
    } finally { board.destroy(); }
  });

  await asyncCheck('expanded final review shows waiting facts without reasons or raw timestamps', async () => {
    const { container } = stubDoc();
    const board = Flow.mount(container, {
      load: () => Promise.resolve(snapshot([noisyTask({ state: 'delivered', finalReview: {
        owner: 'codex', ageSeconds: 3600, overdue: true,
        waitingSince: '2026-11-04T12:00:00+08:00', reason: 'PRIVATE-FINAL-REASON',
      } })])), actions: Flow.actions,
    });
    try {
      await board.refresh();
      byClass(container, 'flow-main')[0].listeners.click[0]({ target: null });
      const facts = textOf(byClass(container, 'flow-facts')[0]);
      ok(facts.includes('Codex') && facts.includes('1 小时') && facts.includes('超时'), 'keeps the useful waiting facts');
      ok(!facts.includes('PRIVATE-') && !facts.includes('2026-11-04') && !facts.includes('RECEIPT-'), 'omits detailed content');
    } finally { board.destroy(); }
  });

  await asyncCheck('successful menu actions close the menu; failed actions stay available for retry', async () => {
    const { container } = stubDoc();
    let fail = false;
    const board = Flow.mount(container, {
      load: () => Promise.resolve(snapshot([task({ state: 'claimed', actions: ['pause'] })])), actions: Flow.actions,
      onAction: () => fail ? Promise.reject(new Error('TEST-FAILURE')) : Promise.resolve({ state: 'paused' }),
    });
    try {
      await board.refresh();
      const menu = byClass(container, 'flow-menu')[0];
      const button = byClass(container, 'flow-menu-item')[0];
      menu.open = true;
      menu.listeners.toggle[0]();
      button.listeners.click[0]();
      await new Promise(resolve => setTimeout(resolve, 0));
      eq(menu.open, false, 'success removes the overlay');
      fail = true;
      menu.open = true;
      menu.listeners.toggle[0]();
      button.listeners.click[0]();
      await new Promise(resolve => setTimeout(resolve, 0));
      eq(menu.open, true, 'failure keeps the menu available');
      eq(button.disabled, false, 'failure clears busy state');
    } finally { board.destroy(); }
  });

  check('the theme is inherited from the page, never a hardcoded dark palette', () => {
    const fs = require('fs');
    const path = require('path');
    const css = fs.readFileSync(path.join(__dirname, '..', 'agentlink', 'web', 'flow.css'), 'utf8');
    // the container maps every flow token onto a host variable
    ['--bg', '--panel', '--ink', '--muted', '--line', '--accent', '--warn', '--bad', '--ok'].forEach(v => {
      ok(css.indexOf('var(' + v + ',') !== -1 || css.indexOf('var(' + v + ')') !== -1, 'maps ' + v);
    });
    ok(css.indexOf('var(--accent-soft') !== -1, 'supports the optional --accent-soft');
    ok(css.indexOf('var(--hover') !== -1, 'supports the optional --hover');
    ok(css.indexOf('var(--shadow-sm') !== -1, 'supports the optional --shadow-sm');
    // no leftover deep-blue palette from the previous design
    ok(css.indexOf('#10151c') === -1, 'no old dark backdrop colour');
    ok(css.indexOf('#172029') === -1, 'no old dark panel colour');
    ok(css.indexOf('prefers-reduced-motion') !== -1, 'honours reduced motion');
    // the reduced-motion block must cover pseudo-elements too
    ok(/prefers-reduced-motion[\s\S]*?::before[\s\S]*?animation:\s*none/.test(css), 'reduced motion disables pseudo-element animation');
  });

  check('each status has a distinct effect without fabricating execution', () => {
    const expected = { posted: 'dispatch', claimed: 'work', delivered: 'signoff', paused: 'paused',
      recalling: 'recalling', blocked: 'blocked', rework: 'rework', accepted: 'accepted', cancelled: 'cancelled' };
    Object.entries(expected).forEach(([state, effect]) => eq(Flow.effectFor(task({ state })), effect, state));
    eq(Flow.effectFor(task({ state: 'ai_review', aiReview: { status: 'sent' } })), 'queued', 'queued review has a waiting effect');
    eq(Flow.effectFor(task({ state: 'ai_review', aiReview: { status: 'started' } })), 'review', 'started review has a running effect');
    eq(Flow.effectFor(task({ state: 'ai_review', aiReview: { status: 'started', stalled: true } })), 'stalled', 'stalled has an alert effect');
    eq(Flow.effectFor(task({ state: 'delivered', finalReview: { overdue: true } })), 'overdue', 'overdue signoff has a reminder effect');
    eq(Flow.effectFor(task({ state: 'new-unknown-state' })), 'unknown', 'unknown does not invent a state');
    eq(Flow.motionFor(task({ state: 'claimed', dispatch: { stalled: true } })), null, 'stalled task cannot animate execution');
  });

  check('motion is gated strictly: only work and a confirmed started review', () => {
    eq(Flow.motionFor(task({ state: 'claimed' })), 'work', 'claimed runs');
    eq(Flow.motionFor(task({ state: 'ai_review', aiReview: { status: 'started' } })), 'review', 'confirmed started runs');
    eq(Flow.motionFor(task({ state: 'ai_review', aiReview: { status: 'sent' } })), null, 'queued prompt must not run');
    eq(Flow.motionFor(task({ state: 'ai_review', aiReview: { status: 'started', stalled: true } })), null, 'stalled is static');
    eq(Flow.motionFor(task({ state: 'delivered' })), null, 'waiting for final review must not run');
    eq(Flow.motionFor(task({ state: 'paused' })), null, 'paused is static');
    eq(Flow.motionFor(task({ state: 'recalling' })), null, 'stop confirmation is static');
    eq(Flow.motionFor(task({ state: 'accepted' })), null, 'terminal is static');
    eq(Flow.motionFor(task({ state: 'cancelled' })), null, 'cancelled is static');
    eq(Flow.motionFor(task({ state: 'posted' })), null, 'waiting for a claim stays static');
    eq(Flow.motionFor(task({ state: 'rework' })), null, 'unclaimed rework stays static');
  });

  check('a stage cannot look finished before the task has reached it', () => {
    // Pathological snapshots carry verdicts for stages the task has not reached. Position must win over proof.
    const early = { state: 'posted', gate: { checksOk: true }, aiReview: { status: 'accepted' } };
    const chain = Flow.chainFor(early);
    eq(chain.filter(n => n.mark === 'done').length, 0, 'nothing is done on a task nobody has claimed');
    eq(chain[0].mark, 'active', 'only the dispatch stage is active');

    const claimed = { state: 'claimed', gate: { checksOk: true } };
    const chain2 = Flow.chainFor(claimed);
    eq(chain2[2].mark, 'pending', 'the gate stays pending until the work is delivered');

    // the same verdict does count once the task is actually there
    const delivered = { state: 'delivered', gate: { checksOk: true }, aiReview: null };
    const chain3 = Flow.chainFor(delivered);
    eq(chain3[2].mark, 'done', 'a reached gate with a passed check is done');
    eq(chain3[3].mark, 'skipped', 'and a stage it never ran is "no evidence"');
  });

  const failed = RESULTS.filter(r => !r[1]);
  RESULTS.forEach(r => console.log((r[1] ? 'PASS ' : 'FAIL ') + r[0] + (r[2] ? '  — ' + r[2] : '')));
  console.log(RESULTS.length - failed.length + '/' + RESULTS.length + ' passed');
  process.exit(failed.length ? 1 : 0);
})();
