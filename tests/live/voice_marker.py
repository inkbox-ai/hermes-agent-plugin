"""Build an ASR-friendly, run-specific marker for the live hosted-call proof."""

from __future__ import annotations

import sys


NATO_WORDS = (
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliett",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
    "quebec",
    "romeo",
    "sierra",
    "tango",
    "uniform",
    "victor",
    "whiskey",
    "xray",
    "yankee",
    "zulu",
)


def marker_from_token(token: str) -> str:
    """Map a short numeric run token to distinct NATO words.

    Position offsets keep repeated digits distinct, preventing a hosted-agent
    action summarizer from collapsing an adjacent duplicate while preserving a
    deterministic marker that has crossed TTS -> PSTN -> STT.
    """
    digits = "".join(character for character in token if character.isdigit())
    if not digits:
        raise ValueError("the live voice marker token must contain a digit")
    if len(digits) > len(NATO_WORDS):
        raise ValueError("the live voice marker token is too long")

    selected: list[str] = []
    used: set[str] = set()
    for position, digit in enumerate(digits):
        index = (int(digit) + position * 10) % len(NATO_WORDS)
        while NATO_WORDS[index] in used:
            index = (index + 1) % len(NATO_WORDS)
        word = NATO_WORDS[index]
        selected.append(word)
        used.add(word)
    return " ".join(selected)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: voice_marker.py RUN_TOKEN")
    print(marker_from_token(sys.argv[1]))
