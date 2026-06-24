# First-Touch Psychology Brief — Manitoba Hospital Labs (Batch 1)

**Date:** 2026-06-19 · **Iteration 6** (pricing deferred in first touch — 2026-06-24)  
**Owner:** persuasion-psychology-partner  
**Handoff to:** brand-voice-partner (wording), revenue-growth (target list), customer-relations (reply path)  
**North star:** 5 qualified first-touch mailtos → 1 pilot reply → $800 managed block

---

## Target profile (ICP)

| Field | Spec |
|-------|------|
| **Who** | Lab manager / staffing coordinator at Manitoba hospital lab (15–60 MLT/MLA lines) |
| **Pain (ranked)** | Wall posting from Excel; rotation footer gaps; union OT equity; posting-season crunch |
| **Inbox reality** | 30-second triage; deletes generic SaaS; opens facility-specific + low-commitment asks |
| **Trust bar** | Peer operator tone; no patient data; compliance claims must match RSI PASS evidence |

---

## Primary psychological lever

**Specificity + single low-commitment ask** — lead with *their* facility and a true pain mirror, then one managed-first offer paragraph in **manager-native language** (8-week rotation built from roster lines → rest-rule check → schedule ready to print and post). **One CTA only:** reply with `"yes — [week] works"` and roughly how many MLT/MLA lines they run. Defer sample exports, Pro, and trial to follow-up #2 or the 15-minute walkthrough — not the first touch.

---

## Manager-native language (mandatory)

Cold Manitoba hospital lab managers do **not** know product internals. First-touch copy must read like a peer who has posted wall schedules — not a developer demo.

| Don't say (ban in first touch) | Say instead |
|--------------------------------|-------------|
| breakroom HTML / breakroom-ready HTML export | schedule your staff can print and post on the wall (or share as a link) |
| breakroom grid / breakroom posting | M/E/N schedule staff see before Monday / posting on the wall |
| managed 8-week publish / managed publish | we build your 8-week rotation from your roster lines — you review, then post |
| compliance check / audit-ready schedules | we check it against Manitoba rest rules before you post |
| RSI / RSI PASS | *(omit in first touch — internal gate)* |
| Distribute / Fill / Save | *(omit — internal workflow)* |
| Port Optical / the product | we / lab scheduling for Manitoba hospitals |
| HTML export | print-ready schedule |

**Rules:**
1. **No product jargon in the first three body lines** — opener must be posting season, Excel, M/E/N, wall posting, OT patches.
2. **Managed offer = one plain sentence** — what you do, what they get, no feature stack.
3. **`validate_first_touch_draft`** warns when banned phrases appear without their plain gloss (`FIRST_TOUCH_JARGON_GLOSSARY` in `email_templates.py`).
4. **Discovery `pain_signals`** and **`derive_pitch_angle`** must not inject banned jargon into manager-facing strings.
5. **Operator UI** (Business tab, onboarding checklist) may keep internal labels; **emails and pitch angles** may not.

---

## Subject-line hypotheses (A/B for first 5 sends)

| Variant | Subject | Hypothesis | Ethical guard |
|---------|---------|------------|---------------|
| **A (recommended)** | `{Facility} — staff schedule before posting season?` | Curiosity gap tied to posting-season pain + true deliverable | Body must describe managed offer in plain language; no sample attachment promise in first email |
| **B** | `{Facility} rotation — one question before you post` | Specificity + low-pressure question before seasonal post | No fake deadlines; answer the question in the first two lines |
| **C** | `Quick question — MLT lines at {Facility}` | Specificity + question format boosts open on mobile | Body must answer the implied question in first 2 lines |

**Rule:** Never use fake Re:, prior relationship implication, or urgency fabrication.

---

## Body structure (recommended — first touch)

