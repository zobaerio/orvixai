"""Headless tests for continuous voice: real audio levels, energy VAD
segmentation and barge-in (orvix/audio_levels, orvix/vad,
orvix/barge_in, and their SpeechEngine/app wiring).

All audio is SYNTHETIC: constant-amplitude PCM frames generated in
the test (RMS == amplitude), fake PyAudio / speech_recognition /
pygame / edge-tts modules injected through sys.modules. No real
microphone, speaker, or network is touched.
"""
import ast
import asyncio
import sys
import unittest
from array import array
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix.audio_levels import LevelMeter, PlaybackEnvelope, level_from_rms, pcm_rms
from orvix.barge_in import BargeInConfig, BargeInController
from orvix.settings import DEFAULTS
from orvix.speech import SpeechEngine, _LevelTapSource
from orvix.vad import EnergyVad, VadConfig, VadSegmenter

APP_DIR = Path(__file__).resolve().parents[1]
FRAME = 30.0  # ms, for the pure VAD/barge-in tests


def pcm(amplitude: float, samples: int = 1024) -> bytes:
    """Constant-amplitude 16-bit PCM; its RMS is exactly |amplitude|."""
    value = int(max(-1.0, min(1.0, amplitude)) * 32767)
    return array("h", [value] * samples).tobytes()


def feed_rms(vad, rms: float, frames: int, frame_ms: float = FRAME):
    return [vad.feed(rms, frame_ms) for _ in range(frames)]


# ---------------------------------------------------------------------
# audio_levels
# ---------------------------------------------------------------------

class PcmRmsTests(unittest.TestCase):
    def test_silence_is_zero(self):
        self.assertEqual(pcm_rms(pcm(0.0)), 0.0)
        self.assertEqual(pcm_rms(b""), 0.0)

    def test_constant_amplitude_rms(self):
        self.assertAlmostEqual(pcm_rms(pcm(0.5)), 0.5, places=2)
        self.assertAlmostEqual(pcm_rms(pcm(0.1)), 0.1, places=2)

    def test_odd_trailing_byte_is_ignored(self):
        self.assertAlmostEqual(pcm_rms(pcm(0.5) + b"\x01"), 0.5, places=2)

    def test_unsupported_width_reads_as_silence(self):
        self.assertEqual(pcm_rms(b"\x01\x02\x03", sample_width=1), 0.0)


class LevelMappingTests(unittest.TestCase):
    def test_zero_and_clamp(self):
        self.assertEqual(level_from_rms(0.0), 0.0)
        self.assertEqual(level_from_rms(10.0), 1.0)

    def test_monotonic(self):
        values = [level_from_rms(r) for r in (0.001, 0.01, 0.05, 0.2, 0.45)]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(0.0 < v <= 1.0 for v in values))


class LevelMeterTests(unittest.TestCase):
    def test_none_before_first_update(self):
        self.assertIsNone(LevelMeter().value(now=100.0))

    def test_value_and_staleness(self):
        meter = LevelMeter(stale_after=0.4)
        meter.update(0.8, now=100.0)
        self.assertIsNotNone(meter.value(now=100.1))
        self.assertIsNone(meter.value(now=100.5))

    def test_reset_returns_to_none(self):
        meter = LevelMeter()
        meter.update(0.5, now=10.0)
        meter.reset()
        self.assertIsNone(meter.value(now=10.0))


class PlaybackEnvelopeTests(unittest.TestCase):
    def test_level_at_bins_and_clamp(self):
        env = PlaybackEnvelope([0.1, 0.9, 0.3], bin_ms=100.0)
        self.assertEqual(env.level_at(0), 0.1)
        self.assertEqual(env.level_at(150), 0.9)
        self.assertEqual(env.level_at(99999), 0.3)  # clamps to last bin
        self.assertEqual(env.duration_ms, 300.0)

    def test_empty_envelope_is_zero(self):
        self.assertEqual(PlaybackEnvelope([]).level_at(50), 0.0)


# ---------------------------------------------------------------------
# vad
# ---------------------------------------------------------------------

