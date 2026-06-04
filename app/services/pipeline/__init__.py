"""Sub-package for the voice-pipeline's pure building blocks.

The main pipeline orchestrator lives at
:mod:`app.services.nokvo_one_voice_pipeline` (the historical
``NokvoOneVoicePipeline`` class). It's grown well past 5000 lines and
mixes high-level routing with low-level message composition.

This subpackage is the first step of the de-monolithing: move pure /
synchronous helpers OUT so the orchestrator becomes a thin coordinator.

Current contents:

  * :mod:`.message_composer` — system-prompt + chat-history builders for
    the RAG and small-talk paths (formerly ``_messages`` and
    ``_messages_smalltalk`` on the pipeline class).

The pipeline class re-exports the legacy names so existing call sites
keep working without changes.
"""
