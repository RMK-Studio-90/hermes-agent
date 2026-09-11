/**
 * notifyBotOpenFailure — how an open failure is surfaced to the user.
 *
 * Regression for the Dev / Research / Review / Growth "does not open at all"
 * bug: the profile backend could not be spawned because the desktop's local
 * backend pool was at its concurrency cap and every pooled backend was
 * mid-turn, so no slot could be freed. The raw failure ("… timed out while
 * waiting for a free slot") was previously shown under the caller's generic
 * "Could not reach the gateway" fallback, which reads as a network outage.
 * It must instead surface the actionable capacity wording verbatim.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { RosterRow } from './types'

const { hostMock } = vi.hoisted(() => ({
  hostMock: { notify: vi.fn(), notifyError: vi.fn() }
}))

vi.mock('@hermes/plugin-sdk', () => ({
  BOT_CHAT_SESSION_HYDRATION_TIMEOUT_MS: 15_000,
  host: hostMock
}))

vi.mock('./routing', () => ({
  backendTargetProfile: (_route: unknown, name: string) => name,
  botConnectionRoute: () => null,
  botRosterMeta: () => ({}),
  botWorkspaceOwnerKey: () => 'bot:ops',
  requestForBot: vi.fn()
}))

vi.mock('./data', () => ({
  $botMeta: { get: () => ({}), set: vi.fn() },
  botMetaKey: () => 'ops',
  botOwner: (owner: RosterRow | string) =>
    typeof owner === 'string'
      ? { bot: { name: owner }, key: owner, name: owner, route: null }
      : { bot: owner, key: owner?.name, name: owner?.name, route: null },
  persistBotMetaSnapshot: vi.fn(),
  saveBotMeta: vi.fn()
}))

vi.mock('./shared', () => ({ getPluginCtx: () => null }))

const bot = { connectionId: 'local', connectionLabel: 'This device', name: 'rmk-dev' } as RosterRow

beforeEach(() => {
  vi.clearAllMocks()
})

describe('notifyBotOpenFailure', () => {
  it('surfaces the backend-pool-exhausted message verbatim, not the generic "could not reach" fallback', async () => {
    const { notifyBotOpenFailure } = await import('./canonical-chat')

    const error = new Error(
      'Can\'t open "rmk-dev" yet: all 3 bot backends are busy with active turns. ' +
        'Wait for one to finish, close a bot chat, or raise the limit in Settings → Advanced → Backend pool.'
    )

    notifyBotOpenFailure(error, bot, 'Could not reach the gateway')

    expect(hostMock.notifyError).not.toHaveBeenCalled()
    expect(hostMock.notify).toHaveBeenCalledTimes(1)
    expect(hostMock.notify.mock.calls[0][0]).toMatchObject({
      kind: 'error',
      title: 'Too many bot backends running'
    })
    expect(hostMock.notify.mock.calls[0][0].message).toContain('busy with active turns')
  })

  it('also catches the lower-level coordinator wording ("free slot")', async () => {
    const { notifyBotOpenFailure } = await import('./canonical-chat')

    notifyBotOpenFailure(
      new Error('Local backend start for "rmk-review" timed out while waiting for a free slot.'),
      bot,
      'Could not reach the gateway'
    )

    expect(hostMock.notify).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'error', title: 'Too many bot backends running' })
    )
    expect(hostMock.notifyError).not.toHaveBeenCalled()
  })

  it('leaves every other failure on the generic fallback path', async () => {
    const { notifyBotOpenFailure } = await import('./canonical-chat')

    const error = new Error('Timed out loading rmk-dev\'s session history.')
    notifyBotOpenFailure(error, bot, 'Could not open Dev\'s chat — try again')

    expect(hostMock.notify).not.toHaveBeenCalled()
    expect(hostMock.notifyError).toHaveBeenCalledWith(error, 'Could not open Dev\'s chat — try again')
  })

  it('still routes an outdated-gateway error to the update prompt', async () => {
    const { notifyBotOpenFailure } = await import('./canonical-chat')

    notifyBotOpenFailure(new Error('no handler for session.list'), bot, 'fallback')

    expect(hostMock.notify).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Update this gateway to use Bot Mode' })
    )
    expect(hostMock.notifyError).not.toHaveBeenCalled()
  })
})
