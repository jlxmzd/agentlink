/* 面板与流程大屏共用的任务操作模型：可用性、确认文案、等待归属、防双击。
 *
 *   node tests/test_panel.js
 *
 * 只测前端策略，不碰真实任务总线、不发网络请求（post 由测试注入）。
 */
'use strict';

const path = require('path');
const Flow = require(path.join(__dirname, '..', 'agentlink', 'web', 'flow.js'));
const A = Flow.actions;   // 同一份策略：面板通过 window.AgentLinkFlow.actions 取到

const RESULTS = [];
function check(name, fn) {
  try { fn(); RESULTS.push([name, true, '']); }
  catch (error) { RESULTS.push([name, false, (error && error.message) || String(error)]); }
}
async function acheck(name, fn) {
  try { await fn(); RESULTS.push([name, true, '']); }
  catch (error) { RESULTS.push([name, false, (error && error.message) || String(error)]); }
}
function eq(actual, expected, what) {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  if (a !== b) throw new Error((what || 'value') + ' expected ' + b + ' but got ' + a);
}
function ok(v, what) { if (!v) throw new Error((what || 'expected truthy') + ' was falsy'); }

const task = over => Object.assign({
  projectRoot: 'E:/a', projectName: 'A', id: 'T-1', title: 't', state: 'delivered',
  from: 'codex', to: 'deepseek', level: 'L1', paths: [], dispatch: {},
  aiReview: null, gate: { checksOk: true }, finalReview: null,
}, over);

// -- 可用操作完全随后端白名单 ----------------------------------------------------
check('only the actions the backend lists are offered', () => {
  const plan = A.planAction(task({ actions: ['accept', 'reject'] }));
  eq(plan.readOnly, false, 'not read-only');
  eq(plan.items.map(i => i.action), ['accept', 'reject'], 'exactly the listed ones');
  ok(plan.items.some(i => i.label === '验收通过'), 'labels come from the shared table');
});

check('a snapshot without actions is read-only — no guessed permissions', () => {
  eq(A.planAction(task({})).readOnly, true, 'read-only without a whitelist');
  eq(A.planAction(task({})).items.length, 0, 'and offers nothing');
  eq(A.planAction({ state: 'delivered' }).readOnly, true, 'also for a bare object');
});

check('an unknown action name is passed through, not invented into a known one', () => {
  const plan = A.planAction(task({ actions: ['archive'] }));
  eq(plan.items.length, 1, 'listed');
  eq(plan.items[0].action, 'archive', 'action kept');
  eq(plan.items[0].label, 'archive', 'label falls back to the raw name');
  ok(plan.items[0].unknown, 'flagged as unknown');
});

check('duplicate action names collapse to one button', () => {
  const plan = A.planAction(task({ actions: ['cancel', 'cancel', 'accept'] }));
  eq(plan.items.map(i => i.action), ['cancel', 'accept'], 'deduped, order kept');
});

check('an empty whitelist means read-only, not "everything allowed"', () => {
  const plan = A.planAction(task({ actions: [] }));
  eq(plan.readOnly, false, 'the snapshot did have a whitelist');
  eq(plan.items.length, 0, 'but it grants nothing');
});

// -- 危险操作确认文案 ------------------------------------------------------------
check('danger and warn actions require confirmation; plain ones do not', () => {
  ok(A.needsConfirm('cancel'), 'cancel confirms');
  ok(A.needsConfirm('pause'), 'pause confirms');
  ok(A.needsConfirm('reject'), 'reject confirms');
  ok(!A.needsConfirm('accept'), 'accept does not');
  ok(!A.needsConfirm('remind'), 'remind does not');
  ok(!A.needsConfirm('resume'), 'resume does not');
});

check('confirmation wording says pause/cancel control scheduling, not the AI process', () => {
  const pause = A.describeConfirm('pause', task({}));
  ok(pause.body.indexOf('暂停 AgentLink 的调度') !== -1, 'pause explains what it pauses');
  ok(pause.caution.indexOf('无法结束') !== -1, 'pause warns it cannot stop the host AI');
  const cancel = A.describeConfirm('cancel', task({}));
  ok(cancel.body.indexOf('不会结束') !== -1, 'cancel says it does not kill the process');
  ok(cancel.caution.indexOf('宿主') !== -1, 'cancel points at the host');
  ok(cancel.danger, 'cancel is flagged dangerous');
});

