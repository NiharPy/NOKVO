from __future__ import annotations

import asyncio

import jwt

from app.core.config import settings
from app.core.email_policy import normalize_email


GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class GoogleOAuthError(RuntimeError):
    """Caller-facing Google OAuth verification error. RuntimeError so the API
    layer's ``_safe_detail`` helper forwards the message instead of replacing
    it with a generic "Operation failed"."""

    pass


class GoogleOAuthService:
    _jwks_client = jwt.PyJWKClient(GOOGLE_JWKS_URL)

    @classmethod
    async def verify_id_token(cls, id_token: str) -> dict:
        if not settings.GOOGLE_OAUTH_CLIENT_ID:
            raise GoogleOAuthError("GOOGLE_OAUTH_CLIENT_ID is not configured")
        return await asyncio.to_thread(cls._verify_id_token_sync, id_token)

    @classmethod
    def _verify_id_token_sync(cls, id_token: str) -> dict:
        try:
            signing_key = cls._jwks_client.get_signing_key_from_jwt(id_token)
            payload = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=settings.GOOGLE_OAUTH_CLIENT_ID,
                issuer=list(GOOGLE_ISSUERS),
            )
        except jwt.PyJWKClientConnectionError as exc:
            raise GoogleOAuthError("Could not reach Google to verify sign-in. Check backend network access and try again.") from exc
        except jwt.PyJWKClientError as exc:
            raise GoogleOAuthError("Google sign-in key verification failed. Try again in a moment.") from exc
        except jwt.PyJWTError as exc:
            raise GoogleOAuthError("Google OAuth token verification failed") from exc

        email = payload.get("email")
        if not email:
            raise GoogleOAuthError("Google account email is missing from the token")
        if payload.get("email_verified") is not True:
            raise GoogleOAuthError("Google account email must be verified")

        return {
            "sub": payload.get("sub"),
            "email": normalize_email(email),
            "email_verified": True,
            "full_name": payload.get("name"),
            "given_name": payload.get("given_name"),
            "family_name": payload.get("family_name"),
            "hosted_domain": payload.get("hd"),
        }
