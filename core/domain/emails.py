"""Transactional email content (pure): a plain-text part and a matching HTML part.

A code instead of a link: corporate mail scanners pre-open links (burning one-time
tokens), and a code never lands in URLs, browser history or proxy logs.

The HTML follows the site (UI_DESIGN §2 tokens): the logo tile, Inter-first type, stone
neutrals and the brand blue. Email clients ignore stylesheets, web fonts and SVG, so it is
table-based with inline styles and the logo is a PNG served by the web app. Every value a
person typed (names, workspace names) is HTML-escaped. The plain-text part is always sent
too, for clients that block HTML.
"""

# ruff: noqa: E501  (the HTML template reads better unwrapped; inline styles are long)

from __future__ import annotations

import html
from dataclasses import dataclass

from core.domain.auth import CODE_TTL, CodePurpose

LOGO_PATH = "/brand/email-logo.png"  # web/public/brand/email-logo.png (96 px, shown at 28)

# UI_DESIGN §2.1 tokens (light): email has no CSS variables, so the values are inlined.
_BG = "#fafaf9"
_CARD = "#ffffff"
_BORDER = "#e7e5e4"
_FG = "#1c1917"
_BODY = "#292524"
_MUTED = "#57534e"
_SUBTLE = "#78716c"
_ACCENT = "#3050f0"
_SANS = "Inter,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO = "'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


@dataclass(frozen=True, slots=True)
class OutgoingEmail:
    to: str
    subject: str
    text: str
    html: str | None = None


def code_email(
    to: str, name: str, code: str, purpose: CodePurpose, *, app_url: str | None = None
) -> OutgoingEmail:
    minutes = int(CODE_TTL.total_seconds() // 60)
    first = (name.split() or [""])[0][:40]  # first name, like Home ("Welcome back, Ada")
    greeting = f"Hi {first}," if first else "Hi,"
    if purpose is CodePurpose.VERIFY_EMAIL:
        subject = f"{code} is your Slate verification code"
        heading = "Verify your email"
        action = "Enter this code in Slate to verify your email address:"
        ignore = "If you didn't create a Slate account, you can ignore this email."
    else:
        subject = f"{code} is your Slate password reset code"
        heading = "Reset your password"
        action = "Enter this code in Slate to choose a new password:"
        ignore = "If you didn't ask to reset your password, ignore this email; nothing changes."
    expiry = f"It expires in {minutes} minutes and works once."
    text = f"{greeting}\n\n{action}\n\n    {code}\n\n{expiry}\n\n{ignore}\n\n— Slate\n"
    body = (
        _p(html.escape(greeting))
        + _p(html.escape(action))
        + _code_block(code)
        + _p(html.escape(expiry), color=_MUTED, size=13)
    )
    page = _layout(
        preheader=f"{code} · {expiry}",
        heading=heading,
        body=body,
        footnote=html.escape(ignore),
        app_url=app_url,
    )
    return OutgoingEmail(to=to, subject=subject, text=text, html=page)


def invite_email_html(
    *, inviter: str, workspace: str, role: str, link: str, days: int, app_url: str | None
) -> str:
    who, ws = html.escape(inviter), html.escape(workspace)
    body = (
        _p(f"<strong style=\"color:{_FG};\">{who}</strong> invited you to join "
           f"<strong style=\"color:{_FG};\">{ws}</strong> on Slate as {html.escape(role)}.")
        + _button("Accept invitation", link)
        + _p(f"The invitation expires in {days} days. Only sources people choose to share "
             "with the workspace are visible to its members; everything you connect stays "
             "private to you unless you share it.", color=_MUTED, size=13)
        + _p("Button not working? Paste this link into your browser:", color=_SUBTLE, size=12)
        + _p(f"<span style=\"word-break:break-all;\">{html.escape(link)}</span>",
             color=_SUBTLE, size=12)
    )  # fmt: skip
    return _layout(
        preheader=f"{inviter} invited you to {workspace} on Slate",
        heading="You're invited",
        body=body,
        footnote="If you didn't expect this, ignore this email.",
        app_url=app_url,
    )


# ------------------------------------------------------------------------- layout


def _p(inner: str, *, color: str = _BODY, size: int = 15) -> str:
    return (
        f'<p style="margin:0 0 16px;font-family:{_SANS};font-size:{size}px;'
        f'line-height:1.6;color:{color};">{inner}</p>'
    )


def _code_block(code: str) -> str:
    # One cell, big tabular mono digits with wide tracking: easy to read and to copy.
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:4px 0 20px;"><tr>'
        f'<td style="background:{_BG};border:1px solid {_BORDER};border-radius:8px;'
        f"padding:14px 22px;font-family:{_MONO};font-size:30px;font-weight:600;"
        f'letter-spacing:8px;color:{_FG};">{html.escape(code)}</td>'
        "</tr></table>"
    )


def _button(label: str, href: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:4px 0 20px;"><tr>'
        f'<td style="background:{_ACCENT};border-radius:8px;">'
        f'<a href="{html.escape(href, quote=True)}" style="display:inline-block;'
        f"padding:11px 20px;font-family:{_SANS};font-size:14px;font-weight:600;"
        f'color:#ffffff;text-decoration:none;">{html.escape(label)}</a>'
        "</td></tr></table>"
    )


def _brand(app_url: str | None) -> str:
    word = (
        f'<span style="font-family:{_SANS};font-size:17px;font-weight:600;'
        f'letter-spacing:-0.01em;color:{_FG};vertical-align:middle;">Slate</span>'
    )
    if not app_url:
        return word
    src = html.escape(app_url.rstrip("/") + LOGO_PATH, quote=True)
    return (
        f'<img src="{src}" width="28" height="28" alt="" '
        'style="display:inline-block;vertical-align:middle;border:0;margin-right:8px;'
        'border-radius:7px;">' + word
    )


def _layout(*, preheader: str, heading: str, body: str, footnote: str, app_url: str | None) -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
<title>{html.escape(heading)}</title>
</head>
<body style="margin:0;padding:0;background:{_BG};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{_BG};">{html.escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{_BG};">
<tr><td align="center" style="padding:40px 16px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:480px;">
    <tr><td style="padding:0 4px 20px;">{_brand(app_url)}</td></tr>
    <tr><td style="background:{_CARD};border:1px solid {_BORDER};border-radius:12px;padding:32px;">
      <h1 style="margin:0 0 20px;font-family:{_SANS};font-size:22px;line-height:1.3;font-weight:600;letter-spacing:-0.02em;color:{_FG};">{html.escape(heading)}</h1>
      {body}
    </td></tr>
    <tr><td style="padding:20px 4px 0;font-family:{_SANS};font-size:12px;line-height:1.6;color:{_SUBTLE};">
      {footnote}<br>Slate · Ask your own documents.
    </td></tr>
  </table>
</td></tr>
</table>
</body></html>
"""