check('takeover is described as moving responsibility, never as auto-approval', () => {
  const d = A.describeConfirm('takeover', task({}));
  ok(d.body.indexOf('不会自动通过') !== -1, 'explicitly denies auto-approval');
  ok(!d.danger, 'not a dangerous action');
});

check('reject requires a note and the confirm says so', () => {
  const d = A.describeConfirm('reject', task({}));
  ok(d.needsNote, 'needs a note');
  ok(d.body.indexOf('需要写清具体问题') !== -1, 'and says why');
});

check('confirm titles carry the task id', () => {
  const d = A.describeConfirm('cancel', task({ id: 'T-9' }));
  ok(d.title.indexOf('T-9') !== -1, 'title names the task');
});

// -- 等待时长的人话 --------------------------------------------------------------
check('age seconds convert to human wording without pretending precision', () => {
  eq(A.humanizeAge(30), '不到 1 分钟', 'sub-minute');
  eq(A.humanizeAge(90), '1 分钟', 'minutes');
  eq(A.humanizeAge(3600 * 5), '5 小时', 'hours');
  eq(A.humanizeAge(86400 * 3), '3 天', 'days');
  eq(A.humanizeAge(-1), '', 'negative is not a duration');
  eq(A.humanizeAge('nope'), '', 'garbage is not a duration');
});

check('a waiting final review reads as waiting for a named owner, not as stuck', () => {
  const text = A.finalReviewText({ owner: 'codex', waitingSince: '2026-11-04T20:00:00+08:00', ageSeconds: 5400, reminders: 1, maxReminders: 2 });
  ok(text.indexOf('等待 Codex 处理') !== -1, 'names the owner');
  ok(text.indexOf('已等待 1 小时') !== -1, 'humanized age');
  ok(text.indexOf('提醒：已提醒 1/2 次') !== -1, 'reminder budget');
  ok(text.indexOf('卡死') === -1 && text.indexOf('失败') === -1, 'does not call waiting a failure');
});

check('an overdue final review is flagged, with an action suggestion', () => {
  const text = A.finalReviewText({ owner: 'codex', ageSeconds: 7200, overdue: true });
  ok(text.indexOf('超过预期等待时间') !== -1, 'overdue stated');
  ok(text.indexOf('提醒验收') !== -1, 'suggests the reminder');
});

check('missing finalReview fields degrade to "unknown", never to a guess', () => {
  const text = A.finalReviewText({ owner: 'codex' });
  ok(text.indexOf('等待 Codex 处理') !== -1, 'owner still shown');
  ok(text.indexOf('已等待') === -1, 'no invented duration');
  ok(!text.includes('undefined'), 'no undefined leaks into the UI');
  ok(text.indexOf('已取消') === -1 && text.indexOf('已完成') === -1, 'no invented outcome');
  eq(A.finalReviewText(null), null, 'no record means no block');
});

check('human owner renders with a human label', () => {
  eq(A.ownerLabel('human'), '你', 'human label');
  eq(A.ownerLabel('codex'), 'Codex', 'agent label');
  eq(A.ownerLabel(null), '发起方', 'fallback');
});

// -- 顶部待处理清单 --------------------------------------------------------------
check('the waiting list counts only things a person must act on', () => {
  const list = A.waitingTasks([
    task({ id: 'a', state: 'delivered' }),
    task({ id: 'b', state: 'blocked' }),
    task({ id: 'c', state: 'paused' }),
    task({ id: 'd', state: 'claimed' }),
    task({ id: 'e', state: 'accepted' }),
    task({ id: 'f', state: 'ai_review' }),
  ]);
  eq(list.map(x => x.id), ['a', 'b', 'c'], 'delivered, blocked, paused only');
  eq(list[0].label, '待最终验收', 'label');
});

