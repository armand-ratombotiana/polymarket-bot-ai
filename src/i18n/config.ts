// i18n/config.ts — locale catalog + persistence primitives.
//
// The workstation ships with two locales today (`en`, `fr`). The catalog
// is the single source of truth that `next-intl`'s server config (see
// `./request.ts`), the `useTranslation` React hook (see
// `src/hooks/useTranslation.ts`), and the `LocaleSwitcher` UI (see
// `src/components/LocaleSwitcher.tsx`) all read from.
//
// `getLocale()` is SSR-safe — it short-circuits to `defaultLocale` when
// `window` is undefined so server components and Next.js's RSC payload
// rendering don't blow up on `localStorage` access.
//
// Persistence is intentionally a single `localStorage` key (`locale`) so
// the trader's choice survives reloads without coupling to `next-themes`
// or any other persisted UI state.

export const locales = ['en', 'fr'] as const
export type Locale = (typeof locales)[number]
export const defaultLocale: Locale = 'en'

/**
 * Read the active locale. On the server (or any non-browser env), returns
 * `defaultLocale`. On the client, returns the persisted locale if it's
 * still in the catalog, otherwise `defaultLocale`. Stale persisted values
 * (e.g. a removed locale) gracefully fall back instead of throwing.
 */
export function getLocale(): Locale {
  if (typeof window === 'undefined') return defaultLocale
  try {
    const stored = window.localStorage.getItem('locale')
    if (stored && (locales as readonly string[]).includes(stored)) {
      return stored as Locale
    }
  } catch {
    // localStorage may be disabled (privacy mode / sandboxed iframes);
    // fall through to defaultLocale instead of crashing the render.
  }
  return defaultLocale
}

/**
 * Persist the active locale. No-op on the server. Wraps the
 * `localStorage.setItem` call in try/catch for the same reason as
 * `getLocale()` — never throw on a write that fails.
 */
export function setLocale(locale: Locale): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem('locale', locale)
  } catch {
    // Best-effort persistence — UI still flips in-memory via the hook.
  }
}
