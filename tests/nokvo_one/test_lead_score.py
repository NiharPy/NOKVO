"""Lead-capture questionnaire + Lead Score scoring.

Covers the canonical questionnaire normalizer, the post-call scorer's parsing /
safe-defaults / qualification math, the multilingual (te/hi) parity guard, and
the load-bearing build_agent_config WRITE+READ round-trip (the read path re-runs
build_agent_config, so a non-named questionnaire would be silently stripped).

Unit tests — no DB or live LLM (the scorer LLM is monkeypatched).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services import agent_outbound_context as ctx_mod
from app.services.agent_outbound_context import (
    OutboundCampaignContext,
    _coerce_questionnaire,
    build_agent_config,
    compose_outbound_system_section,
    generate_outbound_opener_text,
    questionnaire_max_points,
    render_questionnaire_block,
)
from app.services import lead_score_service as ls


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Normalizer ────────────────────────────────────────────────────────────

def test_normalizer_shapes_and_rules():
    raw = {
        "questions": [
            {"type": "intent", "text": "Interested?", "desired_answer": "leak"},
            {"type": "answer", "text": "Which area?", "desired_answer": "Kollur"},
            {"type": "answer", "text": "   ", "desired_answer": "x"},  # blank text dropped
            {"type": "weird", "text": "Budget?"},  # bad type -> intent
        ]
    }
    q = _coerce_questionnaire(raw)
    assert [x["type"] for x in q["questions"]] == ["intent", "answer", "intent"]
    assert q["questions"][0]["desired_answer"] == ""  # intent wipes desired_answer
    assert q["questions"][1]["desired_answer"] == "Kollur"
    assert all(len(x["id"]) >= 4 for x in q["questions"])
    assert q["threshold"] == 3  # missing -> len (must-pass-all)


def test_normalizer_threshold_clamp_and_empty():
    assert _coerce_questionnaire({"questions": [{"type": "intent", "text": "a"}], "threshold": 9})["threshold"] == 1
    assert _coerce_questionnaire({"questions": [{"type": "intent", "text": "a"}], "threshold": 0})["threshold"] == 1
    assert _coerce_questionnaire({"questions": []}) is None
    assert _coerce_questionnaire(None) is None
    assert _coerce_questionnaire("nope") is None


def test_normalizer_strict_raises_on_answer_without_desired():
    bad = {"questions": [{"type": "answer", "text": "Area?"}]}
    try:
        _coerce_questionnaire(bad, strict=True)
        assert False, "expected ValueError"
    except ValueError:
        pass
    # lenient drops it -> None (no valid questions)
    assert _coerce_questionnaire(bad) is None


def test_question_count_capped():
    raw = {"questions": [{"type": "intent", "text": f"q{i}"} for i in range(50)]}
    assert len(_coerce_questionnaire(raw)["questions"]) == ctx_mod._MAX_QUESTIONS


# ── build_agent_config round-trip (the read-path persistence guard) ─────────

def test_questionnaire_survives_write_and_read_path():
    raw = {"questions": [{"type": "intent", "text": "Interested?"},
                         {"type": "answer", "text": "Area?", "desired_answer": "Kollur"}],
           "threshold": 1}
    cfg = build_agent_config(agent_prompt="hi", questionnaire=raw)
    assert "questionnaire" in cfg and len(cfg["questionnaire"]["questions"]) == 2
    # _build_context re-runs build_agent_config(**stored_config); the questionnaire
    # MUST survive that or the agent never sees it.
    cfg2 = build_agent_config(**dict(cfg))
    assert "questionnaire" in cfg2 and len(cfg2["questionnaire"]["questions"]) == 2


def test_context_populates_questionnaire_fields():
    raw = {"questions": [{"type": "intent", "text": "Interested?"}], "threshold": 1}
    cfg = build_agent_config(agent_prompt="hi", questionnaire=raw)
    camp = SimpleNamespace(id="11111111-1111-1111-1111-111111111111", name="c",
                           agent_config=cfg, doc_text=None)
    c = ctx_mod._build_context(camp)
    assert c.has_questionnaire and len(c.questions) == 1 and c.question_threshold == 1
    assert c.is_proactive


# ── Prompt block ───────────────────────────────────────────────────────────

def _ctx(questions=None, threshold=0):
    return OutboundCampaignContext(
        campaign_id="c", name="n", goal="", agent_prompt="Pitch the offer.",
        objectives=["lead"], exit_conditions=["done"], tone="warm", doc_text=None,
        company_name="Acme", caller_name="Riya",
        questions=questions or [], question_threshold=threshold,
    )


def test_normalizer_keeps_intro_and_requires_questions():
    raw = {"intro": "Hi, this is Riya from Acme.",
           "questions": [{"type": "intent", "text": "Interested?"}], "threshold": 1}
    q = _coerce_questionnaire(raw)
    assert q["intro"] == "Hi, this is Riya from Acme."
    # intro alone (no questions) is not a questionnaire
    assert _coerce_questionnaire({"intro": "hello", "questions": []}) is None
    # blank intro dropped
    assert "intro" not in _coerce_questionnaire({"questions": [{"type": "intent", "text": "a"}]})


def test_admin_intro_overrides_template_opener():
    intro = "నమస్తే, నేను రియా, రాఘవ ఎస్టేట్స్ నుండి."  # Telugu intro
    ctx = _ctx([{"id": "q1", "type": "intent", "text": "Interested?", "desired_answer": ""}], 1)
    object.__setattr__(ctx, "question_intro", intro)  # frozen dataclass
    opener = generate_outbound_opener_text(ctx, language="en")
    assert intro in opener  # admin intro is spoken verbatim, not the en template
    # no intro -> falls back to the template (mentions the company)
    base = _ctx()
    assert "Acme" in generate_outbound_opener_text(base, language="en")


def test_block_has_reask_and_human_delivery():
    withq = _ctx([{"id": "q1", "type": "intent", "text": "Interested?", "desired_answer": ""}], 1)
    blk = render_questionnaire_block(withq, language="en")
    low = blk.lower()
    assert "re-ask" in low or "ask that same question again" in low
    assert "real person" in low or "not a form" in low


def test_intent_required_answer_and_outro_normalize():
    q = _coerce_questionnaire({
        "outro": "Thanks for your time, goodbye.",
        "questions": [
            {"type": "intent", "text": "Are you the homeowner?", "required": "NO"},
            {"type": "intent", "text": "Interested?", "required": "garbage"},
            {"type": "answer", "text": "Area?", "desired_answer": "Kollur"},
        ],
    })
    assert q["questions"][0]["required"] == "no"   # normalized lowercase
    assert q["questions"][1]["required"] == "yes"  # invalid -> yes (default)
    assert "required" not in q["questions"][2]      # answer type has none
    assert q["outro"] == "Thanks for your time, goodbye."
    # outro round-trips through build_agent_config read path
    cfg = build_agent_config(agent_prompt="p", questionnaire=q)
    assert build_agent_config(**dict(cfg))["questionnaire"]["outro"] == q["outro"]


def test_outro_context_field_and_opener_unaffected():
    raw = {"outro": "Bye now", "questions": [{"type": "intent", "text": "X"}]}
    cfg = build_agent_config(agent_prompt="p", questionnaire=raw)
    camp = SimpleNamespace(id="11111111-1111-1111-1111-111111111111", name="c",
                           agent_config=cfg, doc_text=None)
    c = ctx_mod._build_context(camp)
    assert c.question_outro == "Bye now"


def test_gate_normalizes_and_is_optional():
    q = _coerce_questionnaire({"questions": [
        {"type": "intent", "text": "Homeowner?", "required": "yes", "gate": True},
        {"type": "intent", "text": "Budget ok?", "required": "yes"},          # no gate
        {"type": "answer", "text": "Area?", "desired_answer": "Kollur", "gate": True},
    ]})
    assert q["questions"][0].get("gate") is True
    assert "gate" not in q["questions"][1]   # stored only when checked
    assert q["questions"][2].get("gate") is True


def test_block_dealbreaker_only_for_gated_questions():
    # gated intent -> dealbreaker shown; non-gated -> required but no dealbreaker
    gated = _ctx([{"id": "q1", "type": "intent", "text": "Are you the homeowner?",
                   "desired_answer": "", "required": "no", "gate": True}], 1)
    object.__setattr__(gated, "question_outro", "Thanks, have a good day.")
    blk = render_questionnaire_block(gated, language="en")
    low = blk.lower()
    assert "required to qualify" in low and '"no"' in low
    assert "dealbreaker" in low
    assert "Thanks, have a good day." in blk  # outro rendered verbatim as the close

    plain = _ctx([{"id": "q1", "type": "intent", "text": "Interested?",
                   "desired_answer": "", "required": "yes"}], 1)
    blk2 = render_questionnaire_block(plain, language="en").lower()
    assert "required to qualify" in blk2
    assert "dealbreaker" not in blk2  # not a gate -> no early exit

    # gated ANSWER question reveals the expected answer to the agent (guarded)
    gated_ans = _ctx([{"id": "q2", "type": "answer", "text": "Which city?",
                       "desired_answer": "Hyderabad", "gate": True}], 1)
    blk3 = render_questionnaire_block(gated_ans, language="en")
    assert "Hyderabad" in blk3 and "NEVER say the expected answer" in blk3


def test_answer_is_outro_matching():
    from app.services.nokvo_one_voice_stream_service import _answer_is_outro
    outro = "Thanks so much for your time, have a great day"
    assert _answer_is_outro("Thanks so much for your time — have a great day!", outro) is True
    assert _answer_is_outro("Okay great, and what's your budget?", outro) is False
    assert _answer_is_outro("", outro) is False
    assert _answer_is_outro("anything", "") is False


def test_answer_is_outro_does_not_fire_when_turn_still_asks(_=None):
    """REGRESSION (field call 072f59e6): the agent bundled the closing line onto
    the SAME turn as the last question, whose words nearly equal the outro
    ("our team will reach out…"), so token-overlap false-fired and the call hung
    up while Q5 was still unanswered — no dealbreaker was ever tripped. A turn
    that still poses a question (more '?' than the outro) must NOT be a close."""
    from app.services.nokvo_one_voice_stream_service import _answer_is_outro
    reach_outro = "Thank you so much for your time. Our team will reach out to you shortly."
    bundled = (
        "Would you like our team to reach out to you for this project? " + reach_outro
    )
    assert _answer_is_outro(bundled, reach_outro) is False        # pending question → keep call alive
    assert _answer_is_outro(reach_outro, reach_outro) is True     # closing line ALONE still ends it
    assert _answer_is_outro(
        "Thank you so much for your time. Our team will reach out shortly.", reach_outro
    ) is True


def test_is_incomplete_answer():
    """A trailing-off fragment ("uh, it's", "I'm") is NOT a finished answer, but a
    real short answer ("1.8 crore", "apartment", "it is") is. Field call
    072f59e6: "uh, it's" was treated as answered and the agent jumped ahead."""
    from app.services.agent_outbound_context import _is_incomplete_answer as inc
    for frag in ("uh", "um", "আহ। Uh, it's", "it's about", "I'm", "um the", "it's around"):
        assert inc(frag) is True, frag
    for done in ("1.8 crore", "It's about 1.8", "apartment", "yes", "Kollur",
                 "it is", "I am", "I think so", "I'm looking for an apartment",
                 "", "అవును"):
        assert inc(done) is False, done


def test_directive_reasks_on_trailing_fragment_instead_of_advancing():
    """The deterministic progress directive must RE-ASK the current question when
    the caller's reply trailed off — not advance to the next question (which is
    what produced the premature Q5+outro in field call 072f59e6)."""
    from app.services.agent_outbound_context import _render_progress_directive
    Q = [
        {"id": "q1", "type": "intent", "text": "Can you give us a few seconds for this survey?"},
        {"id": "q2", "type": "intent", "text": "Are you looking to buy a house?"},
        {"id": "q3", "type": "answer", "text": "Apartment or villa?", "desired_answer": ""},
        {"id": "q4", "type": "answer", "text": "What is your budget range?", "desired_answer": ""},
        {"id": "q5", "type": "intent", "text": "Would you like our team to reach out to you?"},
    ]
    base_hist = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Can you give us a few seconds for this survey?"},
        {"role": "user", "content": "Yeah"},
        {"role": "assistant", "content": "Are you looking to buy a house?"},
        {"role": "user", "content": "Yeah"},
        {"role": "assistant", "content": "Apartment or villa?"},
        {"role": "user", "content": "Apartment"},
        {"role": "assistant", "content": "What is your budget range?"},
    ]
    # Trailing fragment → re-ask Q4 (budget), do NOT advance to Q5.
    frag_hist = base_hist + [{"role": "user", "content": "uh, it's"}]
    d = _render_progress_directive(Q, frag_hist, latest_user_text="uh, it's")
    assert "RE-ASK Q4" in d and "budget" in d.lower()
    assert "Q5" not in d  # must not tell the agent to ask the next question

    # A COMPLETE budget answer advances to Q5 (no re-ask).
    done_hist = base_hist + [{"role": "user", "content": "It's about 1.8 crore"}]
    d2 = _render_progress_directive(Q, done_hist, latest_user_text="It's about 1.8 crore")
    assert "Q5" in d2 and "TRAILED OFF" not in d2


def test_questionnaire_is_complete():
    """Deterministic CLOSE gate (field call fd8c8a77): after all questions are
    asked and the last gets a real answer ("Yeah"), the agent must close — it was
    instead re-asking Q5 six times, inventing a name question, and looping to Q2.
    A fragment / re-greeting must NOT close (re-ask instead); an unasked question
    must NOT close (keep asking)."""
    from app.services.agent_outbound_context import questionnaire_is_complete as done
    Q = [
        {"id": "q1", "type": "intent", "text": "Can you give us few seconds for this survey?"},
        {"id": "q2", "type": "intent", "text": "Are you looking to buy a house?"},
        {"id": "q3", "type": "answer", "text": "Apartment or Villa?", "desired_answer": "apartment"},
        {"id": "q4", "type": "answer", "text": "What is your budget range?", "desired_answer": "any"},
        {"id": "q5", "type": "intent", "text": "Would you like our team to reach out to you for this project?"},
    ]

    def H(asked_texts):
        h = []
        for a in asked_texts:
            h += [{"role": "user", "content": "x"}, {"role": "assistant", "content": a}]
        return h

    all5 = [q["text"] for q in Q]
    four = [q["text"] for q in Q[:4]]  # Q5 not asked yet
    assert done(Q, H(all5), "Yeah") is True       # all asked + real answer → CLOSE
    assert done(Q, H(all5), "Yes") is True
    assert done(Q, H(all5), "um") is False        # fragment → re-ask, don't close
    assert done(Q, H(all5), "uh, it's") is False
    assert done(Q, H(all5), "hello") is False     # re-greeting → don't close
    assert done(Q, H(four), "Yeah") is False      # last question not asked yet
    assert done([], H(all5), "Yeah") is False     # no questionnaire


def test_last_intent_question_unanswered_does_not_close():
    """REGRESSION (field call, lead Nihar): STT split the budget answer
    "1.5 to 2 crores" across two turns — "1.5" answered Q4, and the tail
    "crores to 2 crores" landed on the LAST question Q5 (an intent yes/no
    "would you like a callback?"). The agent closed on that unrelated tail and the
    scorer credited Q5 +100 from budget text. The close (and the directive) must
    RE-ASK Q5 when the reply has no yes/no cue — but only up to the re-ask cap so
    an unrecognised paraphrase can't loop forever."""
    from app.services.agent_outbound_context import (
        questionnaire_is_complete as done,
        _render_progress_directive as directive,
        _is_yes_no_answer,
    )
    Q = [
        {"id": "q1", "type": "intent", "text": "Can you give us few seconds for this survey?"},
        {"id": "q2", "type": "intent", "text": "Are you looking to buy a house?"},
        {"id": "q3", "type": "answer", "text": "Apartment or Villa?", "desired_answer": "apartment"},
        {"id": "q4", "type": "answer", "text": "What is your budget range?", "desired_answer": "any"},
        {"id": "q5", "type": "intent", "text": "Would you like our team to reach out to you for this project?"},
    ]

    def H(asked_texts):
        h = []
        for a in asked_texts:
            h += [{"role": "user", "content": "x"}, {"role": "assistant", "content": a}]
        return h

    all5 = [q["text"] for q in Q]

    # The bug: split-budget tail on Q5 → no yes/no cue → must NOT close.
    assert _is_yes_no_answer("crores to 2 crores") is False
    assert done(Q, H(all5), "crores to 2 crores") is False
    # The directive must tell the agent to re-ask Q5, not wrap up.
    d = directive(Q, H(all5), "crores to 2 crores")
    assert "RE-ASK Q5" in d

    # A real yes/no answer (literal or paraphrased) still closes.
    for ans in ("Yeah", "yes please", "I'd like your team to reach out", "no thanks", "sounds good"):
        assert _is_yes_no_answer(ans) is True, ans
        assert done(Q, H(all5), ans) is True, ans

    # Re-ask CAP: once Q5 has already been asked twice, accept whatever they say
    # (don't loop forever on a paraphrase we can't classify).
    all5_q5_twice = all5 + [Q[4]["text"]]
    assert done(Q, H(all5_q5_twice), "crores to 2 crores") is True

    # The guard is ONLY for a trailing INTENT question — an answer-type last
    # question (free-form) is not gated on a yes/no cue.
    Qans_last = Q[:4] + [{"id": "q5b", "type": "answer", "text": "Which area do you prefer?", "desired_answer": "any"}]
    all5b = [q["text"] for q in Qans_last]
    assert done(Qans_last, H(all5b), "Kollur side") is True


