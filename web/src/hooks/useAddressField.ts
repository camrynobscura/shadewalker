import { useEffect, useRef, useState } from 'react'
import { geocode, GeocodeUnavailableError, reverseGeocode, type GeocodeResult, type Point } from '../api'
import { formatCoords } from '../format'
import { loadRecents, recordRecent } from '../recents'
import { useGeocodeSuggestions } from './useGeocodeSuggestions'

/** 'unavailable' = the search itself failed (proxy/Photon down, no
 * network) — a different sentence from 'notfound', which is the search
 * working and finding nothing. */
type FieldStatus = 'idle' | 'searching' | 'notfound' | 'unavailable' | 'found'

/** Owns one address field's query/status and how to resolve it. A hook,
 * not a component, because Controls needs two independent copies (start,
 * end) that a single shared "find route" submit can resolve together.
 *
 * `externalPoint` is the other direction: a point that landed in `start`/
 * `end` from outside this field's own resolve() -- a map click, or the
 * USE_LOCATION button -- which this field still needs to show *something*
 * for, even though no address text ever got typed. `shownPointRef` is
 * what tells those two directions apart: it's the point (if any) that the
 * current `query` text already represents, kept in a ref rather than
 * state since updating it must never itself trigger a render. Compared by
 * value against `externalPoint` (not object identity) because a point
 * this field resolved itself round-trips back down through the parent as
 * a new object with the same lat/lon -- reference equality would treat
 * that as "a new point," and redundantly reverse-geocode text that's
 * already better than anything reverse-geocoding would produce. */
