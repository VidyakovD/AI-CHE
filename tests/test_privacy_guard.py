"""
Тесты для server/privacy_guard.py — маскировка PII перед LLM.

Покрытие:
- Каждый PII-тип маскируется
- Round-trip: mask → unmask = original
- unmask_response справляется со сорванными скобками
- Luhn-validation: не каждые 16 цифр маскируются как карта
- Не-PII текст не трогается
- Multiple values одного типа → разные токены
- Контекст-pattern'ы (ИНН/КПП без контекста — не маскируются)
"""
import pytest

from server.privacy_guard import PrivacyGuard, with_pii_protection, _passes_luhn


class TestPrivacyGuardEmail:
    def test_mask_single_email(self):
        g = PrivacyGuard()
        out = g.mask("Свяжись с ivanov@mail.ru")
        assert "ivanov@mail.ru" not in out
        assert "[[EMAIL_1]]" in out

    def test_unmask_restores(self):
        g = PrivacyGuard()
        masked = g.mask("Email: ivanov@example.ru")
        unmasked = g.unmask(masked)
        assert unmasked == "Email: ivanov@example.ru"

    def test_multiple_emails_get_different_tokens(self):
        g = PrivacyGuard()
        out = g.mask("a@a.ru, b@b.ru, c@c.ru")
        assert "[[EMAIL_1]]" in out
        assert "[[EMAIL_2]]" in out
        assert "[[EMAIL_3]]" in out
        assert g.map_size() == 3


class TestPrivacyGuardPhones:
    def test_ru_phone_plus7(self):
        g = PrivacyGuard()
        out = g.mask("Звони +7 (999) 123-45-67")
        assert "+7 (999) 123-45-67" not in out
        assert "[[PHONE_1]]" in out

    def test_ru_phone_8format(self):
        g = PrivacyGuard()
        out = g.mask("Контакт: 8 999 1234567")
        assert "8 999 1234567" not in out
        assert "[[PHONE_1]]" in out

    def test_intl_phone(self):
        g = PrivacyGuard()
        out = g.mask("UK: +44 20 7946 0958")
        assert "+44" not in out
        assert "[[PHONE_1]]" in out


class TestPrivacyGuardRussianTaxIds:
    def test_inn_with_context_word(self):
        g = PrivacyGuard()
        out = g.mask("ИНН 7707083893 (Сбербанк)")
        assert "7707083893" not in out
        assert "[[INN_1]]" in out
        # Контекст-слово сохраняется
        assert "ИНН" in out

    def test_inn_individual_12_digits(self):
        g = PrivacyGuard()
        out = g.mask("Мой ИНН: 770708389355")
        assert "770708389355" not in out
        assert "[[INN_1]]" in out

    def test_inn_without_context_NOT_masked(self):
        """Голые 10/12 цифр без слова «ИНН» не должны маскироваться —
        иначе любая дата/код будет ломаться."""
        g = PrivacyGuard()
        out = g.mask("Идентификатор записи: 7707083893")
        # Без слова «ИНН» — оставляем как есть (10 цифр — может быть что угодно)
        assert "7707083893" in out

    def test_kpp_with_context(self):
        g = PrivacyGuard()
        out = g.mask("КПП 773601001")
        assert "773601001" not in out
        assert "[[KPP_1]]" in out

    def test_ogrn_with_context(self):
        g = PrivacyGuard()
        out = g.mask("ОГРН 1027700132195")
        assert "1027700132195" not in out
        assert "[[OGRN_1]]" in out

    def test_snils_format(self):
        g = PrivacyGuard()
        out = g.mask("СНИЛС: 123-456-789 12")
        assert "123-456-789 12" not in out
        assert "[[SNILS_1]]" in out


