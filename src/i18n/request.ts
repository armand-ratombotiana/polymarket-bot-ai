// i18n/request.ts — next-intl server-side request config.
//
// `next-intl/server` calls `getRequestConfig` once per incoming request
// to resolve which locale + message bundle to use for server components
// (and for `next-intl`'s `setRequestLocale` plumbing). The workstation
// uses a single-host App Router layout today (no `[locale]` segment) so
// we pin the locale to `defaultLocale` for the server payload. The
// client-side `useTranslation` hook (see `src/hooks/useTranslation.ts`)
// is what actually flips the visible UI based on the trader's
// `localStorage`-persisted choice; this server config is here so the
// `next-intl` plugin's compile-time checks + `NextIntlClientProvider`
// can resolve a messages bundle on first paint before hydration.
//
// The dynamic import keeps each locale's JSON out of the server's main
// bundle until it's actually needed, which matters once we grow past
// the current 2 locales.

import { getRequestConfig } from 'next-intl/server'
import { defaultLocale } from './config'

export default getRequestConfig(async () => {
  const locale = defaultLocale
  return {
    locale,
    messages: (await import(`../messages/${locale}.json`)).default,
  }
})
