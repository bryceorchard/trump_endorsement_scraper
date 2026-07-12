"""
endorsement_detector.py

Detects Trump company endorsements in text using Qwen3-8B via Ollama.
Returns structured JSON so the rest of the pipeline can act on results.

Usage:
    from detector.endorsement_detector import detect_endorsement
    result = detect_endorsement("Just met with the amazing people at Apple...")
"""

import json
import requests
from dataclasses import dataclass
from typing import Optional

from config import config

OLLAMA_URL = config.OLLAMA_URL
MODEL      = config.OLLAMA_MODEL

# Placeholder strings the model may emit instead of JSON null (the prompt below
# literally says "or null"), normalized to None so they don't count as a real
# company/ticker or pollute the DB.
_NULLISH = {"", "null", "none", "n/a", "n.a.", "unknown"}


class DetectionTimeout(Exception):
    """The Ollama call for a single item timed out.

    Distinct from RuntimeError (which pauses detection): the caller treats this
    as an item-level failure and moves on, so one over-long item can't wedge the
    whole queue. A genuinely-down Ollama surfaces as ConnectionError instead.
    """


def _nullish_to_none(value):
    if isinstance(value, str) and value.strip().lower() in _NULLISH:
        return None
    return value


SYSTEM_PROMPT = """You are an AI that analyzes statements made by Donald Trump and detects whether he is endorsing or promoting a specific company, brand, or financial asset (stocks, crypto, etc.).

Respond ONLY with valid JSON in this exact format:
{
  "endorsement_detected": true or false,
  "company": "Company name or null",
  "ticker": "Stock ticker if known or null",
  "confidence": "high" | "medium" | "low",
  "quote": "The specific phrase that indicates endorsement, or null",
  "endorsement_type": "explicit" | "implicit" | "financial" | "none"
}

endorsement_type definitions:
- explicit: Trump directly says to buy, invest in, or support the company
- implicit: Trump praises the company/CEO in a way that implies support
- financial: Trump references a stock, crypto, or financial product
- none: No endorsement detected"""


@dataclass
class EndorsementResult:
    endorsement_detected: bool
    company: Optional[str]
    ticker: Optional[str]
    confidence: str
    quote: Optional[str]
    endorsement_type: str
    raw_text: str


def detect_endorsement(text: str, timeout: int | None = None) -> EndorsementResult:
    """
    Analyze text for Trump company endorsements.

    Args:
        text: The text to analyze (tweet, post, transcript excerpt, etc.)
        timeout: Request timeout in seconds (defaults to config.OLLAMA_TIMEOUT;
                 inference is slow on an RPi5, and the first call after idle
                 also pays the model-load time)

    Returns:
        EndorsementResult dataclass

    Raises:
        RuntimeError: Ollama is unreachable or the request failed (down, timed
            out, model not pulled, 5xx). The caller should pause detection and
            leave items unprocessed — these failures are not the item's fault.
        ValueError: the model responded but with unparseable output; safe to
            treat as a per-item failure.
    """
    if timeout is None:
        timeout = config.OLLAMA_TIMEOUT

    payload = {
        "model": MODEL,
        "system": SYSTEM_PROMPT,
        "prompt": f"Analyze this statement for company endorsements:\n\n{text}",
        "stream": False,
        "options": {
            "temperature": 0.1,   # Low temp for consistent structured output
            "num_predict": 256,   # We only need a short JSON response
        },
        # Disable Qwen3's thinking mode for faster responses on simple extraction
        "think": False,
        # Keep the model resident between detection cycles — reloading 5 GB on
        # a Pi takes ~a minute, and Ollama's default is to unload after 5 min.
        "keep_alive": "30m",
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError("Ollama is not running. Start it with: ollama serve") from exc
    except requests.exceptions.Timeout as exc:
        # A single call timing out usually means THIS item is too long, not that
        # Ollama is down — raise a distinct type so the caller skips the item
        # rather than pausing the loop (which would wedge forever on a poison item).
        raise DetectionTimeout(f"Ollama timed out after {timeout}s") from exc
    except requests.exceptions.RequestException as exc:
        # HTTP 404 (model not pulled), 5xx, other transport errors → pause.
        raise RuntimeError(f"Ollama request failed: {exc}") from exc

    raw_response = ""
    try:
        raw_response = response.json()["response"].strip()

        # Strip markdown code fences if model wraps output in them
        if raw_response.startswith("```"):
            raw_response = raw_response.split("```")[1]
            if raw_response.startswith("json"):
                raw_response = raw_response[4:]

        data = json.loads(raw_response)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        raise ValueError(f"Model returned invalid JSON: {raw_response[:500]!r}") from e

    return EndorsementResult(
        endorsement_detected=data.get("endorsement_detected", False),
        company=_nullish_to_none(data.get("company")),
        ticker=_nullish_to_none(data.get("ticker")),
        confidence=data.get("confidence", "low"),
        quote=_nullish_to_none(data.get("quote")),
        endorsement_type=data.get("endorsement_type", "none"),
        raw_text=text,
    )


def is_actionable(result: EndorsementResult) -> bool:
    """Returns True if the result warrants sending an alert.

    Requires a concrete company or ticker: an "endorsement" naming neither has
    nothing to act on. This is what filters out the model's spurious hits on
    general economic commentary ("Record Stock Market...") and political
    endorsements of *people* ("he has my Complete and Total Endorsement") — both
    of which it otherwise flags as detected with company=None, ticker=None.
    """
    return (
        result.endorsement_detected
        and result.confidence in ("high", "medium")
        and result.endorsement_type != "none"
        and bool(result.company or result.ticker)
    )


# ── Quick test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_cases = [
        "Just had a GREAT meeting with Tim Cook. Apple is doing TREMENDOUS things for America!",
        "The fake news media is at it again. Very sad!",
        "Buy $TRUMP coin now - it's going to be HUGE. The best coin, everyone says so.",
        "We're bringing jobs back to Ohio. Great people, great state.",
    ]

    for text in test_cases:
        print(f"\nInput: {text[:80]}...")
        result = detect_endorsement(text)
        print(f"  Detected:  {result.endorsement_detected}")
        print(f"  Company:   {result.company}")
        print(f"  Ticker:    {result.ticker}")
        print(f"  Type:      {result.endorsement_type}")
        print(f"  Confidence:{result.confidence}")
        print(f"  Actionable:{is_actionable(result)}")
        if result.quote:
            print(f'  Quote:     "{result.quote}"')
