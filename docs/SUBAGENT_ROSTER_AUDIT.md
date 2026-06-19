# Subagent Roster Audit — lab_staffing_scheduler

**Date:** 2026-06-19  
**Auditor role:** subagent-roster-advisor  
**North star:** $2,000 CAD/month MRR (`docs/REVENUE_2000_PLAN.md`)  
**Roster:** 9 agents in `.cursor/agents/*.md`  
**Action:** Recommendations only — no agent files deleted.

---

## Executive summary

**Roster verdict: HEALTHY with one execution gap**

Nine agents is at the soft cap (8–10) but justified: each has a distinct trigger domain, boundaries are mostly clean, and the roster maps to the revenue plan pillars (GTM, intake, rules, QA, UI, interaction QA, runtime, accountability, composition). **No agent should be removed today.**

The real problem is not roster bloat — it is **under-invocation of production-runtime-partner** and **zero proven outbound** despite strong product/QA work. The team built a sellable engine and a crash-free Business cockpit; deploy and first paid client remain unblocked human/operator actions.

**Loop iteration scorecard (user-reported): 7.5/10** — up from 6.5/10 in `REVENUE_2000_PLAN.md` after scheduling fixes, pytest green, Business button fixes, and Inbox wiring.

| Pillar | Roster coverage | Delivery status |
|--------|-----------------|-----------------|
| Outbound / GTM | revenue-growth | Plan written; **0 emails sent** |
| Post-reply intake | customer-relations | Inbox UI exists; **no live client intake yet** |
| Rules / pitch honesty | scheduling-rules-coordinator | Clearance role defined; landing still Auto-Pilot-heavy |
| Release / scheduling trust | manager-value-qa | **Strong** — RSI PASS, 528 tests green, E block + weekday D fixes |
| Business UX | ui-design-partner | Section shipped; overlaps button-flow-qa on flows |
| Clickability | button-flow-qa | **Strong** — `business_tab_pending` pattern, 7/7 flows PASS |
| Deploy / runtime | production-runtime-partner | **Not invoked to completion** — no public URL |
| Accountability | goal-coordinator | Synthesized revenue plan + scorecard discipline |
| Team composition | subagent-roster-advisor | This audit |

---

## Value ranking — most to least (this project)

| Rank | Agent | Tier | One-line justification |
|------|-------|------|------------------------|
| 1 | **manager-value-qa** | High | Turned HOLD release into SHIP: default pytest 528/528 green, RSI PASS, union weekday-D fix (13→16), legacy test isolation, debug-log removal — direct scheduling fixes (E blocks, weekday D, DN) that make the product pitchable. |
| 2 | **button-flow-qa** | High | Fixed fatal `StreamlitAPIException` on Business tab nav; `business_tab_pending` pattern + unit tests; full 7-flow matrix PASS per `BUSINESS_PRODUCTION_VERDICT.md` — without this, Revenue Pipeline is unusable. |
| 3 | **revenue-growth** | High | Authored GTM framing (managed-first), weekly task matrix, ICP scoring, honest pitch ethics; `REVENUE_2000_PLAN.md` and codebase audit consensus — but **no outbound execution evidence yet**, so rank below builders who shipped code. |
| 4 | **goal-coordinator** | High | Synthesized `REVENUE_2000_PLAN.md`, anti-partial-work discipline, scorecard template; raised team confidence narrative from 6.5→7.5 — meta-value is essential at 9-agent scale. |
| 5 | **scheduling-rules-coordinator** | Medium | Locked-rule canon prevents trust-breaking pitches; clearance gates in revenue plan; protected sacred rotation during E block / weekday D work — high leverage, lower visible artifact count than QA agents. |
| 6 | **ui-design-partner** | Medium | Business section IA, design spec alignment, prospect/email/onboarding UX — real UI shipped (`src/lab_scheduler/ui/business/`); partially superseded by button-flow-qa on the highest-impact Business bugs. |
| 7 | **production-runtime-partner** | Medium | Correct mandate (deploy P0, demo gating, cache, Stripe) and partial wins via audit (env-gated demo creds); **public deploy not done** — highest revenue blocker with lowest agent utilization. |
| 8 | **customer-relations** | Low (latent) | Fills real post-`Proceed` gap (intake briefs, Inbox replies); Inbox tab exists — but **no customer thread processed**, no intake brief artifact yet. Keep for first reply; rank low until invoked. |
| 9 | **subagent-roster-advisor** | Low (meta) | Periodic composition audits only; no product/revenue artifact — necessary at 9 agents, not daily-use. |

---

## Per-agent assessment

### 1. revenue-growth

