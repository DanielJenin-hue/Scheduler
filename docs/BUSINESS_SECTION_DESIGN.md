# Business Section UX Design Spec

**Product:** Port Optical · Portage Lab Staffing Scheduler  
**Audience:** Internal operator (founder / GTM) pursuing Manitoba hospital lab managers  
**Platform:** Streamlit operator console (`scripts/app.py`) — Business section as a dedicated top-level area, visually distinct from scheduling ops  
**Status:** Design only — no implementation in this document  
**Date:** 2026-06-19

---

## 1. Design intent

The Business section is the **revenue cockpit**: find the right lab managers, preview exactly what you will send before anything goes out, and advance a prospect into a real client workspace with one deliberate action.

**North-star interaction:** *Review → Preview email → Proceed with client.*

This mirrors how Stripe surfaces money movement: calm, confident, metric-forward — but the subject matter is hospital lab staffing, not payments. Every screen should feel **trustworthy enough for healthcare** and **sharp enough for outbound sales**.

**Guiding principles**

| Principle | Meaning |
|-----------|---------|
| Preview before send | No prospect advances without seeing the full email draft |
| Human in the loop | Auto-gather proposes; the operator approves, edits, or dismisses |
| Honest maturity | Copy templates never imply fake logos, HIPAA certification, or customers we do not have |
| Manitoba-first | Default filters, pitch angles, and facility dataset center Prairies / MB |
| One clear CTA per card | Primary action is always obvious; secondary actions are quiet |

---

## 2. Information architecture

### 2.1 Top-level placement

Add **Business** as a primary nav item in the operator console (alongside Schedule, Staff, Settings). Only visible to **operator / admin** roles — not manager tenants.

```
┌─────────────────────────────────────────────────────────────────┐
│  Port Optical          Schedule · Staff · Business · Settings   │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Section tabs

Four tabs within Business. Tab order reflects the daily workflow left-to-right.

| Tab | Purpose | Primary user question |
|-----|---------|----------------------|
| **Pipeline** | Kanban + metrics overview | "Where are my deals and what should I do today?" |
| **Prospects** | Searchable queue of gathered leads | "Who should I reach out to this week?" |
| **Email Preview** | Full-screen draft review for one prospect | "Exactly what will they receive?" |
| **Client Onboarding** | Post-commit setup checklist | "What do I need to deliver for this new client?" |

### 2.2.1 Tab relationships

```
                    ┌──────────────┐
   Auto-gather ───► │  Prospects   │
                    └──────┬───────┘
                           │ "Preview email"
                           ▼
                    ┌──────────────┐
                    │ Email Preview│
                    └──────┬───────┘
                           │ "Proceed with client"
                           ▼
                    ┌──────────────┐       ┌──────────────────┐
                    │   Pipeline   │ ◄──── │ Client Onboarding│
                    │ (Active Client)│     │  (setup tasks)   │
                    └──────────────┘       └──────────────────┘
