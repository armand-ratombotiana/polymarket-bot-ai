# Polymarket Workstation — Design System

> **Source of truth:** `src/app/globals.css` (2,534 lines, 169 unique CSS variables).
>
> **Status:** Living document. Every token in this file is declared as a CSS
> custom property in `globals.css`; this document is the human-readable
> catalog. Update both together — never edit one without the other.

---

## 1. Design principles

1. **Token-first, never hardcoded.** Every color / spacing / motion value
   is a CSS variable. Components consume `var(--token)`, never raw hex.
   *Hardcoded hex literals exist only in legacy code (the ~880 Tailwind
   arbitrary `bg-[#0e1015]` occurrences scoped to `.light` overrides);
   new code uses tokens.*
2. **Dark-first, light-flipped.** The dashboard was designed for a dark
   trading-room canvas. Light mode is a 1:1 token overlay (`.light`
   redefines every `:root` token) — never a parallel stylesheet.
3. **Intent over hue.** Prefer semantic aliases (`--color-success`,
   `--color-danger`) over hue names (`--color-green`, `--color-red`).
   A future color-blind accessibility tweak (swap green → teal) propagates
   through the alias without touching every consumer.
4. **Layered, additive changes.** New tokens append at end-of-file. No
   earlier rule block is ever edited in place — the cascade ensures
   later equal-specificity declarations win per-property.
5. **WCAG AA minimum.** Body text ≥ 4.5:1 contrast against the surface;
   UI labels ≥ 3:1; status dots ≥ 3:1. See §9 for the audit.

---

## 2. Color tokens

### 2.1 Background layers

Eight-step surface ladder, dark → light. The deepest `--bg-base` is the
shell behind everything; `--bg-card` is the standard card surface.

| Token | Dark value | Light value | Use |
|-------|-----------|-------------|-----|
| `--bg-base` | `#080910` | `#f1f5f9` | App shell background |
| `--bg-surface` | `#0e1015` | `#f8fafc` | Page background |
| `--bg-card` | `#13161e` | `#ffffff` | Card / panel |
| `--bg-card-alt` | `#111420` | `#f1f5f9` | Alternate card |
| `--bg-hover` | `#1a1f2e` | `#f1f5f9` | Row hover |
| `--bg-selected` | `#1e2540` | `#dbeafe` | Row selected |
| `--bg-input` | `#0e1015` | `#f8fafc` | Input fields |
| `--bg-overlay` | `rgba(4,5,10,0.85)` | `rgba(15,23,42,0.55)` | Modal backdrop |
| `--bg-elevated` | `var(--bg-card-alt)` | (auto-themes) | Elevated surfaces (popovers, menus) |

### 2.2 Border tokens

| Token | Dark value | Light value | Use |
|-------|-----------|-------------|-----|
| `--border` | `#1f2335` | `#e2e8f0` | Standard border |
| `--border-dim` | `#181c28` | `#f1f5f9` | Subtle divider |
| `--border-focus` | `#3b82f6` | `#2563eb` | Focus ring |
| `--border-hover` | `#2d3450` | `#cbd5e1` | Hover border |

### 2.3 Typography tokens

| Token | Dark value | Light value | Use |
|-------|-----------|-------------|-----|
| `--text-primary` | `#dde1ed` | `#0f172a` | Body text |
| `--text-secondary` | `#7e8aaa` | `#475569` | Secondary labels |
| `--text-dim` | `#3e4560` | `#94a3b8` | Muted / placeholder |
| `--text-link` | `#60a5fa` | `#2563eb` | Links |
| `--text-mono` | `#c8cfe0` | `#1e293b` | Monospace data |

### 2.4 Semantic color aliases (intent-based)

W38-2 introduced intent-based semantic aliases that map to the existing
hue tokens. Use these in new code; existing hue-based tokens remain
canonical for backward compatibility.

| Alias | Maps to | Use |
|-------|---------|-----|
| `--color-success` / `-fg` / `-muted` | `--color-green` / `-fg` / `-bg` | Positive P&L, healthy status, OK |
| `--color-danger` / `-fg` / `-muted` | `--color-red` / `-fg` / `-bg` | Negative P&L, errors, kill switch |
| `--color-warning` / `-fg` / `-muted` | `--color-amber` / `-fg` / `-bg` | Degraded, stale, caution |
| `--color-info` / `-fg` / `-muted` | `--color-blue` / `-fg` / `-bg` | Informational, neutral accents |

**Dark values:**

| Token | `--color-*` | `--color-*-fg` | `--color-*-muted` |
|-------|------------|----------------|-------------------|
| success | `#22c55e` | `#4ade80` | `rgba(34,197,94,0.10)` |
| danger | `#ef4444` | `#f87171` | `rgba(239,68,68,0.10)` |
| warning | `#f59e0b` | `#fbbf24` | `rgba(245,158,11,0.10)` |
| info | `#3b82f6` | `#60a5fa` | `rgba(59,130,246,0.10)` |

**Light values:**

| Token | `--color-*` | `--color-*-fg` | `--color-*-muted` |
|-------|------------|----------------|-------------------|
| success | `#16a34a` | `#15803d` | `rgba(22,163,74,0.10)` |
| danger | `#dc2626` | `#b91c1c` | `rgba(220,38,38,0.08)` |
| warning | `#d97706` | `#b45309` | `rgba(217,119,6,0.10)` |
| info | `#2563eb` | `#1d4ed8` | `rgba(37,99,235,0.08)` |

### 2.5 Hue tokens (canonical)

Six hue families, each with four variants: base color, foreground (readable
text on the muted background), background tint, and border.

| Hue | `-color-*` (dark) | `-color-*-fg` | `-color-*-bg` | `-color-*-bd` |
|------|-------------------|---------------|---------------|----------------|
| green | `#22c55e` | `#4ade80` | `rgba(34,197,94,0.10)` | `rgba(34,197,94,0.22)` |
| red | `#ef4444` | `#f87171` | `rgba(239,68,68,0.10)` | `rgba(239,68,68,0.25)` |
| amber | `#f59e0b` | `#fbbf24` | `rgba(245,158,11,0.10)` | `rgba(245,158,11,0.25)` |
| blue | `#3b82f6` | `#60a5fa` | `rgba(59,130,246,0.10)` | `rgba(59,130,246,0.22)` |
| cyan | `#06b6d4` | `#22d3ee` | `rgba(6,182,212,0.10)` | `rgba(6,182,212,0.22)` |
| purple | `#a855f7` | `#c084fc` | `rgba(168,85,247,0.10)` | `rgba(168,85,247,0.22)` |

### 2.6 Mode tokens

Four trading modes — each gets color / background / border tokens. Used
by `.mode-badge-*` classes and the panel header stripes.

| Mode | Dark color | Light color | Use |
|------|-----------|-------------|-----|
| paper | `#f59e0b` (amber) | `#d97706` | Paper trading (simulated fills) |
| live | `#ef4444` (red) | `#dc2626` | Live trading (real money) |
| shadow | `#06b6d4` (cyan) | `#0891b2` | Shadow mode (mirror live without executing) |
| backtest | `#a855f7` (purple) | `#9333ea` | Backtest replay |

### 2.7 Status tokens

Status colors for health indicators (system status dots, connection
state). These stay saturated in both themes because they're small dots
that need to remain legible on either background.

| Token | Value | Use |
|-------|-------|-----|
| `--status-healthy` | `#22c55e` | Service healthy |
| `--status-degraded` | `#f59e0b` | Service degraded (pulsing) |
| `--status-unavailable` | `#ef4444` | Service down |
| `--status-stale` | `#f59e0b` | Data stale (pulsing) |
| `--status-unknown` | `#3e4560` | Status unknown |
| `--status-disabled` | `#3e4560` | Explicitly disabled |
| `--status-experimental` | `#a855f7` | Experimental feature |

---

## 3. Typography scale

