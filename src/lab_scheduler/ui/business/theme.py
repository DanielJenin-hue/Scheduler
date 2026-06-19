"""Dark theme CSS for the Business operator console."""

from __future__ import annotations

import streamlit as st

__all__ = ["BusinessThemeCSS", "inject_business_theme_css"]


def inject_business_theme_css() -> None:
    st.markdown(
        """
        <style>
          :root {
            --biz-bg: #0b1220;
            --biz-surface: #111827;
            --biz-surface-raised: #1a2332;
            --biz-border: #1f2937;
            --biz-text: #e5e7eb;
            --biz-muted: #94a3b8;
            --biz-accent: #38bdf8;
            --biz-accent-deep: #082f49;
            --biz-revenue: #22c55e;
            --biz-revenue-muted: #166534;
            --biz-warning: #fbbf24;
            --biz-danger: #f87171;
            --biz-clinical: #a5b4fc;
          }

          .stApp {
            background: radial-gradient(circle at top, #1e293b 0%, var(--biz-bg) 55%);
            color: var(--biz-text);
          }

          [data-testid="stSidebar"] {
            background: var(--biz-surface);
            border-right: 1px solid var(--biz-border);
          }

          .biz-shell {
            max-width: 1200px;
            margin: 0 auto;
            padding: 8px 4px 48px;
          }

          .biz-hero {
            margin-bottom: 8px;
          }

          .biz-hero-kicker {
            color: var(--biz-accent);
            font-size: 0.8125rem;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            margin: 0 0 4px;
          }

          .biz-hero-title {
            font-size: 1.75rem;
            font-weight: 600;
            margin: 0 0 4px;
            color: var(--biz-text);
          }

          .biz-hero-sub {
            color: var(--biz-muted);
            font-size: 0.9375rem;
            margin: 0;
            line-height: 1.55;
          }

          .biz-metric-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
            margin: 24px 0 32px;
          }

          @media (max-width: 900px) {
            .biz-metric-grid { grid-template-columns: repeat(2, 1fr); }
          }

          .biz-metric-tile {
            background: var(--biz-surface);
            border: 1px solid var(--biz-border);
            border-radius: 12px;
            padding: 16px 18px;
          }

          .biz-metric-label {
            font-size: 0.8125rem;
            font-weight: 500;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: var(--biz-muted);
            margin-bottom: 6px;
          }

          .biz-metric-value {
            font-family: "Cascadia Code", "Consolas", monospace;
            font-size: 1.25rem;
            font-weight: 600;
            color: var(--biz-text);
          }

          .biz-metric-value.revenue { color: var(--biz-revenue); }
          .biz-metric-value.accent { color: var(--biz-accent); }

          .biz-card {
            background: var(--biz-surface);
            border: 1px solid var(--biz-border);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 12px;
            transition: background 150ms ease, border-color 150ms ease;
          }

          .biz-card:hover {
            background: var(--biz-surface-raised);
            border-color: #334155;
          }

          .biz-card-title {
            font-size: 1rem;
            font-weight: 600;
            color: var(--biz-text);
            margin: 0 0 4px;
          }

          .biz-card-sub {
            font-family: "Cascadia Code", "Consolas", monospace;
            font-size: 0.75rem;
            color: var(--biz-muted);
            margin: 0 0 10px;
          }

          .biz-chip-row {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin: 8px 0;
          }

          .biz-chip {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 999px;
            font-size: 0.75rem;
            font-weight: 500;
            background: #1e293b;
            color: var(--biz-clinical);
            border: 1px solid #334155;
          }

          .biz-icp-bar {
            height: 6px;
            border-radius: 999px;
            background: #1f2937;
            overflow: hidden;
            margin: 6px 0 10px;
          }

          .biz-icp-fill {
            height: 100%;
            border-radius: 999px;
            background: linear-gradient(90deg, var(--biz-accent), var(--biz-revenue));
          }

          .biz-mono {
            font-family: "Cascadia Code", "Consolas", monospace;
            font-size: 0.875rem;
            font-weight: 600;
          }

          .biz-mono.revenue { color: var(--biz-revenue); }

          .biz-pain-tag {
            display: inline-block;
            padding: 4px 10px;
            margin: 2px 4px 2px 0;
            border-radius: 6px;
            font-size: 0.8125rem;
            background: #1e293b;
            color: var(--biz-muted);
            border-left: 3px solid var(--biz-accent);
          }

          .biz-pitch {
            font-style: italic;
            color: var(--biz-muted);
            font-size: 0.875rem;
            margin: 10px 0 0;
            line-height: 1.5;
          }

          .biz-badge {
            display: inline-block;
            padding: 6px 10px;
            border-radius: 999px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            line-height: 1;
            white-space: nowrap;
          }

          .biz-badge-new {
            background: #1e3a5f;
            color: #38bdf8;
            border: 1px solid #2563eb40;
          }

          .biz-badge-previewed {
            background: #422006;
            color: #fbbf24;
            border: 1px solid #d9770640;
          }

          .biz-badge-active {
            background: #14532d;
            color: #22c55e;
            border: 1px solid #16a34a40;
          }

          .biz-badge-passed {
            background: #1f2937;
            color: #94a3b8;
            border: 1px solid #374151;
          }

          .biz-empty {
            text-align: center;
            padding: 48px 24px;
            background: var(--biz-surface);
            border: 1px dashed var(--biz-border);
            border-radius: 12px;
            margin: 16px 0;
          }

          .biz-empty-icon {
            font-size: 2rem;
            margin-bottom: 12px;
            opacity: 0.6;
          }

          .biz-empty h3 {
            margin: 0 0 8px;
            font-size: 1.125rem;
            font-weight: 600;
          }

          .biz-empty p {
            color: var(--biz-muted);
            max-width: 28rem;
            margin: 0 auto 20px;
            line-height: 1.55;
          }

          .biz-kanban {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 14px;
            align-items: start;
          }

          @media (max-width: 1100px) {
            .biz-kanban { grid-template-columns: repeat(2, 1fr); }
          }

          @media (max-width: 640px) {
            .biz-kanban { grid-template-columns: 1fr; }
          }

          .biz-kanban-col {
            background: rgba(17, 24, 39, 0.5);
            border: 1px solid var(--biz-border);
            border-radius: 12px;
            padding: 12px;
            min-height: 120px;
          }

          .biz-kanban-header {
            font-size: 0.8125rem;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: var(--biz-muted);
            margin: 0 0 12px;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--biz-border);
          }

          .biz-email-body {
            font-family: Georgia, "Times New Roman", serif;
            font-size: 1rem;
            line-height: 1.6;
            background: #0f172a;
            border: 1px solid var(--biz-border);
            border-radius: 8px;
            padding: 20px;
            white-space: pre-wrap;
            color: var(--biz-text);
          }

          .biz-email-envelope {
            border: 1px solid var(--biz-border);
            border-radius: 10px;
            overflow: hidden;
            margin: 8px 0 16px;
            background: var(--biz-surface);
          }

          .biz-email-meta {
            display: grid;
            grid-template-columns: 72px 1fr;
            gap: 8px;
            align-items: baseline;
            padding: 10px 16px;
            border-bottom: 1px solid var(--biz-border);
            font-size: 0.875rem;
            background: var(--biz-surface-raised);
          }

          .biz-email-meta .biz-email-label {
            color: var(--biz-muted);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.6875rem;
            letter-spacing: 0.05em;
          }

          .biz-email-envelope .biz-email-body {
            border: none;
            border-radius: 0;
            min-height: 200px;
          }

          .biz-confirm-box {
            background: var(--biz-surface-raised);
            border: 1px solid var(--biz-accent);
            border-radius: 12px;
            padding: 20px;
            margin: 16px 0;
          }

          .biz-toast {
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: var(--biz-surface-raised);
            border: 1px solid var(--biz-revenue);
            color: var(--biz-revenue);
            padding: 12px 18px;
            border-radius: 10px;
            font-size: 0.875rem;
            z-index: 9999;
            box-shadow: 0 8px 24px rgba(0,0,0,0.4);
          }

          div[data-testid="stTabs"] button {
            font-weight: 600;
          }

          div[data-testid="stTabs"] [data-baseweb="tab-list"] {
            gap: 8px;
          }

          .biz-mrr-target {
            margin-bottom: 16px;
          }

          .biz-revenue-path {
            display: grid;
            gap: 10px;
            margin: 12px 0 20px;
          }

          .biz-path-step {
            display: flex;
            gap: 12px;
            align-items: flex-start;
            padding: 12px 14px;
            border-radius: 10px;
            border: 1px solid var(--biz-border);
            background: var(--biz-surface);
          }

          .biz-path-step.active {
            border-color: var(--biz-accent);
            background: var(--biz-accent-deep);
          }

          .biz-path-step.done {
            border-color: var(--biz-revenue-muted);
          }

          .biz-path-step.pending {
            opacity: 0.72;
          }

          .biz-path-number {
            width: 28px;
            height: 28px;
            border-radius: 999px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 0.8125rem;
            background: var(--biz-surface-raised);
            color: var(--biz-accent);
            flex-shrink: 0;
          }

          .biz-path-step.done .biz-path-number {
            background: var(--biz-revenue-muted);
            color: var(--biz-revenue);
          }

          .biz-path-title {
            font-weight: 600;
            margin-bottom: 2px;
          }

          .biz-path-detail {
            color: var(--biz-muted);
            font-size: 0.8125rem;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


BusinessThemeCSS = inject_business_theme_css