```

- **Prospects** is the intake queue (status: `New`, optionally `Previewed`).
- **Email Preview** is a focused sub-flow — opened from a prospect card, not a dead-end page.
- **Pipeline** shows all stages including `Active Client` and archived `Passed`.
- **Client Onboarding** auto-opens after "Proceed with client" and stays linked from Pipeline cards.

### 2.3 Pipeline stages (kanban columns)

| Stage | Status badge | Entry trigger | Exit trigger |
|-------|--------------|---------------|--------------|
| **New** | `New` | Auto-gather or manual add | Operator opens email preview |
| **Previewed** | `Previewed` | Email preview viewed ≥1s / explicit "Mark previewed" | "Proceed with client" or "Pass" |
| **Active Client** | `Active Client` | "Proceed with client" confirmed | N/A (terminal success) |
| **Passed** | `Passed` | Operator dismisses with reason | Can restore to New |

Optional future columns (defer v1): `Contacted`, `Trial`, `Paid Pro`. v1 collapses outreach tracking into `Previewed` + notes.

### 2.4 URL / session routing (Streamlit)

| Route key | Tab | Deep-link params |
|-----------|-----|------------------|
| `business_pipeline` | Pipeline | — |
| `business_prospects` | Prospects | `?region=MB&sort=icp` |
| `business_email_preview` | Email Preview | `?prospect_id=MB-WPG-STB` |
| `business_onboarding` | Client Onboarding | `?tenant_id=tenant-st-boniface` |

Use `st.session_state["business_prospect_id"]` for preview context so back-navigation preserves scroll position in Prospects.

---

## 3. Visual design

### 3.1 Aesthetic direction

**Stripe dashboard meets healthcare SaaS:** dark, refined shell; generous whitespace inside cards; monospace accents for IDs and scores; soft clinical blues with revenue green for money metrics.

Extend the existing landing palette (`deploy/landing.html`) rather than inventing a new brand.

### 3.2 Color palette

| Token | Hex | Usage |
|-------|-----|-------|
| `--biz-bg` | `#0b1220` | Page background (matches landing) |
| `--biz-surface` | `#111827` | Card background |
| `--biz-surface-raised` | `#1a2332` | Hover / selected card |
| `--biz-border` | `#1f2937` | Card borders, dividers |
| `--biz-text` | `#e5e7eb` | Primary text |
| `--biz-muted` | `#94a3b8` | Secondary labels, metadata |
| `--biz-accent` | `#38bdf8` | Links, focus rings, primary buttons (sky — clinical trust) |
| `--biz-accent-deep` | `#082f49` | Text on primary buttons |
| `--biz-revenue` | `#22c55e` | MRR, savings projections, success states |
| `--biz-revenue-muted` | `#166534` | Revenue badge backgrounds |
| `--biz-warning` | `#fbbf24` | Medium ICP, needs review |
| `--biz-danger` | `#f87171` | Pass / dismiss destructive secondary |
| `--biz-clinical` | `#a5b4fc` | Healthcare context chips (region, FTE) |

**Light mode:** defer to v2; v1 ships dark-only to match landing and reduce Streamlit theming work.

### 3.3 Typography

| Role | Font stack | Size / weight |
|------|------------|---------------|
| Display / page title | `"Segoe UI", system-ui, sans-serif` | 1.75rem / 600 |
| Section heading | same | 1.125rem / 600 |
| Card title (facility name) | same | 1rem / 600 |
| Body | same | 0.9375rem / 400, line-height 1.55 |
| Metadata / labels | same | 0.8125rem / 500, uppercase tracking 0.04em, `--biz-muted` |
| Metrics (ICP, savings) | `"Cascadia Code", "Consolas", monospace` | 0.875rem / 600 |
| Email preview body | `"Georgia", "Times New Roman", serif` | 1rem / 400 — signals "this is what they read" |

### 3.4 Layout & spacing

- **Max content width:** 1200px (wider than landing — data-dense dashboard).
- **Grid:** 12-column; prospect cards span 4 cols desktop, 6 tablet, 12 mobile.
- **Card padding:** 20px; border-radius 12px; 1px `--biz-border`.
- **Section gap:** 32px between hero metrics and card grid.
- **Sticky sub-header** on Email Preview: facility name + status badge + action buttons always visible.

### 3.5 Card anatomy (prospect)

```
┌────────────────────────────────────────────────────────────┐
│  St. Boniface Hospital                          [New]      │
│  Prairies · MB · 1.1M tests/yr                             │
│────────────────────────────────────────────────────────────│
│  ICP 18/25  ████████░░   Est. savings $198k/yr             │
│  Contact: Lab Manager (role) · LinkedIn / directory          │
│  Pain: MLT hiring surge · rotation complexity                │
│  Pitch: "Footer 2/2 before breakroom posting"              │
│────────────────────────────────────────────────────────────│
│  [ Preview email ]              [ Pass ]                   │
└────────────────────────────────────────────────────────────┘
```

### 3.6 Status badges

Pill shape, 11px uppercase, 6px vertical padding, 10px horizontal.