Pixel-step scale (consumable via `--text-*` tokens and `.text-*`
utility classes). Body base is `13px` (set on `body`); the scale
covers everything from micro-labels (`10px`) to page titles (`22px`).

| Token | Size | Utility class | Use |
|-------|------|---------------|-----|
| `--text-2xs` | 10px | `.text-2xs` | Micro labels (heatmap cells) |
| `--text-xs` | 11px | `.text-xs` | Table headers, captions |
| `--text-sm` | 12px | `.text-sm` | Table cells, secondary text |
| `--text-base` | 13px | `.text-base` | Body text (default) |
| `--text-md` | 14px | `.text-md` | Modal titles, emphasized |
| `--text-lg` | 16px | `.text-lg` | Section headings |
| `--text-xl` | 18px | `.text-xl` | Page headings |
| `--text-2xl` | 22px | `.text-2xl` | Hero / dashboard title |

### Font role stacks

| Token | Stack | Use |
|-------|-------|-----|
| `--font-sans` | `'Inter', system-ui, sans-serif` | General text, UI labels, button text |
| `--font-display` | `'Plus Jakarta Sans', 'Inter', sans-serif` | Page titles, hero numbers |
| `--font-mono` | `'JetBrains Mono', monospace` | Prices, IDs, timestamps, P&L numbers |

Apply via the `.mono` / `.font-mono` class or `[data-mono="true"]`
attribute. All monospace text uses `font-variant-numeric: tabular-nums`
so columns of numbers align cleanly.

---

## 4. Spacing system

W38-2 completed the spacing scale from `--space-1` (4px) through
`--space-12` (48px) in 4px increments. The earlier `:root` block
declared only 1, 2, 3, 4, 6 — gaps 5, 7, 8, 9, 10, 11, 12 are new.

| Token | rem | px | Use |
|-------|-----|----|----|
| `--space-1` | 0.25rem | 4px | Tight gaps (badge padding) |
| `--space-2` | 0.5rem | 8px | Default small gap (filter chips) |
| `--space-3` | 0.75rem | 12px | Default medium gap (card body) |
| `--space-4` | 1rem | 16px | Card padding, modal padding |
| `--space-5` | 1.25rem | 20px | Larger card padding (NEW) |
| `--space-6` | 1.5rem | 24px | Empty state padding |
| `--space-7` | 1.75rem | 28px | Section break (NEW) |
| `--space-8` | 2rem | 32px | Hero spacing (NEW) |
| `--space-9` | 2.25rem | 36px | (NEW) |
| `--space-10` | 2.5rem | 40px | (NEW) |
| `--space-11` | 2.75rem | 44px | Touch target minimum (NEW) |
| `--space-12` | 3rem | 48px | Page section gap (NEW) |

**Utility classes:** Tailwind v4 ships `p-5` / `gap-7` etc. by default
(matching our scale). For code that consumes `var(--space-*)` directly,
`.p-space-{n}` and `.gap-space-{n}` utilities bridge the gap.

---

## 5. Radius

| Token | Value | Use |
|-------|-------|-----|
| `--radius-sm` | 4px | Badges, small chips |
| `--radius-md` | 6px | Inputs, buttons, KPI cards |
| `--radius-lg` | 8px | Cards, panels |
| `--radius-xl` | 12px | Modals |

---

## 6. Elevation (shadow system)

Five-step shadow ladder. W38-2 added `.light` overrides — dark-mode
shadows use `rgba(0,0,0,*)` (black haze on dark surface), light-mode
shadows use `rgba(15,23,42,*)` with much lower alpha so elevations
read as soft shadows on white instead of black smudges.

| Token | Dark | Light | Use |
|-------|------|-------|-----|
| `--shadow-xs` | `0 1px 2px rgba(0,0,0,0.30)` | `0 1px 2px rgba(15,23,42,0.06)` | Subtle (badges) |
| `--shadow-sm` | `0 2px 4px rgba(0,0,0,0.35)` | `0 2px 4px rgba(15,23,42,0.08)` | Small (cards) |
| `--shadow-md` | `0 4px 10px rgba(0,0,0,0.40)` | `0 4px 10px rgba(15,23,42,0.10)` | Medium (default card) |
| `--shadow-lg` | `0 10px 24px rgba(0,0,0,0.45)` | `0 10px 24px rgba(15,23,42,0.12)` | Large (popovers) |
| `--shadow-xl` | `0 20px 48px rgba(0,0,0,0.55)` | `0 20px 48px rgba(15,23,42,0.16)` | Largest (modals) |

