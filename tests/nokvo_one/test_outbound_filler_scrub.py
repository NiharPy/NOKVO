"""Leading-filler scrub for outbound replies.

The outbound agents (especially the deterministic questionnaire agent) are
prompt-banned from opening with the stock filler "right so" / a bare "Right.",
but small models still leak it. ``strip_leading_right_so`` removes it
deterministically from the first spoken sentence so it is never heard, shown, or
stored. These tests pin the exact-target behaviour AND the conservative
boundaries (it must never mangle "right now" / "right away" / a standalone
"So …").

Unit tests — pure function, no DB or LLM.
"""
from __future__ import annotations

from app.services.agent_outbound_context import (
    strip_leading_fillers,
    strip_leading_right_so,
)


def test_strips_connected_right_so_variants():
    # The core complaint: "right so" in every punctuation variant the LLM emits.
    assert strip_leading_right_so("Right so, what's your budget?") == "What's your budget?"
    assert strip_leading_right_so("Right, so what's your budget?") == "What's your budget?"
    assert strip_leading_right_so("Right. So, what's your budget?") == "What's your budget?"
    assert strip_leading_right_so("right so what's your budget?") == "What's your budget?"
    assert strip_leading_right_so("Right—so when works for a visit?") == "When works for a visit?"


def test_bare_right_ack_is_dropped():
    # A standalone "Right." sentence pairs with a following "So …" to reproduce
    # "right so" when split across two sentences — drop it entirely.
    assert strip_leading_right_so("Right.") == ""
    assert strip_leading_right_so("Right") == ""
    assert strip_leading_right_so("Right!") == ""
    assert strip_leading_right_so("  Right,  ") == ""
    # "right so." with nothing after is also pure filler → dropped.
    assert strip_leading_right_so("Right so.") == ""


def test_does_not_touch_real_words_starting_with_right():
    # "right" as a genuine word must survive untouched — these are the
    # false-positives the ``$``/``so``-anchored regexes are designed to avoid.
    assert strip_leading_right_so("Right now we have a 2 BHK available.") == (
        "Right now we have a 2 BHK available."
    )
    assert strip_leading_right_so("Right away, I'll note that down.") == (
        "Right away, I'll note that down."
    )
    assert strip_leading_right_so("Right here in Gachibowli, actually.") == (
        "Right here in Gachibowli, actually."
    )
    # A leading "Right," before non-"so" content is NOT the reported filler and
    # is left alone (only "right so" and a bare ack are removed).
    assert strip_leading_right_so("Right, the 3 BHK is the better fit.") == (
        "Right, the 3 BHK is the better fit."
    )


def test_does_not_eat_hyphenated_so_compounds():
    # Regression: the trailing separator class must NOT include hyphens/dashes,
    # or it bridges into "so-called" / "so-so" / "so-and-so" and corrupts meaning.
    assert strip_leading_right_so("Right. So-called premium units, sir.") == (
        "Right. So-called premium units, sir."
    )
    assert strip_leading_right_so("Right, so-so for the price honestly.") == (
        "Right, so-so for the price honestly."
    )
    assert strip_leading_right_so("Right so—so far so good.") == "Right so—so far so good."


def test_ellipsis_and_alright_variants():
    # Ellipsis separator (the leaked filler often renders "Right…so").
    assert strip_leading_right_so("Right…so what's the plan?") == "What's the plan?"
    assert strip_leading_right_so("Right, so…the budget?") == "The budget?"
    # "Alright so" is the same filler with a common spelling variant.
    assert strip_leading_right_so("Alright so, when can you visit?") == (
        "When can you visit?"
    )
    assert strip_leading_right_so("Alright.") == ""


def test_no_stranded_punctuation_when_so_is_terminal():
    # When "so" is the last token, trailing punctuation must be consumed too —
    # never returned as a lone "?" / "…".
    assert strip_leading_right_so("Right so?") == ""
    assert strip_leading_right_so("Right, so 3 BHK works for you?") == (
        "3 BHK works for you?"
    )


def test_does_not_strip_standalone_so():
    # After a bare "Right." is dropped, the next sentence often starts with
    # "So …" — that is natural speech and must be preserved.
    assert strip_leading_right_so("So, what's your budget?") == "So, what's your budget?"