class TestPrivacyGuardCreditCards:
    def test_valid_luhn_masked(self):
        g = PrivacyGuard()
        # 4111 1111 1111 1111 — стандартный test-VISA, проходит Luhn
        out = g.mask("Карта: 4111 1111 1111 1111")
        assert "4111 1111 1111 1111" not in out
        assert "[[CC_1]]" in out

    def test_invalid_luhn_NOT_masked(self):
        g = PrivacyGuard()
        # 1234 1234 1234 1234 — НЕ проходит Luhn
        out = g.mask("Какой-то ID: 1234 1234 1234 1234")
        # Должно остаться как есть
        assert "1234 1234 1234 1234" in out

    def test_luhn_algorithm_correct(self):
        # Известные test-карты
        assert _passes_luhn("4111111111111111") is True       # VISA
        assert _passes_luhn("5500000000000004") is True       # MasterCard
        assert _passes_luhn("340000000000009") is True        # AmEx (15)
        assert _passes_luhn("1234567812345678") is False
        assert _passes_luhn("0000000000000000") is True        # technically passes Luhn


class TestPrivacyGuardBankingRu:
    def test_iban(self):
        g = PrivacyGuard()
        out = g.mask("DE89370400440532013000")
        assert "DE89370400440532013000" not in out
        assert "[[IBAN_1]]" in out

    def test_bank_account_with_context(self):
        g = PrivacyGuard()
        out = g.mask("р/с 40702810400000123456")
        assert "40702810400000123456" not in out
        assert "[[BANKACCT_1]]" in out


class TestPrivacyGuardUnmaskResponse:
    def test_unmask_when_ai_keeps_brackets(self):
        g = PrivacyGuard()
        masked = g.mask("Привет ivanov@mail.ru")
        # Симулируем что AI вернул токены как есть
        ai_resp = f"Я ответил клиенту {masked.split()[1]}"
        unmasked = g.unmask_response(ai_resp)
        assert "ivanov@mail.ru" in unmasked

    def test_unmask_when_ai_strips_brackets(self):
        """LLM в JSON-mode иногда срезает [[ ]] — fallback должен сработать."""
        g = PrivacyGuard()
        g.mask("email: test@test.ru")
        # AI вернул токен без скобок
        ai_resp = "Письмо отправлено на EMAIL_1"
        unmasked = g.unmask_response(ai_resp)
        assert "test@test.ru" in unmasked


class TestPrivacyGuardRoundTrip:
    def test_real_world_proposal_input(self):
        """Реалистичный сценарий — формирование КП с реальными ПД."""
        original = """
Клиент: ООО «Ромашка»
ИНН 7707083893
КПП 773601001
ОГРН 1027700132195
Контакт: Иван Иванов, ivanov@romashka.ru
Телефон: +7 (999) 123-45-67
р/с 40702810400000123456
Сумма: 150000 ₽
"""
        g = PrivacyGuard()
        masked = g.mask(original)

        # PII должны исчезнуть
        for pii in ["7707083893", "773601001", "1027700132195",
                    "ivanov@romashka.ru", "123-45-67", "40702810400000123456"]:
            assert pii not in masked, f"{pii} НЕ замаскирован!"

        # Не-PII текст должен остаться
        assert "ООО «Ромашка»" in masked
        assert "150000 ₽" in masked
        assert "Иван Иванов" in masked  # имя не маскируем — нет надёжного pattern'а

        # Round-trip
        unmasked = g.unmask(masked)
        assert unmasked == original

    def test_no_pii_text_untouched(self):
        g = PrivacyGuard()
        original = "Просто текст без персональных данных. 2026 год, цена 1500 ₽."
        masked = g.mask(original)
        assert masked == original
        assert g.map_size() == 0


class TestWithPiiProtectionHelper:
    def test_helper_returns_tuple(self):
        safe, guard = with_pii_protection("email me at test@test.ru")
        assert "test@test.ru" not in safe
        assert isinstance(guard, PrivacyGuard)
        # Guard готов к unmask
        restored = guard.unmask_response(safe)
        assert "test@test.ru" in restored