def test_outbound_post_call_targets_gate():
    """The post-call gate that decides whether a finished outbound call gets
    SCORED. The original bug: it required lead_id/customer_id, so bulk CSV
    lead-capture contacts (which have neither) were never scored — the Qualified
    Leads tab stayed permanently empty."""
    from app.services.nokvo_one_voice_stream_service import _outbound_post_call_targets

    # Bulk CSV lead-capture contact: no lead_id / customer_id, but a real dial
    # (call_link_id + campaign) → MUST run the block (so it gets scored), with
    # NO follow-up target (nothing to write a handoff note onto).
    csv_contact = {"phone": "+919000000000", "name": "A", "call_link_id": "L1"}
    assert _outbound_post_call_targets(
        csv_contact, has_outbound_ctx=True, campaign_id="camp"
    ) == (True, False)

    # Lead-based campaign contact: run AND has a follow-up target (the lead row).
    assert _outbound_post_call_targets(
        {"lead_id": "x", "call_link_id": "L2"}, has_outbound_ctx=True, campaign_id="camp"
    ) == (True, True)

    # Clinic customer follow-up: a follow-up target even without a campaign.
    assert _outbound_post_call_targets(
        {"customer_id": "c1"}, has_outbound_ctx=True, campaign_id=None
    ) == (True, True)

    # Live tester: a synthetic contact with no call_link_id and no lead/customer
    # is excluded (it has its own classifier path), even if a campaign id leaks in.
    assert _outbound_post_call_targets(
        {"phone": "+919000000000", "name": "Tester"}, has_outbound_ctx=True, campaign_id="camp"
    ) == (False, False)

    # Inbound / no outbound context / no contact → never runs.
    assert _outbound_post_call_targets(
        csv_contact, has_outbound_ctx=False, campaign_id="camp"
    ) == (False, False)
    assert _outbound_post_call_targets(
        None, has_outbound_ctx=True, campaign_id="camp"
    ) == (False, False)
    # A campaign dial that somehow lost its campaign id is not treated as one.
    assert _outbound_post_call_targets(
        {"call_link_id": "L9"}, has_outbound_ctx=True, campaign_id=None
    ) == (False, False)


