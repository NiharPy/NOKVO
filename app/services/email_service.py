from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from urllib.parse import urlencode

from app.core.config import settings


logger = logging.getLogger(__name__)


class EmailService:
    @staticmethod
    def _send_sync(to_email: str, subject: str, text_body: str, html_body: str | None = None) -> None:
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
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as client:
            if settings.SMTP_USE_TLS:
                client.starttls()
            if settings.SMTP_USERNAME:
                client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            client.send_message(message)

    @classmethod
    async def send(cls, to_email: str, subject: str, text_body: str, html_body: str | None = None) -> None:
        await asyncio.to_thread(cls._send_sync, to_email, subject, text_body, html_body)

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
        text = (
            f"Hi,\n\n"
            f"{invited_by} invited you to join {org_name} on Nokvo One.\n"
            f"Set up your account here:\n\n{url}\n\n"
            f"This invite expires in {settings.NOKVO_ONE_INVITE_TOKEN_TTL_HOURS} hours.\n"
        )
        await cls.send(to_email, f"You're invited to {org_name} on Nokvo One", text)
