"""Reusable Business section UI components."""

from __future__ import annotations

import html
import textwrap
from typing import Callable, Optional

import streamlit as st

from lab_scheduler.business.models import Prospect, ProspectStatus
from lab_scheduler.ui.business.helpers import (
    FacilityEnrichment,
    derive_pitch_angle,
    email_preview_envelope_html,
    format_test_volume,
    icp_band,
    icp_display_score,
    status_badge_class,
    status_label,
)

MRR_TARGET_CAD = 2000
PRO_SEAT_MRR_CAD = 299
MANAGED_BLOCK_CAD = 800


def _biz_html(content: str) -> str:
    """Left-align HTML so Streamlit markdown does not treat it as a code block."""

    return textwrap.dedent(content).strip()


def render_html(content: str) -> None:
    st.markdown(_biz_html(content), unsafe_allow_html=True)


__all__ = [
    "MANAGED_BLOCK_CAD",
    "MRR_TARGET_CAD",
    "PRO_SEAT_MRR_CAD",
    "render_empty_state",
    "render_gather_progress",
    "render_hero",
    "render_icp_score_bar",
    "render_metric_tiles",
    "render_mrr_target_progress",
    "render_pipeline_summary",
    "render_prospect_card",
    "render_prospect_card_html",
    "render_revenue_path",
    "render_email_envelope_preview",
    "render_html",
    "render_status_badge",
    "status_badge_html",
]


