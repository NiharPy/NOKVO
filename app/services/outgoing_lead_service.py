from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.core.secret_crypto import decrypt_secret, encrypt_secret
from app.models.outgoing_lead import (
    LeadCallStatus,
    LeadCaptureForm,
    LeadCaptureFormStatus,
    LeadConnectionStatus,
    LeadConsentStatus,
    LeadSourceConnection,
    LeadSourceProvider,
    OutgoingLead,
)
from app.models.tenant_resources import TenantResources


ALLOWED_CALL_SOURCES = {
    LeadSourceProvider.meta_ads,
    LeadSourceProvider.google_ads,
    LeadSourceProvider.google_forms,
    LeadSourceProvider.nokvo_form,
}

META_SCOPES = [
    # Discover the pages the user manages so we can offer them in the picker.
    "pages_show_list",
    # Read the page's posts/engagement we link to incoming leads.
    "pages_read_engagement",
    # Required by Graph API for ``/{page-id}/leadgen_forms`` — Meta gates this
    # behind ad-management capability even when you only want to read forms.
    # Without it, the lead-forms fetch returns ``(#200) Requires pages_manage_ads
    # permission to manage the object`` even if leads_retrieval is granted.
    "pages_manage_ads",
    # Read aggregate ad performance for the analytics tab.
    "ads_read",
    # Pull the actual lead form submissions.
    "leads_retrieval",
]
GOOGLE_ADS_SCOPES = ["https://www.googleapis.com/auth/adwords"]
GOOGLE_FORMS_SCOPES = [
    "https://www.googleapis.com/auth/forms.body.readonly",
    "https://www.googleapis.com/auth/forms.responses.readonly",
]


class OutgoingLeadServiceError(RuntimeError):
    """Intentional, caller-visible failure during a lead-source sync or OAuth
    handshake. Inherits from ``RuntimeError`` so the API layer's ``_safe_detail``
    helper forwards the message to clients instead of replacing it with a
    generic "Operation failed"."""

    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _normalize_phone(value: str | None) -> str:
    raw = re.sub(r"[^\d+]", "", str(value or ""))
    if not raw:
        return ""
    if raw.startswith("+"):
        return "+" + re.sub(r"\D", "", raw[1:])
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) > 10:
        return f"+{digits}"
    return digits


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "true", "yes", "y", "checked", "on", "agree", "agreed", "consent"}


def _field_lookup(fields: dict[str, Any], keys: list[str]) -> str:
    lowered = {str(k).strip().lower(): v for k, v in fields.items()}
    for key in keys:
        if not key:
            continue
        if key in fields and fields[key] not in (None, ""):
            return str(fields[key]).strip()
        lk = str(key).lower()
        if lk in lowered and lowered[lk] not in (None, ""):
            return str(lowered[lk]).strip()
    return ""


def _guess_field(fields: dict[str, Any], needles: list[str]) -> str:
    for key, value in fields.items():
        k = str(key).lower()
        if any(needle in k for needle in needles) and value not in (None, ""):
            return str(value).strip()
    return ""


