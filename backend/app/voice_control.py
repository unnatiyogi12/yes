"""AURA AI - Local Voice Input / Output

Speech-to-text:
    faster-whisper (runs locally)

Microphone:
    Explicitly uses the user's Bluetooth Airdopes 138 microphone when available.

Text-to-speech:
    pyttsx3 / Windows SAPI

This module is intentionally defensive: microphone, Whisper, and TTS
errors are converted into useful RuntimeError messages instead of
opaque failures.
"""

from __future__ import annotations

import tempfile
import wave
from pathlib import Path
from typing import Any

import numpy as np


# ============================================================
# AURA VOICE CONFIGURATION
# ============================================================

# User's Bluetooth microphone:
# "airpodes (2- Airdopes 138), MME (1 in, 0 out)"
PREFERRED_INPUT_DEVICE_NAME = "airpodes (2- Airdopes 138)"

# SoundDevice device index found on this Windows machine.
# We still search by name first so the code survives device-index changes.
PREFERRED_INPUT_DEVICE_INDEX = 1

SAMPLE_RATE = 16000
CHANNELS = 1

_WHISPER_MODEL = None


# ============================================================
# DEVICE HELPERS
# ============================================================

def _get_input_device() -> int:
    """Return the Airdopes 138 input device index."""

    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(
            "sounddevice is not installed. Run: "
            "python -m pip install sounddevice"
        ) from exc

    try:
        devices = sd.query_devices()
    except Exception as exc:
        raise RuntimeError(
            f"Could not read audio devices: {type(exc).__name__}: {exc}"
        ) from exc

    # 1. Prefer an exact/partial name match with an input channel.
    preferred = PREFERRED_INPUT_DEVICE_NAME.casefold()

    for index, device in enumerate(devices):
        name = str(device.get("name", ""))
        max_input = int(device.get("max_input_channels", 0) or 0)

        if max_input > 0 and preferred in name.casefold():
            return index

    # 2. Fallback to the known device index only if it is still
    #    a valid input device.
    try:
        device = devices[PREFERRED_INPUT_DEVICE_INDEX]
        max_input = int(device.get("max_input_channels", 0) or 0)

        if max_input > 0:
            return PREFERRED_INPUT_DEVICE_INDEX
    except (IndexError, TypeError, ValueError):
        pass

    # 3. Give a useful error instead of silently using another mic.
    available = []

    for index, device in enumerate(devices):
        max_input = int(device.get("max_input_channels", 0) or 0)

        if max_input > 0:
            available.append(
                f"{index}: {device.get('name', 'Unknown device')}"
            )

    available_text = "\n".join(available) or "No input microphones found."

    raise RuntimeError(
        "Airdopes 138 microphone was not found.\n\n"
        "Available input devices:\n"
        f"{available_text}\n\n"
        "Make sure your Airdopes 138 is connected and try again."
    )


def get_input_device_info() -> dict[str, Any]:
    """Return information about the microphone AURA will use."""

    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(
            "sounddevice is not installed. Run: "
            "python -m pip install sounddevice"
        ) from exc

    device_index = _get_input_device()
    device = sd.query_devices(device_index)

    return {
        "index": device_index,
        "name": device.get("name", "Unknown"),
        "max_input_channels": device.get("max_input_channels", 0),
        "sample_rate": SAMPLE_RATE,
    }


# ============================================================
# WHISPER
# ============================================================

def _get_whisper_model():
    global _WHISPER_MODEL

    if _WHISPER_MODEL is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run: "
                "python -m pip install faster-whisper"
            ) from exc

        _WHISPER_MODEL = WhisperModel(
            "base",
            device="cpu",
            compute_type="int8",
        )

    return _WHISPER_MODEL


# ============================================================
# RECORDING
# ============================================================

def _record_wav(seconds: int) -> Path:
    """Record audio from the explicitly selected Bluetooth microphone."""

    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(
            "sounddevice is not installed. Run: "
            "python -m pip install sounddevice"
        ) from exc

    seconds = max(2, min(int(seconds), 10))
    device_index = _get_input_device()

    try:
        device_info = sd.query_devices(device_index)
        device_name = device_info.get("name", "Unknown")

        print(
            f"[AURA Voice] Microphone: {device_name} "
            f"(device {device_index})"
        )

        # Explicitly specify the device instead of relying on Windows
        # default input.
        recording = sd.rec(
            int(seconds * SAMPLE_RATE),
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            device=device_index,
        )

        sd.wait()

    except Exception as exc:
        raise RuntimeError(
            "Could not record from the Airdopes 138 microphone. "
            f"Device index: {device_index}. "
            f"Error: {type(exc).__name__}: {exc}"
        ) from exc

    temp = tempfile.NamedTemporaryFile(
        suffix=".wav",
        delete=False,
    )
    temp.close()

    path = Path(temp.name)

    try:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(
                np.asarray(recording, dtype=np.int16).tobytes()
            )
    except Exception:
        path.unlink(missing_ok=True)
        raise

    return path


# ============================================================
# SPEECH TO TEXT
# ============================================================

def listen_and_transcribe(
    seconds: int = 5,
    language: str = "auto",
) -> dict[str, Any]:
    """Record from Airdopes 138 and transcribe locally with Whisper."""

    seconds = max(2, min(int(seconds), 10))

    wav_path = _record_wav(seconds)

    try:
        model = _get_whisper_model()

        language_text = str(language or "auto").casefold()
        language_arg = (
            None
            if language_text in {"auto", ""}
            else language_text
        )

        print("[AURA Voice] Transcribing...")

        segments, info = model.transcribe(
            str(wav_path),
            language=language_arg,
            vad_filter=True,
            beam_size=5,
        )

        text = " ".join(
            segment.text.strip()
            for segment in segments
            if segment.text.strip()
        ).strip()

        detected_language = getattr(
            info,
            "language",
            language_arg or "unknown",
        )

        print(f"[AURA Voice] Heard: {text!r}")
        print(f"[AURA Voice] Language: {detected_language}")

        return {
            "text": text,
            "language": detected_language,
            "device": get_input_device_info(),
        }

    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# TEXT TO SPEECH
# ============================================================

def speak_text(text: str) -> None:
    """Speak through Windows SAPI.

    TTS is output-side; it does not affect the selected Bluetooth
    microphone used for voice input.
    """

    try:
        import pyttsx3
    except ImportError as exc:
        raise RuntimeError(
            "pyttsx3 is not installed. Run: "
            "python -m pip install pyttsx3"
        ) from exc

    engine = None

    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", 175)
        engine.say(str(text))
        engine.runAndWait()
    except Exception as exc:
        raise RuntimeError(
            f"Text-to-speech failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass
