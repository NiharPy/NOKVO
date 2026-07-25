from __future__ import annotations

import asyncio
import datetime
import functools
import html
import logging
import pathlib
import re
import smtplib
from email.message import EmailMessage
from urllib.parse import urlencode

from app.core.config import settings


logger = logging.getLogger(__name__)

# Black-and-white, boxy ("neobrutalist") theme — mirrors the NOKVO landing
# page: hard black rules, square corners, heavy type, an offset hard-shadow
# button. Strictly monochrome so it reads as part of the product.
_BLACK = "#0a0a0a"
_INK = "#111111"
_MUTED = "#555555"
_FAINT = "#8a8a8a"
_LINE = "#111111"          # strong structural borders
_HAIRLINE = "#e3e3e3"      # subtle separators
_CANVAS = "#f4f4f5"
_CARD = "#ffffff"

# The brand mark is embedded inline (Content-ID) rather than hot-linked so it
# renders without the recipient having to "load remote images", and survives
# even if the portal host is unreachable.
_LOGO_CID = "nokvo-logo"
_LOGO_PATH = pathlib.Path(__file__).resolve().parent.parent / "assets" / "email" / "nokvo-logo.png"


@functools.lru_cache(maxsize=1)
def _logo_bytes() -> bytes | None:
    try:
        return _LOGO_PATH.read_bytes()
    except OSError:
        logger.warning("Brand logo not found at %s — email will fall back to the wordmark", _LOGO_PATH)
        return None


