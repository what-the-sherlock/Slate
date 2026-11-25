from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class AuthConfig(BaseModel):
    signups_enabled: bool = Field(description="False while sign-up is closed (kill switch).")
    turnstile_site_key: str | None = Field(
        description="Public Cloudflare Turnstile site key; None when no widget is needed (dev)."
    )


class SignupBody(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["Ada Lovelace"])
    email: str = Field(min_length=3, max_length=254, examples=["ada@acme.dev"])
    password: str = Field(min_length=1, max_length=256)
    turnstile_token: str = Field(default="", max_length=2048)
    setup_token: str | None = Field(default=None, max_length=256)
    # Honeypot: the form hides this field from people. Anything in it means a bot.
    website: str = Field(default="", max_length=256)


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=254, examples=["ada@acme.dev"])
    password: str = Field(min_length=1, max_length=256)


class VerifyBody(BaseModel):
    code: str = Field(min_length=1, max_length=12, examples=["042137"])


class ResendBody(BaseModel):
    turnstile_token: str = Field(default="", max_length=2048)


class ForgotPasswordBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    turnstile_token: str = Field(default="", max_length=2048)


class ResetPasswordBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=1, max_length=12)
    password: str = Field(min_length=1, max_length=256)


class MeUser(BaseModel):
    id: UUID
    email: str
    name: str


class MeResponse(BaseModel):
    user: MeUser
    workspace_id: UUID
    role: Literal["owner", "admin", "member", "viewer"]
    email_verified: bool
    is_platform_admin: bool
