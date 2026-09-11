/**
 * Regression: the BotChat session-selection failure.
 *
 * Reported symptoms — clicking a bot does nothing, the UI shows
 * "Waking up default…" then "Could not open <bot>'s chat — try again" /
 * "Session open was superseded by a newer selection.", affecting every bot,
 * with the user forced to manually wake bots.
 *
 * Proven root cause: `openSessionGeneration` in `sdk/index.ts` was one
 * process-global counter that EVERY `host.openSession` bumped, so the 5s
 * roster poll re-pulling the open chat's transcript (`refreshOpenBotChat`)
 * and the `session.reclaimed` mass-reap re-resume both cancelled a bot click
 * that was still hydrating. The fix marks those self-initiated opens
 * `intentSource: 'background'` so they no longer advance the user-selection
 * generation.
 *
 * This suite pins the click-path contract at the plugin seam
 * (`openRosterBot`): a genuine user selection always wins, a stale/older
 * open is discarded rather than applied, a failed wake is retryable, and a
 * persisted selection never opens anything on its own.
 */

import type * as HermesSdk from '@hermes/plugin-sdk'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { RosterRow } from './types'

const { ackStoredSessionId, notifyBotOpenFailure, openBotCanonicalChat, prepareBotSource } = vi.hoisted(() => ({
  ackStoredSessionId: vi.fn(),
  notifyBotOpenFailure: vi.fn(),
  openBotCanonicalChat: vi.fn(),
  prepareBotSource: vi.fn(async () => undefined)
}))

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof HermesSdk>()

  return { ...sdk, ackStoredSessionId }
})

vi.mock('./canonical-chat', () => ({
  CANONICAL_CHAT_TITLE: 'Bot Chat',
  ensureBotMetadata: vi.fn(async () => ({})),
  notifyBotOpenFailure,
  openBotCanonicalChat,
  prepareBotSource,
  PROFILE_SESSION_LIST_LIMIT: 200
}))

const { $openBotChat } = await import('./bot-state')
const { openRosterBot } = await import('./roster-actions')

const bot = (name: string): RosterRow => ({ connectionId: 'local', name }) as RosterRow

/** Resolve every canonical open by the owner's name, so ordering — not
 *  mock-call sequence — decides which selection is applied. */
function resolveCanonicalByName() {
  openBotCanonicalChat.mockImplementation(async (owner: RosterRow | string) => {
    const name = typeof owner === 'string' ? owner : owner.name

    return { openedId: `tip-${name}`, registryId: `reg-${name}` }
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  $openBotChat.set(null)
  prepareBotSource.mockImplementation(async () => undefined)
})

describe('a genuine user selection always wins over an older in-flight open', () => {
  it('startup default wake → user picks another bot: the default wake result is discarded', async () => {
    resolveCanonicalByName()

    // Both clicks run to their first await synchronously; the SECOND bumps the
    // open generation, so the first is stale by the time its canonical open
    // resolves and must never be applied.
    const first = openRosterBot(bot('default'))
    const second = openRosterBot(bot('ops'))

    expect(await second).toBe(true)
    expect(await first).toBe(false)
    expect($openBotChat.get()).toMatchObject({ openedRegistryId: 'reg-ops', openedSessionId: 'tip-ops' })
  })

  it('a user selection made while the default wake is still in flight takes over', async () => {
    let releaseDefault: (value: { openedId: string; registryId: string }) => void = () => undefined

    openBotCanonicalChat
      .mockImplementationOnce(
        () =>
          new Promise(resolve => {
            releaseDefault = resolve
          })
      )
      .mockImplementationOnce(async () => ({ openedId: 'tip-ops', registryId: 'reg-ops' }))

    const first = openRosterBot(bot('default'))
    await Promise.resolve()
    const second = await openRosterBot(bot('ops'))

    expect(second).toBe(true)
    expect($openBotChat.get()).toMatchObject({ openedRegistryId: 'reg-ops' })

    // The default wake finally lands — its result is dropped, not painted over
    // the bot the user actually chose.
    releaseDefault({ openedId: 'tip-default', registryId: 'reg-default' })
    await first
    expect($openBotChat.get()).toMatchObject({ openedRegistryId: 'reg-ops' })
  })

  it('rapid A → B → C ends with C open and no error toast', async () => {
    resolveCanonicalByName()

    const a = openRosterBot(bot('a'))
    const b = openRosterBot(bot('b'))
    const c = openRosterBot(bot('c'))

    await Promise.all([a, b, c])

    expect(await c).toBe(true)
    expect($openBotChat.get()).toMatchObject({ openedRegistryId: 'reg-c', openedSessionId: 'tip-c' })
    expect(notifyBotOpenFailure).not.toHaveBeenCalled()
  })
})

describe('opening the same bot is idempotent', () => {
  it('a repeated open of the same bot never toasts and never corrupts the claim', async () => {
    resolveCanonicalByName()

    await openRosterBot(bot('ops'))
    await openRosterBot(bot('ops'))
    await openRosterBot(bot('ops'))

    expect($openBotChat.get()).toMatchObject({ openedRegistryId: 'reg-ops', openedSessionId: 'tip-ops' })
    expect(notifyBotOpenFailure).not.toHaveBeenCalled()
  })
})

describe('a failed wake is reported once and is retryable', () => {
  it('surfaces "try again" on a transient registry failure, then succeeds on retry', async () => {
    openBotCanonicalChat
      .mockRejectedValueOnce(new Error("Could not check ops's Bot Chat registry — not starting a new chat"))
      .mockResolvedValueOnce({ openedId: 'tip-ops', registryId: 'reg-ops' })

    const failed = await openRosterBot(bot('ops'))

    expect(failed).toBe(false)
    expect($openBotChat.get()).toBeNull()
    expect(notifyBotOpenFailure).toHaveBeenCalledTimes(1)
    expect(notifyBotOpenFailure.mock.calls[0][2]).toMatch(/try again/i)

    const ok = await openRosterBot(bot('ops'))

    expect(ok).toBe(true)
    expect($openBotChat.get()).toMatchObject({ openedRegistryId: 'reg-ops' })
  })
})

describe('session restore is presentation-only', () => {
  it('setting the persisted roster selection never opens a chat (no manual wake, no auto wake)', async () => {
    const { $selectedRosterKey, $selectedBot } = await import('./bot-state')

    $selectedRosterKey.set('local::ops')
    $selectedBot.set('ops')
    await Promise.resolve()

    // Restoring the selection paints the roster highlight and nothing else —
    // the only opener is a real click through openRosterBot.
    expect(openBotCanonicalChat).not.toHaveBeenCalled()
    expect($openBotChat.get()).toBeNull()
  })
})
