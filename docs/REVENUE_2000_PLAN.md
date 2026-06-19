# Revenue Plan — $2,000 CAD/month North Star

**Date:** 2026-06-19  
**Workspace:** `lab_staffing_scheduler`  
**Prime goal:** **$2,000 CAD/month recurring revenue (MRR)** within 90 days of deploy  
**Plan owner:** goal-coordinator (synthesized from all subagents)  
**Full plan:** this document — subagents execute weekly tasks below; human only where marked.

---

## Executive summary

**Verdict: Yes — $2,000 CAD/month is achievable, but not via pure self-serve SaaS on day one.**

The product has a **real, RSI-gated scheduling engine** (Distribute → Fill → Save, footer 2/2 E/N, weekday D for DE/DN, union rules) and a **working Business pipeline** (prospect discovery, email preview, proceed-to-tenant). What is missing is **production deploy**, **live billing**, **sales proof**, and **outbound execution** — not core scheduling capability.

**Fastest path to $2,000/mo:** **Managed scheduling first** (you operate `manager_app.py`, deliver breakroom HTML + RSI PASS bundle), with **Pro SaaS ($299/mo)** as upsell after one published client. Pure SaaS needs ~7 Pro seats with no churn — realistic only after managed proof and hosted trial.

**Team confidence (90-day deploy → $2,000 MRR): 6.5 / 10** — see §9. Raises to **7.5+** after Week 2 deploy + 10 qualified outbound touches.

**Product state (verified 2026-06-19):**

| Check | Result |
|-------|--------|
| `pytest tests/test_business_prospects.py -q` | **9 passed** |
| `python scripts/rotation_rsi_gate.py` | **PASS** — 0 operational tally violations, 0 rotation invariant violations |
| `pytest -q` (default suite) | **541 passed**, 1 failed (`test_alternate_shifts_respects_hours_weighted_evening_targets`), 192 deselected |
| Business UI | Implemented — Pipeline, Prospects, Email Preview, Client Onboarding in `src/lab_scheduler/ui/business/` |
| Production host | **Not live** — `deploy/DEPLOY.md` documents path only |

---

## 1. Honest revenue assessment

### Can this product hit $2,000/mo?

| Factor | Assessment |
|--------|------------|
| **Technical sellability** | **Strong** — RSI gate, breakroom export, Portage rotation logic are pitchable with evidence |
| **Market size** | **Narrow but sufficient** — Manitoba hospital labs (~8 in `regional_facilities.csv`; expand Prairies for pipeline depth) |
| **Buyer urgency** | **Moderate** — MLT hiring, breakroom posting, union equity are real pains; sales cycle 2–8 weeks |
| **Self-serve readiness** | **Partial** — signup, billing scaffold, manager UI exist; hosting + live Stripe + UX polish incomplete |
| **Operator capacity** | **Binding constraint** — managed blocks need ~3–6 operator hours each until templated |

**Conclusion:** $2,000/mo is a **credible 60–90 day target** with a **hybrid mix**, not a fantasy. Failure mode is **zero outbound**, not product incapability.

### Revenue mix — math to $2,000

All figures **CAD**.

| Path | Mix | Monthly math | Time to first $ | Leverage |
|------|-----|--------------|-----------------|----------|
| **A — Managed-heavy (recommended)** | 2× managed block @ $800 + 2× Pro @ $299 | **$2,198** | 3–6 weeks | Low — operator hours scale linearly |
| **B — Retainer blend** | 2× retainer @ $600 + 2× Pro @ $299 + 1× block @ $200 top-up | **$2,198** | 4–8 weeks | Medium — recurring base |
| **C — Pure SaaS** | 7× Pro @ $299 | **$2,093** | 8–16 weeks | High — needs hosted trial + proof |
| **D — Whale + SaaS** | 1× managed takeover @ $1,200 + 3× Pro @ $299 | **$2,097** | 6–10 weeks | Low — SLA risk |
| **E — Minimum viable** | 1× block @ $1,200 + 2× retainer @ $400 | **$2,000** | 4–6 weeks | Low — no SaaS yet |

