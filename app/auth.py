import logging

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

from . import db
from .config import settings

log = logging.getLogger("auth")

_jwks_client = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(
            f"https://{settings.CF_TEAM_DOMAIN}/cdn-cgi/access/certs"
        )
    return _jwks_client


def get_current_user(request: Request) -> dict:
    """Resolve the current user.

    Production: verify the Cloudflare Access JWT (Google IdP) from the
    Cf-Access-Jwt-Assertion header or CF_Authorization cookie.
    Dev mode (CF vars unset): single local user, overridable via X-Dev-User.
    """
    if not settings.cloudflare_enabled:
        email = request.headers.get("X-Dev-User", "dev@localhost")
        return db.get_or_create_user(email=email, name=email.split("@")[0])

    token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get(
        "CF_Authorization"
    )
    if not token:
        raise HTTPException(status_code=401, detail="Missing Cloudflare Access credentials")

    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.CF_ACCESS_AUD,
        )
    except jwt.PyJWTError as exc:
        log.warning("Cloudflare Access token verification failed: %s", exc)
        raise HTTPException(status_code=403, detail="Invalid Cloudflare Access token")

    email = payload.get("email") or request.headers.get("Cf-Access-Authenticated-User-Email")
    if not email:
        raise HTTPException(status_code=403, detail="No email present in Access token")
    return db.get_or_create_user(email=email, name=email.split("@")[0])