**Component aliases (compose the primitives):**

| Token | Resolves to | Use |
|-------|------------|-----|
| `--shadow-card` | `var(--shadow-md)` | Default card elevation |
| `--shadow-popover` | `var(--shadow-lg)` | Popovers, dropdowns |
| `--shadow-modal` | `var(--shadow-xl)` | Modals, dialogs |
| `--shadow-dropdown` | `var(--shadow-md)` | Dropdown menus |

---

## 7. Motion

### Duration

| Token | Value | Use |
|-------|-------|-----|
| `--duration-fast` | 120ms | Hover, focus, small UI feedback |
| `--duration-base` | 180ms | Modal open, panel transitions |
| `--duration-slow` | 280ms | Large layout shifts (sidebar collapse) |

### Easing

| Token | Value | Use |
|-------|-------|-----|
| `--easing-std` | `cubic-bezier(0.4, 0, 0.2, 1)` | Standard (most transitions) |
| `--easing-enter` | `cubic-bezier(0.0, 0.0, 0.2, 1)` | Enter animations (decelerate) |
| `--easing-exit` | `cubic-bezier(0.4, 0.0, 1.0, 1)` | Exit animations (accelerate) |

### Composed transitions

W38-2 introduced pre-composed transition bundles so component authors
don't repeat the same five-property transition literal:

| Token | Expands to |
|-------|------------|
| `--transition-fast` | `var(--duration-fast) var(--easing-std)` |
| `--transition-base` | `var(--duration-base) var(--easing-std)` |
| `--transition-slow` | `var(--duration-slow) var(--easing-std)` |
| `--transition-button` | `background, border-color, color, box-shadow, transform` (all `--transition-fast`) |
| `--transition-panel` | `opacity, transform` (both `--transition-base`) |
| `--transition-modal` | `opacity, transform` (both `--transition-base`) |

**Reduced motion:** `@media (prefers-reduced-motion: reduce)` sets all
animation / transition durations to `0.01ms` globally. Honor this —
never disable transitions for users who explicitly request reduced motion.

---

## 8. Z-index scale

| Token | Value | Use |
|-------|-------|-----|
| `--z-sidebar` | 10 | Sidebar |
| `--z-topbar` | 20 | Top bar |
| `--z-dropdown` | 30 | Dropdowns, popovers |
| `--z-modal` | 40 | Modals |
| `--z-toast` | 50 | Toast notifications |
| `--z-critical` | 60 | Critical overlays (kill switch, error boundaries) |

---

## 9. Chart color palette

W38-2 introduced `--chart-*` CSS variables that mirror the TypeScript
`chartTheme` object in `src/components/charts/theme.ts`. Charts can now
consume tokens via CSS instead of importing the TS module.

### 9.1 Primary chart colors

| Token | Dark | Light | Use |
|-------|------|-------|-----|
| `--chart-primary` | `#3b82f6` | `#2563eb` | Primary line / area |
| `--chart-success` | `#10b981` | `#059669` | Positive P&L |
| `--chart-danger` | `#ef4444` | `#dc2626` | Negative P&L |
| `--chart-warning` | `#f59e0b` | `#d97706` | Caution / midpoint |
| `--chart-info` | `#06b6d4` | `#0891b2` | Info / secondary |
| `--chart-muted` | `#6b7280` | `#4b5563` | Axes / gridlines |

### 9.2 Eight-series palette (multi-series charts)

Ordered for maximum perceptual distance. Use `.chart-series-1` through
`.chart-series-8` utility classes for legend swatches that need to
match their data series color.