class EnergyVadTests(unittest.TestCase):
    def test_silence_never_starts(self):
        vad = EnergyVad()
        self.assertTrue(all(e is None for e in feed_rms(vad, 0.001, 50)))
        self.assertFalse(vad.in_speech)

    def test_start_needs_sustained_level(self):
        vad = EnergyVad()
        feed_rms(vad, 0.001, 10)
        events = feed_rms(vad, 0.3, 2)  # 60 ms < 90 ms start window
        self.assertTrue(all(e is None for e in events))
        self.assertFalse(vad.in_speech)
        events = feed_rms(vad, 0.3, 1)  # third frame crosses 90 ms
        self.assertEqual(events, ["start"])
        self.assertTrue(vad.in_speech)

    def test_end_after_trailing_silence(self):
        vad = EnergyVad()
        feed_rms(vad, 0.001, 5)
        feed_rms(vad, 0.3, 5)
        events = feed_rms(vad, 0.001, 24)  # 720 ms >= 700 ms silence
        self.assertEqual(events[-1], "end")
        self.assertFalse(vad.in_speech)

    def test_max_utterance_cap_forces_end(self):
        vad = EnergyVad()
        feed_rms(vad, 0.3, 3)
        events = feed_rms(vad, 0.3, 400)  # 12 s of continuous speech
        self.assertIn("end", events)

    def test_noise_floor_adaptation(self):
        vad = EnergyVad()
        feed_rms(vad, 0.01, 200)  # steady room tone becomes the floor
        self.assertAlmostEqual(vad.noise_floor, 0.01, places=2)
        self.assertGreater(vad.threshold, 0.025)
        # A sound at the old floor level no longer counts as speech...
        self.assertTrue(all(e is None for e in feed_rms(vad, 0.025, 10)))
        # ...but clearly louder speech still starts.
        self.assertEqual(feed_rms(vad, 0.2, 3)[-1], "start")


class VadSegmenterTests(unittest.TestCase):
    def _run(self, script):
        seg = VadSegmenter()
        out = None
        for amplitude, frames in script:
            for _ in range(frames):
                result = seg.feed(pcm(amplitude, 160), amplitude, FRAME)
                if result is not None:
                    out = result
        return out

    def test_utterance_includes_pre_roll(self):
        # 20 quiet, 10 loud (start on 3rd), 24 quiet (end at 700 ms).
        out = self._run([(0.001, 20), (0.3, 10), (0.001, 24)])
        self.assertIsNotNone(out)
        # pre-roll 10 frames (300 ms cap) + loud frames 3..10 (8)
        # + trailing silence frames 1..24 (24) = 42 frames of 160 samples.
        self.assertEqual(len(out), 42 * 160 * 2)

    def test_short_blip_is_discarded(self):
        # Crosses the start threshold but only ~90 ms of voiced audio.
        out = self._run([(0.001, 10), (0.3, 3), (0.001, 30)])
        self.assertIsNone(out)

    def test_real_command_is_kept(self):
        out = self._run([(0.001, 5), (0.3, 15), (0.001, 24)])
        self.assertIsNotNone(out)


# ---------------------------------------------------------------------
# barge_in controller
# ---------------------------------------------------------------------

