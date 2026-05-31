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

Используется новыми модулями ИИ Агентов (server/agent_builder.py итд).
Старые вызовы generate_response() напрямую — продолжают работать без изменений.

═══ Multi-LLM patterns (раздел 4.3 ТЗ) ═══

  pipeline_ask  — output модели A → context для модели B (research → synth)
  parallel_ask  — N веток одновременно + опц. синтез одной моделью
  verify_ask    — основная модель отвечает, верификатор проверяет на галлюцинации

Все три — sync API, переиспользуют ask() и cached_generate_response внутри.
Биллинг суммируется через raw["total_cost_kop"]. PrivacyGuard наследуется.
"""
from __future__ import annotations

import asyncio
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

    # 3. Вызываем generate_response (с попыткой использовать LLM-кэш)
    ex = dict(extra or {})
    ex.setdefault("_router_task", actual_task)
    ex.setdefault("_router_complexity", actual_complexity)
    try:
        from server.llm_cache import cached_generate_response
        result = cached_generate_response(model, messages, extra=ex,
                                          user_api_key=user_api_key)
    except Exception as e:
        log.warning(f"[router] cache wrapper failed, fallback to direct: {e}")
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


# ── Multi-LLM patterns ─────────────────────────────────────────────────────
#
# Три паттерна из ТЗ Project Loom v0.2 раздел 4.3:
#   pipeline_ask  — последовательная цепочка моделей (research → synth)
#   parallel_ask  — N веток одновременно + опц. синтез одной моделью
#   verify_ask    — основная модель отвечает, верификатор проверяет
#
# Все три — sync API поверх ask(). Биллинг суммируется через
# raw["total_cost_kop"]. На каждом шаге cache (cached_generate_response).


def _extract_cost_kop(raw: dict | None) -> int:
    """Безопасно достать actual_cost_kop из result.raw['usage']."""
    if not isinstance(raw, dict):
        return 0
    try:
        return int(raw.get("usage", {}).get("actual_cost_kop", 0) or 0)
    except Exception:
        return 0


def _replace_last_user(messages: list, new_content: str) -> list:
    """Вернуть копию messages с заменой content последнего user-сообщения.
    Если user-сообщений нет — добавляет одно в конец."""
    if not messages:
        return [{"role": "user", "content": new_content}]
    out = [dict(m) for m in messages]
    last_idx = -1
    for i, m in enumerate(out):
        if m.get("role") == "user":
            last_idx = i
    if last_idx >= 0:
        out[last_idx]["content"] = new_content
    else:
        out.append({"role": "user", "content": new_content})
    return out


def _extract_initial_query(messages: list) -> str:
    """Текст последнего user-сообщения (для подстановки {initial} в шаблон)."""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        return blk.get("text", "") or ""
    return ""


def pipeline_ask(
    initial_messages: list,
    steps: list[dict],
    *,
    sensitivity: str = "low",
    user_api_key: str | None = None,
    extra: dict | None = None,
) -> RouteResult:
    """Последовательная цепочка LLM-вызовов. Output шага N → context шага N+1.

    Args:
        initial_messages: стартовый [{role, content}] для шага 0
        steps: список конфигов шагов. Каждый шаг — dict с ключами:
            task: str            — task-type для router'а (обязательно)
            complexity: str      — simple|medium|complex (def "medium")
            prompt_template: str — для step>=1: шаблон с {prev} (def "{prev}")
                                   Также доступен {initial} — исх. user-запрос
            system_prompt: str   — для step>=1: новый system (def — берёт из initial_messages)

    Шаг 0 использует initial_messages как есть. Шаг N>=1: system берётся из
    step.system_prompt или из последнего system в initial_messages, user-msg
    формируется из prompt_template (подстановка {prev}/{initial}).

    Returns:
        RouteResult последнего УСПЕШНОГО шага. Дополнительно в raw:
            pipeline_trace: [{step, model, task, content_preview}, ...]
            total_cost_kop: суммарная стоимость всех шагов
            pipeline_error: str — если цепочка прервалась
    """
    if not steps:
        raise ValueError("pipeline_ask: steps must be non-empty")

    initial_query = _extract_initial_query(initial_messages)
    # Базовый system — первый system-msg из initial_messages (если есть)
    base_system = None
    for m in (initial_messages or []):
        if m.get("role") == "system":
            base_system = m.get("content")
            break

    trace: list[dict] = []
    total_cost = 0
    prev_output: str = ""
    last_result: RouteResult | None = None
    pipeline_error: str | None = None

    for idx, step in enumerate(steps):
        task = step.get("task", "default")
        complexity = step.get("complexity", "medium")

        if idx == 0:
            messages = list(initial_messages or [])
        else:
            template = step.get("prompt_template", "{prev}")
            try:
                user_text = template.format(prev=prev_output, initial=initial_query)
            except (KeyError, IndexError) as e:
                pipeline_error = f"step {idx}: prompt_template format failed: {e}"
                log.warning(f"[pipeline] {pipeline_error}")
                break
            system_text = step.get("system_prompt", base_system)
            messages = []
            if system_text:
                messages.append({"role": "system", "content": system_text})
            messages.append({"role": "user", "content": user_text})

        try:
            result = ask(
                messages, task=task, complexity=complexity,
                sensitivity=sensitivity, extra=extra, user_api_key=user_api_key,
            )
        except Exception as e:
            pipeline_error = f"step {idx} ({task}): {type(e).__name__}: {e}"
            log.warning(f"[pipeline] {pipeline_error}")
            break

        prev_output = result.content or ""
        last_result = result
        total_cost += _extract_cost_kop(result.raw)
        trace.append({
            "step": idx,
            "model": result.model_used,
            "task": result.task_type,
            "complexity": result.complexity,
            "content_preview": (result.content or "")[:200],
        })

    if last_result is None:
        # Все шаги упали (или прервалось до первого). Возвращаем пустой результат.
        return RouteResult(
            content="", model_used="", task_type="default", complexity="medium",
            raw={"pipeline_trace": trace, "total_cost_kop": 0,
                 "pipeline_error": pipeline_error or "no steps executed"},
        )

    last_result.raw.setdefault("pipeline_trace", trace)
    last_result.raw["pipeline_trace"] = trace
    last_result.raw["total_cost_kop"] = total_cost
    if pipeline_error:
        last_result.raw["pipeline_error"] = pipeline_error
    return last_result


PARALLEL_MAX_BRANCHES = 5


def parallel_ask(
    messages: list,
    branches: list[dict],
    *,
    synthesize: dict | None = None,
    sensitivity: str = "low",
    user_api_key: str | None = None,
    extra: dict | None = None,
) -> RouteResult:
    """N веток одновременно через asyncio.gather. Опционально — синтез одной моделью.

    Args:
        messages: общий контекст [{role, content}] для всех веток
        branches: список конфигов веток (1..5). Каждая — dict:
            task: str            — task-type (обязательно)
            complexity: str      — def "medium"
            prompt_template: str — опц. шаблон для замены последнего user-msg,
                                   подстановка {initial} = исх. user-запрос
        synthesize: опц. dict — финальный синтез:
            task: str            — def "deep_analysis"
            complexity: str      — def "medium"
            prompt_template: str — обязательно. Доступны {branch_0}..{branch_N}
                                   и {initial}

    Returns:
        Если synthesize задан: RouteResult синтеза. raw содержит:
            branch_trace: [{branch, model, task, content_preview}, ...]
            total_cost_kop: суммарно
        Если synthesize нет: первая успешная ветка + branch_trace со всеми.

    Raises:
        ValueError: если branches пуст или больше PARALLEL_MAX_BRANCHES.
    """
    if not branches:
        raise ValueError("parallel_ask: branches must be non-empty")
    if len(branches) > PARALLEL_MAX_BRANCHES:
        raise ValueError(
            f"parallel_ask: too many branches ({len(branches)} > "
            f"{PARALLEL_MAX_BRANCHES}). Лимит против биллинг-blow-up."
        )

    initial_query = _extract_initial_query(messages)

    def _build_branch_messages(branch_cfg: dict) -> list:
        tpl = branch_cfg.get("prompt_template")
        if not tpl:
            return list(messages or [])
        try:
            new_user = tpl.format(initial=initial_query)
        except (KeyError, IndexError):
            new_user = tpl  # fallback — без подстановки
        return _replace_last_user(messages or [], new_user)

    def _run_branch(branch_cfg: dict) -> RouteResult | None:
        try:
            return ask(
                _build_branch_messages(branch_cfg),
                task=branch_cfg.get("task", "default"),
                complexity=branch_cfg.get("complexity", "medium"),
                sensitivity=sensitivity, extra=extra, user_api_key=user_api_key,
            )
        except Exception as e:
            log.warning(f"[parallel] branch {branch_cfg.get('task')} failed: "
                        f"{type(e).__name__}: {e}")
            return None

    async def _gather_all() -> list[RouteResult | None]:
        # asyncio.to_thread: ask() — sync, выносим в threadpool чтобы реально
        # ускорить вызовы (httpx внутри отпускает GIL на I/O).
        return await asyncio.gather(
            *[asyncio.to_thread(_run_branch, b) for b in branches],
            return_exceptions=False,
        )

    try:
        # Если уже в event loop — нельзя asyncio.run; используем nested loop.
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Создаём отдельный поток для запуска asyncio.run — редкий путь
            # (когда parallel_ask вызвана из async-контекста). Sync API.
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=1) as pool:
                results = pool.submit(lambda: asyncio.run(_gather_all())).result()
        else:
            results = asyncio.run(_gather_all())
    except RuntimeError:
        results = asyncio.run(_gather_all())

    trace = []
    total_cost = 0
    successful: list[RouteResult] = []
    for i, r in enumerate(results):
        if r is None:
            trace.append({"branch": i, "model": "", "task": branches[i].get("task"),
                          "error": True, "content_preview": ""})
            continue
        trace.append({
            "branch": i, "model": r.model_used, "task": r.task_type,
            "complexity": r.complexity,
            "content_preview": (r.content or "")[:200],
        })
        total_cost += _extract_cost_kop(r.raw)
        successful.append(r)

    if not successful:
        return RouteResult(
            content="", model_used="", task_type="default", complexity="medium",
            raw={"branch_trace": trace, "total_cost_kop": 0,
                 "parallel_error": "all branches failed"},
        )

    # Без синтеза — возвращаем первую успешную ветку
    if not synthesize:
        first = successful[0]
        first.raw["branch_trace"] = trace
        first.raw["total_cost_kop"] = total_cost
        return first

    # С синтезом — формируем prompt с {branch_N}
    template = synthesize.get("prompt_template")
    if not template:
        raise ValueError("parallel_ask: synthesize.prompt_template is required")
    fmt_kwargs: dict[str, str] = {"initial": initial_query}
    for i, r in enumerate(results):
        fmt_kwargs[f"branch_{i}"] = (r.content if r else "") or "(нет ответа)"
    try:
        synth_user = template.format(**fmt_kwargs)
    except (KeyError, IndexError) as e:
        log.warning(f"[parallel] synthesize template failed: {e}")
        first = successful[0]
        first.raw["branch_trace"] = trace
        first.raw["total_cost_kop"] = total_cost
        first.raw["parallel_error"] = f"synthesize template failed: {e}"
        return first

    base_system = None
    for m in (messages or []):
        if m.get("role") == "system":
            base_system = m.get("content")
            break
    synth_messages: list[dict] = []
    if base_system:
        synth_messages.append({"role": "system", "content": base_system})
    synth_messages.append({"role": "user", "content": synth_user})

    try:
        synth_result = ask(
            synth_messages,
            task=synthesize.get("task", "deep_analysis"),
            complexity=synthesize.get("complexity", "medium"),
            sensitivity=sensitivity, extra=extra, user_api_key=user_api_key,
        )
    except Exception as e:
        log.warning(f"[parallel] synthesize call failed: {type(e).__name__}: {e}")
        first = successful[0]
        first.raw["branch_trace"] = trace
        first.raw["total_cost_kop"] = total_cost
        first.raw["parallel_error"] = f"synthesize failed: {e}"
        return first

    total_cost += _extract_cost_kop(synth_result.raw)
    synth_result.raw["branch_trace"] = trace
    synth_result.raw["total_cost_kop"] = total_cost
    return synth_result


_VERIFY_SYSTEM = (
    "Ты верификатор фактической корректности. Проверь ответ другой модели "
    "на галлюцинации, выдуманные цитаты, неверные даты и числа. "
    "Если всё фактически корректно — ответь одним словом VERIFIED. "
    "Если есть проблемы — кратко перечисли (1-3 пункта) что именно неточно. "
    "Если ответ полностью противоречит фактам — начни с CONTRADICTED:."
)


def verify_ask(
    messages: list,
    *,
    primary_task: str | None = None,
    primary_complexity: str = "medium",
    verifier_task: str = "factcheck",
    verifier_complexity: str = "medium",
    sensitivity: str = "low",
    user_api_key: str | None = None,
    extra: dict | None = None,
) -> RouteResult:
    """Первичный ответ + верификация другой моделью.

    Use case: realtime (Grok хорошо читает X но галлюцинирует цитаты на 94%)
    — пара с Perplexity для проверки. Любая creative_writing где важна
    фактическая точность — verify через factcheck.

    Args:
        messages: исходный диалог
        primary_task: task для primary (если None — auto-classify)
        primary_complexity: complexity для primary
        verifier_task: task для верификатора (def "factcheck" → Perplexity)
        verifier_complexity: complexity верификатора
        sensitivity, user_api_key, extra: проброс в ask()

    Returns:
        RouteResult primary call'а. Дополнительно в raw:
            verification: {
                verdict: "verified" | "issues_found" | "contradicted",
                verifier_model: str,
                verifier_content: str,
            }
            total_cost_kop: primary + verifier
            verify_error: str — если верификация упала (primary всё равно вернётся)

    Note: автоматического retry при issues_found НЕТ — caller решает сам
    что делать (показать предупреждение, спросить юзера, переключить модель).
    """
    primary = ask(
        messages, task=primary_task, complexity=primary_complexity,
        sensitivity=sensitivity, extra=extra, user_api_key=user_api_key,
    )
    total_cost = _extract_cost_kop(primary.raw)

    primary_content = primary.content or ""
    initial_query = _extract_initial_query(messages)
    verifier_messages = [
        {"role": "system", "content": _VERIFY_SYSTEM},
        {"role": "user", "content": (
            f"Исходный запрос юзера:\n{initial_query}\n\n"
            f"Ответ от модели ({primary.model_used}):\n{primary_content}\n\n"
            f"Проверь."
        )},
    ]

    try:
        verifier = ask(
            verifier_messages, task=verifier_task, complexity=verifier_complexity,
            sensitivity=sensitivity, extra=extra, user_api_key=user_api_key,
        )
    except Exception as e:
        log.warning(f"[verify] verifier failed: {type(e).__name__}: {e}")
        primary.raw["verify_error"] = f"{type(e).__name__}: {e}"
        primary.raw["total_cost_kop"] = total_cost
        return primary

    total_cost += _extract_cost_kop(verifier.raw)
    vcontent = (verifier.content or "").strip()
    vupper = vcontent.upper()

    if vupper.startswith("CONTRADICTED"):
        verdict = "contradicted"
    elif "VERIFIED" in vupper and len(vcontent) < 60:
        # Короткий ответ с VERIFIED — однозначно ok. Длинный — модель добавила
        # комментарий → скорее всего issues_found.
        verdict = "verified"
    else:
        verdict = "issues_found"

    primary.raw["verification"] = {
        "verdict": verdict,
        "verifier_model": verifier.model_used,
        "verifier_content": vcontent,
    }
    primary.raw["total_cost_kop"] = total_cost
    return primary
