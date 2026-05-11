"""
Voice helpers: OpenAI Whisper (STT) + TTS. Вынесено из chatbot_engine.py.
"""
import logging
import os
import uuid

from server.messaging.senders import HTTP

log = logging.getLogger("chatbot")


async def _whisper_transcribe(file_path: str) -> str:
    """Транскрибировать аудио через OpenAI Whisper."""
    from server.ai import _get_api_keys
    keys = _get_api_keys("openai")
    if not keys:
        return "[Whisper: нет OpenAI ключей]"
    # base — корень проекта (server/messaging → server → корень)
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    abs_path = os.path.join(base, file_path.lstrip("/")) if not os.path.isabs(file_path) else file_path
    if not os.path.exists(abs_path):
        return f"[Файл не найден: {file_path}]"
    try:
        with open(abs_path, "rb") as f:
            files = {"file": (os.path.basename(abs_path), f, "audio/mpeg")}
            data = {"model": "whisper-1"}
            r = await HTTP.post(
                "https://api.openai.com/v1/audio/transcriptions",
                files=files, data=data,
                headers={"Authorization": f"Bearer {keys[0]}"},
                timeout=120,
            )
        if r.status_code == 200:
            return r.json().get("text", "")
        return f"[Whisper error {r.status_code}: {r.text[:200]}]"
    except Exception as e:
        return f"[Whisper exception: {e}]"


async def _tts_generate(text: str, voice: str = "onyx") -> str:
    """Генерирует речь через OpenAI TTS, возвращает путь к файлу."""
    from server.ai import _get_api_keys
    keys = _get_api_keys("openai")
    if not keys:
        return ""
    try:
        r = await HTTP.post(
            "https://api.openai.com/v1/audio/speech",
            json={"model": "tts-1", "voice": voice, "input": text[:4000], "response_format": "mp3"},
            headers={"Authorization": f"Bearer {keys[0]}"},
            timeout=60,
        )
        if r.status_code != 200:
            log.error(f"[TTS] {r.status_code}: {r.text[:200]}")
            return ""
        # base — корень проекта
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        upload_dir = os.path.join(base, "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        fname = f"tts_{uuid.uuid4().hex[:12]}.mp3"
        path = os.path.join(upload_dir, fname)
        with open(path, "wb") as f:
            f.write(r.content)
        return f"/uploads/{fname}"
    except Exception as e:
        log.error(f"[TTS] {e}")
        return ""
