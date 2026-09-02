/* Inline SVG icons, not glyph characters: ⌖ (U+2316) renders as tofu in
   iOS's monospace fallback chain (caught in the iOS 26.3 simulator,
   2026-09-02), and an icon that may or may not exist per-device isn't an
   icon. stroke="currentColor" follows the host button's color states.
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