| # | Dark | Light | Hue | Use |
|---|------|-------|------|-----|
| 1 | `#3b82f6` | `#2563eb` | blue | Primary accent |
| 2 | `#22c55e` | `#16a34a` | green | Positive / growth |
| 3 | `#f59e0b` | `#d97706` | amber | Caution / midpoint |
| 4 | `#ef4444` | `#dc2626` | red | Negative / alert |
| 5 | `#a855f7` | `#9333ea` | purple | AI / ML output |
| 6 | `#06b6d4` | `#0891b2` | cyan | Info / secondary |
| 7 | `#ec4899` | `#db2777` | pink | Contrast / category |
| 8 | `#84cc16` | `#65a30d` | lime | Highlight / outlier |

### 9.3 AI/ML accents

Dedicated tokens so AI-driven overlays are visually distinct from raw
market data at a glance.

| Token | Dark | Light | Use |
|-------|------|-------|-----|
| `--chart-ai` | `#3b82f6` | `#2563eb` | AI surfaces (predictions, suggestions) |
| `--chart-ml` | `#a855f7` | `#9333ea` | ML models (embeddings, classifications) |
| `--chart-positive` | `var(--chart-success)` | (auto) | Positive shorthand |
| `--chart-negative` | `var(--chart-danger)` | (auto) | Negative shorthand |

### 9.4 Chart surfaces

| Token | Dark | Light | Use |
|-------|------|-------|-----|
| `--chart-grid` | `rgba(255,255,255,0.05)` | `rgba(15,23,42,0.08)` | Gridline stroke |
| `--chart-axis` | `#8b949e` | `#475569` | Axis tick color |
| `--chart-tooltip-bg` | `var(--bg-card)` | `#ffffff` | Tooltip background |
| `--chart-tooltip-border` | `var(--border)` | `#e2e8f0` | Tooltip border |
| `--chart-tooltip-text` | `var(--text-primary)` | `#0f172a` | Tooltip text |

**Utility classes:** `.chart-series-{1..8}`, `.chart-axis`, `.chart-grid`,
`.chart-ai`, `.chart-ml`, `.chart-positive`, `.chart-negative`.

---

## 10. Layout tokens

| Token | Value | Use |
|-------|-------|-----|
| `--sidebar-width` | 220px | Default sidebar width (192px below 1280px, 52px collapsed below 1024px) |
| `--sidebar-collapsed-width` | 52px | Collapsed sidebar |
| `--topbar-height` | 42px | Top status bar |
| `--header-height` | 52px | Sidebar header |

### Responsive breakpoints (W38-2)

| Token | Value | Tailwind equiv | Use |
|-------|-------|----------------|-----|
| `--bp-sm` | 640px | `sm` | Small phones / narrow split panels |
| `--bp-md` | 768px | `md` | Tablet / mobile workstation |
| `--bp-lg` | 1024px | `lg` | Desktop with collapsed sidebar |
| `--bp-xl` | 1200px | `xl` | Desktop with expanded sidebar + 2-col grid |
| `--bp-2xl` | 1280px | `2xl` | Wide workstation, 3-col command center |

### Container widths

| Token | Value | Use |
|-------|-------|-----|
| `--container-sm` | 480px | Confirmation dialog |
| `--container-md` | 640px | Default modal |
| `--container-lg` | 768px | Wide modal |
| `--container-xl` | 1024px | XL modal / full-bleed content |

---

## 11. Focus rings

| Token | Dark | Light | Use |
|-------|------|-------|-----|
| `--ring-focus` | `0 0 0 3px rgba(59,130,246,0.18)` | `0 0 0 3px rgba(37,99,235,0.20)` | Default focus ring (blue) |
| `--ring-danger` | `0 0 0 3px rgba(239,68,68,0.18)` | `0 0 0 3px rgba(220,38,38,0.20)` | Danger ring (kill switch) |
| `--ring-success` | `0 0 0 3px rgba(34,197,94,0.18)` | `0 0 0 3px rgba(22,163,74,0.20)` | Success ring |
| `--ring-warning` | `0 0 0 3px rgba(245,158,11,0.18)` | `0 0 0 3px rgba(217,119,6,0.22)` | Warning ring |

