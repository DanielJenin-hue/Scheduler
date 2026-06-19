# FINISH_APP Iterations — $2,000 CAD MRR North Star

**Loop re-armed:** 2026-06-19 (detached `scripts/finish_app_loop.ps1`; logs `logs/finish_app_loop.log`, PID `logs/finish_app_loop.pid`)  
**Loop sentinel:** `AGENT_LOOP_TICK_FINISH_APP` (interval 1d / 86400s)  
**Stop loop:** `Stop-Process -Id (Get-Content logs/finish_app_loop.pid) -Force` (or ask agent to stop the loop)

---

## Iteration 1 — 2026-06-19

**Orchestrator:** goal-coordinator (all 11 subagents accountable)  
**North star:** $2,000 CAD/month MRR  
**Team confidence:** **7.5 / 10** (product) · **3 / 10** (revenue execution)

### Verification

| Check | Result |
|-------|--------|
| `pytest -q` (default suite) | **561 passed**, 192 deselected, 1 warning (`pytest.mark.slow` unregistered) — ~293s |
| `python scripts/rotation_rsi_gate.py` | **PASS** — 0 operational tally violations, 0 rotation invariant violations |
| Business tests (`test_business_ui`, `test_business_inbound`, `test_business_prospects`) | **33 passed** |

### Accountability scorecard (11 agents)

| Agent | Last contribution | Current gap | Grade |
|-------|-------------------|-------------|-------|
| **revenue-growth** | `REVENUE_2000_PLAN.md`, managed-first mix math, weekly outbound matrix | **0 outbound emails sent**; landing was trial-first until this iteration | **B** |
| **manager-value-qa** | Default pytest green (561/561), RSI PASS, union weekday-D fix, debug-log prune, demo creds env-gated | No live pilot publish bundle attached to invoice | **A** |
| **scheduling-rules-coordinator** | Sacred rotation canon, RSI clearance role in revenue plan | Pitch surfaces must stay evidence-backed on landing/outbound | **B+** |
| **ui-design-partner** | Business shell (5 tabs), hero, onboarding checklist, theme CSS | `section.py` still monolithic vs modular `pipeline.py` / `email_preview.py` | **B** |
| **goal-coordinator** | This iteration + `REVENUE_2000_PLAN.md` scorecard discipline | Cannot close deploy/outbound without human operator | **B+** |
| **production-runtime-partner** | Demo creds env-gated (`LAB_ALLOW_DEMO_ACCOUNTS`), `DEPLOY.md` checklist | **No public URL** — P0 revenue blocker | **C+** |
| **button-flow-qa** | `business_tab_pending` pattern; 7 mandatory flows PASS per `BUSINESS_PRODUCTION_VERDICT.md` | No live Streamlit smoke this iteration (code trace + unit tests only) | **A-** |
| **customer-relations** | Inbox tab wired (`inbox.py`, `19_business_inbound.sql`) | **No client thread processed**; no intake brief artifact | **C** |
| **subagent-roster-advisor** | `SUBAGENT_ROSTER_AUDIT.md` — roster healthy at 9→11 agents | Soft cap exceeded; brand/persuasion overlap risk | **B** |
| **brand-voice-partner** | Agent playbook defined; aligns with Port Optical tone | **Not invoked** on live templates/landing copy polish | **D** |
| **persuasion-psychology-partner** | Agent playbook defined; ethical hooks framework | **Not invoked**; no subject-line A/B briefs shipped | **D** |

### button-flow-qa — session_state audit

**Direct `business_tab` violations (production code):** **NONE** — all tab jumps use `request_business_tab()` / `apply_pending_business_tab()` before `st.radio(key="business_tab")`. Only test assertions reference `state["business_tab"]`.

| # | Mandatory flow | Status | Evidence |
|---|----------------|--------|----------|
| 1 | Open Revenue Pipeline | **PASS** | `app.py` → `request_business_tab(..., "Pipeline")` + `force_ops_console` |
| 2 | Scheduling \| Business nav | **PASS** | `app_section` radio separate key |
| 3 | Gather prospects | **PASS** | `_run_auto_gather` → toast + pending Prospects |
| 4 | Preview email | **PASS** | `request_business_tab` → Email Preview |
| 5 | Proceed with client | **PASS** | Confirm box + onboarding tab via pending |
| 6 | Pass | **PASS** | `_pass_prospect` + toast |
| 7 | Back to manager workspace | **PASS** | Clears `force_ops_console`, returns Scheduling |

### Prune check — P0 from `BUSINESS_CODEBASE_AUDIT`

| P0 item | Status |
|---------|--------|
| `scripts/archive/` removed | **DONE** (2026-06-19 audit actions) |
| Demo credentials env-gated / not in prod | **DONE** — `LAB_ALLOW_DEMO_ACCOUNTS` required |
| Production host + persistent DB | **OPEN** — human deploy action |
| Custom domain + HTTPS + `APP_BASE_URL` | **OPEN** — human deploy action |

### UI / deploy / email — top 3 blockers to unanimous 100%

