"""One-off: NOKVO APEX proposal email for EIPL's Head of Sales (Ratna Prasad).

Styled to match the NOKVO APEX frontend + partner booklet, not the Nokvo One
light shell: #0A0A0B ground, #F3F2F0 ink, #E62630 accent (button gradient
#F03540→#D91F29), #7FD9A8 for the money moment, Sora for prose and JetBrains
Mono for numerals/labels (with email-safe fallbacks), layered translucent
cards, and the login-screen brand stack — silver mark, tracked NOKVO wordmark,
rule–APEX–rule. All figures come from the APEX Partner Booklet (2026).

Table layout + inline styles so Gmail/Outlook/Apple Mail render it; solid
dark backgrounds carry the theme where gradients are stripped. The silver
frontend mark is embedded inline (cid) — the email-assets logo is near-black
and would vanish on this ground.

Run from repo root:
    venv/bin/python -m scripts.send_eipl_proposal [recipient@example.com]
"""
from __future__ import annotations

import asyncio
import datetime
import html
import pathlib
import sys

from app.core.config import settings
from app.services.email_service import EmailService

RECIPIENT_DEFAULT = "niharkumar1407@gmail.com"

SUBJECT = "NOKVO APEX - Your tireless Lead Qualifier."
PREHEADER = (
    "The AI calling engine built for Indian real estate. Every rupee in, "
    "₹1,333 of pipeline out — and a free trial of 1,000 minutes for EIPL."
)
CTA_URL = "https://nokvo.org/apex"

# ── APEX theme tokens (frontend/src/apex/apex-theme.css + ApexApp.vue) ──────
_BG = "#0A0A0B"            # page ground
_CARD = "#131315"          # solid stand-in for the translucent card gradient
_CARD_DEEP = "#0E0E10"     # inset panels (stat tables)
_INK = "#F3F2F0"           # primary text
_MUTED = "#8E8D8B"         # ~ rgba(255,255,255,0.48) on #0A0A0B, solid for email
_FAINT = "#5A5A5C"         # ~ rgba(255,255,255,0.32)
_BORDER = "#232326"        # ~ rgba(255,255,255,0.085)
_HAIR = "#1C1C1F"          # row hairlines
_RED = "#E62630"
_RED_TOP = "#F03540"       # accent button gradient top
_RED_BOTTOM = "#D91F29"    # accent button gradient bottom
_GREEN = "#7FD9A8"

_SANS = "'Sora','Segoe UI',Helvetica,Arial,sans-serif"
_MONO = "'JetBrains Mono','SFMono-Regular',Menlo,Consolas,monospace"

# The silver mark the APEX login screen uses — light on dark, unlike the
# black email-assets logo.
_MARK_CID = "nokvo-apex-mark"
_MARK_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "frontend" / "src" / "assets" / "nokvo-logo.png"
)


def _mark_inline_images() -> list[tuple[str, bytes, str]]:
    try:
        return [(_MARK_CID, _MARK_PATH.read_bytes(), "png")]
    except OSError:
        return []


def _p(text_html: str, *, pad_bottom: int = 16, size: int = 14) -> str:
    return (
        f'<p style="margin:0 0 {pad_bottom}px;font:400 {size}px/23px {_SANS};'
        f'color:{_MUTED};">{text_html}</p>'
    )


def _ink(text_html: str) -> str:
    return f'<strong style="color:{_INK};font-weight:600;">{text_html}</strong>'


def _section_label(label: str) -> str:
    """Booklet-style section label: mono, tracked, red — '01 — THE PROBLEM'."""
    return (
        f'<p style="margin:26px 0 12px;font:600 10px/1 {_MONO};letter-spacing:.2em;'
        f'text-transform:uppercase;color:{_RED};">{html.escape(label)}</p>'
    )


