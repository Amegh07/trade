"""
Groq / LLaMA 3 70B Meta-Layer — ADVISORY ONLY.

This module uses LLaMA 3 70B Versatile via Groq API as a:
  1. Market regime classifier
  2. Signal sanity validator
  3. Post-cycle summary narrator

It NEVER generates trade signals, predicts prices, or recommends lot sizes.
The Alpha Engine's statistical pipeline (correlation, AC1, ATR, Chronos) is
the sole authority on entry/exit. Groq provides an independent second opinion
that can VETO a trade only when its confidence is high AND the mathematical
edge is only marginally above threshold.

Failure contract:
  - If Groq is not configured → always returns APPROVED (math-only mode)
  - If API times out (>5s) → always returns APPROVED (never blocks the loop)
  - If response is malformed JSON → always returns APPROVED
  - The bot NEVER crashes due to a failure in this layer
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("GroqMeta")

try:
    from groq import AsyncGroq
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    AsyncGroq = None  # type: ignore
    logger.info("groq SDK not installed. Run: pip install groq  (Groq layer disabled)")


@dataclass
class GroqVerdict:
    regime:          str              # "RANGE" | "TREND" | "CHOPPY" | "UNKNOWN"
    trade_permitted: bool             # LLaMA's advisory recommendation
    confidence:      float            # 0.0–1.0 (Groq's certainty in its verdict)
    concerns:        list = field(default_factory=list)  # specific risk flags raised
    reasoning:       str  = ""        # one-sentence explanation
    available:       bool = True      # False when Groq was unreachable


# Returned whenever Groq is unavailable — always allows the math to decide
_FALLBACK = GroqVerdict(
    regime="UNKNOWN", trade_permitted=True, confidence=0.0,
    concerns=[], reasoning="Groq unavailable — operating in math-only mode.",
    available=False
)

_SYSTEM_PROMPT = """You are a quantitative trading regime classifier.

Your ONLY function is to analyze pre-computed statistical metrics and
classify the MARKET REGIME. You MUST NOT predict future prices or suggest entries.

Strategy context: Statistical arbitrage pairs trading — a MEAN-REVERSION strategy.
It profits when a spread diverges then reverts. It FAILS during directional trends.

RESPOND ONLY with a valid JSON object (no markdown, no extra text):
{
  "regime": "RANGE" or "TREND" or "CHOPPY",
  "trade_permitted": true or false,
  "confidence": integer 0-100,
  "concerns": ["brief concern", "brief concern"],
  "reasoning": "one sentence"
}

Classification rules:
- RANGE:  AC1 < 0 (mean-reverting), correlation stable, normal ATR → trade_permitted: true
- TREND:  AC1 > 0 (momentum), spread moving directionally → trade_permitted: false
- CHOPPY: ATR spike, correlation unstable, unclear regime → trade_permitted: false
- Low correlation approaching 0.6 threshold → add "pair integrity degrading" concern
- Edge multiple < 1.5× → add "edge barely covers cost" concern
- trade_permitted: true ONLY when regime=RANGE AND no critical concerns
"""


def build_client(api_key: str) -> "AsyncGroq | None":
    """Construct an AsyncGroq client from config. Returns None if unavailable."""
    if not api_key or not _SDK_AVAILABLE:
        return None
    try:
        return AsyncGroq(api_key=api_key)
    except Exception as e:
        logger.warning(f"Failed to initialize Groq client: {e}")
        return None


async def classify_regime(pair_id: str, metrics: dict, client) -> GroqVerdict:
    """
    Query LLaMA 3 70B Versatile for regime classification.

    ADVISORY ONLY — does NOT generate trade signals or price targets.
    Always falls back to APPROVED if Groq is unavailable or too slow.

    Args:
        pair_id:  e.g., "AUDUSD/NZDUSD"
        metrics:  pre-computed dict from AlphaEngine:
                    correlation, ac1, atr_a, atr_b, current_spread,
                    divergence_pct, round_trip_cost_pct, edge_multiple
        client:   AsyncGroq instance or None

    Returns:
        GroqVerdict — trade_permitted is ADVISORY, math engine has final say
    """
    if client is None or not _SDK_AVAILABLE:
        return _FALLBACK

    user_content = f"""Classify market regime for pair: {pair_id}

Pre-computed metrics from trading engine:
  pearson_correlation   : {metrics.get('correlation', 0):.4f}  (512 M5 candles; >0.7 healthy)
  ac1_spread_returns    : {metrics.get('ac1', 0):.4f}          (lag-1 autocorr; <0 = mean-reverting RANGE)
  atr_leg_a             : {metrics.get('atr_a', 0):.5f}        (14-period ATR, M5)
  atr_leg_b             : {metrics.get('atr_b', 0):.5f}        (14-period ATR, M5)
  current_spread        : {metrics.get('current_spread', 0):.5f}
  chronos_divergence_pct: {metrics.get('divergence_pct', 0):.4f}%  (AI-predicted 1-bar spread move)
  round_trip_cost_pct   : {metrics.get('round_trip_cost_pct', 0):.4f}%  (total broker execution cost)
  edge_multiple         : {metrics.get('edge_multiple', 0):.2f}×  (divergence ÷ cost; >1.0 = positive EV)

Is this pair in a suitable RANGE regime for mean-reversion stat-arb entry?
"""

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_content},
                ],
                temperature=0.05,   # near-deterministic for reproducible classification
                max_tokens=300,
            ),
            timeout=5.0             # never block the trading loop more than 5 seconds
        )
        raw = response.choices[0].message.content.strip()

        # Robust JSON extraction — handles markdown code fences and surrounding text
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError(f"No JSON object in response: {raw[:200]}")

        data = json.loads(raw[start:end])
        return GroqVerdict(
            regime=          str(data.get("regime", "UNKNOWN")),
            trade_permitted= bool(data.get("trade_permitted", True)),
            confidence=      float(data.get("confidence", 0)) / 100.0,
            concerns=        [str(c) for c in data.get("concerns", [])],
            reasoning=       str(data.get("reasoning", "")),
            available=       True,
        )

    except asyncio.TimeoutError:
        logger.warning(f"[{pair_id}] GroqMeta: API timeout (>5s). Math-only mode.")
        return _FALLBACK
    except json.JSONDecodeError as e:
        logger.warning(f"[{pair_id}] GroqMeta: JSON parse error ({e}). Math-only mode.")
        return _FALLBACK
    except Exception as e:
        logger.warning(f"[{pair_id}] GroqMeta: {type(e).__name__}: {e}. Math-only mode.")
        return _FALLBACK