1. **Production deploy** — no live `manager_app.py` URL; Stripe return URLs blocked (`production-runtime-partner` **veto**)
2. **Zero outbound execution** — pipeline empty until human Gather → mailto → follow-up (`revenue-growth` **veto**)
3. **No paying pilot** — managed $800 block not invoiced; no testimonial (`customer-relations` + `manager-value-qa` **veto**)

### Fix shipped (Iteration 1)

| Item | Why | Files |
|------|-----|-------|
| Managed-first landing CTA + $800 block card | Aligns GTM with fastest path to $2k (managed-heavy mix); trial demoted to secondary CTA | `deploy/landing.html` |
| Toast key `biz_toast` → `business_toast` | Proceed-complete toast in modular `email_preview.py` would silently fail when wired | `src/lab_scheduler/ui/business/email_preview.py` |

### Unanimous verdict — 100% production-ready?

**NO** — confidence **72%** toward product ship, **28%** toward revenue-ready.

| Agent | Would veto? | Reason |
|-------|-------------|--------|
| revenue-growth | **YES** | No outbound, no pipeline proof |
| manager-value-qa | **YES** | No client publish bundle |
| scheduling-rules-coordinator | **NO** | RSI PASS; rules intact |
| ui-design-partner | **NO** | Business UX shippable for operator-led pilot |
| goal-coordinator | **YES** | Human deploy + first $ not closed |
| production-runtime-partner | **YES** | No public host |
| button-flow-qa | **NO** | Tab/button patterns clean |
| customer-relations | **YES** | No live intake thread |
| subagent-roster-advisor | **NO** | Roster adequate; invoke brand/persuasion on outbound |
| brand-voice-partner | **YES** | Email templates not polish-passed for send |
| persuasion-psychology-partner | **YES** | No psychology brief for first-touch |

**Agents that would sign off today (4/11):** scheduling-rules-coordinator, ui-design-partner, button-flow-qa, subagent-roster-advisor.

### Next tick priority

1. **production-runtime-partner** — deploy `manager_app.py` (Streamlit Cloud/Railway + persistent `LAB_SCHEDULER_DB_PATH`, `LAB_SCHEDULER_ENV=production`, no demo accounts)
2. **revenue-growth + brand-voice + persuasion-psychology** — Gather 5 Manitoba prospects → psychology brief → polished first-touch → send 5 mailtos
3. **manager-value-qa** — attach RSI PASS + breakroom HTML to deploy smoke URL before calling ship

---

*Iteration 1 logged by goal-coordinator. Loop armed: `AGENT_LOOP_TICK_FINISH_APP` every 86400s.*

---

## Iteration 2 — 2026-06-19

**Orchestrator:** goal-coordinator (all 11 subagents accountable)  
**North star:** $2,000 CAD/month MRR  
**Team confidence:** **8 / 10** (product) · **3 / 10** (revenue execution)

### Verification

| Check | Result |
|-------|--------|
| `pytest -q` (default suite) | **564 passed**, 192 deselected, **0 warnings** (registered `slow` marker) — ~281s |
| `python scripts/rotation_rsi_gate.py` | **PASS** — 0 operational tally violations, 0 rotation invariant violations |
| Business tests (`test_business_ui`, `test_business_inbound`, `test_business_prospects`) | **36 passed** |

### Accountability scorecard (11 agents)

| Agent | Last contribution | Current gap | Grade |
|-------|-------------------|-------------|-------|
| **revenue-growth** | `REVENUE_2000_PLAN.md`, managed-first mix, sidebar 3-step path | **0 outbound emails sent**; pipeline unproven in production | **B** |
| **manager-value-qa** | 564/564 pytest green, RSI PASS, pruned `auto_generate` debug log block | No live pilot publish bundle on hosted URL | **A** |
| **scheduling-rules-coordinator** | RSI PASS; cleared landing copy (customer-facing “compliance check”, not RSI acronym) | Pitch surfaces must stay evidence-backed on live sends | **A-** |
| **ui-design-partner** | Business shell (5 tabs), envelope preview, revenue path, theme CSS | `section.py` still monolithic; modular `pipeline.py` / `prospects.py` / `email_preview.py` unused | **B** |
| **goal-coordinator** | Iteration 2 orchestration + scorecard discipline | Cannot close deploy/outbound without human operator | **B+** |
| **production-runtime-partner** | Demo creds env-gated; `DEPLOY.md` human-only checklist added | **No public URL** — P0 revenue blocker | **C+** |
| **button-flow-qa** | `business_tab_pending` + `app_section_pending` patterns verified; 7 flows PASS | No live Streamlit smoke this iteration (code trace + unit tests only) | **A-** |
| **customer-relations** | Inbox tab wired; onboarding checklist in Business | **No client thread processed**; no intake brief artifact | **C** |
| **subagent-roster-advisor** | Roster at 11 agents; boundaries documented | Soft cap exceeded; brand/persuasion overlap risk | **B** |
| **brand-voice-partner** | Email subject/CTA polish; landing RSI→compliance check (customer-facing) | Templates not live-send reviewed with operator | **C+** |
| **persuasion-psychology-partner** | Subject-line curiosity hook (“ready for a quick look?”); softer CTA framing | **No psychology brief** or subject-line A/B artifact for first 5 sends | **D+** |

### button-flow-qa — session_state audit

