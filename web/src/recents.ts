import type { GeocodeResult } from './api'

/** Recently-entered addresses, purely local (localStorage, nothing sent
 * anywhere). Only deliberate entries of a completed route get recorded —
 * a picked suggestion or resolved typed text, committed the moment a
 * route draws (useAddressField holds the pending entry; Controls
 * commits on route arrival). Map taps and location fills never land
 * here: their reverse-geocoded "nearest thing" labels are noise, not
 * something the user chose to type. */

const STORAGE_KEY = 'sw-recents'
export const RECENTS_CAP = 5

function isRecent(value: unknown): value is GeocodeResult {
  if (typeof value !== 'object' || value === null) return false
  const entry = value as Record<string, unknown>
  return typeof entry.label === 'string' && typeof entry.lat === 'number' && typeof entry.lon === 'number'
}

/** Newest first, at most RECENTS_CAP. Anything unreadable — storage
 * blocked (private mode), corrupt JSON, entries missing fields — comes
 * back as simply no recents; this feature never gets to break the app. */
// `window.localStorage`, never bare `localStorage`: Node 22+ defines its
// own global localStorage stub (undefined without --localstorage-file)
// that shadows jsdom's under Vitest. Identical in the browser.
export function loadRecents(): GeocodeResult[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(isRecent).slice(0, RECENTS_CAP)
  } catch {
    return []
  }
}

/** Prepends an entry, deduplicating by label (case-insensitive) so a
 * repeat visit moves an address to the front — with its fresh
 * coordinates — instead of listing it twice. */
export function recordRecent(entry: GeocodeResult): void {
  const label = entry.label.trim()
  if (label === '') return
  const rest = loadRecents().filter((r) => r.label.toLowerCase() !== label.toLowerCase())
  const next = [{ label, lat: entry.lat, lon: entry.lon }, ...rest].slice(0, RECENTS_CAP)
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
  } catch {
    // Storage full or blocked: the recent just doesn't persist.
  }
}
