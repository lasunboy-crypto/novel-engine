"""Caller-owned unverified generation checkpoints; never substitute for validation."""
from typing import Protocol

from src.core.llm.base import LLMResponse


class GenerationCheckpointError(RuntimeError):
    """Damaged or unwritable checkpoint: stop before another paid generation."""


class GenerationCheckpoint(Protocol):
    def load(self, request_hash: str) -> LLMResponse | None: ...
    def store(self, request_hash: str, response: LLMResponse) -> None: ...
    def reject(self, request_hash: str) -> None: ...