def normalize_lead_fields(
    fields: dict[str, Any],
    mapping: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    mapping = dict(mapping or {})
    name = _field_lookup(fields, [mapping.get("name"), "name", "full_name", "full name", "first_name"])
    email = _field_lookup(fields, [mapping.get("email"), "email", "email_address", "email address"])
    phone = _field_lookup(
        fields,
        [mapping.get("phone"), "phone", "phone_number", "phone number", "mobile", "mobile_number"],
    )
    if not name:
        name = _guess_field(fields, ["name"])
    if not email:
        email = _guess_field(fields, ["email"])
    if not phone:
        phone = _guess_field(fields, ["phone", "mobile", "whatsapp", "call"])
    return name, email, phone


def _consent_from_fields(
    fields: dict[str, Any],
    *,
    consent_field_key: str | None,
    consent_text: str | None,
    default_call_consent: bool = False,
) -> tuple[LeadConsentStatus, str | None, str | None, datetime | None]:
    if consent_field_key:
        value = fields.get(consent_field_key)
        status = LeadConsentStatus.granted if _truthy(value) else LeadConsentStatus.unknown
        return status, consent_field_key, str(value) if value is not None else None, _now() if status == LeadConsentStatus.granted else None

    for key, value in fields.items():
        normalized = str(key).lower()
        if any(word in normalized for word in ("consent", "agree", "permission", "call")):
            if _truthy(value):
                return LeadConsentStatus.granted, str(key), str(value), _now()

    if default_call_consent and consent_text:
        return LeadConsentStatus.granted, "__form_level_call_consent__", "true", _now()
    return LeadConsentStatus.unknown, None, None, None


def lead_is_callable(lead: OutgoingLead) -> bool:
    provider = lead.source_provider
    if isinstance(provider, str):
        provider = LeadSourceProvider(provider)
    return (
        provider in ALLOWED_CALL_SOURCES
        and bool(lead.phone_e164)
        and lead.consent_status == LeadConsentStatus.granted
        and lead.opt_out_at is None
        and lead.call_status != LeadCallStatus.opted_out
    )


def _connection_token(connection: LeadSourceConnection) -> str:
    if not connection.access_token_encrypted:
        raise OutgoingLeadServiceError("Connection does not have an access token.")
    return decrypt_secret(connection.access_token_encrypted)


def _oauth_state(
    *,
    tenant_id: str,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    provider: str,
    mode: str,
) -> str:
    return jwt.encode(
        {
            "tenant_id": tenant_id,
            "organization_id": str(organization_id),
            "user_id": str(user_id),
            "provider": provider,
            "mode": mode,
            "nonce": secrets.token_urlsafe(12),
            "exp": datetime.utcnow() + timedelta(minutes=20),
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


def decode_oauth_state(state: str) -> dict[str, Any]:
    try:
        return jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.PyJWTError as exc:
        raise OutgoingLeadServiceError("Invalid or expired OAuth state.") from exc


class OutgoingLeadService:
    @staticmethod
    def oauth_start_url(
        tenant_res: TenantResources,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        provider: str,
        mode: str = "ads",
    ) -> dict[str, str]:
        state = _oauth_state(
            tenant_id=tenant_res.tenant_id,
            organization_id=organization_id,
            user_id=user_id,
            provider=provider,
            mode=mode,
        )
        if provider == "meta_ads":
            redirect_uri = settings.META_ADS_REDIRECT_URI
            if not settings.META_ADS_APP_ID or not redirect_uri:
                raise OutgoingLeadServiceError("Meta OAuth app id or redirect URI is not configured.")
            query = urlencode(
                {
                    "client_id": settings.META_ADS_APP_ID,
                    "redirect_uri": redirect_uri,
                    "state": state,
                    "scope": ",".join(META_SCOPES),
                    "response_type": "code",
                }
            )
            return {"authorization_url": f"https://www.facebook.com/{settings.META_GRAPH_VERSION}/dialog/oauth?{query}", "state": state}

        if provider == "google_ads":
            client_id = settings.GOOGLE_LEADS_OAUTH_CLIENT_ID or settings.GOOGLE_OAUTH_CLIENT_ID
            redirect_uri = OutgoingLeadService._google_lead_redirect_uri(provider)
            if not client_id or not redirect_uri:
                raise OutgoingLeadServiceError("Google OAuth client id or redirect URI is not configured.")
            query = urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": " ".join(GOOGLE_ADS_SCOPES),
                    "access_type": "offline",
                    "prompt": "consent",
                    "state": state,
                }
            )
            return {"authorization_url": f"https://accounts.google.com/o/oauth2/v2/auth?{query}", "state": state}

        if provider == "google_forms":
            client_id = settings.GOOGLE_LEADS_OAUTH_CLIENT_ID or settings.GOOGLE_OAUTH_CLIENT_ID
            redirect_uri = OutgoingLeadService._google_lead_redirect_uri(provider)
            if not client_id or not redirect_uri:
                raise OutgoingLeadServiceError("Google OAuth client id or redirect URI is not configured.")
            query = urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": " ".join(GOOGLE_FORMS_SCOPES),
                    "access_type": "offline",
                    "prompt": "consent",
                    "state": state,
                }
            )
            return {"authorization_url": f"https://accounts.google.com/o/oauth2/v2/auth?{query}", "state": state}

        raise OutgoingLeadServiceError("Unsupported lead OAuth provider.")

    @staticmethod
    async def exchange_oauth_code(
        db: AsyncSession,
        *,
        tenant_res: TenantResources,
        user_id: uuid.UUID,
        provider: str,
        mode: str,
        code: str,
    ) -> LeadSourceConnection:
        if provider == "meta_ads":
            token_data = await OutgoingLeadService._exchange_meta_code(code)
            scopes = META_SCOPES
            display_name = "Instagram Ads" if mode == "instagram_ads" else "Facebook Ads"
            provider_enum = LeadSourceProvider.meta_ads
        elif provider in {"google_ads", "google_forms"}:
            token_data = await OutgoingLeadService._exchange_google_code(code, provider=provider)
            scopes = GOOGLE_ADS_SCOPES if provider == "google_ads" else GOOGLE_FORMS_SCOPES
            display_name = "Google Ads" if provider == "google_ads" else "Google Forms"
            provider_enum = LeadSourceProvider.google_ads if provider == "google_ads" else LeadSourceProvider.google_forms
        else:
            raise OutgoingLeadServiceError("Unsupported OAuth provider.")

        expires_at = None
        if token_data.get("expires_in"):
            expires_at = _now() + timedelta(seconds=int(token_data["expires_in"]))
        conn = LeadSourceConnection(
            tenant_id=tenant_res.tenant_id,
            provider=provider_enum,
            status=LeadConnectionStatus.connected,
            display_name=display_name,
            scopes=scopes,
            access_token_encrypted=encrypt_secret(token_data["access_token"]),
            refresh_token_encrypted=encrypt_secret(token_data["refresh_token"]) if token_data.get("refresh_token") else None,
            expires_at=expires_at,
            metadata_={"oauth_mode": mode, "token_type": token_data.get("token_type")},
            created_by_user_id=user_id,
        )
        db.add(conn)
        await db.commit()
        await db.refresh(conn)
        return conn

    @staticmethod
    async def _exchange_meta_code(code: str) -> dict[str, Any]:
        if not settings.META_ADS_APP_ID or not settings.META_ADS_APP_SECRET or not settings.META_ADS_REDIRECT_URI:
            raise OutgoingLeadServiceError("Meta OAuth credentials are not configured.")
        base = f"https://graph.facebook.com/{settings.META_GRAPH_VERSION}/oauth/access_token"
        async with httpx.AsyncClient(timeout=20.0) as client:
            short = await client.get(
                base,
                params={
                    "client_id": settings.META_ADS_APP_ID,
                    "client_secret": settings.META_ADS_APP_SECRET,
                    "redirect_uri": settings.META_ADS_REDIRECT_URI,
                    "code": code,
                },
            )
            short.raise_for_status()
            short_data = short.json()
            access_token = short_data.get("access_token")
            if not access_token:
                raise OutgoingLeadServiceError("Meta did not return an access token.")
            long_resp = await client.get(
                base,
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": settings.META_ADS_APP_ID,
                    "client_secret": settings.META_ADS_APP_SECRET,
                    "fb_exchange_token": access_token,
                },
            )
            long_resp.raise_for_status()
            data = long_resp.json()
            data.setdefault("access_token", access_token)
            return data

    @staticmethod
    def _google_lead_redirect_uri(provider: str) -> str:
        """Resolve the OAuth redirect URI for a Google-backed lead source.

        Priority:
          1. ``GOOGLE_LEADS_OAUTH_REDIRECT_URI`` if the admin set it
             explicitly (e.g., when the leads OAuth client lives in a
             different Google Cloud project than the SSO client).
          2. Otherwise compose from ``AGENT_PUBLIC_BASE_URL`` so the
             generic Nokvo One Google client (``GOOGLE_OAUTH_CLIENT_ID``)
             can also drive the leads flow without extra setup — Google
             accepts the same client across redirect URIs as long as
             they're whitelisted in the Cloud Console.
        """
        explicit = (settings.GOOGLE_LEADS_OAUTH_REDIRECT_URI or "").strip()
        if explicit:
            return explicit
        base = (settings.AGENT_PUBLIC_BASE_URL or "http://localhost:8000").rstrip("/")
        return f"{base}/api/nokvo-one/agents/lead-sources/oauth/{provider}/callback"

    @staticmethod
    async def _exchange_google_code(code: str, *, provider: str = "google_ads") -> dict[str, Any]:
        client_id = settings.GOOGLE_LEADS_OAUTH_CLIENT_ID or settings.GOOGLE_OAUTH_CLIENT_ID
        client_secret = settings.GOOGLE_LEADS_OAUTH_CLIENT_SECRET or settings.GOOGLE_OAUTH_CLIENT_SECRET
        redirect_uri = OutgoingLeadService._google_lead_redirect_uri(provider)
        if not client_id or not client_secret or not redirect_uri:
            raise OutgoingLeadServiceError("Google OAuth credentials are not configured.")
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "code": code,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("access_token"):
                raise OutgoingLeadServiceError("Google did not return an access token.")
            return data

    @staticmethod
    async def list_connections(tenant_res: TenantResources, db: AsyncSession) -> list[LeadSourceConnection]:
        res = await db.execute(
            select(LeadSourceConnection)
            .where(LeadSourceConnection.tenant_id == tenant_res.tenant_id)
            .order_by(LeadSourceConnection.created_at.desc())
        )
        return list(res.scalars().all())

    @staticmethod
    async def update_connection_metadata(
        connection: LeadSourceConnection,
        db: AsyncSession,
        *,
        metadata: dict[str, Any],
        display_name: str | None = None,
        provider_account_id: str | None = None,
    ) -> LeadSourceConnection:
        connection.metadata_ = {**dict(connection.metadata_ or {}), **dict(metadata or {})}
        if display_name:
            connection.display_name = display_name
        if provider_account_id:
            connection.provider_account_id = provider_account_id
        flag_modified(connection, "metadata_")
        db.add(connection)
        await db.commit()
        await db.refresh(connection)
        return connection

    @staticmethod
    async def disconnect_connection(
        connection: LeadSourceConnection,
        db: AsyncSession,
        *,
        revoke_remote: bool = True,
    ) -> LeadSourceConnection:
        """Detach a lead-source connection.

        * Best-effort revoke at the provider (Meta / Google) so the next
          reconnect re-prompts the user for consent. Failures here are
          logged but never block the local disconnect — once we've cleared
          the encrypted token in our DB the connection cannot be used to
          sync further leads anyway.
        * Local state: status becomes ``disabled``, tokens cleared, last
          error stamped so the dashboard reflects the disconnect.
        """
        provider = connection.provider
        token: str | None = None
        if connection.access_token_encrypted:
            try:
                token = decrypt_secret(connection.access_token_encrypted)
            except Exception:
                token = None

        if revoke_remote and token:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    if provider == LeadSourceProvider.meta_ads:
                        # Graph: DELETE /me/permissions revokes every scope.
                        await client.delete(
                            f"https://graph.facebook.com/{settings.META_GRAPH_VERSION}/me/permissions",
                            params={"access_token": token},
                        )
                    elif provider in (LeadSourceProvider.google_ads, LeadSourceProvider.google_forms):
                        await client.post(
                            "https://oauth2.googleapis.com/revoke",
                            data={"token": token},
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                        )
            except Exception:
                logger.warning(
                    "Remote token revoke failed for connection %s; clearing local credentials anyway",
                    connection.id,
                    exc_info=True,
                )

        connection.access_token_encrypted = None
        connection.refresh_token_encrypted = None
        connection.expires_at = None
        connection.status = LeadConnectionStatus.disabled
        connection.last_error = "Disconnected by admin"
        db.add(connection)
        await db.commit()
        await db.refresh(connection)
        return connection

    @staticmethod
    async def list_forms(tenant_res: TenantResources, db: AsyncSession) -> list[LeadCaptureForm]:
        res = await db.execute(
            select(LeadCaptureForm)
            .where(LeadCaptureForm.tenant_id == tenant_res.tenant_id)
            .order_by(LeadCaptureForm.created_at.desc())
        )
        return list(res.scalars().all())

    @staticmethod
    async def create_nokvo_form(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        created_by_user_id: uuid.UUID,
        name: str,
        fields: list[dict[str, Any]],
        consent_text: str,
    ) -> LeadCaptureForm:
        if not name.strip():
            raise OutgoingLeadServiceError("Form name is required.")
        if not consent_text.strip():
            raise OutgoingLeadServiceError("Call consent text is required.")
        normalized_fields = [
            {
                "key": re.sub(r"[^a-z0-9_]", "_", str(f.get("key") or f.get("label") or "").strip().lower()).strip("_"),
                "label": str(f.get("label") or f.get("key") or "").strip(),
                "type": str(f.get("type") or "text"),
                "required": bool(f.get("required", False)),
            }
            for f in (fields or [])
        ]
        normalized_fields = [f for f in normalized_fields if f["key"] and f["label"]]
        required = {f["key"] for f in normalized_fields}
        for key, label, field_type in [
            ("name", "Name", "text"),
            ("phone", "Phone", "phone"),
        ]:
            if key not in required:
                normalized_fields.insert(0, {"key": key, "label": label, "type": field_type, "required": True})
        normalized_fields.append({"key": "call_consent", "label": consent_text.strip(), "type": "checkbox", "required": True})
        slug = f"{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}-{secrets.token_urlsafe(5).lower()}"
        form = LeadCaptureForm(
            tenant_id=tenant_res.tenant_id,
            provider=LeadSourceProvider.nokvo_form,
            status=LeadCaptureFormStatus.active,
            name=name.strip(),
            public_slug=slug,
            field_schema=normalized_fields,
            field_mapping={"name": "name", "phone": "phone", "email": "email"},
            consent_field_key="call_consent",
            consent_text=consent_text.strip(),
            default_call_consent=False,
            created_by_user_id=created_by_user_id,
        )
        db.add(form)
        await db.commit()
        await db.refresh(form)
        return form

    @staticmethod
    async def get_public_form(slug: str, db: AsyncSession) -> LeadCaptureForm | None:
        res = await db.execute(
            select(LeadCaptureForm).where(
                LeadCaptureForm.public_slug == slug,
                LeadCaptureForm.provider == LeadSourceProvider.nokvo_form,
                LeadCaptureForm.status == LeadCaptureFormStatus.active,
            )
        )
        return res.scalars().first()

    @staticmethod
    async def submit_public_form(
        form: LeadCaptureForm,
        db: AsyncSession,
        *,
        fields: dict[str, Any],
        request_metadata: dict[str, Any],
    ) -> OutgoingLead:
        field_schema = list(form.field_schema or [])
        for field in field_schema:
            key = field.get("key")
            if field.get("required") and key and fields.get(key) in (None, "", False):
                raise OutgoingLeadServiceError(f"{field.get('label') or key} is required.")
        return await OutgoingLeadService.upsert_lead(
            form.tenant_id,
            db,
            source_provider=LeadSourceProvider.nokvo_form,
            capture_form=form,
            provider_lead_id=f"nokvo:{form.id}:{hashlib.sha256(_json_safe(fields).encode()).hexdigest()}",
            fields=fields,
            source_metadata=request_metadata,
            submitted_at=_now(),
        )

    @staticmethod
    async def register_external_form(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        created_by_user_id: uuid.UUID,
        provider: LeadSourceProvider,
        name: str,
        provider_form_id: str,
        source_connection_id: uuid.UUID | None,
        field_mapping: dict[str, Any],
        consent_field_key: str | None,
        consent_text: str | None,
        default_call_consent: bool = False,
    ) -> LeadCaptureForm:
        if provider == LeadSourceProvider.nokvo_form:
            raise OutgoingLeadServiceError("Use the Nokvo form endpoint for Nokvo forms.")
        form = LeadCaptureForm(
            tenant_id=tenant_res.tenant_id,
            provider=provider,
            status=LeadCaptureFormStatus.active,
            name=name.strip() or provider_form_id,
            provider_form_id=provider_form_id.strip(),
            source_connection_id=source_connection_id,
            field_mapping=field_mapping or {},
            consent_field_key=consent_field_key,
            consent_text=consent_text,
            default_call_consent=default_call_consent,
            created_by_user_id=created_by_user_id,
        )
        db.add(form)
        await db.commit()
        await db.refresh(form)
        return form

    @staticmethod
    async def upsert_lead(
        tenant_id: str,
        db: AsyncSession,
        *,
        source_provider: LeadSourceProvider,
        capture_form: LeadCaptureForm | None,
        provider_lead_id: str | None,
        fields: dict[str, Any],
        source_connection_id: uuid.UUID | None = None,
        source_metadata: dict[str, Any] | None = None,
        submitted_at: datetime | None = None,
    ) -> OutgoingLead:
        mapping = dict(capture_form.field_mapping or {}) if capture_form else {}
        name, email, phone = normalize_lead_fields(fields, mapping)
        consent_status, consent_key, consent_value, consented_at = _consent_from_fields(
            fields,
            consent_field_key=capture_form.consent_field_key if capture_form else None,
            consent_text=capture_form.consent_text if capture_form else None,
            default_call_consent=bool(capture_form.default_call_consent) if capture_form else False,
        )
        phone_e164 = _normalize_phone(phone)
        existing = None
        if provider_lead_id:
            res = await db.execute(
                select(OutgoingLead).where(
                    OutgoingLead.tenant_id == tenant_id,
                    OutgoingLead.source_provider == source_provider,
                    OutgoingLead.provider_lead_id == provider_lead_id,
                )
            )
            existing = res.scalars().first()
        lead = existing or OutgoingLead(
            tenant_id=tenant_id,
            source_provider=source_provider,
            provider_lead_id=provider_lead_id,
        )
        lead.source_connection_id = source_connection_id or (capture_form.source_connection_id if capture_form else None)
        lead.capture_form_id = capture_form.id if capture_form else None
        lead.name = name or lead.name
        lead.email = email or lead.email
        lead.phone_raw = phone or lead.phone_raw
        lead.phone_e164 = phone_e164 or lead.phone_e164
        lead.fields = fields
        lead.source_metadata = dict(source_metadata or {})
        lead.consent_status = consent_status
        lead.consent_text = capture_form.consent_text if capture_form else None
        lead.consent_field_key = consent_key
        lead.consent_value = consent_value
        lead.consented_at = consented_at or lead.consented_at
        lead.submitted_at = submitted_at or lead.submitted_at
        if not lead.phone_e164:
            lead.call_status = LeadCallStatus.invalid
        db.add(lead)
        await db.commit()
        await db.refresh(lead)
        return lead

    @staticmethod
    async def list_leads(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        eligible_only: bool = False,
        limit: int = 200,
    ) -> list[OutgoingLead]:
        stmt = (
            select(OutgoingLead)
            .where(OutgoingLead.tenant_id == tenant_res.tenant_id)
            .order_by(OutgoingLead.created_at.desc())
            .limit(max(1, min(limit, 500)))
        )
        if eligible_only:
            stmt = stmt.where(
                OutgoingLead.consent_status == LeadConsentStatus.granted,
                OutgoingLead.phone_e164.is_not(None),
                OutgoingLead.opt_out_at.is_(None),
            )
        res = await db.execute(stmt)
        leads = list(res.scalars().all())
        return [lead for lead in leads if lead_is_callable(lead)] if eligible_only else leads

    @staticmethod
    async def validate_callable_leads(
        tenant_res: TenantResources,
        db: AsyncSession,
        lead_ids: list[uuid.UUID],
    ) -> list[OutgoingLead]:
        if not lead_ids:
            raise OutgoingLeadServiceError("Select at least one consented lead.")
        res = await db.execute(
            select(OutgoingLead).where(
                OutgoingLead.tenant_id == tenant_res.tenant_id,
                OutgoingLead.id.in_(lead_ids),
            )
        )
        found = list(res.scalars().all())
        by_id = {lead.id: lead for lead in found}
        missing = [lead_id for lead_id in lead_ids if lead_id not in by_id]
        if missing:
            raise OutgoingLeadServiceError("One or more selected leads were not found.")
        blocked = [lead for lead in found if not lead_is_callable(lead)]
        if blocked:
            raise OutgoingLeadServiceError("Campaign includes leads without call consent or a valid phone number.")
        return [by_id[lead_id] for lead_id in lead_ids]

    @staticmethod
    async def sync_connection(
        connection: LeadSourceConnection,
        db: AsyncSession,
    ) -> dict[str, Any]:
        try:
            if connection.provider == LeadSourceProvider.meta_ads:
                result = await OutgoingLeadService._sync_meta(connection, db)
            elif connection.provider == LeadSourceProvider.google_ads:
                result = await OutgoingLeadService._sync_google_ads(connection, db)
            elif connection.provider == LeadSourceProvider.google_forms:
                result = await OutgoingLeadService._sync_google_forms(connection, db)
            else:
                raise OutgoingLeadServiceError("Unsupported sync provider.")
            connection.status = LeadConnectionStatus.connected
            connection.last_sync_at = _now()
            connection.last_error = None
            db.add(connection)
            await db.commit()
            return result
        except Exception as exc:
            connection.status = LeadConnectionStatus.error
            connection.last_error = str(exc)[:1000]
            db.add(connection)
            await db.commit()
            raise

    @staticmethod
    async def ingest_meta_leadgen_event(
        db: AsyncSession,
        *,
        form_id: str,
        leadgen_id: str,
    ) -> OutgoingLead | None:
        form_res = await db.execute(
            select(LeadCaptureForm).where(
                LeadCaptureForm.provider == LeadSourceProvider.meta_ads,
                LeadCaptureForm.provider_form_id == str(form_id),
            )
        )
        form = form_res.scalars().first()
        if not form or not form.source_connection_id:
            return None
        conn_res = await db.execute(
            select(LeadSourceConnection).where(LeadSourceConnection.id == form.source_connection_id)
        )
        connection = conn_res.scalars().first()
        if not connection:
            return None
        token = _connection_token(connection)
        base = f"https://graph.facebook.com/{settings.META_GRAPH_VERSION}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{base}/{leadgen_id}",
                params={"fields": "id,created_time,field_data,ad_id,form_id,platform", "access_token": token},
            )
            resp.raise_for_status()
            lead_payload = resp.json()
        fields = {
            str(entry.get("name")): (entry.get("values") or [""])[0]
            for entry in (lead_payload.get("field_data") or [])
            if entry.get("name")
        }
        return await OutgoingLeadService.upsert_lead(
            connection.tenant_id,
            db,
            source_provider=LeadSourceProvider.meta_ads,
            source_connection_id=connection.id,
            capture_form=form,
            provider_lead_id=str(lead_payload.get("id") or leadgen_id),
            fields=fields,
            source_metadata=lead_payload,
            submitted_at=_parse_datetime(lead_payload.get("created_time")),
        )

    @staticmethod
    def _explain_meta_error(exc: httpx.HTTPStatusError, *, step: str) -> OutgoingLeadServiceError:
        """Turn a raw Graph API HTTPStatusError into a caller-facing message.

        Meta's error envelope is ``{"error": {"message", "type", "code",
        "error_subcode", "fbtrace_id"}}``. We surface ``message`` because it
        usually explains the missing permission or expired token, while
        suppressing ``fbtrace_id`` and other internal details. When the
        message names a specific scope ("Requires pages_manage_ads permission"),
        we lift it into the actionable guidance so the user knows exactly
        which OAuth consent they need to re-grant.
        """
        import re as _re

        status_code = exc.response.status_code if exc.response is not None else "?"
        message: str | None = None
        try:
            payload = exc.response.json() if exc.response is not None else {}
            err = (payload or {}).get("error") or {}
            message = err.get("message") or err.get("type")
        except Exception:
            message = None
        if not message:
            message = (
                "the access token may be expired or missing the required scopes"
                if status_code in (401, 403)
                else "Meta rejected the request"
            )
        guidance = ""
        if status_code in (401, 403):
            missing = _re.search(
                r"Requires\s+([A-Za-z0-9_]+)\s+permission",
                message,
                _re.IGNORECASE,
            )
            if missing:
                scope = missing.group(1)
                guidance = (
                    f" Reconnect Meta in Outgoing → Lead sources — the existing "
                    f"token does not include the '{scope}' permission Meta now "
                    "requires for lead-form access."
                )
            else:
                guidance = (
                    " Reconnect Meta in Outgoing → Lead sources and confirm "
                    "the page has the leads_retrieval and pages_manage_ads "
                    "permissions."
                )
        return OutgoingLeadServiceError(
            f"Meta {step} failed ({status_code}): {message}.{guidance}"
        )

    @staticmethod
    async def _sync_meta(connection: LeadSourceConnection, db: AsyncSession) -> dict[str, Any]:
        token = _connection_token(connection)
        base = f"https://graph.facebook.com/{settings.META_GRAPH_VERSION}"
        imported_forms = 0
        imported_leads = 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                pages_resp = await client.get(
                    f"{base}/me/accounts",
                    params={"fields": "id,name,access_token", "access_token": token},
                )
                pages_resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise OutgoingLeadService._explain_meta_error(exc, step="page lookup") from exc
            except httpx.HTTPError as exc:
                raise OutgoingLeadServiceError(f"Meta page lookup failed: {exc}") from exc
            pages = pages_resp.json().get("data") or []
            metadata = dict(connection.metadata_ or {})
            metadata["pages"] = [{"id": p.get("id"), "name": p.get("name")} for p in pages]
            connection.metadata_ = metadata
            flag_modified(connection, "metadata_")
            if not pages:
                raise OutgoingLeadServiceError(
                    "Meta returned no Facebook pages for this account. "
                    "Make sure the connected user is an admin/editor on at "
                    "least one page that hosts the lead form."
                )
            for page in pages:
                page_id = page.get("id")
                page_token = page.get("access_token") or token
                if not page_id:
                    continue
                try:
                    forms_resp = await client.get(
                        f"{base}/{page_id}/leadgen_forms",
                        params={"fields": "id,name,status,questions", "limit": 100, "access_token": page_token},
                    )
                    forms_resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise OutgoingLeadService._explain_meta_error(
                        exc, step=f"lead form lookup for page {page.get('name') or page_id}"
                    ) from exc
                except httpx.HTTPError as exc:
                    raise OutgoingLeadServiceError(f"Meta lead form lookup failed: {exc}") from exc
                forms = forms_resp.json().get("data") or []
                for item in forms:
                    form = await OutgoingLeadService._upsert_provider_form(
                        connection,
                        db,
                        provider=LeadSourceProvider.meta_ads,
                        provider_form_id=str(item.get("id")),
                        provider_account_id=str(page_id),
                        name=str(item.get("name") or "Meta lead form"),
                        field_schema=item.get("questions") or [],
                        metadata={"page_id": page_id, "page_name": page.get("name"), "status": item.get("status")},
                    )
                    imported_forms += 1
                    try:
                        leads_resp = await client.get(
                            f"{base}/{item.get('id')}/leads",
                            params={"fields": "id,created_time,field_data,ad_id,form_id,platform", "limit": 100, "access_token": page_token},
                        )
                        leads_resp.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise OutgoingLeadService._explain_meta_error(
                            exc, step=f"lead retrieval for form {item.get('name') or item.get('id')}"
                        ) from exc
                    except httpx.HTTPError as exc:
                        raise OutgoingLeadServiceError(
                            f"Meta lead retrieval failed: {exc}"
                        ) from exc
                    for lead_payload in leads_resp.json().get("data") or []:
                        fields = {
                            str(entry.get("name")): (entry.get("values") or [""])[0]
                            for entry in (lead_payload.get("field_data") or [])
                            if entry.get("name")
                        }
                        await OutgoingLeadService.upsert_lead(
                            connection.tenant_id,
                            db,
                            source_provider=LeadSourceProvider.meta_ads,
                            source_connection_id=connection.id,
                            capture_form=form,
                            provider_lead_id=str(lead_payload.get("id") or ""),
                            fields=fields,
                            source_metadata=lead_payload,
                            submitted_at=_parse_datetime(lead_payload.get("created_time")),
                        )
                        imported_leads += 1
        return {"forms": imported_forms, "leads": imported_leads}

    @staticmethod
    async def _sync_google_ads(connection: LeadSourceConnection, db: AsyncSession) -> dict[str, Any]:
        if not settings.GOOGLE_ADS_DEVELOPER_TOKEN:
            raise OutgoingLeadServiceError("GOOGLE_ADS_DEVELOPER_TOKEN is not configured.")
        customer_id = str(connection.provider_account_id or (connection.metadata_ or {}).get("customer_id") or "").replace("-", "")
        if not customer_id:
            raise OutgoingLeadServiceError("Set a Google Ads customer ID before syncing.")
        token = _connection_token(connection)
        version = settings.GOOGLE_ADS_API_VERSION or "v23"
        headers = {
            "Authorization": f"Bearer {token}",
            "developer-token": settings.GOOGLE_ADS_DEVELOPER_TOKEN,
            "Content-Type": "application/json",
        }
        login_customer = settings.GOOGLE_ADS_LOGIN_CUSTOMER_ID or (connection.metadata_ or {}).get("login_customer_id")
        if login_customer:
            headers["login-customer-id"] = str(login_customer).replace("-", "")
        query = """
            SELECT
              lead_form_submission_data.id,
              lead_form_submission_data.resource_name,
              lead_form_submission_data.submission_date_time,
              lead_form_submission_data.lead_form_submission_fields,
              lead_form_submission_data.custom_lead_form_submission_fields,
              lead_form_submission_data.campaign,
              lead_form_submission_data.ad_group,
              lead_form_submission_data.ad_group_ad,
              lead_form_submission_data.asset,
              lead_form_submission_data.gclid
            FROM lead_form_submission_data
            ORDER BY lead_form_submission_data.submission_date_time DESC
            LIMIT 500
        """
        imported = 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"https://googleads.googleapis.com/{version}/customers/{customer_id}/googleAds:searchStream",
                headers=headers,
                json={"query": query},
            )
            resp.raise_for_status()
            batches = resp.json()
        form = await OutgoingLeadService._upsert_provider_form(
            connection,
            db,
            provider=LeadSourceProvider.google_ads,
            provider_form_id=f"google_ads:{customer_id}",
            provider_account_id=customer_id,
            name=f"Google Ads leads {customer_id}",
            field_schema=[],
            metadata={"customer_id": customer_id},
        )
        for batch in batches if isinstance(batches, list) else []:
            for row in batch.get("results") or []:
                data = row.get("leadFormSubmissionData") or row.get("lead_form_submission_data") or {}
                fields: dict[str, Any] = {}
                for entry in data.get("leadFormSubmissionFields", []) + data.get("customLeadFormSubmissionFields", []):
                    key = entry.get("fieldType") or entry.get("customQuestionFieldType") or entry.get("name") or entry.get("field_type")
                    value = entry.get("fieldValue") or entry.get("answer") or entry.get("value")
                    if key:
                        fields[str(key)] = value
                await OutgoingLeadService.upsert_lead(
                    connection.tenant_id,
                    db,
                    source_provider=LeadSourceProvider.google_ads,
                    source_connection_id=connection.id,
                    capture_form=form,
                    provider_lead_id=str(data.get("id") or data.get("resourceName") or uuid.uuid4()),
                    fields=fields,
                    source_metadata=data,
                    submitted_at=_parse_datetime(data.get("submissionDateTime")),
                )
                imported += 1
        return {"forms": 1, "leads": imported}

    @staticmethod
    async def _sync_google_forms(connection: LeadSourceConnection, db: AsyncSession) -> dict[str, Any]:
        token = _connection_token(connection)
        res = await db.execute(
            select(LeadCaptureForm).where(
                LeadCaptureForm.tenant_id == connection.tenant_id,
                LeadCaptureForm.source_connection_id == connection.id,
                LeadCaptureForm.provider == LeadSourceProvider.google_forms,
                LeadCaptureForm.status == LeadCaptureFormStatus.active,
            )
        )
        forms = list(res.scalars().all())
        imported = 0
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            for form in forms:
                filter_expr = None
                if form.last_synced_at:
                    filter_expr = f'timestamp > {form.last_synced_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}'
                params = {"pageSize": 5000}
                if filter_expr:
                    params["filter"] = filter_expr
                resp = await client.get(
                    f"https://forms.googleapis.com/v1/forms/{form.provider_form_id}/responses",
                    headers=headers,
                    params=params,
                )
                resp.raise_for_status()
                for response in resp.json().get("responses") or []:
                    fields = {}
                    answers = response.get("answers") or {}
                    for question_id, answer in answers.items():
                        text_answers = answer.get("textAnswers", {}).get("answers", [])
                        fields[question_id] = ", ".join(str(a.get("value") or "") for a in text_answers).strip()
                    await OutgoingLeadService.upsert_lead(
                        connection.tenant_id,
                        db,
                        source_provider=LeadSourceProvider.google_forms,
                        source_connection_id=connection.id,
                        capture_form=form,
                        provider_lead_id=str(response.get("responseId") or uuid.uuid4()),
                        fields=fields,
                        source_metadata=response,
                        submitted_at=_parse_datetime(response.get("createTime") or response.get("lastSubmittedTime")),
                    )
                    imported += 1
                form.last_synced_at = _now()
                db.add(form)
                await db.commit()
        return {"forms": len(forms), "leads": imported}

    @staticmethod
    async def _upsert_provider_form(
        connection: LeadSourceConnection,
        db: AsyncSession,
        *,
        provider: LeadSourceProvider,
        provider_form_id: str,
        provider_account_id: str | None,
        name: str,
        field_schema: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> LeadCaptureForm:
        res = await db.execute(
            select(LeadCaptureForm).where(
                LeadCaptureForm.tenant_id == connection.tenant_id,
                LeadCaptureForm.provider == provider,
                LeadCaptureForm.provider_form_id == provider_form_id,
            )
        )
        form = res.scalars().first()
        if form is None:
            form = LeadCaptureForm(
                tenant_id=connection.tenant_id,
                source_connection_id=connection.id,
                provider=provider,
                status=LeadCaptureFormStatus.active,
                name=name,
                provider_form_id=provider_form_id,
                provider_account_id=provider_account_id,
                field_schema=field_schema or [],
                field_mapping={},
                metadata_=metadata or {},
            )
        else:
            form.name = name or form.name
            form.source_connection_id = connection.id
            form.provider_account_id = provider_account_id
            form.field_schema = field_schema or form.field_schema
            form.metadata_ = {**dict(form.metadata_ or {}), **dict(metadata or {})}
            flag_modified(form, "field_schema")
            flag_modified(form, "metadata_")
        db.add(form)
        await db.commit()
        await db.refresh(form)
        return form
