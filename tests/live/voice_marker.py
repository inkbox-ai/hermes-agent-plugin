"""Build an ASR-friendly, run-specific marker for the live hosted-call proof."""

from __future__ import annotations

import sys


# Everyday words avoid conventional phonetic-alphabet phrase bias while
# remaining distinct, single-token spellings through TTS -> PSTN -> STT.
SPEECH_WORDS = (
    "apple",
    "basket",
    "candle",
    "dragon",
    "engine",
    "forest",
    "garden",
    "hammer",
    "island",
    "jacket",
    "kettle",
    "lemon",
    "meadow",
    "napkin",
    "orange",
    "pencil",
    "rabbit",
    "silver",
    "tiger",
    "umbrella",
    "velvet",
    "window",
    "yellow",
    "zebra",
    "coffee",
    "sunset",
)


def marker_from_token(token: str) -> str:
    """Map a short numeric run token to distinct speech-safe words.

    Position offsets keep repeated digits distinct, preventing a hosted-agent
    action summarizer from collapsing an adjacent duplicate while preserving a
    deterministic marker that has crossed TTS -> PSTN -> STT.
    """
    digits = "".join(character for character in token if character.isdigit())
    if not digits:
        raise ValueError("the live voice marker token must contain a digit")
    if len(digits) > len(SPEECH_WORDS):
        raise ValueError("the live voice marker token is too long")

    selected: list[str] = []
    used: set[str] = set()
    for position, digit in enumerate(digits):
        index = (int(digit) + position * 10) % len(SPEECH_WORDS)
        while SPEECH_WORDS[index] in used:
            index = (index + 1) % len(SPEECH_WORDS)
        word = SPEECH_WORDS[index]
        selected.append(word)
        used.add(word)
    return " ".join(selected)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: voice_marker.py RUN_TOKEN")
    print(marker_from_token(sys.argv[1]))