**Recommended target mix (Month 2–3 steady state):**

```
MRR target breakdown:
├── Managed retainer:  2 clients × $500/mo  = $1,000
├── Pro SaaS:          2 seats × $299/mo    =   $598
├── Block top-up:      1 × $400 (amortized) =   $400  (or quarterly $1,200 block)
└── Total                                  ≈ $1,998 → round with annual prepay bonus
```

**Fastest route to first dollar:** Path **A** or **E** — invoice **$800–1,200** for first 8-week breakroom publish before any Stripe self-serve is perfect.

### Pricing anchors (use in pitches)

| Offer | Price (CAD) | Deliverable |
|-------|-------------|-------------|
| **Managed schedule block** | $800–1,200 per 8-week publish | Roster import, Distribute/Fill/Save, RSI PASS log, breakroom HTML |
| **Monthly retainer** | $400–600/mo | One publish cycle + equity review + sick-call support ≤2h/mo |
| **Pro SaaS** | $299/mo | Self-serve manager workspace, breakroom export, compliance audit |
| **Trial** | Free 14 days | Demo roster only — lead-gen, not revenue until conversion |

---

## 2. Gap analysis — today vs deploy-ready for revenue

| Area | Today | Deploy-ready | Owner | Priority |
|------|-------|--------------|-------|----------|
| **Scheduling engine** | RSI PASS; E blocks, weekday D, footer 2/2 | Same + fix 1 pytest failure | manager-value-qa | P1 |
| **Manager UX** | `manager_app.py` works; health panel | Spinner on Fill, breakroom screenshot asset | ui-design-partner | P1 |
| **Business / GTM** | Full Business section + prospect CRUD (9 tests) | Weekly gather + 5 previews/week | revenue-growth | P0 |
| **Auth** | Signup + env-gated demos | `LAB_SCHEDULER_ENV=production`, no demo accounts on public URL | production-runtime-partner | P0 |
| **Hosting** | Local only | Streamlit Cloud or Railway + persistent SQLite volume | production-runtime-partner | P0 |
| **Payments** | Mock Stripe default | Live Stripe + webhook service + `USE_MOCK_STRIPE=0` | production-runtime-partner | P1 (Week 2) |
| **Landing** | `deploy/landing.html` exists; **Auto-Pilot copy misaligned** | Managed-first headline; CTAs → hosted `/?signup=1` | ui-design-partner + revenue-growth | P0 |
| **Proof bundle** | RSI stdout only | Screenshot + anonymized breakroom HTML + 1-pager | revenue-growth | P0 |
| **Outreach** | 8 MB facilities in CSV; pipeline empty until gather | 10+ Previewed prospects, 3+ contacted | revenue-growth | P0 |
| **Postgres** | Schema + migration script | Defer — SQLite on persistent volume OK for first 3 clients | production-runtime-partner | P2 |
| **Employee portal** | None | Defer — not required for $2k | — | P3 |
| **CI / release** | Manual pytest | Green default suite on every release | manager-value-qa | P1 |

### Critical path (blocks revenue if missing)

1. **Public URL** with `manager_app.py` or `app.py` (operator Business + manager path)
2. **Demo credential lockdown** on production
3. **One proof package** (RSI PASS + breakroom export screenshot)
4. **10 outbound touches** to Manitoba lab managers
5. **One signed managed engagement** ($800+)

---

## 3. Subagent task matrix — weekly until $2,000/mo

### Week-by-week rhythm (all agents)

| Day | Cross-team focus |
|-----|------------------|
| **Mon** | goal-coordinator scorecard; revenue-growth prospect gather |
| **Tue–Wed** | production-runtime deploy fixes; ui-design polish; scheduling-rules clearance on pitch claims |
| **Thu** | revenue-growth follow-ups; manager-value-qa release gate |
| **Fri** | goal-coordinator retro; adjust next week targets |

---

### revenue-growth