def test_scorer_intent_required_in_prompt():
    Q = [{"id": "q1", "type": "intent", "text": "Homeowner?", "required": "no"}]
    formatted = ls._format_questions(Q)
    assert 'answers "no"' in formatted  # required surfaced to the scorer
    assert "REQUIRED answer" in ls._LEAD_SCORE_SYSTEM


def test_deterministic_uses_minimal_questionnaire_only_prompt():
    """A questionnaire campaign must NOT inherit the real-estate sales scaffold
    (site-visit FSM, objectives, 'your one goal: book a site visit', booking) —
    that hijacked the call. It gets a dedicated minimal questionnaire prompt."""
    Q = [{"id": "q1", "type": "intent", "text": "Decision maker?",
          "desired_answer": "", "required": "yes", "gate": True}]
    det = _ctx(Q, 1)
    s = compose_outbound_system_section(det, covered_objectives=[], language="en",
                                        business_type="real_estate")
    low = s.lower()
    for bad in ["your one goal", "objectives (drive", "agree to a site visit",
                "earn the next step", "propose a specific site"]:
        assert bad not in low, f"sales/site-visit leaked: {bad}"
    assert "# who you are" in low
    assert "lead-capture questionnaire" in low
    assert "do not offer site visits" in low
    assert "disinterest" in low
    # non-questionnaire campaigns keep the full sales scaffold
    base = _ctx()
    assert "your one goal" in compose_outbound_system_section(
        base, covered_objectives=[], language="en", business_type="real_estate"
    ).lower()


