"""Transactional email content (pure). Plain text only: no HTML, no links to click.

A code instead of a link: corporate mail scanners pre-open links (burning one-time
tokens), and a code never lands in URLs, browser history or proxy logs.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.domain.auth import CODE_TTL, CodePurpose


@dataclass(frozen=True, slots=True)
class OutgoingEmail:
    to: str
    subject: str
    text: str


def code_email(to: str, name: str, code: str, purpose: CodePurpose) -> OutgoingEmail:
    minutes = int(CODE_TTL.total_seconds() // 60)
    greeting = f"Hi {name.strip()[:40]}," if name.strip() else "Hi,"
    if purpose is CodePurpose.VERIFY_EMAIL:
        subject = f"{code} is your Slate verification code"
        action = "Enter this code in Slate to verify your email address:"
        ignore = "If you didn't create a Slate account, you can ignore this email."
    else:
        subject = f"{code} is your Slate password reset code"
        action = "Enter this code in Slate to choose a new password:"
        ignore = "If you didn't ask to reset your password, ignore this email; nothing changes."
    text = (
        f"{greeting}\n\n{action}\n\n    {code}\n\n"
        f"It expires in {minutes} minutes and works once.\n\n{ignore}\n\n— Slate\n"
    )
    return OutgoingEmail(to=to, subject=subject, text=text)
