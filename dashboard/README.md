# SignalIQ Dashboard

A local web dashboard for the SignalIQ engine — no Sema4 required. Give it a
website URL, it opens the site in a real headless browser, scrolls it (so
lazy-loaded units actually fire), captures **both video and display** ad
traffic, and shows you:

- Signal Quality / Monetization Readiness / Daily Revenue Risk gauges
- **Vendor detection** across Display Ad Servers (Google Ad Manager/AdX),
  Header Bidding (Xandr, Magnite/Rubicon, PubMatic, OpenX, Index Exchange,
  Criteo, Amazon TAM, TripleLift, Sharethrough, Teads, The Trade Desk and
  more — see `signaliq/vendors.py`), Identity Sync (LiveRamp, ID5, UID2),
  Verification (IAS, DoubleVerify, Moat), and Consent/CMP calls
- Ad units and sizes requested, plus which GDPR/US-privacy/GPP/NPA consent
  signals actually went out on those calls
- The full **audience & identity signal profile** pulled out of the OpenRTB
  bid requests (device, geo, IFA, consent, identity providers, supply chain,
  bid floors, video specs)
- Every compliance anomaly with severity, actual vs. expected, and which
  requests it hit
- A full Root Cause Analysis with recommended engineering fixes
- The raw classified traffic log (now tagged with the detected vendor)
- A one-off request replay tool (e.g. re-fire a failing VAST tag)

It also keeps the existing paste-a-HAR flow and the mobile/CTV mitmproxy
capture, all through the same UI.

## This must run on your machine, not in a sandbox

`run_browser_capture` launches a real Chromium via Playwright and needs
normal internet access to reach whatever site/ad-server you point it at.
Run it locally.

## Setup

```bash
cd signaliq-actions
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dashboard.txt
playwright install chromium
```

## Run

```bash
python -m dashboard.server
```

Then open **http://127.0.0.1:8787** in your browser.

(Equivalent: `uvicorn dashboard.server:app --reload --port 8787`.)

## Using it

1. **Capture Website** (left panel) — paste any URL, optional login
   credentials, and how long to record (default 45s). Click **Run capture**.
   Watch the status line; when it finishes the report renders automatically.
2. **Audience & Signals tab** — the aggregated audience/device/identity
   profile extracted from every OpenRTB request in the capture, plus a
   per-request breakdown table.
3. **Anomalies & RCA tab** — every compliance finding plus the engineered
   root-cause explanation and fix list.
4. **Raw Traffic tab** — every captured request, classified.
5. **Replay tab** — re-send any single request (e.g. a failing ad tag) and
   inspect the raw response.
6. **Paste Traffic** (left panel) — skip capture entirely and audit HAR/JSON
   you already have.
7. **Sessions** (left panel) — every past capture is saved under
   `sessions/*.json` and stays clickable here across restarts.

## Notes

- If a login form isn't detected (some sites use multi-step or SSO logins),
  leave credentials blank and log in manually by running with
  `headless=False` in `signaliq/capture.py::run_browser_capture` for that
  one run, or capture the session while already logged in via the mitmproxy
  (mobile/CTV) flow adapted to a desktop browser proxy.
- `duration_seconds` defaults to 60. During that window the browser now
  auto-scrolls the page (most display ad units are lazy-loaded and never
  fire a request if the page never scrolls), then returns near the top. For
  sites where a video ad break fires later in playback, or the page is very
  long, increase it further.
- The mobile/CTV proxy capture (`start_proxy`/`stop_proxy` under the hood) is
  reachable at `POST /api/app/start`, `GET /api/app/status`,
  `POST /api/app/stop` if you want to wire a UI tab for it later — the
  backend already supports it.
