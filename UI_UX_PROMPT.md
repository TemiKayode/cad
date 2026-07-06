# Design brief: a world-class UI/UX for crdt-cad

> **Status (2026-07-05): IN PROGRESS.** Phase D1 exists as uncommitted
> work in the working tree (`demo/static/tokens.css`,
> `demo/static/icons.svg`, modified demo files) — finish, verify, and
> commit D1 first, then continue with D2–D8. Parts 1 and 2 of the plan
> are already complete; Part 4 is `DEPLOYMENT_PROMPT.md`.

## Context

This is Part 3 of the improvement plan for `crdt-cad`. Part 1
(`IMPROVEMENT_PROMPT.md`) hardens the platform; Part 2
(`FEATURE_IMPROVEMENT_PROMPT.md`) builds the editing features. This file
makes the tool *feel* world-class — the difference between "a working demo"
and something people describe as fast, precise, and pleasant, the way they
describe Figma, Linear, or Onshape. Read `README.md` and both other briefs
first. Every working rule from Part 1 applies (no unverifiable features,
thin-client JS, tests + README + live browser verification per phase,
phase-by-phase commits, no Claude co-author trailer).

These phases (D1–D8) can be interleaved with Part 2's feature phases — in
fact D1 (design system) should land **before or with** Part 2's Phase 10,
so new features are built on the new foundation instead of restyled later.

## What "world-class feel" means here, concretely

Professional canvas tools share five measurable qualities. Every phase below
serves at least one:

1. **The canvas is the hero.** Chrome recedes; panels are quiet, collapsible,
   and never fight the drawing for attention.
2. **Latency is invisible.** Every input has feedback within one frame
   (the optimistic-render architecture already guarantees the data side;
   the UI side must match it). Micro-transitions run 150–250ms,
   `transform`/`opacity` only, 60fps.
3. **Keyboard-first.** Everything reachable by mouse is reachable faster by
   keyboard; power users almost never touch the toolbar.
4. **State is always legible.** Connection, save, selection, tool, zoom,
   collaborator presence — the user never wonders "did that work?" or
   "who did that?".
5. **The signature moments are designed, not defaulted.** This tool has
   three genuinely differentiating moments — the Time-Travel Merge, the AI
   house streaming in, and live multiplayer cursors — and each deserves
   deliberate art direction instead of default-styled panels.

## Hard constraints

- **No build step, no npm, vanilla JS/CSS** — the project's stated
  architecture. That rules out Tailwind/PostCSS; the design system is
  hand-written CSS custom properties in `demo/static/styles.css`
  (split into `tokens.css` + `components.css` if it grows past ~1500 lines).
- **No emoji as icons.** The current UI uses emoji glyphs (✏ ⬠ 💾 🔗 ⟲).
  Replace every one with inline SVG from a single sprite
  (`demo/static/icons.svg`, Lucide-style 24×24 strokes, `currentColor`).
- **Both demos share one design system.** No 2D/3D drift.
- WCAG: 4.5:1 text contrast minimum, visible focus rings, full keyboard
  reachability, `prefers-reduced-motion` honored (all non-essential motion
  drops to 0ms), touch targets ≥ 44×44px.

## The design system (Phase D1 — everything else builds on this)

Codify as CSS custom properties on `:root`, replacing all ad-hoc values:

**Color (dark theme, default).** Slate scale base, one accent, semantic
status colors:

```
--bg-app: #0F172A        /* app chrome background */
--bg-canvas: #0B1120     /* canvas surface, slightly deeper than chrome */
--bg-panel: #1E293B      /* panels, toolbars */
--bg-raised: #334155     /* hover/raised surfaces, inputs */
--border: #33415580      /* hairline borders */
--text-primary: #F8FAFC
--text-secondary: #94A3B8
--text-disabled: #64748B
--accent: #4DABF7        /* selection, active tool, primary buttons */
--accent-muted: #4DABF733
--success: #22C55E       /* saved, connected, merge complete */
--warning: #F59E0B       /* offline, pending merge */
--danger:  #EF4444       /* rejection, validity warnings, delete */
```

Add a **light theme** as a `[data-theme="light"]` override set (slate-50
chrome, white canvas, same accent), a theme toggle persisted to
`localStorage`, and default from `prefers-color-scheme`. Verify contrast in
both themes — light mode must use slate-900 text and visible borders
(slate-200), not washed-out grays.

**Typography.** `Inter` (400/500/600) for all UI; `JetBrains Mono` (400/500)
for every number a user might read as data — coordinates, dimensions, zoom
level, vertex positions, room names. Load via Google Fonts with
`font-display: swap` and a `system-ui` fallback stack. Base 14px UI text
(13px in dense panels), 1.5 line height, no font below 11px anywhere.
Tabular numerals (`font-variant-numeric: tabular-nums`) on any live-updating
number so readouts don't jitter.

