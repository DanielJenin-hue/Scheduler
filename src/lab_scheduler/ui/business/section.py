"""Business section shell — Pipeline, Prospects, Inbox, Email Preview, Client Onboarding."""

from __future__ import annotations

import html
import sqlite3
from typing import List, Optional

import streamlit as st

from lab_scheduler.business.email_templates import OUTBOUND_REPLY_TO_NOTES
from lab_scheduler.business.inbound_email import (
    active_conversation_count,
    ensure_business_inbound_schema,
    prospect_ids_with_inbound,
)
from lab_scheduler.business.discovery import DEFAULT_FACILITY_DATASET, EXCLUDED_FACILITY_IDS
from lab_scheduler.business.models import Prospect, ProspectStatus, ensure_business_prospects_schema
from lab_scheduler.business.prospect_service import (
    ProspectServiceError,
    discover_and_persist_manitoba,
    generate_email_preview,
    get_prospect,
    list_prospects,
    proceed_with_client,
    update_prospect,
    update_prospect_status,
)
from lab_scheduler.ui.business.components import (
    MANAGED_BLOCK_CAD,
    MRR_TARGET_CAD,
    PRO_SEAT_MRR_CAD,
    render_empty_state,
    render_hero,
    render_metric_tiles,
    render_mrr_target_progress,
    render_pipeline_summary,
    render_prospect_card,
    render_prospect_card_html,
    render_email_envelope_preview,
    render_html,
    render_revenue_path,
    render_status_badge,
)
from lab_scheduler.ui.business.helpers import (
    DEFAULT_EMAIL_BODY_TEMPLATE,
    DEFAULT_EMAIL_SUBJECT_TEMPLATE,
    DEFAULT_ONBOARDING_TASKS,
    FIRST_TOUCH_SUBJECT_VARIANT_LABELS,
    blocked_honesty_phrases,
    build_template_context,
    default_outreach_sender_name,
    derive_pitch_angle,
    first_touch_subject,
    load_facility_enrichment,
    load_onboarding_tasks,
    inbound_reply_to_address,
    mailto_link,
    merge_template_variables,
    save_onboarding_tasks,
    validate_first_touch_draft,
)
from lab_scheduler.ui.business.inbox import render_inbox_tab, unread_inbox_badge_label
from lab_scheduler.ui.business.navigation import (
    BUSINESS_TABS,
    apply_pending_business_tab,
    request_business_tab,
)
from lab_scheduler.ui.business.theme import inject_business_theme_css

__all__ = ["render_business_section"]

_PIPELINE_COLUMNS = (
    (ProspectStatus.DISCOVERED, "New"),
    (ProspectStatus.PREVIEWED, "Previewed"),
    (ProspectStatus.CONTACTED, "Previewed"),
    ("replied", "Replied"),
    (ProspectStatus.ACTIVE_CLIENT, "Active Client"),
    (ProspectStatus.DECLINED, "Passed"),
)

_TEMPLATE_OPTIONS = (
    "First touch — managed service",
    "First touch — trial SaaS",
    "Follow-up #2",
    "LinkedIn connection note",
)


def _toast(message: str) -> None:
    st.session_state["business_toast"] = message


def _show_toast() -> None:
    message = st.session_state.pop("business_toast", None)
    if message:
        st.toast(message)


def _run_auto_gather(conn: sqlite3.Connection) -> None:
    with st.spinner("Scanning Manitoba facilities…"):
        result = discover_and_persist_manitoba(conn, skip_existing=True)
    if result.created:
        next_step = "Preview email on your highest-ICP prospect."
    elif result.skipped:
        next_step = "Pipeline already populated — open your top target for preview."
    else:
        next_step = "No facilities matched — check regional_facilities.csv."
    _toast(
        f"Gather complete · {result.created} new, {result.skipped} skipped · {next_step}"
    )
    st.session_state["business_gather_summary"] = {
        "created": result.created,
        "skipped": result.skipped,
        "total": len(result.prospects),
    }
    request_business_tab(st.session_state, "Prospects")
    st.rerun()