// -- 执行器：防双击与失败路径 ----------------------------------------------------
(async () => {
  await acheck('a double click sends exactly one request', async () => {
    const calls = [];
    let resolveFirst;
    const gate = new Promise(r => { resolveFirst = r; });
    const runner = A.createRunner((path_, body) => {
      calls.push(body);
      return calls.length === 1 ? gate : Promise.resolve({ state: 'accepted' });
    });
    const t = task({ actions: ['accept'] });
    const first = runner.run(t, 'accept');
    const second = runner.run(t, 'accept');
    await Promise.resolve();                               // let the first request reach the injected post
    eq(calls.length, 1, 'the second click did not send');
    ok(runner.isBusy(t, 'accept'), 'button is busy while running');
    const skipped = await second;
    ok(skipped.skipped, 'the second click reports it was skipped');
    resolveFirst({ state: 'accepted' });
    await first;
    eq(runner.isBusy(t.id, 'accept'), true === false, 'busy cleared after the response');
  });

  await acheck('a failed action clears busy and rethrows for the UI to report', async () => {
    const runner = A.createRunner(() => Promise.reject(new Error('后端拒绝：paused 只能 resume')));
    let caught = null;
    try { await runner.run(task({ actions: ['pause'] }), 'pause'); } catch (e) { caught = e; }
    ok(caught && caught.message.indexOf('后端拒绝') !== -1, 'the backend message survives');
    eq(runner.isBusy(task({}), 'pause'), false, 'busy is released even on failure');
  });

  await acheck('reject without a note never reaches the backend', async () => {
    const calls = [];
    const runner = A.createRunner((p, body) => { calls.push(body); return Promise.resolve({}); });
    let caught = null;
    try { await runner.run(task({ actions: ['reject'] }), 'reject', { note: '   ' }); }
    catch (e) { caught = e; }
    eq(calls.length, 0, 'no request sent');
    ok(caught && caught.message.indexOf('具体问题') !== -1, 'the reason is explained');
  });

  await acheck('the request carries root, id, action and the optional note', async () => {
    const calls = [];
    const runner = A.createRunner((p, body) => { calls.push({ p, body }); return Promise.resolve({ state: 'rework' }); });
    await runner.run(task({ actions: ['reject'], projectRoot: 'E:/p', id: 'T-42' }), 'reject', { note: '证据不足', kind: 'code' });
    eq(calls[0].p, '/api/task/act', 'endpoint');
    eq(calls[0].body, { root: 'E:/p', id: 'T-42', action: 'reject', note: '证据不足', kind: 'code' }, 'payload');
  });

  await acheck('two different actions on one task do not block each other', async () => {
    const seen = [];
    const runner = A.createRunner((p, body) => { seen.push(body.action); return new Promise(() => {}); });
    const t = task({ actions: ['accept', 'remind'] });
    runner.run(t, 'accept');
    runner.run(t, 'remind');
    await Promise.resolve();                               // both requests reach the injected post
    eq(seen.sort(), ['accept', 'remind'], 'both were allowed to start');
  });

  await acheck('the busy key includes projectRoot: equal task IDs in two projects do not block each other', async () => {
    const seen = [];
    const runner = A.createRunner((p2, body) => { seen.push(body.root + '|' + body.id); return new Promise(() => {}); });
    const one = task({ id: 'T-9', projectRoot: 'E:/a', actions: ['cancel'] });
    const two = task({ id: 'T-9', projectRoot: 'E:/b', actions: ['cancel'] });
    runner.run(one, 'cancel');
    runner.run(two, 'cancel');
    await Promise.resolve();
    eq(seen.sort(), ['E:/a|T-9', 'E:/b|T-9'], 'both projects sent their own request');
    ok(runner.isBusy(one, 'cancel'), 'project A stays busy');
    ok(runner.isBusy(two, 'cancel'), 'project B is busy independently');
    eq(runner.busyKey(one, 'cancel') === runner.busyKey(two, 'cancel'), false, 'the keys differ');
  });

  await acheck('the same task in the same project still refuses a double click', async () => {
    const seen = [];
    const runner = A.createRunner((p2, body) => { seen.push(body.id); return new Promise(() => {}); });
    const one = task({ id: 'T-9', projectRoot: 'E:/a', actions: ['cancel'] });
    const twin = task({ id: 'T-9', projectRoot: 'E:/a', actions: ['cancel'] });
    runner.run(one, 'cancel');
    runner.run(twin, 'cancel');
    await Promise.resolve();
    eq(seen, ['T-9'], 'the second click was swallowed');
  });

  await acheck('a resolved action reports the new state, falling back when absent', async () => {
    eq(A.stateAfter({ state: 'paused' }, 'x'), 'paused', 'backend state wins');
    eq(A.stateAfter({}, 'delivered'), 'delivered', 'falls back');
    eq(A.stateAfter(null, ''), '', 'null-safe');
  });

  const failed = RESULTS.filter(r => !r[1]);
  RESULTS.forEach(r => console.log((r[1] ? 'PASS ' : 'FAIL ') + r[0] + (r[2] ? '  — ' + r[2] : '')));
  console.log(RESULTS.length - failed.length + '/' + RESULTS.length + ' passed');
  process.exit(failed.length ? 1 : 0);
})();
