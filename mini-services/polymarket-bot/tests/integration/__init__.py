"""W17-9 — Cross-module integration tests.

This sub-package contains integration tests that verify the full
end-to-end trading pipeline works across module boundaries:

  * ``test_decision_chain.py`` — PREDICTION → SIGNAL → RISK → ORDER → FILL
    decision ledger chain, ledger query consistency, execution-quality
    after a fill.
  * ``test_ml_pipeline.py`` — ML model train → predict → drift → retrain
    cycle, calibration improvement, shadow-inference champion/challenger
    recording.
  * ``test_risk_pipeline.py`` — Kill switch halt + recovery, circuit
    breaker state machine for external API calls, max-drawdown breaker.
  * ``test_observability_pipeline.py`` — Metrics record → health report →
    history query, alert evaluate → ack pipeline, profiling stats after
    HTTP requests.
  * ``test_cache_pipeline.py`` — TTLCache hit/miss cycle, TTL expiration,
    stats accuracy.

All tests run hermetically against ``tmp_path``-scoped SQLite files (or
``monkeypatch``ed module-level singletons) so they never interfere with
the production on-disk state or with sibling tests in the same suite.
"""