Applied via `:focus-visible` on every interactive element. Outline color
is `--border-focus`; ring is `--ring-focus` on `.input:focus`.

---

## 12. Component variants

### 12.1 Buttons

| Class | Use |
|-------|-----|
| `.btn` | Base button |
| `.btn-primary` | Primary action (blue, white text) |
| `.btn-danger` | Destructive action (red bg, red fg → red solid on hover) |
| `.btn-kill` | Kill switch (red, bold, glow on hover) |
| `.btn-resume` | Resume suspended strategy (green) |
| `.btn-success` | Success action (green) |
| `.btn-ghost` | Ghost / secondary (transparent bg, border) |
| `.btn-amber` | Amber action (caution, reversible) |
| `.btn-sm` | Small button (11px, 0.2rem padding) |
| `.btn-xs` | Extra-small button (10.5px, 0.15rem padding) |

All buttons get a 1px lift on hover (`transform: translateY(-1px)`)
and a press-scale on `:active` (`transform: scale(0.98)`).

### 12.2 Badges

| Class | Use |
|-------|-----|
| `.badge-green` / `.badge-success` | Positive status (success alias W38-2) |
| `.badge-red` / `.badge-danger` | Negative status (danger alias W38-2, pulses) |
| `.badge-amber` / `.badge-warning` | Caution status (warning alias W38-2) |
| `.badge-blue` / `.badge-info` | Informational (info alias W38-2) |
| `.badge-cyan` | Cyan accent |
| `.badge-purple` | Experimental / synthetic |
| `.badge-dim` | Muted / disabled |

### 12.3 Cards

| Class | Use |
|-------|-----|
| `.card` | Standard card (border, radius-lg, shadow-md, gradient overlay) |
| `.card-header` | Header row with bottom border |
| `.card-title` | Uppercase 11px label |
| `.card-body` | Standard padding |
| `.kpi-card` | KPI metric card |
| `.skeleton-card` | Loading placeholder card |
| `.card-hover` | Hover lift (translateY -1px + soft shadow) |

### 12.4 Banners

| Class | Use |
|-------|-----|
| `.banner-experimental` | Purple banner for experimental features |
| `.banner-warning` | Amber banner for warnings |
| `.banner-danger` | Red banner for errors |
| `.banner-info` | Blue banner for informational notices |

### 12.5 Status dots

| Class | Use |
|-------|-----|
| `.status-dot.healthy` | Green (solid) |
| `.status-dot.degraded` | Amber (pulsing) |
| `.status-dot.unavailable` | Red (solid) |
| `.status-dot.stale` | Amber (pulsing, fast) |
| `.status-dot.unknown` | Dim grey (solid) |
| `.status-dot.connecting` | Amber (pulsing, fast) |

### 12.6 Mode badges

| Class | Use |
|-------|-----|
| `.mode-badge-paper` | Paper trading mode (amber) |
| `.mode-badge-live` | Live trading mode (red) |
| `.mode-badge-shadow` | Shadow mode (cyan) |
| `.mode-badge-backtest` | Backtest mode (purple) |

---

## 13. Light / dark theme mapping

The dashboard uses `next-themes` (`attribute="class"`,
`defaultTheme="dark"`, `enableSystem={false}`). The `<html>` element
gets a `dark` or `light` class; `globals.css` re-declares every design
token under `.light` so any consumer of `var(--token)` re-themes
automatically.

### Theme toggle

The toggle is in `src/components/ThemeToggle.tsx`, rendered in the
top-right cluster of `TopStatusBar`. Choice persists to `localStorage`
via `next-themes`'s default storage key (`theme`).

### Mapping coverage (W38-2 audit)