| Week | Tasks | Done when |
|------|-------|-----------|
| **1** | Run **Gather prospects** in Business → Prospects; score top 5 MB facilities (St. Boniface, Portage, Selkirk) | ≥5 prospects in pipeline, ICP scored |
| **1** | Draft **managed-first** email for #1 target using Email Preview; honesty-linter clean | Subject + body copied; scheduling-rules clearance on claims |
| **2** | Attach **proof bundle** to pitch: RSI PASS excerpt + breakroom screenshot | PDF or email attachment ready |
| **2** | Update `deploy/landing.html` copy: lead with Distribute/Fill/Save, not Auto-Pilot | scheduling-rules-coordinator clears claims |
| **3** | Send 5 first-touch emails; log in prospect notes | 5× status → Previewed + outreach timestamp |
| **3** | Follow-up sequence for non-responders (rotation compliance angle) | 2nd touch templates in Email Preview |
| **4** | Close **first managed block** — proposal $800–1,200, scope: one 8-week publish | Verbal/written yes + roster collection started |
| **5–8** | Second prospect parallel; upsell Pro to first client after successful publish | 2nd pipeline → Active Client or Pro trial |
| **9–12** | Expand to 2 retainers + 2 Pro; Prairies facilities beyond MB | MRR ≥ $2,000 on scorecard |

**Key files:** `data/rsi/regional_facilities.csv`, `src/lab_scheduler/business/`, `deploy/landing.html`, Business → Email Preview

---

### manager-value-qa

| Week | Tasks | Done when |
|------|-------|-----------|
| **1** | Fix or document `test_alternate_shifts_respects_hours_weighted_evening_targets` | Default suite green OR explicit legacy mark + note |
| **1** | Release gate doc: RSI PASS + pytest summary pasted to `docs/RELEASE_EVIDENCE.md` | SHIP checklist complete |
| **2** | Manager smoke: Distribute → Fill → Save on demo roster; capture health panel | Screenshot for revenue-growth |
| **2** | Breakroom export audit on publish week — footer 2/2 | `audit_breakroom.py` or RSI gate PASS |
| **3** | Client #1 publish QA — run RSI gate on **client roster** before delivery | PASS log attached to invoice |
| **4+** | Weekly RSI gate before any client deliverable | Zero publish without PASS |
| **Ongoing** | Block release if default pytest regresses | HOLD verdict to goal-coordinator |

**Key commands:**

```powershell
cd lab_staffing_scheduler
$env:PYTHONPATH="src;."
python scripts/rotation_rsi_gate.py
pytest -q --tb=no
pytest tests/test_rotation_invariants.py tests/test_preference_fill.py tests/test_business_prospects.py -q
```

---

### scheduling-rules-coordinator

| Week | Tasks | Done when |
|------|-------|-----------|
| **1** | **Rule clearance** on landing.html + default email template claims | Written clearance or revised copy |
| **1** | Audit pitch proof paragraph — only RSI gate, demo roster, trial; no fake customers | revenue-growth templates updated |
| **2** | Clearance on any client-specific roster claims before outbound | Per-prospect note in pipeline |
| **3** | Pre-publish review for client #1 — footer, weekend D, E blocks | Rule clearance before invoice |
| **4+** | Weekly sync: any manager-value-qa rotation change → re-clear pitches | Cross-agent sync note |
| **Ongoing** | Block pitches promising Auto-Pilot one-click or HIPAA | Violation flagged to goal-coordinator |

**Locked claims safe to sell:**

- Footer **2/2 E and 2/2 N** daily (1 MLT + 1 MLA per band)
- **8-week** master catalog, Portage-style D/E rotation
- **RSI gate** — automated compliance check before publish
- **Breakroom HTML** export

**Do not claim:** Auto-Pilot as primary, HIPAA certification, named hospital customers without permission.

---

### ui-design-partner

| Week | Tasks | Done when |
|------|-------|-----------|
| **1** | Business section audit vs `docs/BUSINESS_SECTION_DESIGN.md` — fix top P0 (Preview CTA, empty states) | Pre-ship checklist PASS on Prospects + Email Preview |
| **1** | Manager app: health panel hierarchy — violations scannable in 5s | manager-value-qa smoke OK |
| **2** | Landing alignment with Business theme tokens; managed-first hero | Matches design spec palette |
| **2** | Email Preview: sticky action bar, honesty-linter visible | Review → Preview → Proceed flow smooth |
| **3** | Client onboarding checklist UX — progress bar, deep links to manager workspace | 8-task checklist usable |
| **4+** | Polish only — no grid behavior changes without QA | Minimal diffs |

