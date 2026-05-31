"""Тесты Multi-LLM паттернов в llm_router: pipeline / parallel / verify.

Мокируем `ask()` через monkeypatch — для unit-уровня этого достаточно.
Реальный generate_response не вызывается.
"""
import pytest

from server import llm_router
from server.llm_router import (
    RouteResult, pipeline_ask, parallel_ask, verify_ask,
    PARALLEL_MAX_BRANCHES,
)


def _make_result(content: str, model: str = "claude-haiku",
                 task: str = "default", cost_kop: int = 5) -> RouteResult:
    return RouteResult(
        content=content,
        model_used=model,
        task_type=task,
        complexity="medium",
        raw={"type": "text", "content": content,
             "usage": {"input_tokens": 10, "output_tokens": 20,
                       "actual_cost_kop": cost_kop}},
    )


# ── pipeline_ask ────────────────────────────────────────────────────────────


class TestPipelineAsk:
    def test_two_steps_chain(self, monkeypatch):
        """Step 1 получает {prev} = вывод step 0. Trace содержит оба шага."""
        calls = []

        def fake_ask(messages, *, task=None, complexity=None, **kw):
            calls.append({"task": task, "messages": messages})
            n = len(calls)
            return _make_result(f"STEP{n}_OUTPUT", task=task or "default", cost_kop=10)

        monkeypatch.setattr(llm_router, "ask", fake_ask)

        result = pipeline_ask(
            [{"role": "system", "content": "sys"},
             {"role": "user", "content": "напиши план"}],
            steps=[
                {"task": "research", "complexity": "simple"},
                {"task": "creative_writing",
                 "prompt_template": "Расширь черновик: {prev}"},
            ],
        )

        # 2 шага выполнены
        assert len(calls) == 2
        # Финальный content — от step 1
        assert result.content == "STEP2_OUTPUT"
        # Step 1 должен получить вывод step 0 в user-сообщении
        step1_user_msg = calls[1]["messages"][-1]["content"]
        assert "STEP1_OUTPUT" in step1_user_msg
        assert "Расширь черновик" in step1_user_msg
        # Trace
        trace = result.raw["pipeline_trace"]
        assert len(trace) == 2
        assert trace[0]["step"] == 0 and trace[1]["step"] == 1
        assert result.raw["total_cost_kop"] == 20

    def test_failure_returns_last_success(self, monkeypatch):
        """Если шаг 2 падает — возвращаем результат шага 1 + pipeline_error."""
        def fake_ask(messages, *, task=None, **kw):
            if task == "creative_writing":
                raise RuntimeError("LLM exploded")
            return _make_result("STEP1_OK", task="research")

        monkeypatch.setattr(llm_router, "ask", fake_ask)

        result = pipeline_ask(
            [{"role": "user", "content": "test"}],
            steps=[
                {"task": "research"},
                {"task": "creative_writing", "prompt_template": "{prev}"},
            ],
        )
        assert result.content == "STEP1_OK"
        assert "pipeline_error" in result.raw
        assert "LLM exploded" in result.raw["pipeline_error"]
        assert len(result.raw["pipeline_trace"]) == 1

    def test_empty_steps_raises(self):
        with pytest.raises(ValueError):
            pipeline_ask([{"role": "user", "content": "x"}], steps=[])

    def test_initial_placeholder_in_template(self, monkeypatch):
        """Шаблон может ссылаться на {initial} = оригинальный user-запрос."""
        calls = []

        def fake_ask(messages, *, task=None, **kw):
            calls.append(messages)
            return _make_result(f"out_{len(calls)}", task=task or "default")

        monkeypatch.setattr(llm_router, "ask", fake_ask)

        pipeline_ask(
            [{"role": "user", "content": "ИСХОДНЫЙ_ЗАПРОС"}],
            steps=[
                {"task": "research"},
                {"task": "deep_analysis",
                 "prompt_template": "Контекст: {initial}\nЧерновик: {prev}"},
            ],
        )
        step1_user = calls[1][-1]["content"]
        assert "ИСХОДНЫЙ_ЗАПРОС" in step1_user
        assert "out_1" in step1_user


# ── parallel_ask ────────────────────────────────────────────────────────────


