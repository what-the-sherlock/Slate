"""EmailSender over SMTP (stdlib ``smtplib``; no vendor SDK, no new dependency).

Works with any relay: Brevo's free tier (300/day, no card) or a Gmail app password.
TLS is always on (STARTTLS on 587 or implicit SSL on 465) with certificate checks.

Not retried: sending isn't idempotent (a retry after a timeout can deliver twice), and the
user can press "Send a new code". Failures become typed errors. Neither the body (it holds
a one-time code) nor the recipient is ever logged.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import structlog

from core.domain.emails import OutgoingEmail
from core.domain.errors import RateLimited, UpstreamUnavailable

log = structlog.get_logger(__name__)
TIMEOUT_S = 15.0


class SmtpEmailSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        security: str,
        username: str | None,
        password: str | None,
        sender: str,
    ) -> None:
        self._host = host
        self._port = port
        self._ssl = security == "ssl"
        self._username = username
        self._password = password
        self._sender = sender

    async def send(self, email: OutgoingEmail) -> None:
        message = _build(self._sender, email)
        await asyncio.to_thread(self._send_blocking, message)

    def _send_blocking(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        try:
            if self._ssl:
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    self._host, self._port, timeout=TIMEOUT_S, context=context
                )
            else:
                client = smtplib.SMTP(self._host, self._port, timeout=TIMEOUT_S)
            with client:
                if not self._ssl:
                    client.starttls(context=context)
                if self._username and self._password:
                    client.login(self._username, self._password)
                client.send_message(message)
        except smtplib.SMTPResponseException as exc:
            # 421/450/451/452 are temporary (often a provider's sending quota).
            log.warning("email.smtp_refused", code=exc.smtp_code)
            if exc.smtp_code in {421, 450, 451, 452}:
                raise RateLimited("The mail service is busy. Try again shortly.") from None
            raise UpstreamUnavailable("We couldn't send the email. Try again shortly.") from None
        except (smtplib.SMTPException, OSError, UnicodeError) as exc:
            log.warning("email.smtp_failed", error=type(exc).__name__)
            raise UpstreamUnavailable("We couldn't send the email. Try again shortly.") from None


def _build(sender: str, email: OutgoingEmail) -> EmailMessage:
    # Header injection: EmailMessage refuses CR/LF in header values; the recipient was
    # validated as an address with no whitespace, and the user's name goes only in the body.
    if any(c in email.to for c in "\r\n,;"):
        raise ValueError("invalid recipient")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = email.to
    message["Subject"] = email.subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain="slate.local")
    message["Auto-Submitted"] = "auto-generated"  # RFC 3834: don't auto-reply to this
    message.set_content(email.text)
    if email.html:  # multipart/alternative: clients that block HTML show the text part
        message.add_alternative(email.html, subtype="html")
    return message
