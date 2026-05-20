"""Тесты finance_csv: парсинг банковских выписок + keyword-категоризация.

Покрывают:
  - _parse_decimal / _parse_date (форматы 'X,YZ' / '1 234,56 ₽' / разные даты)
  - detect_format по заголовкам Tinkoff/Sber/Alfa/generic
  - categorize: keyword-rules матчинг
  - parse_csv_statement: end-to-end с тестовыми CSV-snippet'ами
  - build_finance_summary: формирование context для LLM
  - _build_module_extra_context для slug='finance' end-to-end
"""
import io
import time

import pytest


class TestParseDecimal:
    def test_simple(self):
        from server.finance_csv import _parse_decimal
        from decimal import Decimal
        assert _parse_decimal("123.45") == Decimal("123.45")
        assert _parse_decimal("-50.00") == Decimal("-50.00")

    def test_russian_format(self):
        from server.finance_csv import _parse_decimal
        from decimal import Decimal
        assert _parse_decimal("1 234,56") == Decimal("1234.56")
        assert _parse_decimal("-1 000,00 ₽") == Decimal("-1000.00")

    def test_garbage(self):
        from server.finance_csv import _parse_decimal
        assert _parse_decimal("") is None
        assert _parse_decimal("abc") is None
        assert _parse_decimal("-") is None


class TestParseDate:
    def test_russian_dot_format(self):
        from server.finance_csv import _parse_date
        d = _parse_date("01.05.2026")
        assert d is not None
        assert d.year == 2026 and d.month == 5 and d.day == 1

    def test_iso_format(self):
        from server.finance_csv import _parse_date
        d = _parse_date("2026-05-20 14:30:00")
        assert d is not None
        assert d.hour == 14

    def test_garbage_returns_none(self):
        from server.finance_csv import _parse_date
        assert _parse_date("not a date") is None
        assert _parse_date("") is None


class TestDetectFormat:
    def test_tinkoff(self):
        from server.finance_csv import detect_format
        headers = ["Дата операции", "Дата платежа", "Номер карты",
                   "Статус", "Сумма операции", "Валюта операции",
                   "Сумма платежа", "Описание"]
        assert detect_format(headers) == "csv:tinkoff"

    def test_sber(self):
        from server.finance_csv import detect_format
        headers = ["Дата операции", "Сумма", "Operation type", "Сбер"]
        assert detect_format(headers) == "csv:sber"

    def test_alfa(self):
        from server.finance_csv import detect_format
        headers = ["Альфа-банк", "дата", "сумма", "описание"]
        assert detect_format(headers) == "csv:alfa"

    def test_generic(self):
        from server.finance_csv import detect_format
        assert detect_format(["Date", "Amount", "Memo"]) == "csv:generic"


class TestCategorize:
    def test_food(self):
        from server.finance_csv import categorize
        assert categorize("PYATEROCHKA #234 MOSCOW") == "food"
        assert categorize("Магнит-Косметик") == "food"
        assert categorize("ВкусВилл, ул. Тверская") == "food"

    def test_cafe(self):
        from server.finance_csv import categorize
        assert categorize("Coffee Bean") == "cafe"
        assert categorize("Шоколадница, кафе") == "cafe"
        assert categorize("Yandex Eda заказ") == "cafe"

    def test_transport_and_fuel(self):
        from server.finance_csv import categorize
        assert categorize("YANDEX TAXI") == "transport"
        assert categorize("Lukoil АЗС") == "fuel"

    def test_income_via_keyword(self):
        from server.finance_csv import categorize
        # «зарплата» — явное слово в описании → income, даже если amount > 0
        # (но это решает caller — categorize даёт только namespace)
        assert categorize("Зарплата за апрель") == "income"
        assert categorize("Возврат за товар") == "income"

    def test_subscriptions(self):
        from server.finance_csv import categorize
        assert categorize("Apple.com/Bill") == "subscript"
        assert categorize("Netflix") == "subscript"

    def test_utility(self):
        from server.finance_csv import categorize
        assert categorize("МТС, мобильная связь") == "utility"
        assert categorize("ЖКХ, единый счет") == "utility"

    def test_unknown_returns_other(self):
        from server.finance_csv import categorize
        assert categorize("Some unknown merchant XYZ") == "other"


