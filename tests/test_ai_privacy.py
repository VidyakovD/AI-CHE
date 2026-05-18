"""Wiring-тесты: убедиться что PrivacyGuard действительно обёрнут
вокруг generate_response() в server/ai.py.

Цель — не дать регрессировать 152-ФЗ защите: если кто-то случайно
уберёт обёртку или _privacy_mask_messages, эти тесты упадут.
"""
import server.ai as ai_module


def _patch_provider(monkeypatch, handler):
    """Подменить resolve_model + PROVIDERS так, чтобы generate_response()
    вызвал наш handler вместо реальной LLM."""
    monkeypatch.setattr(
        ai_module, "resolve_model",
        lambda m: {"provider": "fake", "real_model": "fake-1"}
    )
    monkeypatch.setattr(ai_module, "PROVIDERS", {"fake": handler})
    # Заглушаем логирование (оно лезет в БД)
    monkeypatch.setattr(ai_module, "_log_ai_request", lambda **kw: None)


def test_user_phone_masked_before_handler(monkeypatch):
    """Хендлер LLM НЕ должен видеть оригинальный телефон."""
    captured = {}

    def fake_handler(real, messages, *a, **kw):
        captured["messages"] = messages
        return {"type": "text", "content": "ок", "usage": {}}

    _patch_provider(monkeypatch, fake_handler)
    ai_module.generate_response(
        "fake",
        [{"role": "user", "content": "позвони +7 999 1234567 клиенту"}],
    )
    sent = str(captured["messages"])
    assert "+7 999 1234567" not in sent, f"PII утекло в LLM: {sent}"
    assert "[[PHONE_" in sent, f"Маркер не подставлен: {sent}"


def test_email_in_response_unmasked(monkeypatch):
    """LLM может вернуть токен [[EMAIL_N]] — он должен превратиться обратно."""
    def fake_handler(real, messages, *a, **kw):
        # Имитируем LLM, которая аккуратно вернула токен
        sent = str(messages)
        # Найдём токен email
        import re
        m = re.search(r"\[\[EMAIL_\d+\]\]", sent)
        assert m, f"В messages нет токена email: {sent}"
        token = m.group(0)
        return {"type": "text", "content": f"Письмо на {token} отправлено.", "usage": {}}

    _patch_provider(monkeypatch, fake_handler)
    res = ai_module.generate_response(
        "fake",
        [{"role": "user", "content": "напиши на ivanov@mail.ru"}],
    )
    assert "ivanov@mail.ru" in res["content"], (
        f"Ответ не размаскирован: {res['content']}"
    )
    assert "[[EMAIL_" not in res["content"]


def test_inn_in_blocks_content_masked(monkeypatch):
    """multimodal content (list of blocks) — text-блоки тоже маскируются."""
    captured = {}

    def fake_handler(real, messages, *a, **kw):
        captured["messages"] = messages
        return {"type": "text", "content": "ок", "usage": {}}

    _patch_provider(monkeypatch, fake_handler)
    ai_module.generate_response(
        "fake",
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": "ИНН 7707083893 — проверь"},
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
            ],
        }],
    )
    sent = str(captured["messages"])
    assert "7707083893" not in sent, f"ИНН утёк: {sent}"
    assert "[[INN_" in sent
    # image_url block не должен быть тронут
    assert "https://x/y.png" in sent


def test_privacy_skip_disables_masking(monkeypatch):
    """extra={'_privacy_skip': True} — PII уходит сырьём (для финансового парсера)."""
    captured = {}

    def fake_handler(real, messages, *a, **kw):
        captured["messages"] = messages
        return {"type": "text", "content": "ок", "usage": {}}

    _patch_provider(monkeypatch, fake_handler)
    ai_module.generate_response(
        "fake",
        [{"role": "user", "content": "ИНН 7707083893"}],
        extra={"_privacy_skip": True},
    )
    sent = str(captured["messages"])
    assert "7707083893" in sent, f"Skip не сработал, замаскировало: {sent}"
    assert "[[INN_" not in sent


def test_image_provider_skipped(monkeypatch):
    """Провайдеры kling/veo маскировку не получают (там промпт для картинки)."""
    captured = {}

    def fake_handler(real, messages, *a, **kw):
        captured["messages"] = messages
        return {"type": "image", "content": "https://x/y.png", "usage": {}}

    monkeypatch.setattr(
        ai_module, "resolve_model",
        lambda m: {"provider": "kling", "real_model": "kling-1"}
    )
    monkeypatch.setattr(ai_module, "PROVIDERS", {"kling": fake_handler})
    monkeypatch.setattr(ai_module, "_log_ai_request", lambda **kw: None)

    ai_module.generate_response(
        "fake",
        [{"role": "user", "content": "портрет ivanov@mail.ru"}],
    )
    sent = str(captured["messages"])
    assert "ivanov@mail.ru" in sent, f"kling замаскировал email: {sent}"


def test_non_text_response_unaffected(monkeypatch):
    """Если LLM вернула не строку — unmask не должен ломать."""
    def fake_handler(real, messages, *a, **kw):
        # Например image-провайдер возвращает url, а не строку с PII
        return {"type": "image", "content": "https://server/file.png", "usage": {}}

    _patch_provider(monkeypatch, fake_handler)
    res = ai_module.generate_response(
        "fake",
        [{"role": "user", "content": "звонок +79991234567"}],
    )
    # Не упало, ответ остался
    assert res["content"] == "https://server/file.png"


def test_no_pii_no_changes_to_content(monkeypatch):
    """Если в сообщении нет PII — content идёт неизменным."""
    captured = {}

    def fake_handler(real, messages, *a, **kw):
        captured["messages"] = messages
        return {"type": "text", "content": "пять", "usage": {}}

    _patch_provider(monkeypatch, fake_handler)
    ai_module.generate_response(
        "fake",
        [{"role": "user", "content": "сколько будет два плюс три"}],
    )
    assert captured["messages"][0]["content"] == "сколько будет два плюс три"