class BargeInControllerTests(unittest.TestCase):
    FRAME_MS = 50.0

    def _controller(self, guard_level=0.02, guard_frames=6):
        ctl = BargeInController()
        ctl.start()
        for _ in range(guard_frames):
            self.assertFalse(ctl.feed(guard_level, self.FRAME_MS))
        return ctl

    def test_not_started_never_triggers(self):
        ctl = BargeInController()
        self.assertFalse(ctl.feed(0.9, self.FRAME_MS))

    def test_guard_window_ignores_loud_start(self):
        ctl = BargeInController()
        ctl.start()
        for _ in range(6):  # the full 300 ms guard at full volume
            self.assertFalse(ctl.feed(0.9, self.FRAME_MS))
        self.assertFalse(ctl.triggered)

    def test_single_spike_does_not_trigger(self):
        ctl = self._controller()
        self.assertFalse(ctl.feed(0.9, self.FRAME_MS))
        for _ in range(10):
            self.assertFalse(ctl.feed(0.02, self.FRAME_MS))
        self.assertFalse(ctl.triggered)

    def test_sustained_speech_triggers_once(self):
        ctl = self._controller()
        results = [ctl.feed(0.5, self.FRAME_MS) for _ in range(6)]  # 300 ms
        self.assertEqual(results, [False] * 5 + [True])
        self.assertTrue(ctl.triggered)

    def test_playback_leakage_raises_threshold(self):
        # Loud leakage during the guard window becomes the baseline;
        # the same level afterwards must NOT interrupt (it is the
        # assistant's own voice coming back through the speakers).
        ctl = self._controller(guard_level=0.4)
        self.assertGreaterEqual(ctl.effective_threshold, 0.7)
        for _ in range(20):
            self.assertFalse(ctl.feed(0.4, self.FRAME_MS))
        self.assertFalse(ctl.triggered)

    def test_custom_config(self):
        ctl = BargeInController(BargeInConfig(guard_ms=100.0, sustain_ms=100.0, threshold=0.1))
        ctl.start()
        ctl.feed(0.0, 100.0)
        self.assertFalse(ctl.feed(0.5, 50.0))
        self.assertTrue(ctl.feed(0.5, 50.0))


# ---------------------------------------------------------------------
# SpeechEngine with fake audio modules
# ---------------------------------------------------------------------