def test_prompt_block_gated_ordered_and_hides_desired_answer():
    base = _ctx()
    withq = _ctx([{"id": "q1", "type": "intent", "text": "Interested?", "desired_answer": ""},
                  {"id": "q2", "type": "answer", "text": "Which area?", "desired_answer": "Kollur"}], 1)
    s_base = compose_outbound_system_section(base, covered_objectives=[], language="en")
    s_q = compose_outbound_system_section(withq, covered_objectives=[], language="en")
    assert "LEAD-CAPTURE QUESTIONNAIRE" not in s_base  # no leak into non-questionnaire calls
    assert "LEAD-CAPTURE QUESTIONNAIRE" in s_q
    # questionnaire block sits BEFORE the final, dead-last disinterest rule
    assert s_q.index("LEAD-CAPTURE QUESTIONNAIRE") < s_q.rindex("# DISINTEREST — STOP SELLING")
    # the expected answer is NEVER revealed to the agent
    assert "Kollur" not in s_q


# ── Scorer: parsing + qualification + safe defaults ────────────────────────

_Q = [
    {"id": "q1", "type": "intent", "text": "Interested?", "desired_answer": ""},
    {"id": "q2", "type": "answer", "text": "Area?", "desired_answer": "Kollur"},
    {"id": "q3", "type": "answer", "text": "Budget?", "desired_answer": "50 lakh"},
]


