"""Streaming mono PCM16 conversion for call audio."""

from __future__ import annotations

import base64
import sys


class AudioConverter:
    """Convert one continuous audio stream, preserving phase and partial samples."""

    def __init__(self, source_rate: int, target_rate: int, *, decode_ulaw=False, encode_ulaw=False):
        self.source_rate = source_rate
        self.target_rate = target_rate
        self.decode_ulaw = decode_ulaw
        self.encode_ulaw = encode_ulaw
        self.reset()

    def reset(self):
        self._state = None
        self._pending = b""

    def convert(self, payload: str) -> str:
        import audioop

        data = base64.b64decode(payload, validate=True)
        if self.decode_ulaw:
            data = audioop.ulaw2lin(data, 2)
        else:
            data = self._pending + data
            complete = len(data) - len(data) % 2
            self._pending, data = data[complete:], data[:complete]
            if sys.byteorder != "little":
                data = audioop.byteswap(data, 2)
        if not data:
            return ""
        converted, self._state = audioop.ratecv(
            data, 2, 1, self.source_rate, self.target_rate, self._state,
        )
        if self.encode_ulaw:
            converted = audioop.lin2ulaw(converted, 2)
        elif sys.byteorder != "little":
            converted = audioop.byteswap(converted, 2)
        return base64.b64encode(converted).decode("ascii")


class CallAudio:
    """Negotiate call media independently from the 24 kHz realtime session."""

    def __init__(self):
        self.configure(None)

    def configure(self, descriptor):
        if descriptor is None:
            rate, ulaw = 8000, True
        elif not isinstance(descriptor, dict):
            raise ValueError("Unsupported call audio format")
        elif (descriptor.get("encoding"), descriptor.get("sample_rate"), descriptor.get("channels")) == ("L16", 16000, 1):
            rate, ulaw = 16000, False
        elif (str(descriptor.get("encoding", "")).lower(), descriptor.get("sample_rate"), descriptor.get("channels")) == ("pcmu", 8000, 1):
            rate, ulaw = 8000, True
        else:
            raise ValueError("Unsupported call audio format")
        self.inbound = AudioConverter(rate, 24000, decode_ulaw=ulaw)
        self.outbound = AudioConverter(24000, rate, encode_ulaw=ulaw)
