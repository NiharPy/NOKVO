"""Sub-package for the voice STREAM service's pure building blocks.

The session orchestrator lives at
:mod:`app.services.nokvo_one_voice_stream_service`
(``NokvoOneVoiceStreamService``). Like its sibling :mod:`app.services.pipeline`
(the turn processor's extractions), this subpackage holds the pure /
leaf helpers moved OUT of the 5000+-line module so the orchestrator
becomes a thin coordinator.

The stream-service module re-exports every legacy name so existing call
sites and tests keep working without changes.
"""