**Direct `business_tab` violations (production code):** **NONE** — all tab jumps use `request_business_tab()` / `apply_pending_business_tab()` before `st.radio(key="business_tab")`.

**Direct `app_section` violations (production code):** **NONE** — `request_app_section()` / `apply_pending_app_section()` used in `scripts/app.py` for Open Revenue Pipeline and Back to manager workspace. Unit tests confirm pending pattern (`test_request_app_section_queues_pending_navigation`, `test_apply_pending_app_section_before_widget_render`).

| # | Mandatory flow | Status | Evidence |
|---|----------------|--------|----------|
| 1 | Open Revenue Pipeline | **PASS** | `app.py` → `request_app_section(Business)` + `request_business_tab(Pipeline)` + `force_ops_console` |
| 2 | Scheduling \| Business nav | **PASS** | `apply_pending_app_section` before `st.radio(key="app_section")` |
| 3 | Gather prospects | **PASS** | `_run_auto_gather` → toast + `request_business_tab(Prospects)` |
| 4 | Preview email | **PASS** | `request_business_tab(Email Preview)` + prospect id in session |
| 5 | Proceed with client | **PASS** | Confirm box + `request_business_tab(Client Onboarding)` |
| 6 | Pass | **PASS** | `_pass_prospect` + toast |
| 7 | Back to manager workspace | **PASS** | `request_app_section(Scheduling)` + clears `force_ops_console` + rerun |

### Prune check — P0 from `BUSINESS_CODEBASE_AUDIT`

| P0 item | Status |
|---------|--------|
| `scripts/archive/` removed | **DONE** (confirmed absent) |
| Demo credentials env-gated / not in prod | **DONE** — `LAB_ALLOW_DEMO_ACCOUNTS` + `LAB_SCHEDULER_ENV=production` |
| `auto_generate.py` agent debug log block | **DONE** — removed after RSI PASS |
| Modular stubs unused (`pipeline.py`, `prospects.py`, `email_preview.py`) | **DEFER** — wired in `section.py` next iteration; not dead imports |
| Production host + persistent DB | **OPEN** — human deploy action |
| Custom domain + HTTPS + `APP_BASE_URL` | **OPEN** — human deploy action |

### Fixes shipped (Iteration 2)

| Item | Why | Files |
|------|-----|-------|
| Register `slow` pytest marker | Eliminate PytestUnknownMarkWarning on default runs | `pyproject.toml` |
| Email subject + CTA polish | Brand-voice + persuasion: shorter subject, warmer CTA; scheduling-rules cleared claims | `src/lab_scheduler/business/email_templates.py`, `src/lab_scheduler/ui/business/helpers.py` |
| Landing customer-facing compliance copy | Remove RSI acronym from public landing; keep accurate footer/compliance claim | `deploy/landing.html` |
| Prune Auto-Pilot debug instrumentation | manager-value-qa: remove post-verification debug log block | `src/lab_scheduler/legacy/auto_generate.py` |
| Human-only deploy checklist | production-runtime-partner: clarify operator steps code cannot close | `deploy/DEPLOY.md` |

### Unanimous verdict — 100% production-ready?

**NO** — confidence **78%** toward product ship, **30%** toward revenue-ready.

| Agent | Would veto? | Reason |
|-------|-------------|--------|
| revenue-growth | **YES** | No outbound, no pipeline proof on live URL |
| manager-value-qa | **YES** | No client publish bundle on hosted smoke URL |
| scheduling-rules-coordinator | **NO** | RSI PASS; rotation claims cleared for copy changes |
| ui-design-partner | **NO** | Business UX shippable for operator-led pilot |
| goal-coordinator | **YES** | Human deploy + first $ not closed |
| production-runtime-partner | **YES** | No public host |
| button-flow-qa | **NO** | Tab/section pending patterns clean; 7/7 flows pass |
| customer-relations | **YES** | No live intake thread |
| subagent-roster-advisor | **NO** | Roster adequate; invoke brand/persuasion on outbound |
| brand-voice-partner | **YES** | Copy polish started; no operator sign-off on live sends |
| persuasion-psychology-partner | **YES** | No psychology brief for first-touch batch |

**Agents that would sign off today (4/11):** scheduling-rules-coordinator, ui-design-partner, button-flow-qa, subagent-roster-advisor. *Sign-off count unchanged vs Iteration 1; product confidence up (+5%), revenue blockers unchanged.*

### Next tick priorities (ranked)

1. **production-runtime-partner** — deploy `scripts/app.py` (Streamlit Cloud/Railway + persistent `LAB_SCHEDULER_DB_PATH`, `LAB_SCHEDULER_ENV=production`, no demo accounts)
2. **revenue-growth + brand-voice + persuasion-psychology** — psychology brief for top 5 MB targets → polished first-touch → human sends 5 mailtos
3. **manager-value-qa** — attach RSI PASS + breakroom HTML to deploy smoke URL before calling ship
4. **ui-design-partner** — wire modular `email_preview.py` into `section.py` (reduce duplication) once live smoke confirms flows

---

*Iteration 2 logged by goal-coordinator.*

---

## Iteration 3 — 2026-06-19

