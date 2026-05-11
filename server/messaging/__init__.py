"""
Messaging-слой: отправка сообщений в каналы (TG/MAX/VK/WhatsApp/Avito) +
Whisper/TTS. Вынесено из server/chatbot_engine.py для уменьшения god-object'а.
"""
from server.messaging.senders import (
    HTTP,
    setup_telegram_webhook, delete_telegram_webhook,
    send_telegram, send_telegram_with_buttons, send_telegram_with_reply_keyboard,
    send_telegram_photo, edit_telegram_message, set_telegram_commands,
    send_telegram_chat_action, send_telegram_document, send_telegram_audio,
    setup_max_webhook, delete_max_webhook,
    send_max, send_max_with_reply_keyboard, send_max_photo, get_max_me,
    send_vk, send_whatsapp, send_avito,
)
from server.messaging.voice import _whisper_transcribe, _tts_generate

__all__ = [
    "HTTP",
    "setup_telegram_webhook", "delete_telegram_webhook",
    "send_telegram", "send_telegram_with_buttons", "send_telegram_with_reply_keyboard",
    "send_telegram_photo", "edit_telegram_message", "set_telegram_commands",
    "send_telegram_chat_action", "send_telegram_document", "send_telegram_audio",
    "setup_max_webhook", "delete_max_webhook",
    "send_max", "send_max_with_reply_keyboard", "send_max_photo", "get_max_me",
    "send_vk", "send_whatsapp", "send_avito",
    "_whisper_transcribe", "_tts_generate",
]