**Key files:** `src/lab_scheduler/ui/business/`, `scripts/manager_app.py`, `deploy/landing.html`

---

### goal-coordinator

| Week | Tasks | Done when |
|------|-------|-----------|
| **1** | Publish **Week 1 scorecard** (template §7) | MRR, pipeline, blockers documented |
| **1** | Audit deploy checklist — score production-runtime Partial vs Complete | No false green on "live" |
| **2** | Scorecard + delegation fixes for any Partial agent deliverables | Handoff blocks issued |
| **3** | **First revenue event** tracked — block invoiced or Stripe subscription | MRR > $0 |
| **4** | 90-day trajectory check — on track for $2,000? | Correct course memo |
| **Weekly** | Friday retro: ON TRACK / AT RISK / OFF TRACK | User sees one-page status |

**North star:** $2,000 CAD MRR — judge all agent "done" against revenue impact, not task completion alone.

---

### production-runtime-partner

| Week | Tasks | Done when |
|------|-------|-----------|
| **1** | Deploy **operator app** (`streamlit run scripts/app.py`) to Streamlit Cloud or Railway | Public HTTPS URL |
| **1** | Env: `LAB_SCHEDULER_ENV=production`, `LAB_ALLOW_DEMO_ACCOUNTS` unset, persistent `LAB_SCHEDULER_DB_PATH` | Security checklist PASS |
| **1** | Deploy **manager entry** — `streamlit run scripts/manager_app.py` (separate service or path) | Manager URL documented |
| **2** | `APP_BASE_URL` + landing static host or proxy | Trial CTA resolves |
| **2** | Stripe live keys + `uvicorn scripts.stripe_webhook:app`; `USE_MOCK_STRIPE=0` | Test checkout completes |
| **2** | Cache roster load (`@st.cache_data`); spinner on Fill | Fill flow not silent |
| **3** | Backup SQLite weekly; error surfaces user-friendly | No stack traces in prod |
| **4+** | Monitor cold start; Postgres migration only if >3 tenants | Defer until needed |

**Key files:** `deploy/DEPLOY.md`, `scripts/app.py`, `scripts/manager_app.py`, `scripts/stripe_webhook.py`, `src/lab_scheduler/auth/session.py`

---

## 4. Phased roadmap

### Phase 0 — Foundation (Days 1–3)

| Task | Files / commands | Agent |
|------|------------------|-------|
| Green default pytest or mark failure legacy | `tests/test_preference_fill.py` | manager-value-qa |
| RSI gate evidence file | `docs/RELEASE_EVIDENCE.md` | manager-value-qa |
| Rule clearance on landing copy | `deploy/landing.html` | scheduling-rules-coordinator |
| Gather MB prospects | Business → Prospects | revenue-growth |

### Phase 1 — Deploy (Week 1–2)

| Task | Detail |
|------|--------|
| Streamlit Cloud project | Repo connected; secrets: `LAB_SCHEDULER_DB_PATH`, `LAB_SCHEDULER_ENV=production` |
| Persistent volume | SQLite at `/mount/data/demo.sqlite3` or Railway volume |
| Manager app live | Second Streamlit app or subdomain `manager.` |
| Landing live | `deploy/landing.html` on marketing domain; CTAs → `APP_BASE_URL/?signup=1` |
| Demo removal | Confirm no `LAB_ALLOW_DEMO_ACCOUNTS` in prod secrets |
| Proof asset | Breakroom HTML + RSI PASS screenshot in `docs/sales/` |
| Stripe test → live | Create $299 CAD/mo price; webhook endpoint registered |

**Week 2 exit criteria:** Public URLs work; operator can log in; Business gather returns prospects; one complete email preview exported.

### Phase 2 — First client (Week 3–4)