class TestParallelAsk:
    def test_three_branches_all_run(self, monkeypatch):
        """3 ветки → 3 вызова ask, без синтеза возвращается первая успешная."""
        calls = []

        def fake_ask(messages, *, task=None, **kw):
            calls.append(task)
            return _make_result(f"branch_{task}", task=task or "default",
                                cost_kop=7)

        monkeypatch.setattr(llm_router, "ask", fake_ask)

        result = parallel_ask(
            [{"role": "user", "content": "вопрос"}],
            branches=[
                {"task": "research"},
                {"task": "deep_analysis"},
                {"task": "creative_writing"},
            ],
        )
        assert sorted(calls) == ["creative_writing", "deep_analysis", "research"]
        assert len(result.raw["branch_trace"]) == 3
        # total_cost = 7×3 = 21 (без синтеза)
        assert result.raw["total_cost_kop"] == 21

    def test_with_synthesize(self, monkeypatch):
        """С synthesize — финальный вызов получает {branch_0..N} в prompt."""
        calls = []

        def fake_ask(messages, *, task=None, **kw):
            calls.append({"task": task, "user_msg": messages[-1]["content"]})
            if task == "deep_analysis":
                return _make_result("FINAL_SYNTH", task=task, cost_kop=15)
            return _make_result(f"answer_{task}", task=task or "default",
                                cost_kop=5)

        monkeypatch.setattr(llm_router, "ask", fake_ask)

        result = parallel_ask(
            [{"role": "user", "content": "вопрос"}],
            branches=[
                {"task": "research"},
                {"task": "creative_writing"},
            ],
            synthesize={
                "task": "deep_analysis",
                "prompt_template": "Ветка A: {branch_0}\nВетка B: {branch_1}",
            },
        )
        # Должен быть финальный синтез
        assert result.content == "FINAL_SYNTH"
        # Синтез получил оба ответа веток
        synth_call = [c for c in calls if c["task"] == "deep_analysis"][0]
        assert "answer_research" in synth_call["user_msg"]
        assert "answer_creative_writing" in synth_call["user_msg"]
        # Cost: 5+5+15 = 25
        assert result.raw["total_cost_kop"] == 25

    def test_one_branch_fails(self, monkeypatch):
        """Одна упавшая ветка не валит остальные."""
        def fake_ask(messages, *, task=None, **kw):
            if task == "research":
                raise RuntimeError("perplexity down")
            return _make_result(f"ok_{task}", task=task or "default")

        monkeypatch.setattr(llm_router, "ask", fake_ask)

        result = parallel_ask(
            [{"role": "user", "content": "x"}],
            branches=[
                {"task": "research"},
                {"task": "creative_writing"},
            ],
        )
        # trace содержит обе записи — одну с error=True
        trace = result.raw["branch_trace"]
        assert len(trace) == 2
        errored = [t for t in trace if t.get("error")]
        assert len(errored) == 1
        assert errored[0]["task"] == "research"
        # Result — успешная ветка
        assert result.content == "ok_creative_writing"

    def test_cap_max_branches(self):
        """Превышение PARALLEL_MAX_BRANCHES → ValueError."""
        many = [{"task": "default"}] * (PARALLEL_MAX_BRANCHES + 1)
        with pytest.raises(ValueError, match="too many branches"):
            parallel_ask([{"role": "user", "content": "x"}], branches=many)

    def test_empty_branches_raises(self):
        with pytest.raises(ValueError):
            parallel_ask([{"role": "user", "content": "x"}], branches=[])

    def test_all_branches_fail(self, monkeypatch):
        """Все ветки падают → пустой content + parallel_error."""
        def fake_ask(messages, **kw):
            raise RuntimeError("everything broken")

        monkeypatch.setattr(llm_router, "ask", fake_ask)

        result = parallel_ask(
            [{"role": "user", "content": "x"}],
            branches=[{"task": "research"}, {"task": "creative_writing"}],
        )
        assert result.content == ""
        assert "parallel_error" in result.raw


# ── verify_ask ──────────────────────────────────────────────────────────────


class TestVerifyAsk:
    def test_verified_short_response(self, monkeypatch):
        """Verifier отвечает 'VERIFIED' → verdict=verified."""
        calls = []

        def fake_ask(messages, *, task=None, **kw):
            calls.append(task)
            if task == "factcheck":
                return _make_result("VERIFIED", task="factcheck",
                                    model="perplexity-pro", cost_kop=3)
            return _make_result("primary answer", task="research",
                                model="claude-sonnet", cost_kop=10)

        monkeypatch.setattr(llm_router, "ask", fake_ask)

        result = verify_ask(
            [{"role": "user", "content": "что нового в физике"}],
            primary_task="research",
        )
        assert result.content == "primary answer"
        assert result.raw["verification"]["verdict"] == "verified"
        assert result.raw["verification"]["verifier_model"] == "perplexity-pro"
        assert result.raw["total_cost_kop"] == 13

    def test_issues_found(self, monkeypatch):
        """Verifier пишет критику → verdict=issues_found."""
        def fake_ask(messages, *, task=None, **kw):
            if task == "factcheck":
                return _make_result(
                    "1. Цифра 50% не подтверждается. 2. Дата неверна.",
                    task="factcheck", cost_kop=4,
                )
            return _make_result("ответ с ошибками", task="research", cost_kop=8)

        monkeypatch.setattr(llm_router, "ask", fake_ask)

        result = verify_ask(
            [{"role": "user", "content": "стата по чему-то"}],
            primary_task="research",
        )
        assert result.raw["verification"]["verdict"] == "issues_found"
        assert "не подтверждается" in result.raw["verification"]["verifier_content"]

    def test_contradicted(self, monkeypatch):
        """Verifier начинает с CONTRADICTED → verdict=contradicted."""
        def fake_ask(messages, *, task=None, **kw):
            if task == "factcheck":
                return _make_result(
                    "CONTRADICTED: фактов нет, всё выдумано",
                    task="factcheck",
                )
            return _make_result("выдумка", task="realtime")

        monkeypatch.setattr(llm_router, "ask", fake_ask)

        result = verify_ask(
            [{"role": "user", "content": "что было в X сегодня"}],
            primary_task="realtime",
        )
        assert result.raw["verification"]["verdict"] == "contradicted"

    def test_verifier_failure_returns_primary(self, monkeypatch):
        """Если verifier падает — primary всё равно отдаётся, плюс verify_error."""
        def fake_ask(messages, *, task=None, **kw):
            if task == "factcheck":
                raise RuntimeError("perplexity 503")
            return _make_result("primary ok", task="default")

        monkeypatch.setattr(llm_router, "ask", fake_ask)

        result = verify_ask([{"role": "user", "content": "x"}])
        assert result.content == "primary ok"
        assert "verify_error" in result.raw
        assert "verification" not in result.raw
