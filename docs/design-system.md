# Design system (Part 3, Phase D1)

This documents the token system in `demo/static/tokens.css` and the icon
sprite in `demo/static/icons.svg` — the foundation every later Part 3
phase (D2–D8) and every existing component was re-cut to use. Nothing
in `demo/static/*.html`/`*.js` should hard-code a color, a px spacing
value that has a token equivalent, a duration, or an emoji glyph; if a
new one is genuinely needed, it's added here first, not inline at the
call site.

## Why a separate `tokens.css`

`styles.css` holds component rules; `tokens.css` holds only custom
property *values* (color/type/space/radius/elevation/z-index/motion) —
each HTML page links `tokens.css` before `styles.css`. This is what
lets D2–D8 restyle components without touching the values, and what
lets a whole second theme (light) exist as one small override block
instead of a parallel stylesheet.

## Color

Dark is the default (`:root`); `[data-theme="light"]` overrides the
same variable names. `--bg-canvas`/`--bg-app` etc. are picked so the
canvas is always the deepest surface in dark mode and the brightest in
light mode — "the canvas is the hero" per the brief.

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg-app` | `#0f172a` | `#f8fafc` | app chrome background |
| `--bg-canvas` | `#0b1120` | `#ffffff` | 2D canvas / 3D scene background |
| `--bg-panel` | `#1e293b` | `#f1f5f9` | panels, toolbars, modals |
| `--bg-raised` | `#334155` | `#e2e8f0` | buttons, inputs, hover surfaces |
| `--border` | `#33415580` | `#cbd5e1` | hairline borders, also the 2D grid / 3D GridHelper color |
| `--text-primary` | `#f8fafc` | `#0f172a` | primary text |
| `--text-secondary` | `#94a3b8` | `#475569` | secondary/hint text |
| `--text-disabled` | `#64748b` | `#94a3b8` | disabled controls only -- see note below |
| `--accent` | `#4dabf7` | `#1864ab` | selection, active tool, primary buttons, links |
| `--accent-muted` | `#4dabf733` | `#1864ab1a` | active-row backgrounds |
| `--accent-on` | `#06121a` | `#ffffff` | text/icon color drawn *on* a solid `--accent` fill |
| `--success` | `#22c55e` | `#16803c` | saved, connected, merge complete, editor role |
| `--warning` | `#f59e0b` | `#b45309` | offline, pending merge, reconnecting |
| `--danger` | `#ef4444` | `#dc2626` | rejection, validity warnings, delete |

**`--text-disabled` does not clear 4.5:1 contrast in either theme by
design** — WCAG 1.4.3's own exception excludes inactive UI components
from the minimum-contrast requirement. Only ever apply it to a
`disabled` control; never to text carrying information a user needs to
read (that's what `--text-secondary` is for, which does clear 4.5:1 in
both themes).

**Light theme's `--accent` is a darker blue than dark theme's**
(`#1864ab` vs `#4dabf7`), not a straight reuse. The dark-theme accent
only clears 4.5:1 as large text once you also need white text sitting
*on* an accent-colored button background at normal size — verified
directly (WCAG relative-luminance contrast ratio, not eyeballed):

| Pair | Dark | Light |
|---|---|---|
| text-primary / bg-app | 17.1:1 | 17.1:1 |
| text-secondary / bg-app | 7.0:1 | 7.2:1 |
| accent-on / accent (button text) | 7.7:1 | 6.1:1 |
| accent text / bg-app (links) | -- | 5.8:1 |
| danger / bg-app | 4.7:1 | 4.6:1 |

## Typography

`Inter` (400/500/600) for UI text, `JetBrains Mono` (400/500) for every
number a user might read as data — coordinates, dimensions, zoom level,
vertex positions, room names, keyboard shortcut chips. Loaded via a
Google Fonts `<link>` in each page's own `<head>` with `&display=swap`
(so text is never invisible while the font loads) and a `system-ui`/
`ui-monospace` fallback stack baked into `--font-ui`/`--font-mono`
themselves — a page that somehow can't reach Google Fonts still gets a
readable system font, not missing text.

Apply `.mono` (or the `--font-mono` var directly) plus
`font-variant-numeric: tabular-nums` to anything that updates live, so
digits don't jitter in place as they change — already wired onto
`#zoomIndicator`, `#cursorCoords`, `#opsCounter`, `.vertex-coord`,
`#actorLabel`, `#roomInput`, and the version-history/room-card
timestamps.

## Space, radius, elevation, z-index

4px spacing grid (`--space-1` through `--space-8` = 4/8/12/16/24/32/
48/64px); three radii (`--r-sm: 6px`, `--r-md: 10px`, `--r-lg: 14px`);
two shadow levels (`--shadow-panel`, `--shadow-floating`); a six-level
z-index scale (`--z-canvas: 0` through `--z-modal: 50`) that **every**
`z-index` in the codebase must come from — the pre-D1 code had raw
values like `1000`/`1500`/`2000` scattered across toasts/banners/
modals with no relationship to each other, which is exactly the kind of
stacking bug waiting to happen this scale prevents.

## Motion

