/* AgentLink flow board: a self-contained big-screen view of the task pipeline.
 *
 * Exposed as window.AgentLinkFlow (mounted by the panel) and as a CommonJS module (so the pure model,
 * filtering and the refresh lifecycle can be tested in Node without a DOM).
 *
 *   const board = AgentLinkFlow.mount(container, { load, onOpenTask })
 *   board.start(); board.stop(); board.refresh()
 *
 * `load` is an argument-less callback returning a Promise for the snapshot. Nothing here talks to the network
 * or to the task bus itself: the page injects that, which keeps this module testable and side-effect free.
 */
(function (global, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module !== null && module.exports) module.exports = api;
  if (global) global.AgentLinkFlow = api;
}(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  // -- the legal dictionary -----------------------------------------------------
  // Fixed so an unknown value is shown as itself rather than guessed into a familiar phase.
  var LEVELS = ['L0', 'L1', 'L2', 'L3'];
  var AGENTS = { claude: 'Claude Code', codex: 'Codex', deepseek: 'DeepSeek', user: '你' };
  var STATE = {
    posted: '待领取', claimed: '进行中', delivered: '待验收', rework: '返工',
    blocked: '受阻', paused: '已暂停调度', recalling: '等待停止确认',
    ai_review: 'AI 审查中', accepted: '已验收', cancelled: '已取消',
  };
  var TERMINAL = ['accepted', 'cancelled'];
  // paused is a scheduling state, not a terminal one: it stays visible in the default (open tasks) filter.
  // `sent` means the prompt is queued, never that the reviewer started: it must not read as "审查中".
  var AI_STATUS = {
    pending: '待投递', sending: '投递中', sent: '已入队·未确认启动', started: '审查中（已确认）',
    accepted: '审查通过', rejected: '审查驳回', failed: '未送达', uncertain: '投递不确定',
    stalled: '审查停滞',
  };

  var PHASES = [
    { key: 'dispatch', label: '派发' },
    { key: 'work', label: '执行' },
    { key: 'gate', label: '程序验收' },
    { key: 'ai', label: 'AI 验收' },
    { key: 'signoff', label: '最终验收' },
    { key: 'end', label: '结束' },
  ];
  var PHASE_INDEX = { dispatch: 0, work: 1, gate: 2, ai: 3, signoff: 4, end: 5 };

  var stateLabel = function (s) { return STATE[s] || (s ? String(s) : '未知状态'); };
  var aiLabel = function (s) { return AI_STATUS[s] || (s ? String(s) : ''); };
  var agentLabel = function (a) { return AGENTS[a] || (a ? String(a) : ''); };

  // -- shared task actions ------------------------------------------------------
  // 面板与大屏共用这份操作策略（后端 actions 白名单、确认文案、等待归属、防双击）。
  // 放在 flow.js 里是因为服务端静态白名单只发固定文件，后端不归本任务改。

  var AGENTS = { claude: 'Claude Code', codex: 'Codex', deepseek: 'DeepSeek', user: '你', human: '你' };

  // 每个操作的定义集中在此：标签、语气、是否需要备注、确认文案。
  // 文案刻意不说"结束进程"：这些控制只作用于 AgentLink 的调度与状态。
  var ACTIONS = {
    accept: {
      label: '验收通过', tone: 'primary', note: false,
      detail: '把这条交付验收为完成，任务结束。',
    },
    reject: {
      label: '驳回返工', tone: 'danger', note: true,
      detail: '把任务退回执行方返工，需要写清具体问题。',
    },
    takeover: {
      label: '交总控验收', tone: 'ghost', note: false,
      detail: '把最终验收交给项目总控处理。这只是转移验收责任，不会自动通过。',
    },
    remind: {
      label: '提醒验收', tone: 'ghost', note: false,
      detail: '给等待验收的一方发一次提醒，不改变任务状态。',
    },
    pause: {
      label: '暂停调度', tone: 'warn', note: false,
      detail: '暂停 AgentLink 的调度、投递与过期判断，文件锁仍然保留。' +
              '不会中断宿主里已经在运行的 AI；如果那段对话正在干活，需要你到宿主里自己停。',
    },
    resume: {
      label: '恢复调度', tone: 'primary', note: false,
      detail: '恢复这条任务的调度，暂停期间累积的事项会重新参与流转。',
    },
    cancel: {
      label: '终止任务', tone: 'danger', note: false,
      // 未领取可以立即终止；已领取必须先懂求原执行方停止并等它确认，所以两种说法分开。
      detail: '还没被领取时直接终止；已被领取时先向原执行方发送停止请求，等它确认停止后才终止。' +
              '无论哪种，都不会结束宿主里正在运行的 AI 进程，也不会回滚已经写入的文件。',
    },
    dispatch: {
      label: '重新投递', tone: 'ghost', note: false,
      detail: '把任务重新投递给执行方。若上一次结果不确定，重复投递可能导致重复执行。',
    },
  };

  // 后端允许但这里没定义的操作也不该被猜成某个意思：显式列出来，标签就用原字符串。
  function spec(name) {
    return ACTIONS[name] || { label: String(name), tone: 'ghost', note: false, detail: '', unknown: true };
  }

  function allowedNames(task) {
    var raw = task && task.actions;
    if (!Array.isArray(raw)) return null;                 // 旧快照：没有白名单，只能只读
    var seen = {};
    return raw.filter(function (name) {
      if (typeof name !== 'string' || seen[name]) return false;
      seen[name] = true;
      return true;
    });
  }

  // 返回 { readOnly, items:[{action,label,tone,detail,needsNote,confirmText}] }
  // 可用性完全由后端 actions 决定；前端不再自行推断越权操作。
  function planAction(task) {
    var names = allowedNames(task);
    if (names === null) return { readOnly: true, items: [] };
    var items = names.map(function (name) {
      var s = spec(name);
      return {
        action: name,
        label: s.label,
        tone: s.tone,
        detail: s.detail,
        needsNote: !!s.note,
        unknown: !!s.unknown,
        // 危险操作一律要确认；文案由 describeConfirm 统一给出。
        confirmText: needsConfirm(name) ? describeConfirm(name) : null,
      };
    });
    return { readOnly: false, items: items };
  }

  function needsConfirm(name) {
    var s = spec(name);
    return ['danger', 'warn'].indexOf(s.tone) !== -1;
  }

  function describeConfirm(name, task) {
    var s = spec(name);
    var head = s.label + (task && task.id ? '：' + task.id : '');
    return {
      title: head,
      body: s.detail,
      // 明确"这些控制只保证调度/状态"，避免被理解成终止进程。
      caution: (name === 'cancel' || name === 'pause')
        ? '提示：AgentLink 只能控制调度与任务状态，无法结束宿主的 AI 运行；需要的话请到对应宿主里停止。'
        : null,
      needsNote: !!s.note,
      danger: s.tone === 'danger',
    };
  }

  // -- 最终验收：谁在等、等多久、是否超时、提醒次数 --------------------------------
  function humanizeAge(seconds) {
    var s = Number(seconds);
    if (!isFinite(s) || s < 0) return '';
    if (s < 60) return '不到 1 分钟';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时';
    return Math.floor(s / 86400) + ' 天';
  }

  function ownerLabel(owner) {
    return AGENTS[owner] || (owner ? String(owner) : '发起方');
  }

  // 只负责把后端给的事实变成人话。缺字段就说不详，绝不推测成"卡死"或"已完成"。
  function finalReviewText(fr) {
    if (!fr) return null;
    var lines = [];
    lines.push('最终验收：等待 ' + ownerLabel(fr.owner) + ' 处理');
    if (typeof fr.ageSeconds === 'number' && isFinite(fr.ageSeconds)) {
      lines.push('已等待 ' + humanizeAge(fr.ageSeconds) +
                 (fr.waitingSince ? '（自 ' + String(fr.waitingSince) + '）' : ''));
    } else if (fr.waitingSince) {
      lines.push('等待开始于 ' + String(fr.waitingSince));
    }
    if (fr.overdue) lines.push('状态：已超过预期等待时间，建议提醒验收或交总控');
    if (typeof fr.reminders === 'number' && typeof fr.maxReminders === 'number') {
      lines.push('提醒：已提醒 ' + fr.reminders + '/' + fr.maxReminders + ' 次');
    } else if (typeof fr.reminders === 'number') {
      lines.push('提醒：已提醒 ' + fr.reminders + ' 次');
    }
    if (fr.reason) lines.push('说明：' + String(fr.reason));
    return lines.join('\n');
  }

  // 顶部"待处理"清单用这个：只统计真正需要人动的事，不把等待当成异常。
  function waitingTasks(tasks) {
    var out = [];
    (tasks || []).forEach(function (t) {
      if (!t) return;
      if (t.state === 'delivered') out.push({ id: t.id, kind: 'final', label: '待最终验收', task: t });
      else if (t.state === 'blocked') out.push({ id: t.id, kind: 'blocked', label: '受阻', task: t });
      else if (t.state === 'paused') out.push({ id: t.id, kind: 'paused', label: '已暂停调度', task: t });
      // 等待停止确认也是要人盯着的事：原执行方不回话就一直等你，超时也不能跳过。
      else if (t.state === 'recalling') out.push({ id: t.id, kind: 'recalling', label: '等待停止确认', task: t });
    });
    return out;
  }

  // -- 执行器：防双击 + 失败原样上抛，界面自己决定怎么提示 -------------------------
  // post(path, body) 由调用方注入（面板用 fetch，测试用假函数）。
  function createRunner(post) {
    var busy = {};                                        // `${taskId}:${action}` -> true

    // 同编号的任务在不同项目里是两条任务：键必须带 projectRoot，否则跨项目会互相吞掉操作。
    function busyKey(task, action) {
      return String((task && task.projectRoot) || '') + '\u0000' + String(task && task.id) + ':' + action;
    }

    function isBusy(task, action) {
      if (typeof task === 'string') return !!busy[task + ':' + action];   // 兼容旧签名 (id, action)
      return !!busy[busyKey(task, action)];
    }

    function run(task, action, opts) {
      var options = opts || {};
      var key = busyKey(task, action);
      if (busy[key]) return Promise.resolve({ skipped: true });   // 双击只算一次
      var s = spec(action);
      if (s.note && !(options.note || '').trim()) {
        return Promise.reject(new Error('「' + s.label + '」需要写清具体问题'));
      }
      busy[key] = true;
      var body = { root: task.projectRoot, id: task.id, action: action };
      if (options.note) body.note = options.note;
      if (options.kind) body.kind = options.kind;
      return Promise.resolve()
        .then(function () { return post('/api/task/act', body); })
        .then(function (out) { delete busy[key]; return out; },
              function (error) { delete busy[key]; throw error; });
    }

    return { run: run, isBusy: isBusy, busyKey: busyKey, busyMap: function () { return busy; } };
  }

  function stateAfter(out, fallback) {
    return (out && out.state) || fallback || '';
  }


  // -- phase mapping ------------------------------------------------------------
  // A phase is claimed only with evidence: `delivered` alone does not prove the program gate passed, and
  // a queued AI prompt proves nothing about the reviewer having started.
  // -- evidence ----------------------------------------------------------------
  // The real API sends `aiReview: { status: null, ... }`, so a truthy object proves nothing. Every stage below
  // is decided by an explicit value, and "no record" is its own outcome rather than a silent "done".
  function aiStarted(task) {
    return !!task && !!(task.aiReview && task.aiReview.status === 'started');
  }
  function aiFinished(task) {
    var s = task && task.aiReview && task.aiReview.status;
    return s === 'accepted' || s === 'rejected';
  }
  function gateValue(task) {
    var v = task && task.gate && task.gate.checksOk;
    return v === true ? true : (v === false ? false : null);    // undefined and null both mean "no verdict"
  }

  function phaseFor(task) {
    var state = task && task.state;
    var gate = (task && task.gate) || {};
    var ai = (task && task.aiReview) || {};
    var checks = gateValue(task);

    if (state === 'accepted') return { key: 'end', label: '已验收', tone: 'ok', note: '' };
    if (state === 'cancelled') return { key: 'end', label: '已取消', tone: 'muted', note: '任务被取消' };
    if (['posted', 'claimed', 'rework'].indexOf(state) >= 0 && (task.executionIssue || {}).status === 'unreported')
      return { key: state === 'posted' ? 'dispatch' : 'work', label: '回执待核查', tone: 'warn', note: '等待总控核查，原状态与文件锁保留' };
    if (state === 'posted') return { key: 'dispatch', label: '待领取', tone: 'idle', note: '' };
    if (state === 'claimed') return { key: 'work', label: '进行中（已领取）', tone: 'busy', note: '已领取，未交付' };
    if (state === 'rework') {
      return { key: 'work', label: '返工', tone: 'warn', note: (task.review && task.review.note) || '被驳回，需返工' };
    }
    if (state === 'blocked') return { key: 'work', label: '受阻', tone: 'bad', note: '执行方报告受阻' };
    if (state === 'recalling') {
      // 等待停止确认：任务尚未终止，也不是完成。只说明在等谁，以及确认后去哪。
      var rec = task.recall || {};
      var owner = rec.from ? agentLabel(rec.from) : '原执行方';
      var after = rec.intent === 'cancel' ? '确认后终止任务' : ('确认后交给 ' + agentLabel(rec.to || task.to));
      return {
        key: 'work', label: '等待停止确认', tone: 'recall',
        note: '等待 ' + owner + ' 停止写入、工具与子智能体；' + after,
      };
    }
    if (state === 'paused') {
      // 暂停是调度层面的状态：保留暂停前处在哪个阶段，但整行静止、不装作正在跑。
      var before = (task.control && task.control.previousState) || null;
      var where = before ? stateLabel(before) : '原状态';
      return {
        key: 'work', label: '已暂停调度', tone: 'paused',
        note: '调度已暂停（暂停前：' + where + '）；已运行的 AI 需到宿主停止',
      };
    }
    if (state === 'ai_review') {
      if (ai.status === 'started') {
        // A started review that has gone quiet is still *started*: stalled, not "never began".
        if (ai.stalled) return { key: 'ai', label: 'AI 审查停滞', tone: 'bad', note: '已开始但迟迟无结论' };
        return { key: 'ai', label: 'AI 审查中', tone: 'busy', note: '审查方已确认开始' };
      }
      return {
        key: 'ai', label: 'AI 审查未开始', tone: 'warn',
        note: aiLabel(ai.status) || '提示已入队，尚未确认启动',
      };
    }
    if (state === 'delivered') {
      if (checks === false) return { key: 'gate', label: '程序验收未通过', tone: 'bad', note: '' };
      if (checks === null) {
        return { key: 'signoff', label: '待验收', tone: 'idle', note: '未设验收命令（无程序验收记录）' };
      }
      return { key: 'signoff', label: '待最终验收', tone: 'idle', note: '程序验收已通过' };
    }
    return { key: 'work', label: stateLabel(state), tone: 'muted', note: '未知状态，按原样显示' };
  }

  // Node chain for one task. A node is `done` only when the task carries the evidence for it:
  //   gate      done only for an explicit checksOk === true
  //   ai        done only for aiReview.status accepted (rejected is an error, started is running)
  //   signoff   done only when the task is accepted
  // Everything else is pending, or "no record" (skipped) when the task has already moved past it without one.
  function chainFor(task) {
    var phase = phaseFor(task);
    var at = PHASE_INDEX[phase.key];
    if (at === undefined) at = PHASE_INDEX.work;
    var state = task && task.state;
    var checks = gateValue(task);
    var ai = (task && task.aiReview) || {};
    var nodes = [];

    // 暂停与等待停止确认都是"冻结在原处等人"的状态：只标出位置，不推进、不宣布结束。
    var frozenAt = null;
    if (state === 'paused') {
      var prev = (task && task.control && task.control.previousState) || null;
      var prevPhase = prev ? phaseFor({ state: prev, gate: task.gate, aiReview: task.aiReview }).key : 'work';
      frozenAt = PHASE_INDEX[prevPhase] === undefined ? PHASE_INDEX.work : PHASE_INDEX[prevPhase];
    } else if (state === 'recalling') {
      frozenAt = PHASE_INDEX.work;
    }

    for (var i = 0; i < PHASES.length; i++) {
      var key = PHASES[i].key;
      var mark = 'pending';
      var note = '';

      if (state === 'cancelled') {
        mark = key === 'end' ? 'error' : 'skipped';
        nodes.push({ key: key, label: PHASES[i].label, mark: mark, note: key === 'end' ? '任务被取消' : '' });
        continue;
      }

      if (frozenAt !== null) {
        if (i < frozenAt) mark = 'done';
        else if (i === frozenAt) {
          mark = state === 'paused' ? 'paused' : 'recalling';
          note = state === 'paused' ? '调度已暂停' : '等待停止确认';
        } else mark = 'pending';
        nodes.push({ key: key, label: PHASES[i].label, mark: mark, note: note });
        continue;
      }

      // A stage behind the cursor is only "done" with proof; otherwise it is "no record".
      var behind = i < at;
      if (key === 'dispatch') {
        mark = behind ? 'done' : (i === at ? 'active' : 'pending');
      } else if (key === 'work') {
        if (state === 'rework' || state === 'blocked') mark = 'error';
        else if (state === 'accepted') mark = 'done';
        else if (i < at) mark = 'done';
        else if (i === at) mark = 'active';
      } else if (key === 'gate') {
        // A passed verdict only counts once the task has actually reached the gate.
        if (checks === true && i <= at) mark = 'done';
        else if (checks === false && i <= at) mark = 'error';
        else mark = behind ? 'skipped' : 'pending';               // no verdict / not reached: never "done"
        if (checks === null && behind) note = '无程序验收记录';
      } else if (key === 'ai') {
        var aiStatus = ai.status;
        // Same rule: an accepted review cannot paint a task that never reached this stage.
        if (aiStatus === 'accepted' && i <= at) mark = 'done';
        else if (aiStatus === 'rejected' && i <= at) mark = 'error';
        else if (aiStatus === 'started') mark = i <= at ? 'active' : 'pending';
        else if (i < at) mark = 'skipped';                        // passed without any AI review
        else mark = i === at ? 'active' : 'pending';
        if (i < at && (aiStatus === null || aiStatus === undefined)) note = '无 AI 审查记录';
      } else if (key === 'signoff') {
        if (state === 'accepted') mark = 'done';
        else if (i === at) mark = 'active';
        else mark = behind ? 'done' : 'pending';
      } else {
        if (state === 'accepted') mark = 'done';
        else if (i === at) mark = 'active';
        else mark = behind ? 'done' : 'pending';
      }

      // The active node carries the phase reason; a done-but-unproven node carries "no record".
      if (i === at && phase.note) note = phase.note;
      nodes.push({ key: key, label: PHASES[i].label, mark: mark, note: note });
    }
    return nodes;
  }

  // 动画由真实状态驱动：只有明确证据的阶段才动；暂停与终态一律静止。
  // 'review' = 已确认 started 的审查；'work' = 已领取的执行。等待领取或返工静止。
  // 其余（sent 只是入队、等待最终验收）不产生动画，绝不伪造进度。
  function motionFor(t) {
    if (!t) return null;
    // 暂停/等待停止确认/终态都是"等人"的状态：静止，避免看成还在跑。
    if (t.state === 'paused' || t.state === 'recalling' || t.state === 'accepted' || t.state === 'cancelled') return null;
    if (['posted', 'claimed', 'rework'].indexOf(t.state) >= 0 && (t.executionIssue || {}).status === 'unreported') return null;
    if (t.state === 'ai_review') {
      var ai = t.aiReview || {};
      return (ai.status === 'started' && !ai.stalled) ? 'review' : null;
    }
    if (t.state === 'claimed' && !(t.dispatch || {}).stalled) return 'work';
    return null;
  }

  // Decorative status effects are separate from evidence of execution: a waiting badge may breathe,
  // while only motionFor() may animate progress along the pipeline.
  function effectFor(t) {
    if (!t) return 'unknown';
    if (t.state === 'paused' || t.state === 'recalling' || t.state === 'accepted' || t.state === 'cancelled') return t.state;
    if (['posted', 'claimed', 'rework'].indexOf(t.state) >= 0 && (t.executionIssue || {}).status === 'unreported') return 'stalled';
    if (t.state === 'blocked' || gateValue(t) === false) return 'blocked';
    if (t.state === 'rework') return 'rework';
    if (t.state === 'ai_review') {
      var ai = t.aiReview || {};
      if (ai.stalled || ['stalled', 'failed', 'uncertain', 'rejected'].indexOf(ai.status) >= 0) return 'stalled';
      return ai.status === 'started' ? 'review' : 'queued';
    }
    if ((t.dispatch || {}).stalled) return 'stalled';
    if (t.state === 'delivered') return (t.finalReview || {}).overdue ? 'overdue' : 'signoff';
    if (t.state === 'claimed') return 'work';
    if (t.state === 'posted') return 'dispatch';
    return 'unknown';
  }

  function dispatchNote(task) {
    var d = (task && task.dispatch) || {};
    if (!d || !d.state) return '';
    var text = d.state;
    if (d.state === 'sent') text = '已入队';
    if (d.stalled) text = text + ' · 疑似停滞';
    if (d.reason) text = text + '（' + d.reason + '）';
    return text;
  }

  // -- filtering --------------------------------------------------------------
  // Default: every open task of every registered project, in full. Nothing is truncated and nothing is
  // hidden for being old: only the explicit terminal states drop out until "show finished" is on.
  function filterTasks(snapshot, opts) {
    var options = opts || {};
    var project = options.project || 'all';
    var query = (options.query || '').trim().toLowerCase();
    var showDone = !!options.showDone;
    var tasks = (snapshot && snapshot.tasks) || [];
    var known = {};
    var projects = (snapshot && snapshot.projects) || [];
    for (var p = 0; p < projects.length; p++) {
      if (projects[p] && projects[p].root) known[projects[p].root] = true;
    }
    return tasks.filter(function (t) {
      if (!t || !known[t.projectRoot]) return false;                  // only registered projects
      if (project !== 'all' && t.projectRoot !== project) return false;
      if (!showDone && TERMINAL.indexOf(t.state) !== -1) return false;
      if (!query) return true;
      return [t.id, t.title, t.projectName, t.from, t.to, stateLabel(t.state)]
        .join(' ').toLowerCase().indexOf(query) !== -1;
    });
  }

  // Counters are an exact partition of `total`: every task lands in exactly one bucket, so the header can
  // never disagree with the number of rows on screen. AI counters are a separate breakdown, not a bucket.
  function statsOf(tasks) {
    var out = { total: 0, active: 0, done: 0, failed: 0, aiWaiting: 0, aiRunning: 0, aiStalled: 0 };
    for (var i = 0; i < tasks.length; i++) {
      var t = tasks[i] || {};
      out.total++;
      var ai = t.aiReview || {};
      var rejected = t.state === 'ai_review' && ai.status === 'rejected';
      // Exactly one bucket per task.
      if (TERMINAL.indexOf(t.state) !== -1) out.done++;
      else if (t.state === 'blocked' || t.state === 'rework' || rejected || gateValue(t) === false) out.failed++;
      else out.active++;
      // AI counters are a separate breakdown over the same tasks, not extra buckets.
      if (t.state !== 'ai_review') continue;
      if (ai.status === 'started') { if (ai.stalled) out.aiStalled++; else out.aiRunning++; }
      else if (ai.status !== 'rejected') out.aiWaiting++;
    }
    return out;
  }

  // Which exception banner a task needs, if any. Empty means "no anomaly to report".
  function alertsOf(task) {
    var out = [];
    var gate = (task && task.gate) || {};
    var ai = (task && task.aiReview) || {};
    if (task.state === 'blocked') out.push({ tone: 'bad', text: '受阻：' + ((task.lastEvent && task.lastEvent.note) || '执行方报告受阻') });
    if (task.state === 'rework') out.push({ tone: 'warn', text: '返工：' + ((task.review && task.review.note) || '被驳回') });
    if (task.rejects) out.push({ tone: 'warn', text: '已被驳回 ' + task.rejects + ' 次' });
    if (gate.checksOk === false) out.push({ tone: 'bad', text: '程序验收未通过' });
    if ((gate.outside || []).length) out.push({ tone: 'warn', text: '允许路径外改动：' + gate.outside.join(', ') });
    if (ai.status === 'started' && ai.stalled) out.push({ tone: 'bad', text: 'AI 审查已停滞' });
    if (['failed', 'uncertain', 'stalled'].indexOf(ai.status) !== -1) {
      out.push({ tone: 'warn', text: 'AI 验收：' + aiLabel(ai.status) + (ai.error ? '（' + ai.error + '）' : '') });
    }
    if ((task.dispatch || {}).stalled) out.push({ tone: 'warn', text: 'DeepSeek 疑似停滞' });
    if (task.state === 'recalling') {
      var rec = task.recall || {};
      var who = rec.from ? agentLabel(rec.from) : '原执行方';
      var why = rec.note ? '（' + rec.note + '）' : '';
      var after = rec.intent === 'cancel' ? '确认后终止任务' : ('确认后交给 ' + agentLabel(rec.to || task.to));
      out.push({ tone: rec.overdue ? 'warn' : 'idle',
                 text: '等待停止：' + who + ' 尚未确认停止写入、工具与子智能体' + why + '；' + after +
                       (rec.overdue ? '。已超过预期等待时间，但确认不能被跳过，仍需原执行方回话' : '') });
    }
    return out;
  }

  // -- refresh lifecycle -------------------------------------------------------
  // Pure controller: no timers, DOM or network of its own, so Node can drive it with a fake clock.
  //   epoch      bumped on every start/stop: a response from a previous epoch is dropped.
  //   inflight   one request at a time; a refresh arriving mid-flight is coalesced, never stacked.
  function createController(options) {
    var opts = options || {};
    var load = opts.load;
    var onData = opts.onData || function () {};
    var onError = opts.onError || function () {};
    var intervalMs = opts.intervalMs === undefined ? 5000 : opts.intervalMs;
    var setTimer = opts.setInterval || (typeof setInterval === 'function' ? setInterval : null);
    var clearTimer = opts.clearInterval || (typeof clearInterval === 'function' ? clearInterval : null);

    var epoch = 0;
    var stoppedEpoch = -1;     // the epoch at which stop() happened: nothing started at or after it may render
    var timer = null;
    var running = false;
    var current = null;        // the request that owns the in-flight slot, or null
    var queued = false;
    var paused = false;
    var last = null;

    function emit(snapshot, stale, error) {
      if (snapshot !== null && snapshot !== undefined) last = snapshot;
      onData({ snapshot: last, stale: !!stale, error: error || null });
    }

    // One request owns the in-flight slot through a ticket. The slot is cleared only by the request that
    // actually holds it, so a late answer from a stopped (or superseded) request can never clear the flag of
    // the request that replaced it.
    // `force` lets start()/an explicit manual retry fetch even though the guard below would otherwise refuse:
    // a stopped controller or a hidden page has no business fetching on its own, and must not bank a request
    // to replay when it returns.
    function refresh(force) {
      if (!load) return Promise.resolve(null);
      if (force !== true && (!running || paused)) return Promise.resolve(null);
      // The manual/permitted nature of an ask is not inherited by the request that follows it: a folded
      // follow-up is always the polling path, so a hidden page can never replay it.
      if (current) { queued = true; return Promise.resolve(null); }
      var ticket = { epoch: epoch };
      current = ticket;
      var request;
      try { request = Promise.resolve(load()); }
      catch (error) { request = Promise.reject(error); }

      var stale = function () {
        return ticket.epoch !== epoch || ticket.epoch <= stoppedEpoch;
      };
      function finish() {
        if (current === ticket) current = null;                   // only the owner frees the slot
      }
      return request.then(function (snapshot) {
        finish();
        if (stale()) { queued = false; return null; }             // stale: never renders, never re-queues
        if (!snapshot || typeof snapshot !== 'object') {
          emit(last, true, '快照格式无效');                        // keep the old view, mark it stale
        } else {
          emit(snapshot, false, null);
        }
        // A folded follow-up only runs when the controller is still allowed to poll: completing a request must
        // not resurrect an ask that was made while the page was hidden.
        if (queued) {
          queued = false;
          if (running && !paused) return refresh();        // never forced: polling path only
        }
        return snapshot;
      }, function (error) {
        finish();
        if (stale()) { queued = false; return null; }
        emit(null, true, (error && error.message) ? error.message : '加载失败');
        // A folded follow-up only runs when the controller is still allowed to poll: completing a request must
        // not resurrect an ask that was made while the page was hidden.
        if (queued) {
          queued = false;
          if (running && !paused) return refresh();        // never forced: polling path only
        }
        return null;
      });
    }

    function tick() {
      if (!running || paused) return;
      refresh();                                                  // guarded: the timer never forces
    }

    function start() {
      if (running) { return refresh(); }
      running = true;
      paused = false;
      epoch++;
      stoppedEpoch = -1;                                          // a new generation: earlier stops no longer apply
      if (setTimer) timer = setTimer(tick, intervalMs);
      return refresh(true);                                       // the controller is running now
    }

    function stop() {
      running = false;
      paused = false;
      epoch++;                                                    // every late answer becomes stale
      stoppedEpoch = epoch;                                       // nothing issued from here on may render
      current = null;                                             // the slot is released for the next start
      queued = false;
      if (timer !== null && clearTimer) { clearTimer(timer); timer = null; }
      return true;
    }

    // The page hides: polling pauses and no refresh is queued while hidden. It comes back: one immediate
    // refresh, then the interval resumes.
    function setPaused(hidden) {
      paused = !!hidden;
      if (!running) return null;
      if (paused) { queued = false; return null; }                // dropped, not remembered for later
      return refresh(true);                                       // coming back is an explicit act
    }

    return {
      start: start, stop: stop, setPaused: setPaused,
      // Manual entry points are allowed to fetch regardless of the polling guard: the person asked.
      refresh: function () { return refresh(true); },
      state: function () {
        return { running: running, paused: paused, inflight: current !== null, queued: queued,
                 epoch: epoch, last: last, timer: timer !== null };
      },
    };
  }

  // -- DOM --------------------------------------------------------------------
  // Every task string goes in through textContent / createTextNode: task titles, receipts and notes are
  // written by other agents, so they are never parsed as markup.
  function mount(container, options) {
    var opts = options || {};
    if (!container || !container.ownerDocument) throw new Error('mount 需要一个容器元素');
    var doc = container.ownerDocument;
    var load = opts.load;
    var onOpenTask = opts.onOpenTask || function () {};
    var onAction = opts.onAction || null;              // (task, action) -> Promise：API 与确认在 app 侧
    var actionApi = opts.actions || (typeof global !== 'undefined' && global.AgentLinkActions) || null;
    var view = { project: 'all', query: '', showDone: false, stale: false, error: null, snapshot: null };
    var expanded = Object.create(null);

    function el(tag, cls, text) {
      var node = doc.createElement(tag);
      if (cls) node.className = cls;
      if (text !== undefined && text !== null) node.appendChild(doc.createTextNode(String(text)));
      return node;
    }

    // shell
    var rootEl = el('div', 'flow-root');
    var headEl = el('div', 'flow-head');
    var titleEl = el('div', 'flow-title', '任务流程大屏');
    var metaEl = el('div', 'flow-meta');
    var statsEl = el('div', 'flow-stats');
    var toolsEl = el('div', 'flow-tools');
    var bannerEl = el('div', 'flow-banner');
    var boardEl = el('div', 'flow-board');
    var boardLane = null;                       // the stable rows container: rebuilt only when it must be
    var hintEl = el('div', 'flow-error-note');

    var projectSel = el('select', 'flow-select');
    projectSel.setAttribute('aria-label', '筛选项目');
    var searchIn = el('input', 'flow-search');
    searchIn.type = 'search';
    searchIn.setAttribute('aria-label', '搜索任务');
    searchIn.placeholder = '搜索 ID / 标题 / 项目';
    var doneLbl = el('label', 'flow-check');
    var doneBox = el('input');
    doneBox.type = 'checkbox';
    doneLbl.appendChild(doneBox);
    doneLbl.appendChild(doc.createTextNode(' 含已结束'));
    var refreshBtn = el('button', 'flow-btn', '刷新');
    var fullBtn = el('button', 'flow-btn', '全屏');
    refreshBtn.type = 'button'; fullBtn.type = 'button';

    toolsEl.appendChild(projectSel);
    toolsEl.appendChild(searchIn);
    toolsEl.appendChild(doneLbl);
    toolsEl.appendChild(refreshBtn);
    toolsEl.appendChild(fullBtn);
    var titleLine = el('div', 'flow-titleline');
    titleLine.appendChild(titleEl);
    titleLine.appendChild(metaEl);
    headEl.appendChild(titleLine);
    headEl.appendChild(statsEl);
    headEl.appendChild(toolsEl);
    rootEl.appendChild(headEl);
    rootEl.appendChild(bannerEl);
    rootEl.appendChild(boardEl);
    rootEl.appendChild(hintEl);

    var prev = container.querySelector ? container.querySelector(':scope > .flow-root') : null;
    if (prev && prev.parentNode) prev.parentNode.removeChild(prev);
    container.appendChild(rootEl);
    if (container.classList) container.classList.add('flow-container');

    function renderProjects() {
      var projects = (view.snapshot && view.snapshot.projects) || [];
      while (projectSel.firstChild) projectSel.removeChild(projectSel.firstChild);
      var all = el('option', null, '全部项目');
      all.value = 'all';
      projectSel.appendChild(all);
      projects.forEach(function (p) {
        var o = el('option', null, (p.name || p.root) + (p.error ? '（读取失败）' : ''));
        o.value = p.root;
        projectSel.appendChild(o);
        if (p.error) o.setAttribute('title', String(p.error));
      });
      projectSel.value = view.project;
      if (projectSel.value !== view.project) { projectSel.value = 'all'; view.project = 'all'; }
    }

    function renderBanner() {
      while (bannerEl.firstChild) bannerEl.removeChild(bannerEl.firstChild);
      if (view.error) {
        bannerEl.className = 'flow-banner flow-stale';
        bannerEl.appendChild(doc.createTextNode('数据获取失败：' + view.error + '（显示的是上一次成功的数据，不代表当前状态）'));
        return;
      }
      if (view.stale) {
        bannerEl.className = 'flow-banner flow-stale';
        bannerEl.appendChild(doc.createTextNode('数据可能已过期'));
        return;
      }
      bannerEl.className = 'flow-banner flow-empty-banner';
    }

    function renderMeta() {
      while (metaEl.firstChild) metaEl.removeChild(metaEl.firstChild);
      var snap = view.snapshot;
      if (!snap) return;
      var updated = new Date(snap.updatedAt);
      metaEl.appendChild(doc.createTextNode('最近更新 ' + (isNaN(updated.getTime()) ? '—' :
        updated.toLocaleTimeString('zh-CN', { hour12: false }))));
      var bad = ((snap.projects || []).filter(function (p) { return p.error; }));
      if (bad.length) metaEl.appendChild(doc.createTextNode(' · ' + bad.length + ' 个项目读取失败'));
    }

    // The legend shows the pipeline once; the row shows the same six stages per task.

    function renderStats(tasks) {
      while (statsEl.firstChild) statsEl.removeChild(statsEl.firstChild);
      var s = statsOf(tasks);
      [['任务', s.total], ['进行中', s.active], ['异常/返工', s.failed], ['已结束', s.done],
       ['AI 审查中', s.aiRunning], ['AI 待开始', s.aiWaiting], ['AI 停滞', s.aiStalled]].forEach(function (pair) {
        var chip = el('div', 'flow-chip');
        chip.appendChild(el('span', 'flow-chip-num', pair[1]));
        chip.appendChild(el('span', 'flow-chip-label', pair[0]));
        statsEl.appendChild(chip);
      });
    }

    // A short, stable identity for the row: the tail of the id keeps equal ids in two projects distinguishable
    // through the project name on the same line.
    function shortTaskId(id) {
      var text = String(id || '');
      var parts = text.split('-');
      return parts.length > 1 ? parts[parts.length - 1] : text;
    }

    var rowCache = Object.create(null);        // key -> { sig, node, state }

    function taskKeyOf(t) {
      return String(t.projectRoot || '') + '\u0000' + String(t.id || '');
    }

    function attemptKey(t) {
      var ai = t.aiReview || {}, d = t.dispatch || {};
      return String(ai.attempts || 0) + '/' + String(d.attempt || 0) + '/' + String(t.rejects || 0);
    }

    // The signature decides whether a row must be rebuilt. It deliberately excludes wall-clock values that change
    // on every poll (ageSeconds, timestamps) — otherwise a row would be recreated every five seconds, killing
    // focus, an open menu and any running animation.
    function signatureOf(t, isOpen) {
      return JSON.stringify([
        t.state, t.title, t.level, t.projectName, t.rejects,
        t.from, t.to, t.origin || null, t.escalation ? t.escalation.to : null,
        t.dispatch ? [t.dispatch.state, t.dispatch.stalled] : null,
        t.aiReview ? [t.aiReview.status, t.aiReview.stalled] : null,
        t.gate ? [t.gate.checksOk] : null,
        t.finalReview ? [t.finalReview.owner, t.finalReview.overdue, t.finalReview.reminders] : null,
        t.control ? [t.control.previousState] : null,
        t.recall ? [t.recall.from, t.recall.to, t.recall.intent, t.recall.overdue] : null,
        t.actions || null,
        isOpen ? 1 : 0,
      ]);
    }

    // A one-word badge for an active anomaly. Full explanations live in the main panel, not on this row.
    function anomalyLabel(task) {
      if (!task) return null;
      var gate = task.gate || {}, ai = task.aiReview || {};
      if (task.state === 'blocked') return { tone: 'bad', text: '受阻' };
      if (task.state === 'rework') return { tone: 'warn', text: '返工' };
      if (task.state === 'recalling') return { tone: 'warn', text: '等停止' };
      if (task.state === 'paused') return { tone: 'warn', text: '已暂停' };
      if (['posted', 'claimed', 'rework'].indexOf(task.state) >= 0 && (task.executionIssue || {}).status === 'unreported')
        return { tone: 'warn', text: '缺回执' };
      if (gate.checksOk === false) return { tone: 'bad', text: '验收未过' };
      if (ai.status === 'stalled') return { tone: 'warn', text: 'AI 停滞' };
      if (ai.status === 'failed' || ai.status === 'uncertain') return { tone: 'warn', text: 'AI 未送达' };
      if ((task.dispatch || {}).stalled) return { tone: 'warn', text: '疑似停滞' };
      if (task.finalReview && task.finalReview.overdue) return { tone: 'warn', text: '验收超时' };
      return null;
    }

    // -- the compact row ---------------------------------------------------------
    // One console line per task: summary | the six-stage pipeline | status | actions. Everything detailed
    // (receipt, paths, checks, review text, raw errors, event bodies, timestamps) is deliberately NOT built into
    // the DOM here — it lives in the main panel, and hiding it with CSS would still ship it to the screen reader.

    function nodeRow(task) {
      var list = el('ol', 'flow-chain');
      list.setAttribute('aria-label', '阶段进度');
      chainFor(task).forEach(function (n) {
        var item = el('li', 'flow-node flow-node-' + n.mark);
        // A dot plus a short label; the per-node explanation is not shown on a console row.
        item.appendChild(el('span', 'flow-dot'));
        item.appendChild(el('span', 'flow-node-label', n.label));
        item.setAttribute('title', n.label + (n.mark === 'active' ? '（进行中）' : n.mark === 'done' ? '（已完成）'
          : n.mark === 'skipped' ? '（无证据）' : n.mark === 'error' ? '（异常）' : n.mark === 'paused' ? '（已暂停）'
          : n.mark === 'recalling' ? '（等待停止确认）' : '（未到达）'));
        list.appendChild(item);
      });
      return list;
    }

    // The expanded row carries only progress facts a person needs to act on: who is waited for, whether it
    // timed out, and the pause/stop-confirmation facts. No receipts, no paths, no raw errors.
    function factBlock(task) {
      var box = el('div', 'flow-facts');
      var fr = task.finalReview;
      if (fr) {
        box.appendChild(el('p', 'flow-fact', '最终验收：等待 ' + ownerLabel(fr.owner) + ' 处理'));
        if (typeof fr.ageSeconds === 'number' && isFinite(fr.ageSeconds)) {
          box.appendChild(el('p', 'flow-fact', '已等待 ' + humanizeAge(fr.ageSeconds) + (fr.overdue ? ' · 已超时' : '')));
        } else if (fr.overdue) box.appendChild(el('p', 'flow-fact', '等待已超时'));
      }
      if (task.state === 'paused') {
        var before = (task.control && task.control.previousState) || null;
        box.appendChild(el('p', 'flow-fact',
          '调度已暂停' + (before ? '（暂停前：' + stateLabel(before) + '）' : '') +
          '；已运行的 AI 需到宿主停止'));
      }
      if (task.state === 'recalling') {
        var rec = task.recall || {};
        var who = rec.from ? agentLabel(rec.from) : '原执行方';
        box.appendChild(el('p', 'flow-fact',
          '等待 ' + who + ' 确认停止写入、工具与子智能体；' +
          (rec.intent === 'cancel' ? '确认后终止任务' : '确认后交给 ' + agentLabel(rec.to || task.to)) +
          (rec.overdue ? '（已超时，但确认不可跳过）' : '')));
      }
      if (task.state === 'ai_review') {
        var ai = task.aiReview || {};
        if (ai.status === 'started' && !ai.stalled) box.appendChild(el('p', 'flow-fact', '审查方已确认开始'));
        else if (ai.status === 'started' && ai.stalled) box.appendChild(el('p', 'flow-fact', '审查已开始但停滞，需要人工处理'));
        else box.appendChild(el('p', 'flow-fact', '提示已入队，尚未确认开始'));
      }
      if (task.state === 'claimed') {
        box.appendChild(el('p', 'flow-fact', '执行方已领取，尚未交付'));
      }
      // The only verb allowed here: hand off to the main panel for the management detail.
      var open = el('button', 'flow-btn flow-open-task', '打开任务');
      open.type = 'button';
      open.addEventListener('click', function (ev) {
        if (ev && ev.stopPropagation) ev.stopPropagation();
        onOpenTask(task);
      });
      box.appendChild(open);
      return box;
    }

    // -- action menu: collapsed by default, whitelist-driven, never a bypass ---------------
    // A small "操作" disclosure per row. It stays exactly as authoritative as before: the backend's `actions`
    // array decides what exists, the confirm text comes from the shared module, and a busy action cannot be
    // fired twice. The open/closed state is kept in rowState so a 5-second refresh does not close it.
    var openMenu = null;
    function closeMenu(restoreFocus) {
      if (!openMenu) return;
      var current = openMenu;
      openMenu = null;
      current.state.menuOpen = false;
      current.wrap.open = false;
      if (restoreFocus) current.summary.focus();
    }
    function menuOutsideClick(ev) {
      if (openMenu && (!ev.target || !ev.target.closest || ev.target.closest('.flow-menu') !== openMenu.wrap)) closeMenu(false);
    }
    function menuEscape(ev) {
      if (ev.key === 'Escape' && openMenu) { ev.preventDefault(); closeMenu(true); }
    }
    doc.addEventListener('click', menuOutsideClick);
    doc.addEventListener('keydown', menuEscape);

    function actionMenu(task, rowState) {
      var plan = actionApi ? actionApi.planAction(task) : { readOnly: true, items: [] };
      if (plan.readOnly || !plan.items.length) return null;

      var wrap = el('details', 'flow-menu');
      if (rowState.menuOpen) wrap.open = true;
      var summary = el('summary', 'flow-menu-btn', '操作');
      wrap.appendChild(summary);
      wrap.addEventListener('toggle', function () {
        rowState.menuOpen = wrap.open;
        if (wrap.open) {
          if (openMenu && openMenu.wrap !== wrap) closeMenu(false);
          openMenu = { wrap: wrap, state: rowState, summary: summary };
          if (wrap.getBoundingClientRect && doc.defaultView) {
            wrap.classList.toggle('flow-menu-up', wrap.getBoundingClientRect().bottom + list.offsetHeight + 8 > doc.defaultView.innerHeight);
          }
        } else if (openMenu && openMenu.wrap === wrap) openMenu = null;
      });

      var list = el('div', 'flow-menu-list');
      plan.items.forEach(function (item) {
        var btn = el('button', 'flow-menu-item flow-act-' + item.tone, item.label);
        btn.type = 'button';
        btn.setAttribute('data-action', item.action);
        btn.addEventListener('click', function (ev) {
          if (ev && ev.stopPropagation) ev.stopPropagation();
          if (rowState.running) return;
          if (!onAction) { hintEl.textContent = '当前页面没有接入任务操作'; return; }
          rowState.running = true;
          hintEl.textContent = '';
          btn.disabled = true;
          Array.from(list.querySelectorAll('button')).forEach(function (button) { button.disabled = true; });
          wrap.setAttribute('aria-busy', 'true');
          btn.classList.add('flow-busy');
          var settled = function () {
            rowState.running = false;
            btn.disabled = false;
            Array.from(list.querySelectorAll('button')).forEach(function (button) { button.disabled = false; });
            wrap.setAttribute('aria-busy', 'false');
            btn.classList.remove('flow-busy');
          };
          Promise.resolve()
            .then(function () { return onAction(task, item.action); })
            .then(function (out) {
              settled();
              if (!out || (!out.cancelled && !out.skipped)) {
                rowState.menuOpen = false;
                wrap.open = false;
                if (openMenu && openMenu.wrap === wrap) openMenu = null;
                summary.focus();
              }
              return controller.refresh();
            })
            .catch(function (error) {
              settled();
              // Operation failures stay visible; they are never swallowed to keep the row tidy.
              var msg = (error && error.message) ? error.message : '操作失败';
              hintEl.textContent = task.id + '：' + msg;
            });
        });
        list.appendChild(btn);
      });
      wrap.appendChild(list);
      return wrap;
    }

    function statusBadge(task) {
      var phase = phaseFor(task);
      // ai_review uses the phase label so a queued prompt is never painted as "审查中".
      var text = task.state === 'ai_review' ? phase.label : stateLabel(task.state);
      var badge = el('span', 'flow-badge flow-badge-' + task.state + ' flow-badge-tone-' + phase.tone);
      var symbols = { dispatch: '↗', work: '›', review: '✦', queued: '⋯', signoff: '◷', overdue: '!',
        paused: 'Ⅱ', recalling: '◇', blocked: '!', stalled: '!', rework: '↶', accepted: '✓', cancelled: '−', unknown: '·' };
      var symbol = el('span', 'flow-state-symbol', symbols[effectFor(task)]);
      symbol.setAttribute('aria-hidden', 'true');
      badge.appendChild(symbol);
      badge.appendChild(el('span', 'flow-state-label', text));
      return badge;
    }

    function taskRow(task, rowState) {
      var key = taskKeyOf(task);
      var row = el('article', 'flow-task flow-effect-' + effectFor(task));
      row.setAttribute('role', 'listitem');
      row.setAttribute('data-key', key);

      // Main information: title on the first line, the small secondary facts on the second.
      var main = el('div', 'flow-main');
      var line1 = el('div', 'flow-line1');
      line1.appendChild(el('span', 'flow-task-title', task.title || '（无标题）'));
      var line2 = el('div', 'flow-line2');
      line2.appendChild(el('span', 'flow-task-meta', task.projectName || ''));
      line2.appendChild(el('span', 'flow-task-meta mono', shortTaskId(task.id)));
      line2.appendChild(el('span', 'flow-task-meta', agentLabel(task.from) + '→' + agentLabel(task.to)));
      if (task.rejects) line2.appendChild(el('span', 'flow-task-meta flow-warn', '驳回 ' + task.rejects));
      // An anomaly is a short badge, not a paragraph.
      var anomaly = anomalyLabel(task);
      if (anomaly) line1.appendChild(el('span', 'flow-anomaly flow-anomaly-' + anomaly.tone, anomaly.text));
      main.appendChild(line1);
      main.appendChild(line2);
      row.appendChild(main);

      row.appendChild(nodeRow(task));
      row.appendChild(statusBadge(task));

      var menu = actionMenu(task, rowState);
      var actions = el('div', 'flow-row-acts');
      if (menu) actions.appendChild(menu);
      row.appendChild(actions);

      // Expanded facts render only when opened; the node is created here but kept empty until then,
      // so nothing detailed is in the DOM of a collapsed row.
      var factsWrap = el('div', 'flow-row-facts');
      factsWrap.hidden = !expanded[key];
      if (expanded[key]) factsWrap.appendChild(factBlock(task));
      row.appendChild(factsWrap);
      row.classList.toggle('flow-open', !!expanded[key]);

      // A click anywhere on the summary area toggles; the action menu and buttons stop propagation themselves.
      var renderFacts = function () {
        while (factsWrap.firstChild) factsWrap.removeChild(factsWrap.firstChild);
        factsWrap.hidden = !expanded[key];
        if (expanded[key]) factsWrap.appendChild(factBlock(task));
        row.classList.toggle('flow-open', !!expanded[key]);
        main.setAttribute('aria-expanded', expanded[key] ? 'true' : 'false');
      };
      var toggle = function (ev) {
        var t = ev && ev.target;
        // A click inside the action menu or on a button must not also collapse the row.
        if (t && typeof t.closest === 'function' && t.closest('.flow-menu, .flow-open-task')) return;
        expanded[key] = !expanded[key];
        renderFacts();
        if (rowCache[key]) rowCache[key].sig = signatureOf(task, expanded[key]);
      };
      main.addEventListener('click', toggle);
      main.tabIndex = 0;
      main.setAttribute('role', 'button');
      main.setAttribute('aria-expanded', expanded[key] ? 'true' : 'false');
      main.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ' || ev.key === 'Spacebar') { ev.preventDefault(); toggle(ev); }
      });

      var motion = motionFor(task);
      if (motion) { row.classList.add('flow-motion', 'flow-motion-' + motion); }
      return row;
    }

    // First-load failure: explicit and recoverable, never disguised as progress.
    function errorPanel(message) {
      var box = el('div', 'flow-empty flow-error-panel');
      box.appendChild(el('div', 'flow-error-title', '加载失败：' + message));
      box.appendChild(el('div', 'flow-line', '这里显示的不是实时数据。请检查连接后重试。'));
      var retry = el('button', 'flow-btn flow-retry', '重新加载');
      retry.type = 'button';
      retry.addEventListener('click', function () { controller.refresh(); });
      box.appendChild(retry);
      return box;
    }

    // An empty board and an empty filter result are different facts and must read differently.
    function renderEmpty(tasks, filtered) {
      var total = ((view.snapshot && view.snapshot.tasks) || []).length;
      var box = el('div', 'flow-empty');
      if (!total) box.appendChild(doc.createTextNode('还没有任务：当前注册项目里没有任何任务记录。'));
      else if (filtered) box.appendChild(doc.createTextNode('没有匹配的任务：' + total + ' 条任务被当前筛选条件排除。'));
      else box.appendChild(doc.createTextNode('没有可显示的任务。'));
      return box;
    }

    function renderBoard() {
      var focused = doc.activeElement;
      var focusedRow = focused && focused.closest ? focused.closest('.flow-task') : null;
      var focusKey = focusedRow && focusedRow.getAttribute('data-key');
      var focusAction = focused && focused.getAttribute ? focused.getAttribute('data-action') : null;
      if (!view.snapshot) {
        while (boardEl.firstChild) boardEl.removeChild(boardEl.firstChild);
        boardLane = null;
        // No data yet. A dead first request must say so instead of showing "loading" forever.
        renderBanner();
        if (view.error) {
          boardEl.appendChild(errorPanel(view.error));
        } else {
          boardEl.appendChild(el('div', 'flow-empty', '正在加载……'));
        }
        return;
      }
      renderProjects();
      var tasks = filterTasks(view.snapshot, { project: view.project, query: view.query, showDone: view.showDone });
      renderStats(tasks);
      renderMeta();
      renderBanner();
      if (!boardLane) {
        while (boardEl.firstChild) boardEl.removeChild(boardEl.firstChild);
        boardEl.appendChild(legendRow());
        boardLane = el('div', 'flow-lane');
        boardEl.appendChild(boardLane);
      }
      var nodes = tasks.map(reusableRow);
      if (!nodes.length) nodes.push(renderEmpty(tasks, !!view.query || view.project !== 'all' || view.showDone));
      if (boardLane.insertBefore) {
        nodes.forEach(function (node, i) {
          if (boardLane.children[i] !== node) boardLane.insertBefore(node, boardLane.children[i] || null);
        });
        Array.from(boardLane.children).forEach(function (node) {
          if (nodes.indexOf(node) < 0) boardLane.removeChild(node);
        });
      } else { // Minimal test DOMs without insertBefore.
        while (boardLane.firstChild) boardLane.removeChild(boardLane.firstChild);
        nodes.forEach(function (node) { boardLane.appendChild(node); });
      }
      if (focusKey && focused && !focused.isConnected) {
        var row = rowCache[focusKey] && rowCache[focusKey].node;
        var target = row && (focusAction ? Array.from(row.querySelectorAll('[data-action]')).find(function (n) {
          return n.getAttribute('data-action') === focusAction;
        }) : row.querySelector('.flow-main'));
        if (target) target.focus({preventScroll: true});
      }
    }

    // 复用：键为 projectRoot+id（同编号跨项目不会串），签名不变就挪回原来的节点。
    // 这样 5 秒刷新不会重置动画、丢滚动位置、抢焦点或折叠已展开的详情。
    function reusableRow(t) {
      var key = taskKeyOf(t);
      var open = !!expanded[key];
      var sig = signatureOf(t, open);
      var cached = rowCache[key];
      if (cached && cached.sig === sig) return cached.node;      // unchanged: keep the very same node
      var rowState = cached ? cached.state : { running: false, menuOpen: false, seenState: null, seenAttempt: null };
      var node = taskRow(t, rowState);
      // A real state change flashes once; the first appearance and unchanged rows stay quiet.
      if (cached && (rowState.seenState !== null && (rowState.seenState !== t.state || rowState.seenAttempt !== attemptKey(t)))) {
        node.classList.add('flow-changed');
        if (typeof setTimeout === 'function') setTimeout(function () { node.classList.remove('flow-changed'); }, 1400);
      }
      rowState.seenState = t.state;
      rowState.seenAttempt = attemptKey(t);
      var prev = cached && cached.node;
      if (prev && prev.parentNode) {
        var parent = prev.parentNode;
        if (typeof parent.replaceChild === 'function') parent.replaceChild(node, prev);
        else parent.removeChild(prev);
      }
      rowCache[key] = { sig: sig, node: node, state: rowState };
      return node;
    }

    // The overall pipeline, drawn once above the task rows, with the exception branch named explicitly.
    function legendRow() {
      var wrap = el('div', 'flow-legend');
      wrap.appendChild(el('span', 'flow-legend-lead', '整体流程'));
      PHASES.forEach(function (phase, i) {
        if (i) wrap.appendChild(el('span', 'flow-legend-arrow', '→'));
        wrap.appendChild(el('span', 'flow-legend-node', phase.label));
      });
      wrap.appendChild(el('span', 'flow-legend-arrow', '↺'));
      wrap.appendChild(el('span', 'flow-legend-node flow-legend-branch', '返工 / 受阻 → 回到执行'));
      return wrap;
    }

    function fullscreenSupported() {
      return !!(rootEl.requestFullscreen || rootEl.webkitRequestFullscreen ||
                (doc.documentElement && doc.documentElement.requestFullscreen));
    }

    function toggleFullscreen() {
      var active = doc.fullscreenElement || doc.webkitFullscreenElement;
      if (active) {
        var exit = doc.exitFullscreen || doc.webkitExitFullscreen;
        if (exit) { try { exit.call(doc); } catch (e) { /* ignore */ } }
        return;
      }
      var enter = rootEl.requestFullscreen || rootEl.webkitRequestFullscreen ||
                  (doc.documentElement && doc.documentElement.requestFullscreen);
      if (!enter) {
        hintEl.textContent = '当前环境不支持全屏，大屏仍可正常使用。';
        return;
      }
      hintEl.textContent = '';
      var result = enter.call(doc.fullscreenElement ? doc.fullscreenElement : rootEl);
      if (result && typeof result.catch === 'function') {
        result.catch(function () {
          hintEl.textContent = '进入全屏被浏览器拒绝，大屏仍可正常使用（按 F11 可全屏）。';
        });
      }
    }

    function onFullscreenChange() {
      var active = doc.fullscreenElement || doc.webkitFullscreenElement;
      if (fullBtn) fullBtn.textContent = active ? '退出全屏' : '全屏';
    }

    projectSel.addEventListener('change', function () { view.project = projectSel.value; renderBoard(); });
    searchIn.addEventListener('input', function () { view.query = searchIn.value; renderBoard(); });
    doneBox.addEventListener('change', function () { view.showDone = doneBox.checked; renderBoard(); });
    refreshBtn.addEventListener('click', function () { controller.refresh(); });
    fullBtn.addEventListener('click', toggleFullscreen);
    doc.addEventListener('fullscreenchange', onFullscreenChange);
    if (doc.addEventListener) doc.addEventListener('webkitfullscreenchange', onFullscreenChange);

    function isHidden() {
      return !!(doc.hidden || (typeof doc.visibilityState === 'string' && doc.visibilityState === 'hidden'));
    }

    var controller = createController({
      load: load,
      intervalMs: opts.intervalMs === undefined ? 5000 : opts.intervalMs,
      setInterval: opts.setInterval, clearInterval: opts.clearInterval,
      onData: function (payload) {
        view.snapshot = payload.snapshot || view.snapshot;
        view.stale = payload.stale;
        view.error = payload.error;
        renderBoard();
      },
      onError: function () { /* surfaced through onData */ },
    });

    function visibilityHandler() { controller.setPaused(isHidden()); }
    if (doc.addEventListener) doc.addEventListener('visibilitychange', visibilityHandler);

    // Keep the checkbox and search box in sync when the page re-renders the toolbar.
    renderBoard();

    return {
      start: function () {
        doneBox.checked = view.showDone;
        searchIn.value = view.query;
        return controller.start();
      },
      stop: function () {
        return controller.stop();
      },
      refresh: function () { return controller.refresh(); },
      destroy: function () {
        controller.stop();
        if (doc.removeEventListener) doc.removeEventListener('visibilitychange', visibilityHandler);
        if (doc.removeEventListener) {
          doc.removeEventListener('click', menuOutsideClick);
          doc.removeEventListener('keydown', menuEscape);
          doc.removeEventListener('fullscreenchange', onFullscreenChange);
          doc.removeEventListener('webkitfullscreenchange', onFullscreenChange);
        }
        if (rootEl.parentNode) rootEl.parentNode.removeChild(rootEl);
        if (container.classList) container.classList.remove('flow-container');
      },
      model: { filterTasks: filterTasks, phaseFor: phaseFor, chainFor: chainFor, statsOf: statsOf, alertsOf: alertsOf },
    };
  }

  // motionFor / signatureOf are internal to the renderer but exported so tests can assert the animation
  // contract (real evidence only, and refresh must not restart it) without scraping the DOM.
  function signatureOfForTest(t, isOpen) {
    return JSON.stringify([
      t.state, t.title, t.level, t.from, t.to, t.projectName, t.rejects,
      t.origin || null, t.escalation ? t.escalation.to : null,
      t.updated || null, t.dispatch ? [t.dispatch.state, t.dispatch.stalled, t.dispatch.reason] : null,
      t.aiReview ? [t.aiReview.status, t.aiReview.stalled, t.aiReview.startedAt, t.aiReview.attempts,
                   t.aiReview.nextRetryAt, t.aiReview.error, t.aiReview.sessionTitle] : null,
      t.gate ? [t.gate.checksOk, t.gate.outside] : null,
      t.recall || null,
      t.finalReview ? [t.finalReview.owner, t.finalReview.overdue, t.finalReview.reminders,
                       t.finalReview.ageSeconds] : null,
      t.actions || null, t.review ? t.review.note : null,
      t.receipt ? [t.receipt.summary, t.receipt.checks, t.receipt.limits] : null,
      t.lastEvent ? t.lastEvent.seq : null,
      isOpen ? 1 : 0,
    ]);
  }

  return {
    mount: mount,
    createController: createController,
    filterTasks: filterTasks,
    phaseFor: phaseFor,
    chainFor: chainFor,
    statsOf: statsOf,
    alertsOf: alertsOf,
    motionFor: motionFor,
    effectFor: effectFor,
    signatureOf: signatureOfForTest,
    // 任务操作：面板从 window.AgentLinkFlow.actions 取，Node 测试 require flow.js 直接拿到。
    actions: {
      ACTIONS: ACTIONS, AGENTS: AGENTS,
      spec: spec, planAction: planAction, needsConfirm: needsConfirm, describeConfirm: describeConfirm,
      humanizeAge: humanizeAge, ownerLabel: ownerLabel, finalReviewText: finalReviewText,
      waitingTasks: waitingTasks, createRunner: createRunner, stateAfter: stateAfter,
    },
    PHASES: PHASES, PHASE_INDEX: PHASE_INDEX, STATE: STATE, AI_STATUS: AI_STATUS,
    LEVELS: LEVELS, AGENTS: AGENTS, TERMINAL: TERMINAL,
    stateLabel: stateLabel, aiLabel: aiLabel, agentLabel: agentLabel,
  };
}));