def test_parse_verdicts_alignment_and_threshold():
    txt = ('noise {"verdicts":[{"id":"q1","earned":true,"evidence":"avunu"},'
           '{"id":"q2","earned":true,"evidence":"Kollur"},'
           '{"id":"q3","earned":false,"evidence":""}]} tail')
    r = ls._parse_verdicts(txt, _Q, threshold=2)
    assert r.score == 2 and r.max_score == 3 and r.qualified
    assert [b["awarded"] for b in r.breakdown] == [True, True, False]
    assert r.breakdown[0]["type"] == "intent" and r.breakdown[1]["text"] == "Area?"
    # higher threshold -> not qualified
    assert not ls._parse_verdicts(txt, _Q, threshold=3).qualified


def test_parse_missing_verdict_defaults_false():
    r = ls._parse_verdicts('{"verdicts":[{"id":"q1","earned":true}]}', _Q, threshold=1)
    assert r.score == 1 and [b["awarded"] for b in r.breakdown] == [True, False, False]


def test_parse_unparseable_returns_none():
    assert ls._parse_verdicts("no json", _Q, threshold=1) is None


def test_parse_verdicts_earned_robust_to_string_values():
    """Small models emit ``earned`` as a STRING ("false"/"none"/"no") instead of
    a JSON boolean. ``bool("false")`` is TRUTHY — which previously FALSELY awarded
    the point and over-qualified the lead (creating a wrong qualified ticket).
    Only genuine true values earn; everything else scores 0."""
    Q = [{"id": "q1", "type": "intent", "text": "Interested?", "required": "yes"}]
    earns = ['"true"', '"yes"', '"y"', '"1"', "true", "1"]
    not_earns = ['"false"', '"none"', '"no"', '"0"', "false", "0", "null"]
    for raw in earns:
        r = ls._parse_verdicts('{"verdicts":[{"id":"q1","earned":%s}]}' % raw, Q, threshold=1)
        assert r.score == 1 and r.qualified is True, f"earned={raw} should earn"
    for raw in not_earns:
        r = ls._parse_verdicts('{"verdicts":[{"id":"q1","earned":%s}]}' % raw, Q, threshold=1)
        assert r.score == 0 and r.qualified is False, f"earned={raw} must NOT earn"
    # a missing earned field never awards
    assert ls._parse_verdicts('{"verdicts":[{"id":"q1"}]}', Q, threshold=1).score == 0
    # the helper directly
    assert ls._verdict_earned({"earned": "none"}) is False
    assert ls._verdict_earned({"earned": "false"}) is False
    assert ls._verdict_earned({"earned": True}) is True
    assert ls._verdict_earned({"earned": "YES"}) is True
    assert ls._verdict_earned({}) is False
    assert ls._verdict_earned(None) is False


