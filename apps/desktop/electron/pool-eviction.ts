// LRU cap accounting for the desktop backend pool.
//
// The pool holds two very different kinds of entries under one Map:
//   1. SPAWNED local profile backends — a real child process each (the thing
//      the POOL_MAX_BACKENDS cap exists to bound).
//   2. Process-less connection DESCRIPTORS — remote/cloud registry sources and
//      per-profile remote overrides (`entry.process === null`). These hold no
//      local process; their only cost is a cached descriptor.
//
// Counting both kinds against the cap meant a roster refresh across N
// registered remote connections could push the Map size over the cap and
// LRU-evict a REAL spawned backend that had merely been idle past the
// keepalive window. Cap accounting (and cap-driven eviction) therefore only
// considers entries with a live child process; descriptor entries remain
// subject to the idle reaper, just not to the process cap.

export interface PoolEvictionEntry {
  lastActiveAt?: null | number
  process?: unknown
}

export interface SelectPoolEvictionsOptions<K> {
  /**
   * 'soft' (default): background cap-convergence — spare any backend touched
   * within `freshMs` (a kept-alive pool may exceed the soft cap rather than
   * kill a running session; #95189).
   *
   * 'foreground': a user is opening a bot RIGHT NOW and the pool is at cap.
   * The keepalive-fresh guard no longer applies — the renderer pings every
   * open bot's backend every 60s, so with >= maxBackends bots open NOTHING is
   * ever `freshMs`-stale and the click can never get a slot (the Dev /
   * Research / Review / Growth "hard open failure"). Instead spare only a
   * backend touched within `minIdleMs` (a running turn touches on every
   * streamed chunk; an idle-but-kept-alive one only every 60s), so an LRU
   * idle backend yields its slot to the bot being opened while a mid-turn
   * one is left alone.
   */
  mode?: 'foreground' | 'soft'
  /** foreground mode only: spare a backend touched within this window
   *  (default 15s — comfortably longer than a turn's inter-token gap,
   *  shorter than the 60s keepalive cadence). */
  minIdleMs?: number
  /** Never evict these keys (e.g. the primary profile). */
  protect?: Iterable<K>
}

/**
 * Pick which pool keys the LRU cap should evict so that at most `keep`
 * SPAWNED backends remain. Only entries with a live child process count
 * toward the cap or are eligible for cap eviction. In the default 'soft'
 * mode only entries idle beyond `freshMs` may be evicted; 'foreground' mode
 * (a live user navigation that needs a slot now) relaxes that to `minIdleMs`
 * so a kept-alive-but-idle backend yields its slot instead of the open
 * failing outright. LRU order and the `keep` floor are identical in both.
 */
export function selectPoolEvictions<K>(
  entries: Iterable<[K, PoolEvictionEntry]>,
  keep: number,
  now: number,
  freshMs: number,
  options: SelectPoolEvictionsOptions<K> = {}
): K[] {
  const protectedKeys = new Set<K>(options.protect ?? [])
  const spawned = [...entries].filter(([key, entry]) => Boolean(entry.process) && !protectedKeys.has(key))

  if (spawned.length <= keep) {
    return []
  }

  const idleGate = options.mode === 'foreground' ? Math.max(0, options.minIdleMs ?? 15_000) : freshMs

  const evictable = spawned
    .filter(([, entry]) => now - (entry.lastActiveAt || 0) > idleGate)
    .sort((a, b) => (a[1].lastActiveAt || 0) - (b[1].lastActiveAt || 0))

  let removable = spawned.length - Math.max(0, keep)
  const evictions: K[] = []

  for (const [key] of evictable) {
    if (removable <= 0) {
      break
    }

    evictions.push(key)
    removable -= 1
  }

  return evictions
}