**Orchestrator:** goal-coordinator (all 11 subagents accountable)  
**North star:** $2,000 CAD/month MRR  
**Team confidence:** **8 / 10** (product) · **3 / 10** (revenue execution)

### Verification

| Check | Result |
|-------|--------|
| `pytest -q` (default suite) | **564 passed**, 192 deselected, **0 warnings** — ~303s (no `PYTHONPATH` required after fix) |
| `python scripts/rotation_rsi_gate.py` | **PASS** — 0 operational tally violations, 0 rotation invariant violations |
| Business tests (`test_business_ui`, `test_business_inbound`, `test_business_prospects`) | **36 passed** |

### Accountability scorecard (11 agents)

| Agent | Last contribution | Current gap | Grade |
|-------|-------------------|-------------|-------|
| **revenue-growth** | `REVENUE_2000_PLAN.md`, managed-first mix, sidebar 3-step path | **0 outbound emails sent**; pipeline unproven on live URL | **B** |
| **manager-value-qa** | 564/564 pytest green (no env hack), RSI PASS | No live pilot publish bundle on hosted smoke URL | **A** |
| **scheduling-rules-coordinator** | RSI PASS; compliance-check copy on landing/outbound | Pitch surfaces must stay evidence-backed on live sends | **A-** |
| **ui-design-partner** | Business shell (5 tabs), envelope preview, revenue path, theme CSS | `section.py` still monolithic; modular `email_preview.py` unused | **B** |
| **goal-coordinator** | Iteration 3 orchestration + scorecard discipline | Cannot close deploy/outbound without human operator | **B+** |
| **production-runtime-partner** | Demo creds env-gated; `DEPLOY.md` human-only checklist | **No public URL** — P0 revenue blocker | **C+** |
| **button-flow-qa** | `business_tab_pending` + `app_section_pending` patterns verified; 7 flows PASS | No live Streamlit smoke this iteration (code trace + unit tests only) | **A-** |
| **customer-relations** | Inbox tab wired; onboarding checklist in Business | **No client thread processed**; no intake brief artifact | **C** |
| **subagent-roster-advisor** | Roster at 11 agents; boundaries documented | Soft cap exceeded; brand/persuasion overlap risk | **B** |
| **brand-voice-partner** | Email subject/CTA polish; landing compliance-check copy | Templates not operator-reviewed on live sends | **C+** |
| **persuasion-psychology-partner** | `FIRST_TOUCH_PSYCHOLOGY_BRIEF.md` — subject A/B, body structure, reply path | **No operator sign-off**; 0 mailtos sent; A/B not executed | **C+** |

### button-flow-qa — session_state audit

**Direct `business_tab` violations (production code):** **NONE** — all tab jumps use `request_business_tab()` / `apply_pending_business_tab()` before `st.radio(key="business_tab")`.

**Direct `app_section` violations (production code):** **NONE** — `request_app_section()` / `apply_pending_app_section()` used in `scripts/app.py`.

| # | Mandatory flow | Status | Evidence |
|---|----------------|--------|----------|
| 1 | Open Revenue Pipeline | **PASS** | `app.py` → `request_app_section(Business)` + `request_business_tab(Pipeline)` + `force_ops_console` |
| 2 | Scheduling \| Business nav | **PASS** | `apply_pending_app_section` before `st.radio(key="app_section")` |
| 3 | Gather prospects | **PASS** | `_run_auto_gather` → toast + `request_business_tab(Prospects)` |
| 4 | Preview email | **PASS** | `request_business_tab(Email Preview)` + prospect id in session |
| 5 | Proceed with client | **PASS** | Confirm box + `request_business_tab(Client Onboarding)` |
| 6 | Pass | **PASS** | `_pass_prospect` + toast |
| 7 | Back to manager workspace | **PASS** | `request_app_section(Scheduling)` + clears `force_ops_console` + rerun |

### Prune check — P0 from `BUSINESS_CODEBASE_AUDIT`

| P0 item | Status |
|---------|--------|
| `scripts/archive/` removed | **DONE** (confirmed absent) |
| Demo credentials env-gated / not in prod | **DONE** — `LAB_ALLOW_DEMO_ACCOUNTS` + `LAB_SCHEDULER_ENV=production` |
| `auto_generate.py` agent debug log block | **DONE** — removed after RSI PASS |
| pytest import path (`scripts`, `tests` packages) | **DONE** — `pythonpath = ["src", "."]` in `pyproject.toml` |
| Modular stubs unused (`pipeline.py`, `prospects.py`, `email_preview.py`) | **DEFER** — wire into `section.py` once live smoke confirms flows |
| Production host + persistent DB | **OPEN** — human deploy action |
| Custom domain + HTTPS + `APP_BASE_URL` | **OPEN** — human deploy action |

### Fixes shipped (Iteration 3)

| Item | Why | Files |
|------|-----|-------|
| pytest `pythonpath` in pyproject | Default suite failed collection without `$env:PYTHONPATH="src;."` — 6 import errors on fresh runs | `pyproject.toml` |
| First-touch psychology brief | persuasion-psychology-partner: subject A/B hypotheses, body structure, friction checklist, reply path for batch 1 | `docs/FIRST_TOUCH_PSYCHOLOGY_BRIEF.md` |

