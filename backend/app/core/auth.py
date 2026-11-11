"""Single-user authentication.

* Browser UI: signed, HttpOnly, SameSite=Lax session cookie issued by POST /api/v1/auth/login.
* CSRF: every state-changing request authenticated by cookie must carry the header
  `X-JobWatch-Client: web`. Browsers cannot send custom headers cross-site without a CORS
  preflight, which we never grant to foreign origins.
* Bookmarklet: POST /api/v1/jobs/capture authenticates with `Authorization: Bearer <capture_token>`.

Generate a password hash:  uv run python -m app.core.auth hash-password
"""

import hmac
import sys
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import APIRouter, Depends, Request, Response
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.errors import Forbidden, Unauthorized

SESSION_COOKIE = "jobwatch_session"
CSRF_HEADER = "X-JobWatch-Client"
CSRF_VALUE = "web"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_hasher = PasswordHasher()


def _signer(settings: Settings) -> TimestampSigner:
    return TimestampSigner(settings.session_secret.get_secret_value(), salt="jobwatch-session")


def verify_password(settings: Settings, username: str, password: str) -> bool:
    expected_hash = settings.admin_password_hash.get_secret_value()
    if not expected_hash or not hmac.compare_digest(username, settings.admin_username):
        return False
    try:
        return _hasher.verify(expected_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def current_user(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> str:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise Unauthorized("Not signed in")
    try:
        username = _signer(settings).unsign(token, max_age=settings.session_max_age_days * 86400).decode()
    except (BadSignature, SignatureExpired) as exc:
        raise Unauthorized("Session expired") from exc
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise Forbidden(f"Missing {CSRF_HEADER} header", code="csrf")
    return username


def capture_client(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> str:
    """Accepts either the bookmarklet bearer token or a normal UI session."""
    expected = settings.capture_token.get_secret_value()
    auth = request.headers.get("Authorization", "")
    if expected and auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], expected):
        return "capture"
    return current_user(request, settings)


CurrentUser = Annotated[str, Depends(current_user)]
CaptureClient = Annotated[str, Depends(capture_client)]


class LoginRequest(BaseModel):
    username: str
    password: str


class Me(BaseModel):
    username: str


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=Me)
def login(body: LoginRequest, response: Response, settings: Annotated[Settings, Depends(get_settings)]) -> Me:
    if not verify_password(settings, body.username, body.password):
        raise Unauthorized("Invalid username or password", code="invalid_credentials")
    token = _signer(settings).sign(body.username.encode()).decode()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_max_age_days * 86400,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )
    return Me(username=body.username)


@router.post("/logout", status_code=204)
def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=Me)
def me(user: CurrentUser) -> Me:
    return Me(username=user)


if __name__ == "__main__" and sys.argv[1:] == ["hash-password"]:
    import getpass

    print(_hasher.hash(getpass.getpass("Password: ")))
