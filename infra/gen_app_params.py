#!/usr/bin/env python3
"""Generate Azure Container Apps deployment inputs from a prod env file.

Two outputs (one place owns the secret-vs-plain classification):

  --mode params       → writes a Bicep params JSON for app.bicep (appEnv +
                        secretsKv arrays + scalar params). Secret *values* are
                        never written here — only secretRef names + KV URIs.
  --mode kv-script    → prints `az keyvault secret set ...` lines for every
                        secret key present in the env file (values come from
                        the env file), for deploy.sh to eval.

Usage (see deploy.sh): this is glue, not a library.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

# Keys whose values are sensitive → stored in Key Vault and referenced by the
# container as secretRefs. Everything else in the env file becomes a plain env
# var. Keep this list authoritative.
SECRET_KEYS = {
    "POSTGRES_PASSWORD",
    "SECRET_KEY",
    "SUPERADMIN_JWT_SECRET_KEY",
    "ORGANIZATION_JWT_SECRET_KEY",
    "NOKVO_ONE_SETUP_JWT_SECRET_KEY",
    "NOKVO_TOTP_ENCRYPTION_KEY",
    "REDIS_URL",  # contains the cache access key
    "SARVAM_API_KEY",
    "SONIOX_API_KEY",
    "PLIVO_AUTH_ID",
    "PLIVO_AUTH_TOKEN",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "AZURE_OPENAI_POOL_JSON",
    "AZURE_OPENAI_NANO_POOL_JSON",
    "AZURE_OPENAI_GLOBAL_API_KEY",
    "OPENAI_API_KEY",
    "META_ADS_APP_SECRET",
    "META_LEADGEN_WEBHOOK_VERIFY_TOKEN",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "ZOHO_CLIENT_SECRET",
    "QDRANT_API_KEY",
    "SMTP_PASSWORD",
    "LANGSMITH_API_KEY",
    "TWILIO_AUTH_TOKEN",
    "TELNYX_API_KEY",
}

# Keys to drop from the deployed env entirely: managed-identity replaces the
# service-principal secret, and Qdrant/KB is retired.
DROP_KEYS = {"AZURE_CLIENT_SECRET"}


def _kebab(key: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", key.lower().replace("_", "-"))


def parse_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                out[key] = val
    return out


def build(args) -> tuple[list, list]:
    """Return (appEnv, secretsKv) honoring overrides + secret classification."""
    env = parse_env(args.env_file)

    # Hard overrides for the prod runtime (win over whatever is in the file).
    overrides = {
        "ENVIRONMENT": "production",
        "AZURE_PREFER_MANAGED_IDENTITY": "true",
        "AZURE_MANAGED_IDENTITY_CLIENT_ID": args.identity_client_id,
        "POSTGRES_SERVER": args.pg_fqdn,
        "AZURE_SHARED_STORAGE_ACCOUNT": args.storage_account,
        "AZURE_SHARED_KEY_VAULT_NAME": args.kv_name,
        "AGENT_PUBLIC_BASE_URL": args.api_base,
        "PLIVO_WEBHOOK_BASE_URL": args.api_base,
        "NOKVO_ONE_PUBLIC_BASE_URL": args.portal_base,
        "EXPECTED_ORIGIN": args.portal_base,
        "RP_ID": args.rp_id,
        "OTEL_ENABLED": "true",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": args.appinsights_conn,
        "QDRANT_URL": ":memory:",
    }
    env.update(overrides)
    # REDIS_URL is a secret (carries the key) and is injected by deploy.sh into
    # KV directly, so don't surface it as a plain value here.
    env.pop("REDIS_URL", None)

    app_env: list[dict] = []
    secrets_kv: list[dict] = []
    seen_secret_refs: set[str] = set()

    def add_secret_ref(key: str) -> None:
        ref = _kebab(key)
        if ref not in seen_secret_refs:
            seen_secret_refs.add(ref)
            secrets_kv.append({
                "name": ref,
                "kvSecretUri": f"{args.kv_uri.rstrip('/')}/secrets/{ref}",
            })
        app_env.append({"name": key, "secretRef": ref})

    for key, val in env.items():
        if key in DROP_KEYS:
            continue
        if key in SECRET_KEYS:
            add_secret_ref(key)
        else:
            app_env.append({"name": key, "value": val})

    # REDIS_URL always present as a secretRef (value set in KV by deploy.sh).
    add_secret_ref("REDIS_URL")
    return app_env, secrets_kv


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["params", "kv-script"], required=True)
    p.add_argument("--env-file", required=True)
    # platform outputs / deploy context (required for params mode)
    p.add_argument("--image", default="")
    p.add_argument("--identity-id", default="")
    p.add_argument("--identity-client-id", default="")
    p.add_argument("--managed-env-id", default="")
    p.add_argument("--acr-login-server", default="")
    p.add_argument("--kv-uri", default="")
    p.add_argument("--kv-name", default="")
    p.add_argument("--pg-fqdn", default="")
    p.add_argument("--storage-account", default="")
    p.add_argument("--appinsights-conn", default="")
    p.add_argument("--api-base", default="")
    p.add_argument("--portal-base", default="")
    p.add_argument("--rp-id", default="")
    p.add_argument("--concurrent-requests", type=int, default=10)
    p.add_argument("--out", default="app.params.json")
    args = p.parse_args()

    if args.mode == "kv-script":
        env = parse_env(args.env_file)
        for key in sorted(SECRET_KEYS):
            if key in env and env[key]:
                # Single-quote the value for the shell; escape embedded quotes.
                safe = env[key].replace("'", "'\\''")
                print(f"az keyvault secret set --vault-name \"$KV_NAME\" "
                      f"--name {_kebab(key)} --value '{safe}' --output none")
        return 0

    app_env, secrets_kv = build(args)
    params = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "image": {"value": args.image},
            "identityId": {"value": args.identity_id},
            "managedEnvironmentId": {"value": args.managed_env_id},
            "acrLoginServer": {"value": args.acr_login_server},
            "concurrentRequests": {"value": args.concurrent_requests},
            "appEnv": {"value": app_env},
            "secretsKv": {"value": secrets_kv},
        },
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(params, fh, indent=2)
    print(f"wrote {args.out}: {len(app_env)} env vars, {len(secrets_kv)} kv secret refs",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