def _pipeline_counts(prospects: List[Prospect], replied_ids: set[str]) -> dict[str, int]:
    return {
        "new": sum(1 for p in prospects if p.status == ProspectStatus.DISCOVERED),
        "preview": sum(
            1
            for p in prospects
            if p.status in {ProspectStatus.PREVIEWED, ProspectStatus.CONTACTED}
            and p.id not in replied_ids
        ),
        "replied": sum(
            1
            for p in prospects
            if p.id in replied_ids
            and p.status not in {ProspectStatus.ACTIVE_CLIENT, ProspectStatus.DECLINED}
        ),
        "active": sum(1 for p in prospects if p.status == ProspectStatus.ACTIVE_CLIENT),
        "passed": sum(1 for p in prospects if p.status == ProspectStatus.DECLINED),
    }


def _revenue_path_step(counts: dict[str, int]) -> int:
    if counts["active"] > 0:
        return 3
    if counts["replied"] > 0 or counts["preview"] > 0:
        return 3
    if counts["new"] > 0:
        return 2
    return 1


def _pipeline_items_for_column(
    prospects: List[Prospect],
    column_key: object,
    replied_ids: set[str],
) -> List[Prospect]:
    if column_key == "replied":
        return [
            p
            for p in prospects
            if p.id in replied_ids
            and p.status not in {ProspectStatus.ACTIVE_CLIENT, ProspectStatus.DECLINED}
        ]
    if column_key == ProspectStatus.DECLINED:
        return [p for p in prospects if p.status == column_key]
    if column_key == ProspectStatus.PREVIEWED:
        return [
            p
            for p in prospects
            if p.status in {ProspectStatus.PREVIEWED, ProspectStatus.CONTACTED}
            and p.id not in replied_ids
        ]
    return [p for p in prospects if p.status == column_key]


def _current_mrr_cad(prospects: List[Prospect]) -> int:
    active = sum(1 for p in prospects if p.status == ProspectStatus.ACTIVE_CLIENT)
    return active * PRO_SEAT_MRR_CAD


def _render_business_landing(conn: sqlite3.Connection, prospects: List[Prospect]) -> None:
    replied_ids = prospect_ids_with_inbound(conn)
    counts = _pipeline_counts(prospects, replied_ids)
    render_mrr_target_progress(
        current_mrr_cad=_current_mrr_cad(prospects),
        target_cad=MRR_TARGET_CAD,
        active_conversations=active_conversation_count(conn),
    )
    render_pipeline_summary(
        new_count=counts["new"],
        preview_count=counts["preview"] + counts["replied"],
        active_count=counts["active"],
        passed_count=counts["passed"],
    )
    render_revenue_path(active_step=_revenue_path_step(counts))

    if counts["new"] + counts["preview"] + counts["active"] == 0:
        st.markdown("#### Start here")
        if st.button(
            "Gather prospects",
            key="landing_gather_prospects",
            type="primary",
            use_container_width=True,
        ):
            _run_auto_gather(conn)


def _clear_stale_email_preview_prospect(conn: sqlite3.Connection) -> None:
    prospect_id = st.session_state.get("business_prospect_id")
    if not prospect_id:
        return
    try:
        prospect = get_prospect(conn, str(prospect_id))
    except ProspectServiceError:
        st.session_state.pop("business_prospect_id", None)
        return
    if (
        prospect.facility_id in EXCLUDED_FACILITY_IDS
        or prospect.facility.startswith("Portage Regional")
    ):
        st.session_state.pop("business_prospect_id", None)


def _top_icp_prospect(prospects: List[Prospect]) -> Optional[Prospect]:
    candidates = [
        p
        for p in prospects
        if p.status not in {ProspectStatus.DECLINED, ProspectStatus.ACTIVE_CLIENT}
        and p.facility_id not in EXCLUDED_FACILITY_IDS
        and not p.facility.startswith("Portage Regional")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.icp_score)


def _pass_prospect(conn: sqlite3.Connection, prospect: Prospect) -> None:
    update_prospect_status(conn, prospect.id, ProspectStatus.DECLINED)
    _toast(f"Passed · {prospect.facility}")
    st.rerun()


