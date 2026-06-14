"""
transcribe.py — транскрибация аудио: Groq → Finland VPS → локальный faster-whisper.

Та же трёхуровневая схема что в notion-pm, audio-transcriber и claude-bot.
"""

import logging
import os
import tempfile

import requests

_GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
_FINLAND_URL = os.environ.get("FINLAND_WHISPER_URL", "http://2.26.85.234:5000/transcribe")

_whisper_model = None
logger = logging.getLogger("transcribe")


def _get_local_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
    return _whisper_model


def transcribe_sync(audio_bytes: bytes, suffix: str = ".ogg") -> str:
    """Groq → Finland VPS → локальный faster-whisper."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    try:
        if _GROQ_API_KEY:
            try:
                with open(tmp_path, "rb") as af:
                    resp = requests.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {_GROQ_API_KEY}"},
                        files={"file": (f"audio{suffix}", af, "audio/ogg")},
                        data={"model": "whisper-large-v3", "language": "ru", "response_format": "text"},
                        timeout=60,
                    )
                if resp.status_code != 429:
                    resp.raise_for_status()
                    return resp.text.strip()
                logger.warning("Groq rate limit → пробуем Finland")
            except Exception as e:
                logger.warning(f"Groq не сработал: {e} → пробуем Finland")

        try:
            with open(tmp_path, "rb") as af:
                resp = requests.post(
                    _FINLAND_URL,
                    files={"file": (f"audio{suffix}", af, "audio/ogg")},
                    params={"language": "ru"},
                    timeout=120,
                )
            resp.raise_for_status()
            return resp.json()["text"]
        except Exception as e:
            logger.warning(f"Finland не сработал: {e} → локальный whisper")

        model = _get_local_whisper()
        segments, _ = model.transcribe(tmp_path, language="ru", vad_filter=True)
        return " ".join(s.text for s in segments).strip()

    finally:
        os.unlink(tmp_path)