| Status | Background | Text | Border |
|--------|------------|------|--------|
| **New** | `#1e3a5f` | `#38bdf8` | `#2563eb40` |
| **Previewed** | `#422006` | `#fbbf24` | `#d9770640` |
| **Active Client** | `#14532d` | `#22c55e` | `#16a34a40` |
| **Passed** | `#1f2937` | `#94a3b8` | `#374151` |

### 3.7 Empty states

Each tab gets a bespoke empty state — never a bare "No data."

| Tab | Illustration | Headline | Body | CTA |
|-----|--------------|----------|------|-----|
| Pipeline | Minimal kanban outline (ASCII/icon) | "Your pipeline is clear" | "Run a prospect scan to fill the New column with Manitoba hospital labs." | **Gather prospects** |
| Prospects | Radar / map pin | "No prospects in queue" | "Import from `regional_facilities.csv` or run the weekly Prospector scan." | **Run auto-gather** |
| Email Preview | Envelope | "Select a prospect to preview" | "Open any card in Prospects and click Preview email." | **Go to Prospects** |
| Client Onboarding | Checklist | "No clients onboarding" | "When you proceed with a prospect, their setup checklist appears here." | **View Pipeline** |

Empty states use `--biz-muted` copy and a single `--biz-accent` button.

### 3.8 Motion & feedback

- Card hover: `surface → surface-raised`, 150ms ease.
- "Proceed with client": confirmation modal with 300ms fade — consequential action.
- Toast on success: "Tenant created · Opening onboarding" (4s, bottom-right).
- Auto-gather progress: indeterminate bar + "Scanning 8 facilities…" — maps to `run_prospector_scan`.

---

## 4. Prospect card fields

Each prospect merges **facility dataset**, **Prospector viability report**, and **revenue-growth scrub** output.

### 4.1 Required fields

| Field | Source | Display |
|-------|--------|---------|
| **Facility name** | `RegionalFacilityRecord.facility_name` | Card title |
| **Facility ID** | `facility_id` (e.g. `MB-WPG-STB`) | Monospace subtitle, copy button |
| **Region / province** | `region`, `state_province` | Chip row |
| **Annual test volume** | `annual_test_volume` | Formatted `1.1M tests/yr` |
| **Roster size** | `mlt_fte` + `mla_fte` | `25 FTE (14 MLT · 11 MLA)` |
| **Contact** | Manual / research | Name, title, channel (email pattern, LinkedIn URL) |
| **ICP score** | Revenue-growth 5×5 rubric | `18/25` + progress bar |
| **Deployment score** | `ViabilityReport.deployment_score` | Secondary metric, tooltip explains formula |
| **Est. annual savings** | `ViabilityReport.estimated_annual_savings_usd` | Green monospace, from `prospector.py` hard-lock model |
| **Pain signals** | Scrub + heuristics | 2–4 bullet tags |
| **Suggested pitch angle** | Generated one-liner | Italic quote under pain tags |
| **Rationale** | `ViabilityReport.rationale` | Expandable "Why this facility?" |
| **Status** | Pipeline state | Badge |
| **Last updated** | Scan timestamp | Relative time |

### 4.2 ICP score rubric (1–5 each, show total /25)

Aligned with `.cursor/agents/revenue-growth.md`:

| Dimension | Strong signal (5) | Weak signal (1) |
|-----------|-------------------|-----------------|
| Size fit | 15–60 lines, hospital lab | Clinic / &lt;10 FTE |
| Pain likelihood | Union rules, breakroom cadence, rotation complexity | Unknown workflow |
| Reachability | Named contact + professional channel | Facility only, no contact |
| Timing | Hiring surge, schedule change window | No public signals |
| Strategic | Manitoba anchor, reference potential | Far from ICP geography |

**Composite display:** score + qualitative band:

- **22–25:** "Strong fit" (green)
- **16–21:** "Good fit" (accent)
- **10–15:** "Moderate" (warning)
- **&lt;10:** "Low fit" (muted — default collapsed in Prospects, visible in detail)

### 4.3 Pain signal tags (examples)

Auto-derived where possible; operator can add/remove.