| Task | Detail |
|------|--------|
| Outbound | 5–10 emails to MB hospital lab managers |
| Discovery call | 15-min screen share — `manager_app` Distribute/Fill/Save |
| Roster import | Client CSV via `import_manager` |
| Publish cycle | Operator runs fill; RSI gate on client data; deliver HTML |
| Invoice | **$800–1,200** managed block (e-transfer / invoice — Stripe optional) |
| Testimonial ask | Permission for anonymized quote |

**Week 4 exit criteria:** **MRR ≥ $400** (block amortized or retainer signed) + 1 breakroom published.

### Phase 3 — Scale to $2,000 (Month 2)

| Task | Detail |
|------|--------|
| Client #2 | Second managed block or retainer |
| Pro upsell | Offer $299/mo self-serve to client #1 after successful publish |
| Pipeline refill | Weekly gather; 3 Previewed minimum at all times |
| Template hours | Document publish runbook — target **<4 operator hours** per block |
| Landing proof | Add honest "compliance-checked publish" section with RSI screenshot |

**Month 2 exit criteria:** **MRR ≥ $2,000** OR clear line-of-sight (signed retainers + Pro pending).

### Concrete feature tasks (by file)

| Priority | File / area | Task |
|----------|-------------|------|
| P0 | `deploy/landing.html` | Replace Auto-Pilot lead with managed/Distribute-Fill-Save |
| P0 | `deploy/DEPLOY.md` | Add Streamlit Cloud step-by-step with screenshot placeholders |
| P0 | `scripts/manager_app.py` | Verify production env branch hides dev tools |
| P0 | `src/lab_scheduler/ui/business/section.py` | Confirm Proceed → tenant + onboarding |
| P1 | `src/lab_scheduler/billing/stripe_checkout.py` | Live mode smoke test |
| P1 | `scripts/stripe_webhook.py` | Deploy on Railway/Fly |
| P1 | `tests/test_preference_fill.py` | Fix hours-weighted evening target test |
| P2 | `scripts/app.py` | Cache tenant roster load |
| P2 | `docs/sales/proof_bundle.md` | Create sales one-pager |

---

## 5. Deploy checklist — exact go-live steps

### Pre-flight (local)

```powershell
cd c:\Users\Danie\OneDrive\Pictures\Documents\lab_staffing_scheduler
$env:PYTHONPATH="src;."
pytest tests/test_business_prospects.py -q
python scripts/rotation_rsi_gate.py
pytest -q --tb=no
# Target: 0 failed (fix or mark legacy first)
```

### Streamlit Cloud — operator console (`app.py`)

