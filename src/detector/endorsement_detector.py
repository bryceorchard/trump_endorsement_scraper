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


def detect_endorsement(text: str, timeout: int = config.OLLAMA_TIMEOUT) -> EndorsementResult:
    """
    Analyze text for Trump company endorsements.

    Args:
        text: The text to analyze (tweet, post, transcript excerpt, etc.)
        timeout: Request timeout in seconds (inference can be slow on RPi5)

    Returns:
        EndorsementResult dataclass
    """
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
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        response.raise_for_status()
        raw_response = response.json()["response"].strip()

        # Strip markdown code fences if model wraps output in them
        if raw_response.startswith("```"):
            raw_response = raw_response.split("```")[1]
            if raw_response.startswith("json"):
                raw_response = raw_response[4:]

        data = json.loads(raw_response)

        return EndorsementResult(
            endorsement_detected=data.get("endorsement_detected", False),
            company=data.get("company"),
            ticker=data.get("ticker"),
            confidence=data.get("confidence", "low"),
            quote=data.get("quote"),
            endorsement_type=data.get("endorsement_type", "none"),
            raw_text=text,
        )

    except requests.exceptions.ConnectionError:
        raise RuntimeError("Ollama is not running. Start it with: ollama serve")
    except json.JSONDecodeError as e:
        raise ValueError(f"Model returned invalid JSON: {raw_response}") from e


def is_actionable(result: EndorsementResult) -> bool:
    """Returns True if the result warrants sending an alert."""
    return (
        result.endorsement_detected
        and result.confidence in ("high", "medium")
        and result.endorsement_type != "none"
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
