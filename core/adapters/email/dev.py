"""Development-only stand-ins, wired by the composition root only when ENV=dev.

* ``LogEmailSender`` writes the email to the API log so you can sign up locally without an
  SMTP account. It prints a one-time code, which is why it must never run in prod; the
  composition root refuses that.
* ``AllowAllHumanVerifier`` accepts any non-empty token, so local sign-up works without a
  Turnstile widget.
"""

from __future__ import annotations

import structlog

from core.domain.emails import OutgoingEmail

log = structlog.get_logger(__name__)


class LogEmailSender:
    async def send(self, email: OutgoingEmail) -> None:
        log.warning("dev_email_not_sent", subject=email.subject, body=email.text)


class AllowAllHumanVerifier:
    async def verify(self, token: str, ip: str | None) -> bool:
        return bool(token)