def render_hero() -> None:
    st.markdown(
        """
        <div class="biz-hero">
          <p class="biz-hero-kicker">Revenue cockpit · $2,000 CAD/mo north star</p>
          <h1 class="biz-hero-title">Business</h1>
          <p class="biz-hero-sub">Gather prospects → Preview email → Replies in Inbox → Proceed with client</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_mrr_target_progress(
    *,
    current_mrr_cad: int,
    target_cad: int = MRR_TARGET_CAD,
    active_conversations: int = 0,
) -> None:
    safe_current = max(0, int(current_mrr_cad))
    safe_target = max(1, int(target_cad))
    pct = min(100, int(safe_current / safe_target * 100))
    convo_line = ""
    if active_conversations > 0:
        convo_line = (
            f'<p style="color:var(--biz-accent);font-size:0.8125rem;margin:8px 0 0;">'
            f"{active_conversations} active conversation{'s' if active_conversations != 1 else ''} in Inbox</p>"
        )
    st.markdown(
        f"""
        <div class="biz-card biz-mrr-target">
          <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;">
            <div>
              <div class="biz-metric-label">MRR progress</div>
              <div class="biz-metric-value revenue">${safe_current:,} / ${safe_target:,} CAD</div>
            </div>
            <div class="biz-mono" style="color:var(--biz-muted);">{pct}% of goal</div>
          </div>
          <div class="biz-icp-bar" style="margin-top:12px;">
            <div class="biz-icp-fill" style="width:{pct}%;"></div>
          </div>
          <p style="color:var(--biz-muted);font-size:0.8125rem;margin:10px 0 0;">
            Fastest path: managed block (${MANAGED_BLOCK_CAD:,}) then Pro SaaS (${PRO_SEAT_MRR_CAD}/mo).
          </p>
          {convo_line}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_pipeline_summary(
    *,
    new_count: int,
    preview_count: int,
    active_count: int,
    passed_count: int,
) -> None:
    st.markdown(
        f"""
        <div class="biz-metric-grid" style="margin-bottom:12px;">
          <div class="biz-metric-tile">
            <div class="biz-metric-label">New</div>
            <div class="biz-metric-value">{new_count}</div>
          </div>
          <div class="biz-metric-tile">
            <div class="biz-metric-label">Previewed</div>
            <div class="biz-metric-value accent">{preview_count}</div>
          </div>
          <div class="biz-metric-tile">
            <div class="biz-metric-label">Active clients</div>
            <div class="biz-metric-value revenue">{active_count}</div>
          </div>
          <div class="biz-metric-tile">
            <div class="biz-metric-label">Passed</div>
            <div class="biz-metric-value">{passed_count}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_revenue_path(*, active_step: int) -> None:
    """Three-step revenue path using native Streamlit (avoids raw HTML in markdown)."""

    steps = (
        (1, "Gather prospects", "Scan Manitoba hospital labs into your pipeline"),
        (2, "Preview email", f"Managed-first pitch · ${MANAGED_BLOCK_CAD:,} block"),
        (3, "Proceed with client", f"Create tenant · ${PRO_SEAT_MRR_CAD}/mo Pro upsell"),
    )
    cols = st.columns(len(steps))
    for col, (number, title, detail) in zip(cols, steps):
        with col:
            if number < active_step:
                st.success(f"**{number}. {title}**")
            elif number == active_step:
                st.info(f"**{number}. {title}**")
            else:
                st.markdown(f"**{number}. {title}**")
            st.caption(detail)


def render_email_envelope_preview(*, to: str, subject: str, body: str) -> None:
    render_html(email_preview_envelope_html(to=to, subject=subject, body=body))


def status_badge_html(status: ProspectStatus) -> str:
    css = status_badge_class(status)
    label = html.escape(status_label(status))
    return f'<span class="biz-badge {css}">{label}</span>'


def render_status_badge(status: ProspectStatus) -> None:
    st.markdown(status_badge_html(status), unsafe_allow_html=True)


def render_icp_score_bar(prospect: Prospect) -> str:
    display_score, max_score = icp_display_score(prospect.icp_score)
    band_label, _ = icp_band(display_score)
    pct = max(4, min(100, int(display_score / max_score * 100)))
    return _biz_html(
        f"""
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin:8px 0 4px;">
          <span class="biz-mono">ICP {display_score}/{max_score}</span>
          <span style="color:var(--biz-muted);font-size:0.8125rem;">{html.escape(band_label)}</span>
        </div>
        <div class="biz-icp-bar"><div class="biz-icp-fill" style="width:{pct}%;"></div></div>
        """
    )


def render_metric_tiles(
    *,
    mrr_label: str,
    active_clients: int,
    in_preview: int,
    top_target: str,
) -> None:
    target = html.escape(top_target or "—")
    st.markdown(
        f"""
        <div class="biz-metric-grid">
          <div class="biz-metric-tile">
            <div class="biz-metric-label">MRR</div>
            <div class="biz-metric-value revenue">{html.escape(mrr_label)}</div>
          </div>
          <div class="biz-metric-tile">
            <div class="biz-metric-label">Active clients</div>
            <div class="biz-metric-value revenue">{active_clients}</div>
          </div>
          <div class="biz-metric-tile">
            <div class="biz-metric-label">In preview</div>
            <div class="biz-metric-value accent">{in_preview}</div>
          </div>
          <div class="biz-metric-tile">
            <div class="biz-metric-label">Top target</div>
            <div class="biz-metric-value" style="font-size:0.95rem;">{target}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_gather_progress(step: int) -> None:
    steps = [
        "Loaded facilities from regional dataset",
        "Scored viability (prospector)",
        "Queuing for review",
    ]
    pct = min(100, max(15, int(step / len(steps) * 100)))
    rows = []
    for index, label in enumerate(steps):
        if index < step:
            mark = "✓"
        elif index == step:
            mark = "◌"
        else:
            mark = "·"
        rows.append(f"<div style='color:var(--biz-muted);margin:4px 0;'>{mark} {html.escape(label)}</div>")
    st.markdown(
        f"""
        <div class="biz-card">
          <strong>Gathering prospects…</strong>
          <div class="biz-icp-bar" style="margin:12px 0;"><div class="biz-icp-fill" style="width:{pct}%;"></div></div>
          {''.join(rows)}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_prospect_card_html(
    prospect: Prospect,
    enrichment: Optional[FacilityEnrichment],
    *,
    compact: bool = False,
) -> str:
    badge = status_badge_html(prospect.status)
    display_icp, max_icp = icp_display_score(prospect.icp_score)
    band_label, band_class = icp_band(display_icp)
    volume = format_test_volume(enrichment.annual_test_volume) if enrichment else "—"
    region = html.escape(enrichment.region if enrichment else prospect.province)
    province = html.escape(prospect.province or "MB")
    savings = (
        f'<span class="biz-mono revenue">${enrichment.estimated_savings_usd:,.0f}/yr</span>'
        if enrichment
        else ""
    )
    pitch = html.escape(derive_pitch_angle(prospect, enrichment))
    pain_html = ""
    if prospect.pain_signals and not compact:
        pain_html = "".join(
            f'<span class="biz-pain-tag">{html.escape(signal[:80])}</span>'
            for signal in prospect.pain_signals[:3]
        )
    muted = "opacity:0.72;" if display_icp < 10 else ""
    return _biz_html(
        f"""
        <div class="biz-card" style="{muted}">
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start;">
            <div class="biz-card-title">{html.escape(prospect.facility)}</div>
            {badge}
          </div>
          <div class="biz-card-sub">{html.escape(prospect.facility_id or prospect.id)}</div>
          <div class="biz-chip-row">
            <span class="biz-chip">{region} · {province}</span>
            <span class="biz-chip">{volume}</span>
          </div>
          {render_icp_score_bar(prospect)}
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
            <span class="biz-mono">{band_label}</span>
            {savings}
          </div>
          {f'<div style="margin-top:8px;">{pain_html}</div>' if pain_html else ''}
          {f'<p class="biz-pitch">"{pitch}"</p>' if not compact else ''}
        </div>
        """
    )


def render_prospect_card(
    prospect: Prospect,
    enrichment: Optional[FacilityEnrichment],
    *,
    key_prefix: str,
    on_preview: Callable[[], None],
    on_pass: Callable[[], None],
) -> None:
    render_html(render_prospect_card_html(prospect, enrichment))
    preview_col, pass_col = st.columns([2, 1])
    with preview_col:
        if st.button(
            "Preview email",
            key=f"{key_prefix}_preview_{prospect.id}",
            type="primary",
            use_container_width=True,
        ):
            on_preview()
    with pass_col:
        if prospect.status not in {ProspectStatus.DECLINED, ProspectStatus.ACTIVE_CLIENT}:
            if st.button(
                "Pass",
                key=f"{key_prefix}_pass_{prospect.id}",
                use_container_width=True,
            ):
                on_pass()


def render_empty_state(
    *,
    icon: str,
    headline: str,
    body: str,
    cta_label: str,
    cta_key: str,
) -> bool:
    st.markdown(
        f"""
        <div class="biz-empty">
          <div class="biz-empty-icon">{html.escape(icon)}</div>
          <h3>{html.escape(headline)}</h3>
          <p>{html.escape(body)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    return st.button(cta_label, key=cta_key, type="primary")