### Unanimous verdict — 100% production-ready?

**NO** — confidence **79%** toward product ship (+1% vs Iteration 2), **30%** toward revenue-ready (unchanged).

| Agent | Would veto? | Reason |
|-------|-------------|--------|
| revenue-growth | **YES** | No outbound, no pipeline proof on live URL |
| manager-value-qa | **YES** | No client publish bundle on hosted smoke URL |
| scheduling-rules-coordinator | **NO** | RSI PASS; rotation claims cleared |
| ui-design-partner | **NO** | Business UX shippable for operator-led pilot |
| goal-coordinator | **YES** | Human deploy + first $ not closed |
| production-runtime-partner | **YES** | No public host |
| button-flow-qa | **NO** | Tab/section pending patterns clean; 7/7 flows pass |
| customer-relations | **YES** | No live intake thread |
| subagent-roster-advisor | **NO** | Roster adequate; brief closes persuasion artifact gap |
| brand-voice-partner | **YES** | Copy polish exists; no operator sign-off on live sends |
| persuasion-psychology-partner | **YES** | Brief shipped; operator must pick subjects and send 5 mailtos |

**Agents that would sign off today (4/11):** scheduling-rules-coordinator, ui-design-partner, button-flow-qa, subagent-roster-advisor. *Sign-off count unchanged; product hygiene improved (pytest path), persuasion artifact gap closed, revenue blockers still human-only.*

### Next tick priorities (ranked)

1. **production-runtime-partner** — deploy `scripts/app.py` (Streamlit Cloud/Railway + persistent `LAB_SCHEDULER_DB_PATH`, `LAB_SCHEDULER_ENV=production`, no demo accounts)
2. **revenue-growth + human operator** — Gather 5 Manitoba prospects → pick subject A/B from `FIRST_TOUCH_PSYCHOLOGY_BRIEF.md` → send 5 mailtos with Reply-To wired
3. **manager-value-qa** — attach RSI PASS + breakroom HTML to deploy smoke URL before calling ship
4. **ui-design-partner** — wire modular `email_preview.py` into `section.py` after live smoke confirms flows

---

*Iteration 3 logged by goal-coordinator.*

---

## Iteration 4 — 2026-06-19

**Orchestrator:** goal-coordinator (all 11 subagents accountable)  
**North star:** $2,000 CAD/month MRR  
**Team confidence:** **8 / 10** (product) · **3 / 10** (revenue execution)

### Verification

| Check | Result |
|-------|--------|
| `pytest -q` (default suite) | **565 passed**, 192 deselected, **0 warnings** — ~293s |
| `python scripts/rotation_rsi_gate.py` | **PASS** — 0 operational tally violations, 0 rotation invariant violations |
| Business tests (`test_business_ui`, `test_business_inbound`, `test_business_prospects`) | **37 passed** (+1 subject-variant test) |

### Accountability scorecard (11 agents)

| Agent | Last contribution | Current gap | Grade |
|-------|-------------------|-------------|-------|
| **revenue-growth** | `REVENUE_2000_PLAN.md`, managed-first mix, sidebar 3-step path | **0 outbound emails sent**; pipeline unproven on live URL | **B** |
| **manager-value-qa** | 565/565 pytest green, RSI PASS | No live pilot publish bundle on hosted smoke URL | **A** |
| **scheduling-rules-coordinator** | RSI PASS; compliance-check copy on landing/outbound | Pitch surfaces must stay evidence-backed on live sends | **A-** |
| **ui-design-partner** | Business shell (5 tabs), envelope preview, subject A/B/C picker in Email Preview | `section.py` still monolithic; modular `email_preview.py` unused | **B+** |
| **goal-coordinator** | Iteration 4 orchestration + scorecard discipline | Cannot close deploy/outbound without human operator | **B+** |
| **production-runtime-partner** | Demo creds env-gated; `DEPLOY.md` human-only checklist | **No public URL** — P0 revenue blocker | **C+** |
| **button-flow-qa** | `business_tab_pending` + `app_section_pending` patterns verified; 7 flows PASS | No live Streamlit smoke this iteration (code trace + unit tests only) | **A-** |
| **customer-relations** | Inbox tab wired; onboarding checklist in Business | **No client thread processed**; no intake brief artifact | **C** |
| **subagent-roster-advisor** | Roster at 11 agents; boundaries documented | Soft cap exceeded; brand/persuasion overlap risk | **B** |
| **brand-voice-partner** | Port Optical default sign-off; managed-first templates; landing compliance copy | **No operator-reviewed live sends** | **B-** |
| **persuasion-psychology-partner** | `FIRST_TOUCH_PSYCHOLOGY_BRIEF.md` + subject A/B/C wired in Email Preview UI | **0 mailtos sent**; operator must pick variant per prospect and send | **B** |

### button-flow-qa — session_state audit

**Direct `business_tab` violations (production code):** **NONE** — all tab jumps use `request_business_tab()` / `apply_pending_business_tab()` before `st.radio(key="business_tab")`.

**Direct `app_section` violations (production code):** **NONE** — `request_app_section()` / `apply_pending_app_section()` used in `scripts/app.py`.