def _stat_card(rows: list[tuple[str, str, str]]) -> str:
    """The booklet's stat table: label left, mono value right; value color per row."""
    cells = "".join(
        f"<tr>"
        f'<td style="padding:11px 0;border-bottom:1px solid {_HAIR};'
        f'font:400 13px/18px {_SANS};color:{_MUTED};">{html.escape(label)}</td>'
        f'<td style="padding:11px 0;border-bottom:1px solid {_HAIR};'
        f"font:600 13px/18px {_MONO};color:{color};"
        f'text-align:right;white-space:nowrap;">{html.escape(value)}</td>'
        f"</tr>"
        for label, value, color in rows
    )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'bgcolor="{_CARD_DEEP}" style="background-color:{_CARD_DEEP};border:1px solid {_BORDER};'
        f'border-radius:12px;"><tr><td style="padding:4px 18px 6px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{cells}</table>'
        f"</td></tr></table>"
    )


def _funnel_strip() -> str:
    steps = [
        ("10,000", "dials — apex", _INK),
        ("1,000", "qualified — apex", _INK),
        ("100", "site visits — closers", _INK),
        ("10", "bookings — closers", _GREEN),
    ]
    tds = []
    for i, (num, label, color) in enumerate(steps):
        tds.append(
            f'<td align="center" style="padding:16px 2px 14px;">'
            f'<div style="font:700 21px/23px {_MONO};color:{color};">{num}</div>'
            f'<div style="font:600 8.5px/12px {_MONO};letter-spacing:.14em;'
            f'text-transform:uppercase;color:{_FAINT};padding-top:5px;">{html.escape(label)}</div>'
            f"</td>"
        )
        if i < len(steps) - 1:
            tds.append(
                f'<td align="center" style="font:600 12px/1 {_MONO};color:{_RED};width:22px;">'
                f'&rarr;<div style="font:600 8px/10px {_MONO};color:{_FAINT};padding-top:2px;">10%</div></td>'
            )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'bgcolor="{_CARD_DEEP}" style="background-color:{_CARD_DEEP};border:1px solid {_BORDER};'
        f'border-radius:12px;"><tr>{"".join(tds)}</tr></table>'
        f'<p style="margin:10px 0 0;font:600 10px/15px {_MONO};letter-spacing:.12em;'
        f'text-transform:uppercase;color:{_FAINT};">One law governs every funnel — '
        f'<span style="color:{_MUTED};">10% a step. APEX runs it at machine scale.</span></p>'
    )


def _offer_card() -> str:
    bullets = [
        ("50% bonus minutes on every purchase — permanent",
         "Buy 10,000 minutes, call with 15,000. Locked for founding clients, not a promotion."),
        ("Flat ₹6,499/mo platform — nothing else",
         "No per-seat fees, no setup fee, no dialer infrastructure, no contracts."),
        ("Everything included",
         "AI qualification & per-question lead scoring, full transcripts & call notes, "
         "unlimited team accounts, and Nova — the built-in AI assistant."),
        ("Live in 10 minutes",
         "Upload the CSV, define your exact questionnaire, launch. Qualified leads appear in real time."),
    ]
    rows = "".join(
        f'<tr><td style="padding:13px 20px;border-bottom:1px solid {_HAIR};">'
        f'<p style="margin:0;font:600 13.5px/19px {_SANS};color:{_INK};">{html.escape(head)}</p>'
        f'<p style="margin:4px 0 0;font:400 12.5px/19px {_SANS};color:{_MUTED};">{html.escape(body)}</p>'
        f"</td></tr>"
        for head, body in bullets
    )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'bgcolor="{_CARD_DEEP}" style="background-color:{_CARD_DEEP};'
        f'border:1px solid {_RED};border-radius:12px;">'
        f'<tr><td style="padding:14px 20px 12px;border-bottom:1px solid {_RED};">'
        f'<span style="font:600 10px/1 {_MONO};letter-spacing:.2em;text-transform:uppercase;'
        f'color:{_RED};">The founding-15 proposal — one slot held for EIPL</span>'
        f"</td></tr>{rows}</table>"
    )