1. **Greeting** — first name if known  
2. **Posting-season peer opener** (1–2 sentences) — evenings/nights/grid alignment, not vendor pitch  
3. **Pain mirror** — one honest sentence from prospect pain_signals (OT/equity when volume pain is present)  
4. **Managed-first offer** — one paragraph: we build your 8-week rotation from roster lines, check Manitoba rest rules, hand you a print-and-post schedule. **Defer dollar amount** to walkthrough or reply thread; optional toggle in Email Preview if prospect asked about budget upfront.  
5. **Single CTA** — `Reply with "yes — [week] works" and roughly how many MLT/MLA lines you run` (walkthrough times follow on reply)  
6. **Sign-off** — em dash + sender name (Port Optical team)

**Not in first touch:** sample breakroom export offer, Pro self-serve pricing, 14-day trial link, **lead-with price range ($800–1,200)**, extra value-bullet stacks, or secondary CTAs.

---

## Pricing in first touch (iteration 6 — 2026-06-24)

| Question | Answer |
|----------|--------|
| **Is $800–1,200 justified?** | Yes for operator-led work (3–6h roster import, schedule build, rest-rule review, print-ready wall schedule). Not comparable to $299/mo Pro — that's self-serve after they know the product. |
| **Include in cold email?** | **No (default).** Sticker shock before trust/reciprocity kills reply rate. Price belongs on the 15-minute walkthrough or in reply-thread step 5 (billing contact). |
| **When to include** | Prospect explicitly asked about cost; follow-up #2 after they engaged; Email Preview toggle ON for that send only. |
| **Deferred wording** | "Fixed fee once we confirm your line count on a short walkthrough" — not silence on money, just no number before conversation. |
| **Soft framing if included** | "Typically $800–1,200 CAD depending on roster size" + "exact scope and fee after walkthrough" — never bare range as the hook. |

---

## Follow-up #2 (after reply or no response)

Use to deliver deferred reciprocity and upsell paths without cluttering the opener:

- **Sample print-ready schedule** from demo roster (no PHI) — on walkthrough or second email  
- **Pro self-serve** and **14-day trial** — only after managed path is understood or post successful publish  
- Calendar times for 15-minute walkthrough — confirm once they reply with week + line count

---

## Friction removal checklist

- [ ] One primary action only (no trial link + calendar link + attachment in first touch)  
- [ ] No Pro/trial/sample-export language in first-touch body (Email Preview honesty scan + human read)  
- [ ] Subject ≤ 60 characters for mobile  
- [ ] Honesty scan passes (`blocked_honesty_phrases` in Email Preview)  
- [ ] Reply-To set to monitored inbox (`LAB_INBOUND_REPLY_TO`) so replies sync to Business → Inbox  
- [ ] Mailto opened from app — human sends; nothing auto-sent

---

## Reply path (for customer-relations)

When prospect replies, use numbered intake:

1. Confirm MLT/MLA line count and 8-week period start  
2. Roster format (Excel export / PDF / photo of grid — no PHI in email)  
3. Union rules summary (DE/DN, weekend pattern, max consecutive)  
4. Preferred wall-posting date for the schedule  
5. Billing contact for managed block invoice

(Offer sample export and walkthrough scheduling in this thread, not in the cold first touch.)

---

## Success metrics (Week 1)

| Metric | Target |
|--------|--------|
| Mailtos sent | 5 |
| Opens (estimated) | ≥ 3 |
| Replies | ≥ 1 |
| 15-min calls booked | ≥ 1 |
| Managed block invoiced | 0–1 (stretch) |

---

## Agent sign-off criteria (persuasion-psychology-partner)

- [x] Psychology brief artifact exists for batch 1  
- [x] Subject A/B/C selectable in Business → Email Preview (`first_touch_subject` helper)  
- [x] First-touch template aligned to managed-first single CTA (no sample/Pro/trial in opener)  
- [x] First-touch default defers dollar amount; Email Preview toggle for optional pricing  
- [ ] First 5 mailtos sent with Reply-To wired  
- [ ] Reply converted via numbered intake template

*Brief synced with managed-first Email Preview defaults and goal-coordinator dispatch (2026-06-19 iteration 5).*
