from app.db.session import Base
from app.models.user import SuperAdminUser
from app.models.session import SuperAdminSession
from app.models.audit import SuperAdminAuditLog
from app.models.approval import SuperAdminApprovalRequest
from app.models.organization import Organization
from app.models.organization_session import OrganizationSession
from app.models.organization_user import OrganizationUser
from app.models.tenant_resources import TenantResources
from app.models.tenant_usage_event import TenantUsageEvent
from app.models.mcp_tool_registry import MCPToolRegistryEntry
from app.models.email_verification import EmailVerification
from app.models.member_invitation import MemberInvitation
from app.models.nokvo_one_tool_record import NokvoOneToolRecord
from app.models.nokvo_one_agent import NokvoOneAgent
from app.models.member_assignment import (
    ClinicMemberScheduleSettings,
    MemberBlockedSlot,
    NokvoOneAssignmentAuditLog,
    OrganizationMemberAssignmentSettings,
)

# For Alembic to discover all models
__all__ = [
    "Base",
    "SuperAdminUser",
    "SuperAdminSession",
    "SuperAdminAuditLog",
    "SuperAdminApprovalRequest",
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
    "OrganizationMemberAssignmentSettings",
    "ClinicMemberScheduleSettings",
    "MemberBlockedSlot",
    "NokvoOneAssignmentAuditLog",
]