`--t-fast: 150ms` / `--t-med: 220ms`, both `cubic-bezier(0.2, 0, 0, 1)`.
Transitions are only ever written on `transform`/`opacity`/
`background-color`/`border-color`/`box-shadow` (never on a layout
property like `width`/`height`/`padding`, which would trigger reflow
every frame). A single `@media (prefers-reduced-motion: reduce)` block
in `tokens.css` zeroes every animation/transition duration globally --
components don't need their own opt-out.

## Icon sprite

`demo/static/icons.svg` holds ~35 hand-authored 24×24 stroke icons
(2px stroke, round caps/joins, `currentColor`, no fill) replacing every
emoji glyph the UI used to render as a tool/action icon (✏ ⬠ 💾 🔗 ⟲
etc., plus several plain-Unicode stand-ins like ▭ ○ ↖ that had the same
inconsistent-rendering problem). Referenced as:

```html
<svg class="icon"><use href="#icon-name"></use></svg>
```

**Important: the `href` is a bare `#icon-name` fragment, not
`/static/icons.svg#icon-name`.** Cross-document external `<use
href="other-file.svg#id">` was the first approach tried and confirmed
*not* to render in this environment (tested with both `href` and legacy
`xlink:href` in isolation — a same-document reference to a dynamically
inserted symbol worked immediately, the external reference rendered
nothing, silently, no console error). `common.js`'s `loadIconSprite()`
fetches `icons.svg`'s raw markup once at startup and injects it into a
hidden `<div>` in the page, so every `#icon-name` fragment resolves
locally; SVG `<use>` is reactive to DOM mutations, so icon markup
already present in the page before the fetch resolves picks up the
symbol the instant the sprite lands (a brief, generally imperceptible
flash on a same-origin fetch, not a persistent bug).

Use `iconHtml(name, extraClass?)` (`common.js`) to build the markup
from JS rather than hand-writing the `<svg><use>` string.

**Two glyph families were deliberately left alone, not replaced:**
- The five constraint badge glyphs drawn on the 2D canvas
  (`CONSTRAINT_GLYPHS` in `sketch.js`: `≡ ∥ ⊥ ↔ ⊙` for coincident/
  parallel/perpendicular/fixed-distance/tangent) are mathematical/
  geometric notation, not emoji standing in for a missing icon --
  real CAD tools (Onshape, SolidWorks) use exactly this kind of symbol
  for constraint badges. They're also drawn via `ctx.fillText`, not
  DOM, so they couldn't use the SVG sprite regardless.
- Per-content colors chosen by a user (a path's stroke color, a 3D
  face's palette color) are document data, not chrome, and are
  untouched by this phase.

## Canvas-drawn colors

A `ctx.fillStyle`/`strokeStyle` can't reference a CSS custom property
directly. `canvasColor(varName)` (`common.js`) resolves one via
`getComputedStyle` and caches it per-theme (canvas redraws happen every
frame during a drag; re-reading computed style that often would be
wasted work), invalidating the cache only when the active theme
actually changes. Applied to the 2D grid lines (`sketch.js`) and the 3D
scene background + `GridHelper` (`mesh3d.js`) in this phase, since a
hardcoded dark grid color would be nearly invisible against a
light-theme canvas -- the highest-visual-risk item for shipping light
theme at all. **Known gap, not silently missed**: the 3D `GridHelper`
bakes its two colors into a per-vertex buffer attribute at construction
time, so a live theme toggle while the 3D page is already open won't
retint an already-built grid (the scene *background* does update live,
since that's a plain property). A fresh page load in either theme
renders correctly either way. Full remaining canvas-color theme
coverage (snap glyphs, selection handles) is D3's scope, not D1's.

## Theme toggle

A `#themeToggleBtn` in every page's top bar, wired by
`initThemeToggle(onChange?)` (`common.js`). The *decision* of which
theme to show on first paint happens in a tiny inline, synchronous
`<script>` in each page's own `<head>` (reads `localStorage`, falls
back to `prefers-color-scheme`) -- it has to run before CSS paints,
which a deferred `<script src="common.js">` loaded at the bottom of
`<body>` cannot do without a flash of the wrong theme. Toggling persists
to `localStorage` under `crdt_cad_theme` and survives reload/navigation
between all three pages (confirmed live). `onChange` exists for state a
CSS custom property can't reach -- `mesh3d.js` passes it to re-apply
`scene.background` after every toggle.

## What's still open (later Part 3 phases, not gaps in D1)

- The left toolbar is still a labeled button row, not the icon-only
  rail with tooltips the brief describes -- that's D2 (layout).
- Per-tool cursors, snap-glyph/selection-handle theming, and button
  hover/active/focus polish beyond the baseline states already in
  `styles.css` are D3.
- No command palette yet (D4).
- The connection/save status cluster, toast copy, and empty states are
  functionally unchanged from before D1 -- just re-skinned onto tokens.
  Redesigning them is D5.
- Remote presence cursor color palette (currently `ACTOR_COLORS` in
  `common.js`, unchanged) checked against both themes is D6.
- The Time-Travel Merge modal and AI-generation flow got a token-only
  re-skin here (colors, radii, shadow, `.modal`/`.modal-overlay`
  classes), not the art direction the brief describes for either --
  that's D7.
- The full WCAG/screenshot audit (contrast automation, touch targets,
  focus-ring visibility, `aria-label` sweep, kill-list) is D8's job, run
  once, at the end of Part 3 -- not repeated per phase.
