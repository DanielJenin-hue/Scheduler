"""Email preview tab — full draft review before any send."""

from __future__ import annotations

import sqlite3
from typing import Callable

import streamlit as st

from lab_scheduler.business.models import ProspectStatus
from lab_scheduler.business.prospect_service import (
    ProspectServiceError,
    generate_email_preview,
    get_prospect,
    proceed_with_client,
    update_prospect,
)
from lab_scheduler.ui.business.components import (
    MANAGED_BLOCK_CAD,
    PRO_SEAT_MRR_CAD,
    render_email_envelope_preview,
    render_empty_state,
    render_status_badge,
)
from lab_scheduler.ui.business.helpers import (
    DEFAULT_EMAIL_BODY_TEMPLATE,
    DEFAULT_EMAIL_SUBJECT_TEMPLATE,
    blocked_honesty_phrases,
    build_template_context,
    derive_pitch_angle,
    icp_band,
    icp_display_score,
    load_facility_enrichment,
    mailto_link,
    merge_template_variables,
)

__all__ = ["render_email_preview_tab"]

TEMPLATE_OPTIONS = {
    "First touch — managed service": "managed",
    "First touch — trial SaaS": "trial",
    "Follow-up #2": "followup",
    "LinkedIn connection note": "linkedin",
}


def _managed_body(context: dict[str, str]) -> str:
    return merge_template_variables(DEFAULT_EMAIL_BODY_TEMPLATE, context)


def _trial_body(context: dict[str, str]) -> str:
    return merge_template_variables(
        """Hi {{first_name}},

{{pain_opener}}

We built Lab Staffing Scheduler for Manitoba hospital labs — a 14-day trial lets you run Auto-Pilot on a Portage-style demo roster and export breakroom HTML in one session.

{{proof_paragraph}}

Start here: {{trial_link}}

{{cta_line}}

Best regards,
{{sender_name}}""",
        context,
    )


def _followup_body(context: dict[str, str]) -> str:
    return merge_template_variables(
        """Hi {{first_name}},

Quick follow-up on {{facility_short_name}} — rotation compliance and footer coverage are usually where manual schedules break before breakroom posting.

{{solution_paragraph}}

Happy to show a 15-minute walkthrough on your roster shape ({{mlt_mla_summary}}).

{{sender_name}}""",
        context,
    )


def _linkedin_note(context: dict[str, str]) -> str:
    text = merge_template_variables(
        "Hi {{first_name}} — I work on breakroom-ready lab scheduling for Manitoba hospitals "
        "({{facility_short_name}}). {{pitch_angle}} Open to connect?",
        context,
    )
    return text[:300]


