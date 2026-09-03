# Contributing to Polymarket Bot

## 1. Welcome

Thank you for your interest in contributing to the Polymarket trading bot.
This project combines a Next.js 16 + TypeScript dashboard with a Python 3.12
FastAPI trading engine and an ML ensemble. See the project README for a high
level overview of features and architecture before diving in.

We welcome contributions of all sizes: bug fixes, new UI panels, new API
routes, new ML models, tests, and documentation improvements.

## 2. Code of Conduct

By participating in this project you agree to the following expectations:

- **Be respectful.** Treat everyone with courtesy. Disagreement is fine;
  personal attacks are not.
- **Be constructive.** When reviewing code or filing issues, explain the
  problem and propose a path forward. Vague criticism wastes everyone's time.
- **Be inclusive.** Use welcoming, professional language. Harassment,
  discrimination, and exclusionary behaviour will not be tolerated.
- **Assume good faith.** Most mistakes are unintentional. Help contributors
  understand what went wrong and how to fix it.
- **Focus on the work.** Critique code and design decisions, not the person
  who wrote them.

Reports of conduct violations can be sent to the maintainers listed in the
README. All reports are handled confidentially.

## 3. Getting Started

### 3.1 Prerequisites

- Node.js 20+ and the `bun` runtime (https://bun.sh)
- Python 3.12+
- Git 2.30+

### 3.2 Clone the repository

```bash
git clone https://github.com/armand-ratombotiana/polymarket-bot-ai.git
cd polymarket-bot-ai
```

### 3.3 Install frontend dependencies

```bash
bun install
```

### 3.4 Install backend dependencies

```bash
pip install -r mini-services/polymarket-bot/requirements.txt
```

For ML work you may also want `lightgbm` (already listed in requirements).
The 4th ensemble member falls back gracefully if LightGBM is unavailable.

### 3.5 Configure environment

Copy the environment template for each side of the stack:

```bash
# Frontend (Next.js /api/bot proxy + dashboard)
cp .env .env.local
# Edit .env.local: set API_TOKEN to match the backend value.

# Backend (FastAPI trading engine)
cd mini-services/polymarket-bot
cp .env .env
# Edit .env: set TRADING_MODE=paper, set API_TOKEN, risk limits, paths.
```

The two `API_TOKEN` values must match. The Next.js route handler at
`src/app/api/bot/route.ts` proxies authenticated requests to the FastAPI
backend using this shared bearer token.

### 3.6 Start the dev servers

From the repository root:

```bash
bun run dev
```

This launches `next dev -p 3000` and tees output to `dev.log`. The Next.js
dashboard auto-starts the backend on first `/api/bot?action=start` request
from the dashboard, or you can start it manually:

```bash
cd mini-services/polymarket-bot
python main.py serve --port 8080
```

### 3.7 Run the tests

The backend has the test suite. From `mini-services/polymarket-bot`:

```bash
python -m pytest tests/ -v
```

A `conftest.py` in `tests/` provides shared fixtures (test data store,
temp DBs, isolated config). pytest configuration lives in `pytest.ini`.

## 4. Project Structure

```
.
+- src/                          # Next.js 16 frontend (app router, TS)
|  +- app/                       #   routes, layout, page.tsx, /api/bot proxy
|  +- components/                #   panels, header, sidebar, ui (shadcn)
|  +- hooks/                     #   useBot, useToast, useMobile
|  +- lib/                       #   api.ts (apiFetch), utils, design-tokens
+- mini-services/polymarket-bot/ # Python 3.12 FastAPI backend + ML
|  +- api/server.py              #   FastAPI app + 77 routes
|  +- core/                      #   trading engine, safety, persistence
|  +- ml/                        #   ensemble, drift, shadow, copilot
|  +- strategies/                #   market maker, arb, signal trader
|  +- paper/                     #   paper-trade simulator
|  +- risk/                      #   risk manager, exposure caps
|  +- tests/                     #   pytest suite
|  +- data/                      #   SQLite DBs, model.pkl, state (gitignored)
+- docs/                         #   long-form documentation
+- prisma/                       #   Prisma schema + migrations (if used)
+- public/                       #   static assets served by Next.js
+- Caddyfile                     #   reverse proxy + SSL gateway (production)
+- package.json                  #   bun/next scripts + JS deps
```

For component-level wiring, data flow, and per-module responsibilities, see
`docs/ARCHITECTURE.md` (created in a later task).

## 5. Development Workflow

We use a standard feature-branch flow.

1. **Create a branch** off `main`:

   ```bash
   git checkout main
   git pull --ff-only
   git checkout -b feat/your-feature
   ```

2. **Make your changes.** Keep commits focused and reviewable.

3. **Run the linter** before committing:

   ```bash
   bun run lint
   ```

4. **Run the tests** (only relevant if you touched the backend):

   ```bash
   cd mini-services/polymarket-bot && python -m pytest tests/ -v
   ```

5. **Commit with conventional commits.** Use one of these prefixes:

   | Prefix      | When to use                                         |
   | ----------- | --------------------------------------------------- |
   | `feat:`     | New feature or capability                           |
   | `fix:`      | Bug fix                                             |
   | `docs:`     | Documentation only (README, CONTRIBUTING, DEPLOYMENT)|
   | `test:`     | Test additions or fixes                             |
   | `refactor:` | Code restructuring with no behaviour change         |
   | `chore:`    | Tooling, deps, configs                              |

   Example:

   ```bash
   git commit -m "feat(ml): add LightGBM shadow inference path"
   ```

6. **Push and open a PR.**

   ```bash
   git push -u origin feat/your-feature
   ```

   Open the PR against `main`. Fill in the PR template (what changed, why,
   how to test, screenshots for UI changes).

## 6. Coding Standards

### 6.1 Frontend (Next.js 16 + TypeScript)

- **TypeScript strict.** No `any` types in committed code. Use `unknown` and
  narrow with type guards if needed.
- **shadcn/ui components.** Prefer the components in `src/components/ui/`. Do
  not pull in raw Radix primitives directly when a shadcn wrapper exists.
- **`'use client'` directive** at the top of any component that uses hooks,
  `window`, or `localStorage`. Use `dynamic(() => import(...), { ssr:false })`
  in `page.tsx` for panels that touch browser APIs at module scope.
- **`apiFetch` for all requests.** Never call `fetch` directly; `apiFetch`
  (in `src/lib/api.ts`) injects the bearer token, base URL, and handles
  errors uniformly.
- **CSS variables only.** Use the design tokens defined in
  `src/lib/design-tokens.ts` and the CSS variables in `globals.css`. Never
  hardcode hex colors inline.
- **Polling pattern.** Use a `useEffect` with `setInterval`, pause when
  `document.hidden` is true (visibilitychange listener), and clean up on
  unmount. See existing panels (e.g. `RetentionPanel.tsx`) for the pattern.

### 6.2 Backend (Python 3.12 + FastAPI)

- **Python 3.12+.** Use modern syntax (PEP 695 type aliases, `match`
  statements where appropriate, `from __future__ import annotations`).
- **Type hints required** on all public function signatures. Use
  `pydantic` models for request/response schemas.
- **Docstrings on public functions and classes.** Triple-quoted, describe
  args, return value, and any side effects.
- **SQLite for persistence.** No external DB dependencies required for
  development. `asyncpg`/TimescaleDB is supported in production but optional.
- **No print() in libraries.** Use the module-level `log = logging.getLogger
  (__name__)` pattern.
- **Config via `config.py`.** Read environment variables through
  `pydantic-settings` (`settings`). Do not call `os.environ.get` directly in
  library code.

### 6.3 Tests

- **One test file per module.** `core/attribution.py` -> `tests/test_attribution.py`.
- **Descriptive names.** Use the `test_<behavior>_<condition>` convention:
  `test_risk_manager_blocks_order_when_exposure_exceeded`.
- **Use `conftest.py` fixtures.** Common fixtures (test data store, temp
  SQLite paths, isolated config) live in `tests/conftest.py`. Do not
  duplicate setup logic across test files.
- **No network in tests.** Mock `httpx`/`websockets` clients. Tests must
  run offline and deterministically.
- **Aim for coverage.** New code should ship with tests. Critical paths
  (risk manager, safety gate, order state machine) require 100% coverage
  of branches touching money flow.

## 7. Adding a New UI Panel

A panel is a single React component mounted in `src/app/page.tsx` and
surfaced through the sidebar. Follow these steps:

1. **Create the component.**

   ```bash
   touch src/components/XxxPanel.tsx
   ```

   - Start with `'use client';`.
   - Default-export the component.
   - Use `apiFetch` for all data; follow the polling pattern from §6.1.
   - Include a loading skeleton, an error state with a Retry button, and
     an empty state. See `RetentionPanel.tsx` for the canonical layout.

2. **Add a NavSection to the sidebar.**

   Open `src/components/Sidebar.tsx`:

   - Extend the `NavSection` union type with `'group-xxx'`.
   - Add the entry to the appropriate `NAV_GROUPS` array (or create a new
     group). Each NavItem needs `id`, `label`, `shortLabel`, `icon`, and
     `group`.

3. **Wire it into `page.tsx`.**

   Open `src/app/page.tsx`:

   - Import the panel via `dynamic(() => import('@/components/XxxPanel'), { ssr: false })`.
   - Add a conditional render block in the `page-area` div:
     ```tsx
     {activeSection === 'group-xxx' && (
       <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
         <XxxPanel />
       </div>
     )}
     ```

4. **Add the API route the panel consumes** (see §8 if a new route is needed).

5. **Verify:**

   - `bun run lint` is clean.
   - `bunx tsc --noEmit` reports no new errors in the touched files.
   - The dev server (`bun run dev`) compiles the panel without warnings.

## 8. Adding a New API Route

Most routes live in `mini-services/polymarket-bot/api/server.py`. Routes
specific to a subsystem can also be registered through a `register_routes
(app)` function in the relevant module (e.g. `core/retention.py`,
`ml/routes.py`, `risk/routes.py`).

1. **Choose where the route belongs.**
   - Generic / cross-cutting -> `api/server.py`.
   - Module-specific -> create `register_routes(app)` in that module and
     call it from `api/server.py` during lifespan startup.

2. **Add Bearer auth.** Every non-public route must use the shared
   `require_token` dependency (or equivalent) defined near the top of
   `api/server.py`:

   ```python
   from fastapi import Depends
   from api.server import require_token

   @app.get("/api/xxx", dependencies=[Depends(require_token)])
   async def get_xxx() -> dict:
       ...
   ```

3. **Add input validation.** Use `pydantic` for request bodies and
   `Query(...)` for query-string params with defaults and ranges. Never
   trust raw `request.json()`.

4. **Document the response.** Add a `response_model=` and a docstring.
   The docstring is surfaced by FastAPI's `/docs` explorer.

5. **Write tests.** Create or extend `tests/test_<module>.py`. Use
   `TestClient(app)` (from `fastapi.testclient`) with the bearer token
   injected via a fixture.

6. **Verify:**

   ```bash
   cd mini-services/polymarket-bot
   python -m pytest tests/test_<module>.py -v
   ```

## 9. Adding a New ML Model

The ML stack lives in `mini-services/polymarket-bot/ml/`. Models are
composed into an ensemble and run in shadow mode before promotion.

1. **Implement the model class.** Add a new module under `ml/`, e.g.
   `ml/my_model.py`. Implement `fit(X, y)`, `predict_proba(X)`, and a
   `name` property. Follow the interface in `ml/model.py`.

2. **Register in the ensemble.** Edit `ml/ensemble_meta_learner.py` (or
   the relevant orchestrator) to include your model in the ensemble
   averaging. Assign it a weight and an `enabled` flag.

3. **Add shadow inference.** Hook the new model into
   `ml/shadow_inference.py` so its predictions are recorded without
   affecting live order flow. Shadow trades land in the `shadow_trades.db`
   SQLite database.

4. **Add drift tracking.** If the model emits feature importances or
   calibration metrics, wire them into `ml/drift_detector.py` so the
   ML Validation panel surfaces them.

5. **Write tests.** Create `tests/test_my_model.py` covering fit/predict
   shapes, monotonic response to a clear signal, and graceful degradation
   when the model file is missing.

6. **Verify the ensemble:**

   ```bash
   cd mini-services/polymarket-bot
   python -m pytest tests/test_meta_learner.py tests/test_my_model.py -v
   ```

7. **Do not enable live trading.** New models stay in shadow mode until
   the 10-check live safety gate (`core/live_safety_gate.py`) confirms
   performance meets the promotion criteria.

## 10. Testing Guidelines

- **Run the whole suite before pushing:**

  ```bash
  cd mini-services/polymarket-bot && python -m pytest tests/ -v
  ```

- **Run a single test file during development:**

  ```bash
  python -m pytest tests/test_attribution.py -v
  ```

- **Run a single test:**

  ```bash
  python -m pytest tests/test_attribution.py::test_attribution_returns_zero_when_no_trades -v
  ```

- **Coverage:**

  ```bash
  python -m pytest tests/ --cov=. --cov-report=term-missing
  ```

- **Write tests for every new feature.** Untested code will be blocked at
  PR review.
- **Frontend tests** are not yet wired; for now, verify UI changes by
  running `bun run dev` and exercising the panel manually. Lint + tsc
  must be clean.

## 11. Pull Request Process

1. **Self-review your PR** before requesting review. Re-read the diff,
   check for leftover debug code, console.log statements, and commented
   out blocks.
2. **Ensure CI passes.** The CI pipeline runs `bun run lint` and
   `python -m pytest tests/`. Both must be green.
3. **Request review** from at least one maintainer. Tag relevant
   reviewers based on the area (frontend / backend / ML).
4. **Address feedback** in new commits pushed to the same branch. Do not
   force-push once review has started unless asked.
5. **Squash on merge.** Maintainers squash-merge PRs into `main`. The
   squashed commit message follows conventional-commit format.
6. **Delete the branch** after merge.

## 12. Release Process

Releases are cut from `main` after the live safety gate has been re-run
against the candidate commit.

1. **Bump the version** in `package.json` and `mini-services/polymarket-bot/pyproject.toml`.
   Use semantic versioning: `MAJOR.MINOR.PATCH`.
2. **Update the changelog.** Append a dated entry summarising user-facing
   changes since the last release.
3. **Run the full test suite** one final time:

   ```bash
   cd mini-services/polymarket-bot && python -m pytest tests/ -v
   ```

4. **Tag the release:**

   ```bash
   git tag -a v0.X.Y -m "Release v0.X.Y"
   git push origin v0.X.Y
   ```

5. **Deploy** following `docs/DEPLOYMENT.md` (build, restart services,
   run the post-deploy smoke checks).
6. **Announce** the release: post a summary to the project changelog
   and notify users of any breaking changes or required migrations.
7. **Monitor** for 24 hours after release. Watch `server.log`,
   `dev.log`, and the `/api/observability` endpoint for anomalies.
