"""Legal grounding: excerpt lookup quality + drift alarm against the JS source.

The backend text assets (app/assets/legal/*.txt) are generated from
frontend/src/content/apexLegalDocs.js. Regenerate with::

    venv/bin/python - <<'EOF'
    import re, html
    src = open('frontend/src/content/apexLegalDocs.js').read()
    def extract(name):
        return re.search(rf'export const {name} = `\\n?(.*?)`;', src, re.S).group(1)
    def to_text(raw):
        t = raw
        t = re.sub(r'<h3>(.*?)</h3>', r'\\n# \\1\\n', t, flags=re.S)
        t = re.sub(r'<h4>(.*?)</h4>', r'\\n## \\1\\n', t, flags=re.S)
        t = re.sub(r'<li>(.*?)</li>', r'- \\1\\n', t, flags=re.S)
        t = re.sub(r'</p>|</ul>', '\\n', t)
        t = re.sub(r'<[^>]+>', '', t)
        t = html.unescape(t)
        t = re.sub(r'\\n{3,}', '\\n\\n', t)
        return t.strip() + '\\n'
    header = ("# SYNCED COPY — source of truth is frontend/src/content/apexLegalDocs.js.\\n"
              "# Regenerate with the snippet in tests/nokvo_one/test_nova_legal_grounding.py when the JS changes.\\n\\n")
    open('app/assets/legal/apex_tos.txt', 'w').write(header + to_text(extract('APEX_TERMS_OF_SERVICE_HTML')))
    open('app/assets/legal/apex_privacy.txt', 'w').write(header + to_text(extract('APEX_PRIVACY_POLICY_HTML')))
    EOF
"""
import html
import re
from pathlib import Path

from app.services.nova_legal_grounding import _chunks, lookup_legal

_REPO = Path(__file__).resolve().parents[2]
_JS = _REPO / "frontend" / "src" / "content" / "apexLegalDocs.js"


def _js_headings(const_name: str) -> set[str]:
    src = _JS.read_text(encoding="utf-8")
    block = re.search(rf"export const {const_name} = `\n?(.*?)`;", src, re.S).group(1)
    return {html.unescape(h).strip() for h in re.findall(r"<h4>(.*?)</h4>", block)}


def test_drift_alarm_tos_headings_match_js_source():
    asset = {h for h, _ in _chunks("tos")}
    assert asset == _js_headings("APEX_TERMS_OF_SERVICE_HTML"), (
        "app/assets/legal/apex_tos.txt has drifted from apexLegalDocs.js — "
        "regenerate it (see this file's docstring)."
    )


def test_drift_alarm_privacy_headings_match_js_source():
    asset = {h for h, _ in _chunks("privacy")}
    assert asset == _js_headings("APEX_PRIVACY_POLICY_HTML"), (
        "app/assets/legal/apex_privacy.txt has drifted from apexLegalDocs.js — "
        "regenerate it (see this file's docstring)."
    )


def test_refund_question_finds_no_refunds_section():
    hits = lookup_legal("can I get a refund for unused credits", "both")
    assert hits, "expected at least one excerpt"
    assert any("refund" in h["heading"].lower() for h in hits)


def test_privacy_question_scoped_to_privacy_doc():
    hits = lookup_legal("how long are call recordings retained", "privacy")
    assert hits
    assert all(h["doc"] == "privacy" for h in hits)


def test_nonsense_query_returns_empty():
    assert lookup_legal("zzzz qqqq xyzzy", "both") == []


def test_excerpts_are_bounded():
    for h in lookup_legal("payment subscription fees credits", "both"):
        assert len(h["excerpt"]) <= 1600
