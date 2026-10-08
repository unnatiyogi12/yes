"""
AURA AI - Local Voice Control

Features:
- Local speech-to-text using faster-whisper
- Local text-to-speech using Windows SAPI / pyttsx3
- Microphone recording
- Whisper typo normalization for common AURA commands
- English + Hinglish command cleanup
"""

from __future__ import annotations

import difflib
import re
import threading
from typing import Optional

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
import pyttsx3


# ============================================================
# CONFIGURATION
# ============================================================

SAMPLE_RATE = 16000
CHANNELS = 1

# Whisper model.
# "base" gives a good balance between speed and accuracy.
MODEL_SIZE = "base"

# Preferred Bluetooth microphone.
PREFERRED_INPUT_DEVICE_NAME = "airpodes (2- Airdopes 138)"

# Fallback device index.
PREFERRED_INPUT_DEVICE_INDEX = 1

# Recording defaults.
DEFAULT_SECONDS = 5

# Silence settings.
SILENCE_THRESHOLD = 0.012
SILENCE_DURATION = 0.8

# Minimum audio required.
MIN_AUDIO_SECONDS = 0.5


# ============================================================
# GLOBALS
# ============================================================

_whisper_model: Optional[WhisperModel] = None
_whisper_lock = threading.Lock()

_tts_engine = None
_tts_lock = threading.Lock()


# ============================================================
# AURA VOCABULARY
# ============================================================

# Words that are very important for AURA commands.
# Whisper can sometimes produce slightly wrong spellings.

AURA_WORDS = [
    # Apps
    "calculator",
    "notepad",
    "chrome",
    "edge",
    "firefox",
    "camera",
    "paint",
    "explorer",
    "photos",
    "clock",
    "calendar",
    "mail",
    "settings",
    "task manager",
    "snipping tool",
    "media player",
    "visual studio code",
    "visual studio",
    "terminal",
    "powershell",
    "cmd",
    "postman",
    "github desktop",
    "word",
    "excel",
    "powerpoint",
    "outlook",
    "teams",
    "whatsapp",
    "spotify",
    "discord",
    "zoom",
    "telegram",
    "obs studio",

    # Websites
    "github",
    "gmail",
    "drive",
    "leetcode",
    "youtube",
    "linkedin",
    "chatgpt",
    "google",
    "stackoverflow",
    "instagram",

    # Commands
    "open",
    "launch",
    "start",
    "run",
    "visit",
    "go to",
    "kholo",
    "khol",
    "open karo",
    "chalao",
    "start karo",
    "shuru karo",

    # Actions
    "screenshot",
    "scroll",
    "scroll up",
    "scroll down",
    "type",
    "press",
    "click",
    "double click",
]


# ============================================================
# COMMON WHISPER CORRECTIONS
# ============================================================

# Exact known mistakes are handled first.
COMMON_CORRECTIONS = {
    # Calculator
    "canculator": "calculator",
    "calculater": "calculator",
    "calcultor": "calculator",
    "calculater": "calculator",
    "calculater": "calculator",
    "calculatorr": "calculator",

    # Chrome
    "crome": "chrome",
    "chrom": "chrome",
    "chorme": "chrome",

    # Notepad
    "notpad": "notepad",
    "note pad": "notepad",

    # WhatsApp
    "what's app": "whatsapp",
    "whats app": "whatsapp",
    "whatsapp": "whatsapp",

    # GitHub
    "git hub": "github",
    "githab": "github",
    "githubb": "github",

    # Gmail
    "g mail": "gmail",
    "g-mail": "gmail",

    # LinkedIn
    "linked in": "linkedin",
    "linkdin": "linkedin",
    "linkden": "linkedin",

    # LeetCode
    "leet code": "leetcode",
    "leatcode": "leetcode",
    "leat code": "leetcode",

    # YouTube
    "you tube": "youtube",
    "utube": "youtube",

    # Calculator-related
    "calc": "calculator",

    # VS Code
    "vs code": "visual studio code",
    "vs-code": "visual studio code",
}


# ============================================================
# NORMALIZATION
# ============================================================