def _open_preview(conn: sqlite3.Connection, prospect_id: str) -> None:
    try:
        facility = get_prospect(conn, prospect_id).facility
    except ProspectServiceError:
        facility = "prospect"
    st.session_state["business_prospect_id"] = prospect_id
    request_business_tab(st.session_state, "Email Preview")
    _toast(f"Email preview · {facility} — review draft before sending")
    st.rerun()


def _render_pipeline_tab(conn: sqlite3.Connection, prospects: List[Prospect]) -> None:
    replied_ids = prospect_ids_with_inbound(conn)
    active = sum(1 for p in prospects if p.status == ProspectStatus.ACTIVE_CLIENT)
    in_preview = sum(
        1
        for p in prospects
        if p.status in {ProspectStatus.PREVIEWED, ProspectStatus.CONTACTED}
        and p.id not in replied_ids
    )
    in_replied = sum(
        1
        for p in prospects
        if p.id in replied_ids
        and p.status not in {ProspectStatus.ACTIVE_CLIENT, ProspectStatus.DECLINED}
    )
    top = _top_icp_prospect(prospects)
    mrr = f"${299 * active:,}/mo" if active else "$299/mo"
    render_metric_tiles(
        mrr_label=mrr,
        active_clients=active,
        in_preview=in_preview + in_replied,
        top_target=top.facility if top else "—",
    )

    if not prospects:
        if render_empty_state(
            icon="▢▢▢",
            headline="Your pipeline is clear",
            body="Run a prospect scan to fill the New column with Manitoba hospital labs.",
            cta_label="Gather prospects",
            cta_key="pipeline_gather_empty",
        ):
            _run_auto_gather(conn)
        return

    _, gather_col = st.columns([3, 1])
    with gather_col:
        if st.button("Gather prospects", key="pipeline_gather", type="primary", use_container_width=True):
            _run_auto_gather(conn)

    visible_columns: list[tuple[str, List[Prospect]]] = []
    for status, label in _PIPELINE_COLUMNS:
        if status == ProspectStatus.DECLINED:
            items = _pipeline_items_for_column(prospects, status, replied_ids)
            if not items:
                continue
        else:
            items = _pipeline_items_for_column(prospects, status, replied_ids)
            if status == ProspectStatus.CONTACTED:
                continue
        visible_columns.append((label, items))

    if visible_columns:
        kanban_cols = st.columns(len(visible_columns))
        for col, (label, items) in zip(kanban_cols, visible_columns):
            with col:
                render_html(
                    f"""
                    <div class="biz-kanban-col">
                      <p class="biz-kanban-header">{html.escape(label)} ({len(items)})</p>
                    </div>
                    """
                )
                if not items:
                    render_html(
                        '<p style="color:var(--biz-muted);font-size:0.8125rem;">Empty</p>'
                    )
                for prospect in items[:6]:
                    enrichment = load_facility_enrichment(prospect)
                    render_html(render_prospect_card_html(prospect, enrichment, compact=True))

    st.markdown("##### Quick actions")
    for prospect in prospects[:8]:
        if prospect.status in {ProspectStatus.DECLINED, ProspectStatus.ACTIVE_CLIENT}:
            if prospect.status == ProspectStatus.ACTIVE_CLIENT and prospect.tenant_id:
                if st.button(
                    f"Open onboarding · {prospect.facility}",
                    key=f"pipe_onboard_{prospect.id}",
                ):
                    st.session_state["business_onboarding_tenant_id"] = prospect.tenant_id
                    request_business_tab(st.session_state, "Client Onboarding")
                    st.rerun()
            continue
        enrichment = load_facility_enrichment(prospect)
        render_prospect_card(
            prospect,
            enrichment,
            key_prefix="pipeline",
            on_preview=lambda pid=prospect.id: _open_preview(conn, pid),
            on_pass=lambda p=prospect: _pass_prospect(conn, p),
        )