| Signal | Detection hint |
|--------|----------------|
| `MLT hiring` | Job postings / news |
| `Rotation complexity` | High FTE + high volume |
| `Footer coverage risk` | Pitch template default for hospital labs |
| `Excel scheduling` | Manual scrub note |
| `Union negotiation` | News / regional health authority |
| `Accreditation cycle` | Public accreditation dates |

### 4.4 Suggested pitch angle

One sentence mapping pain → product capability. Examples:

- *"Your 40-line roster and monthly breakroom post are a fit for our 8-week Portage-style catalog — footer 2/2/2 before you publish."*
- *"With 1.1M tests/yr and 25 FTE, manual rotation swaps likely eat manager hours — we deliver a compliance-checked HTML breakroom grid in one session."*

Generated from template + facility fields; editable in Email Preview.

### 4.5 Sort & filter (Prospects tab)

**Sort:** ICP score (default) · Est. savings · Test volume · Recently added  
**Filter:** Province (MB default) · Min volume · Has contact · Status · Region

---

## 5. Email preview flow

Email Preview is a **full-width editor** — the most important screen in the section.

### 5.1 Entry points

1. Prospect card → **Preview email** (primary)
2. Pipeline card (New) → **Preview email**
3. Deep link `?prospect_id=…`

On entry: status auto-updates to `Previewed` (configurable — see settings).

### 5.2 Layout

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ← Back to Prospects    St. Boniface Hospital    [Previewed]             │
├──────────────────────────────┬──────────────────────────────────────────┤
│  CONTEXT PANEL (320px)       │  EMAIL PREVIEW                            │
│                              │                                           │
│  ICP 18/25                   │  To: [lab.manager@…        ] [↗ LinkedIn]│
│  Contact: …                  │  Subject: [________________________]     │
│  Pain tags                   │  ─────────────────────────────────────   │
│  Pitch angle (editable)      │  Hi {{first_name}},                       │
│                              │                                           │
│  Template: [First touch ▼]   │  {{pain_opener}}                          │
│  Channel:  Email · LinkedIn  │                                           │
│                              │  {{solution_paragraph}}                   │
│  [ Regenerate draft ]        │                                           │
│                              │  {{proof_paragraph}}                      │
│                              │                                           │
│                              │  {{cta_line}}                             │
│                              │  — {{sender_name}}                        │
├──────────────────────────────┴──────────────────────────────────────────┤
│  [ Copy to clipboard ]  [ Open in mail client ]     [ Proceed with client ▶ ] │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.3 Template variables

| Variable | Populated from | Editable |
|----------|----------------|----------|
| `{{first_name}}` | Contact scrub | Yes |
| `{{facility_name}}` | Facility record | Yes |
| `{{facility_short_name}}` | Derived (drop "Hospital") | Yes |
| `{{region}}` | Facility record | Yes |
| `{{mlt_mla_summary}}` | e.g. "14 MLT and 11 MLA lines" | Yes |
| `{{annual_test_volume}}` | Formatted volume | Yes |
| `{{pain_opener}}` | Top pain signal → sentence | Yes |
| `{{solution_paragraph}}` | Product capabilities mapped to pain | Yes |
| `{{proof_paragraph}}` | Honest proof only (RSI gate, demo roster, trial) | Yes |
| `{{estimated_savings}}` | From viability report | Yes |
| `{{cta_line}}` | Template default | Yes |
| `{{sender_name}}` | Operator profile | Yes |
| `{{trial_link}}` | `APP_BASE_URL/?signup=1` | Auto |

**Template library (v1):**

1. **First touch — managed service** (default): pain → we run the scheduler → breakroom HTML deliverable  
2. **First touch — trial SaaS**: pain → 14-day trial → self-serve manager workspace  
3. **Follow-up #2**: bump with rotation compliance angle  
4. **LinkedIn connection note**: ≤300 chars, separate preview pane

### 5.4 Subject line

- Separate input above body; included in copy payload.
- Default pattern: `{{facility_short_name}} lab schedule — quick idea`
- Character count indicator; warn if &gt;60 chars.

### 5.5 Editable preview behavior