def test_qualified_score_has_nonempty_reason_for_call_note_fallback():
    """A qualified contact whose call is too short to condense falls back to the
    LeadScore.reason as its Call Note, so the ticket is never note-less — verify
    that reason is always populated and human-readable for a qualified result."""
    txt = ('{"verdicts":[{"id":"q1","earned":true,"evidence":"yes"},'
           '{"id":"q2","earned":true,"evidence":"Kollur"},'
           '{"id":"q3","earned":false,"evidence":""}]}')
    r = ls._parse_verdicts(txt, _Q, threshold=2)
    assert r.qualified and r.reason and "Scored" in r.reason and "Earned" in r.reason


def test_safe_default_never_overqualifies():
    sd = ls._safe_default(_Q, threshold=1, reason="boom")
    assert sd.score == 0 and not sd.qualified and sd.degraded
    assert len(sd.breakdown) == 3 and sd.interest_outcome == "not_interested"


def test_extract_questionnaire():
    got = ls.extract_questionnaire({"questionnaire": {"questions": _Q, "threshold": 2}})
    assert got and len(got[0]) == 3 and got[1] == 2
    assert ls.extract_questionnaire({}) is None
    assert ls.extract_questionnaire(None) is None


# ── Multilingual (te/hi) parity guard ──────────────────────────────────────

def test_system_prompt_lists_native_script_affirmations():
    # te/hi parity: the scorer must recognise native-script yes-words.
    assert "అవును" in ls._LEAD_SCORE_SYSTEM  # Telugu "avunu"
    assert "हाँ" in ls._LEAD_SCORE_SYSTEM      # Hindi "haan"
    assert "SEMANTICALLY" in ls._LEAD_SCORE_SYSTEM


def test_scorer_e2e_passes_language_and_questions(monkeypatch):
    captured = {}

    async def fake_complete_nano(messages, **kw):
        captured["messages"] = messages
        captured["max_tokens"] = kw.get("max_tokens")
        # Telugu native-script affirmation + matching area -> q1,q2 earned.
        return ('{"verdicts":[{"id":"q1","earned":true,"evidence":"అవును"},'
                '{"id":"q2","earned":true,"evidence":"Kollur"},'
                '{"id":"q3","earned":false,"evidence":""}]}')

    monkeypatch.setattr(
        "app.services.nokvo_one_voice_pipeline.AzureGroundedLLM.complete_global",
        staticmethod(fake_complete_nano),
    )
    turns = [
        {"query": "అవును, ఆసక్తి ఉంది", "answer": "Which area?"},
        {"query": "Kollur lo", "answer": "Budget?"},
        {"query": "Madhapur", "answer": "ok"},
    ]
    res = _run(ls.classify_lead_score(
        SimpleNamespace(tenant_id="t1"),
        transcript_turns=turns, questions=_Q, threshold=2,
        language="te", campaign_name="Diwali",
    ))
    assert res.score == 2 and res.qualified and not res.degraded
    # language hint + all questions reach the model
    user_msg = captured["messages"][1]["content"]
    assert "te" in user_msg and "Area?" in user_msg and "Budget?" in user_msg
    # the desired answer IS available to the scorer (unlike the agent prompt)
    assert "Kollur" in user_msg