def test_mid_sentence_right_preserved():
    assert strip_leading_right_so("That's the right configuration for you.") == (
        "That's the right configuration for you."
    )
    # "righto" / other words that merely start with the letters are untouched
    # (word-boundary anchored).
    assert strip_leading_right_so("Righto, let's lock it in.") == "Righto, let's lock it in."


def test_code_switched_opener():
    # Code-switched Hindi/Telugu replies that open with an English "Right so".
    assert strip_leading_right_so("Right so, aapka budget kya hai?") == (
        "Aapka budget kya hai?"
    )


def test_empty_and_non_filler_passthrough():
    assert strip_leading_right_so("") == ""
    assert strip_leading_right_so("What's your name?") == "What's your name?"


# ── strip_leading_fillers: the DETERMINISTIC questionnaire agent ──────────────
# This is the wide scrub: the deterministic agent must open every turn with the
# question itself, so the WHOLE acknowledgement set is removed — not just
# "right so". It must still never mangle a real word that merely starts with a
# filler token ("Good morning", "Right now", "So happy", "so-called").

def test_fillers_strips_the_full_acknowledgement_set():
    assert strip_leading_fillers("Great, what's your budget?") == "What's your budget?"
    assert strip_leading_fillers("Perfect. Are you the homeowner?") == "Are you the homeowner?"
    assert strip_leading_fillers("Nice, when works for you?") == "When works for you?"
    assert strip_leading_fillers("Sure, what's your timeline?") == "What's your timeline?"
    assert strip_leading_fillers("Cool, which area?") == "Which area?"
    assert strip_leading_fillers("Okay, are you looking to buy?") == "Are you looking to buy?"
    assert strip_leading_fillers("Got it, and your name?") == "And your name?"
    assert strip_leading_fillers("Good to hear, when can you visit?") == "When can you visit?"
    assert strip_leading_fillers("Thanks for that. Which area?") == "Which area?"
    assert strip_leading_fillers("Understood, what's the budget?") == "What's the budget?"
    assert strip_leading_fillers("Awesome, are you the decision maker?") == (
        "Are you the decision maker?"
    )


def test_fillers_peels_chained_acknowledgements():
    # "Great, thanks — what's your budget?" stacks two fillers before content.
    assert strip_leading_fillers("Great, thanks — what's your budget?") == (
        "What's your budget?"
    )
    assert strip_leading_fillers("Okay, perfect, are you the homeowner?") == (
        "Are you the homeowner?"
    )
    # right-so (no punctuation) composed with the wider set in either order.
    assert strip_leading_fillers("Right so great, what type of house?") == (
        "What type of house?"
    )


def test_fillers_whole_sentence_filler_becomes_empty():
    # Pure acknowledgement turns scrub to "" so the caller skips them and the
    # next sentence carries the turn (same as a bare "Right.").
    for s in ("Perfect.", "Got it.", "Great, thanks!", "Sure.", "Okay.", "Nice!",
              "Good to hear.", "Understood."):
        assert strip_leading_fillers(s) == "", s


def test_fillers_preserve_real_words_starting_with_a_filler_token():
    # The conservative gate: a filler word is only removed when SENTENCE
    # PUNCTUATION follows it. These run straight into content, so they survive.
    keep = [
        "Good morning, are you free to talk?",
        "Right now we have a 2 BHK available.",
        "So happy to help you find a home.",
        "So-called premium units, sir.",
        "Well-being is our priority here.",
        "Actually wonderful news on pricing today.",  # no comma after 'actually'
        "Sure-fire way to lock the unit.",            # hyphen compound
    ]
    for s in keep:
        assert strip_leading_fillers(s) == s, s


def test_fillers_standalone_question_and_passthrough():
    assert strip_leading_fillers("Are you the homeowner?") == "Are you the homeowner?"
    assert strip_leading_fillers("What's your budget?") == "What's your budget?"
    assert strip_leading_fillers("") == ""


def test_fillers_recapitalize_and_code_switch():
    # Re-capitalizes the first real word; works on code-switched openers.
    assert strip_leading_fillers("great, what is your budget?") == "What is your budget?"
    assert strip_leading_fillers("Great, aapka budget kya hai?") == "Aapka budget kya hai?"
    # A digit first char is left as-is (nothing to capitalize).
    assert strip_leading_fillers("Perfect, 3 BHK or 4 BHK?") == "3 BHK or 4 BHK?"
