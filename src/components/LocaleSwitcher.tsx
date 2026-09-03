// components/LocaleSwitcher.tsx — compact locale dropdown.
//
// Renders as a tiny 2-letter dropdown (EN / FR) so the trader can flip
// the workstation's UI language without leaving the status bar. Uses
// inline styles so it inherits the dark dashboard aesthetic without
// pulling in the shadcn Select primitive (~12KB gzipped + a portal)
// for what is genuinely a 2-option control.
//
// The selected value is persisted via `useTranslation.setLocale` (which
// writes to `localStorage`) so the choice survives reloads. The flip is
// synchronous in-memory — no roundtrip — and any component using the
// `useTranslation` hook re-renders on the next React commit.

'use client'

import { useTranslation } from '@/hooks/useTranslation'
import { locales, type Locale } from '@/i18n/config'

export default function LocaleSwitcher() {
  const { locale, setLocale } = useTranslation()

  return (
    <select
      value={locale}
      onChange={(e) => setLocale(e.target.value as Locale)}
      aria-label="Select language"
      title="Switch UI language"
      style={{
        background: 'transparent',
        border: '1px solid var(--border)',
        color: 'var(--text-secondary)',
        borderRadius: '4px',
        padding: '2px 6px',
        fontSize: '11px',
        cursor: 'pointer',
        lineHeight: 1.2,
        height: '26px',
      }}
    >
      {locales.map((l) => (
        <option key={l} value={l}>
          {l.toUpperCase()}
        </option>
      ))}
    </select>
  )
}