- **Live merge:** typing in context panel or body updates preview instantly.
- **Regenerate draft:** re-runs pitch generator from current pain tags + template; confirms before overwrite.
- **Diff hint:** if operator edited body manually, show subtle "Customized" chip on tab.
- **Honesty linter:** soft warning if proof paragraph contains blocked phrases (`HIPAA certified`, `used by X hospital`) — matches revenue-growth ethical constraints.

### 5.6 CTA buttons

| Button | Action |
|--------|--------|
| **Copy to clipboard** | Copies `Subject:` + blank line + body as plain text; toast confirmation |
| **Open in mail client** | `mailto:` with encoded subject/body if email present; else disabled with tooltip |
| **Proceed with client** | Primary green; opens confirmation modal (§6) |
| **Pass** | Secondary; requires reason dropdown + optional note |

LinkedIn variant: **Copy connection note** instead of mailto.

---

## 6. Proceed with client

"Proceed with client" is the **commit point** — it means we are pursuing this facility as a real engagement, not just sending an email.

### 6.1 Confirmation modal

```
┌─────────────────────────────────────────────────────────┐
│  Proceed with St. Boniface Hospital?                    │
│                                                         │
│  This will:                                             │
│  ✓ Create a client tenant (draft)                       │
│  ✓ Mark prospect as Active Client in Pipeline           │
│  ✓ Open Client Onboarding checklist                     │
│  ✓ Pre-seed facility metadata from regional dataset     │
│                                                         │
│  Engagement type:  (•) Managed first   ( ) Trial SaaS   │
│  Tenant slug:        [ st-boniface-health        ]      │
│                                                         │
│            [ Cancel ]    [ Proceed with client ]        │
└─────────────────────────────────────────────────────────┘
```

### 6.2 System actions (ordered)

| Step | Action | Detail |
|------|--------|--------|
| 1 | **Create tenant** | Insert into `tenants` table (pattern from `seed_southbridge_tenant.sql`): `id=tenant-{slug}`, `name={facility_name}`, `status=draft`, slug from modal |
| 2 | **Seed tenant configuration** | `tenant_configurations`: `facility_id`, `region`, `annual_test_volume`, `mlt_fte`, `mla_fte`, `engagement_type`, `prospect_source=prospector` |
| 3 | **Link prospect record** | Store `tenant_id` on prospect; prevents duplicate Proceed |
| 4 | **Update pipeline status** | `New` / `Previewed` → `Active Client` |
| 5 | **Mark facility deployed** | Add `facility_id` to `deployed_facility_ids` so `run_prospector_scan` excludes it |
| 6 | **Persist email snapshot** | Save final subject + body + timestamp to prospect history (outreach log) |
| 7 | **Navigate** | Redirect to **Client Onboarding** tab with `tenant_id` |
| 8 | **Optional: open manager workspace** | Button "Open manager workspace" → spawns `manager_app` session scoped to new tenant (or deep link if hosted separately) |

**What it does NOT do (v1):**

- Does not send email automatically (operator copies/sends externally).
- Does not charge Stripe — billing remains manual / separate flow.
- Does not import roster — onboarding checklist covers import.

### 6.3 Client Onboarding checklist (auto-created)

| Task | Status | Action link |
|------|--------|-------------|
| Send outreach email | Pending | Opens Email Preview snapshot (read-only) |
| Collect roster CSV | Pending | Link to import docs / upload in manager app |
| Create schedule period | Pending | Open manager app → Period setup |
| Run Distribute / Fill / Save | Pending | Open manager app |
| RSI gate check | Pending | Run `rotation_rsi_gate.py` or in-app health |
| Deliver breakroom HTML | Pending | Manager Print tab |
| Invoice first block | Pending | External (manual) |
| Request testimonial | Pending | Note field |

Progress bar at top: `2/8 complete`. Completing "Open manager workspace" does not auto-check tasks — operator marks done.

### 6.4 Active Client card (Pipeline)

Shows: facility name, tenant slug, engagement type, onboarding %, MRR placeholder, **Open manager app** button, last activity.

---

## 7. Auto-gather flow

Auto-gather replaces manual CSV scanning with a **review queue** the operator controls.

### 7.1 Trigger points