def _render_prospects_tab(conn: sqlite3.Connection, prospects: List[Prospect]) -> None:
    gather_summary = st.session_state.pop("business_gather_summary", None)
    if gather_summary:
        created = int(gather_summary.get("created", 0))
        skipped = int(gather_summary.get("skipped", 0))
        st.success(
            f"**{created}** new prospect{'s' if created != 1 else ''} added "
            f"({skipped} already in pipeline). **Next:** click **Preview email** on your highest-ICP card."
        )

    toolbar_left, toolbar_right = st.columns([2, 1])
    with toolbar_right:
        if st.button("Gather prospects", key="prospects_gather", type="primary", use_container_width=True):
            _run_auto_gather(conn)

    with toolbar_left:
        filter_col, status_col, sort_col = st.columns(3)
        with filter_col:
            province = st.selectbox("Province", ["MB", "All"], key="biz_filter_province")
        with status_col:
            status_filter = st.selectbox(
                "Status",
                ["All statuses", "New", "Previewed", "Active Client", "Passed"],
                key="biz_filter_status",
            )
        with sort_col:
            sort_by = st.selectbox(
                "Sort",
                ["ICP score", "Facility name", "Recently updated"],
                key="biz_sort",
            )

    filtered = list(prospects)
    if province != "All":
        filtered = [p for p in filtered if p.province == province]
    status_map = {
        "New": {ProspectStatus.DISCOVERED},
        "Previewed": {ProspectStatus.PREVIEWED, ProspectStatus.CONTACTED},
        "Active Client": {ProspectStatus.ACTIVE_CLIENT},
        "Passed": {ProspectStatus.DECLINED},
    }
    if status_filter != "All statuses":
        filtered = [p for p in filtered if p.status in status_map[status_filter]]
    if sort_by == "Facility name":
        filtered.sort(key=lambda p: p.facility)
    elif sort_by == "Recently updated":
        filtered.sort(key=lambda p: p.updated_at or p.created_at, reverse=True)
    else:
        filtered.sort(key=lambda p: (-p.icp_score, p.facility))

    if not filtered:
        if render_empty_state(
            icon="◎",
            headline="No prospects in queue",
            body="Import from regional_facilities.csv or run the weekly Prospector scan.",
            cta_label="Run auto-gather",
            cta_key="prospects_gather_empty",
        ):
            _run_auto_gather(conn)
        return

    for idx in range(0, len(filtered), 3):
        row = filtered[idx : idx + 3]
        cols = st.columns(3)
        for col, prospect in zip(cols, row):
            with col:
                enrichment = load_facility_enrichment(prospect)
                render_prospect_card(
                    prospect,
                    enrichment,
                    key_prefix=f"grid_{idx}",
                    on_preview=lambda pid=prospect.id: _open_preview(conn, pid),
                    on_pass=lambda p=prospect: _pass_prospect(conn, p),
                )


def _template_body(template: str, context: dict[str, str]) -> str:
    if template == "First touch — managed service":
        raw = DEFAULT_EMAIL_BODY_TEMPLATE
    elif template == "First touch — trial SaaS":
        raw = """Hi {{first_name}},

{{pain_opener}}

Start a 14-day trial: {{trial_link}}

{{proof_paragraph}}

{{cta_line}}

Best regards,
{{sender_name}}"""
    elif template == "Follow-up #2":
        raw = """Hi {{first_name}},

Follow-up on {{facility_short_name}} — rotation compliance before breakroom posting.

{{solution_paragraph}}

{{sender_name}}"""
    elif template == "LinkedIn connection note":
        raw = (
            "Hi {{first_name}} — breakroom-ready scheduling for {{facility_short_name}}. "
            "{{pitch_angle}} Open to connect?"
        )
        return merge_template_variables(raw, context)[:300]
    else:
        raw = DEFAULT_EMAIL_BODY_TEMPLATE
    return merge_template_variables(raw, context)