class EmailService:
    @staticmethod
    def _branded_email_html(
        *,
        preheader: str,
        eyebrow: str,
        heading: str,
        intro_html: str,
        cta_label: str,
        cta_url: str,
        note_html: str | None = None,
        footer_html: str | None = None,
    ) -> str:
        """Render a responsive, client-safe HTML email (B&W, boxy).

        Table layout + inline styles (Gmail/Outlook/Apple Mail strip <style>
        and flexbox). The CTA uses a hard offset shadow via ``box-shadow`` —
        clients that ignore it (Outlook desktop) still show a solid bordered
        black button, on-theme. The raw URL is shown for clients that block
        the button, and the logo is referenced by ``cid:`` (inline image).
        """
        year = datetime.datetime.now().year
        logo_img = (
            f'<img src="cid:{_LOGO_CID}" alt="" width="31" height="22" '
            f'style="display:inline-block;height:22px;width:auto;border:0;vertical-align:middle;margin-right:10px;">'
            if _logo_bytes() is not None
            else ""
        )
        note_block = (
            f'<tr><td style="padding:0 36px 8px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'<tr><td style="border:1.5px solid {_LINE};padding:11px 14px;background:{_CANVAS};">'
            f'<p style="margin:0;font:600 12px/18px Helvetica,Arial,sans-serif;letter-spacing:.02em;color:{_INK};text-transform:uppercase;">{note_html}</p>'
            f"</td></tr></table></td></tr>"
            if note_html
            else ""
        )
        footer_block = footer_html or (
            "If you weren’t expecting this invitation, you can safely ignore this email."
        )
        return f"""\
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light">
<title>{html.escape(eyebrow)}</title></head>
<body style="margin:0;padding:0;background:{_CANVAS};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{html.escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_CANVAS};">
  <tr><td align="center" style="padding:36px 16px;">
    <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="width:480px;max-width:100%;">
      <tr><td style="background:{_CARD};border:2px solid {_LINE};">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <!-- masthead -->
          <tr><td style="padding:20px 36px;border-bottom:2px solid {_LINE};">
            {logo_img}<span style="font:800 18px/1 Helvetica,Arial,sans-serif;letter-spacing:.12em;color:{_INK};vertical-align:middle;">NOKVO</span>
          </td></tr>
          <!-- body -->
          <tr><td style="padding:34px 36px 4px;">
            <span style="display:inline-block;border:1.5px solid {_LINE};padding:4px 9px;font:700 10px/1 Helvetica,Arial,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:{_INK};">{html.escape(eyebrow)}</span>
          </td></tr>
          <tr><td style="padding:16px 36px 0;">
            <h1 style="margin:0;font:800 28px/32px Helvetica,Arial,sans-serif;letter-spacing:-.01em;color:{_INK};">{html.escape(heading)}</h1>
          </td></tr>
          <tr><td style="padding:14px 36px 0;">
            <p style="margin:0;font:400 15px/24px Helvetica,Arial,sans-serif;color:{_MUTED};">{intro_html}</p>
          </td></tr>
          <!-- CTA: offset hard-shadow button -->
          <tr><td style="padding:26px 36px 10px;">
            <table role="presentation" cellpadding="0" cellspacing="0"><tr>
              <td style="background:{_BLACK};border:2px solid {_LINE};box-shadow:6px 6px 0 {_LINE};">
                <a href="{html.escape(cta_url, quote=True)}" target="_blank"
                   style="display:inline-block;padding:14px 28px;font:800 14px/1 Helvetica,Arial,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:#ffffff;text-decoration:none;">
                  {html.escape(cta_label)}&nbsp;&rarr;
                </a>
              </td>
            </tr></table>
          </td></tr>
          {note_block}
          <!-- fallback link -->
          <tr><td style="padding:18px 36px 0;">
            <p style="margin:0 0 6px;font:600 11px/16px Helvetica,Arial,sans-serif;letter-spacing:.06em;text-transform:uppercase;color:{_FAINT};">Or paste this link into your browser</p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
              <td style="border:1px solid {_HAIRLINE};background:{_CANVAS};padding:10px 12px;">
                <span style="font:400 12px/18px 'SFMono-Regular',Menlo,Consolas,monospace;color:{_INK};word-break:break-all;">{html.escape(cta_url)}</span>
              </td>
            </tr></table>
          </td></tr>
          <tr><td style="padding:26px 36px 30px;">
            <div style="height:2px;background:{_LINE};font-size:0;line-height:0;">&nbsp;</div>
            <p style="margin:16px 0 0;font:400 13px/20px Helvetica,Arial,sans-serif;color:{_MUTED};">{footer_block}</p>
          </td></tr>
        </table>
      </td></tr>
      <tr><td style="padding:16px 4px 4px;">
        <p style="margin:0;font:600 11px/18px Helvetica,Arial,sans-serif;letter-spacing:.04em;color:{_FAINT};text-transform:uppercase;">© {year} NOKVO ONE · Voice agents for your business</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""

    @staticmethod
    def _text_to_paragraphs_html(text: str) -> str:
        """Turn an admin-authored plain-text body into safe HTML paragraphs.

        Blank lines split paragraphs; single newlines become <br>. Everything
        is HTML-escaped so a broadcast can never inject markup.
        """
        blocks = [b.strip() for b in re.split(r"\n\s*\n", (text or "").strip()) if b.strip()]
        out = []
        for block in blocks:
            safe = html.escape(block).replace("\n", "<br>")
            out.append(
                f'<p style="margin:0 0 14px;font:400 15px/24px Helvetica,Arial,sans-serif;color:{_MUTED};">{safe}</p>'
            )
        return "".join(out)

    @staticmethod
    def _branded_message_html(
        *,
        preheader: str,
        eyebrow: str,
        heading: str,
        body_html: str,
        cta_label: str | None = None,
        cta_url: str | None = None,
        footer_html: str | None = None,
    ) -> str:
        """Same B&W boxy shell as the invite, but for a free-form announcement.

        The CTA + fallback-link blocks render only when ``cta_url`` is given, so
        a plain broadcast (no link) still looks on-theme.
        """
        year = datetime.datetime.now().year
        logo_img = (
            f'<img src="cid:{_LOGO_CID}" alt="" width="31" height="22" '
            f'style="display:inline-block;height:22px;width:auto;border:0;vertical-align:middle;margin-right:10px;">'
            if _logo_bytes() is not None
            else ""
        )
        cta_block = ""
        fallback_block = ""
        if cta_url:
            cta_block = (
                f'<tr><td style="padding:26px 36px 10px;">'
                f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
                f'<td style="background:{_BLACK};border:2px solid {_LINE};box-shadow:6px 6px 0 {_LINE};">'
                f'<a href="{html.escape(cta_url, quote=True)}" target="_blank" '
                f'style="display:inline-block;padding:14px 28px;font:800 14px/1 Helvetica,Arial,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:#ffffff;text-decoration:none;">'
                f'{html.escape(cta_label or "Open")}&nbsp;&rarr;</a>'
                f"</td></tr></table></td></tr>"
            )
            fallback_block = (
                f'<tr><td style="padding:18px 36px 0;">'
                f'<p style="margin:0 0 6px;font:600 11px/16px Helvetica,Arial,sans-serif;letter-spacing:.06em;text-transform:uppercase;color:{_FAINT};">Or paste this link into your browser</p>'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                f'<td style="border:1px solid {_HAIRLINE};background:{_CANVAS};padding:10px 12px;">'
                f"<span style=\"font:400 12px/18px 'SFMono-Regular',Menlo,Consolas,monospace;color:{_INK};word-break:break-all;\">{html.escape(cta_url)}</span>"
                f"</td></tr></table></td></tr>"
            )
        footer_block = footer_html or (
            "You’re receiving this because your organization uses Nokvo One."
        )
        return f"""\
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light">
<title>{html.escape(eyebrow)}</title></head>
<body style="margin:0;padding:0;background:{_CANVAS};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{html.escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_CANVAS};">
  <tr><td align="center" style="padding:36px 16px;">
    <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="width:480px;max-width:100%;">
      <tr><td style="background:{_CARD};border:2px solid {_LINE};">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <tr><td style="padding:20px 36px;border-bottom:2px solid {_LINE};">
            {logo_img}<span style="font:800 18px/1 Helvetica,Arial,sans-serif;letter-spacing:.12em;color:{_INK};vertical-align:middle;">NOKVO</span>
          </td></tr>
          <tr><td style="padding:34px 36px 4px;">
            <span style="display:inline-block;border:1.5px solid {_LINE};padding:4px 9px;font:700 10px/1 Helvetica,Arial,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:{_INK};">{html.escape(eyebrow)}</span>
          </td></tr>
          <tr><td style="padding:16px 36px 0;">
            <h1 style="margin:0;font:800 28px/32px Helvetica,Arial,sans-serif;letter-spacing:-.01em;color:{_INK};">{html.escape(heading)}</h1>
          </td></tr>
          <tr><td style="padding:14px 36px 0;">
            {body_html}
          </td></tr>
          {cta_block}
          {fallback_block}
          <tr><td style="padding:26px 36px 30px;">
            <div style="height:2px;background:{_LINE};font-size:0;line-height:0;">&nbsp;</div>
            <p style="margin:16px 0 0;font:400 13px/20px Helvetica,Arial,sans-serif;color:{_MUTED};">{footer_block}</p>
          </td></tr>
        </table>
      </td></tr>
      <tr><td style="padding:16px 4px 4px;">
        <p style="margin:0;font:600 11px/18px Helvetica,Arial,sans-serif;letter-spacing:.04em;color:{_FAINT};text-transform:uppercase;">© {year} NOKVO ONE · Voice agents for your business</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""

    @staticmethod
    def _send_sync(
        to_email: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
        inline_images: list[tuple[str, bytes, str]] | None = None,
    ) -> None:
        if not settings.SMTP_HOST:
            logger.info(
                "SMTP not configured — would have sent email to %s subject=%r body=%r",
                to_email,
                subject,
                text_body,
            )
            return
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        message["To"] = to_email
        message.set_content(text_body)
        if html_body:
            message.add_alternative(html_body, subtype="html")
            if inline_images:
                # Attach each image as a related part of the HTML alternative so
                # `cid:` references resolve inline (no remote-image blocking).
                html_part = message.get_payload()[-1]
                for cid, data, subtype in inline_images:
                    html_part.add_related(data, maintype="image", subtype=subtype, cid=f"<{cid}>")
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as client:
            if settings.SMTP_USE_TLS:
                client.starttls()
            if settings.SMTP_USERNAME:
                client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            client.send_message(message)

    @classmethod
    async def send(
        cls,
        to_email: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
        inline_images: list[tuple[str, bytes, str]] | None = None,
    ) -> None:
        await asyncio.to_thread(cls._send_sync, to_email, subject, text_body, html_body, inline_images)

    @staticmethod
    def _brand_inline_images() -> list[tuple[str, bytes, str]]:
        """Inline images referenced by the branded template (the logo)."""
        data = _logo_bytes()
        return [(_LOGO_CID, data, "png")] if data is not None else []

    @staticmethod
    def build_verification_url(token: str) -> str:
        base = settings.NOKVO_ONE_PUBLIC_BASE_URL.rstrip("/")
        return f"{base}/nokvo-one/verify-email?{urlencode({'token': token})}"

    @staticmethod
    def build_invitation_url(token: str) -> str:
        base = settings.NOKVO_ONE_PUBLIC_BASE_URL.rstrip("/")
        return f"{base}/nokvo-one/accept-invite?{urlencode({'token': token})}"

    @classmethod
    async def send_verification_email(cls, to_email: str, admin_name: str | None, org_name: str, token: str) -> None:
        url = cls.build_verification_url(token)
        greeting = admin_name.strip() if admin_name and admin_name.strip() else "there"
        text = (
            f"Hi {greeting},\n\n"
            f"You started a Nokvo One sign-up for {org_name}.\n"
            f"Please verify your work email by opening this link:\n\n{url}\n\n"
            f"The link expires in {settings.NOKVO_ONE_EMAIL_TOKEN_TTL_HOURS} hours.\n\n"
            "If you did not request this, you can ignore this email."
        )
        await cls.send(to_email, "Verify your Nokvo One email", text)

    @classmethod
    async def send_member_invitation_email(
        cls,
        to_email: str,
        org_name: str,
        inviter_name: str | None,
        token: str,
    ) -> None:
        url = cls.build_invitation_url(token)
        invited_by = (inviter_name or "your team").strip() or "your team"
        ttl = settings.NOKVO_ONE_INVITE_TOKEN_TTL_HOURS
        subject = f"You're invited to {org_name} on Nokvo One"
        text = (
            f"Hi,\n\n"
            f"{invited_by} invited you to join {org_name} on Nokvo One.\n"
            f"Set up your account here:\n\n{url}\n\n"
            f"This invite expires in {ttl} hours.\n"
        )
        html_body = cls._branded_email_html(
            preheader=f"{invited_by} invited you to join {org_name} on Nokvo One.",
            eyebrow="Team invitation",
            heading=f"Join {org_name}",
            intro_html=(
                f"<strong style=\"color:{_INK};\">{html.escape(invited_by)}</strong> "
                f"has invited you to join <strong style=\"color:{_INK};\">{html.escape(org_name)}</strong> "
                f"on Nokvo One. Accept below to set up your account — you’ll create your "
                f"own password and two-factor authentication."
            ),
            cta_label="Accept invitation",
            cta_url=url,
            note_html=f"This invitation expires in {ttl} hours",
        )
        await cls.send(to_email, subject, text, html_body=html_body, inline_images=cls._brand_inline_images())

    @staticmethod
    def build_apex_invitation_url(token: str) -> str:
        base = settings.NOKVO_ONE_PUBLIC_BASE_URL.rstrip("/")
        return f"{base}/nokvo-apex/invite/{token}"

    @classmethod
    async def send_apex_member_invitation_email(
        cls,
        to_email: str,
        org_name: str,
        inviter_name: str | None,
        token: str,
    ) -> None:
        """APEX team invite — like the Nokvo One one, but the link lands on the APEX
        accept page and the copy is password-only (no two-factor step)."""
        url = cls.build_apex_invitation_url(token)
        invited_by = (inviter_name or "your team").strip() or "your team"
        ttl = settings.NOKVO_ONE_INVITE_TOKEN_TTL_HOURS
        subject = f"You're invited to {org_name} on NOKVO APEX"
        text = (
            f"Hi,\n\n"
            f"{invited_by} invited you to join {org_name} on NOKVO APEX.\n"
            f"Set up your account here:\n\n{url}\n\n"
            f"This invite expires in {ttl} hours.\n"
        )
        html_body = cls._branded_email_html(
            preheader=f"{invited_by} invited you to join {org_name} on NOKVO APEX.",
            eyebrow="Team invitation",
            heading=f"Join {org_name}",
            intro_html=(
                f"<strong style=\"color:{_INK};\">{html.escape(invited_by)}</strong> "
                f"has invited you to join <strong style=\"color:{_INK};\">{html.escape(org_name)}</strong> "
                f"on NOKVO APEX. Accept below to set up your account — you’ll create your own password."
            ),
            cta_label="Accept invitation",
            cta_url=url,
            note_html=f"This invitation expires in {ttl} hours",
        )
        await cls.send(to_email, subject, text, html_body=html_body, inline_images=cls._brand_inline_images())

    @classmethod
    async def send_apex_payment_link_email(
        cls,
        to_email: str,
        admin_name: str | None,
        org_name: str,
        plan_label: str,
        monthly_paise: int,
        payment_url: str,
    ) -> None:
        """APEX onboarding — email the Razorpay subscription payment link. Once paid, the
        account is activated by our team (within 6 hours; 24 hours for Free Trial)."""
        greeting = admin_name.strip() if admin_name and admin_name.strip() else "there"
        monthly_inr = f"₹{monthly_paise / 100:,.2f}"
        subject = f"Activate your NOKVO APEX account — {plan_label}"
        text = (
            f"Hi {greeting},\n\n"
            f"Your NOKVO APEX account for {org_name} is ready on the {plan_label} plan.\n"
            f"Complete your monthly subscription ({monthly_inr}/month) to get started:\n\n"
            f"{payment_url}\n\n"
            f"Once payment is received, your account is activated within 6 hours.\n"
        )
        html_body = cls._branded_email_html(
            preheader=f"Complete payment to activate {org_name} on NOKVO APEX.",
            eyebrow="Activate your account",
            heading=f"{plan_label} plan — {monthly_inr}/month",
            intro_html=(
                f"Your NOKVO APEX account for <strong style=\"color:{_INK};\">{html.escape(org_name)}</strong> "
                f"is ready. Complete your monthly subscription to activate — once payment is received, "
                f"your account goes live within 6 hours."
            ),
            cta_label="Complete payment",
            cta_url=payment_url,
            note_html="Questions? Just reply to this email.",
        )
        await cls.send(to_email, subject, text, html_body=html_body, inline_images=cls._brand_inline_images())

    @classmethod
    async def send_usage_invoice_email(
        cls,
        to_email: str,
        *,
        org_name: str,
        invoice_number: str,
        period_label: str,
        minutes: int,
        call_count: int,
        amount_display: str,
        dashboard_url: str,
    ) -> None:
        """Email a tenant their monthly usage invoice (B&W boxy, on-brand)."""
        subject = f"Your Nokvo One invoice · {amount_display} · {period_label}"
        # Itemized summary box, inline-styled so it survives Gmail/Outlook.
        rows = [
            ("Billing period", html.escape(period_label)),
            ("Calls", str(call_count)),
            ("Minutes used", f"{minutes:,}"),
        ]
        row_html = "".join(
            f'<tr>'
            f'<td style="padding:8px 0;border-bottom:1px solid {_HAIRLINE};font:400 13px/18px Helvetica,Arial,sans-serif;color:{_MUTED};">{label}</td>'
            f'<td style="padding:8px 0;border-bottom:1px solid {_HAIRLINE};font:600 13px/18px Helvetica,Arial,sans-serif;color:{_INK};text-align:right;">{value}</td>'
            f"</tr>"
            for label, value in rows
        )
        body_html = (
            f'<p style="margin:0 0 16px;font:400 15px/24px Helvetica,Arial,sans-serif;color:{_MUTED};">'
            f'Here is your usage invoice for <strong style="color:{_INK};">{html.escape(org_name)}</strong>, '
            f'covering the voice minutes used this billing cycle.</p>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1.5px solid {_LINE};background:{_CANVAS};">'
            f'<tr><td style="padding:14px 16px 4px;font:700 10px/1 Helvetica,Arial,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:{_FAINT};">'
            f'Invoice {html.escape(invoice_number)}</td></tr>'
            f'<tr><td style="padding:6px 16px 14px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{row_html}'
            f'<tr>'
            f'<td style="padding:12px 0 2px;font:800 15px/20px Helvetica,Arial,sans-serif;color:{_INK};text-transform:uppercase;letter-spacing:.04em;">Amount due</td>'
            f'<td style="padding:12px 0 2px;font:800 18px/20px Helvetica,Arial,sans-serif;color:{_INK};text-align:right;">{html.escape(amount_display)}</td>'
            f'</tr></table></td></tr></table>'
        )
        text = (
            f"Invoice {invoice_number} — {org_name}\n"
            f"Billing period: {period_label}\n"
            f"Calls: {call_count}\nMinutes used: {minutes:,}\n"
            f"Amount due: {amount_display}\n\n"
            f"View the full breakdown: {dashboard_url}\n\n— The Nokvo One team"
        )
        html_body = cls._branded_message_html(
            preheader=f"Usage invoice {amount_display} for {period_label}.",
            eyebrow="Invoice",
            heading=f"{amount_display} due",
            body_html=body_html,
            cta_label="View invoice",
            cta_url=dashboard_url,
            footer_html=(
                "This invoice covers metered voice usage for the period shown. "
                "Questions? Just reply to this email."
            ),
        )
        await cls.send(to_email, subject, text, html_body=html_body, inline_images=cls._brand_inline_images())

    @classmethod
    async def send_apex_support_ticket_email(
        cls,
        *,
        ticket_id: str,
        org_name: str,
        requester_email: str,
        subject: str,
        description: str,
        diagnosis_text: str,
    ) -> None:
        """Notify the support inbox of an APEX ticket raised via Nova. The
        diagnosis snapshot rides along so the operator sees the tenant's recent
        state without asking them to reproduce anything."""
        to_email = "officialnokvo@nokvo.org"
        mail_subject = f"[APEX Ticket #{ticket_id[:8]}] {subject}"
        body_text = (
            f"Organization: {org_name}\n"
            f"Raised by: {requester_email}\n\n"
            f"{description.strip()}\n\n"
            f"— Diagnosis snapshot —\n{diagnosis_text.strip() or '(none attached)'}"
        )
        html_body = cls._branded_message_html(
            preheader=f"APEX support ticket from {org_name}",
            eyebrow="APEX Support",
            heading=subject,
            body_html=cls._text_to_paragraphs_html(body_text),
            cta_label=None,
            cta_url=None,
        )
        await cls.send(to_email, mail_subject, body_text, html_body=html_body, inline_images=cls._brand_inline_images())

    @classmethod
    async def send_campaign_moderation_alert(
        cls,
        *,
        org_name: str,
        actor_email: str,
        campaign_name: str,
        category: str,
        reason: str,
        mode: str,
        content_snippet: str,
    ) -> None:
        """Notify the operator inbox that the campaign content guard flagged a
        create/edit (blocked under enforce, allowed-through under warn). The
        offending text rides along so repeat offenders are auditable from the
        email alone — there is deliberately no review queue."""
        to_email = "officialnokvo@nokvo.org"
        action = "BLOCKED" if mode == "enforce" else "flagged (warn mode — allowed through)"
        mail_subject = f"[Campaign moderation] {category}: '{campaign_name[:60]}' — {org_name[:60]}"
        body_text = (
            f"Organization: {org_name}\n"
            f"Submitted by: {actor_email}\n"
            f"Campaign: {campaign_name}\n"
            f"Verdict: {category} — {action}\n"
            f"Reason: {reason or '(none given)'}\n\n"
            f"— Campaign text snapshot —\n{content_snippet.strip() or '(empty)'}"
        )
        html_body = cls._branded_message_html(
            preheader=f"Campaign content guard: {category} from {org_name}",
            eyebrow="Campaign Moderation",
            heading=f"Campaign {action}",
            body_html=cls._text_to_paragraphs_html(body_text),
            cta_label=None,
            cta_url=None,
        )
        await cls.send(to_email, mail_subject, body_text, html_body=html_body, inline_images=cls._brand_inline_images())

    @classmethod
    async def send_broadcast_email(
        cls,
        to_email: str,
        *,
        subject: str,
        heading: str,
        body_text: str,
        eyebrow: str = "Announcement",
        cta_label: str | None = None,
        cta_url: str | None = None,
    ) -> None:
        """Send a SuperAdmin broadcast to one tenant, styled like the invite."""
        preheader = " ".join((body_text or "").split())[:140]
        link_line = f"\n\n{cta_label or 'Open'}: {cta_url}" if cta_url else ""
        text = f"{heading}\n\n{(body_text or '').strip()}{link_line}\n\n— The Nokvo One team"
        html_body = cls._branded_message_html(
            preheader=preheader or heading,
            eyebrow=eyebrow or "Announcement",
            heading=heading,
            body_html=cls._text_to_paragraphs_html(body_text),
            cta_label=cta_label,
            cta_url=cta_url,
        )
        await cls.send(to_email, subject, text, html_body=html_body, inline_images=cls._brand_inline_images())
