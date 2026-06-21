"""Plivo compliance (India KYC) + number allotment for onboarding.

India local DIDs are gated behind a regulatory **compliance application**: an
End User (the business), a set of uploaded **Compliance Documents** (incorporation
certificate + GST/business-PAN), and a **Compliance Application** tying them to the
number type. Plivo then reviews the application (async — hours to days). We file
everything, auto-allot a number immediately, and leave it ``pending_compliance``
until Plivo approves (a later poll/webhook flips it ``active``).

Everything is recorded under ``TenantResources.provider_status["plivo"]["compliance"]``
and the call is **idempotent** — re-running a step reuses stored ids instead of
creating duplicate end users / applications.

The exact ``document_type_id``s + required meta fields are discovered at runtime
from ``GET /ComplianceDocumentType/`` (they differ per country/number type and can
change), so this service maps our two document kinds onto whatever Plivo returns by
name heuristics rather than hard-coding ids.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.plivo_service import PlivoError, PlivoService

logger = logging.getLogger(__name__)

# Heuristics to map our collected docs onto Plivo's returned document types.
_DOC_KIND_KEYWORDS = {
    "incorporation": ("incorporation", "registration", "business_registration", "certificate"),
    "gst_or_pan": ("gst", "pan", "tax", "tin"),
}


class PlivoComplianceService:
    @staticmethod
    def _compliance_record(tenant_res: TenantResources) -> dict[str, Any]:
        plivo = dict((tenant_res.provider_status or {}).get("plivo") or {})
        return dict(plivo.get("compliance") or {})

    @staticmethod
    def _save(tenant_res: TenantResources, *, compliance: dict, number: str | None, number_status: str) -> None:
        provider_status = dict(tenant_res.provider_status or {})
        plivo = dict(provider_status.get("plivo") or {})
        plivo["compliance"] = compliance
        if number is not None:
            plivo["number"] = number
        plivo["number_status"] = number_status
        provider_status["plivo"] = plivo
        tenant_res.provider_status = provider_status
        flag_modified(tenant_res, "provider_status")

    @classmethod
    async def _resolve_document_types(cls, auth: tuple[str, str], base: str) -> list[dict[str, Any]]:
        """Plivo's India business document types (id + name)."""
        try:
            data = await PlivoService._request(
                "GET",
                f"{base}/ComplianceDocumentType/?country_iso=IN&number_type=local&end_user_type=business",
                auth=auth,
            )
            return data.get("objects") or data.get("compliance_document_types") or data.get("data") or []
        except PlivoError:
            logger.warning("PLIVO-COMPLIANCE: could not list document types", exc_info=True)
            return []

    @staticmethod
    def _match_type_id(doc_types: list[dict], kind: str) -> str | None:
        keywords = _DOC_KIND_KEYWORDS.get(kind, ())
        for dt in doc_types:
            name = str(dt.get("document_name") or dt.get("name") or "").lower()
            if any(k in name for k in keywords):
                return str(dt.get("document_type_id") or dt.get("id") or "")
        return None

    @classmethod
    async def submit_compliance_and_allot_number(
        cls,
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        legal_name: str,
        alias_name: str | None,
        business_pan: str | None,
        cin: str | None,
        documents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """File the compliance application + allot a number. Idempotent.

        ``documents`` = ``[{"kind","filename","content"(bytes),"content_type"}, ...]``.
        Returns the persisted compliance record + number/number_status. Best-effort:
        any Plivo failure is captured into ``compliance["error"]`` and the number is
        left ``pending_compliance`` so onboarding can still proceed.
        """
        plivo_cfg = dict((tenant_res.provider_status or {}).get("plivo") or {})
        sub_auth_id = plivo_cfg.get("subaccount_auth_id")
        app_id = plivo_cfg.get("application_id")
        record = cls._compliance_record(tenant_res)

        # Fully done already → no-op (idempotent).
        if record.get("application_id") and plivo_cfg.get("number"):
            return {"compliance": record, "number": plivo_cfg.get("number"), "number_status": plivo_cfg.get("number_status")}

        try:
            auth = PlivoService._master_auth()
        except PlivoError as exc:
            record["error"] = str(exc)
            cls._save(tenant_res, compliance=record, number=None, number_status="pending_compliance")
            await db.commit()
            return {"compliance": record, "number": None, "number_status": "pending_compliance"}

        base = PlivoService._base(auth[0])
        number: str | None = plivo_cfg.get("number")
        number_status = "pending_compliance"

        try:
            doc_types = await cls._resolve_document_types(auth, base)

            # 1) End user (the business). Reuse if already created. Plivo end-users
            # are unique by business name, so a prior signup for the same legal
            # entity (or a retry after our record was lost) makes the POST 400 with
            # "already exists". In that case look the existing end-user up and reuse
            # it rather than dead-ending the whole compliance flow.
            if not record.get("end_user_id"):
                eu_name = legal_name[:120]
                try:
                    eu = await PlivoService._request(
                        "POST",
                        f"{base}/EndUser/",
                        auth=auth,
                        json_body={
                            "name": eu_name,
                            "last_name": (alias_name or legal_name)[:120],
                            "end_user_type": "business",
                        },
                    )
                    record["end_user_id"] = str(eu.get("end_user_id") or eu.get("id") or "")
                except PlivoError as exc:
                    if "already exists" not in str(exc).lower():
                        raise
                    listing = await PlivoService._request(
                        "GET", f"{base}/EndUser/?limit=50", auth=auth
                    )
                    objects = listing.get("objects") or listing.get("end_users") or []
                    needle = eu_name.strip().lower()
                    match = next(
                        (
                            o
                            for o in objects
                            if needle and needle in str(o.get("name") or "").strip().lower()
                        ),
                        None,
                    )
                    if not match:
                        raise
                    record["end_user_id"] = str(match.get("end_user_id") or match.get("id") or "")
            end_user_id = record["end_user_id"]

            # 2) Upload each document (skip ones already uploaded by kind).
            # Bind the dict into ``record`` up front so a mid-loop failure still
            # persists the docs uploaded so far (the save on the error path reads
            # ``record``) — preventing the duplicate-alias collision on retry.
            uploaded = dict(record.get("document_ids") or {})
            record["document_ids"] = uploaded
            for doc in documents:
                kind = doc.get("kind")
                if not kind or kind in uploaded:
                    continue
                type_id = cls._match_type_id(doc_types, kind) or ""
                data = {
                    "end_user_id": end_user_id,
                    "document_type_id": type_id,
                    "alias": f"{kind}-{legal_name}"[:120],
                    "business_name": legal_name,
                }
                if business_pan:
                    data["pan"] = business_pan
                if cin:
                    data["cin"] = cin
                files = {
                    "file": (
                        doc.get("filename") or f"{kind}.pdf",
                        doc.get("content") or b"",
                        doc.get("content_type") or "application/octet-stream",
                    )
                }
                try:
                    up = await PlivoService._request_multipart(
                        f"{base}/ComplianceDocument/", auth=auth, data=data, files=files
                    )
                    uploaded[kind] = str(up.get("document_id") or up.get("id") or "")
                except PlivoError as exc:
                    # The document was uploaded on a prior run but our document_ids
                    # record was lost (the save happens late / on the error path), so
                    # the retry collides on the unique alias. Mirror the end-user
                    # path: look the existing document up and REUSE it instead of
                    # dead-ending the whole compliance filing.
                    if "already exists" not in str(exc).lower():
                        raise
                    alias = data["alias"]
                    listing = await PlivoService._request(
                        "GET", f"{base}/ComplianceDocument/?limit=50", auth=auth
                    )
                    objects = listing.get("objects") or listing.get("compliance_documents") or []
                    match = next(
                        (
                            o for o in objects
                            if str(o.get("alias") or "").strip() == alias.strip()
                            and (not end_user_id or str(o.get("end_user_id") or "") == str(end_user_id))
                        ),
                        None,
                    )
                    if match is None:
                        # Fall back to alias-only match (some list rows omit end_user_id).
                        match = next(
                            (o for o in objects if str(o.get("alias") or "").strip() == alias.strip()),
                            None,
                        )
                    if match is None:
                        raise
                    uploaded[kind] = str(match.get("document_id") or match.get("id") or "")
            record["document_ids"] = uploaded

            # Retry-safety: if no documents are recorded (a retry with no fresh
            # files, or a prior run that uploaded to Plivo but lost our record),
            # reuse the end-user's EXISTING Plivo ComplianceDocuments so we can
            # still file the application instead of dead-ending.
            if not [v for v in uploaded.values() if v] and end_user_id:
                listing = await PlivoService._request(
                    "GET", f"{base}/ComplianceDocument/?limit=50", auth=auth
                )
                for o in (listing.get("objects") or listing.get("compliance_documents") or []):
                    if str(o.get("end_user_id") or "") == str(end_user_id):
                        did = str(o.get("document_id") or o.get("id") or "")
                        if did:
                            uploaded[str(o.get("alias") or did)] = did
                record["document_ids"] = uploaded

            # 3) Compliance application tying end user + documents to the number.
            # Plivo resolves a requirement by (country, number_type, end_user_type,
            # OPERATION_TYPE) — omitting operation_type 400s with "Could not find
            # any requirement". For buying an India DID the requirement is:
            #   country=IN, number_type=local, end_user_type=business, op=buy_number
            # Discover its ``compliance_requirement_id`` from the catalog and pass
            # that to the application (the cleanest, version-stable path).
            country = (settings.PLIVO_NUMBER_COUNTRY or "IN").upper()
            number_type = "local"
            operation_type = "buy_number"
            requirement_id = record.get("compliance_requirement_id")
            if not requirement_id:
                try:
                    reqs = await PlivoService._request(
                        "GET",
                        f"{base}/ComplianceRequirement/?country_iso2={country}"
                        f"&number_type={number_type}&end_user_type=business"
                        f"&operation_type={operation_type}",
                        auth=auth,
                    )
                    cand = reqs.get("objects") or (
                        [reqs] if (reqs.get("compliance_requirement_id") or reqs.get("id")) else []
                    )
                    if cand:
                        requirement_id = str(
                            cand[0].get("compliance_requirement_id") or cand[0].get("id") or ""
                        ) or None
                except PlivoError:
                    requirement_id = None
            if requirement_id:
                record["compliance_requirement_id"] = requirement_id

            if not record.get("application_id"):
                app_body: dict[str, Any] = {
                    "end_user_id": end_user_id,
                    "end_user_type": "business",
                    "document_ids": [v for v in uploaded.values() if v],
                    "alias": legal_name[:120],
                }
                if requirement_id:
                    app_body["compliance_requirement_id"] = requirement_id
                else:
                    # Fallback: the explicit triple WITH operation_type so Plivo can
                    # resolve the requirement itself.
                    app_body["country_iso2"] = country
                    app_body["number_type"] = number_type
                    app_body["operation_type"] = operation_type
                app = await PlivoService._request(
                    "POST", f"{base}/ComplianceApplication/", auth=auth, json_body=app_body
                )
                record["application_id"] = str(app.get("compliance_application_id") or app.get("id") or "")
            record["status"] = "submitted"
            record.pop("error", None)

            # 4) Auto-allot a number (linked to the tenant's app + subaccount). India
            # DIDs may not be instantly rentable pre-approval → leave pending.
            if not number:
                try:
                    rented = await PlivoService.rent_number(
                        country=settings.PLIVO_NUMBER_COUNTRY,
                        app_id=app_id,
                        sub_auth_id=sub_auth_id,
                        compliance_application_id=record.get("application_id") or None,
                    )
                    number = rented.get("number")
                    number_status = "active" if number else "pending_compliance"
                except PlivoError as num_exc:
                    logger.info("PLIVO-COMPLIANCE: number not instantly rentable: %s", num_exc)
                    number_status = "pending_compliance"
        except PlivoError as exc:
            logger.warning("PLIVO-COMPLIANCE: submit failed: %s", exc)
            record["error"] = str(exc)[:300]

        record["number"] = number
        cls._save(tenant_res, compliance=record, number=number, number_status=number_status)
        await db.commit()
        return {"compliance": record, "number": number, "number_status": number_status}
