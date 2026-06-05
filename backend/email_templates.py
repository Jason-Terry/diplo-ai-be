"""Shared HTML email layout for MetisDolos transactional mail.

Every transactional email (verify, password reset, future BYOK-required
notices, etc.) goes through `render_card_email` so branding, colors, and
the dark-mode-aware shell stay consistent. Inline styles only — email
clients strip <style> tags and have wildly different CSS support.

The palette mirrors the FE's parchment theme (warm cream + sepia text +
deep cyan-blue accent). The serif used in the FE is Libre Baskerville;
in email we fall back to Georgia/Times because most clients don't load
web fonts.
"""

from __future__ import annotations

from html import escape
from typing import Optional


# Colors lifted from /Users/jterry/dev/diplo-ai-fe/src/app.css [data-theme='parchment']
_BG = "#ece1c2"          # warm cream backdrop
_SURFACE = "#f6ecd0"     # card surface
_BORDER = "#c5b893"      # parchment border
_FG = "#3c2e1e"          # deep walnut text
_FG_MUTED = "#6b5a42"
_FG_DIM = "#8a7c63"
_ACCENT = "#1f6f9f"      # deep cyan-blue (CTA)
_ACCENT_FG = "#ffffff"
_DANGER = "#c1432f"      # italic word in hero / hover

_FONT_SERIF = "'Libre Baskerville', 'Georgia', 'Times New Roman', serif"
_FONT_SANS = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"


def render_card_email(
    *,
    preheader: str,
    heading: str,
    intro_html: str,
    cta_label: Optional[str] = None,
    cta_url: Optional[str] = None,
    after_cta_html: str = "",
    footer_html: Optional[str] = None,
) -> str:
    """Build a one-card transactional email.

    Args:
        preheader: Hidden text shown in the inbox preview (after the
            subject). Should be a short one-liner that complements the
            subject — clients show ~90 chars.
        heading: Big serif heading at the top of the card. Plain text;
            escaped automatically.
        intro_html: Body paragraph(s) above the CTA. HTML is passed
            through as-is — callers should escape any user input first.
        cta_label: Button label. If omitted, no button is rendered.
        cta_url: Button href. Required when cta_label is set.
        after_cta_html: Optional paragraph(s) below the button (e.g.
            "if the button doesn't work, paste this link …").
        footer_html: Override for the footer text. Defaults to the
            standard disclaimer.

    Returns:
        Full <html>…</html> document, ready to drop into Resend's `html`
        field.
    """
    if cta_label and not cta_url:
        raise ValueError("cta_url is required when cta_label is set")

    preheader_safe = escape(preheader)
    heading_safe = escape(heading)
    footer = footer_html if footer_html is not None else (
        '<p style="margin: 0;">You\'re receiving this because someone '
        "(hopefully you) used your email on "
        f'<a href="https://www.metisdolos.com" style="color:{_FG_MUTED};">'
        "MetisDolos</a>. If that wasn't you, you can safely ignore this email.</p>"
    )

    cta_block = ""
    if cta_label and cta_url:
        cta_block = f"""
        <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin: 24px 0;">
          <tr>
            <td align="center" bgcolor="{_ACCENT}" style="border-radius: 8px;">
              <a href="{escape(cta_url, quote=True)}" target="_blank" style="
                display: inline-block;
                padding: 12px 28px;
                font-family: {_FONT_SANS};
                font-size: 15px;
                font-weight: 600;
                color: {_ACCENT_FG};
                text-decoration: none;
                border-radius: 8px;
                letter-spacing: 0.02em;
              ">{escape(cta_label)}</a>
            </td>
          </tr>
        </table>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>{heading_safe}</title>
</head>
<body style="margin: 0; padding: 0; background: {_BG}; font-family: {_FONT_SANS}; color: {_FG};">
  <!-- Preheader: hidden, but appears as inbox preview text -->
  <div style="display: none; max-height: 0; overflow: hidden; opacity: 0; visibility: hidden;
              mso-hide: all; font-size: 1px; line-height: 1px; color: {_BG};">
    {preheader_safe}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
  </div>

  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
         style="background: {_BG}; padding: 32px 16px;">
    <tr>
      <td align="center">

        <!-- Brand wordmark -->
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
               style="max-width: 560px; margin: 0 auto 16px;">
          <tr>
            <td align="center">
              <a href="https://www.metisdolos.com" style="text-decoration: none;">
                <span style="font-family: {_FONT_SERIF}; font-style: italic; font-weight: 700;
                             font-size: 22px; color: {_FG}; letter-spacing: 0.5px;">Metis</span><span
                             style="font-family: {_FONT_SERIF}; font-style: italic; font-weight: 700;
                             font-size: 22px; color: {_ACCENT}; letter-spacing: 0.5px;">Dolos</span>
              </a>
            </td>
          </tr>
        </table>

        <!-- Card -->
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
               style="max-width: 560px; margin: 0 auto; background: {_SURFACE};
                      border: 1px solid {_BORDER}; border-radius: 14px;
                      box-shadow: 0 8px 24px rgba(60, 46, 30, 0.08);">
          <tr>
            <td style="padding: 32px 36px;">
              <h1 style="margin: 0 0 16px 0; font-family: {_FONT_SERIF}; font-weight: 700;
                         font-size: 24px; line-height: 1.25; color: {_FG};">{heading_safe}</h1>

              <div style="font-size: 15px; line-height: 1.65; color: {_FG};">
                {intro_html}
              </div>

              {cta_block}

              <div style="font-size: 14px; line-height: 1.6; color: {_FG_MUTED};">
                {after_cta_html}
              </div>
            </td>
          </tr>
        </table>

        <!-- Footer -->
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
               style="max-width: 560px; margin: 16px auto 0;">
          <tr>
            <td align="center" style="padding: 16px 12px; font-size: 12px; line-height: 1.6;
                                      color: {_FG_DIM}; font-family: {_FONT_SANS};">
              {footer}
              <p style="margin: 8px 0 0 0; color: {_FG_DIM};">
                MetisDolos · <a href="https://www.metisdolos.com" style="color: {_FG_DIM};">www.metisdolos.com</a>
              </p>
            </td>
          </tr>
        </table>

      </td>
    </tr>
  </table>
</body>
</html>
"""


