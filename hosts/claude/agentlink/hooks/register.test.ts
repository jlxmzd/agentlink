import { expect, mock, test } from 'claude-code/testing'

const ENV = { AGENTLINK_PYTHON: 'python', AGENTLINK_CLI: 'E:/AgentLink/agentlink.py' }

test('the poll wakes the session when the bus says so, and only then', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, ENV)
  const calls: string[][] = []
  let answer = { ids: [] as string[], wake: false }
  on('process.run', async (_$, e) => {
    calls.push([...e.argv])
    return { value: { exitCode: 0, stdout: JSON.stringify(answer), stderr: '' } }
  })
  on('session.id', async () => ({ value: 'sess-1' }))
  on('session.cwd', async () => ({ value: 'E:/proj' }))
  on('command.register', async (_$, e) => ({ value: { command: e.name } }))
  const submitted: string[] = []
  on('prompt.submit', async (_$, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('session.start', async (_$, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: 'E:/proj', surface: null, isInteractive: true })

  await clock.advance(30000)
  const poll = calls.find(argv => argv.includes('pending'))
  expect(poll).toEqual(['python', 'E:/AgentLink/agentlink.py', '--as', 'claude', '--cwd', 'E:/proj',
    'pending', '--claim', '--session', 'sess-1'])
  expect(submitted.length).toBe(0)

  answer = { ids: ['T-20261024-09'], wake: true }
  await clock.advance(30000)
  expect(submitted.filter(t => t.startsWith('AgentLink：T-20261024-09')).length).toBe(1)
})
