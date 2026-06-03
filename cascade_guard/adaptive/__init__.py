"""Adaptive Resonance — Phase 5 of cascade-guard evolution.

Introduces learning from inference history:
  - Inference History Buffer (LMDB + ZSTD)
  - Novelty Detector (Bloom filter)
  - Experience Replay Controller
  - Meta-Maat Validator (8th pillar: Temporal Consistency)
  - Pattern Mining Engine (event-triggered)
"""

from cascade_guard.adaptive.history_buffer import (
    InferenceHistoryBuffer,
    InferenceTrace,
)
from cascade_guard.adaptive.meta_maat import MetaMaatValidator, TemporalConsistencyResult
from cascade_guard.adaptive.novelty_detector import NoveltyDetector
from cascade_guard.adaptive.replay_controller import (
    BetaPrior,
    ExperienceReplayController,
    ReplayDecision,
)

__all__ = [
    "BetaPrior",
    "ExperienceReplayController",
    "InferenceHistoryBuffer",
    "InferenceTrace",
    "MetaMaatValidator",
    "NoveltyDetector",
    "ReplayDecision",
    "TemporalConsistencyResult",
]