export function useAddressField(
  onResolve: (p: Point | null) => void,
  externalPoint: Point | null,
  /** Reports the display text whenever it settles into representing the
   * resolved point — a picked suggestion's label, resolved typed text, a
   * reverse-geocode name. App mirrors it to the URL beside the point, so
   * a reload shows the SAME text instead of re-deriving a (often
   * different) name from the bare coordinate. */
  onLabel: (label: string) => void,
  /** The label a reload restored for `externalPoint` (from the URL) —
   * read once, at mount: with it, the field starts out already showing
   * the stored text and the reverse-geocode round trip never happens. */
  initialLabel: string | null,
) {
  const restored = initialLabel !== null && externalPoint !== null
  const [query, setQuery] = useState(restored ? initialLabel : '')
  const [status, setStatus] = useState<FieldStatus>(restored ? 'found' : 'idle')
  // The dropdown is wanted only while the current text is something the
  // user TYPED (suggestions), or while a focused field is EMPTY (recents)
  // -- a suggestion pick, a submit, or a programmatic fill (map click,
  // reverse geocode) all turn this off, so the dropdown never reopens
  // over text this code wrote itself.
  const [suggestOn, setSuggestOn] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  // Read fresh each time the dropdown opens (openSuggestions), so a pick
  // made in the OTHER field is already in this one's list.
  const [recents, setRecents] = useState<GeocodeResult[]>([])
  // Starts as the restored point when a label came back from the URL —
  // that's what stops the mount effect below from reverse-geocoding over
  // the restored text.
  const shownPointRef = useRef<Point | null>(restored ? externalPoint : null)
  // The deliberate entry (suggestion pick / resolved typed text) behind
  // this field's CURRENT point, held until a route completes —
  // commitRecent() records it then. Recents mean "addresses from real
  // routes", not everything ever typed (user call 2026-09-03); nulled
  // whenever the point it described is cleared or replaced from outside.
  // Not set on mount for URL-restored labels: a deliberate one was
  // already recorded when its route first drew.
  const pendingRecentRef = useRef<GeocodeResult | null>(null)
  // Bumped whenever the text an in-flight resolve() was about stops
  // being current (cleared, retyped) — the response is then stale and
  // gets dropped instead of refilling the field (the ✕-mid-lookup
  // resurrection, caught 2026-09-03).
  const resolveSeqRef = useRef(0)
  // The lookup resolve() has in flight, so a second caller waits for it
  // instead of starting another: FIND_ROUTE's tap blurs this field (which
  // resolves) a moment before its click asks every field to resolve, and
  // it must wait for THAT answer before routing.
  const inflightRef = useRef<Promise<void> | null>(null)
  // Previous externalPoint, so the effect below can tell a point being
  // CLEARED (value -> null) from the steady "no point yet" state while
  // someone types a fresh address.
  const prevExternalRef = useRef<Point | null>(externalPoint)

  const suggestions = useGeocodeSuggestions(query, suggestOn)

  // What the listbox holds right now: recents while the text is empty
  // (the slot that used to show nothing), live suggestions once there's
  // typed text. One list at a time — the keyboard/highlight machinery
  // below only ever sees `options`.
  const showingRecents = query.trim() === ''
  const options = showingRecents ? recents : suggestions

  // A fresh option list starts with nothing highlighted -- keeping an
  // old index would silently point Enter at whatever happens to occupy
  // that position now. Reset DURING RENDER (React's "adjusting state when
  // a prop changes" pattern), so the new list and its reset reach the
  // screen together. It was an effect until 2026-09-26, and an effect
  // runs a beat after the list is already showing: an ArrowDown landing
  // in that gap was wiped a moment later, so Enter found nothing
  // highlighted and resolved the empty text instead (the flaky
  // recent-addresses e2e on CI; reproduced by pressing ArrowDown the
  // instant the list rendered).
  const [highlightedList, setHighlightedList] = useState(options)
  if (highlightedList !== options) {
    setHighlightedList(options)
    setActiveIndex(-1)
  }

  function onChange(value: string) {
    setQuery(value)
    setStatus('idle')
    setSuggestOn(true)
    shownPointRef.current = null // free-typed text no longer matches any known point
    resolveSeqRef.current++ // any in-flight lookup is about older text now
  }

  /** A picked suggestion already carries its point -- no second geocode
   * round trip on submit; the field behaves exactly as if resolve() had
   * just succeeded with this result. Also serves picking a RECENT (a
   * recent is a stored GeocodeResult) — re-committing one just bumps it
   * back to the front of the list. */
  function selectSuggestion(suggestion: GeocodeResult) {
    setSuggestOn(false)
    setQuery(suggestion.label)
    setStatus('found')
    shownPointRef.current = { lat: suggestion.lat, lon: suggestion.lon }
    pendingRecentRef.current = suggestion
    onResolve({ lat: suggestion.lat, lon: suggestion.lon })
    onLabel(suggestion.label)
  }

  /** Records the pending deliberate entry, if any. Called by Controls
   * the moment a route exists — never before, so a lone entry in one
   * field (or an abandoned one) doesn't reach the recents. One-shot:
   * nulled after recording, so preset switches (which swap `selected`
   * without a new geocode) can't re-record. */
  function commitRecent() {
    if (pendingRecentRef.current) {
      recordRecent(pendingRecentRef.current)
      pendingRecentRef.current = null
    }
  }

  function closeSuggestions() {
    setSuggestOn(false)
  }

  /** Leaving an emptied field drops the point it stood for -- the marker
   * shouldn't outlive the text (user report 2026-08-31). Only on blur,
   * never per-keystroke, so retyping an address doesn't nuke the marker
   * mid-edit. Guarded on externalPoint so tabbing through an
   * already-empty field does nothing.
   *
   * Blur is also where typed text gets geocoded since 2026-09-02 — the
   * FIND_ROUTE button's old job, moved to the moment attention leaves
   * the field (the button was dead weight once suggestion picks and map
   * taps auto-routed). `abandon` skips that: CANCEL and Escape end the
   * mobile search WITHOUT acting on half-typed text. */
  function onBlur(abandon = false) {
    setSuggestOn(false)
    if (query.trim() === '' && externalPoint) {
      shownPointRef.current = null
      onResolve(null)
      return
    }
    if (!abandon) void resolve()
  }

  /** The per-field ✕: text, point, and marker drop together — one field's
   * worth of the old CLEAR_ROUTE (removed 2026-09-02; clearing both is
   * two taps, or the wordmark's full reset). */
  function clearField() {
    setQuery('')
    setStatus('idle')
    setSuggestOn(false)
    shownPointRef.current = null
    pendingRecentRef.current = null
    resolveSeqRef.current++ // a lookup still in flight is for cleared text — drop its answer
    onResolve(null)
  }

  /** Opens the dropdown: ArrowDown on a closed field (ARIA combobox
   * convention — the hook refetches for the unchanged text after its
   * debounce), and AddressField's empty-while-focused effect (recents).
   * Reloads recents each time so the list is fresh however it opens —
   * including a pick just made in the OTHER field. */
  function openSuggestions() {
    setRecents(loadRecents())
    setSuggestOn(true)
  }

  // The status guard blocks a repeat: onChange resets status back to
  // 'idle' on every keystroke, so this only re-fires once there's actually
  // new text to resolve — not every time Enter is pressed again. The one
  // exception is 'unavailable': an outage is worth retrying on the same
  // text, so Enter (or a blur) tries again without retyping.
  function resolve(): Promise<void> {
    if (inflightRef.current) return inflightRef.current
    const run = lookUp().finally(() => {
      if (inflightRef.current === run) inflightRef.current = null
    })
    inflightRef.current = run
    return run
  }

  async function lookUp() {
    setSuggestOn(false) // submitting is the end of the suggestion phase
    if (!query.trim() || (status !== 'idle' && status !== 'unavailable')) return
    setStatus('searching')
    const seq = resolveSeqRef.current
    let result: GeocodeResult | null
    try {
      result = await geocode(query)
    } catch (err) {
      if (!(err instanceof GeocodeUnavailableError)) throw err
      // Same staleness checks as the success path below: an answer about
      // text that's gone, or a point that landed meanwhile, changes nothing.
      if (seq !== resolveSeqRef.current || shownPointRef.current) return
      setStatus('unavailable')
      return
    }
    // The text this lookup was about is gone (✕, retyped, cleared from
    // outside) — the late answer must not refill the field it was
    // cleared out of.
    if (seq !== resolveSeqRef.current) return
    // A point that landed while the lookup was in flight — a map tap, a
    // picked suggestion, USE_LOCATION — supersedes the typed text this
    // resolve started from; drop the response instead of stomping it.
    // (Those paths already set query/status, so bailing leaves the field
    // consistent.) Likelier now that blur triggers resolve (2026-09-02).
    if (shownPointRef.current) return
    if (result) {
      setStatus('found')
      shownPointRef.current = { lat: result.lat, lon: result.lon }
      // The typed text is the pending recent's label too (not the
      // geocoder's), matching what the field keeps showing and the URL
      // restores.
      pendingRecentRef.current = { label: query, lat: result.lat, lon: result.lon }
      onResolve({ lat: result.lat, lon: result.lon })
      // The field keeps showing the TYPED text after a resolve (not the
      // geocoder's label), so that text is what the URL must restore.
      onLabel(query)
    } else {
      setStatus('notfound')
    }
  }

  useEffect(() => {
    const prev = prevExternalRef.current
    prevExternalRef.current = externalPoint

    if (!externalPoint) {
      // Point cleared from outside (a map tap starting a fresh pair drops
      // the end point; the ✕; this field emptied and blurred) -- empty the
      // text so field and map never disagree (the old CLEAR_ROUTE button
      // once left the addresses behind, user report 2026-08-31). Guarded
      // on `prev` so it fires only on the value->null transition, never on
      // the steady no-point state while a fresh address is being typed.
      // suggestOn is left alone: a blurred field already has it off, and
      // on a still-focused one (the ✕) forcing it off here would close
      // the recents that AddressField's emptied-while-focused effect
      // just opened — this effect runs a render behind it.
      if (prev) {
        setQuery('')
        setStatus('idle')
        shownPointRef.current = null
        pendingRecentRef.current = null
        resolveSeqRef.current++ // any in-flight lookup is for text that just got cleared
      }
      return
    }
    const shown = shownPointRef.current
    if (shown && shown.lat === externalPoint.lat && shown.lon === externalPoint.lon) return

    shownPointRef.current = externalPoint
    pendingRecentRef.current = null // this point wasn't typed or picked — it must not become a recent
    setSuggestOn(false) // the text below is generated, not typed
    // Coordinates first, instantly -- reverse-geocoding is a real network
    // round trip (measured ~70-100ms once warm, up to ~1s on a session's
    // first call), and the field showing nothing while a marker's already
    // on the map would look broken. Also doubles as the fallback if the
    // lookup below fails outright (open water, the geocoder down).
    setQuery(formatCoords(externalPoint))
    setStatus('found')
    reverseGeocode(externalPoint).then((label) => {
      // Bail if a newer point (another click) or free-typed text has since
      // superseded this one -- shownPointRef.current would no longer be
      // this exact object in either case. Without this check, a slow
      // response landing late could stomp on something newer.
      if (label && shownPointRef.current === externalPoint) {
        setQuery(label)
        // Into the URL too: a reload then restores THIS name instantly
        // instead of re-asking the geocoder, whose nearest-thing answer
        // isn't stable call to call (and whose failure mode is showing
        // raw coordinates).
        onLabel(label)
      }
    })
  }, [externalPoint, onLabel])

  return {
    query,
    status,
    options,
    /** Whether `options` is the recents list (empty text) rather than
     * live suggestions — drives the listbox's header row and name. */
    showingRecents,
    activeIndex,
    /** Whether the listbox is rendered: options exist AND the dropdown
     * phase is on (typed text, or a focused empty field). */
    open: suggestOn && options.length > 0,
    onChange,
    resolve,
    selectSuggestion,
    commitRecent,
    setActiveIndex,
    closeSuggestions,
    openSuggestions,
    onBlur,
    clearField,
  }
}
