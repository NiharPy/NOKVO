from app.db.session import Base
from app.models.user import SuperAdminUser
from app.models.session import SuperAdminSession
from app.models.audit import SuperAdminAuditLog, VoiceDataAccessAuditLog
from app.models.organization import Organization
from app.models.organization_session import OrganizationSession
from app.models.organization_user import OrganizationUser
from app.models.tenant_resources import TenantResources
from app.models.tenant_usage_event import TenantUsageEvent
from app.models.mcp_tool_registry import MCPToolRegistryEntry
from app.models.feedback import TenantFeedback
from app.models.superadmin_todo import SuperAdminTodo
from app.models.email_verification import EmailVerification
from app.models.member_invitation import MemberInvitation
from app.models.nokvo_one_tool_record import NokvoOneToolRecord
from app.models.nokvo_one_agent import NokvoOneAgent
from app.models.agent_tool_invocation import AgentToolInvocation
from app.models.outgoing_lead import (
    LeadCaptureForm,
    LeadSourceConnection,
    OutboundCampaignContact,
    OutgoingLead,
)
from app.models.lead_followup_schedule import (
    FollowupReason,
    FollowupStatus,
    LeadFollowupSchedule,
)
from app.models.member_assignment import (
    ClinicMemberScheduleSettings,
    MemberBlockedSlot,
    NokvoOneAssignmentAuditLog,
    OrganizationAssignmentDefaults,
    OrganizationMemberAssignmentSettings,
)
from app.models.customer_base import CustomerBase
from app.models.connect_api_key import OrganizationApiKey
from app.models.connect_session import ConnectSession
from app.models.call_cost import CallCost
from app.models.subscription import Subscription
from app.models.minute_purchase import MinutePurchase
from app.models.usage_invoice import UsageInvoice
from app.models.llm_pool_key import LlmPoolKey
from app.models.call_transcript import CallTranscript
from app.models.notification import Notification
from app.models.real_estate_project import RealEstateProject
from app.models.bulk_calling_request import BulkCallingRequest
from app.models.apex_support_ticket import ApexSupportTicket

# For Alembic to discover all models
__all__ = [
    "Base",
    "SuperAdminUser",
    "SuperAdminSession",
    "SuperAdminAuditLog",
    "VoiceDataAccessAuditLog",
    "Organization",
    "OrganizationUser",
    "OrganizationSession",
    "TenantResources",
    "TenantUsageEvent",
    "MCPToolRegistryEntry",
    "EmailVerification",
    "MemberInvitation",
    "NokvoOneToolRecord",
    "NokvoOneAgent",
    "AgentToolInvocation",
    "LeadSourceConnection",
    "LeadCaptureForm",
    "OutgoingLead",
    "OutboundCampaignContact",
    "OrganizationAssignmentDefaults",
    "OrganizationMemberAssignmentSettings",
    "ClinicMemberScheduleSettings",
    "MemberBlockedSlot",
    "NokvoOneAssignmentAuditLog",
    "OrganizationApiKey",
    "ConnectSession",
    "CallCost",
    "Subscription",
    "MinutePurchase",
    "UsageInvoice",
    "LlmPoolKey",
    "CallTranscript",
    "Notification",
    "RealEstateProject",
    "FollowupReason",
    "FollowupStatus",
    "LeadFollowupSchedule",
    "CustomerBase",
    "TenantFeedback",
    "SuperAdminTodo",
    "BulkCallingRequest",
    "ApexSupportTicket",
]