class TestParseCsvStatement:
    def test_tinkoff_minimal(self):
        from server.finance_csv import parse_csv_statement
        csv_text = (
            "Дата операции;Сумма операции;Валюта операции;Описание\n"
            "01.05.2026 14:30:00;-150.50;RUB;PYATEROCHKA #123\n"
            "02.05.2026 18:00:00;-300.00;RUB;Yandex Taxi\n"
            "05.05.2026;75000.00;RUB;Зарплата за апрель\n"
        )
        result = parse_csv_statement(csv_text.encode("utf-8"))
        assert result["source"] == "csv:tinkoff"
        assert len(result["rows"]) == 3
        assert result["rows"][0]["amount_kop"] == -15050
        assert result["rows"][0]["category"] == "food"
        assert result["rows"][1]["category"] == "transport"
        assert result["rows"][2]["amount_kop"] == 7500000
        assert result["rows"][2]["category"] == "income"

    def test_generic_csv_with_comma(self):
        from server.finance_csv import parse_csv_statement
        csv_text = (
            "Date,Amount,Description\n"
            "2026-05-01,-99.50,Coffee Bean\n"
            "2026-05-02,500.00,Cashback Tinkoff\n"
        )
        result = parse_csv_statement(csv_text.encode("utf-8"))
        assert result["source"] == "csv:generic"
        assert len(result["rows"]) == 2
        assert result["rows"][0]["category"] == "cafe"
        # Cashback → income via keyword
        assert result["rows"][1]["category"] == "income"

    def test_cp1251_encoding(self):
        from server.finance_csv import parse_csv_statement
        csv_text = "Дата;Сумма;Описание\n01.05.2026;-100;Магнит\n"
        result = parse_csv_statement(csv_text.encode("cp1251"))
        assert len(result["rows"]) == 1
        assert "Магнит" in result["rows"][0]["description"]
        assert result["rows"][0]["category"] == "food"

    def test_no_date_column(self):
        from server.finance_csv import parse_csv_statement
        csv_text = "Колонка1;Колонка2;Текст\nA;B;C\n"
        result = parse_csv_statement(csv_text.encode("utf-8"))
        assert len(result["rows"]) == 0
        assert result["errors"]

    def test_empty_file(self):
        from server.finance_csv import parse_csv_statement
        result = parse_csv_statement(b"")
        assert result["rows"] == []
        assert result["errors"]

    def test_skips_broken_rows(self):
        from server.finance_csv import parse_csv_statement
        csv_text = (
            "Дата;Сумма;Описание\n"
            "01.05.2026;-100;Magnit\n"
            "битая_дата;-50;Coffee\n"
            "03.05.2026;abc;Описание\n"
            "04.05.2026;-200;Lukoil\n"
        )
        result = parse_csv_statement(csv_text.encode("utf-8"))
        # 2 валидных, 2 битых
        assert len(result["rows"]) == 2
        # В errors упоминание про skipped
        assert any("Пропущено" in e for e in result["errors"])


class TestBuildSummary:
    def test_empty(self):
        from server.finance_csv import build_finance_summary
        assert "пока нет" in build_finance_summary([]).lower()

    def test_with_data(self):
        from datetime import datetime
        from server.finance_csv import build_finance_summary
        txs = [
            {"date": datetime(2026, 5, 1), "amount_kop": -15050,
             "description": "PYATEROCHKA", "category": "food", "currency":"RUB"},
            {"date": datetime(2026, 5, 2), "amount_kop": -30000,
             "description": "Yandex Taxi", "category": "transport", "currency":"RUB"},
            {"date": datetime(2026, 5, 5), "amount_kop": 7500000,
             "description": "Зарплата", "category": "income", "currency":"RUB"},
        ]
        out = build_finance_summary(txs)
        assert "75000.00" in out  # доходы
        assert "450.50" in out    # расходы 15050 + 30000 = 45050 коп = 450.50 ₽
        assert "🍲" in out         # категория food
        assert "🚖" in out         # transport


# ── module_extra_context end-to-end ─────────────────────────────────────────


class TestFinanceModuleContext:
    def _setup_user_with_txs(self, n: int = 5):
        from server.db import db_session
        from server.models import User, FinanceTransaction
        from datetime import datetime
        import hashlib
        with db_session() as db:
            u = User(email=f"fin-{time.time_ns()}@x.x",
                    password_hash="h", is_verified=True,
                    agreed_to_terms=True, tokens_balance=0)
            db.add(u); db.commit(); db.refresh(u)
            for i in range(n):
                desc = f"Test merchant {i}"
                tx = FinanceTransaction(
                    user_id=u.id, source="csv:test",
                    date=datetime(2026, 5, i+1),
                    amount_kop=-(100 + i*50),
                    currency="RUB", description=desc,
                    category="food",
                    description_hash=hashlib.md5(desc.encode()).hexdigest()[:16],
                )
                db.add(tx)
            db.commit()
            return u.id

    def _cleanup(self, uid):
        from server.db import db_session
        from server.models import User, FinanceTransaction
        with db_session() as db:
            db.query(FinanceTransaction).filter_by(user_id=uid).delete()
            db.query(User).filter_by(id=uid).delete()
            db.commit()

    def test_no_transactions_returns_empty(self):
        from server.agent_builder import _build_module_extra_context
        from server.db import db_session
        from server.models import User
        with db_session() as db:
            u = User(email=f"empty-{time.time_ns()}@x.x",
                    password_hash="h", is_verified=True,
                    agreed_to_terms=True, tokens_balance=0)
            db.add(u); db.commit(); db.refresh(u)
            uid = u.id
        try:
            assert _build_module_extra_context("finance", user_id=uid) == ""
        finally:
            self._cleanup(uid)

    def test_with_transactions_injects_summary(self):
        from server.agent_builder import _build_module_extra_context
        uid = self._setup_user_with_txs(n=5)
        try:
            ctx = _build_module_extra_context("finance", user_id=uid)
            assert "ФИНАНСЫ" in ctx
            assert "Test merchant" in ctx
            assert "🍲" in ctx
        finally:
            self._cleanup(uid)