def _render_email_preview_tab(conn: sqlite3.Connection) -> None:
    prospect_id = st.session_state.get("business_prospect_id")
    if not prospect_id:
        if render_empty_state(
            icon="✉",
            headline="Select a prospect to preview",
            body="Open any card in Prospects and click Preview email.",
            cta_label="Go to Prospects",
            cta_key="preview_go_prospects",
        ):
            request_business_tab(st.session_state, "Prospects")
            st.rerun()
        return

    try:
        prospect = get_prospect(conn, str(prospect_id))
    except ProspectServiceError:
        st.error("Prospect not found — it may have been removed from the pipeline.")
        if st.button("Back to Prospects", key="preview_missing_back"):
            st.session_state.pop("business_prospect_id", None)
            request_business_tab(st.session_state, "Prospects")
            st.rerun()
        return

    enrichment = load_facility_enrichment(prospect)
    sender_name = st.session_state.get("biz_sender_name", default_outreach_sender_name())
    pitch_angle = derive_pitch_angle(prospect, enrichment)
    context = build_template_context(
        prospect,
        enrichment,
        sender_name=sender_name,
        pitch_angle=pitch_angle,
    )

    header_col, badge_col = st.columns([4, 1])
    with header_col:
        if st.button("← Back to Prospects", key="preview_back"):
            request_business_tab(st.session_state, "Prospects")
            st.rerun()
        st.markdown(f"### {prospect.facility}")
        st.caption(
            f"Outreach draft for **{prospect.facility}** · facility-specific claims only · "
            "nothing is sent until you copy or open your mail client"
        )
    with badge_col:
        render_status_badge(prospect.status)

    preview_flag = f"preview_marked_{prospect.id}"
    if prospect.status == ProspectStatus.DISCOVERED and not st.session_state.get(preview_flag):
        update_prospect_status(conn, prospect.id, ProspectStatus.PREVIEWED)
        st.session_state[preview_flag] = True
        prospect = get_prospect(conn, prospect.id)

    if not prospect.email_draft_body:
        draft = generate_email_preview(conn, prospect.id, sender_name=sender_name, mark_previewed=True)
        prospect = get_prospect(conn, prospect.id)
        default_subject = draft.subject
        default_body = draft.body
    else:
        default_subject = prospect.email_draft_subject or merge_template_variables(
            DEFAULT_EMAIL_SUBJECT_TEMPLATE, context
        )
        default_body = prospect.email_draft_body

    context_col, email_col = st.columns([1, 2])
    with context_col:
        st.markdown("**Context**")
        render_html(render_prospect_card_html(prospect, enrichment, compact=True))
        pitch_angle = st.text_input("Pitch angle", value=pitch_angle, key=f"pitch_{prospect.id}")
        context["pitch_angle"] = pitch_angle
        template_label = st.selectbox("Template", _TEMPLATE_OPTIONS, key=f"template_{prospect.id}")
        subject_variant_label = st.selectbox(
            "Subject variant (psych brief A/B/C)",
            list(FIRST_TOUCH_SUBJECT_VARIANT_LABELS),
            key=f"subject_variant_{prospect.id}",
        )
        if st.button("Apply subject variant", key=f"apply_subject_{prospect.id}"):
            variant_key = FIRST_TOUCH_SUBJECT_VARIANT_LABELS[subject_variant_label]
            st.session_state[f"draft_subject_{prospect.id}"] = first_touch_subject(
                facility_name=prospect.facility,
                variant=variant_key,
            )
            st.rerun()
        st.caption("Channel: Email · LinkedIn")
        if st.button("Regenerate draft", key=f"regen_{prospect.id}"):
            draft = generate_email_preview(conn, prospect.id, sender_name=sender_name)
            st.session_state[f"draft_subject_{prospect.id}"] = draft.subject
            st.session_state[f"draft_body_{prospect.id}"] = draft.body
            if template_label != "First touch — managed service":
                st.session_state[f"draft_body_{prospect.id}"] = _template_body(
                    template_label, context
                )
            st.rerun()

    with email_col:
        to_email = st.text_input(
            "To",
            value=prospect.email or "",
            placeholder="Add lab manager email",
            key=f"preview_email_{prospect.id}",
        )
        subject = st.text_input(
            "Subject",
            value=st.session_state.get(f"draft_subject_{prospect.id}", default_subject),
            key=f"preview_subject_{prospect.id}",
        )
        if len(subject) > 60:
            st.caption(f"⚠ {len(subject)} characters — consider shortening")
        body = st.text_area(
            "Email body",
            value=st.session_state.get(f"draft_body_{prospect.id}", default_body),
            height=320,
            key=f"preview_body_{prospect.id}",
        )
        preview_body = body.strip() or _template_body(template_label, context)

        blocked = blocked_honesty_phrases(preview_body)
        if blocked:
            st.warning(f"Honesty check: avoid unverified claims — {', '.join(blocked)}")

        for warning in validate_first_touch_draft(preview_body, subject):
            st.warning(f"Draft quality: {warning}")

        st.markdown("**Mail client preview**")
        st.caption("What you copy or open in your mail app — edits above are reflected here.")
        render_email_envelope_preview(to=to_email, subject=subject, body=preview_body)

        st.caption(OUTBOUND_REPLY_TO_NOTES)
        reply_to = inbound_reply_to_address()
        if reply_to:
            st.caption(f"Reply-To for this send: **{reply_to}** (replies sync to Inbox)")

        action_left, action_mid, action_right = st.columns(3)
        with action_left:
            if st.button("Copy to clipboard", key=f"copy_{prospect.id}"):
                st.code(f"Subject: {subject}\n\n{preview_body}", language=None)
                _toast("Copy subject + body from the code block above")
        with action_mid:
            if to_email.strip():
                st.link_button(
                    "Open in mail client",
                    mailto_link(
                        email=to_email.strip(),
                        subject=subject,
                        body=preview_body,
                        reply_to=reply_to,
                    ),
                    use_container_width=True,
                )
            else:
                st.button("Open in mail client", disabled=True, use_container_width=True)
        with action_right:
            if st.button("Save draft", key=f"save_draft_{prospect.id}"):
                update_prospect(
                    conn,
                    prospect.id,
                    email=to_email.strip() or prospect.email,
                    email_draft_subject=subject,
                    email_draft_body=preview_body,
                    status=ProspectStatus.PREVIEWED,
                )
                _toast("Draft saved — nothing sent")
                st.rerun()

    st.divider()
    proceed_col, pass_col = st.columns([2, 1])
    with proceed_col:
        if st.button("Proceed with client ▶", key=f"proceed_{prospect.id}", type="primary"):
            st.session_state[f"business_proceed_{prospect.id}"] = True
    with pass_col:
        if st.button("Pass", key=f"preview_pass_{prospect.id}"):
            _pass_prospect(conn, prospect)

    if st.session_state.get(f"business_proceed_{prospect.id}"):
        render_html(
            f"""
            <div class="biz-confirm-box">
              <strong>Proceed with {html.escape(prospect.facility)}?</strong>
              <p style="color:var(--biz-muted);margin:8px 0 0;">
                Revenue path: <strong>${MANAGED_BLOCK_CAD:,} CAD</strong> managed block, then
                <strong>${PRO_SEAT_MRR_CAD}/mo</strong> Pro SaaS — stack toward $2,000 MRR.
              </p>
              <p style="color:var(--biz-muted);margin:8px 0 0;">
                Creates a tenant workspace, marks Active Client, opens onboarding.
                Email is <strong>not</strong> sent automatically.
              </p>
            </div>
            """
        )
        st.radio("Engagement type", ["Managed first", "Trial SaaS"], horizontal=True, key=f"engage_{prospect.id}")
        slug_hint = prospect.facility.lower().replace(" ", "-")[:32]
        st.text_input("Tenant slug", value=slug_hint, key=f"slug_{prospect.id}")
        confirm_col, cancel_col = st.columns(2)
        with cancel_col:
            if st.button("Cancel", key=f"proceed_cancel_{prospect.id}"):
                st.session_state.pop(f"business_proceed_{prospect.id}", None)
                st.rerun()
        with confirm_col:
            if st.button("Proceed with client", key=f"proceed_confirm_{prospect.id}", type="primary"):
                update_prospect(
                    conn,
                    prospect.id,
                    email_draft_subject=subject,
                    email_draft_body=preview_body,
                )
                try:
                    result = proceed_with_client(conn, prospect.id, create_tenant=True)
                except ProspectServiceError as exc:
                    st.error(str(exc))
                else:
                    st.session_state.pop(f"business_proceed_{prospect.id}", None)
                    st.session_state["business_onboarding_tenant_id"] = result.tenant_id
                    request_business_tab(st.session_state, "Client Onboarding")
                    _toast(
                        f"Client active · {prospect.facility} · ${MANAGED_BLOCK_CAD:,} block path opened in onboarding"
                    )
                    st.rerun()


