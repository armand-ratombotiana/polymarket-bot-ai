// components/SWRegister.test.tsx — W42-2 component test.
//
// `SWRegister` is a client-only side-effect mount: it returns `null`
// and calls `registerServiceWorker()` exactly once on mount. The
// component has no visual footprint, so the only behaviour worth
// asserting in isolation is "renders without crashing" (i.e. the
// useEffect wiring does not throw during mount/unmount in jsdom).
// Deeper assertions on the SW registration live in
// `lib/registerSW.test.ts`.

import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import SWRegister from './SWRegister'

describe('SWRegister', () => {
  it('renders without crashing', () => {
    const { container } = render(<SWRegister />)
    expect(container.firstChild).toBeNull() // Renders null
  })
})