| Field | Assessment |
|-------|------------|
| **Mission** | Outbound GTM: prospect scrub, ICP scoring, honest pitches, weekly rhythm toward trials and Pro seats. |
| **Value delivered (evidence)** | Managed-first revenue model in `BUSINESS_CODEBASE_AUDIT.md` and `REVENUE_2000_PLAN.md`; weekly task matrix (gather → preview → 5 emails → close $800 block); facility CSV + prospector integration; ethics guardrails (no fake HIPAA/customers). **Gap:** pipeline empty until human runs Gather; landing still leads Auto-Pilot per plan §2. |
| **Overlap with siblings** | ~20% with customer-relations (post-reply vs outbound); ~15% with ui-design-partner (conversion copy); ~10% with scheduling-rules-coordinator (pitch claims). |
| **Verdict** | **KEEP** — directly unblocks $2k via outbound; augment with proof-bundle and managed-first landing tasks. |
| **Value tier** | **High** |

---

### 2. manager-value-qa

| Field | Assessment |
|-------|------------|
| **Mission** | pytest + RSI gate + manager UX smoke; release SHIP/HOLD; small manager-value upgrades. |
| **Value delivered (evidence)** | `BUSINESS_CODEBASE_AUDIT.md` actions: 16 failing tests → 0; union weekday-D expectation fixed; `_agent_debug_log` removed; demo creds env-gated; RSI PASS maintained. Scheduling conversation wins: E blocks, weekday D for DE/DN, footer 2/2. Release gate now credible for sales proof. |
| **Overlap with siblings** | ~25% with button-flow-qa (manager buttons vs Business buttons); ~30% with scheduling-rules-coordinator (invariants vs policy interpretation); ~15% with production-runtime-partner (release checklist). |
| **Verdict** | **KEEP** — non-negotiable for managed-block deliverable quality. |
| **Value tier** | **High** |

---

### 3. scheduling-rules-coordinator

| Field | Assessment |
|-------|------------|
| **Mission** | Locked Portage rules canon; clearance/violation verdicts for code and pitch changes. |
| **Value delivered (evidence)** | Sacred list in codebase audit; locked claims in `REVENUE_2000_PLAN.md` §scheduling-rules-coordinator; vote consensus on managed-first and legacy auto_generate. Implicit guard during E block / weekday D / DN fixes. |
| **Overlap with siblings** | ~30% with manager-value-qa (rules vs test execution); ~15% with revenue-growth (pitch clearance). |
| **Verdict** | **KEEP** — do not merge into manager-value-qa; policy ≠ execution. |
| **Value tier** | **Medium** |

---

### 4. ui-design-partner

| Field | Assessment |
|-------|------------|
| **Mission** | Streamlit UI hierarchy, Business UX spec, CSS polish, scannable healthcare SaaS aesthetic. |
| **Value delivered (evidence)** | Business section (`section.py`, `components.py`, pipeline/prospects/email_preview); hero copy "Gather → Preview → Inbox → Proceed"; `docs/BUSINESS_SECTION_DESIGN.md` alignment; manager health panel work referenced in revenue plan. |
| **Overlap with siblings** | ~40% with button-flow-qa (flows vs clickability — **healthy if scoped**); ~20% with production-runtime-partner (loading states); ~15% with revenue-growth (CTA placement). |
| **Verdict** | **KEEP** — augment landing.html ownership and post-ship polish only; defer new Business layout work until deploy. |
| **Value tier** | **Medium** |

---

### 5. goal-coordinator

| Field | Assessment |
|-------|------------|
| **Mission** | User-intent advocate; deliverable accountability; anti–false-done across siblings. |
| **Value delivered (evidence)** | `REVENUE_2000_PLAN.md` synthesis and scorecard template; `BUSINESS_PRODUCTION_VERDICT.md` confidence framing (7/10); catches backend-without-UI and plan-without-artifact anti-patterns. User-reported loop score 7.5/10 aligns with coordinator discipline. |
| **Overlap with siblings** | ~30% with subagent-roster-advisor (outcomes vs composition — **healthy**); ~20% with each specialist (audit, not duplicate). |
| **Verdict** | **KEEP** — essential at 9 agents; invoke after multi-agent sprints. |
| **Value tier** | **High** |

---

### 6. production-runtime-partner

| Field | Assessment |
|-------|------------|
| **Mission** | Streamlit perf, session-state hygiene, deploy checklist, prod env, caching, Stripe path. |
| **Value delivered (evidence)** | Demo credential gating documented in audit actions; `deploy/DEPLOY.md` referenced in revenue plan P0; mandate covers Fill spinner, roster cache. **Not delivered:** public HTTPS URL, live Stripe, persistent volume deploy — **#1 revenue blocker** per plan §10. |
| **Overlap with siblings** | ~35% with ui-design-partner (loading/error UX); ~25% with button-flow-qa (rerun churn vs click bugs); ~20% with manager-value-qa (release gate). |
| **Verdict** | **AUGMENT** — add "Week 1 deploy exit criteria" checklist and explicit handoff from manager-value-qa after green pytest; **invoke immediately**, not prune. |
| **Value tier** | **Medium** (potential **High** once deploy runs) |

