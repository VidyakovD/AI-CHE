"""LLM Router — выбор модели по типу задачи.

Слой ВЫШЕ generate_response. Вместо того чтобы caller жёстко выбирал
модель ("claude-sonnet" / "perplexity-pro"), он говорит ROUTER:
  ask(task="research", query="что сейчас в РФ с инфляцией")
  ask(task="creative_writing", complexity="complex", messages=[...])

Router сам выбирает оптимальную модель из матрицы (раздел 4.2 ТЗ), c учётом:
  - task_type (research / creative / code / agent / realtime / simple / factcheck / longdoc)
  - complexity (simple / medium / complex)
  - sensitivity (low / high — для критичных = Claude который скорее откажется чем галлюцинирует)
  - user-override (если задан) — для продвинутых юзеров

Базовый MVP — single-model routing. Паттерны Pipeline/Parallel/Verify добавим позже.

Используется новыми модулями ИИ Агентов (server/agent_builder.py итд).
Старые вызовы generate_response() напрямую — продолжают работать без изменений.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# ── Task taxonomy ────────────────────────────────────────────────────────────

TASK_TYPES = {
    "research",            # свежие факты, новости, что-то новое в мире
    "deep_analysis",       # большие тексты, длинная аналитика, синтез
    "creative_writing",    # копирайтинг, посты, тексты
    "code",                # генерация/правка кода
    "agent_tools",         # tool-calling, выполнение действий
    "realtime",            # X/Twitter, биржа, события сейчас
    "simple",              # короткий ответ, классификация, генерация JSON по схеме
    "factcheck",           # верификация фактов другой модели
    "longdoc",             # >100K токенов
    "default",             # неклассифицированный — по умолчанию
}

COMPLEXITY = ("simple", "medium", "complex")
SENSITIVITY = ("low", "high")


# ── Routing matrix (из ТЗ Project Loom v0.2 раздел 4.2) ─────────────────────
#
# Структура: task_type → {complexity → [primary, fallback1, fallback2]}
# При недоступности primary (нет ключа / health-check fail) → пробуем fallback.

ROUTING_MATRIX: dict[str, dict[str, list[str]]] = {
    "research": {
        "simple":  ["perplexity",         "claude-haiku",   "gpt-4o-mini"],
        "medium":  ["perplexity-pro",     "claude-sonnet",  "gpt-4o"],
        "complex": ["perplexity-pro",     "claude-sonnet",  "gpt-4o"],
    },
    "deep_analysis": {
        "simple":  ["claude-sonnet",      "gpt-4o",         "perplexity-pro"],
        "medium":  ["claude-sonnet",      "gpt-4o",         "perplexity-pro"],
        "complex": ["claude-opus",        "claude-sonnet",  "gpt-4o"],
    },
    "creative_writing": {
        "simple":  ["claude-haiku",       "gpt-4o-mini",    "grok"],
        "medium":  ["claude-sonnet",      "gpt-4o",         "grok"],
        "complex": ["claude-sonnet",      "claude-opus",    "gpt-4o"],
    },
    "code": {
        "simple":  ["claude-haiku",       "gpt-4o-mini",    "claude-sonnet"],
        "medium":  ["claude-sonnet",      "gpt-4o",         "grok"],
        "complex": ["claude-opus",        "claude-sonnet",  "gpt-4o"],
    },
    "agent_tools": {
        # Claude — лидер MCP-Atlas (77%), используем для tool-calling
        "simple":  ["claude-haiku",       "gpt-4o-mini"],
        "medium":  ["claude-sonnet",      "gpt-4o"],
        "complex": ["claude-sonnet",      "claude-opus",    "gpt-4o"],
    },
    "realtime": {
        # Grok нативно читает X. НО! Один — высокая галлюцинация цитат (94%).
        # Поэтому в паре с Perplexity для верификации (будет в pattern=verify).
        "simple":  ["grok",               "perplexity",     "claude-haiku"],
        "medium":  ["grok",               "perplexity-pro"],
        "complex": ["grok",               "perplexity-pro", "claude-sonnet"],
    },
    "simple": {
        "simple":  ["claude-haiku",       "gpt-4o-mini"],
        "medium":  ["claude-haiku",       "gpt-4o-mini"],
        "complex": ["claude-sonnet",      "gpt-4o"],
    },
    "factcheck": {
        # Perplexity — лучший CJR (37%), SimpleQA F-score 0.858
        "simple":  ["perplexity",         "claude-sonnet"],
        "medium":  ["perplexity-pro",     "claude-sonnet"],
        "complex": ["perplexity-pro",     "claude-opus"],
    },
    "longdoc": {
        # Длинные документы >100K — Claude / GPT с 1M контекстом
        "simple":  ["claude-sonnet",      "gpt-4o"],
        "medium":  ["claude-sonnet",      "gpt-4o"],
        "complex": ["claude-opus",        "claude-sonnet"],
    },
    "default": {
        "simple":  ["claude-haiku",       "gpt-4o-mini"],
        "medium":  ["claude-sonnet",      "gpt-4o"],
        "complex": ["claude-sonnet",      "claude-opus"],
    },
}


# ── Keyword-based classifier (без LLM, чтобы не было recursion) ─────────────

_RESEARCH_KEYWORDS = {
    "найди", "поищи", "что сейчас", "что происходит", "тренды", "статистика",
    "конкуренты", "рынок", "исследуй", "research", "search", "swot",
    "новости", "обзор", "цены", "сколько стоит", "стоимость рынка",
}
_REALTIME_KEYWORDS = {
    "сейчас в twitter", "x.com", "твиттер", "сейчас в x", "happening now",
    "live", "только что", "последние минуты", "в этом часе",
}
_CODE_KEYWORDS = {
    "напиши код", "напиши функцию", "программа", "скрипт", "python", "javascript",
    "typescript", "rust", "golang", "регулярка", "regex", "sql-запрос",
    "разработай", "имплементируй", "code", "function", "class", "api endpoint",
}
_CREATIVE_KEYWORDS = {
    "напиши пост", "придумай", "тексты для", "копирайт", "контент-план",
    "слоган", "название", "креатив", "продающий текст", "сторителлинг",
}
_FACTCHECK_KEYWORDS = {
    "проверь факт", "правда ли", "верификация", "это так?", "источник",
    "подтверди", "factcheck", "fact-check",
}
_SIMPLE_KEYWORDS = {
    "переведи", "переформулируй", "сократи", "перепиши короче",
    "сделай list", "оформи списком", "извлеки", "классифицируй",
}


def classify_task(text: str) -> str:
    """Быстрая keyword-классификация задачи. Без LLM — синхронно, мгновенно.
    Для сложных случаев caller может передать task явно."""
    if not text:
        return "default"
    t = text.lower()

    def _hit(keywords: set[str]) -> bool:
        return any(k in t for k in keywords)

    if _hit(_REALTIME_KEYWORDS):
        return "realtime"
    if _hit(_FACTCHECK_KEYWORDS):
        return "factcheck"
    if _hit(_CODE_KEYWORDS):
        return "code"
    if _hit(_RESEARCH_KEYWORDS):
        return "research"
    if _hit(_CREATIVE_KEYWORDS):
        return "creative_writing"
    if _hit(_SIMPLE_KEYWORDS):
        return "simple"
    return "default"


def detect_complexity(text: str, *, has_attachments: bool = False) -> str:
    """Грубая эвристика сложности по длине + attachments. Без LLM."""
    if not text:
        return "simple"
    n = len(text)
    if has_attachments or n > 4000:
        return "complex"
    if n > 800:
        return "medium"
    return "simple"


# ── Pick model with availability check ──────────────────────────────────────

def _is_provider_available(model_alias: str) -> bool:
    """Проверяем что у провайдера есть рабочий ключ. Используем ту же логику
    что generate_response — _get_api_keys(). Кэшировано на 60 сек в ai.py."""
    try:
        from server.ai import resolve_model, _get_api_keys
        cfg = resolve_model(model_alias)
        if not cfg:
            return False
        keys = _get_api_keys(cfg["provider"])
        return bool(keys)
    except Exception as e:
        log.warning(f"[router] availability check failed for {model_alias}: {e}")
        return False


def pick_model(task_type: str = "default", complexity: str = "medium",
               sensitivity: str = "low") -> str:
    """Выбрать модель по типу задачи + сложности. Гарантирует возврат
    доступной модели (fallback по цепочке если primary недоступна)."""
    if task_type not in ROUTING_MATRIX:
        task_type = "default"
    if complexity not in COMPLEXITY:
        complexity = "medium"

    chain = ROUTING_MATRIX[task_type].get(complexity, ROUTING_MATRIX["default"]["medium"])

    # Для high-sensitivity предпочитаем Claude (он скорее откажется чем галлюцинирует)
    if sensitivity == "high":
        for m in chain:
            if m.startswith("claude-"):
                if _is_provider_available(m):
                    return m

    # Обычный порядок: первый доступный из цепочки
    for m in chain:
        if _is_provider_available(m):
            return m

    # Hardcoded fallback — должен быть всегда (Claude Haiku самый дешёвый из 4)
    for fallback in ("claude-haiku", "gpt-4o-mini", "claude-sonnet"):
        if _is_provider_available(fallback):
            log.warning(f"[router] all matrix options unavailable for {task_type}/{complexity}, "
                        f"using fallback {fallback}")
            return fallback

    # Совсем плохо — никаких ключей. Возвращаем claude-haiku, generate_response
    # вернёт ошибку, caller увидит её и покажет юзеру.
    log.error(f"[router] no providers available at all!")
    return "claude-haiku"


# ── Main entry point ────────────────────────────────────────────────────────

@dataclass
class RouteResult:
    content: str
    model_used: str
    task_type: str
    complexity: str
    raw: dict       # сырой ответ generate_response


def ask(messages: list, *, task: str | None = None,
        complexity: str | None = None,
        sensitivity: str = "low",
        user_query_hint: str | None = None,
        extra: dict | None = None,
        user_api_key: str | None = None) -> RouteResult:
    """Главный entry point: получить ответ LLM с автоматическим выбором модели.

    Args:
        messages: список [{role, content}] в OpenAI-style формате
        task: явный task-type (если None — классифицируем по последнему user-msg)
        complexity: simple|medium|complex (если None — detect_complexity)
        sensitivity: low|high (для критичных запросов — Claude приоритет)
        user_query_hint: если есть отдельная "тема" — используется для классификации
        extra: пробрасывается в generate_response (max_tokens, temperature, _purpose)
        user_api_key: проброс юзер-ключа если есть

    Returns: RouteResult
    """
    from server.ai import generate_response

    # 1. Определяем task если не задан явно
    sample = user_query_hint or ""
    if not sample:
        # Берём последнее user-сообщение
        for m in reversed(messages or []):
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    sample = c
                    break
                elif isinstance(c, list):
                    # multi-modal content — берём первый text-блок
                    for blk in c:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            sample = blk.get("text", "")
                            break
                    if sample:
                        break

    actual_task = task or classify_task(sample)
    if actual_task not in TASK_TYPES:
        actual_task = "default"

    actual_complexity = complexity or detect_complexity(sample)
    if actual_complexity not in COMPLEXITY:
        actual_complexity = "medium"

    # 2. Выбираем модель
    model = pick_model(actual_task, actual_complexity, sensitivity)

    log.info(f"[router] task={actual_task} complexity={actual_complexity} "
             f"sens={sensitivity} → model={model}")

    # 3. Вызываем generate_response
    ex = dict(extra or {})
    ex.setdefault("_router_task", actual_task)
    ex.setdefault("_router_complexity", actual_complexity)
    result = generate_response(model, messages, extra=ex, user_api_key=user_api_key)

    content = ""
    if isinstance(result, dict):
        content = result.get("content", "")

    return RouteResult(
        content=content,
        model_used=model,
        task_type=actual_task,
        complexity=actual_complexity,
        raw=result if isinstance(result, dict) else {"content": str(result)},
    )