| # | Mandatory flow | Status | Evidence |
|---|----------------|--------|----------|
| 1 | Open Revenue Pipeline | **PASS** | `app.py` → `request_app_section(Business)` + `request_business_tab(Pipeline)` + `force_ops_console` |
| 2 | Scheduling \| Business nav | **PASS** | `apply_pending_app_section` before `st.radio(key="app_section")` |
| 3 | Gather prospects | **PASS** | `_run_auto_gather` → toast + `request_business_tab(Prospects)` |
| 4 | Preview email | **PASS** | `request_business_tab(Email Preview)` + prospect id in session |
| 5 | Proceed with client | **PASS** | Confirm box + `request_business_tab(Client Onboarding)` |
| 6 | Pass | **PASS** | `_pass_prospect` + toast |
| 7 | Back to manager workspace | **PASS** | `request_app_section(Scheduling)` + clears `force_ops_console` + rerun |

### Brand / persuasion audit

| Surface | Status | Notes |
|---------|--------|-------|
| `email_templates.py` | **PASS** | Variant A subject default; managed-first body; Port Optical sign-off; no hype/urgency |
| `helpers.py` templates | **PASS** | `DEFAULT_EMAIL_*` aligned; honesty blocklist active in preview |
| `deploy/landing.html` | **PASS** | Managed-first CTA; compliance-check language; no RSI acronym in hero |
| `FIRST_TOUCH_PSYCHOLOGY_BRIEF.md` | **PASS** | A/B/C hypotheses documented; friction checklist; reply path for customer-relations |
| Email Preview UI | **PASS** | Subject variant picker + Apply button; mobile length warning; Reply-To caption; honesty scan |

### Prune check — P0 from `BUSINESS_CODEBASE_AUDIT`

| P0 item | Status |
|---------|--------|
| `scripts/archive/` removed | **DONE** (confirmed absent) |
| Demo credentials env-gated / not in prod | **DONE** — `LAB_ALLOW_DEMO_ACCOUNTS` + `LAB_SCHEDULER_ENV=production` |
| `auto_generate.py` agent debug log block | **DONE** — removed after RSI PASS |
| pytest import path (`scripts`, `tests` packages) | **DONE** — `pythonpath = ["src", "."]` in `pyproject.toml` |
| Modular stubs unused (`pipeline.py`, `prospects.py`, `email_preview.py`) | **DEFER** — wire into `section.py` after live smoke confirms flows |
| Production host + persistent DB | **OPEN** — human deploy action |
| Custom domain + HTTPS + `APP_BASE_URL` | **OPEN** — human deploy action |

### Fixes shipped (Iteration 4)

| Item | Why | Files |
|------|-----|-------|
| Subject A/B/C picker in Email Preview | persuasion-psychology-partner: operator can apply psych-brief variants without editing subject by hand | `src/lab_scheduler/ui/business/helpers.py`, `src/lab_scheduler/ui/business/section.py`, `tests/test_business_ui.py` |
| Port Optical default sender | brand-voice-partner: consistent sign-off across generated drafts and preview service | `src/lab_scheduler/business/email_templates.py`, `src/lab_scheduler/business/prospect_service.py` |
| Psychology brief checklist update | Mark subject variant UI as shipped; operator send still human-only | `docs/FIRST_TOUCH_PSYCHOLOGY_BRIEF.md` |

### Unanimous verdict — 100% production-ready?

**NO** — confidence **80%** toward product ship (+1% vs Iteration 3), **30%** toward revenue-ready (unchanged).

| Agent | Would veto? | Reason |
|-------|-------------|--------|
| revenue-growth | **YES** | No outbound, no pipeline proof on live URL |
| manager-value-qa | **YES** | No client publish bundle on hosted smoke URL |
| scheduling-rules-coordinator | **NO** | RSI PASS; rotation claims cleared |
| ui-design-partner | **NO** | Business UX shippable; subject variant picker improves operator send path |
| goal-coordinator | **YES** | Human deploy + first $ not closed |
| production-runtime-partner | **YES** | No public host |
| button-flow-qa | **NO** | Tab/section pending patterns clean; 7/7 flows pass |
| customer-relations | **YES** | No live intake thread |
| subagent-roster-advisor | **NO** | Roster adequate; psych brief + UI bridge outbound prep |
| brand-voice-partner | **YES** | Copy aligned; operator must review and send first 5 mailtos |
| persuasion-psychology-partner | **YES** | A/B/C UI wired; 0 mailtos sent; operator must execute batch 1 |

**Agents that would sign off today (4/11):** scheduling-rules-coordinator, ui-design-partner, button-flow-qa, subagent-roster-advisor. *Sign-off count unchanged; product outbound prep improved, revenue blockers still human-only (deploy, mailtos, publish bundle).*

### Next tick priorities (ranked)

1. **production-runtime-partner** — deploy `scripts/app.py` (Streamlit Cloud/Railway + persistent `LAB_SCHEDULER_DB_PATH`, `LAB_SCHEDULER_ENV=production`, no demo accounts)
2. **revenue-growth + human operator** — Gather 5 Manitoba prospects → pick subject A/B/C in Email Preview → send 5 mailtos with Reply-To wired
3. **manager-value-qa** — attach RSI PASS + breakroom HTML to deploy smoke URL before calling ship
4. **ui-design-partner** — wire modular `email_preview.py` into `section.py` after live smoke confirms flows

