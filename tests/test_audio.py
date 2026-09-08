import base64
import math
import struct

import pytest

from tests.test_realtime_bridge_parity import (
    _BridgeState, _FakeOpenAIWS, _FakeWS, _meta,
)
from inkbox_plugin.audio import AudioConverter, CallAudio
from inkbox_plugin import realtime


def _encoded(data):
    return base64.b64encode(data).decode()


@pytest.mark.parametrize('source,target', [(16000, 24000), (24000, 16000)])
def test_streaming_conversion_preserves_tone_duration_and_chunk_boundaries(source, target):
    samples = [round(12000 * math.sin(2 * math.pi * 1000 * n / source)) for n in range(source // 10)]
    pcm = struct.pack(f'<{len(samples)}h', *samples)
    expected = base64.b64decode(AudioConverter(source, target).convert(_encoded(pcm)))
    stream = AudioConverter(source, target)
    actual = b''.join(base64.b64decode(stream.convert(_encoded(pcm[n:n + 37]))) for n in range(0, len(pcm), 37))
    assert actual == expected
    assert abs(len(actual) // 2 - target // 10) <= 1
    output = struct.unpack(f'<{len(actual) // 2}h', actual)
    crossings = sum(a < 0 <= b for a, b in zip(output, output[1:]))
    assert 98 <= crossings <= 100
    assert max(output) > 11000


def test_pcm_silence_reset_and_legacy_formats():
    call = CallAudio()
    assert set(base64.b64decode(call.inbound.convert(_encoded(b'\xff' * 160)))) == {0}
    assert set(base64.b64decode(call.outbound.convert(_encoded(bytes(960))))) == {255}
    call.configure({'encoding': 'L16', 'sample_rate': 16000, 'channels': 1})
    assert call.outbound.convert(_encoded(b'\xff')) == ''
    call.outbound.reset()
    assert set(base64.b64decode(call.outbound.convert(_encoded(bytes(960))))) == {0}
    with pytest.raises(ValueError, match='Unsupported'):
        call.configure({'encoding': 'L16', 'sample_rate': 8000, 'channels': 1})


def test_start_descriptor_drives_actual_inbound_conversion(caplog):
    caplog.set_level("INFO")
    import asyncio
    descriptor = {'encoding': 'L16', 'sample_rate': 16000, 'channels': 1}
    peer = _FakeOpenAIWS([
        {'event': 'start', 'start': {'media_format': descriptor, 'stream_id': 'hd-stream'}},
        {'event': 'media', 'media': {'payload': _encoded(bytes(640))}},
    ])
    upstream = _FakeWS()
    state = _BridgeState()
    asyncio.run(realtime._inkbox_to_openai_pump(peer, upstream, state, _meta()))
    append = next(frame for frame in upstream.sent if frame['type'] == 'input_audio_buffer.append')
    assert 956 <= len(base64.b64decode(append['audio'])) <= 960
    assert state.audio.outbound.target_rate == 16000
    assert state.stream_id == "hd-stream"
    assert "call_id=call-123 audio_format=pcm_s16le sample_rate=16000" in caplog.text


def test_hd_output_converts_and_resets_partial_samples_on_interrupt():
    import asyncio
    state = _BridgeState()
    state.audio.configure({'encoding': 'L16', 'sample_rate': 16000, 'channels': 1})
    upstream = _FakeOpenAIWS([
        {'type': 'response.output_audio.delta', 'delta': _encoded(b'\xff')},
        {'type': 'input_audio_buffer.speech_started'},
        {'type': 'response.output_audio.delta', 'delta': _encoded(bytes(960))},
        {'type': 'response.output_audio.done'},
    ])
    peer = _FakeWS()

    async def noop(*args):
        return ''

    asyncio.run(realtime._openai_to_inkbox_pump(
        openai_ws=upstream, inkbox_ws=peer, state=state,
        config=realtime.RealtimeConfig(enabled=True, api_key='test'), meta=_meta(), on_agent_consult=noop,
    ))
    media = [frame for frame in peer.sent if frame.get('event') == 'media']
    assert len(media) == 1
    assert base64.b64decode(media[0]['media']['payload']) == bytes(640)
    assert state.audio.outbound._state is None


def test_session_uses_supported_realtime_pcm_rate():
    import asyncio
    ws = _FakeWS()
    asyncio.run(realtime._send_session_update(ws, realtime.RealtimeConfig(), _meta()))
    audio = ws.sent[0]['session']['audio']
    assert audio['input']['format'] == audio['output']['format'] == {'type': 'audio/pcm', 'rate': 24000}
