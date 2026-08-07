"""Build an ASR-friendly, run-specific marker for the live hosted-call proof."""

from __future__ import annotations

import hashlib
import sys


# Everyday words avoid conventional phonetic-alphabet phrase bias while
# remaining distinct, single-token spellings through TTS -> PSTN -> STT.
SPEECH_WORDS = (
    "banana",
    "elephant",
    "pineapple",
    "alligator",
    "motorcycle",
    "umbrella",
    "dinosaur",
    "potato",
    "computer",
    "volcano",
    "airplane",
    "butterfly",
    "kangaroo",
    "octopus",
    "calendar",
    "chocolate",
    "hospital",
    "library",
    "sandwich",
    "telescope",
)


def marker_from_token(token: str) -> str:
    """Hash a run identity into three distinct speech-safe words.

    The full token contributes to the hash. Sampling without replacement avoids
    adjacent duplicates that an action summarizer might collapse while providing
    6,840 possible phrases that can cross TTS -> PSTN -> STT reliably.
    """
    if not token.strip():
        raise ValueError("the live voice marker token must not be empty")
    value = int.from_bytes(hashlib.sha256(token.encode()).digest(), "big")
    available = list(SPEECH_WORDS)
    selected: list[str] = []
    for _ in range(3):
        index = value % len(available)
        value //= len(available)
        selected.append(available.pop(index))
    return " ".join(selected)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: voice_marker.py RUN_TOKEN")
    print(marker_from_token(sys.argv[1]))