---

*Iteration 4 logged by goal-coordinator.*

---

## Iteration 5 — 2026-06-19

**Orchestrator:** goal-coordinator (all 11 subagents accountable)  
**North star:** $2,000 CAD/month MRR  
**Team confidence:** **8 / 10** (product) · **3 / 10** (revenue execution)

### Verification

| Check | Result |
|-------|--------|
| `pytest -q` (default suite) | **565 passed**, 192 deselected, **0 warnings** — ~305s |
| `python scripts/rotation_rsi_gate.py` | **PASS** — 0 operational tally violations, 0 rotation invariant violations |
| Business tests (`test_business_ui`, `test_business_inbound`, `test_business_prospects`) | **37 passed** (unchanged vs Iteration 4) |

### Accountability scorecard (11 agents)

| Agent | Last contribution | Current gap | Grade |
|-------|-------------------|-------------|-------|
| **revenue-growth** | `REVENUE_2000_PLAN.md`, managed-first mix, sidebar 3-step path | **0 outbound emails sent**; pipeline unproven on live URL | **B** |
| **manager-value-qa** | 565/565 pytest green, RSI PASS | No live pilot publish bundle on hosted smoke URL | **A** |
| **scheduling-rules-coordinator** | RSI PASS; compliance-check copy on landing/outbound | Pitch surfaces must stay evidence-backed on live sends | **A-** |
| **ui-design-partner** | Business shell (5 tabs), envelope preview, subject A/B/C picker in Email Preview | `section.py` still monolithic; modular `email_preview.py` stale vs canonical tab | **B+** |
| **goal-coordinator** | Iteration 5 orchestration + scorecard discipline | Cannot close deploy/outbound without human operator | **B+** |
| **production-runtime-partner** | Demo creds env-gated; `DEPLOY.md` checklist + inbound env table | **No public URL** — P0 revenue blocker | **C+** |
| **button-flow-qa** | `business_tab_pending` + `app_section_pending` patterns verified; 7 flows PASS | No live Streamlit smoke this iteration (code trace + unit tests only) | **A-** |
| **customer-relations** | Inbox tab wired; onboarding checklist in Business | **No client thread processed**; no intake brief artifact | **C** |
| **subagent-roster-advisor** | Roster at 11 agents; boundaries documented | Soft cap exceeded; brand/persuasion overlap risk | **B** |
| **brand-voice-partner** | Port Optical default sign-off; managed-first templates; landing RSI slip fixed | **No operator-reviewed live sends** | **B-** |
| **persuasion-psychology-partner** | `FIRST_TOUCH_PSYCHOLOGY_BRIEF.md` + subject A/B/C wired in Email Preview UI | **0 mailtos sent**; operator must pick variant per prospect and send | **B** |

### button-flow-qa — session_state audit

**Direct `business_tab` violations (production code):** **NONE** — all tab jumps use `request_business_tab()` / `apply_pending_business_tab()` before `st.radio(key="business_tab")`.

**Direct `app_section` violations (production code):** **NONE** — `request_app_section()` / `apply_pending_app_section()` used in `scripts/app.py`.

| # | Mandatory flow | Status | Evidence |
|---|----------------|--------|----------|
| 1 | Open Revenue Pipeline | **PASS** | `app.py` → `request_app_section(Business)` + `request_business_tab(Pipeline)` + `force_ops_console` |
| 2 | Scheduling \| Business nav | **PASS** | `apply_pending_app_section` before `st.radio(key="app_section")` |
| 3 | Gather prospects | **PASS** | `_run_auto_gather` → toast + `request_business_tab(Prospects)` |
| 4 | Preview email | **PASS** | `request_business_tab(Email Preview)` + `business_prospect_id` in session |
| 5 | Proceed with client | **PASS** | Confirm box + `request_business_tab(Client Onboarding)` |
| 6 | Pass | **PASS** | `_pass_prospect` + toast |
| 7 | Back to manager workspace | **PASS** | `request_app_section(Scheduling)` + clears `force_ops_console` + rerun |

### Brand / persuasion audit

| Surface | Status | Notes |
|---------|--------|-------|
| `email_templates.py` | **PASS** | Variant A subject default; managed-first body; Port Optical sign-off; no hype/urgency |
| `helpers.py` templates | **PASS** | `DEFAULT_EMAIL_*` aligned; `first_touch_subject` A/B/C; honesty blocklist active |
| `deploy/landing.html` | **PASS** (fixed) | Managed card had stray "RSI gate" — corrected to "compliance check"; hero already customer-facing |
| `FIRST_TOUCH_PSYCHOLOGY_BRIEF.md` | **PASS** | A/B/C hypotheses documented; friction checklist; reply path for customer-relations |
| Email Preview UI (`section.py`) | **PASS** | Subject variant picker + Apply; Reply-To in mailto; mobile length warning; Save draft; honesty scan |

### Modular `email_preview.py` wiring assessment