**Space, radius, elevation, z-index.** 4px spacing grid
(`--space-1..8: 4/8/12/16/24/32/48/64`); radii `--r-sm: 6px`, `--r-md: 10px`,
`--r-lg: 14px`; two shadow levels (panel, floating/modal); z-index scale
`--z-canvas: 0, --z-panel: 10, --z-toolbar: 20, --z-popover: 30,
--z-toast: 40, --z-modal: 50` — no other z-index values allowed.

**Motion.** `--t-fast: 150ms`, `--t-med: 220ms`, both
`cubic-bezier(0.2, 0, 0, 1)`. Transitions on `transform`, `opacity`,
`color`, `background-color`, `border-color`, `box-shadow` only — never on
layout properties. A single `@media (prefers-reduced-motion: reduce)` block
zeroes all of it.

Deliverable for D1: tokens in place, every existing component re-cut to use
them, emoji icons replaced by the SVG sprite, light theme working, a
`docs/design-system.md` page documenting the tokens.

## Phase D2 — Layout: make the canvas the hero

Restructure both demos into the professional-tool layout:

- **Top bar (48px)**: document name (click to rename — wires to Part 2
  Phase 17), room/share controls, presence avatar stack, connection/save
  status cluster, theme toggle. Nothing else.
- **Left rail**: a slim (48px) icon-only tool rail with tooltips
  (name + shortcut, 500ms delay, instant when moving between tools) — Pen,
  Select, shapes (Part 2), Polygon, Constrain… Active tool shows accent
  background + accent left-edge indicator.
- **Right panel (280px, collapsible)**: contextual inspector — shows
  properties of the current selection (color, stroke, layer, coordinates in
  mono font), or layers/paths list when nothing is selected. Collapsing it
  (and the left rail, via a keyboard toggle) gives a near-full-bleed canvas.
- **Bottom-left status strip**: zoom % (click → fit/100% menu), cursor
  coordinates in document units, snap indicator.
- Panels are `--bg-panel` with hairline borders, never heavy card shadows;
  the canvas gets the deepest background so the eye lands on the drawing.
- Responsive: at <900px panels overlay the canvas (slide-in) rather than
  squeezing it; at 375px the tool rail becomes a bottom bar with ≥44px
  targets. No horizontal page scroll at any width.

## Phase D3 — Input feel: cursors, selection, snapping feedback

The frame-by-frame details that make a canvas tool feel precise:

- **Per-tool cursors**: crosshair for drawing tools, default arrow for
  select, grab/grabbing for pan, `ns/ew/nwse-resize` on transform handles.
  Never the text I-beam over the canvas.
- **Selection visuals**: accent-colored bounding box with 8 square handles
  (white fill, accent stroke, ≥10px hit area), marquee as
  `--accent-muted` fill + accent hairline. Hovered-but-unselected geometry
  gets a subtle accent halo so users can see what a click will select.
- **Snap feedback** (pairs with Part 2 Phase 12): when a snap engages, show
  the standard glyph (square = endpoint, triangle = midpoint,
  circle = center, grid dot = grid) plus a brief alignment guide line; the
  cursor visibly "sticks". Snap glyphs use `--accent`, never new colors.
- **Buttons/inputs everywhere**: hover (background shift), active
  (scale 0.97 or background deepen), focus-visible (2px accent ring, 2px
  offset), disabled (50% opacity + `not-allowed`) — all four states on
  every control, via shared component classes, not per-element CSS.
- Drag operations (vertex drag, panel resize) apply `user-select: none`
  globally while active and never trigger text selection or layout shift.

## Phase D4 — Keyboard-first: command palette and shortcuts

- **Command palette** (`Ctrl/Cmd+K`): fuzzy-searchable list of every
  action — tool switching, export/import, save, share, go offline, AI
  generate, view commands (fit, 100%, ortho views in 3D), theme toggle.
  Each row: icon, name, shortcut chip (mono font). Arrow keys + Enter,
  Esc closes. This is also the discoverability answer for everything that
  has no toolbar button.
- **Single-key tool shortcuts** (V select, P pen, R rectangle, C circle,
  L line…), `Ctrl+Z/Y`, `Ctrl+D`, Delete, arrow-key nudge (Shift = ×10),
  Space-drag pan, `Ctrl+0` fit / `Ctrl+1` 100%, `?` opens the shortcut
  overlay (grouped, searchable, styled like the palette).
- Shortcuts must not fire while typing in inputs; tooltips and palette rows
  always display the binding, so the UI itself teaches the keyboard layer.
- Full keyboard reachability audit: every control tabbable in visual order,
  skip-to-canvas link, modals trap focus and restore it on close.

## Phase D5 — State legibility: status, toasts, empty states

