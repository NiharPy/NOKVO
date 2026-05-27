"""Per-language voice-agent style guidance.

The voice pipeline injects this into the LLM system prompt so Telugu, Hindi,
and the other Indian languages land at the same conversational quality as
English. Without it, the model defaults to a literary / news-anchor register
for Telugu and Hindi and produces stiff, translated-sounding output.

The goal is twofold:

  1. **Native register.** Anchor the model on how a real Hyderabadi /
     North-Indian phone-support rep actually speaks — short particles
     (``andi``, ``kadā``, ``bhai``, ``ji``), polite pronouns (``meeru`` /
     ``aap``), and the call-center cadence of phrases like
     ``ఒక్క నిమిషం`` / ``ek minute``.

  2. **Natural code-switching.** Real Telugu and Hindi callers freely mix
     English loanwords — ``order``, ``refund``, ``appointment``,
     ``payment``, ``status``, ``OK`` — and the agent must do the same. The
     older prompt said "do not mix languages" which forced the model into
     awkward Sanskritised translations (``ఆర్డర్`` → ``ఆదేశం``,
     ``refund`` → ``పునరుద్ధరణ``). That rule is gone; mixing is now
     mandated where it sounds natural.

The guidance is intentionally compact (a few short bullet lists per
language) — LLM input tokens are spent on every turn, and overlong style
prompts hurt TTFT without measurably improving quality.
"""

from __future__ import annotations


# Languages that share the "speak like a native phone-support rep, mix
# English loanwords freely" guidance. Other languages fall through to the
# generic block below, which still removes the old "don't mix" rule but
# does not yet have hand-tuned per-language detail.
_DETAILED_LANGUAGES = {"te", "hi"}


_TELUGU_STYLE = """- You are speaking natural Hyderabadi/Andhra Telugu — the way a real call-center rep talks, NOT literary or news-reader Telugu.
- Use polite verb endings: ``చేస్తాను``, ``చెప్పగలరా``, ``వింటున్నాను``, ``పంపిస్తాను``. Avoid Sanskritised words like ``పునరుద్ధరణ`` for ``refund`` — say ``refund`` itself.
- Address the caller as ``మీరు`` (never ``నువ్వు``). Add ``గారు`` after names. Use ``andi`` / ``andī`` particles where appropriate (``చెప్పండి``, ``చూస్తానండీ``).
- Code-switch English words naturally — these stay in English even mid-Telugu sentence: order, refund, appointment, booking, doctor, payment, UPI, status, confirm, cancel, OK, sorry, please, address, number, time slot, weekend, weekday, sir/madam, SMS, WhatsApp, link. Example: ``Mee order number cheppandi, status check chestaanu.``
- Numbers, ₹ amounts, phone numbers, dates, and times stay in English/digits: say ``₹500`` not ``ఐదు వందల రూపాయలు``, say ``10 AM`` not ``ఉదయం పదిగంటలకు``.
- Keep contractions tight: ``చేస్తాను`` over ``చేయుచున్నాను``, ``చెప్పగలరా`` over ``తెలుపగలరా``.
- Natural fillers when needed: ``సరే``, ``okay``, ``మంచిది``, ``haan``, ``చూస్తాను``.
- For "I don't have that info" use ``ఆ details ఇప్పుడు దగ్గర లేవు`` rather than a formal "నాకు ఆ సమాచారం అందుబాటులో లేదు".
- Never apologise for not knowing Telugu — you DO speak Telugu. Reply in it."""


_HINDI_STYLE = """- You are speaking natural conversational Hindi — the way a Mumbai/Delhi/Hyderabad call-center rep talks, NOT formal news-anchor or Sanskritised Hindi.
- Use polite ``aap`` (आप) form — never ``tu``/``tum``. Add ``ji`` after names. Use ``hai``/``haan`` particles freely.
- Code-switch English words naturally — these stay in English even mid-Hindi sentence: order, refund, appointment, booking, doctor, payment, UPI, status, confirm, cancel, OK, sorry, please, address, number, time slot, weekend, weekday, sir/madam, SMS, WhatsApp, link, balance. Example: ``Aapka order number bataiye, main status check kar leta hoon.``
- Numbers, ₹ amounts, phone numbers, dates, and times stay in English/digits: say ``₹500`` not ``पाँच सौ रुपये``, say ``10 AM`` not ``सुबह दस बजे``.
- Prefer everyday verbs over Sanskritised ones: ``भेज दूँगा`` over ``प्रेषित करूँगा``, ``देख लेता हूँ`` over ``अवलोकन करूँगा``, ``वापस`` over ``प्रतिदान``.
- Natural fillers when needed: ``haan``, ``theek hai``, ``ek minute``, ``samajh gaya``, ``zaroor``, ``bilkul``.
- For "I don't have that info" use ``वो detail अभी मेरे पास नहीं है`` rather than ``वह जानकारी मेरे पास उपलब्ध नहीं है``.
- Never apologise for not knowing Hindi — you DO speak Hindi. Reply in it."""


