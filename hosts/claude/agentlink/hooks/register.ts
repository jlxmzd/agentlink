import type { EngineInterface, Register } from 'claude-code'

// All coordination lives in agentlink.py; this module only relays. The installer sets both variables
// in ~/.claude/settings.json because a module cannot see its own folder.
// How often an idle session asks the bus whether another agent left it work. One local process call, no model.
const POLL_MS = 30000

// Shell-like split that keeps "quoted summaries" whole, without a shell.
const split = (text: string): string[] =>
  [...text.matchAll(/"([^"]*)"|(\S+)/g)].map(m => m[1] ?? m[2] ?? '')

// The command line every call starts with, plus this conversation's identity and directory:
// the bus answers only the conversation bound in the panel (or any, while Claude is unbound).
// Declared at the top of the file: the engine only lets `$` be handed to such functions.
async function base($: EngineInterface): Promise<{ argv: string[]; session: string } | undefined> {
  const python = (await $.env.get('AGENTLINK_PYTHON')) || 'python'
  const script = await $.env.get('AGENTLINK_CLI')
  if (!script) return undefined
  const session = await $.session.id()
  const cwd = await $.session.cwd()
  return { argv: [python, script, '--as', 'claude', '--cwd', cwd], session }
}

export const register: Register = on => {
  let isFirstPrompt = true
  let isPolling = false

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.command.register({
      name: 'agentlink',
      description: 'AgentLink 三方任务：list / show ID / post / accept / reject / reassign / locks',
      argumentHint: 'list | locks | show <ID> | accept <ID> | reject <ID> --note "…"',
    })
    // Self-wake: when Codex or DeepSeek hands this conversation work or delivers for review, start a turn.
    // agentlink.py decides (bound session only, new work, settled, debounced, hourly cap, panel switch).
    $.clock.every(POLL_MS, () => {
      if (isPolling) return
      isPolling = true
      void (async () => {
        try {
          const call = await base($)
          if (!call) return
          const ran = await $.process.run([...call.argv, 'pending', '--claim', '--session', call.session], {
            timeoutMs: 20000,
          })
          if (ran.exitCode !== 0) return
          const pending = JSON.parse(ran.stdout) as { ids?: string[]; wake?: boolean }
          if (!pending.wake || !pending.ids?.length) return
          await $.prompt.submit({
            // Fixed text and task IDs only; the details arrive inline through the prompt.submit hook below.
            text: `AgentLink：${pending.ids.join('、')} 需要你处理。详情已附在本轮上下文，无需再 list / show；处理完只回一行。`,
          })
        } catch {
          // A failed poll is retried on the next tick; it must never surface in the session.
        } finally {
          isPolling = false
        }
      })()
    })
    return started
  })

  // Attach only what changed for this conversation since its last prompt; nothing at all when quiet.
  on('prompt.submit', async ($, e, next) => {
    const event = isFirstPrompt ? 'SessionStart' : 'UserPromptSubmit'
    isFirstPrompt = false
    const call = await base($).catch(() => undefined)
    const ran = call
      ? await $.process
          .run([...call.argv, 'hook', '--plain', '--event', event, '--session', call.session], { timeoutMs: 20000 })
          .catch(() => undefined)
      : undefined
    const text = ran?.exitCode === 0 ? ran.stdout.trim() : ''
    return next(text ? { ...e, context: [...(e.context ?? []), text] } : e)
  }).catch(($, e, next) => next(e)) // a failed relay never holds the person's prompt back

  on('command.run', { command: 'agentlink' }, async ($, e) => {
    const call = await base($)
    if (!call) return { text: 'AgentLink 未安装：在管理面板的「宿主插件」里安装 Claude 插件后重启。', exitCode: 1 }
    const args = split(e.args.trim())
    const ran = await $.process.run([...call.argv, ...(args.length > 0 ? args : ['list'])], { timeoutMs: 60000 })
    return { text: `${ran.stdout}${ran.stderr}`.trim() || '（无输出）', exitCode: ran.exitCode }
  })
}