| Trigger | Location |
|---------|----------|
| **Gather prospects** button | Prospects empty state + Prospects toolbar |
| **Weekly scan** | Background via `rsi/manager.py` / `auto_manager.py` (existing) |
| **Refresh dataset** | Settings → upload new `regional_facilities.csv` |

### 7.2 Pipeline stages (auto-gather)

```
┌─────────────┐    ┌──────────────┐    ┌─────────────┐    ┌──────────────┐
│ Load CSV    │───►│ Score & rank │───►│ Dedupe vs    │───►│ Queue as     │
│ + optional  │    │ (prospector) │    │ pipeline +   │    │ New prospects│
│ web enrich  │    │ + ICP rubric │    │ deployed IDs │    │ for review   │
└─────────────┘    └──────────────┘    └─────────────┘    └──────────────┘
```

### 7.3 Step detail

**1. Load facilities**

- Read `data/rsi/regional_facilities.csv` via `load_regional_facility_dataset`.
- Optional v1.1: operator paste CSV or add single facility form.

**2. Score & rank**

- Run `run_prospector_scan(dataset_path, high_volume_threshold=750_000, deployed_facility_ids=…)`.
- For each `ViabilityReport`, compute ICP score (§4.2).
- Merge into unified prospect record.

**3. Dedupe**

Skip if:

- `facility_id` already in pipeline (any status except Passed &gt;90 days).
- `facility_id` in `deployed_facility_ids` (already Active Client).
- Operator previously Passed with reason "Duplicate".

**4. Web enrich (optional, async)**

- Search public sources for lab manager title, hiring signals, news.
- Never scrape private data; flag low-confidence contacts with "Verify" chip.
- Revenue-growth agent can run headless and write results to `.rsi/prospector/enrichment_{date}.json`.

**5. Queue for review**

- Insert new records as `New` in Prospects.
- Show summary toast: "4 new prospects · 2 skipped (already in pipeline)".

### 7.4 Auto-gather UI (progress panel)

```
┌────────────────────────────────────────────────────────────┐
│  Gathering prospects…                                      │
│  ████████████░░░░░░░░  63%                                 │
│                                                            │
│  ✓ Loaded 8 facilities from regional dataset               │
│  ✓ Scored viability (prospector)                           │
│  ◌ Enriching contacts (optional)                           │
│  · Queuing for review                                      │
└────────────────────────────────────────────────────────────┘
```

### 7.5 Review queue behavior

- New prospects appear at top of Prospects tab with `New` badge.
- Operator reviews highest ICP first (default sort).
- Batch actions (defer v1.1): select multiple → Pass.

---

## 8. Wireframe descriptions

### 8.1 Pipeline (default Business landing)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Business                                                                      │
│ Pipeline · Prospects · Email Preview · Client Onboarding                     │
├──────────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐             │
│  │ MRR         │ │ Active      │ │ In preview  │ │ Top target  │             │
│  │ $299/mo     │ │ 1 client    │ │ 3 prospects │ │ St. Boniface│             │
│  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘             │
│                                                                               │
│  NEW (2)          PREVIEWED (1)      ACTIVE CLIENT (1)      PASSED (▾)       │
│  ┌──────────┐     ┌──────────┐       ┌──────────┐                            │
│  │ Card     │     │ Card     │       │ Southbr. │                            │
│  │ Card     │     │          │       │ Onbd 75% │                            │
│  └──────────┘     └──────────┘       └──────────┘                            │
│                                                                               │
│  [ Gather prospects ]                                                         │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Behavior:** Kanban columns horizontally scroll on narrow viewports. Metric tiles pull from `ValueFirstDashboard` (`total_revenue_month_usd`, `next_best_facility_target`). Passed column collapsed by default.

### 8.2 Prospects (list + filters)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Prospects                                    [ Gather prospects ]  [ + Add ] │
│ Filters: [MB ▼] [All statuses ▼] [Min volume ▼]     Sort: [ICP score ▼]      │
├──────────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐   │
│  │ St. Boniface Hosp   │ │ Ottawa Civic Lab    │ │ Selkirk Regional    │   │
│  │ ICP 18/25  $198k/yr │ │ ICP 12/25  $89k/yr  │ │ ICP 8/25   low fit  │   │
│  │ [Preview email]     │ │ [Preview email]     │ │ [Preview email]     │   │
│  └─────────────────────┘ └─────────────────────┘ └─────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