def render_email_preview_tab(
    conn: sqlite3.Connection,
    *,
    on_back: Callable[[], None],
    on_proceed_complete: Callable[[str], None],
    on_go_prospects: Callable[[], None],
) -> None:
    prospect_id = st.session_state.get("business_prospect_id")
    if not prospect_id:
        if render_empty_state(
            icon="✉",
            headline="Select a prospect to preview",
            body="Open any card in Prospects and click Preview email.",
            cta_label="Go to Prospects",
            cta_key="email_empty_go_prospects",
        ):
            on_go_prospects()
        return

    try:
        prospect = get_prospect(conn, prospect_id)
    except ProspectServiceError:
        st.error("Prospect not found.")
        if st.button("Back to Prospects", key="email_missing_back"):
            on_go_prospects()
        return

    enrichment = load_facility_enrichment(prospect)
    sender_name = st.session_state.get("biz_sender_name", "Dan — Portage Lab Staffing")

    header_left, header_mid, header_right = st.columns([1, 3, 1])
    with header_left:
        if st.button("← Back to Prospects", key="email_back"):
            on_back()
    with header_mid:
        st.markdown(f"### {prospect.facility}")
    with header_right:
        render_status_badge(prospect.status)

    display_icp, max_icp = icp_display_score(prospect.icp_score)
    band_label, _ = icp_band(display_icp)
    pitch_angle = derive_pitch_angle(prospect, enrichment)
    context = build_template_context(
        prospect,
        enrichment,
        sender_name=sender_name,
        pitch_angle=pitch_angle,
    )

    if prospect.email_draft_subject and prospect.email_draft_body:
        default_subject = prospect.email_draft_subject
        default_body = prospect.email_draft_body
    else:
        default_subject = merge_template_variables(DEFAULT_EMAIL_SUBJECT_TEMPLATE, context)
        default_body = merge_template_variables(DEFAULT_EMAIL_BODY_TEMPLATE, context)

    context_col, email_col = st.columns([1, 2])

    with context_col:
        st.markdown("**Context**")
        st.markdown(
            f'<span class="biz-mono">ICP {display_icp}/{max_icp}</span> · {band_label}',
            unsafe_allow_html=True,
        )
        st.caption(f"Contact: {prospect.contact_name or 'Lab Manager'}")
        if prospect.email:
            st.caption(prospect.email)
        if prospect.pain_signals:
            for signal in prospect.pain_signals[:4]:
                st.markdown(f'<span class="biz-pain-tag">{signal[:100]}</span>', unsafe_allow_html=True)

        pitch_angle = st.text_input("Pitch angle", value=pitch_angle, key="biz_pitch_angle")
        context["pitch_angle"] = pitch_angle

        template_label = st.selectbox(
            "Template",
            list(TEMPLATE_OPTIONS),
            key="biz_template_select",
        )
        st.caption("Channel: Email · LinkedIn")

        if st.button("Regenerate draft", key="biz_regenerate"):
            if st.session_state.get("biz_confirm_regenerate"):
                draft = generate_email_preview(conn, prospect_id, sender_name=sender_name)
                st.session_state["biz_email_subject"] = draft.subject
                template_key = TEMPLATE_OPTIONS[template_label]
                if template_key == "managed":
                    regenerated_body = _managed_body(context)
                elif template_key == "trial":
                    regenerated_body = _trial_body(context)
                elif template_key == "followup":
                    regenerated_body = _followup_body(context)
                else:
                    regenerated_body = _linkedin_note(context)
                st.session_state["biz_email_body"] = regenerated_body
                st.session_state["biz_email_customized"] = False
                st.session_state.pop("biz_confirm_regenerate", None)
                st.toast("Draft regenerated from prospect data")
                st.rerun()
            else:
                st.session_state["biz_confirm_regenerate"] = True
                st.warning("This will overwrite manual edits. Click Regenerate draft again to confirm.")

    with email_col:
        to_email = st.text_input(
            "To",
            value=prospect.email or "",
            placeholder="lab.manager@example.com",
            key="biz_email_to",
        )
        subject = st.text_input(
            "Subject",
            value=st.session_state.get("biz_email_subject", default_subject),
            key="biz_email_subject_input",
        )
        subject_len = len(subject)
        if subject_len > 60:
            st.caption(f"⚠ {subject_len} characters — consider shortening for mobile")
        else:
            st.caption(f"{subject_len} characters")

        body = st.text_area(
            "Email body",
            value=st.session_state.get("biz_email_body", default_body),
            height=360,
            key="biz_email_body_input",
        )
        st.session_state["biz_email_subject"] = subject
        st.session_state["biz_email_body"] = body
        preview_body = body.strip() or _managed_body(context)

        if st.session_state.get("biz_email_customized"):
            st.markdown('<span class="biz-chip">Customized</span>', unsafe_allow_html=True)

        blocked = blocked_honesty_phrases(preview_body)
        if blocked:
            st.warning(f"Honesty check: avoid unverified claims — {', '.join(blocked)}")

        st.markdown("**Mail client preview**")
        st.caption("What you copy or open in your mail app — edits above are reflected here.")
        render_email_envelope_preview(to=to_email, subject=subject, body=preview_body)

    action1, action2, action3, action4 = st.columns([1, 1, 1, 1])
    with action1:
        if st.button("Copy to clipboard", key="biz_copy", use_container_width=True):
            payload = f"Subject: {subject}\n\n{preview_body}"
            st.toast("Copied subject + body (use code block below)")
            st.code(payload, language=None)
    with action2:
        mail_disabled = not to_email.strip()
        if st.button(
            "Open in mail client",
            key="biz_mailto",
            disabled=mail_disabled,
            use_container_width=True,
        ):
            link = mailto_link(email=to_email.strip(), subject=subject, body=preview_body)
            st.markdown(f"[Launch mail client]({link})")
        if mail_disabled:
            st.caption("Add recipient email to enable mailto")
    with action3:
        if prospect.status != ProspectStatus.ACTIVE_CLIENT:
            if st.button("Pass", key="biz_pass", use_container_width=True):
                update_prospect(conn, prospect_id, status=ProspectStatus.DECLINED)
                st.toast("Prospect passed")
                on_back()
    with action4:
        if prospect.status != ProspectStatus.ACTIVE_CLIENT:
            if st.button("Proceed with client ▶", type="primary", key="biz_proceed_btn", use_container_width=True):
                st.session_state["biz_proceed_confirm_id"] = prospect_id

    if st.session_state.get("biz_proceed_confirm_id") == prospect_id:
        st.markdown('<div class="biz-confirm-box">', unsafe_allow_html=True)
        st.markdown(f"#### Proceed with {prospect.facility}?")
        st.markdown(
            f"""
            **Revenue path:** ${MANAGED_BLOCK_CAD:,} CAD managed block, then ${PRO_SEAT_MRR_CAD}/mo Pro SaaS — stack toward $2,000 MRR.

            This will:
            - Create a client tenant (draft)
            - Mark prospect as **Active Client** in Pipeline
            - Open Client Onboarding checklist
            - Pre-seed facility metadata from regional dataset
            """
        )
        st.radio(
            "Engagement type",
            ["Managed first", "Trial SaaS"],
            horizontal=True,
            key="biz_engagement_type",
        )
        slug_default = prospect.facility.lower().replace(" ", "-").replace(".", "")[:32]
        st.text_input("Tenant slug", value=slug_default, key="biz_tenant_slug")
        st.caption("Email is **not** sent automatically — copy or open mail client first.")

        cancel_col, confirm_col = st.columns(2)
        with cancel_col:
            if st.button("Cancel", key="biz_proceed_cancel", use_container_width=True):
                st.session_state.pop("biz_proceed_confirm_id", None)
                st.rerun()
        with confirm_col:
            if st.button("Proceed with client", type="primary", key="biz_proceed_confirm", use_container_width=True):
                update_prospect(
                    conn,
                    prospect_id,
                    email_draft_subject=subject,
                    email_draft_body=preview_body,
                    email=to_email.strip() or prospect.email,
                )
                if prospect.status == ProspectStatus.DISCOVERED:
                    update_prospect(conn, prospect_id, status=ProspectStatus.PREVIEWED)
                try:
                    result = proceed_with_client(conn, prospect_id, create_tenant=True)
                except ProspectServiceError as exc:
                    st.error(str(exc))
                else:
                    st.session_state.pop("biz_proceed_confirm_id", None)
                    st.session_state["business_toast"] = "Tenant created · Opening onboarding"
                    on_proceed_complete(result.tenant_id)
        st.markdown("</div>", unsafe_allow_html=True)

    if prospect.status == ProspectStatus.DISCOVERED:
        update_prospect(conn, prospect_id, status=ProspectStatus.PREVIEWED)