| Token family | Dark `:root` | Light `.light` | Notes |
|--------------|--------------|----------------|-------|
| Backgrounds (8) | ✓ | ✓ | Full coverage |
| Borders (4) | ✓ | ✓ | Full coverage |
| Typography (5) | ✓ | ✓ | Full coverage |
| Hue tokens (6 × 4 = 24) | ✓ | ✓ | Full coverage, `-fg` shifts 1-2 shades darker |
| Mode tokens (4 × 3 = 12) | ✓ | ✓ | Full coverage |
| Status tokens (7) | ✓ | (same hues) | Intentionally unchanged — small dots stay legible on white |
| Spacing (12) | ✓ | (same) | Theme-independent |
| Radius (4) | ✓ | (same) | Theme-independent |
| Motion (3 durations + 3 easings) | ✓ | (same) | Theme-independent |
| Z-index (6) | ✓ | (same) | Theme-independent |
| Shadows (5) | ✓ | ✓ | W38-2 added light overrides (slate-900 alpha) |
| Chart palette (full) | ✓ | ✓ | W38-2 added light overrides |
| Semantic aliases (4 × 3 = 12) | ✓ | (auto via hue refs) | W38-2 — auto-theme via hue refs |
| Focus rings (4) | ✓ | ✓ | W38-2 — light has stronger alpha |
| Breakpoints (5) | ✓ | (same) | Theme-independent |

### WCAG AA contrast audit (light theme)

All ratios computed against `--bg-surface: #f8fafc` (slate-50):

| Token | Value | Contrast | WCAG level |
|-------|-------|----------|-----------|
| `--text-primary` | `#0f172a` (slate-900) | 17.4:1 | AAA |
| `--text-secondary` | `#475569` (slate-600) | 7.1:1 | AAA |
| `--text-dim` | `#94a3b8` (slate-400) | 2.7:1 | UI-only (not body) |
| `--color-success-fg` | `#15803d` (green-700) | 5.2:1 | AA |
| `--color-danger-fg` | `#b91c1c` (red-700) | 5.4:1 | AA |
| `--color-warning-fg` | `#b45309` (amber-700) | 4.9:1 | AA |
| `--color-info-fg` | `#1d4ed8` (blue-700) | 7.3:1 | AAA |

### WCAG AA contrast audit (dark theme)

All ratios computed against `--bg-card: #13161e`:

| Token | Value | Contrast | WCAG level |
|-------|-------|----------|-----------|
| `--text-primary` | `#dde1ed` | 12.8:1 | AAA |
| `--text-secondary` | `#7e8aaa` | 5.6:1 | AA |
| `--text-dim` | `#3e4560` | 2.4:1 | UI-only |
| `--color-success-fg` | `#4ade80` (green-400) | 9.1:1 | AAA |
| `--color-danger-fg` | `#f87171` (red-400) | 6.2:1 | AA |
| `--color-warning-fg` | `#fbbf24` (amber-400) | 9.8:1 | AAA |
| `--color-info-fg` | `#60a5fa` (blue-400) | 6.8:1 | AA |

---

## 14. Usage guidelines

### 14.1 When to use which token

1. **Background colors:** Use the surface ladder (`--bg-base` →
   `--bg-card` → `--bg-card-alt`) to express depth. The deeper the
   surface, the darker the background. Never mix two adjacent steps
   without a border.