**DEFER — not trivial or safe this iteration.**

Canonical Email Preview lives in `section.py` (`_render_email_preview_tab`) with Iteration 4 features absent from the modular stub:

- Subject A/B/C picker + `first_touch_subject` apply
- Reply-To wired in `mailto_link` + `OUTBOUND_REPLY_TO_NOTES` caption
- Per-prospect session keys (`draft_subject_{id}`, `draft_body_{id}`)
- Save draft + `st.link_button` mailto pattern

Wiring the stale `email_preview.py` module would regress outbound prep. Revisit after live deploy smoke confirms flows.

### production-runtime-partner — DEPLOY.md gaps

| Gap | Status |
|-----|--------|
| No public host / `APP_BASE_URL` | **OPEN** — human deploy |
| Persistent `LAB_SCHEDULER_DB_PATH` | **OPEN** — human deploy |
| Stripe live keys + webhook service | **OPEN** — human deploy |
| Inbound IMAP env vars in reference table | **DONE** — added `LAB_INBOUND_*` + `LAB_INBOUND_REPLY_TO` to env table |
| Postgres cutover | **DEFER** — SQLite on persistent volume OK for early pilots per DEPLOY.md |
| Smoke checklist (Distribute→Fill→Save, Business mailto) | **DOCUMENTED** — operator-only |

### Prune check — P0 from `BUSINESS_CODEBASE_AUDIT`

| P0 item | Status |
|---------|--------|
| `scripts/archive/` removed | **DONE** (confirmed absent) |
| Demo credentials env-gated / not in prod | **DONE** — `LAB_ALLOW_DEMO_ACCOUNTS` + `LAB_SCHEDULER_ENV=production` |
| `auto_generate.py` agent debug log block | **DONE** — removed after RSI PASS |
| pytest import path (`scripts`, `tests` packages) | **DONE** — `pythonpath = ["src", "."]` in `pyproject.toml` |
| Modular stubs unused (`pipeline.py`, `prospects.py`, `email_preview.py`) | **DEFER** — `email_preview.py` stale; wire after live smoke |
| Production host + persistent DB | **OPEN** — human deploy action |
| Custom domain + HTTPS + `APP_BASE_URL` | **OPEN** — human deploy action |

### Fixes shipped (Iteration 5)

| Item | Why | Files |
|------|-----|-------|
| Landing managed-card RSI → compliance check | brand-voice-partner: customer-facing landing still had internal "RSI gate" acronym in Managed card (missed in Iteration 2 hero pass) | `deploy/landing.html` |
| Inbound IMAP env reference in DEPLOY | production-runtime-partner: operator checklist referenced IMAP secrets but env table omitted them | `deploy/DEPLOY.md` |

### Unanimous verdict — 100% production-ready?

**NO** — confidence **80%** toward product ship (unchanged vs Iteration 4), **30%** toward revenue-ready (unchanged).

| Agent | Would veto? | Reason |
|-------|-------------|--------|
| revenue-growth | **YES** | No outbound, no pipeline proof on live URL |
| manager-value-qa | **YES** | No client publish bundle on hosted smoke URL |
| scheduling-rules-coordinator | **NO** | RSI PASS; rotation claims cleared |
| ui-design-partner | **NO** | Business UX shippable; subject variant picker improves operator send path |
| goal-coordinator | **YES** | Human deploy + first $ not closed |
| production-runtime-partner | **YES** | No public host |
| button-flow-qa | **NO** | Tab/section pending patterns clean; 7/7 flows pass |
| customer-relations | **YES** | No live intake thread |
| subagent-roster-advisor | **NO** | Roster adequate; psych brief + UI bridge outbound prep |
| brand-voice-partner | **YES** | Copy aligned; operator must review and send first 5 mailtos |
| persuasion-psychology-partner | **YES** | A/B/C UI wired; 0 mailtos sent; operator must execute batch 1 |

**Agents that would sign off today (4/11):** scheduling-rules-coordinator, ui-design-partner, button-flow-qa, subagent-roster-advisor. *Sign-off count unchanged; product hygiene improved (landing copy, DEPLOY inbound docs), revenue blockers still human-only (deploy, mailtos, publish bundle).*

### Next tick priorities (ranked)

1. **production-runtime-partner** — deploy `scripts/app.py` (Streamlit Cloud/Railway + persistent `LAB_SCHEDULER_DB_PATH`, `LAB_SCHEDULER_ENV=production`, no demo accounts, `LAB_INBOUND_*` for Inbox)
2. **revenue-growth + human operator** — Gather 5 Manitoba prospects → pick subject A/B/C in Email Preview → send 5 mailtos with Reply-To wired
3. **manager-value-qa** — attach RSI PASS + breakroom HTML to deploy smoke URL before calling ship
4. **ui-design-partner** — sync or retire modular `email_preview.py` after live smoke confirms flows

### Iteration 6 warranted?

**YES** — human blockers (deploy, first 5 mailtos, publish bundle) remain; product verification green but revenue execution at 3/10. Next iteration should focus on **post-deploy smoke evidence** and **outbound batch 1 execution proof**, not further product polish unless deploy smoke fails.

---

*Iteration 5 logged by goal-coordinator.*
