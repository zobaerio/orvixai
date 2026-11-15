"""Speech engine: recognition + synthesis, with live audio levels,
energy VAD segmentation and barge-in for the neural (edge-tts) path.

Listening
---------
Two capture paths feed the *same* recogniser (Google via
``speech_recognition``):

- **VAD path (default)** — a raw PyAudio stream the app controls.
  Every frame's RMS updates the level meter (Part 1: the floating
  core pulses with the user's real voice) and feeds
  :class:`orvix.vad.VadSegmenter`; when an utterance is complete its
  bytes — the exact frames captured, pre-roll included — are handed
  to the recogniser as ``sr.AudioData``. Recording starts/stops on
  speech boundaries instead of a fixed timeout.
- **Legacy path** — ``sr.Microphone`` + ``recognizer.listen`` exactly
  as before, with the microphone stream wrapped in a read tap so the
  level meter still sees the recogniser's own frames (no second mic
  stream is ever opened). Used when VAD is disabled in Settings, and
  as the automatic fallback if the VAD capture fails before producing
  an utterance (missing/busy device, stream error, ...).

Speaking
--------
edge-tts synthesises an MP3 which pygame plays. Before playback the
MP3 is decoded once (``pygame.sndarray``, when available) into a
:class:`~orvix.audio_levels.PlaybackEnvelope`, so the SPEAKING level
is the real loudness of the reply at each moment. When decoding is
unavailable the level stays ``None`` and the core keeps its
state-driven pulse — a level is never invented.

Barge-in (neural path only): while playback runs, a monitor thread
watches the microphone through :class:`orvix.barge_in.BargeInController`
(guard window, leakage-adaptive threshold, sustained-energy trigger)
and stops pygame playback when the user interrupts; the app then
listens. The pyttsx3 fallback is a blocking call with no stoppable
playback handle and no audio file, so neither barge-in nor playback
levels exist there — documented in FLOATING_CORE.md.

Fail-safe rule for everything new here: any exception in the level,
VAD or barge-in code degrades to the pre-existing behaviour and never
breaks the voice loop.
"""
from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from pathlib import Path

from .audio_levels import LevelMeter, PlaybackEnvelope, level_from_rms, pcm_rms
from .barge_in import BargeInController
from .vad import VadSegmenter

_CAPTURE_CHUNK = 1024
# Mirrors the legacy budget: ~0.45 s ambient calibration + 5 s listen
# timeout to start speaking; the segmenter caps an utterance at 12 s,
# mirroring the legacy phrase_time_limit.
_VAD_WAIT_SECONDS = 5.5


def _playback_timeout_seconds(text_length: int) -> float:
    """Bound speech playback time while allowing longer responses extra time."""
    return max(15.0, min(180.0, 10.0 + max(0, int(text_length)) / 20.0))


class _VadUnavailable(Exception):
    """The VAD capture path cannot run; the legacy path should be used."""


def _default_input_rate(pa) -> int:
    """Sample rate of the default input device (44100 fallback)."""
    try:
        info = pa.get_default_input_device_info()
        rate = int(float(info.get("defaultSampleRate") or 0))
    except Exception:
        rate = 0
    return rate if rate > 0 else 44100


class _LevelTapStream:
    """Delegating stream wrapper that reports each read's RMS."""

    def __init__(self, inner, sink) -> None:
        self._inner = inner
        self._sink = sink

    def read(self, size, *args, **kwargs):
        data = self._inner.read(size, *args, **kwargs)
        if data:
            try:
                self._sink(pcm_rms(data, 2))
            except Exception:
                pass  # metering must never break capture
        return data

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _LevelTapSource:
    """``speech_recognition`` AudioSource wrapper adding an RMS tap.

    Duck-typed delegation: SAMPLE_RATE / SAMPLE_WIDTH / CHUNK and any
    other attribute come from the wrapped source; only ``stream`` is
    intercepted so the recogniser's own frames feed the level meter.
    """

    def __init__(self, inner, sink) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_sink", sink)
        object.__setattr__(self, "_tap", None)

    def __enter__(self):
        self._inner.__enter__()
        stream = getattr(self._inner, "stream", None)
        if stream is not None:
            object.__setattr__(self, "_tap", _LevelTapStream(stream, self._sink))
        return self

    def __exit__(self, *exc_info):
        object.__setattr__(self, "_tap", None)
        return self._inner.__exit__(*exc_info)

    @property
    def stream(self):
        return self._tap if self._tap is not None else self._inner.stream

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)


class SpeechEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pygame_ready = False
        self._mic_meter = LevelMeter()
        self._playing = False
        self._play_envelope: PlaybackEnvelope | None = None
        self._play_started_at = 0.0
        self._stop_event = threading.Event()
        self._barge_in_event = threading.Event()

    # -- live levels -------------------------------------------------

    def _note_mic_rms(self, rms: float) -> None:
        self._mic_meter.update(level_from_rms(rms))

    def current_level(self) -> float | None:
        """Latest real audio level (0..1), or None when nothing is live.

        Playback wins while speaking (the envelope is the actual reply
        audio); otherwise the freshest microphone level. Stale sources
        report None so the core falls back to its state-driven pulse.
        """
        if self._playing and self._play_envelope is not None:
            elapsed_ms = (time.monotonic() - self._play_started_at) * 1000.0
            return self._play_envelope.level_at(elapsed_ms)
        return self._mic_meter.value()

    # -- barge-in / stop control --------------------------------------

    def consume_barge_in(self) -> bool:
        """True (once) if the last playback was interrupted by the user."""
        if self._barge_in_event.is_set():
            self._barge_in_event.clear()
            return True
        return False

    def stop_speaking(self) -> None:
        """Request that any in-progress neural playback stop."""
        self._stop_event.set()
        try:
            import pygame

            pygame.mixer.music.stop()
        except Exception:
            pass

    # -- listening -----------------------------------------------------

    def listen_once(self, language: str = "bn-BD", *, vad_enabled: bool = True) -> str:
        try:
            import speech_recognition as sr
        except ImportError as exc:
            raise RuntimeError("SpeechRecognition is not installed. Run install.bat again.") from exc
        recognizer = sr.Recognizer()
        recognizer.pause_threshold = 0.8
        # Bound network-backed recognition so a stalled provider cannot block the UI worker forever.
        recognizer.operation_timeout = 15
        if vad_enabled:
            try:
                return self._listen_vad(recognizer, language)
            except _VadUnavailable:
                pass  # automatic fallback to the classic listener
        return self._listen_legacy(recognizer, language)

    def _listen_legacy(self, recognizer, language: str) -> str:
        import speech_recognition as sr

        try:
            source = sr.Microphone()
            try:
                source = _LevelTapSource(source, self._note_mic_rms)
            except Exception:
                pass  # an untapped microphone is still a working microphone
            with source as tapped:
                recognizer.adjust_for_ambient_noise(tapped, duration=0.45)
                audio = recognizer.listen(tapped, timeout=5, phrase_time_limit=12)
            return recognizer.recognize_google(audio, language=language)
        except sr.WaitTimeoutError as exc:
            raise RuntimeError("I did not hear anything. Try speaking closer to the microphone.") from exc
        except sr.UnknownValueError as exc:
            raise RuntimeError("I could not understand that speech. Please try again.") from exc
        except sr.RequestError as exc:
            raise RuntimeError("Speech recognition needs an internet connection. Check your connection and retry.") from exc
        except OSError as exc:
            raise RuntimeError("No usable microphone found. Check Windows microphone permissions and input device.") from exc

    def _listen_vad(self, recognizer, language: str) -> str:
        import speech_recognition as sr

        try:
            import pyaudio
        except ImportError as exc:
            raise _VadUnavailable("PyAudio is not installed") from exc
        pa = None
        stream = None
        try:
            try:
                pa = pyaudio.PyAudio()
                rate = _default_input_rate(pa)
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=rate,
                    input=True,
                    frames_per_buffer=_CAPTURE_CHUNK,
                )
            except Exception as exc:
                raise _VadUnavailable(f"microphone stream unavailable: {exc}") from exc
            segmenter = VadSegmenter()
            frame_ms = _CAPTURE_CHUNK * 1000.0 / rate
            wait_deadline = time.monotonic() + _VAD_WAIT_SECONDS
            utterance = None
            while utterance is None:
                if not segmenter.in_speech and time.monotonic() >= wait_deadline:
                    break
                try:
                    data = stream.read(_CAPTURE_CHUNK, exception_on_overflow=False)
                except Exception as exc:
                    raise _VadUnavailable(f"microphone read failed: {exc}") from exc
                if not data:
                    continue
                rms = pcm_rms(data, 2)
                self._note_mic_rms(rms)
                utterance = segmenter.feed(data, rms, frame_ms)
            if utterance is None:
                raise RuntimeError("I did not hear anything. Try speaking closer to the microphone.")
            audio = sr.AudioData(utterance, rate, pa.get_sample_size(pyaudio.paInt16))
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:
                    pass
        try:
            return recognizer.recognize_google(audio, language=language)
        except sr.UnknownValueError as exc:
            raise RuntimeError("I could not understand that speech. Please try again.") from exc
        except sr.RequestError as exc:
            raise RuntimeError("Speech recognition needs an internet connection. Check your connection and retry.") from exc

    # -- speaking ------------------------------------------------------

    def speak(self, text: str, voice: str = "bn-BD-PradeepNeural", *, barge_in: bool = False) -> None:
        if not text.strip():
            return
        with self._lock:
            try:
                asyncio.run(self._edge_speak(text[:3000], voice, barge_in=barge_in))
                return
            except Exception:
                pass
            self._speak_fallback(text[:1000])

    async def _edge_speak(self, text: str, voice: str, *, barge_in: bool = False) -> None:
        import edge_tts
        import pygame
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            out = Path(f.name)
        loaded = False
        monitor_stop: threading.Event | None = None
        monitor_thread: threading.Thread | None = None
        try:
            communicate = edge_tts.Communicate(text=text, voice=voice, rate="+0%", volume="+0%")
            await asyncio.wait_for(communicate.save(str(out)), timeout=45.0)
            if not self._pygame_ready:
                pygame.mixer.init()
                self._pygame_ready = True
            envelope = self._build_playback_envelope(out)
            pygame.mixer.music.load(str(out))
            loaded = True
            self._begin_playback(envelope)
            pygame.mixer.music.play()
            if barge_in:
                monitor_stop = threading.Event()
                monitor_thread = threading.Thread(
                    target=self._run_barge_in_monitor,
                    args=(monitor_stop,),
                    daemon=True,
                )
                monitor_thread.start()
            deadline = time.monotonic() + _playback_timeout_seconds(len(text))
            while pygame.mixer.music.get_busy():
                if self._barge_in_event.is_set() or self._stop_event.is_set():
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Speech playback exceeded its safe time limit.")
                await asyncio.sleep(0.12)
        finally:
            if monitor_stop is not None:
                monitor_stop.set()
            if monitor_thread is not None:
                monitor_thread.join(timeout=1.0)
            self._end_playback()
            if loaded:
                try:
                    pygame.mixer.music.stop()
                except Exception:
                    pass
                try:
                    pygame.mixer.music.unload()
                except Exception:
                    pass
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass

    def _begin_playback(self, envelope: PlaybackEnvelope | None) -> None:
        self._stop_event.clear()
        self._barge_in_event.clear()
        self._play_envelope = envelope
        self._play_started_at = time.monotonic()
        self._playing = True

    def _end_playback(self) -> None:
        self._playing = False
        self._play_envelope = None
        self._play_started_at = 0.0

    @staticmethod
    def _build_playback_envelope(path: Path, bin_ms: float = 100.0) -> PlaybackEnvelope | None:
        """Decode the synthesised file into per-bin real levels.

        Uses pygame's own decoder plus ``pygame.sndarray`` (numpy).
        Any gap — codec, numpy, stub mixers in tests — yields None,
        which the core treats as "no live level", never a fake one.
        """
        try:
            import pygame
            import pygame.sndarray as sndarray

            sound = pygame.mixer.Sound(str(path))
            samples = sndarray.array(sound)
            if samples is None or len(samples) == 0:
                return None
            init = pygame.mixer.get_init()
            rate = int(init[0]) if init else 44100
            per_bin = max(1, int(rate * bin_ms / 1000.0))
            frames = len(samples)
            levels: list[float] = []
            for start in range(0, frames, per_bin):
                chunk = samples[start:start + per_bin].astype("float64")
                if chunk.size == 0:
                    continue
                rms = float((chunk * chunk).mean() ** 0.5) / 32768.0
                levels.append(level_from_rms(rms))
            return PlaybackEnvelope(levels, bin_ms=bin_ms) if levels else None
        except Exception:
            return None

    def _run_barge_in_monitor(self, stop_event: threading.Event) -> None:
        """Watch the mic during playback; stop playback on interruption.

        Runs on its own thread and owns its own PyAudio stream (the
        recogniser is idle while speaking, so this is the only mic
        consumer). Any failure disables barge-in for this reply only —
        playback itself is never affected.
        """
        try:
            import pyaudio

            pa = pyaudio.PyAudio()
            stream = None
            try:
                rate = _default_input_rate(pa)
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=rate,
                    input=True,
                    frames_per_buffer=_CAPTURE_CHUNK,
                )
                controller = BargeInController()
                controller.start()
                frame_ms = _CAPTURE_CHUNK * 1000.0 / rate
                while not stop_event.is_set():
                    data = stream.read(_CAPTURE_CHUNK, exception_on_overflow=False)
                    if not data:
                        continue
                    if controller.feed(pcm_rms(data, 2), frame_ms):
                        self._barge_in_event.set()
                        try:
                            import pygame

                            pygame.mixer.music.stop()
                        except Exception:
                            pass
                        return
            finally:
                if stream is not None:
                    try:
                        stream.stop_stream()
                    except Exception:
                        pass
                    try:
                        stream.close()
                    except Exception:
                        pass
                try:
                    pa.terminate()
                except Exception:
                    pass
        except Exception:
            return

    @staticmethod
    def _speak_fallback(text: str) -> None:
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception:
            return