1. Push repo to GitHub (if not already).
2. [share.streamlit.io](https://share.streamlit.io) → New app → `scripts/app.py`.
3. **Secrets** (TOML):

```toml
LAB_SCHEDULER_ENV = "production"
LAB_SCHEDULER_DB_PATH = "/mount/data/lab_scheduler.sqlite3"
# Do NOT set LAB_ALLOW_DEMO_ACCOUNTS
APP_BASE_URL = "https://YOUR-APP.streamlit.app"
```

4. **Advanced settings → Mount persistent storage** at `/mount/data`.
5. Deploy; verify login + Business → Gather prospects works.

### Streamlit Cloud — manager app (`manager_app.py`)

1. Second app → `scripts/manager_app.py`.
2. Same secrets + volume (or shared DB path).
3. Document URL as **manager workspace** for clients.

### Landing page

1. Host `deploy/landing.html` on:
   - **Option A:** GitHub Pages / Netlify on marketing domain, **or**
   - **Option B:** Reverse-proxy root to static HTML, `/app` to Streamlit.
2. Update CTAs:
   - `Start free 14-day trial` → `{APP_BASE_URL}/?signup=1`
   - `Sign in` → `{APP_BASE_URL}/`
3. Set `APP_BASE_URL` in Streamlit secrets to match.

### Stripe live

1. Stripe Dashboard → Product **Pro** → Price **$299 CAD/month** recurring → copy `price_…`.
2. Secrets:

```toml
STRIPE_SECRET_KEY = "sk_live_…"
STRIPE_PRICE_ID = "price_…"
USE_MOCK_STRIPE = "0"
```

3. Deploy webhook service (Railway example):

```bash
pip install -e ".[billing]"
export LAB_SCHEDULER_DB_PATH=/data/lab_scheduler.sqlite3
export STRIPE_SECRET_KEY=sk_live_…
export STRIPE_WEBHOOK_SECRET=whsec_…
uvicorn scripts.stripe_webhook:app --host 0.0.0.0 --port 8080
```

4. Register `https://YOUR-API/stripe/webhook` for `checkout.session.completed`.
5. Test: signup → checkout → subscription row in DB.

### Demo removal verification

- [ ] `LAB_SCHEDULER_ENV=production` set
- [ ] `LAB_ALLOW_DEMO_ACCOUNTS` **not** set
- [ ] No plaintext passwords in public README/deploy docs
- [ ] `scripts/app.py` does not auto-seed demo users in production

### business_app (optional split)

- Defer unless `app.py` monolith causes deploy issues; Business already lives in `app.py` operator shell.

### Post-deploy smoke

1. Signup new tenant → login → manager path.
2. Business → Gather → Preview email → Proceed (test tenant).
3. Distribute → Fill → Save on demo roster.
4. Export breakroom HTML.
5. `python scripts/rotation_rsi_gate.py` locally on reference roster.

---

## 6. Human-only actions (minimized)

Agents handle research, copy, code, QA, and deploy wiring. **Human required only for:**

| Action | Why human | When |
|--------|-----------|------|
| **Stripe account verification** | Identity/business verification | Week 1–2 |
| **Domain purchase / DNS** | Billing credentials | Week 1 |
| **Send email / LinkedIn message** | Authentic sender identity; CAN-SPAM consent | Week 2+ |
| **Discovery call / screen share** | Trust-building with lab manager | Week 3 |
| **Sign contract / SOW** | Legal authority | Week 3–4 |
| **Collect client roster CSV** | Client HR data — PHI boundaries | Week 3 |
| **Invoice / payment collection** | Bank account, e-transfer, tax | Week 4 |
| **Cold call (optional)** | Voice — only if email fails | Week 4+ |

**Not human-required (agents do this):** prospect scoring, email drafts, RSI gate, deploy config drafts, pytest fixes, landing copy, onboarding checklist, weekly scorecard.

---

## 7. Success metrics

### Primary — MRR tracking

| Metric | Week 4 target | Day 90 target |
|--------|---------------|---------------|
| **MRR (CAD)** | $400–800 | **≥ $2,000** |
| **Managed blocks sold (cumulative)** | 1 | 2–3 |
| **Pro subscribers** | 0–1 | 2–4 |
| **Retainer clients** | 0–1 | 2 |

**Formula:** `MRR = (Pro seats × 299) + Σ retainers + (blocks ÷ amortization months)`

Track in Business → Pipeline metric tiles + weekly scorecard (`goal-coordinator`).

### Pipeline metrics

| Metric | Target |
|--------|--------|
| Prospects gathered | ≥ 8 (all MB facilities) |
| Previewed | ≥ 5 by Week 3 |
| Contacted (email sent) | ≥ 10 by Week 4 |
| Active clients | ≥ 1 by Week 4 |
| Conversion Previewed → Active | ≥ 10% |

### Delivery metrics

| Metric | Target |
|--------|--------|
| Operator hours per managed block | ≤ 6 (Week 4); ≤ 4 (Month 2) |
| RSI gate PASS before publish | 100% |
| Client support hours / month | ≤ 2h per retainer client |
| pytest default suite | 0 failures |

### Weekly scorecard template (goal-coordinator)

```markdown
## Week N scorecard — $2,000 MRR goal

**MRR:** $___ / $2,000  
**Pipeline:** __ New · __ Previewed · __ Active · __ Passed  
**Outbound:** __ emails sent · __ calls · __ meetings  
**Deliverables:** __ blocks published · RSI PASS: Y/N  
**Hours (operator):** __h on client work  
**Verdict:** ON TRACK | AT RISK | OFF TRACK  
**Top blocker:** ___  
**Next week #1 priority:** ___
```

---

## 8. Risk & mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| **No outbound** | Fatal — $0 forever | High if deprioritized | revenue-growth Week 1 gather + 5 emails Week 2; scorecard accountability |
| **Sales cycle > 90 days** | Miss $2k target | Medium | Managed block ($800) faster than SaaS; offer free compliance audit on one week |
| **Operator bottleneck** | Can't scale past 2–3 clients | High | Document runbook; cap managed clients at 3 until <4h/block |
| **Small MB market** | Pipeline exhausts | Medium | Expand `regional_facilities.csv` to SK/AB Prairies; 6 facilities ready |
| **Landing overpromises Auto-Pilot** | Trust break on demo | High today | scheduling-rules clearance + ui-design landing rewrite Week 1 |
| **Prod security leak (demo accounts)** | Reputation / data | Low if checklist followed | production-runtime pre-flight |
| **pytest regression** | HOLD release | Medium | manager-value-qa weekly gate |
| **Client roster edge cases** | Failed publish | Medium | Operator-owned publish; RSI gate on client data; no self-serve until proven |
| **Stripe delay** | Blocks SaaS MRR | Low | Invoice managed blocks via e-transfer first |
| **Union dispute on schedule** | Churn | Low | Audit log + equity tallies in deliverable; scheduling-rules-coordinator review |

**What kills the $2,000 goal:** Treating this as "build more product" instead of **deploy + outbound + one paid publish**.

---

## 9. Iteration verdict

### Team confidence score: **6.5 / 10** (90 days to $2,000 MRR)

| Dimension | Score | Notes |
|-----------|-------|-------|
| Product / scheduling | **9/10** | RSI PASS, Business tests green, recent E block + weekday D wins |
| GTM readiness | **5/10** | Pipeline code exists; no live deploy, no proof bundle, landing misaligned |
| Market fit | **7/10** | Strong for Portage-style MB labs; narrow ICP |
| Execution clarity | **7/10** | This plan + subagent matrix |
| Revenue proof | **2/10** | No paying clients yet |

### To reach ≥ 7/10 before deploy

1. **production-runtime-partner:** Public URL live with production env checklist PASS.
2. **revenue-growth:** 5 prospects Previewed + 1 email sent with proof attachment.
3. **manager-value-qa:** Default pytest suite fully green (1 failure fixed).

### To reach ≥ 8/10 (Day 30)

1. First **$800+ invoice paid** or retainer signed.
2. Breakroom HTML posted by a real lab (with permission).
3. **2 Pro trials** or 1 Pro paid.

### If still below 7 after Week 4

- Pivot mix toward **higher managed pricing** ($1,200/block).
- Add **SK/AB outreach** to widen funnel.
- Do **not** expand product scope (employee portal, Postgres, Auto-Pilot) until MRR > $1,000.

---

## 10. Top 3 actions — operator this week

1. **Deploy to Streamlit Cloud** — `app.py` + `manager_app.py`, production secrets, persistent volume (`production-runtime-partner` checklist §5).
2. **Gather + preview top 3 MB prospects** — Business → Prospects → Email Preview for St. Boniface, Portage Regional, Selkirk; send first managed-service email with RSI PASS proof (`revenue-growth`).
3. **Fix landing copy + 1 pytest failure** — Remove Auto-Pilot-first messaging; green default suite (`ui-design-partner` + `manager-value-qa`).

---

## References

| Asset | Path |
|-------|------|
| Business audit | `docs/BUSINESS_CODEBASE_AUDIT.md` |
| Business UX spec | `docs/BUSINESS_SECTION_DESIGN.md` |
| Deploy guide | `deploy/DEPLOY.md` |
| Landing | `deploy/landing.html` |
| Subagents | `.cursor/agents/*.md` |
| Prospect pipeline | `src/lab_scheduler/business/` |
| Business UI | `src/lab_scheduler/ui/business/` |
| RSI gate | `scripts/rotation_rsi_gate.py` |

---

*Plan synthesized 2026-06-19. Subagents execute weekly matrix; goal-coordinator owns scorecard and course corrections until $2,000 CAD MRR.*
