"""
PrivacyGuard — маскировка PII перед отправкой в LLM + unmask после.

Зачем: 152-ФЗ ст. 6 (передача персональных данных третьим лицам без согласия
запрещена). OpenAI/Anthropic/Google — иностранные операторы. Если юзер
отправляет AI промпт «составь КП клиенту Иванов И.И., email ivanov@mail.ru,
телефон +7 999 1234567, ИНН 7707083893» — мы передаём ПД зарубежному
оператору без согласия Иванова → штраф РКН.

Решение (паттерн от Dolibarr GDPR-modула, адаптировано под РФ):
  1. Перед LLM: текст пропускаем через .mask() — emails/phones/cards/IBAN/
     ИНН/СНИЛС/банковские реквизиты заменяются на токены [[EMAIL_1]],
     [[PHONE_2]], [[INN_3]] и т.д. Мапа token → original хранится в инстансе.
  2. LLM работает с анонимизированным текстом, не видит реальных ПД.
  3. После ответа: .unmask_response() заменяет токены обратно на оригинал.

Использование:
    guard = PrivacyGuard()
    safe_prompt = guard.mask(user_input)
    ai_response = generate_response("claude", [{"role": "user", "content": safe_prompt}])
    final = guard.unmask_response(ai_response["content"])

Каждый экземпляр PrivacyGuard — для одного запроса (одноразовый,
не thread-safe из-за внутренней мапы). Не нужен — не используем.

Тесты: tests/test_privacy_guard.py
"""
from __future__ import annotations

import re
from typing import Pattern


class PrivacyGuard:
    """Mask/unmask PII в тексте перед отправкой в LLM.

    Не thread-safe — каждый экземпляр обслуживает один запрос.
    """

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        self._index = 0

    # ── Public API ─────────────────────────────────────────────────────────

    def mask(self, text: str) -> str:
        """Заменить PII в тексте на токены. Порядок паттернов важен —
        длинные/специфичные паттерны идут первыми (СНИЛС с пробелами →
        потом обычные phone, etc.)."""
        if not text:
            return text
        self._map.clear()
        self._index = 0

        # 1. СНИЛС: 11 цифр в формате XXX-XXX-XXX XX (специфичный РФ)
        text = self._sub(_RE_SNILS, text, "SNILS")

        # 2. ИНН (10 или 12 цифр) — но с контекст-словом, иначе ложноположительные
        text = self._sub(_RE_INN_CTX, text, "INN", group=1)

        # 3. КПП (9 цифр) с контекстом
        text = self._sub(_RE_KPP_CTX, text, "KPP", group=1)

        # 4. ОГРН (13 или 15 цифр) с контекстом
        text = self._sub(_RE_OGRN_CTX, text, "OGRN", group=1)

        # 5. Кредитные карты с Luhn-валидацией (иначе любые 16 цифр маскируются)
        text = _RE_CC.sub(self._mask_cc_callback, text)

        # 6. IBAN
        text = self._sub(_RE_IBAN, text, "IBAN")

        # 7. SWIFT / BIC
        text = self._sub(_RE_SWIFT, text, "SWIFT")

        # 8. Banking accounts (РФ): 20 цифр с ключевым словом
        text = self._sub(_RE_BANK_ACCT_RU_CTX, text, "BANKACCT", group=1)

        # 9. Российские телефоны (+7 / 8 / 7 формат)
        text = self._sub(_RE_PHONE_RU, text, "PHONE")

        # 10. Международные phone (+ префикс)
        text = self._sub(_RE_PHONE_INTL, text, "PHONE")

        # 11. Emails
        text = self._sub(_RE_EMAIL, text, "EMAIL")

        # 12. Адреса серверов / IP (опц): домены НЕ маскируем (это не PII),
        # но IPv4 в персональном контексте — маскируем
        # text = self._sub(_RE_IPV4, text, "IP")  # отключено, чаще false-positive

        return text

    def unmask(self, text: str) -> str:
        """Замена токенов обратно на оригинал (точное совпадение)."""
        if not self._map or not text:
            return text
        for token, original in self._map.items():
            text = text.replace(token, original)
        return text

    def unmask_response(self, text: str) -> str:
        """Расширенный unmask: некоторые LLM срезают [[ и ]] вокруг токенов
        (например при JSON-mode). Сначала пытаемся точное совпадение,
        потом — без скобок (EMAIL_1 → orig)."""
        if not self._map or not text:
            return text
        text = self.unmask(text)
        # Fallback: AI мог удалить [[ ]]
        for token, original in self._map.items():
            stripped = token[2:-2]  # [[EMAIL_1]] → EMAIL_1
            if stripped in text:
                text = text.replace(stripped, original)
        return text

    def map_size(self) -> int:
        """Сколько токенов сейчас в мапе (для метрик/тестов)."""
        return len(self._map)

    # ── Internals ──────────────────────────────────────────────────────────

    def _create_token(self, value: str, type_: str) -> str:
        """Создать [[TYPE_N]], сохранить original в map."""
        self._index += 1
        token = f"[[{type_}_{self._index}]]"
        self._map[token] = value
        return token

    def _sub(self, regex: Pattern[str], text: str, type_: str, *, group: int = 0) -> str:
        """Заменить все совпадения regex'а на токены. group=N — если
        нужно сохранить не весь match а конкретную группу (для контекст-regex'ов)."""
        def repl(m: re.Match) -> str:
            value = m.group(group) if group else m.group(0)
            token = self._create_token(value, type_)
            if group:
                # При context-pattern (контекст-слово + значение) сохраняем
                # контекст-часть, заменяем только значение
                return m.group(0).replace(value, token)
            return token
        return regex.sub(repl, text)

    def _mask_cc_callback(self, m: re.Match) -> str:
        """Callback для CC: validate Luhn перед маскировкой."""
        cc = m.group(0)
        if _passes_luhn(cc):
            return self._create_token(cc, "CC")
        return cc  # не карта — не маскируем