def build_body_html() -> str:
    warm_quote = (
        "&ldquo;Hi Rajesh, I understand you&#39;re looking for a 3&nbsp;BHK "
        "in Kokapet in the ₹1.5 crore range — I&#39;d love to show you our "
        "project this Saturday.&rdquo;"
    )
    free_trial_line = (
        "we&#39;ll open your account with a free trial of 1,000 calling "
        "minutes — on us."
    )
    return "".join([
        _p(f'Dear {_ink("Ratna Prasad")},', pad_bottom=18),
        _p(
            "Every project EIPL launches fills the CRM with thousands of portal "
            "enquiries — and your closers can physically reach only the first "
            "few hundred. The buyers in the back half of that list don't wait: "
            "a portal lead goes cold in under four hours, and the average team "
            "gets to it in two to five days. They didn't lose interest — they "
            "visited whoever called first."
        ),
        _p(
            f'{_ink("Your sales team isn&#39;t underperforming. They&#39;re under-resourced.")} '
            "Closers spend 70% of their day dialing dead numbers to find the "
            "three real buyers buried in a hundred calls. That's not a people "
            "problem — it's a pipeline problem."
        ),
        _section_label("01 — The math is brutal"),
        _stat_card([
            ("Portal leads go cold in", "< 4 hours", _RED),
            ("Your team reaches them in", "2–5 days", _INK),
            ("One telecaller costs (all-in)", "₹15,000–25,000/mo", _INK),
            ("Dials per telecaller per day", "60–100", _INK),
            ("Leads in your CRM right now, untouched", "Thousands", _RED),
        ]),
        _section_label("02 — What NOKVO APEX is"),
        _p(
            f'{_ink("The tireless first-call layer.")} APEX dials your entire '
            "list 9 AM–7 PM, holds a real conversation in the buyer's language, "
            "and asks <em>your exact qualification questionnaire</em> — "
            "configuration, budget, self-use or investment, timeline, "
            "site-visit intent. Every question, in order, every time. No AI "
            "drift, no hallucinated pricing, no off-brand promises."
        ),
        _p(
            "Every answer is scored per-question, automatically. Your closers "
            "open a ranked dashboard of live buyers — score, budget, BHK, "
            "timeline, full transcript — and claim them. The first human call "
            f"is never cold: {_ink(warm_quote)}"
        ),
        _section_label("03 — The funnel, at machine scale"),
        _funnel_strip(),
        _section_label("04 — The revenue math"),
        _stat_card([
            ("Pipeline from 10 bookings @ ₹1.2 Cr avg", "₹12 crore", _INK),
            ("APEX cost for those 10,000 conversations", "₹90,000", _INK),
            ("Every rupee in → pipeline out", "₹1 → ₹1,333", _GREEN),
        ]),
        _p(
            f'{_ink("Same team. Same salaries. 10× the qualified conversations.")} '
            "APEX doesn't replace your telecallers — it frees your budget to "
            "hire closers, not dialers.",
            pad_bottom=20,
        ),
        _offer_card(),
        _section_label("05 — The pilot: 1,000 minutes free"),
        _p(
            f"If this interests you, {_ink(free_trial_line)} Hand us 1,000 of "
            "the leads your team never got to. By Friday, your closers have a "
            "scored, ranked buyer list with full transcripts — site-visit-ready. "
            "If it doesn't change how EIPL sells, walk away: "
            f'{_ink("there is no contract to cancel.")} If it does, your '
            "founding-15 slot is waiting."
        ),
        _p(
            "I'd love 15 minutes this week to run APEX live on your own lead "
            "list — reply to this email or write to "
            f'{_ink("officialnokvo@nokvo.org")}.',
            pad_bottom=8,
        ),
    ])


