"""Business / GTM prospect pipeline for Manitoba hospital labs."""

from lab_scheduler.business.discovery import (
    DEFAULT_FACILITY_DATASET,
    DiscoveryResult,
    compute_icp_score,
    derive_pain_signals,
    discover_manitoba_prospects,
    list_scored_manitoba_facilities,
    score_facility_record,
)
from lab_scheduler.business.email_templates import (
    EmailDraft,
    PRODUCT_VALUE_PROPS,
    generate_outreach_email,
)
from lab_scheduler.business.inbound_email import (
    InboundMessage,
    SyncResult,
    ensure_business_inbound_schema,
    sync_inbound_from_imap,
)
from lab_scheduler.business.models import (
    PROSPECT_STATUSES,
    Prospect,
    ProspectStatus,
    ensure_business_prospects_schema,
)
from lab_scheduler.business.prospect_service import (
    ProceedClientResult,
    ProspectServiceError,
    create_prospect,
    discover_and_persist_manitoba,
    generate_email_preview,
    get_prospect,
    list_prospects,
    proceed_with_client,
    update_prospect,
    update_prospect_status,
)

__all__ = [
    "DEFAULT_FACILITY_DATASET",
    "DiscoveryResult",
    "EmailDraft",
    "InboundMessage",
    "PROSPECT_STATUSES",
    "PRODUCT_VALUE_PROPS",
    "ProceedClientResult",
    "Prospect",
    "ProspectServiceError",
    "ProspectStatus",
    "SyncResult",
    "compute_icp_score",
    "create_prospect",
    "derive_pain_signals",
    "discover_and_persist_manitoba",
    "discover_manitoba_prospects",
    "ensure_business_inbound_schema",
    "ensure_business_prospects_schema",
    "generate_email_preview",
    "generate_outreach_email",
    "get_prospect",
    "list_prospects",
    "list_scored_manitoba_facilities",
    "proceed_with_client",
    "score_facility_record",
    "sync_inbound_from_imap",
    "update_prospect",
    "update_prospect_status",
]
