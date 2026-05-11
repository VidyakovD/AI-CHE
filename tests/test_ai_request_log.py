"""
Тесты для AiRequestLog — fire-and-forget трэкинг AI-вызовов.
"""
import pytest


class TestAiRequestLogModel:
    def test_model_importable(self):
        from server.models import AiRequestLog
        assert AiRequestLog.__tablename__ == "ai_request_logs"

    def test_can_create_row(self):
        from server.db import db_session
        from server.models import AiRequestLog
        with db_session() as db:
            row = AiRequestLog(
                provider="openai",
                model="gpt-4o",
                purpose="chat",
                input_tokens=100,
                output_tokens=50,
                cost_kop=42,
                duration_ms=1234,
                success=True,
            )
            db.add(row)
            db.commit()
            assert row.id is not None


class TestLogAiRequestHelper:
    """Проверяем что _log_ai_request никогда не падает (fail-safe)."""

    def test_writes_success_row(self):
        from server.ai import _log_ai_request
        from server.db import db_session
        from server.models import AiRequestLog
        _log_ai_request(
            provider="anthropic", model="claude-sonnet-4-6",
            purpose="test_unit", user_id=None,
            input_tokens=200, output_tokens=300,
            cost_kop=15, duration_ms=2500,
            success=True,
        )
        with db_session() as db:
            row = (db.query(AiRequestLog)
                   .filter_by(purpose="test_unit")
                   .order_by(AiRequestLog.id.desc())
                   .first())
            assert row is not None
            assert row.provider == "anthropic"
            assert row.model == "claude-sonnet-4-6"
            assert row.input_tokens == 200
            assert row.success is True

    def test_writes_failure_row(self):
        from server.ai import _log_ai_request
        from server.db import db_session
        from server.models import AiRequestLog
        _log_ai_request(
            provider="openai", model="gpt-4o",
            purpose="test_fail", user_id=None,
            input_tokens=0, output_tokens=0,
            cost_kop=0, duration_ms=100,
            success=False, error="TimeoutError",
        )
        with db_session() as db:
            row = (db.query(AiRequestLog)
                   .filter_by(purpose="test_fail")
                   .order_by(AiRequestLog.id.desc())
                   .first())
            assert row is not None
            assert row.success is False
            assert row.error == "TimeoutError"

    def test_fail_safe_on_bad_input(self):
        """Кривой ввод не должен ломать AI-биллинг."""
        from server.ai import _log_ai_request
        # Должно проглотить и НЕ raise
        _log_ai_request(
            provider="openai", model="x",
            purpose=None, user_id=None,
            input_tokens="not_int",  # type: ignore[arg-type]
            output_tokens=None,
            cost_kop=None, duration_ms=None,
            success=True,
        )

    def test_truncates_long_strings(self):
        from server.ai import _log_ai_request
        from server.db import db_session
        from server.models import AiRequestLog
        long_purpose = "x" * 500
        long_error = "y" * 500
        _log_ai_request(
            provider="x", model="m", purpose=long_purpose, user_id=None,
            input_tokens=0, output_tokens=0, cost_kop=0, duration_ms=0,
            success=False, error=long_error,
        )
        with db_session() as db:
            row = (db.query(AiRequestLog)
                   .order_by(AiRequestLog.id.desc()).first())
            assert row is not None
            # Поля обрезаются (purpose 50, error 200)
            assert len(row.purpose or "") <= 50
            assert len(row.error or "") <= 200