- **Connection/save cluster** (top bar): one compact component with a
  colored dot + label — `● Live` (success), `● Offline — 4 edits queued`
  (warning, count updates live), `● Reconnecting…` (pulsing), and save
  state (`Saved just now` / `Saving…` / relative time, tabular-nums).
  Clicking it opens a popover explaining the state in a sentence —
  this is where the project's honest offline/merge model becomes visible UX.
- **Toast system** (bottom-center, `--z-toast`): one queued, auto-dismissing
  (4s, pausable on hover) component for save confirmations, share-link
  copied, import results ("Imported 12 paths"), geometry rejections
  ("Edge rejected — polygon would self-intersect", danger accent, paired
  with a brief red flash of the offending geometry on canvas), and Part 1
  Phase 6 validity warnings. Toasts are announced via `aria-live="polite"`.
- **Empty states**: a fresh 2D room shows a centered, quiet hint ("Press P
  and drag to draw · ? for shortcuts"); a fresh 3D room shows "Click the
  grid to place a vertex, or try AI Generate". One line each, dismissed by
  first interaction, never modal, never a tour wizard.
- **Destructive confirmations**: deleting a face/layer with dependents gets
  an undo-toast ("Layer deleted — Undo") instead of a blocking confirm
  dialog, leaning on the CRDT undo machinery.

## Phase D6 — Multiplayer presence as a delight, not a debug overlay

- **Remote cursors**: each collaborator gets a stable color (generate from
  actor id, fixed palette of 8 distinguishable hues checked against both
  themes), a smooth cursor (interpolate between presence updates with a
  ~80ms ease — no teleporting), and a name label that fades to a bare
  cursor after 3s idle, returning on movement.
- **Avatar stack** (top bar): overlapping initial-circles in presence
  colors, "+N" overflow, tooltip with full names; a subtle scale/fade
  entrance when someone joins, and a quiet toast ("Ada joined").
- **Remote selections/edits**: geometry a collaborator has selected shows a
  hairline outline in their color; a just-arrived remote edit flashes its
  color at low opacity for 600ms so changes are attributable at a glance.
- **Follow mode** (stretch within this phase): click an avatar to sync your
  viewport to theirs; any manual pan/zoom exits. Client-local only — the
  viewport still never syncs as document state.

## Phase D7 — Art-direct the two signature moments

**Time-Travel Merge** (the product's differentiator — currently a default
modal): redesign as a two-column branch comparison — "While you were away"
vs "Meanwhile, in the room" — each column a timeline of change chips
(icon + plain-language summary, presence-colored per author), with a
center spine converging on a single **Merge now** button (accent, the only
primary button on screen). On merge: the panel collapses with a brief
converge animation (two lines joining, 220ms, reduced-motion-safe) and a
success toast. The copy must reflect the true semantics: this is a preview
of an automatic, lossless merge — never "resolve conflicts".

**AI generation**: the ops already stream in batches, so stage it — the
prompt box gets a shimmer border while interpreting (distinct "thinking"
state), then a progress line ("Building floor… walls… roof…" driven by
batch arrival), the camera gently orbits ~15° as geometry lands (skipped
under reduced motion), and a completion toast names the actor ("Built by
ai_generator_bot — 18 vertices, 11 faces"). Failures (422/504) render as a
danger toast with the server's reason, inline retry, and the prompt
preserved in the box.

## Phase D8 — Performance and polish audit (gate before "done")

- 60fps verification: pan/zoom/drag on a 500-path document and a
  1,000-vertex mesh profiled in Chrome DevTools; no interaction >16ms
  scripting per frame; presence updates and coordinate readouts batched
  through `requestAnimationFrame`, canvas redraws only on dirty state.
- No layout shift anywhere: panels reserve space, toasts overlay, fonts
  swap without reflowing controls (size-adjusted fallback metrics).
- A `tests/e2e/test_design_system.py` Playwright pass: screenshots of both
  demos at 375/768/1280/1920 in both themes archived to
  `docs/screenshots/`; automated checks for focus-ring visibility,
  touch-target sizes on the mobile bottom bar, `aria-label` presence on
  every icon-only button, and computed text contrast ≥ 4.5:1 for
  primary/secondary text in both themes.
- Kill list sweep: no emoji icons anywhere, no `outline: none` without a
  replacement, no transition on layout properties, no hover-only
  functionality without a keyboard/touch path, no z-index outside the scale.

## Definition of done

- D1–D8 complete, committed per phase, full pytest + e2e suites green.
- Both demos share the token system; zero hard-coded colors/spacing/z-index
  outside `tokens.css`.
- Both themes pass the D8 audit; screenshots in `docs/screenshots/` updated
  (they're referenced by README's hero section — keep them current).
- README gains a short "Design system" section linking
  `docs/design-system.md`, written with the project's usual honesty about
  anything descoped.