def _render_onboarding_tab(conn: sqlite3.Connection, prospects: List[Prospect]) -> None:
    active = [p for p in prospects if p.status == ProspectStatus.ACTIVE_CLIENT]
    tenant_id = st.session_state.get("business_onboarding_tenant_id")
    selected: Optional[Prospect] = None

    if tenant_id:
        for prospect in active:
            if prospect.tenant_id == tenant_id:
                selected = prospect
                break
    if selected is None and active:
        selected = active[0]

    if not selected or not selected.tenant_id:
        if render_empty_state(
            icon="☑",
            headline="No clients onboarding",
            body="When you proceed with a prospect, their setup checklist appears here.",
            cta_label="View Pipeline",
            cta_key="onboarding_go_pipeline",
        ):
            request_business_tab(st.session_state, "Pipeline")
            st.rerun()
        return

    tenant_row = conn.execute(
        "SELECT name, slug FROM tenants WHERE id = ?",
        (selected.tenant_id,),
    ).fetchone()
    slug = tenant_row[1] if tenant_row else selected.tenant_id

    st.markdown(f"### Client Onboarding — {selected.facility}")
    st.caption(f"{slug} · Managed first")
    render_status_badge(selected.status)

    tasks = load_onboarding_tasks(conn, selected.tenant_id)
    completed = sum(1 for value in tasks.values() if value)
    total = len(DEFAULT_ONBOARDING_TASKS)
    st.progress(completed / total if total else 0, text=f"Setup progress · {completed}/{total} complete")

    for task_id, label in DEFAULT_ONBOARDING_TASKS:
        row_left, row_mid = st.columns([0.4, 4])
        done = tasks.get(task_id, False)
        with row_left:
            checked = st.checkbox(
                "done",
                value=done,
                key=f"onboard_{selected.tenant_id}_{task_id}",
                label_visibility="collapsed",
            )
            tasks[task_id] = checked
        with row_mid:
            suffix = " · Done automatically" if task_id == "create_tenant" and checked else ""
            st.markdown(f"{'☑' if checked else '☐'} **{label}**{suffix}")

    save_onboarding_tasks(conn, selected.tenant_id, tasks)

    if selected.email_draft_subject:
        with st.expander("Email snapshot", expanded=False):
            st.markdown(f"**Subject:** {selected.email_draft_subject}")
            st.text(selected.email_draft_body or "")
            if st.button("View in Email Preview", key="onboard_view_draft"):
                st.session_state["business_prospect_id"] = selected.id
                request_business_tab(st.session_state, "Email Preview")
                _toast(f"Opening saved draft · {selected.facility}")
                st.rerun()

    notes = st.text_area("Notes", value=selected.notes or "", key=f"onboard_notes_{selected.id}", height=100)
    if notes != (selected.notes or ""):
        update_prospect(conn, selected.id, notes=notes)


