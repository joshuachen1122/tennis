"""Claude in two roles:

* **Trade reviewer** - gives a take/skip verdict with its own probability. For strategies with
  `llm_gate` it is asked *before* entry and a skip blocks the paper trade; for the others it
  reviews after entry in shadow. Verdicts are stored and scored once the match settles.
* **Strategy analyst** - every few hours, reads the aggregated paper results and recorded-data
  diagnostics and proposes parameter changes and new hypotheses (shown on the dashboard).

Fails closed and stays inside a daily budget.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens: (input, output, cache read). Cache writes bill 1.25x input.
PRICING = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


class TradeVerdict(BaseModel):
    take: bool
    probability: float = Field(ge=0.0, le=1.0, description="Your probability that the bought side wins the match")
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    risk_flags: list[str]


class ParamSuggestion(BaseModel):
    strategy: str
    param: str
    current: str
    suggested: str
    rationale: str


class AnalystReport(BaseModel):
    summary: str
    findings: list[str]
    parameter_suggestions: list[ParamSuggestion]
    new_hypotheses: list[str]
    data_gaps: list[str]


REVIEW_SYSTEM = """\
You review paper trades made by an automated system on Kalshi tennis match-winner contracts \
(binary: pays $1 if the player wins the match). Retirement after the first ball counts as a loss \
for the retiring player; a walkover before the first ball settles at a 'fair price'.

The system prices each match with a point-based Markov model: each player's probability of \
winning a point on serve is backed out of the pre-match market price (assuming a tour-typical \
sum of serve rates) and then updated with observed serve points. It buys when the model \
probability (blended with the market mid by `model_weight`; the raw model is also shown) exceeds the ask by a margin, only while the score and price have been stable for a \
few seconds. The `favourite` strategy ignores the model: it prices off the market mid stretched \
toward the favourite (`market_slope`), betting that in-play favourites are underpriced.

Judge whether the model is likely right and the market wrong *in this situation*. Typical \
reasons the model is wrong: the pre-match calibration is stale or the serve-rate sum is off for \
this player pair/surface; injury, fatigue or a medical timeout the model cannot see; momentum \
or break-back dynamics; very low liquidity where the quote is not a real price; the score feed \
lagging the market. You have no internet access; judge only from the context given.\
"""

ANALYST_SYSTEM = """\
You are the research lead for a paper-trading system on Kalshi tennis match-winner markets. You \
receive aggregated results of several strategies and diagnostics from recorded data (model vs \
market calibration, markouts after entry, results by tier, price bucket, edge bucket and match \
state). Identify what is working, what is not, and why, using only the numbers given. Be \
skeptical of small samples: say how many trades a conclusion rests on, and prefer changes that \
are robust (thresholds, filters, sizing) over fragile curve-fitting. Suggest concrete parameter \
changes using the exact parameter names shown, and new hypotheses that the recorded data could \
test. Note any data that is missing for a sound conclusion.\
"""


@dataclass
class Usage:
    model: str
    cost_usd: float


def _usage(response: Any, model: str) -> Usage:
    u = getattr(response, "usage", None)
    m = getattr(response, "model", None) or model
    inp, out, cr = PRICING.get(m, PRICING["claude-opus-5-5"])
    if u is None:
        return Usage(m, 0.0)
    cost = (
        (getattr(u, "input_tokens", 0) or 0) * inp
        + (getattr(u, "output_tokens", 0) or 0) * out
        + (getattr(u, "cache_read_input_tokens", 0) or 0) * cr
        + (getattr(u, "cache_creation_input_tokens", 0) or 0) * inp * 1.25
    ) / 1e6
    return Usage(m, cost)


class LLM:
    def __init__(self, model: str = "claude-opus-5-5", review_effort: str = "low", analyst_effort: str = "high") -> None:
        self.model = model
        self.review_effort = review_effort
        self.analyst_effort = analyst_effort
        self.client = AsyncAnthropic(timeout=120.0, max_retries=2)

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    async def _parse(self, system: str, user: str, schema: type[BaseModel], effort: str, max_tokens: int) -> tuple[Optional[BaseModel], Usage, str]:
        try:
            response = await self.client.beta.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                output_config={"effort": effort},
                output_format=schema,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.RateLimitError as exc:
            logger.warning("LLM rate limited: %s", exc)
            return None, Usage(self.model, 0.0), "rate_limited"
        except anthropic.APIStatusError as exc:
            logger.warning("LLM API error %s: %s", exc.status_code, exc.message)
            return None, Usage(self.model, 0.0), f"api_error_{exc.status_code}"
        except anthropic.APIConnectionError as exc:
            logger.warning("LLM connection error: %s", exc)
            return None, Usage(self.model, 0.0), "connection_error"
        except (ValidationError, ValueError) as exc:
            logger.warning("LLM invalid output: %s", exc)
            return None, Usage(self.model, 0.0), "invalid_output"
        usage = _usage(response, self.model)
        if response.stop_reason in ("refusal", "max_tokens"):
            return None, usage, str(response.stop_reason)
        return response.parsed_output, usage, "ok"

    async def review_trade(self, context: dict[str, Any], proposed: bool = False) -> tuple[Optional[TradeVerdict], Usage, str]:
        head = "Proposed paper trade (it is placed only if you say take):\n" if proposed else "Paper trade just entered:\n"
        user = head + json.dumps(context, indent=2, sort_keys=True, default=str) + "\n\nWould you take this trade?"
        out, usage, status = await self._parse(REVIEW_SYSTEM, user, TradeVerdict, self.review_effort, 8000)
        return out, usage, status  # type: ignore[return-value]

    async def analyze(self, report: dict[str, Any]) -> tuple[Optional[AnalystReport], Usage, str]:
        user = "Current results and diagnostics:\n" + json.dumps(report, indent=2, sort_keys=True, default=str)
        out, usage, status = await self._parse(ANALYST_SYSTEM, user, AnalystReport, self.analyst_effort, 32000)
        return out, usage, status  # type: ignore[return-value]