# ── Compiled regex patterns (модульный scope = compile один раз) ──────────

# СНИЛС: 11 цифр, обычно XXX-XXX-XXX XX или XXXXXXXXXXX
_RE_SNILS = re.compile(r"\b\d{3}-\d{3}-\d{3}[\s\-]\d{2}\b")

# ИНН: 10 (юрлица) или 12 (физлица/ИП). С контекст-словом обязательно.
_RE_INN_CTX = re.compile(
    r"(?i)(?:ИНН|inn)[:\s№]*\b(\d{10}|\d{12})\b"
)

# КПП: 9 цифр с контекст-словом
_RE_KPP_CTX = re.compile(r"(?i)(?:КПП|kpp)[:\s№]*\b(\d{9})\b")

# ОГРН: 13 (организация) или 15 (ИП)
_RE_OGRN_CTX = re.compile(r"(?i)(?:ОГРН|ОГРНИП|ogrn)[:\s№]*\b(\d{13}|\d{15})\b")

# Кредитные карты: 13-19 цифр с возможными разделителями. Luhn-проверка в callback
_RE_CC = re.compile(r"\b(?:\d[ \-]*?){13,19}\b")

# IBAN: 2 letters + 2 digits + 4-30 alphanum
_RE_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b")

# SWIFT/BIC: 8 или 11 символов
_RE_SWIFT = re.compile(r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")

# Российский банковский счёт: 20 цифр с контекст-словом (счёт, р/с, расч)
_RE_BANK_ACCT_RU_CTX = re.compile(
    r"(?i)(?:р/с|р\.\s*с|расч[её]тный\s+сч[её]т|расчётный\s+счет|сч[её]т)[:\s№]*\b(\d{20})\b"
)

# Российский телефон: +7XXX..., 8XXX..., 7XXX..., с разделителями
# Покрывает: +7 (999) 123-45-67, 8 999 1234567, +79991234567
_RE_PHONE_RU = re.compile(
    r"(?:\+7|\b8|\b7)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}\b"
)

# Международные телефоны (+ префикс не-7)
_RE_PHONE_INTL = re.compile(r"\+[1-689]\d[\d\-\s().]{7,15}\b")

# Email
_RE_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _passes_luhn(number: str) -> bool:
    """Алгоритм Луна — проверка чек-суммы кредитной карты."""
    digits = re.sub(r"\D", "", number)
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    is_even = False  # начинаем с правого конца, первая цифра — НЕ удваивается
    for ch in reversed(digits):
        d = int(ch)
        if is_even:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        is_even = not is_even
    return total % 10 == 0


# ── Public helper: оборачивание generate_response ──────────────────────────

def with_pii_protection(text: str) -> tuple[str, PrivacyGuard]:
    """Удобный helper: маскирует text, возвращает (safe_text, guard).
    Используй guard.unmask_response(...) на ответе LLM.

    Пример:
        safe, guard = with_pii_protection(user_prompt)
        ans = generate_response("claude", [{"role": "user", "content": safe}])
        final = guard.unmask_response(ans["content"])
    """
    guard = PrivacyGuard()
    return guard.mask(text), guard