---

### 7. button-flow-qa

| Field | Assessment |
|-------|------------|
| **Mission** | Streamlit button/tab matrix; hunt StreamlitAPIException and silent no-ops. |
| **Value delivered (evidence)** | `BUSINESS_PRODUCTION_VERDICT.md`: `business_tab_pending` via `navigation.py`; all post-widget tab writes replaced with `request_business_tab()`; 7/7 controls PASS; `tests/test_business_ui.py` covers pending promotion. Team confidence 6.5→7 cited in verdict. |
| **Overlap with siblings** | ~40% with ui-design-partner (**healthy boundary**: clicks vs visuals); ~25% with manager-value-qa (Business vs manager grid); ~20% with production-runtime-partner (reruns). |
| **Verdict** | **KEEP** — do not merge into manager-value-qa; Streamlit pitfall expertise is distinct. |
| **Value tier** | **High** |

---

### 8. customer-relations

| Field | Assessment |
|-------|------------|
| **Mission** | Post-reply intake, roster collection templates, Inbox parsing, operator handoff briefs. |
| **Value delivered (evidence)** | Inbox tab shipped (`inbox.py`, `inbound_email.py`, `ONE_STOP_SHOP_VISION.md` flow); agent added to close Proceed→Fill gap. **No evidence yet:** intake brief, reply template used on real prospect, completeness score on live thread. |
| **Overlap with siblings** | ~25% with revenue-growth (outbound vs post-reply); ~15% with ui-design-partner (onboarding forms); ~10% with button-flow-qa (Inbox nav). |
| **Verdict** | **KEEP** — augment with one worked example from synthetic Manitoba prospect; **review in 30 days** — if still zero replies, consider MERGE intake templates into revenue-growth (not delete file). |
| **Value tier** | **Low** (latent **Medium** at first client) |

---

### 9. subagent-roster-advisor

| Field | Assessment |
|-------|------------|
| **Mission** | Roster overlap/gap detection; KEEP/PRUNE/MERGE/ADD recommendations; team composition vs $2k plan. |
| **Value delivered (evidence)** | This audit; overlap matrix heuristics prevented premature merge of button-flow-qa and manager-value-qa in prior design. |
| **Overlap with siblings** | ~30% with goal-coordinator (composition vs accountability — intentional). |
| **Verdict** | **KEEP** — invoke quarterly or when adding agent #10; not daily. |
| **Value tier** | **Low** (meta) |

---

## Overlap matrix

| Pair | Overlap % | Boundary status | Action |
|------|-----------|-----------------|--------|
| ui-design-partner ↔ button-flow-qa | ~40% | Healthy — design vs clickability | **Monitor** |
| manager-value-qa ↔ button-flow-qa | ~25% | Healthy — rotation QA vs Business nav | KEEP separate |
| production-runtime-partner ↔ ui-design-partner | ~35% | Healthy — perf vs visual loading | KEEP separate |
| revenue-growth ↔ customer-relations | ~20% | Healthy — outbound vs intake | KEEP separate |
| scheduling-rules-coordinator ↔ manager-value-qa | ~30% | Healthy — policy vs tests | KEEP separate |
| goal-coordinator ↔ subagent-roster-advisor | ~30% | Healthy — tasks vs team | KEEP separate |
| manager-value-qa ↔ production-runtime-partner | ~20% | Healthy — QA vs deploy | Coordinate on release |
| revenue-growth ↔ scheduling-rules-coordinator | ~15% | Healthy — pitch vs clearance | KEEP separate |

**No pair exceeds 60% merge threshold.**

---

## North-star lens: revenue-direct vs nice-to-have

| Agent | Revenue impact | Role |
|-------|----------------|------|
| revenue-growth | **Direct** | Outbound, first $800 block, proof bundle |
| production-runtime-partner | **Direct** | Public URL, Stripe, trial signup — **currently blocking** |
| manager-value-qa | **Direct** | RSI PASS + green tests = sales proof for managed blocks |
| button-flow-qa | **Direct** | Business cockpit must work to run pipeline |
| scheduling-rules-coordinator | **Direct** | Prevents pitch/demo trust break on rotation claims |
| customer-relations | **Direct** (at first reply) | Roster intake → first publish without email ping-pong |
| ui-design-partner | **Indirect–Medium** | Conversion polish; landing alignment still P0 |
| goal-coordinator | **Indirect** | Prioritization, stops wasted agent cycles |
| subagent-roster-advisor | **Nice-to-have** | Prevents roster drift; no revenue artifact |