def render_business_section(conn: sqlite3.Connection) -> None:
    """Render the operator Business section (five tabs)."""

    ensure_business_prospects_schema(conn)
    ensure_business_inbound_schema(conn)
    inject_business_theme_css()
    st.session_state.setdefault("biz_sender_name", default_outreach_sender_name())
    _clear_stale_email_preview_prospect(conn)
    apply_pending_business_tab(st.session_state)
    _show_toast()

    st.markdown('<div class="biz-shell">', unsafe_allow_html=True)
    render_hero()

    prospects = list_prospects(conn)
    _render_business_landing(conn, prospects)

    def _tab_display_label(tab: str) -> str:
        if tab == "Inbox":
            return unread_inbox_badge_label(conn)
        return tab

    selected_tab = st.radio(
        "Business section",
        list(BUSINESS_TABS),
        horizontal=True,
        label_visibility="collapsed",
        key="business_tab",
        format_func=_tab_display_label,
    )

    if selected_tab == "Pipeline":
        _render_pipeline_tab(conn, prospects)
    elif selected_tab == "Prospects":
        _render_prospects_tab(conn, prospects)
    elif selected_tab == "Inbox":
        render_inbox_tab(conn)
    elif selected_tab == "Email Preview":
        _render_email_preview_tab(conn)
    elif selected_tab == "Client Onboarding":
        _render_onboarding_tab(conn, prospects)

    st.markdown("</div>", unsafe_allow_html=True)
