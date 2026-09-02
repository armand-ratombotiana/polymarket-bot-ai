# Event-Sourced Order Execution State Machine

## 1. Order Lifecycle State Machine

Every order follows a deterministic, immutable state machine persisted in `trading.order` and `trading.order_transition`.

```mermaid
stateDiagram-v2
    [*] --> INTENT_CREATED: Strategy Decision
    INTENT_CREATED --> RISK_VALIDATED: Risk Kernel Approves
    INTENT_CREATED --> RISK_REJECTED: Risk Kernel Fails
    RISK_VALIDATED --> SUBMISSION_PENDING: Enqueued in Outbox
    SUBMISSION_PENDING --> OPEN: Exchange/Simulator Ack
    SUBMISSION_PENDING --> REJECTED: Exchange Rejection
    OPEN --> PARTIALLY_FILLED: Partial Match
    OPEN --> FILLED: Full Match
    OPEN --> CANCEL_PENDING: Operator / Strategy Cancel
    CANCEL_PENDING --> CANCELLED: Cancellation Confirmed
    PARTIALLY_FILLED --> FILLED: Final Match
    PARTIALLY_FILLED --> CANCELLED: Remainder Cancelled
```

---

## 2. Transition Rules & Idempotency

* **Idempotency Keys**: Every order submission generates a deterministic UUID `client_order_id`. Retried submissions check for existing records to prevent double-execution.
* **Partial Fills**: Remaining size is updated dynamically upon fill execution.
* **Fill Ingestion**: Fills are recorded in `trading.fill` with unique fill IDs and attribution to strategy, model, and decision ID.