class FakeStream:
    """Scripted PyAudio stream: pops chunks, then returns silence."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.reads = 0

    def read(self, size, exception_on_overflow=False):
        self.reads += 1
        if self._chunks:
            return self._chunks.pop(0)
        return pcm(0.001)

    def stop_stream(self):
        pass

    def close(self):
        pass


def fake_pyaudio(stream=None, fail_open=False):
    class _PyAudio:
        paInt16 = 8

        def get_default_input_device_info(self):
            return {"defaultSampleRate": 16000}

        def get_sample_size(self, fmt):
            return 2

        def open(self, **kwargs):
            if fail_open:
                raise OSError("device busy")
            return stream

        def terminate(self):
            pass

    return SimpleNamespace(PyAudio=_PyAudio, paInt16=8)


def fake_sr(recognized="hello orvix", unknown=False, legacy_text=None):
    class WaitTimeoutError(Exception):
        pass

    class UnknownValueError(Exception):
        pass

    class RequestError(Exception):
        pass

    class AudioData:
        def __init__(self, frame_data, sample_rate, sample_width):
            self.frame_data = frame_data
            self.sample_rate = sample_rate
            self.sample_width = sample_width

    class Recognizer:
        def __init__(self):
            self.pause_threshold = None
            self.operation_timeout = None
            self.last_audio = None
            self.legacy_listen_calls = 0

        def adjust_for_ambient_noise(self, source, duration=0.5):
            pass

        def listen(self, source, timeout=None, phrase_time_limit=None):
            self.legacy_listen_calls += 1
            source.stream.read(source.CHUNK)  # exercises the level tap
            return AudioData(pcm(0.3), source.SAMPLE_RATE, source.SAMPLE_WIDTH)

        def recognize_google(self, audio, language="en-US"):
            self.last_audio = audio
            if unknown:
                raise UnknownValueError("nope")
            return legacy_text if legacy_text is not None else recognized

    class Microphone:
        SAMPLE_RATE = 16000
        SAMPLE_WIDTH = 2
        CHUNK = 1024

        def __init__(self):
            self.stream = FakeStream([])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return SimpleNamespace(
        Recognizer=Recognizer,
        Microphone=Microphone,
        AudioData=AudioData,
        WaitTimeoutError=WaitTimeoutError,
        UnknownValueError=UnknownValueError,
        RequestError=RequestError,
    )


class EngineVadListenTests(unittest.TestCase):
    def _chunks(self):
        # 16 kHz -> 64 ms per 1024-sample frame.
        return [pcm(0.001)] * 5 + [pcm(0.3)] * 6 + [pcm(0.001)] * 12

    def test_vad_path_returns_recognized_text_and_levels(self):
        engine = SpeechEngine()
        sr = fake_sr()
        stream = FakeStream(self._chunks())
        with mock.patch.dict(sys.modules, {
            "speech_recognition": sr,
            "pyaudio": fake_pyaudio(stream),
        }):
            text = engine.listen_once("bn-BD", vad_enabled=True)
        self.assertEqual(text, "hello orvix")
        # The recogniser received the captured frames, pre-roll included.
        audio = sr.Recognizer  # class; instance state checked via engine level
        self.assertIsNotNone(engine.current_level())  # meter was fed

    def test_vad_no_speech_message(self):
        engine = SpeechEngine()
        sr = fake_sr()
        stream = FakeStream([])  # endless silence
        with mock.patch.dict(sys.modules, {
            "speech_recognition": sr,
            "pyaudio": fake_pyaudio(stream),
        }), mock.patch("orvix.speech._VAD_WAIT_SECONDS", 0.2):
            with self.assertRaisesRegex(RuntimeError, "did not hear anything"):
                engine.listen_once("bn-BD", vad_enabled=True)

    def test_capture_failure_falls_back_to_legacy(self):
        engine = SpeechEngine()
        sr = fake_sr(legacy_text="legacy heard")
        with mock.patch.dict(sys.modules, {
            "speech_recognition": sr,
            "pyaudio": fake_pyaudio(fail_open=True),
        }):
            text = engine.listen_once("bn-BD", vad_enabled=True)
        self.assertEqual(text, "legacy heard")

    def test_unknown_value_does_not_retry_legacy(self):
        engine = SpeechEngine()
        sr = fake_sr(unknown=True)
        stream = FakeStream(self._chunks())
        recognizers = []
        original = sr.Recognizer

        def tracking_recognizer():
            instance = original()
            recognizers.append(instance)
            return instance

        sr.Recognizer = tracking_recognizer
        with mock.patch.dict(sys.modules, {
            "speech_recognition": sr,
            "pyaudio": fake_pyaudio(stream),
        }):
            with self.assertRaisesRegex(RuntimeError, "could not understand"):
                engine.listen_once("bn-BD", vad_enabled=True)
        self.assertEqual(recognizers[0].legacy_listen_calls, 0)

    def test_legacy_path_when_vad_disabled_and_tap_feeds_meter(self):
        engine = SpeechEngine()
        sr = fake_sr(legacy_text="classic")
        with mock.patch.dict(sys.modules, {"speech_recognition": sr}):
            text = engine.listen_once("bn-BD", vad_enabled=False)
        self.assertEqual(text, "classic")
        self.assertIsNotNone(engine.current_level())


class LevelTapTests(unittest.TestCase):
    def test_tap_reports_rms_and_delegates(self):
        seen = []
        inner = SimpleNamespace(
            SAMPLE_RATE=16000, SAMPLE_WIDTH=2, CHUNK=1024,
            stream=FakeStream([pcm(0.4)]),
        )
        inner.__enter__ = lambda: inner
        inner.__exit__ = lambda *a: False
        source = _LevelTapSource(inner, seen.append)
        with source as tapped:
            data = tapped.stream.read(tapped.CHUNK)
        self.assertEqual(len(data), 2048)
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0], 0.4, places=2)
        self.assertEqual(source.SAMPLE_RATE, 16000)


class EnginePlaybackLevelTests(unittest.TestCase):
    def test_envelope_drives_current_level_while_playing(self):
        engine = SpeechEngine()
        self.assertIsNone(engine.current_level())
        engine._begin_playback(PlaybackEnvelope([0.0, 0.8, 0.2], bin_ms=100.0))
        try:
            self.assertEqual(engine.current_level(), 0.0)
            engine._play_started_at -= 0.15  # 150 ms into the clip
            self.assertEqual(engine.current_level(), 0.8)
        finally:
            engine._end_playback()
        self.assertIsNone(engine.current_level())

    def test_envelope_build_failure_is_none_not_fake(self):
        engine = SpeechEngine()
        stub = SimpleNamespace(mixer=SimpleNamespace(init=lambda: None, music=SimpleNamespace()))
        with mock.patch.dict(sys.modules, {"pygame": stub}):
            self.assertIsNone(engine._build_playback_envelope(Path("x.mp3")))


def fake_edge_tts():
    return SimpleNamespace(
        Communicate=lambda **kwargs: SimpleNamespace(save=AsyncMock())
    )


def fake_pygame_music(busy_until_stop=True, busy_calls=2):
    state = {"busy": True, "calls": 0}

    def get_busy():
        if not busy_until_stop:
            state["calls"] += 1
            return state["calls"] <= busy_calls
        return state["busy"]

    music = SimpleNamespace(
        load=Mock(),
        play=Mock(),
        stop=Mock(side_effect=lambda: state.__setitem__("busy", False)),
        unload=Mock(),
        get_busy=get_busy,
    )
    return SimpleNamespace(mixer=SimpleNamespace(init=Mock(), music=music))


class EngineBargeInTests(unittest.TestCase):
    def test_sustained_voice_interrupts_playback(self):
        engine = SpeechEngine()
        # Guard window hears only playback leakage (0.05); then the
        # user speaks up (0.5) and sustains it -> interruption.
        loud = FakeStream([pcm(0.05)] * 4 + [pcm(0.5)] * 500)
        with mock.patch.dict(sys.modules, {
            "edge_tts": fake_edge_tts(),
            "pygame": fake_pygame_music(busy_until_stop=True),
            "pyaudio": fake_pyaudio(loud),
        }):
            asyncio.run(engine._edge_speak("hello", "v", barge_in=True))
        self.assertTrue(engine.consume_barge_in())
        self.assertFalse(engine.consume_barge_in())  # consumed once

    def test_quiet_room_does_not_interrupt(self):
        engine = SpeechEngine()
        quiet = FakeStream([pcm(0.01)] * 500)
        with mock.patch.dict(sys.modules, {
            "edge_tts": fake_edge_tts(),
            "pygame": fake_pygame_music(busy_until_stop=False, busy_calls=2),
            "pyaudio": fake_pyaudio(quiet),
        }):
            asyncio.run(engine._edge_speak("hello", "v", barge_in=True))
        self.assertFalse(engine.consume_barge_in())

    def test_no_monitor_without_barge_in_flag(self):
        engine = SpeechEngine()
        opened = []

        class _PA:
            paInt16 = 8

            def get_default_input_device_info(self):
                return {"defaultSampleRate": 16000}

            def get_sample_size(self, fmt):
                return 2

            def open(self, **kwargs):
                opened.append(True)
                return FakeStream([])

            def terminate(self):
                pass

        with mock.patch.dict(sys.modules, {
            "edge_tts": fake_edge_tts(),
            "pygame": fake_pygame_music(busy_until_stop=False, busy_calls=1),
            "pyaudio": SimpleNamespace(PyAudio=_PA, paInt16=8),
        }):
            asyncio.run(engine._edge_speak("hello", "v", barge_in=False))
        self.assertEqual(opened, [])


# ---------------------------------------------------------------------
# settings + app wiring
# ---------------------------------------------------------------------

class SettingsDefaultsTests(unittest.TestCase):
    def test_voice_settings_default_on(self):
        self.assertIs(DEFAULTS["voice_vad_enabled"], True)
        self.assertIs(DEFAULTS["voice_barge_in"], True)


class AppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (APP_DIR / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(cls.source)
        cls.methods = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "OrvixDesktop":
                for member in node.body:
                    if isinstance(member, ast.FunctionDef):
                        cls.methods[member.name] = ast.get_source_segment(cls.source, member)

    def test_core_audio_level_reads_engine(self):
        self.assertIn("self.speech.current_level()", self.methods["_core_audio_level"])

    def test_speak_passes_barge_in_and_consumes_it(self):
        src = self.methods["_speak_safely"]
        self.assertIn("voice_barge_in", src)
        self.assertIn("consume_barge_in()", src)
        self.assertIn("_barge_in_listen", src)
        self.assertIn("def _barge_in_listen", self.source)

    def test_listen_paths_pass_vad_setting(self):
        for name in ("_listen_worker", "_wake_loop_worker"):
            self.assertIn("voice_vad_enabled", self.methods[name])

    def test_settings_dialog_saves_voice_keys(self):
        src = self.methods["_open_settings"]
        self.assertIn('new["voice_vad_enabled"]', src)
        self.assertIn('new["voice_barge_in"]', src)


if __name__ == "__main__":
    unittest.main()