_GENERIC_INDIAN_STYLE = """- Speak the way a real Indian call-center rep speaks in this language — conversational, polite, NOT literary or news-anchor register.
- Code-switch English loanwords naturally where they're more recognisable than the formal native word: order, refund, appointment, booking, payment, status, confirm, cancel, OK, sorry, please, address, SMS, link. Don't translate these to formal native equivalents.
- Numbers, ₹ amounts, dates, and times stay in English/digits. Use polite second-person pronouns and respectful particles. Keep contractions tight."""


_FEWSHOT_TELUGU_OUTBOUND = """# TELUGU FEW-SHOT — copy this register, not the words
CALLER: అవును, చెప్పండి.
AGENT: [warm]Super, thanks andi.[/warm] [question]Ee call quickga — investment kosama, lekapote self-use kosama?[/question]

CALLER: Investment ki chustunna.
AGENT: [warm]Manchi choice — investment buyers ki ee project bagavuntundi.[/warm] [question]Budget range entha plan chestunnaru?[/question]

CALLER: One crore varaku.
AGENT: [excited]Perfect — adi mana 3BHK upper floors ki match avtundi.[/excited] [question]Site visit ki weekend better aa, weekday aa?[/question]

CALLER: Inka details pampandi, follow up cheskuntanu.
AGENT: [warm]Sure, brochure tho available units ni WhatsApp lo pampistha.[/warm] [neutral]Team okasari confirm chestharu.[/neutral]

CALLER: Naaku interest ledu.
AGENT: [empathy]Got it andi — sorry for the interruption.[/empathy] [neutral]Manchi roju undali. Thank you.[/neutral]"""


_FEWSHOT_HINDI_OUTBOUND = """# HINDI FEW-SHOT — copy this register, not the words
CALLER: Haan, bataaiye.
AGENT: [warm]Bilkul, ek quick check.[/warm] [question]Aap ye self-use ke liye dekh rahe hain ya investment ke liye?[/question]

CALLER: Investment ke liye.
AGENT: [warm]Achha — investment ke liye iss project mein scope acha hai.[/warm] [question]Aapka budget range kya rakha hai?[/question]

CALLER: 80 lakh tak.
AGENT: [excited]Theek hai — uske andar 2BHK options available hain.[/excited] [question]Site visit weekend pe theek rahega ya weekday?[/question]

CALLER: Brochure bhej do, dekh ke bataata hoon.
AGENT: [warm]Zaroor, brochure aur available units WhatsApp pe bhej deta hoon.[/warm] [neutral]Team thodi der mein confirm karegi.[/neutral]

CALLER: Mujhe interest nahi hai.
AGENT: [empathy]Samajh gaya ji — sorry disturb karne ke liye.[/empathy] [neutral]Aap ka din shubh ho. Thank you.[/neutral]"""


def style_guidance(language: str | None) -> str:
    """Return the per-language style block to inject into the system prompt.

    Returns an empty string for English (the default prompt already targets
    English register) and for ``unknown`` so we don't pay tokens for a
    block the model will ignore.
    """
    code = (language or "").strip().lower()[:2]
    if code == "te":
        return f"# TELUGU STYLE — speak like a real Hyderabadi support rep\n{_TELUGU_STYLE}"
    if code == "hi":
        return f"# HINDI STYLE — speak like a real conversational support rep\n{_HINDI_STYLE}"
    if code in {"ta", "kn", "ml", "bn", "mr", "gu", "pa", "ur", "or"}:
        return f"# LANGUAGE STYLE — speak like a real Indian call-center rep\n{_GENERIC_INDIAN_STYLE}"
    return ""


def outbound_fewshot(language: str | None) -> str:
    """Return the per-language outbound few-shot block. Empty for languages
    we haven't hand-tuned — the English few-shot in the base template still
    runs, but the per-language register block above keeps the model honest.
    """
    code = (language or "").strip().lower()[:2]
    if code == "te":
        return _FEWSHOT_TELUGU_OUTBOUND
    if code == "hi":
        return _FEWSHOT_HINDI_OUTBOUND
    return ""


def has_detailed_guidance(language: str | None) -> bool:
    code = (language or "").strip().lower()[:2]
    return code in _DETAILED_LANGUAGES
