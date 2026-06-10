"""Unit tests for the LangSmith tracing seam.

These tests cover the contracts the rest of the codebase depends on:

  1. With ``LANGSMITH_API_KEY`` unset, every public helper is a no-op.
     No SDK import, no exceptions, no behavioural change to wrapped fns.
  2. With tracing enabled (mocked Client), opening ``trace_call`` posts a
     root run, ``trace_turn`` attaches as a child, ``trace_llm`` attaches
     under the turn, and each one ends + patches on exit.
  3. ``traceable_chain`` returns the function untouched when disabled.
  4. Streaming + barge-in: when an LLM-span yields and the caller raises
     ``asyncio.CancelledError`` (the signal the voice arbiter sends on
     barge-in), the wrapper's ``end_llm_span`` records the partial-buffer
     outputs AND the CancelledError still propagates so the turn arbiter
     can tear down TTS.

The voice pipeline's `stream()` wrapper applies this pattern directly; if
the contract here changes, that wrapper has to change too.
"""
from __future__ import annotations

import asyncio
import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _reset_tracer_state(monkeypatch):
    """Each test starts with a fresh tracer module — module-level globals
    (``_initialized``, ``_enabled``, ``_client``) make it stateful."""
    if "app.services.langsmith_tracer" in sys.modules:
        del sys.modules["app.services.langsmith_tracer"]
    yield
    if "app.services.langsmith_tracer" in sys.modules:
        del sys.modules["app.services.langsmith_tracer"]


def _import_tracer():
    return importlib.import_module("app.services.langsmith_tracer")


# ── 1. Disabled-mode no-op contract ────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_mode_is_noop(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.LANGSMITH_API_KEY", "", raising=False)
    tracer = _import_tracer()
    tracer.init_tracer()
    assert tracer.is_enabled() is False

    # All three context managers yield None and exit cleanly.
    async with tracer.trace_call(call_id="abc") as run:
        assert run is None
    async with tracer.trace_turn(turn_index=1, user_text="hi") as turn:
        assert turn is None
    async with tracer.trace_llm(name="azure_openai.complete", model="m") as span:
        assert span is None

    # end_llm_span tolerates None.
    tracer.end_llm_span(None, {"response": "x"})


