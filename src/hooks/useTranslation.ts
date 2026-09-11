// hooks/useTranslation.ts — client-side i18n hook.
//
// The workstation's UI is rendered entirely on the client today (every
// panel is a client component that polls the bot REST API), so we don't
// need `next-intl`'s RSC integration for visible strings. This hook
// wraps a tiny dictionary lookup over `src/messages/{locale}.json` and
// re-renders on locale change.
//
// Design choices:
//  - `useState('en')` initial value keeps the FIRST render stable for SSR
//    + hydration — the same English strings are emitted by the server
//    payload and the first client render, then the `useEffect` flips to
//    the persisted locale on mount if needed (single scheduled re-render
//    instead of a hydration mismatch).
//  - `t()` is memoised per-locale via `useCallback` so consumers don't
//    re-render when an unrelated parent updates.
//  - Missing keys fall back to the key itself (prefixed with the locale
//    name is intentionally NOT done — keys are already unique strings
//    like `nav.command`, so seeing one in the UI is an obvious red flag
//    that the messages file is missing an entry, not a confused user).
//  - Non-string leaf values (arrays / objects) also fall back to the key
//    so the type contract (`t(): string`) is never violated.

'use client'

import { useState, useEffect, useCallback } from 'react'
import { getLocale, setLocale, type Locale } from '@/i18n/config'

// Bundled at build time — both locales ship to the client so the trader
// can flip instantly without a network roundtrip. The two files together
// are ~3KB gzipped, well under any reasonable First Load JS budget.
import en from '@/messages/en.json'
import fr from '@/messages/fr.json'

const messages: Record<Locale, typeof en> = { en, fr }

export function useTranslation() {
  // Initial state is the default locale so the first paint matches the
  // server payload. The mount effect then reconciles to the persisted
  // (or browser-default) locale on the client only.
  const [locale, setLocaleState] = useState<Locale>('en')

  useEffect(() => {
    const persisted = getLocale()
    if (persisted !== locale) {
      setLocaleState(persisted)
    }
  }, [locale])

  const t = useCallback(
    (key: string): string => {
      const parts = key.split('.')
      let result: unknown = messages[locale]
      for (const part of parts) {
        if (result && typeof result === 'object' && part in (result as Record<string, unknown>)) {
          result = (result as Record<string, unknown>)[part]
        } else {
          return key
        }
      }
      return typeof result === 'string' ? result : key
    },
    [locale],
  )

  const changeLocale = useCallback((newLocale: Locale) => {
    setLocale(newLocale)
    setLocaleState(newLocale)
  }, [])

  return { t, locale, setLocale: changeLocale }
}
