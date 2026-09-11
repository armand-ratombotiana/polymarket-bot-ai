// components/ErrorReporterInit.test.tsx — W42-2 component test.
//
// `ErrorReporterInit` is a client-only side-effect mount: it returns
// `null` and installs the global `error` / `unhandledrejection` /
// `beforeunload` window listeners from `lib/errorReporter` exactly once
// per mount. The component has no visual footprint, so the only
// behaviour worth asserting in isolation is "renders without crashing"
// (i.e. the useEffect wiring does not throw during mount/unmount in
// jsdom). Deeper assertions on the listeners themselves live in
// `lib/errorReporter.test.ts`.

import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import ErrorReporterInit from './ErrorReporterInit'

describe('ErrorReporterInit', () => {
  it('renders without crashing', () => {
    const { container } = render(<ErrorReporterInit />)
    expect(container.firstChild).toBeNull() // Renders null
  })
})