# ─── Concrete email bodies ───────────────────────────────────────────────────


def verification_email(*, first_name: str, link: str) -> str:
    """Body for the post-signup email-verification email."""
    safe_link = escape(link, quote=True)
    name = escape(first_name) if first_name else "there"
    intro = (
        f"<p style='margin: 0 0 12px;'>Hi {name},</p>"
        "<p style='margin: 0 0 12px;'>Welcome to <strong>MetisDolos</strong> — a research "
        "sandbox where seven LLM agents negotiate, betray, and ally their way through a "
        "game of classic Diplomacy.</p>"
        "<p style='margin: 0;'>Click below to verify your email so you can start running games.</p>"
    )
    after = (
        f"<p style='margin: 0 0 8px;'>If the button doesn't work, paste this link into your browser:</p>"
        f"<p style='margin: 0; word-break: break-all; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;'>"
        f"<a href='{safe_link}' style='color: {_ACCENT};'>{safe_link}</a></p>"
    )
    return render_card_email(
        preheader="Verify your MetisDolos email to start running games.",
        heading="Verify your email",
        intro_html=intro,
        cta_label="Verify my email",
        cta_url=link,
        after_cta_html=after,
    )


def password_reset_email(*, first_name: str, link: str) -> str:
    """Body for the password-reset email."""
    safe_link = escape(link, quote=True)
    name = escape(first_name) if first_name else "there"
    intro = (
        f"<p style='margin: 0 0 12px;'>Hi {name},</p>"
        "<p style='margin: 0 0 12px;'>We got a request to reset the password on your "
        "MetisDolos account. Click below to choose a new one — the link works once "
        "and expires in <strong>one hour</strong>.</p>"
        "<p style='margin: 0;'>If you didn't request this, you can ignore this email "
        "— your password won't change.</p>"
    )
    after = (
        f"<p style='margin: 0 0 8px;'>If the button doesn't work, paste this link into your browser:</p>"
        f"<p style='margin: 0; word-break: break-all; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;'>"
        f"<a href='{safe_link}' style='color: {_ACCENT};'>{safe_link}</a></p>"
    )
    return render_card_email(
        preheader="Reset your MetisDolos password — link expires in 1 hour.",
        heading="Reset your password",
        intro_html=intro,
        cta_label="Reset my password",
        cta_url=link,
        after_cta_html=after,
    )
