/* Inline SVG icons, not glyph characters: ⌖ (U+2316) renders as tofu in
   iOS's monospace fallback chain, and an icon that may or may not exist
   per-device isn't an icon. stroke="currentColor" follows the host
   button's color states.
   Every icon is aria-hidden — the button's aria-label (or visible text)
   is the accessible name; these are decoration. */

/** The location crosshair: circle + four ticks, the conventional
 * "use/center on my location" mark. */
export function CrosshairIcon() {
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
    >
      <circle cx="9" cy="9" r="5" />
      <line x1="9" y1="0.5" x2="9" y2="3.5" />
      <line x1="9" y1="14.5" x2="9" y2="17.5" />
      <line x1="0.5" y1="9" x2="3.5" y2="9" />
      <line x1="14.5" y1="9" x2="17.5" y2="9" />
    </svg>
  )
}

/** Expand-map arrows: two diagonals pointing out to opposite corners. */
export function ExpandIcon() {
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
    >
      <polyline points="11,2.5 15.5,2.5 15.5,7" />
      <line x1="15.5" y1="2.5" x2="10" y2="8" />
      <polyline points="7,15.5 2.5,15.5 2.5,11" />
      <line x1="2.5" y1="15.5" x2="8" y2="10" />
    </svg>
  )
}

/** Collapse-map arrows: the same two diagonals pointing back inward. */
export function CollapseIcon() {
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
    >
      <line x1="16" y1="2" x2="10.5" y2="7.5" />
      <polyline points="10.5,3.5 10.5,7.5 14.5,7.5" />
      <line x1="2" y1="16" x2="7.5" y2="10.5" />
      <polyline points="7.5,14.5 7.5,10.5 3.5,10.5" />
    </svg>
  )
}

/** The ⓘ info mark: the mobile header's ABOUT (the text swaps out
 * ≤720px — see Header.tsx). Circle + stem stroked like every icon here;
 * the dot is the one filled bit, since a sub-pixel stroked dot vanishes
 * at 18px. */
export function InfoIcon() {
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
    >
      <circle cx="9" cy="9" r="7.5" />
      <line x1="9" y1="8" x2="9" y2="12.5" />
      <circle cx="9" cy="5.4" r="0.9" fill="currentColor" stroke="none" />
    </svg>
  )
}

/** The share mark (box with an up arrow, the iOS-familiar form): sits
 * inside SHARE_ROUTE's text. The box's top edge is split so the arrow's
 * shaft passes through the gap instead of crossing a stroke. The head is
 * narrower than the gap on purpose — wing tips at the same x as the stub
 * ends read as touching; at 2-unit wings and 3.5-unit stubs the closest
 * inks keep ~0.6 units of daylight. Shaft stops a unit short of the apex, or its
 * square cap corners out past the head (RouteStats' glyph rule #1). */
export function ShareIcon() {
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
    >
      <path d="M5.5 6 H3.5 V15.5 H14.5 V6 H12.5" />
      <line x1="9" y1="3.5" x2="9" y2="11" />
      <polyline points="7,4.5 9,2.5 11,4.5" />
    </svg>
  )
}

/** The per-field clear ✕. */
export function ClearIcon() {
  return (
    <svg
      aria-hidden="true"
      width="14"
      height="14"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
    >
      <line x1="2" y1="2" x2="12" y2="12" />
      <line x1="12" y1="2" x2="2" y2="12" />
    </svg>
  )
}

/** The map's time control: a clock face, the universal "when" mark. */
export function ClockIcon() {
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
    >
      <circle cx="9" cy="9" r="7.5" />
      <polyline points="9,4.5 9,9 12,10.5" />
    </svg>
  )
}

/** A small down chevron: "this opens a choice", after the time line's
 * text. SVG for the same reason as the rest -- ▾ isn't safe in iOS's
 * monospace fallback. */
export function ChevronDownIcon() {
  return (
    <svg
      aria-hidden="true"
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
    >
      <polyline points="2.5,4.5 6,8 9.5,4.5" />
    </svg>
  )
}

/** Two stacked sheets: the shade pill -- which kinds of shade count. */
export function LayersIcon() {
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinejoin="round"
    >
      <polygon points="9,2.5 16,6.5 9,10.5 2,6.5" />
      <polyline points="2,10 9,14 16,10" />
    </svg>
  )
}

/** A tick: the picked line in a pill's menu. */
export function CheckIcon() {
  return (
    <svg
      aria-hidden="true"
      width="14"
      height="14"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polyline points="2.5,7.5 5.5,10.5 11.5,3.5" />
    </svg>
  )
}

/** The trip summary's way back: a left chevron. */
export function BackIcon() {
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
    >
      <polyline points="11.5,3 5.5,9 11.5,15" />
    </svg>
  )
}
