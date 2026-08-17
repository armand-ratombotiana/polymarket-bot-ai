# Decision Register (D1–D8)

Tracking for the stakeholder decision gates defined in
`docs/STRATEGIC_IMPROVEMENT_AND_IMPLEMENTATION_PLAN.md` §4 and §26.

Status legend: **OPEN** = not yet decided by a stakeholder; **DEFERRED** = default in force,
revisit at the milestone shown; **DECIDED** = stakeholder value recorded (name + date).

| Gate | Question | Recommended default | Status | Required by | Decided value |
|---|---|---|---|---|---|
| D1 | Role: research workstation or trading tool? | Credible paper-trading workstation with a gated live path; operating $100 / ceiling $200 / per-market $3 | OPEN (default active) | M1 | — |
| D2 | Persistence authority | TimescaleDB, after write-integrity proven (M3); sqlite stays cold standby | OPEN (default active) | M3 | — |
| D3 | Trading-mode ladder | paper → shadow (no orders) → live-small (caps) → full, each gated | OPEN (default active) | M7 | — |
| D4 | Symmetric settlement | Support both YES and NO outcomes of a market | OPEN (default active) | M7 | — |
| D5 | Feed strategy | Tiered REST polling (2 s/6 s); WS feed retired | OPEN (default active) | M4 | — |
| D6 | ML scope | Keep ensemble; pause `ml_random_forest_quant` by default until P3 gates | OPEN (default active) | M10 | — |
| D7 | Strategy scope | 3 core strategies implemented end-to-end; catalog stays as roadmap | OPEN (default active) | M6 | — |
| D8 | Packaging | Single docker-compose release with pinned lockfile + runbook | OPEN (default active) | M15 | — |

## Changelog

| Date | Gate | Action |
|---|---|---|
| 2026-08-17 | D1–D8 | Register created with defaults recorded (sprint 1, item 1) |
| 2026-08-17 | D1–D8 | Sprint 1 completed under defaults; containment-only work, no gate forced. Evidence in `docs/SPRINT_1_REPORT.md`. Next checkpoint: D2 at M3. |