@pytest.mark.asyncio
async def test_disabled_traceable_chain_returns_original(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.LANGSMITH_API_KEY", "", raising=False)
    tracer = _import_tracer()
    tracer.init_tracer()

    async def my_fn(x):
        return x * 2

    wrapped = tracer.traceable_chain("test_chain")(my_fn)
    # When disabled, decorator should be a pass-through — same function object.
    assert wrapped is my_fn
    assert await wrapped(3) == 6


# ── 2. Enabled mode: parent/child/llm wiring ───────────────────────────────


class _FakeRunTree:
    """Minimal stand-in for langsmith.run_trees.RunTree.

    Captures the calls the tracer makes so the test can assert on them
    without a network round-trip.
    """
    instances: list["_FakeRunTree"] = []

    def __init__(self, *, name, run_type, inputs=None, extra=None, project_name=None, **_):
        self.name = name
        self.run_type = run_type
        self.inputs = inputs or {}
        self.extra = extra or {}
        self.project_name = project_name
        self.children: list[_FakeRunTree] = []
        self.outputs: dict = {}
        self.posted = False
        self.ended = False
        self.patched = False
        self.error = None
        _FakeRunTree.instances.append(self)

    def post(self):
        self.posted = True

    def end(self, error=None):
        self.ended = True
        self.error = error

    def patch(self):
        self.patched = True

    def add_outputs(self, outputs):
        self.outputs.update(outputs)

    def create_child(self, *, name, run_type, inputs=None, extra=None):
        child = _FakeRunTree(
            name=name, run_type=run_type, inputs=inputs, extra=extra,
            project_name=self.project_name,
        )
        self.children.append(child)
        return child


@pytest.fixture
def _enabled_tracer(monkeypatch):
    """Stand up the tracer in enabled mode with the fake RunTree + a
    trivial tracing_context shim so child runs see their parent."""
    _FakeRunTree.instances.clear()

    monkeypatch.setattr("app.core.config.settings.LANGSMITH_API_KEY", "fake-key", raising=False)
    monkeypatch.setattr("app.core.config.settings.LANGSMITH_PROJECT", "nokvo-one-test", raising=False)
    monkeypatch.setattr("app.core.config.settings.LANGSMITH_TRACING_V2", True, raising=False)

    # Stub langsmith.Client so init_tracer doesn't hit the network.
    fake_langsmith = type(sys)("langsmith")

    class _FakeClient:
        def __init__(self, **_):
            pass

    fake_langsmith.Client = _FakeClient
    sys.modules["langsmith"] = fake_langsmith

    # Stub the submodules the helpers import lazily.
    fake_run_trees = type(sys)("langsmith.run_trees")
    fake_run_trees.RunTree = _FakeRunTree
    sys.modules["langsmith.run_trees"] = fake_run_trees

    fake_run_helpers = type(sys)("langsmith.run_helpers")

    _current_parent: list = [None]

    class _TracingCtx:
        def __init__(self, parent=None):
            self.parent = parent
            self.prev = None

        def __enter__(self):
            self.prev = _current_parent[0]
            _current_parent[0] = self.parent
            return self.parent

        def __exit__(self, exc_type, exc, tb):
            _current_parent[0] = self.prev
            return False

    def _get_current_run_tree():
        return _current_parent[0]

    def _traceable(name=None, run_type=None, metadata=None):
        def _decorator(fn):
            async def _wrapped(*a, **kw):
                # Minimal child-run emission for the chain decorator test.
                parent = _current_parent[0]
                if parent is not None:
                    child = parent.create_child(name=name, run_type=run_type or "chain", inputs={"args": str(a)[:80]})
                    child.post()
                    try:
                        result = await fn(*a, **kw)
                        child.add_outputs({"result_type": type(result).__name__})
                        child.end()
                        child.patch()
                        return result
                    except BaseException as exc:
                        child.end(error=repr(exc))
                        child.patch()
                        raise
                return await fn(*a, **kw)
            return _wrapped
        return _decorator

    fake_run_helpers.tracing_context = _TracingCtx
    fake_run_helpers.get_current_run_tree = _get_current_run_tree
    fake_run_helpers.traceable = _traceable
    sys.modules["langsmith.run_helpers"] = fake_run_helpers

    tracer = _import_tracer()
    tracer.init_tracer()
    assert tracer.is_enabled() is True
    yield tracer

    # Tear down stub modules so other tests get a clean import.
    for mod in ("langsmith", "langsmith.run_trees", "langsmith.run_helpers"):
        sys.modules.pop(mod, None)


@pytest.mark.asyncio
async def test_trace_call_posts_root_and_ends(_enabled_tracer):
    tracer = _enabled_tracer
    async with tracer.trace_call(
        call_id="call-xyz",
        tenant_id="tenant-1",
        organization_id="org-1",
        language="en",
    ) as run:
        assert run is not None
        assert run.name == "voice_call"
        assert run.run_type == "chain"
        assert run.inputs["call_id"] == "call-xyz"
        # tenant_id / organization_id surface in metadata, not just inputs.
        assert run.extra["metadata"]["tenant_id"] == "tenant-1"
        assert run.extra["metadata"]["organization_id"] == "org-1"

    assert run.posted is True
    assert run.ended is True
    assert run.patched is True


@pytest.mark.asyncio
async def test_turn_attaches_under_call(_enabled_tracer):
    tracer = _enabled_tracer
    async with tracer.trace_call(call_id="c1") as call_run:
        async with tracer.trace_turn(turn_index=3, user_text="hello", mode="voice") as turn_run:
            assert turn_run is not None
            assert turn_run.name == "turn:3"
            # The child is recorded on the parent.
            assert turn_run in call_run.children

    assert call_run.ended and call_run.patched
    assert turn_run.ended and turn_run.patched


@pytest.mark.asyncio
async def test_llm_attaches_under_turn(_enabled_tracer):
    tracer = _enabled_tracer
    async with tracer.trace_call(call_id="c1") as call_run:
        async with tracer.trace_turn(turn_index=1, user_text="hi") as turn_run:
            async with tracer.trace_llm(
                name="azure_openai.complete",
                model="gpt-4-1-mini",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=100,
            ) as llm_run:
                assert llm_run is not None
                assert llm_run.run_type == "llm"
                assert llm_run.extra["metadata"]["model"] == "gpt-4-1-mini"
                tracer.end_llm_span(llm_run, {"response": "hello there"})

            assert llm_run.outputs == {"response": "hello there"}
            assert llm_run in turn_run.children


@pytest.mark.asyncio
async def test_traceable_chain_decorator_attaches_when_enabled(_enabled_tracer):
    tracer = _enabled_tracer

    @tracer.traceable_chain("intent_classifier")
    async def classify(text: str) -> str:
        return "kb_question"

    async with tracer.trace_call(call_id="c1") as call_run:
        result = await classify("what's your refund policy")

    assert result == "kb_question"
    # The chain decorator should have posted a child under call_run.
    assert any(c.name == "intent_classifier" for c in call_run.children)


# ── 3. Streaming barge-in contract ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_barge_in_records_partial_and_reraises(_enabled_tracer):
    """The voice pipeline's ``stream()`` wrapper buffers tokens then, on
    ``asyncio.CancelledError``, records the partial buffer with
    ``status="cancelled_barge_in"`` and re-raises so the turn arbiter
    can tear down TTS. Mirror that pattern here as a contract test."""
    tracer = _enabled_tracer

    buffer: list[str] = []

    async def _simulated_stream_consumer():
        async with tracer.trace_llm(
            name="azure_openai.stream_prosody",
            model="gpt-4-1-mini",
        ) as span:
            try:
                for chunk in ["Hello ", "there, "]:
                    buffer.append(chunk)
                # Simulate the voice arbiter cancelling mid-stream.
                raise asyncio.CancelledError()
            except asyncio.CancelledError:
                tracer.end_llm_span(span, {
                    "response": "".join(buffer),
                    "status": "cancelled_barge_in",
                    "tokens_before_cancel": len(buffer),
                })
                raise

    with pytest.raises(asyncio.CancelledError):
        await _simulated_stream_consumer()

    # Pull the llm span out of the global recorder.
    llm_spans = [r for r in _FakeRunTree.instances if r.run_type == "llm"]
    assert llm_spans, "trace_llm should have created an llm-typed RunTree"
    llm = llm_spans[-1]
    assert llm.outputs.get("status") == "cancelled_barge_in"
    assert llm.outputs.get("tokens_before_cancel") == 2
    assert llm.outputs.get("response") == "Hello there, "
    # The span still ended + patched even though the caller raised.
    assert llm.ended is True
    assert llm.patched is True
