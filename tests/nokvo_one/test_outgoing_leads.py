from datetime import datetime, timezone

from app.models.outgoing_lead import LeadCallStatus, LeadConsentStatus, LeadSourceProvider, OutgoingLead
from app.services.outgoing_lead_service import lead_is_callable, normalize_lead_fields


def _lead(**overrides):
    base = {
        "tenant_id": "tenant-test",
        "source_provider": LeadSourceProvider.meta_ads,
        "phone_e164": "+919999999999",
        "consent_status": LeadConsentStatus.granted,
        "consented_at": datetime.now(timezone.utc),
        "call_status": LeadCallStatus.new,
    }
    base.update(overrides)
    return OutgoingLead(**base)


def test_only_granted_consent_lead_is_callable():
    assert lead_is_callable(_lead()) is True
    assert lead_is_callable(_lead(consent_status=LeadConsentStatus.unknown)) is False
    assert lead_is_callable(_lead(phone_e164="")) is False
    assert lead_is_callable(_lead(call_status=LeadCallStatus.opted_out)) is False
    assert lead_is_callable(_lead(opt_out_at=datetime.now(timezone.utc))) is False


def test_normalize_lead_fields_respects_mapping_and_common_names():
    fields = {
        "Full Name": "Asha Rao",
        "mobile_number": "9999999999",
        "email_address": "asha@example.com",
    }
    name, email, phone = normalize_lead_fields(fields)
    assert name == "Asha Rao"
    assert email == "asha@example.com"
    assert phone == "9999999999"

    mapped = {"customer": "Vikram", "contact": "+919888888888"}
    name, _, phone = normalize_lead_fields(mapped, {"name": "customer", "phone": "contact"})
    assert name == "Vikram"
    assert phone == "+919888888888"
