"""Strict advisor output schema (SPEC §5.5).

The advisor can only say "approve" or "veto" (A1). There is no field for
quantity, new orders or config. Anything else fails validation, which the
advisor treats as "unavailable" (A3).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AdvisorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["approve", "veto"]
    reasons: list[str] = Field(min_length=1, max_length=5)
    confidence: float = Field(ge=0.0, le=1.0)
    plain_english: str = Field(min_length=1, max_length=600)


def json_schema() -> dict:
    """JSON Schema sent to the model as a structured-output format."""
    return {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["approve", "veto"]},
            "reasons": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
            "plain_english": {"type": "string"},
        },
        "required": ["verdict", "reasons", "confidence", "plain_english"],
        "additionalProperties": False,
    }