def test_scorer_empty_transcript_not_degraded(monkeypatch):
    async def fake(messages, **kw):
        return "x"
    monkeypatch.setattr(
        "app.services.nokvo_one_voice_pipeline.AzureGroundedLLM.complete_global",
        staticmethod(fake),
    )
    res = _run(ls.classify_lead_score(SimpleNamespace(tenant_id="t"),
               transcript_turns=[], questions=_Q, threshold=1))
    assert res.score == 0 and not res.qualified and not res.degraded


# ── Weighted points + graded tiers ─────────────────────────────────────────

# Pre-normalized weighted/graded questionnaire (the shape the scorer consumes).
_WQ = [
    {"id": "i1", "type": "intent", "text": "Decision maker?", "required": "yes", "points": 3},
    {"id": "a1", "type": "answer", "text": "Area?", "desired_answer": "Kollur", "points": 2},
    {"id": "g1", "type": "answer", "text": "Budget?", "tiers": [
        {"id": "t1", "label": "above 1 crore", "points": 50},
        {"id": "t2", "label": "50L to 1Cr", "points": 30},
        {"id": "t3", "label": "below 50L", "points": 10},
    ]},
]


def test_max_points_helper():
    # weighted intent(3) + weighted answer(2) + best band(50) = 55
    assert questionnaire_max_points(_WQ) == 55
    # legacy all-1-point questionnaire == len
    assert questionnaire_max_points(_Q) == 3
    assert questionnaire_max_points([]) == 0
    assert questionnaire_max_points(None) == 0


def test_normalizer_points_clamped_and_stored_only_when_nondefault():
    q = _coerce_questionnaire({"questions": [
        {"type": "intent", "text": "a", "points": 1},      # default -> not stored
        {"type": "intent", "text": "b", "points": 5},
        {"type": "intent", "text": "c", "points": 9999},   # clamp to 100
        {"type": "intent", "text": "d"},                   # missing -> 1, not stored
    ]})
    qs = q["questions"]
    assert "points" not in qs[0]
    assert qs[1]["points"] == 5
    assert qs[2]["points"] == 100
    assert "points" not in qs[3]


def test_normalizer_tiers_normalized_capped_and_deduped():
    q = _coerce_questionnaire({"questions": [
        {"type": "answer", "text": "Budget?", "tiers": [
            {"label": "high", "points": 50},
            {"label": "   ", "points": 10},            # blank label dropped
            {"label": "mid", "points": 0},             # clamp up to 1
            {"label": "x", "points": 999},             # clamp down to 100
            {"id": "dup", "label": "y", "points": 5},
            {"id": "dup", "label": "z", "points": 5},  # dup id reassigned
            {"label": "a", "points": 5},
            {"label": "b", "points": 5},               # beyond _MAX_TIERS
        ]},
    ]})
    tiers = q["questions"][0]["tiers"]
    assert len(tiers) == ctx_mod._MAX_TIERS  # capped at 6
    assert [t["label"] for t in tiers][:3] == ["high", "mid", "x"]  # blank dropped
    assert tiers[1]["points"] == 1    # "mid" clamped up
    assert tiers[2]["points"] == 100  # "x" clamped down
    ids = [t["id"] for t in tiers]
    assert len(ids) == len(set(ids))  # all unique (dup reassigned)


def test_graded_answer_needs_no_desired_answer():
    raw = {"questions": [{"type": "answer", "text": "Budget?", "tiers": [
        {"label": "high", "points": 50}]}]}
    assert len(_coerce_questionnaire(raw)["questions"]) == 1
    assert len(_coerce_questionnaire(raw, strict=True)["questions"]) == 1
    # answer with neither desired_answer nor tiers -> strict raises, lenient drops
    bad = {"questions": [{"type": "answer", "text": "X"}]}
    try:
        _coerce_questionnaire(bad, strict=True)
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert _coerce_questionnaire(bad) is None


def test_normalizer_threshold_clamps_to_max_points():
    raw = {"questions": [
        {"type": "intent", "text": "DM?", "points": 3},
        {"type": "answer", "text": "Budget?", "tiers": [
            {"label": "high", "points": 50}, {"label": "low", "points": 10}]},
    ], "threshold": 40}
    assert _coerce_questionnaire(raw)["threshold"] == 40          # within range kept
    raw_missing = {k: v for k, v in raw.items() if k != "threshold"}
    assert _coerce_questionnaire(raw_missing)["threshold"] == 53  # missing -> max points
    raw_over = dict(raw); raw_over["threshold"] = 999
    assert _coerce_questionnaire(raw_over)["threshold"] == 53     # over-max clamps