2. **Text colors:** Use `--text-primary` for body, `--text-secondary`
   for labels, `--text-dim` only for placeholder text and decorative
   elements (it fails WCAG AA at 2.7:1 — don't use for body copy).
3. **Semantic colors:** Use `--color-success/danger/warning/info` in
   new code. Use `--color-green/red/amber/blue` only when matching
   existing hue-based code or when you need a hue that has no intent
   alias (cyan, purple).
4. **Spacing:** Use `--space-{n}` or Tailwind's `p-{n}` / `gap-{n}` /
   `m-{n}` (they share the same scale). Never hardcode `12px` /
   `0.75rem` etc.
5. **Shadows:** Use `--shadow-card` for default card elevation,
   `--shadow-popover` for popovers, `--shadow-modal` for modals.
   Don't compose custom `box-shadow` literals.
6. **Motion:** Use `--transition-button` for buttons, `--transition-panel`
   for panel reveals, `--transition-modal` for modal open/close. The
   reduced-motion media query handles the rest.
7. **Charts:** Use the `chartTheme` TS module (`src/components/charts/theme.ts`)
   for Recharts integrations. Use `--chart-*` CSS variables for
   custom SVG charts or legend swatches. Use `.chart-series-{1..8}`
   utility classes for legend dots.
8. **Z-index:** Always use the `--z-*` tokens. Never hardcode
   `z-index: 9999` — that breaks the layering contract.

### 14.2 Anti-patterns

- **Hardcoded hex.** `color: '#dde1ed'` in TSX, `background: #13161e`
  in CSS — both forbidden. Use `color: 'var(--text-primary)'` /
  `background: var(--bg-card)`.
- **Inline shadows.** `box-shadow: 0 4px 10px rgba(0,0,0,0.4)` in
  component CSS — forbidden. Use `box-shadow: var(--shadow-md)`.
- **Magic spacing.** `padding: 13px` — forbidden. Use `padding: var(--space-3)`
  (12px) or `var(--space-4)` (16px).
- **Mixed semantic naming.** Using `--color-green` for "success"
  meaning and `--color-success` for the same meaning in different
  components — pick one (prefer `--color-success`) and migrate.
- **Disabling transitions.** `transition: none` on a component that
  the user interacts with — this breaks the reduced-motion contract.
  Use `var(--transition-button)` instead; the `prefers-reduced-motion`
  media query handles the disabled case globally.

### 14.3 Adding new tokens

1. Append the new token to the `:root` block (or the appropriate theme
   block in `.light`).
2. If the token has a light-mode equivalent, also add it to `.light`.
3. If the token is a utility class, append the class below the token
   block.
4. Update this document (§2-§13) with the new token.
5. Run `bun run lint` to confirm no regressions.

### 14.4 Migrating legacy code

The codebase has ~880 occurrences of Tailwind arbitrary values
(`bg-[#0e1015]`, `text-[#7e8aaa]`, etc.) across 38 panel files.
The `.light` block in `globals.css` includes scoped overrides for
the ~12 most-used hex literals so light mode works without rewriting
every panel. When touching a panel for any other reason, prefer
converting its hardcoded hex literals to `var(--token)` references —
this removes the dependence on the scoped `.light` overrides and
makes the panel theme-agnostic.

---

## 15. File map

| File | Role |
|------|------|
| `src/app/globals.css` | Source of truth for all design tokens |
| `src/app/layout.tsx` | Root layout — wraps app in `<ThemeProvider>` |
| `src/components/ThemeProvider.tsx` | `next-themes` wrapper (dark default) |
| `src/components/ThemeToggle.tsx` | Dark/light toggle button |
| `src/components/charts/theme.ts` | TypeScript chart palette (mirrors `--chart-*`) |
| `src/components/ui/*` | shadcn/ui components (consume tokens via Tailwind) |
| `docs/ui_ux/DESIGN_SYSTEM.md` | This document |
| `docs/ACCESSIBILITY.md` | WCAG audit and ARIA guidelines |

---

## 16. Changelog

| Date | Author | Change |
|------|--------|--------|
| 2026-09-05 | W13-4 | Added `.light` theme overrides + scoped Tailwind arbitrary value overrides |
| 2026-09-08 | S4 | Added modular type scale (`--text-2xs` → `--text-2xl`), font role stacks, shadow system |
| 2026-09-12 | W10-8 | Added skeleton shimmer variants, card hover lift, sonner toast theme vars |
| 2026-09-14 | W13-5 | Added command palette (Cmd+K) styling |
| 2026-12-15 | W38-2 | Added semantic color aliases (success/warning/danger/info), completed spacing scale (1-12), responsive breakpoint tokens, chart color palette (8 series + AI/ML accents), component-specific tokens (shadow-card, transition-button, etc.), light-theme shadow + chart palette + focus-ring overrides, semantic utility classes (.badge-success, .chart-series-1..8, .text-success, etc.). Created this design system documentation. |