def build_html() -> str:
    year = datetime.datetime.now().year
    mark_img = (
        f'<img src="cid:{_MARK_CID}" alt="NOKVO" width="44" '
        f'style="display:block;width:44px;height:auto;border:0;margin:0 auto 14px;">'
        if _mark_inline_images()
        else ""
    )
    rule = f'<td style="width:36px;height:2px;background:{_RED};font-size:0;line-height:0;">&nbsp;</td>'
    brand_stack = (
        f"{mark_img}"
        f'<div style="font:600 24px/26px {_SANS};letter-spacing:.36em;color:{_INK};'
        f'padding-left:.36em;text-align:center;">NOKVO</div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:10px auto 0;"><tr>'
        f"{rule}"
        f'<td style="padding:0 13px;font:600 11px/1 {_SANS};letter-spacing:.5em;'
        f'padding-left:calc(.5em + 13px);color:{_RED};">APEX</td>'
        f"{rule}"
        f"</tr></table>"
    )
    return f"""\
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>NOKVO APEX</title></head>
<body style="margin:0;padding:0;background-color:{_BG};" bgcolor="{_BG}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{html.escape(PREHEADER)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{_BG}" style="background-color:{_BG};">
  <tr><td align="center" style="padding:44px 16px;">
    <table role="presentation" width="560" cellpadding="0" cellspacing="0" style="width:560px;max-width:100%;">
      <!-- brand stack — the APEX login masthead -->
      <tr><td align="center" style="padding:0 0 30px;">{brand_stack}</td></tr>
      <tr><td bgcolor="{_CARD}" style="background-color:{_CARD};border:1px solid {_BORDER};border-radius:16px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <!-- masthead row: eyebrow + booklet page tag -->
          <tr><td style="padding:26px 34px 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
              <td style="font:600 10px/1 {_MONO};letter-spacing:.24em;text-transform:uppercase;color:{_FAINT};">Partnership proposal</td>
              <td align="right" style="font:600 10px/1 {_MONO};letter-spacing:.18em;color:{_FAINT};">2026</td>
            </tr></table>
            <div style="height:1px;background:{_BORDER};font-size:0;line-height:0;margin-top:16px;">&nbsp;</div>
          </td></tr>
          <tr><td style="padding:28px 34px 0;">
            <h1 style="margin:0 0 20px;font:600 26px/32px {_SANS};letter-spacing:-.01em;color:{_INK};">
              Your closers were never<br>the problem.
            </h1>
          </td></tr>
          <tr><td style="padding:0 34px;">{build_body_html()}</td></tr>
          <!-- CTA — the accent button -->
          <tr><td style="padding:10px 34px 8px;">
            <table role="presentation" cellpadding="0" cellspacing="0"><tr>
              <td bgcolor="{_RED_BOTTOM}" style="background-color:{_RED_BOTTOM};background-image:linear-gradient(180deg,{_RED_TOP},{_RED_BOTTOM});border-radius:8px;">
                <a href="{html.escape(CTA_URL, quote=True)}" target="_blank"
                   style="display:inline-block;padding:14px 28px;font:600 14px/1 {_SANS};letter-spacing:.02em;color:#ffffff;text-decoration:none;">
                  See APEX in action&nbsp;&rarr;
                </a>
              </td>
            </tr></table>
            <p style="margin:14px 0 0;font:400 12px/18px {_MONO};color:{_FAINT};word-break:break-all;">{html.escape(CTA_URL)}</p>
          </td></tr>
          <!-- sign-off -->
          <tr><td style="padding:24px 34px 30px;">
            <div style="height:1px;background:{_BORDER};font-size:0;line-height:0;">&nbsp;</div>
            <p style="margin:18px 0 0;font:400 14px/22px {_SANS};color:{_MUTED};">
              — <strong style="color:{_INK};font-weight:600;">Nihar Neelala</strong><br>
              <span style="color:{_FAINT};">Co-founder, NOKVO&nbsp;&nbsp;·&nbsp;&nbsp;officialnokvo@nokvo.org</span>
            </p>
            <p style="margin:16px 0 0;font:600 13px/20px {_SANS};color:{_INK};">
              APEX is the world's most tireless telecaller. Your team is still the closer.<br>
              <span style="color:{_RED};">One fills the room. The other closes the deal.</span>
            </p>
          </td></tr>
        </table>
      </td></tr>
      <!-- booklet footer strip -->
      <tr><td style="padding:18px 6px 6px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="font:600 9.5px/16px {_MONO};letter-spacing:.16em;text-transform:uppercase;color:{_FAINT};">NOKVO APEX — Partnership proposal</td>
          <td align="right" style="font:600 9.5px/16px {_MONO};letter-spacing:.16em;color:{_FAINT};">nokvo.org/apex</td>
        </tr></table>
        <p style="margin:10px 0 0;font:400 10px/16px {_MONO};letter-spacing:.04em;color:{_FAINT};">
          Sent for the attention of Ratna Prasad, Head of Sales, EIPL.
          © {year} NEEDLES COUTURE AND COLLECTIVE PRIVATE LIMITED, India. All rights reserved.
        </p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""


def build_text_body() -> str:
    return (
        "Dear Ratna Prasad,\n\n"
        "Every project EIPL launches fills the CRM with thousands of portal enquiries — "
        "and your closers can physically reach only the first few hundred. A portal lead "
        "goes cold in under 4 hours; the average team reaches it in 2-5 days. The buyers "
        "didn't lose interest — they visited whoever called first.\n\n"
        "Your sales team isn't underperforming. They're under-resourced. Closers spend 70% "
        "of their day dialing dead numbers to find the 3 real buyers in 100 calls.\n\n"
        "01 — THE MATH IS BRUTAL\n"
        "- Portal leads go cold in: < 4 hours\n"
        "- Your team reaches them in: 2-5 days\n"
        "- One telecaller costs: Rs 15,000-25,000/mo all-in\n"
        "- Dials per telecaller per day: 60-100\n"
        "- Leads in your CRM right now, untouched: thousands\n\n"
        "02 — WHAT NOKVO APEX IS\n"
        "The tireless first-call layer. It dials your entire list 9 AM-7 PM, holds a real "
        "conversation in the buyer's language, and asks YOUR exact qualification "
        "questionnaire — configuration, budget, self-use or investment, timeline, "
        "site-visit intent. Every question, in order, every time. No AI drift, no "
        "hallucinated pricing. Every answer is scored per-question; your closers claim "
        "ranked buyers with full transcripts, so the first human call is a warm follow-up.\n\n"
        "03 — THE FUNNEL: 10,000 dials -> 1,000 qualified -> 100 site visits -> 10 bookings.\n\n"
        "04 — THE REVENUE MATH: 10 bookings @ Rs 1.2 Cr avg = Rs 12 crore pipeline. APEX "
        "cost for those 10,000 conversations: Rs 90,000. Every Rs 1 in -> Rs 1,333 out.\n"
        "Same team. Same salaries. 10x the qualified conversations.\n\n"
        "THE FOUNDING-15 PROPOSAL — one slot held for EIPL:\n"
        "- 50% bonus minutes on every purchase, permanent (buy 10,000 min, call with 15,000)\n"
        "- Flat Rs 6,499/mo platform — no per-seat fees, no setup fee, no contracts\n"
        "- AI qualification, per-question lead scoring, transcripts, unlimited team "
        "accounts, and Nova (the built-in AI assistant) included\n"
        "- Live in 10 minutes: upload CSV, define questionnaire, launch\n\n"
        "05 — THE PILOT: 1,000 MINUTES FREE. If this interests you, we'll open your "
        "account with a free trial of 1,000 calling minutes — on us. Hand us 1,000 of the "
        "leads your team never got to. By Friday your closers have a scored, ranked, "
        "site-visit-ready buyer list with transcripts. If it doesn't change how EIPL "
        "sells, walk away — there's no contract to cancel. If it does, your founding-15 "
        "slot is waiting.\n\n"
        "I'd love 15 minutes this week to run APEX live on your own lead list.\n"
        f"See it in action: {CTA_URL}\n\n"
        "— Nihar Neelala\n"
        "Co-founder, NOKVO · officialnokvo@nokvo.org\n\n"
        "APEX is the world's most tireless telecaller. Your team is still the closer.\n"
        "One fills the room. The other closes the deal.\n"
    )


async def main() -> int:
    recipient = sys.argv[1] if len(sys.argv) > 1 else RECIPIENT_DEFAULT
    try:
        settings.SMTP_FROM_NAME = "NOKVO APEX"
    except Exception:
        pass
    await EmailService.send(
        recipient,
        SUBJECT,
        build_text_body(),
        html_body=build_html(),
        inline_images=_mark_inline_images(),
    )
    print(f"Proposal email sent to {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