Low-fit cards render muted with collapsed pain details; still accessible for manual outreach.

### 8.3 Email Preview

See §5.2 — split pane: context left, rendered email right, action bar pinned bottom.

**Mobile / narrow:** context panel collapses to accordion above email body.

### 8.4 Client Onboarding

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Client Onboarding — St. Boniface Hospital          [ Active Client ]         │
│ tenant-st-boniface-health · Managed first · Created today                    │
├──────────────────────────────────────────────────────────────────────────────┤
│  Setup progress  ████████░░░░░░░░  3/8                                         │
│                                                                               │
│  ☐ Send outreach email              [ View sent draft ]                       │
│  ☑ Create tenant                    Done automatically                        │
│  ☐ Collect roster CSV               [ Open import guide ]                     │
│  ☐ Create schedule period           [ Open manager workspace ]                │
│  ☐ Distribute / Fill / Save         [ Open manager workspace ]                │
│  ☐ RSI gate pass                    [ Run check ]                             │
│  ☐ Deliver breakroom HTML           [ Open Print tab ]                        │
│  ☐ Invoice first block              [ Add note ]                                │
│                                                                               │
│  Notes ─────────────────────────────────────────────────────────────────────  │
│  │ Contact: Jane Doe, Lab Manager — met at MLT conference…                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 9. Component list (implementation)

Components map to Streamlit custom components or composable Python renderers under `src/lab_scheduler/ui/business/`.

### 9.1 Layout & navigation

| Component | Responsibility |
|-----------|----------------|
| `BusinessSectionShell` | Top-level wrapper, tab routing, role gate |
| `BusinessTabBar` | Pipeline · Prospects · Email Preview · Client Onboarding |
| `BusinessMetricTiles` | MRR, active clients, preview count, next target |

### 9.2 Pipeline

| Component | Responsibility |
|-----------|----------------|
| `PipelineKanban` | Horizontal columns by status |
| `PipelineColumn` | Header + count + card list |
| `PipelineCard` | Compact card variant for kanban |
| `PassedColumnCollapse` | Expand/collapse archived |

### 9.3 Prospects

| Component | Responsibility |
|-----------|----------------|
| `ProspectGrid` | Responsive card grid |
| `ProspectCard` | Full field layout (§4) |
| `ProspectFilters` | Province, status, volume, sort |
| `IcpScoreBar` | Numeric score + 5-dimension tooltip |
| `PainSignalTags` | Chip list + add/remove |
| `SavingsBadge` | Monospace green savings from viability report |
| `ContactRow` | Name, title, channel icons |

### 9.4 Email preview

| Component | Responsibility |
|-----------|----------------|
| `EmailPreviewLayout` | Split pane + sticky header/footer |
| `EmailContextPanel` | ICP, contact, template picker |
| `EmailSubjectInput` | Subject with char count |
| `EmailBodyEditor` | Rich-enough textarea / markdown preview |
| `TemplateVariableMerge` | Merge + live update |
| `PitchAngleEditor` | Single-line angle → regenerates opener |
| `HonestyLinter` | Soft warning on blocked claims |
| `EmailActionBar` | Copy, mailto, Proceed, Pass |
| `OutreachTemplateSelect` | First touch managed / trial / follow-up / LinkedIn |

### 9.5 Proceed & onboarding

| Component | Responsibility |
|-----------|----------------|
| `ProceedConfirmModal` | Engagement type + slug + confirm |
| `TenantProvisioner` | Backend: create tenant + config (wraps SQL helpers) |
| `OnboardingChecklist` | Task list + progress bar |
| `OnboardingTaskRow` | Checkbox, label, action link |
| `OpenManagerWorkspaceButton` | Deep link to `manager_app` with tenant context |

### 9.6 Auto-gather