**What kills $2k is not missing agents — it is missing deploy + outbound execution** (`REVENUE_2000_PLAN.md` §8).

---

## Team-level recommendations

### Remove? (recommend only — do not delete files)

**None today.** All nine earn a place on paper.

**Watch list (30-day review):**
- **customer-relations** — if zero Inbox threads processed, merge intake templates into `revenue-growth.md` as a section and stop standalone invocation (keep file).
- **subagent-roster-advisor** — if never invoked again, treat as on-demand only (still keep file).

### Augment? (add to `.md` files)

| Agent | Add |
|-------|-----|
| **production-runtime-partner** | Week 1 deploy exit checklist copied from `REVENUE_2000_PLAN.md` §5; explicit "DONE = public URL + production secrets verified"; Stripe webhook deploy steps as mandatory smoke. |
| **revenue-growth** | `docs/sales/proof_bundle.md` template; managed-first landing rewrite task; "first 5 MB emails" tracking tied to Business pipeline statuses. |
| **customer-relations** | One full worked example: St. Boniface prospect reply → 85% intake brief; Inbox → Proceed handoff steps with file paths. |
| **manager-value-qa** | Client publish runbook (import → Distribute → Fill → RSI on client roster → breakroom HTML); `docs/RELEASE_EVIDENCE.md` template. |
| **ui-design-partner** | Explicit `deploy/landing.html` ownership for managed-first hero; "no grid changes during revenue sprint" constraint. |
| **button-flow-qa** | Add Inbox tab to mandatory smoke matrix (currently 7 flows — Inbox is 8th). |
| **scheduling-rules-coordinator** | Landing.html claim audit checklist (Auto-Pilot, HIPAA, customer logos). |

### Missing specialist?

**No new agent needed before first paying client.**

| Considered role | Verdict |
|-----------------|---------|
| deploy-ops / billing-ops | Absorbed by **production-runtime-partner** — augment, don't add |
| copywriter | Absorbed by **revenue-growth** + **ui-design-partner** |
| legal/compliance | Human-only per revenue plan §6 |
| data/CRM engineer | Defer until >10 prospects in pipeline |

**Add only after:** first client signed AND operator hours/block exceed 6h — then consider **operations-runbook** specialist (not before).

### Is 9 agents too many?

**At the soft cap, but acceptable** for this phase because:
- Product is multi-surface (scheduling engine + Business GTM + Streamlit footguns + deploy).
- Boundaries are documented and mostly held.
- Coordination cost is mitigated by **goal-coordinator**.

**Do not add a 10th agent** until one of: (a) first paying client, (b) deploy live + weekly outbound rhythm, or (c) same gap cited 3+ times in scorecards.

**Optional consolidation (not recommended now):**
- MERGE customer-relations → revenue-growth **only if** no intake work in 30 days.
- MERGE subagent-roster-advisor into goal-coordinator **not recommended** — scope collision.

---

## Sibling perspectives (synthesized)

- **goal-coordinator:** Recent work scores Complete on scheduling QA and Business button flows; Partial on deploy and outbound; Off-track risk if "build more product" continues without Streamlit Cloud.
- **revenue-growth:** GTM plan exists; execution gap is human send + proof attachment, not missing agent.
- **manager-value-qa:** Release QA and Business button QA stayed separate in practice — merge would lose Streamlit-specific expertise.
- **button-flow-qa:** Highest-impact fix this sprint; boundaries with ui-design-partner held.
- **customer-relations:** Role validated by Inbox ship; awaits first live thread.

---

## Suggested actions

1. **[Action: Invoke production-runtime-partner Week 1 deploy]** — Streamlit Cloud `app.py` + `manager_app.py`; done when public URL in scorecard.
2. **[Action: Invoke revenue-growth gather + first email]** — 3 MB prospects Previewed with RSI proof; human sends mailto.
3. **[Action: Augment button-flow-qa matrix]** — Add Inbox tab smoke to agent `.md` and re-run after any Business change.

---

## User decisions required

| Proposal | Decision |
|----------|----------|
| Keep all 9 agents | **Recommended: approve** |
| Augment production-runtime-partner + revenue-growth | **Recommended: approve** |
| Merge customer-relations → revenue-growth in 30 days if unused | **Optional: decide at Day 30** |
| Add 10th agent | **Recommended: reject until first client** |

---

*Audit complete. No files deleted in `.cursor/agents/`.*