def test_graded_drops_gate_and_intent_ignores_tiers():
    q = _coerce_questionnaire({"questions": [
        {"type": "answer", "text": "Budget?", "gate": True, "tiers": [
            {"label": "high", "points": 50}]},
        {"type": "intent", "text": "DM?", "tiers": [{"label": "x", "points": 9}]},
    ]})
    assert "gate" not in q["questions"][0]   # graded answer can't be a live gate
    assert "tiers" not in q["questions"][1]  # intent is never graded


def test_backward_compat_no_points_tiers_keys():
    raw = {"questions": [
        {"type": "intent", "text": "DM?", "required": "yes"},
        {"type": "answer", "text": "Area?", "desired_answer": "Kollur"},
    ], "threshold": 2}
    q = _coerce_questionnaire(raw)
    for x in q["questions"]:
        assert "points" not in x and "tiers" not in x  # byte-identical to pre-feature
    assert questionnaire_max_points(q["questions"]) == 2 and q["threshold"] == 2


def test_round_trip_preserves_points_and_tiers():
    raw = {"questions": [
        {"type": "intent", "text": "DM?", "points": 3},
        {"type": "answer", "text": "Budget?", "tiers": [
            {"label": "high", "points": 50}, {"label": "low", "points": 10}]},
    ], "threshold": 30}
    cfg = build_agent_config(agent_prompt="p", questionnaire=raw)
    cfg2 = build_agent_config(**dict(cfg))  # the load-bearing READ-path re-run
    q2 = cfg2["questionnaire"]["questions"]
    assert q2[0]["points"] == 3
    assert len(q2[1]["tiers"]) == 2 and q2[1]["tiers"][0]["points"] == 50
    assert cfg2["questionnaire"]["threshold"] == 30


def test_resolve_tier_points_id_label_index():
    tiers = _WQ[2]["tiers"]
    assert ls._resolve_tier_points(tiers, {"tier": "t2"}) == (30, "t2")          # by id
    assert ls._resolve_tier_points(tiers, {"tier": "above 1 crore"}) == (50, "t1")  # by label
    assert ls._resolve_tier_points(tiers, {"tier": "3"}) == (10, "t3")           # 1-based index
    assert ls._resolve_tier_points(tiers, {"tier": "none"}) == (0, "")
    assert ls._resolve_tier_points(tiers, {"tier": "zzz"}) == (0, "")
    assert ls._resolve_tier_points(tiers, {}) == (0, "")
    assert ls._resolve_tier_points(tiers, None) == (0, "")


def test_parse_verdicts_weighted_and_graded():
    txt = ('{"verdicts":[{"id":"i1","earned":true},'
           '{"id":"a1","earned":false},'
           '{"id":"g1","tier":"t1"}]}')
    r = ls._parse_verdicts(txt, _WQ, threshold=40)
    # i1 earned=3, a1 not=0, g1 top band=50 -> 53/55, qualified (>=40)
    assert r.score == 53 and r.max_score == 55 and r.qualified
    b = {x["id"]: x for x in r.breakdown}
    assert b["i1"]["awarded_points"] == 3 and b["i1"]["awarded"]
    assert b["a1"]["awarded_points"] == 0 and not b["a1"]["awarded"]
    assert b["g1"]["awarded_points"] == 50 and b["g1"]["tier"] == "t1"
    # a lower band crosses a lower threshold but not a higher one
    txt2 = '{"verdicts":[{"id":"i1","earned":false},{"id":"a1","earned":false},{"id":"g1","tier":"t3"}]}'
    r2 = ls._parse_verdicts(txt2, _WQ, threshold=40)
    assert r2.score == 10 and not r2.qualified


def test_parse_verdicts_graded_none_scores_zero():
    r = ls._parse_verdicts('{"verdicts":[{"id":"g1","tier":"none"}]}', [_WQ[2]], threshold=1)
    assert r.score == 0 and not r.qualified
    assert r.breakdown[0]["awarded_points"] == 0 and r.breakdown[0]["tier"] == ""


def test_format_questions_renders_bands_and_weights():
    f = ls._format_questions(_WQ)
    assert "GRADED" in f and "above 1 crore" in f and "50 pts" in f and "[t1]" in f
    assert "3 pts" in f  # weighted intent surfaced
    assert "2 pts" in f  # weighted simple answer surfaced


def test_system_prompt_has_graded_rules():
    s = ls._LEAD_SCORE_SYSTEM
    assert "GRADED" in s and "band id" in s and '"tier"' in s