| Component | Responsibility |
|-----------|----------------|
| `GatherProspectsButton` | Triggers scan flow |
| `GatherProgressPanel` | Step checklist + progress bar |
| `ProspectScanService` | Wraps `run_prospector_scan` + ICP merge + dedupe |
| `EnrichmentWorker` | Optional async public-source enrich |
| `GatherSummaryToast` | "N new · M skipped" |

### 9.7 Shared / design system

| Component | Responsibility |
|-----------|----------------|
| `StatusBadge` | New / Previewed / Active Client / Passed |
| `EmptyState` | Per-tab empty content |
| `ConfirmDialog` | Generic modal |
| `CopyButton` | Clipboard + toast |
| `MonospaceMetric` | Aligned numeric display |
| `BusinessThemeCSS` | Injects §3 palette tokens into Streamlit |

### 9.8 Data layer (backend modules)

| Module | Responsibility |
|--------|----------------|
| `business/prospect_store.py` | CRUD prospects, status transitions |
| `business/outreach_log.py` | Email snapshots, timestamps |
| `business/onboarding_store.py` | Checklist state per tenant |
| `business/gather.py` | Orchestrates prospector + dedupe + queue |
| `business/pitch_generator.py` | Template merge, pitch angle text |
| `business/icp_scoring.py` | 5-dimension rubric |

Persist to SQLite alongside existing tenant tables; JSON artifacts remain compatible with `.rsi/prospector/viability_{date}.json`.

---

## 10. Data model sketch (prospect record)

```yaml
prospect_id: "MB-WPG-STB"           # same as facility_id
facility_name: "St. Boniface Hospital"
region: "Prairies"
state_province: "MB"
annual_test_volume: 1100000
mlt_fte: 14.0
mla_fte: 11.0
contact:
  name: null                          # enriched later
  title: "Lab Manager"
  email: null
  linkedin_url: null
  confidence: "unverified"            # verified | unverified
icp_score: 18
icp_dimensions: { size: 5, pain: 4, reach: 3, timing: 3, strategic: 3 }
viability:
  deployment_score: 142.0
  estimated_annual_savings_usd: 198000.0
  rationale: "High-volume lab (1,100,000 tests/yr)…"
pain_signals: ["MLT hiring", "Rotation complexity"]
pitch_angle: "Footer 2/2 before breakroom posting"
status: "new"                         # new | previewed | active_client | passed
tenant_id: null                       # set on Proceed
pass_reason: null
email_snapshot: null                  # { subject, body, template_id, sent_at }
created_at: "2026-06-19T12:00:00Z"
updated_at: "2026-06-19T12:00:00Z"
source: "prospector_scan"             # manual | prospector_scan | enrichment
```

---

## 11. Success metrics (UX)

| Metric | Target |
|--------|--------|
| Time from open Business → email copied | &lt; 90 seconds |
| Prospects reviewed per weekly scan | ≥ 80% marked Previewed or Passed |
| Proceed without preview | 0 (disabled by default) |
| Duplicate tenant creation | 0 (dedupe on facility_id) |

---

## 12. Out of scope (v1)

- Automated email send (SMTP / SendGrid)
- CRM sync (HubSpot, Salesforce)
- LinkedIn automation
- Client-facing portal for onboarding
- Light theme
- Multi-user operator permissions

---

## 13. References

| Asset | Path |
|-------|------|
| Prospector scoring | `src/lab_scheduler/rsi/prospector.py` |
| Regional facilities | `data/rsi/regional_facilities.csv` |
| Revenue-growth agent | `.cursor/agents/revenue-growth.md` |
| Landing visual baseline | `deploy/landing.html` |
| Tenant seed pattern | `sql/seed_southbridge_tenant.sql` |
| Value dashboard metrics | `src/lab_scheduler/rsi/value_dashboard.py` |
| Business audit | `docs/BUSINESS_CODEBASE_AUDIT.md` |
| Manager production app | `scripts/manager_app.py` |

---

*Design spec for Port Optical Business section — preview-first outbound, proceed-to-tenant client conversion, Prospector-powered auto-gather.*
