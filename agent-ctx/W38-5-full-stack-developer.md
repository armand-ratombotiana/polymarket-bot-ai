# W38-5 — Explainable AI / ML Prediction Panel

**Agent:** full-stack-developer
**Task ID:** W38-5
**Scope:** NEW `src/components/AIPredictionExplainerPanel.tsx` + NEW `src/components/AIPredictionExplainerPanel.test.tsx` + EDIT `src/components/AIMLCommandCenter.tsx` (additive "NOT A GUARANTEE" disclaimer banner) + EDIT `src/components/Sidebar.tsx` + `src/app/page.tsx` (wire new "AI Prediction Explainer" nav item under Intelligence group) + EDIT `src/components/CommandPalette.tsx` (add nav command for the new panel).

## Goal
Make the AI / ML interface more explainable and trustworthy by:

1. **Clear labeling** — surface every model probability as `AI Prediction: X% YES (confidence: Y)` with a confidence-interval range, a "NOT A GUARANTEE" disclaimer, and a blue/purple color system that visually distinguishes AI-generated content from market data.
2. **Model vs Market comparison** — side-by-side card comparing the model's predicted P(YES) against the order-book mid price (the market-implied probability), with the edge estimate labelled.
3. **Explainability** — per-prediction "Why?" expandable section that calls `/api/ml/explain/{token_id}` and surfaces the top-3 SHAP feature contributions, plus champion-vs-challenger model agreement + drift status (OK/warning/critical).
4. **Prediction history** — last 20 predictions table with timestamps + token + side + prediction confidence + actual outcome (resolved vs pending), plus a calibration curve (predicted vs actual) backed by `/api/ml/metrics.reliability_curve`.
5. **Status audit** — single header strip that surfaces every required field from the task spec (model status / version / training-data timestamp / feature freshness / prediction probability / confidence score / calibration status / market-implied probability / edge estimate / drift indicators / data quality warnings).

The component is additive — it does not alter any existing panel's tests.
