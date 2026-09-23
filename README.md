# SignalIQ Actions (Sema4.ai)

Autonomous ad-tech diagnostics: capture traffic from web/mobile/CTV apps,
audit OpenRTB + VAST compliance, score revenue risk, generate RCA reports.

## Structure
- actions.py         - 10 Sema4 actions (the agent's tools)
- package.yaml       - Sema4 environment definition
- RUNBOOK.md         - agent instructions
- sample_capture.json- test data (broken CTV session)
- signaliq/          - the analysis engine (parser, classifier, validators,
                       scoring, audit+RCA, report, capture, proxy_capture)

## Actions
run_browser_capture, start_app_capture, check_app_capture, stop_app_capture,
diagnose_capture, get_session_traffic, audit_session, audit_traffic_json,
replay_request, list_sessions

## Setup in Sema4 Studio
1. Open this folder as an action package.
2. Let Studio build the environment (installs playwright + mitmproxy).
3. Create an agent, attach the package, paste RUNBOOK.md as instructions.
4. Test: paste sample_capture.json contents, ask the agent to audit it.

## Or: run the local dashboard (no Sema4 needed)
`dashboard/` is a standalone FastAPI + web UI that wraps the same engine.
Give it any website URL and it captures + audits it for you.
See `dashboard/README.md` for setup. Quick start:

```bash
pip install -r requirements-dashboard.txt
playwright install chromium
python -m dashboard.server
# open http://127.0.0.1:8787
```