def _clean_text(text: str) -> str:
    """Basic cleanup of Whisper output."""

    text = text.strip()

    # Remove repeated spaces.
    text = re.sub(r"\s+", " ", text)

    # Remove obvious trailing punctuation.
    text = text.strip(" .,!?;:")

    return text


def _exact_correction(text: str) -> str:
    """
    Apply safe known corrections.

    We replace complete words/phrases only.
    """

    result = text

    # Longest phrases first.
    for wrong, correct in sorted(
        COMMON_CORRECTIONS.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        pattern = rf"\b{re.escape(wrong)}\b"

        result = re.sub(
            pattern,
            correct,
            result,
            flags=re.IGNORECASE,
        )

    return result


def _fuzzy_correct_word(word: str) -> str:
    """
    Correct a single word only when it is very close
    to a known AURA word.

    We keep the threshold conservative so random words
    are not changed.
    """

    clean_word = word.casefold().strip()

    if not clean_word:
        return word

    # Don't touch very short words.
    if len(clean_word) <= 3:
        return word

    # Direct correction already handled.
    if clean_word in COMMON_CORRECTIONS:
        return COMMON_CORRECTIONS[clean_word]

    candidates = [
        item
        for item in AURA_WORDS
        if " " not in item
        and abs(len(item) - len(clean_word)) <= 3
    ]

    if not candidates:
        return word

    matches = difflib.get_close_matches(
        clean_word,
        candidates,
        n=1,
        cutoff=0.82,
    )

    if not matches:
        return word

    corrected = matches[0]

    # Don't modify if it is already identical.
    if corrected.casefold() == clean_word:
        return word

    return corrected


def normalize_voice_command(text: str) -> str:
    """
    Convert Whisper's slightly incorrect transcription
    into a cleaner AURA command.

    Example:

        Open Canculator
        ↓
        Open calculator
    """

    if not text:
        return ""

    text = _clean_text(text)

    # First use known corrections.
    text = _exact_correction(text)

    # Then conservative fuzzy correction.
    words = text.split()

    corrected_words = []

    for word in words:
        corrected_words.append(
            _fuzzy_correct_word(word)
        )

    text = " ".join(corrected_words)

    # Final cleanup.
    text = _clean_text(text)

    return text


# ============================================================
# MICROPHONE
# ============================================================

def _find_input_device():
    """
    Find the preferred microphone.

    Preference:
    1. Airdopes 138
    2. configured fallback index
    3. system default input
    """

    try:
        devices = sd.query_devices()

        # First search by name.
        preferred_lower = PREFERRED_INPUT_DEVICE_NAME.casefold()

        for index, device in enumerate(devices):
            name = str(device.get("name", "")).casefold()
            max_input = int(device.get("max_input_channels", 0))

            if max_input > 0 and preferred_lower in name:
                return index

        # Fallback index.
        if 0 <= PREFERRED_INPUT_DEVICE_INDEX < len(devices):
            device = devices[PREFERRED_INPUT_DEVICE_INDEX]

            if int(device.get("max_input_channels", 0)) > 0:
                return PREFERRED_INPUT_DEVICE_INDEX

    except Exception:
        pass

    # None = system default.
    return None


def get_microphone_info() -> str:
    """Return the microphone currently selected by AURA."""

    device_index = _find_input_device()

    try:
        devices = sd.query_devices()

        if device_index is None:
            default_device = sd.default.device

            if isinstance(default_device, (list, tuple)):
                device_index = default_device[0]

        if device_index is not None:
            return str(
                devices[int(device_index)]["name"]
            )

    except Exception:
        pass

    return "System default microphone"


# ============================================================
# RECORDING
# ============================================================

def _record_audio(seconds: int = DEFAULT_SECONDS) -> np.ndarray:
    """
    Record microphone audio.

    Uses the preferred input device.
    """

    device_index = _find_input_device()

    print(
        f"[AURA Voice] Microphone: "
        f"{get_microphone_info()}"
    )

    print("[AURA Voice] Listening...")

    try:
        audio = sd.rec(
            int(seconds * SAMPLE_RATE),
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            device=device_index,
        )

        sd.wait()

    except Exception as exc:
        raise RuntimeError(
            f"Microphone recording failed: {exc}"
        ) from exc

    return audio.reshape(-1)


# ============================================================
# WHISPER
# ============================================================

def _get_whisper_model() -> WhisperModel:
    """Load Whisper only once."""

    global _whisper_model

    if _whisper_model is not None:
        return _whisper_model

    with _whisper_lock:

        if _whisper_model is None:

            print(
                "[AURA Voice] Loading Whisper model..."
            )

            _whisper_model = WhisperModel(
                MODEL_SIZE,
                device="cpu",
                compute_type="int8",
            )

            print(
                "[AURA Voice] Whisper model ready."
            )

    return _whisper_model


def _transcribe_audio(audio: np.ndarray) -> tuple[str, str]:
    """Transcribe audio locally."""

    model = _get_whisper_model()

    print("[AURA Voice] Transcribing...")

    segments, info = model.transcribe(
        audio,
        language="en",
        beam_size=5,
        best_of=3,
        temperature=0.0,
        vad_filter=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
    )

    text_parts = []

    for segment in segments:
        text = segment.text.strip()

        if text:
            text_parts.append(text)

    raw_text = " ".join(text_parts).strip()

    language = getattr(
        info,
        "language",
        "en",
    ) or "en"

    return raw_text, language


# ============================================================
# MAIN LISTEN FUNCTION
# ============================================================

def listen_and_transcribe(
    seconds: int = DEFAULT_SECONDS,
    language: str = "en",
) -> dict:
    """
    Record -> Whisper -> normalize.

    Returns:

    {
        "text": "open calculator",
        "raw_text": "Open Canculator",
        "language": "en"
    }
    """

    audio = _record_audio(seconds)

    # Check if audio contains enough signal.
    if audio.size == 0:
        return {
            "text": "",
            "raw_text": "",
            "language": language,
        }

    duration = len(audio) / SAMPLE_RATE

    if duration < MIN_AUDIO_SECONDS:
        return {
            "text": "",
            "raw_text": "",
            "language": language,
        }

    # Check audio level.
    rms = float(
        np.sqrt(
            np.mean(
                np.square(audio)
            )
        )
    )

    print(
        f"[AURA Voice] Audio level: {rms:.4f}"
    )

    if rms < SILENCE_THRESHOLD:
        print(
            "[AURA Voice] No clear speech detected."
        )

        return {
            "text": "",
            "raw_text": "",
            "language": language,
        }

    raw_text, detected_language = _transcribe_audio(
        audio
    )

    if not raw_text:
        return {
            "text": "",
            "raw_text": "",
            "language": detected_language,
        }

    normalized_text = normalize_voice_command(
        raw_text
    )

    print(
        f"[AURA Voice] Heard: '{raw_text}'"
    )

    if normalized_text.casefold() != raw_text.casefold():
        print(
            f"[AURA Voice] Corrected: "
            f"'{normalized_text}'"
        )

    return {
        "text": normalized_text,
        "raw_text": raw_text,
        "language": detected_language,
    }


# ============================================================
# TEXT TO SPEECH
# ============================================================

def _get_tts_engine():
    global _tts_engine

    if _tts_engine is not None:
        return _tts_engine

    with _tts_lock:

        if _tts_engine is None:

            try:
                engine = pyttsx3.init()

                # Slightly natural speaking speed.
                engine.setProperty(
                    "rate",
                    175,
                )

                engine.setProperty(
                    "volume",
                    1.0,
                )

                _tts_engine = engine

            except Exception as exc:
                raise RuntimeError(
                    f"TTS initialization failed: {exc}"
                ) from exc

    return _tts_engine


def speak_text(text: str) -> None:
    """Speak text using Windows local TTS."""

    if not text:
        return

    try:
        engine = _get_tts_engine()

        with _tts_lock:
            engine.say(str(text))
            engine.runAndWait()

    except Exception as exc:
        print(
            f"[AURA Voice] TTS error: {exc}"
        )


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print("AURA VOICE TEST")
    print("=" * 60)

    print(
        "Microphone:",
        get_microphone_info(),
    )

    result = listen_and_transcribe()

    print()
    print("Raw:", result["raw_text"])
    print("Final:", result["text"])
    print()

    if result["text"]:
        speak_text(
            f"I heard {result['text']}"
        )