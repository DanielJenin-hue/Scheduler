# Business Revenue Cockpit — Production Verdict

**Date:** 2026-06-19  
**North star:** $2,000 CAD/mo MRR ($800 managed block + $299/mo Pro seats)

## Bugs fixed

| Issue | Fix |
|-------|-----|
| `StreamlitAPIException` on Preview email / tab navigation | Introduced `business_tab_pending` via `navigation.py`; `apply_pending_business_tab()` runs **before** `st.radio(key="business_tab")` |
| All post-widget tab writes | Replaced direct `st.session_state["business_tab"] = …` with `request_business_tab()` in `section.py` and `app.py` |
| Weak gather feedback | Toast + success banner with prospect count and explicit next action (Preview email) |
| Proceed confirmation lacked revenue context | $800 block + $299/mo Pro path shown in confirmation box |
| Email preview trust gap | Facility name in header caption; honesty blocklist warning retained |
| Missing prospect error state | Clear error + Back to Prospects when prospect ID is stale |

## Button flow audit

| Control | Status |
|---------|--------|
| Gather prospects (landing, pipeline, prospects, empty states) | Uses pending tab → Prospects + toast |
| Preview email (prospect cards) | Pending tab → Email Preview + toast |
| Pass | Toast + rerun (same tab) |
| Copy / Save draft / mailto | Toast or inline feedback |
| Proceed with client | Revenue confirmation → onboarding tab via pending |
| Back to Prospects / Go to Prospects / View Pipeline | Pending tab navigation |
| Open onboarding (pipeline) | Pending → Client Onboarding |
| View in Email Preview (onboarding) | Pending → Email Preview |
| Open Revenue Pipeline (`app.py`) | `force_ops_console` + pending Pipeline |
| Scheduling \| Business switch | `app_section` radio (separate key — OK) |
| Back to manager workspace | Clears `force_ops_console`, returns to Scheduling |

## Remaining human-only gaps

- **Deploy:** Production hosting, HTTPS, auth hardening, tenant isolation review
- **Send email:** Operator must copy/mailto — no automated SMTP (intentional)
- **Real outreach:** Verify contact emails, personalize drafts, follow-up cadence
- **Billing:** Invoice first $800 block and Pro subscription outside the app
- **Legal/compliance:** Privacy policy, Manitoba health-sector outreach norms

## Team confidence: **7 / 10**

The in-app revenue workflow is now navigable without Streamlit crashes, surfaces honest drafts, and ties actions to the $2,000 MRR path. Confidence is below 9 because production revenue still depends on deployment, verified contacts, and human send/close — not yet proven in a live pilot.

### Improved this pass

- Crash-free tab navigation pattern with unit tests
- Operator toasts and gather success banner
- Revenue-aware proceed confirmation
- Trustworthy email preview framing

### Still needed for 9+

1. End-to-end pilot with one real Manitoba lab (gather → preview → proceed → onboarding checklist complete)
2. Production deploy with non-demo auth and audit logging on prospect/tenant mutations
3. Optional: wire modular tab modules (`pipeline.py`, `prospects.py`, etc.) into `section.py` to reduce duplication
4. Contact enrichment (verified lab manager emails) before scaling gather

## Retest

```powershell
cd c:\Users\Danie\OneDrive\Pictures\Documents\lab_staffing_scheduler
$env:PYTHONPATH="src;."
streamlit run scripts/app.py
```

1. Sidebar → **Open Revenue Pipeline**
2. **Gather prospects** → toast + Prospects tab + green success banner
3. Click **Preview email** on a card → Email Preview tab (no crash)
4. **← Back to Prospects** → returns cleanly
5. **Proceed with client ▶** → confirm revenue box → **Proceed with client** → Client Onboarding
