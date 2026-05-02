"""
Multi-agent оркестрация для бизнес-решений (Solutions).

Концепция:
  Solution.orchestra_json содержит JSON-граф из stage'ов. Stage — это атомарная
  работа: web_search / browse_url / llm / parallel_llm / synthesize. Stage'и
  выполняются последовательно (по порядку в массиве); внутри parallel_llm —
  N веток параллельно через asyncio.gather.

  Промпт-шаблоны ссылаются на предыдущие stage'и через placeholder'ы:
    {{input}}                — исходный пользовательский ввод
    {{<stage_id>.output}}    — текстовый output stage'а (для llm/synthesize/web_search/browse_url)
    {{<stage_id>.outputs}}   — массив текстов parallel_llm-ветки, склеенный через "\\n\\n---\\n\\n"
    {{<stage_id>.outputs[i]}} — конкретная ветка i (0-based)

  По мере выполнения SolutionRun.stages_state обновляется (JSON dump),
  и broadcast'ится по SSE-каналу для UI live-progress.

Биллинг:
  Каждый llm-stage списывается по реальным токенам × margin_pct (5x как
  ai.improve_margin_pct). Web-search/browse_url не списываются (бесплатные
  для нас вызовы httpx). Если у юзера не хватает баланса — оркестрация
  останавливается с error="Insufficient balance".

Стриминг:
  Подписчики (SSE-stream / WebSocket) регистрируются через subscribe_run(run_id).
  notify_run отправляет им snapshot stages_state. Подписчики хранятся в RAM —
  при рестарте сервера фронт переоткроет SSE и получит свежий snapshot из БД.
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any

from server.db import db_session
from server.models import Solution, SolutionRun, Transaction
from server.billing import deduct_strict
from server.pricing import get_price

log = logging.getLogger("orchestra")

# ── Шаблонизация ────────────────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def _resolve_placeholder(expr: str, ctx: dict) -> str:
    """Резолвит выражение типа `input` / `stage_id.output` / `stage_id.outputs[2]`.
    Возвращает текст или пустую строку при ошибке."""
    expr = expr.strip()
    if expr == "input":
        return str(ctx.get("input", "") or "")
    # stage_id.outputs[i]
    m = re.match(r"^([a-zA-Z0-9_]+)\.outputs\[(\d+)\]$", expr)
    if m:
        sid, idx = m.group(1), int(m.group(2))
        st = ctx.get("stages", {}).get(sid, {})
        outputs = st.get("outputs") or []
        if 0 <= idx < len(outputs):
            return str(outputs[idx] or "")
        return ""
    # stage_id.outputs (склейка всех веток)
    m = re.match(r"^([a-zA-Z0-9_]+)\.outputs$", expr)
    if m:
        sid = m.group(1)
        st = ctx.get("stages", {}).get(sid, {})
        outputs = st.get("outputs") or []
        return "\n\n---\n\n".join(str(o or "") for o in outputs)
    # stage_id.output
    m = re.match(r"^([a-zA-Z0-9_]+)\.output$", expr)
    if m:
        sid = m.group(1)
        return str(ctx.get("stages", {}).get(sid, {}).get("output", "") or "")
    log.warning(f"[orchestra] unknown placeholder: {expr!r}")
    return ""


def _render_template(tpl: str, ctx: dict) -> str:
    if not tpl:
        return ""
    return _PLACEHOLDER_RE.sub(lambda m: _resolve_placeholder(m.group(1), ctx), tpl)


# ── Подписчики на прогресс (in-memory, per-process) ─────────────────────────

_subscribers: dict[int, list] = {}


def subscribe_run(run_id: int) -> asyncio.Queue:
    """Подписка на live-обновления выполнения run'а. Возвращает asyncio.Queue,
    в которую кладутся словари {stages_state, status, ...}. Caller должен
    `unsubscribe_run` при отключении."""
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _subscribers.setdefault(run_id, []).append(q)
    return q


def unsubscribe_run(run_id: int, q: asyncio.Queue) -> None:
    arr = _subscribers.get(run_id, [])
    if q in arr:
        arr.remove(q)
    if not arr and run_id in _subscribers:
        _subscribers.pop(run_id, None)


def _notify_run(run_id: int, snapshot: dict) -> None:
    """Кидает snapshot всем подписчикам run'а. Очередь bounded — если
    подписчик не успевает читать, сообщение теряется (не блокируем оркестрацию)."""
    for q in _subscribers.get(run_id, []):
        try:
            q.put_nowait(snapshot)
        except asyncio.QueueFull:
            pass


# ── Стейт стейджей ───────────────────────────────────────────────────────────


def _empty_stage_state(stage: dict) -> dict:
    return {
        "id": stage["id"],
        "label": stage.get("label", stage["id"]),
        "type": stage.get("type", "llm"),
        "status": "pending",  # pending | running | done | error | skipped
        "output": None,
        "outputs": None,
        "cost_kop": 0,
        "error": None,
        "started_at": None,
        "finished_at": None,
    }


def _build_initial_state(orchestra: dict, user_input: str,
                          attachments: list | None = None) -> dict:
    stages = orchestra.get("stages", []) or []
    return {
        "input": user_input or "",
        "attachments": attachments or [],
        "stages": {st["id"]: _empty_stage_state(st) for st in stages},
        "status": "running",
        "final_output": None,
        "started_at": datetime.utcnow().isoformat(),
        "finished_at": None,
    }


def _persist(run_id: int, ctx: dict) -> None:
    """Записывает текущее состояние в БД + рассылает подписчикам."""
    try:
        with db_session() as db:
            run = db.query(SolutionRun).filter_by(id=run_id).first()
            if not run:
                return
            run.stages_state = json.dumps({
                "stages": ctx.get("stages", {}),
                "status": ctx.get("status"),
                "started_at": ctx.get("started_at"),
                "finished_at": ctx.get("finished_at"),
            }, ensure_ascii=False, separators=(",", ":"))
            run.status = ctx.get("status", "running")
            db.commit()
    except Exception as e:
        log.warning(f"[orchestra] persist failed: {type(e).__name__}: {e}")
    _notify_run(run_id, {
        "stages": ctx.get("stages", {}),
        "status": ctx.get("status"),
    })


# ── Атомарные исполнители stage'ей ──────────────────────────────────────────


async def _run_web_search(stage: dict, ctx: dict) -> str:
    from server.agent_runner import tool_web_search
    query = _render_template(stage.get("query", "{{input}}"), ctx)
    num = int(stage.get("num_results", 5))
    text = await tool_web_search(
        {"query": query[:300], "num_results": max(1, min(10, num))},
        context={},
    )
    return text or ""


async def _run_browse_url(stage: dict, ctx: dict) -> str:
    from server.agent_runner import tool_browse_url
    url = _render_template(stage.get("url", ""), ctx)
    if not url.strip():
        return ""
    text = await tool_browse_url({"url": url.strip()}, context={})
    return text or ""


# ── Расширенные stage-типы для глубокого анализа ────────────────────────────


async def _run_file_extract(stage: dict, ctx: dict) -> str:
    """Извлекает текст из загруженного юзером файла. Параметр `attachment`
    выбирает, какой attachment взять:
      - "first"   — первый из списка attachments (default)
      - индекс N  — по индексу
      - "kind:doc" — первый attachment с указанным kind ('doc'|'image'|...)

    Использует server.knowledge.extract_text — поддерживает PDF/DOCX/XLSX/CSV/TXT.
    Возвращает обрезанный до 50000 символов текст.
    """
    from server.knowledge import extract_text
    attachments: list[dict] = ctx.get("attachments", []) or []
    selector = stage.get("attachment", "first")
    target: dict | None = None
    if selector == "first" and attachments:
        target = attachments[0]
    elif isinstance(selector, int) and 0 <= selector < len(attachments):
        target = attachments[selector]
    elif isinstance(selector, str) and selector.startswith("kind:"):
        want = selector.split(":", 1)[1]
        target = next((a for a in attachments if a.get("kind") == want), None)
    if not target:
        return "[нет загруженного файла]"
    rel_path = target.get("file_url") or target.get("path") or ""
    if not rel_path:
        return "[нет пути к файлу]"
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, lambda: extract_text(rel_path))
    if not text:
        return f"[не удалось извлечь текст из {target.get('name', rel_path)}]"
    return text[:50_000]


async def _run_vision_describe(stage: dict, ctx: dict) -> str:
    """Vision-описание загруженного изображения через Claude Haiku.
    Использует server.presentation_builder.describe_image_via_claude если
    доступно, иначе делает прямой call. Параметры:
      - attachment: какой attachment (kind:image / first / индекс)
      - hint: подсказка для модели (что именно описывать)
    """
    attachments: list[dict] = ctx.get("attachments", []) or []
    selector = stage.get("attachment", "kind:image")
    target: dict | None = None
    if isinstance(selector, str) and selector.startswith("kind:"):
        want = selector.split(":", 1)[1]
        target = next((a for a in attachments if a.get("kind") == want), None)
    elif selector == "first" and attachments:
        target = attachments[0]
    elif isinstance(selector, int) and 0 <= selector < len(attachments):
        target = attachments[selector]
    if not target:
        return "[нет изображения]"
    rel_path = target.get("file_url") or target.get("path") or ""
    if not rel_path:
        return "[нет пути к картинке]"
    hint = _render_template(stage.get("hint", "Опиши, что изображено."), ctx)
    try:
        from server.presentation_builder import describe_image_via_claude
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(
            None, lambda: describe_image_via_claude(rel_path, hint))
        return text or "[не удалось описать изображение]"
    except Exception as e:
        log.warning(f"[orchestra] vision_describe failed: {type(e).__name__}: {e}")
        return f"[ошибка vision: {type(e).__name__}]"


async def _run_extract_urls(stage: dict, ctx: dict) -> list[str]:
    """Из output указанного stage'а вытягивает HTTP-URL'ы (regex). Возвращает
    список строк (для дальнейшей подачи в parallel_browse)."""
    src = _render_template(stage.get("source", ""), ctx)
    if not src:
        return []
    urls = re.findall(r"https?://[^\s\)\]\>\"'`,]+", src)
    # dedup сохраняя порядок, ограничение
    seen, out = set(), []
    for u in urls:
        u = u.rstrip(".,;)")
        if u not in seen:
            seen.add(u)
            out.append(u)
    max_n = int(stage.get("max", 10))
    return out[:max_n]


async def _run_parallel_browse(stage: dict, ctx: dict) -> list[str]:
    """Параллельный browse_url для списка URL'ов.
    Источник URL'ов:
      - "urls": [...] — явный массив
      - "from": "<placeholder>" — ссылка на output stage'а с массивом URL
        (либо stage extract_urls, либо текстовый output из которого
        мы выдернем URL'ы regex'ом)
    """
    from server.agent_runner import tool_browse_url
    urls: list[str] = []
    if isinstance(stage.get("urls"), list):
        urls = [str(u) for u in stage["urls"] if u]
    elif "from" in stage:
        # Может быть либо результат extract_urls (list внутри stage state),
        # либо просто текст с URL'ами.
        from_expr = stage["from"].strip()
        # Попробуем взять как массив
        m = re.match(r"^\{\{\s*([a-zA-Z0-9_]+)\.outputs\s*\}\}$", from_expr)
        if m:
            sid = m.group(1)
            st = ctx.get("stages", {}).get(sid, {})
            if isinstance(st.get("outputs"), list):
                urls = [str(u) for u in st["outputs"]]
        if not urls:
            # Fallback: text → regex по URL
            text = _render_template(from_expr, ctx)
            urls = re.findall(r"https?://[^\s\)\]\>\"'`,]+", text or "")
    max_n = int(stage.get("max", 5))
    urls = urls[:max_n]
    if not urls:
        return []

    async def _one(url: str) -> str:
        try:
            text = await tool_browse_url({"url": url}, context={})
            # Сжимаем — для дальнейшего LLM-анализа
            head = f"=== {url} ===\n"
            return head + (text or "[пустая страница]")[:5000]
        except Exception as e:
            return f"=== {url} ===\n[ошибка: {type(e).__name__}]"

    results = await asyncio.gather(*[_one(u) for u in urls],
                                    return_exceptions=True)
    return [r if isinstance(r, str) else f"[err: {type(r).__name__}]"
            for r in results]


def _llm_call(model: str, system_prompt: str, user_prompt: str,
              max_tokens: int = 4000, user_api_key: str | None = None,
              temperature: float | None = None) -> dict:
    """Синхронный вызов модели. Возвращает {content, usage}."""
    from server.ai import generate_response
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})
    extra: dict = {"max_tokens": max_tokens}
    if temperature is not None:
        extra["temperature"] = temperature
    return generate_response(model, messages, extra, user_api_key=user_api_key)


def _calc_cost_kop(usage: dict | None, margin_pct: int) -> int:
    """Простая модель: real ≈ in*0.08 + out*0.30 коп за 1k токенов (Sonnet),
    margin берётся из ai.improve_margin_pct (default 500 = ×5).

    Возвращает 0 если нет токенов (web_search / пустой usage). Иначе минимум 1.
    """
    if not isinstance(usage, dict):
        return 0
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    if inp <= 0 and out <= 0:
        return 0
    real = inp / 1000 * 8 + out / 1000 * 30  # копейки
    return max(1, int(real * margin_pct / 100))


async def _run_llm(stage: dict, ctx: dict, default_model: str,
                    user_api_key: str | None) -> tuple[str, int]:
    """Запускает один LLM-вызов. Возвращает (text, cost_kop)."""
    model = stage.get("model", default_model)
    system_prompt = _render_template(stage.get("system_prompt", ""), ctx)
    user_prompt = _render_template(stage.get("user_prompt", ""), ctx)
    max_tokens = int(stage.get("max_tokens", 4000))
    temperature = stage.get("temperature")
    margin_pct = int(get_price("ai.improve_margin_pct", default=500))
    loop = asyncio.get_event_loop()
    answer = await loop.run_in_executor(
        None,
        lambda: _llm_call(model, system_prompt, user_prompt, max_tokens,
                           user_api_key, temperature),
    )
    if not isinstance(answer, dict):
        return str(answer or ""), 0
    text = answer.get("content", "") or ""
    cost = _calc_cost_kop(answer.get("usage"), margin_pct)
    return text, cost


async def _run_parallel_llm(stage: dict, ctx: dict, default_model: str,
                             user_api_key: str | None) -> tuple[list[str], int]:
    """N параллельных llm-вызовов. branches:
        - явный массив объектов {input, label?}
        - либо `from_lines` = "<placeholder>" → разбиваем по \\n
    Возвращает (outputs, total_cost_kop).
    """
    branches: list[dict] = []
    explicit = stage.get("branches")
    if isinstance(explicit, list):
        branches = explicit
    elif "from_lines" in stage:
        raw = _render_template(stage["from_lines"], ctx)
        for line in raw.split("\n"):
            line = line.strip()
            if line:
                branches.append({"input": line, "label": line[:80]})
    if not branches:
        return [], 0
    max_branches = int(stage.get("max_branches", 8))
    branches = branches[:max_branches]
    model = stage.get("model", default_model)
    system_prompt = _render_template(stage.get("system_prompt", ""), ctx)
    user_prompt_tpl = stage.get("user_prompt", "{{input}}")
    max_tokens = int(stage.get("max_tokens", 2500))
    temperature = stage.get("temperature")
    margin_pct = int(get_price("ai.improve_margin_pct", default=500))
    loop = asyncio.get_event_loop()

    async def _one(branch: dict) -> tuple[str, int]:
        sub_ctx = dict(ctx)
        sub_ctx["input"] = str(branch.get("input", ""))
        prompt = _render_template(user_prompt_tpl, sub_ctx)
        answer = await loop.run_in_executor(
            None,
            lambda: _llm_call(model, system_prompt, prompt, max_tokens,
                               user_api_key, temperature),
        )
        text = answer.get("content", "") if isinstance(answer, dict) else str(answer)
        cost = _calc_cost_kop(answer.get("usage") if isinstance(answer, dict) else None,
                               margin_pct)
        return text or "", cost

    results = await asyncio.gather(*[_one(b) for b in branches],
                                    return_exceptions=True)
    outputs: list[str] = []
    total_cost = 0
    for r in results:
        if isinstance(r, BaseException):
            outputs.append(f"[ошибка ветки: {type(r).__name__}]")
            continue
        text, cost = r
        outputs.append(text)
        total_cost += cost
    return outputs, total_cost


# ── Главный entry-point ──────────────────────────────────────────────────────


async def restage(run_id: int, stage_id: str,
                   extra_instruction: str | None = None) -> dict:
    """Перезапустить ОДИН конкретный stage в уже завершённом run'е и
    автоматически пересобрать финальный stage (если этот не финальный).

    Используется когда юзер недоволен отдельным разделом отчёта — например,
    хочет другой набор гарантий в КП или другое позиционирование в SWOT.
    Списание: real-токены × margin (так же, как обычный stage).

    Параметры:
        run_id          — id завершённого SolutionRun
        stage_id        — id stage'а который надо перегенерировать
        extra_instruction — опц. дополнительная инструкция от юзера
                           (добавляется в user_prompt текущего stage'а)
    """
    with db_session() as db:
        run = db.query(SolutionRun).filter_by(id=run_id).first()
        if not run:
            return {"status": "error", "error": "Run not found"}
        if run.status not in ("done", "error"):
            return {"status": "error",
                    "error": "Re-run возможен только для завершённого решения"}
        solution = db.query(Solution).filter_by(id=run.solution_id).first()
        if not solution or not solution.orchestra_json:
            return {"status": "error", "error": "No orchestra"}
        try:
            orchestra = json.loads(solution.orchestra_json)
            saved_state = json.loads(run.stages_state or "{}")
        except Exception as e:
            return {"status": "error", "error": f"Invalid state: {e}"}
        user_id = run.user_id
        user_input = run.user_input or ""
        attachments: list = []
        try:
            if run.attachments_json:
                attachments = json.loads(run.attachments_json) or []
        except Exception:
            attachments = []
        user_api_key = None
        try:
            from server.models import UserApiKey
            uk = db.query(UserApiKey).filter_by(user_id=user_id,
                                                  provider="anthropic").first()
            user_api_key = uk.api_key if uk else None
        except Exception:
            pass

    stages = orchestra.get("stages", []) or []
    target = next((s for s in stages if s["id"] == stage_id), None)
    if not target:
        return {"status": "error", "error": f"Stage {stage_id} not in orchestra"}
    final_id = orchestra.get("final_stage") or stages[-1]["id"]
    default_model = orchestra.get("default_model", "claude-sonnet")

    # Восстанавливаем контекст из сохранённого состояния
    ctx = {
        "input": user_input,
        "attachments": attachments,
        "stages": saved_state.get("stages", {}) or {},
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
    }
    # Сбрасываем target stage + все стадии после него (они зависели от него)
    seen_target = False
    for s in stages:
        if s["id"] == stage_id:
            seen_target = True
        if seen_target:
            ctx["stages"][s["id"]] = _empty_stage_state(s)
    run.status = "running"
    _persist(run_id, ctx)

    total_extra = 0

    # Список stages для пере-выполнения: [target, ... все после, до final включительно]
    to_run: list[dict] = []
    seen_target = False
    for s in stages:
        if s["id"] == stage_id:
            seen_target = True
        if seen_target:
            to_run.append(s)
    # final гарантированно в to_run если стоит после target. Если target ==
    # последний stage, to_run = [target] — этого достаточно.

    for stage in to_run:
        sid = stage["id"]
        st = ctx["stages"][sid]
        st["status"] = "running"
        st["started_at"] = datetime.utcnow().isoformat()
        _persist(run_id, ctx)

        try:
            # Если это target — добавляем extra_instruction к user_prompt
            stage_eff = dict(stage)
            if sid == stage_id and extra_instruction and extra_instruction.strip():
                base_up = stage_eff.get("user_prompt", "")
                stage_eff["user_prompt"] = (
                    base_up + "\n\n=== ДОПОЛНИТЕЛЬНАЯ ИНСТРУКЦИЯ ОТ ПОЛЬЗОВАТЕЛЯ ===\n"
                    + extra_instruction.strip()
                )

            stype = stage.get("type", "llm")
            cost = 0
            if stype == "web_search":
                text = await _run_web_search(stage_eff, ctx); st["output"] = text
            elif stype == "browse_url":
                text = await _run_browse_url(stage_eff, ctx); st["output"] = text
            elif stype in ("llm", "synthesize"):
                text, cost = await _run_llm(stage_eff, ctx, default_model, user_api_key)
                st["output"] = text
            elif stype == "parallel_llm":
                outputs, cost = await _run_parallel_llm(stage_eff, ctx, default_model,
                                                         user_api_key)
                st["outputs"] = outputs; st["output"] = "\n\n---\n\n".join(outputs)
            elif stype == "file_extract":
                st["output"] = await _run_file_extract(stage_eff, ctx)
            elif stype == "vision_describe":
                st["output"] = await _run_vision_describe(stage_eff, ctx)
            elif stype == "extract_urls":
                urls = await _run_extract_urls(stage_eff, ctx)
                st["outputs"] = urls; st["output"] = "\n".join(urls)
            elif stype == "parallel_browse":
                outputs = await _run_parallel_browse(stage_eff, ctx)
                st["outputs"] = outputs; st["output"] = "\n\n---\n\n".join(outputs)
            else:
                raise ValueError(f"Unknown stage type: {stype}")

            st["cost_kop"] = cost
            st["finished_at"] = datetime.utcnow().isoformat()
            st["status"] = "done"

            if cost > 0 and user_id:
                with db_session() as db:
                    if not deduct_strict(db, user_id, cost):
                        st["status"] = "error"
                        st["error"] = "Недостаточно средств"
                        ctx["status"] = "error"
                        _persist(run_id, ctx)
                        return {"status": "error", "error": "Insufficient balance",
                                "total_cost_kop": total_extra}
                    db.add(Transaction(
                        user_id=user_id, type="usage", tokens_delta=-cost,
                        description=f"{(solution.title or 'Решение')[:60]} · re-run · {stage.get('label', sid)[:60]}",
                        model=stage.get("model", default_model),
                    ))
                    db.commit()
                total_extra += cost
        except Exception as e:
            log.error(f"[restage] run={run_id} stage={sid} error: {type(e).__name__}: {e}")
            st["status"] = "error"
            st["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            st["finished_at"] = datetime.utcnow().isoformat()
            ctx["status"] = "error"
            _persist(run_id, ctx)
            return {"status": "error", "error": st["error"], "total_cost_kop": total_extra}
        _persist(run_id, ctx)

    # Обновляем final_output + регенерируем PDF
    final_text = ctx["stages"].get(final_id, {}).get("output") or ""
    pdf_url = None
    try:
        from server.pdf_builder import markdown_to_pdf
        import os, uuid
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        upload_dir = os.path.join(base, "uploads", "solutions")
        os.makedirs(upload_dir, exist_ok=True)
        fid = f"sol_{run_id}_{uuid.uuid4().hex[:8]}.pdf"
        out_path = os.path.join(upload_dir, fid)
        ok = markdown_to_pdf(md_text=final_text,
                              title=solution.title if solution else "Бизнес-решение",
                              out_path=out_path,
                              subtitle=solution.description if solution else "")
        if ok:
            pdf_url = f"/uploads/solutions/{fid}"
    except Exception as e:
        log.warning(f"[restage] PDF gen failed: {e}")

    ctx["status"] = "done"
    ctx["finished_at"] = datetime.utcnow().isoformat()
    with db_session() as db:
        run = db.query(SolutionRun).filter_by(id=run_id).first()
        if run:
            run.final_output = final_text
            if pdf_url:
                run.pdf_path = pdf_url
            run.total_cost_kop = (run.total_cost_kop or 0) + total_extra
            run.status = "done"
            run.stages_state = json.dumps({
                "stages": ctx["stages"], "status": "done",
                "started_at": ctx.get("started_at"),
                "finished_at": ctx["finished_at"],
            }, ensure_ascii=False)
            db.commit()
    _notify_run(run_id, {"stages": ctx["stages"], "status": "done",
                          "final_output": final_text, "pdf_url": pdf_url})
    return {"status": "done", "final_output": final_text, "pdf_url": pdf_url,
            "total_cost_kop": total_extra}


async def run_orchestra(run_id: int) -> dict:
    """Выполняет оркестрацию для SolutionRun. Обновляет SolutionRun.stages_state
    после каждого stage'а, в конце пишет final_output + pdf_path. Возвращает
    {status, final_output, total_cost_kop, error?}."""
    with db_session() as db:
        run = db.query(SolutionRun).filter_by(id=run_id).first()
        if not run:
            return {"status": "error", "error": "Run not found"}
        solution = db.query(Solution).filter_by(id=run.solution_id).first()
        if not solution or not solution.orchestra_json:
            return {"status": "error", "error": "No orchestra"}
        try:
            orchestra = json.loads(solution.orchestra_json)
        except Exception as e:
            return {"status": "error", "error": f"Invalid orchestra_json: {e}"}
        user_id = run.user_id
        user_input = run.user_input or ""
        # Attachments юзера (PDF/DOCX/картинки) — для file_extract / vision_describe
        attachments: list = []
        try:
            if run.attachments_json:
                attachments = json.loads(run.attachments_json) or []
        except Exception:
            attachments = []
        # Берём свой ключ юзера (Anthropic) — даём 80%-ю скидку
        user_api_key = None
        try:
            from server.models import UserApiKey
            uk = db.query(UserApiKey).filter_by(user_id=user_id,
                                                  provider="anthropic").first()
            user_api_key = uk.api_key if uk else None
        except Exception:
            pass

    default_model = orchestra.get("default_model", "claude-sonnet")
    final_stage_id = orchestra.get("final_stage")
    stages = orchestra.get("stages", []) or []
    if not stages:
        return {"status": "error", "error": "Orchestra has no stages"}

    ctx = _build_initial_state(orchestra, user_input, attachments)
    _persist(run_id, ctx)
    total_cost = 0

    for stage in stages:
        sid = stage["id"]
        st = ctx["stages"][sid]
        st["status"] = "running"
        st["started_at"] = datetime.utcnow().isoformat()
        _persist(run_id, ctx)

        try:
            t0 = time.monotonic()
            stype = stage.get("type", "llm")
            cost = 0
            if stype == "web_search":
                text = await _run_web_search(stage, ctx)
                st["output"] = text
            elif stype == "browse_url":
                text = await _run_browse_url(stage, ctx)
                st["output"] = text
            elif stype in ("llm", "synthesize"):
                text, cost = await _run_llm(stage, ctx, default_model, user_api_key)
                st["output"] = text
            elif stype == "parallel_llm":
                outputs, cost = await _run_parallel_llm(stage, ctx, default_model,
                                                          user_api_key)
                st["outputs"] = outputs
                # output = объединённый текст для удобства placeholder'а
                st["output"] = "\n\n---\n\n".join(outputs)
            elif stype == "file_extract":
                text = await _run_file_extract(stage, ctx)
                st["output"] = text
            elif stype == "vision_describe":
                text = await _run_vision_describe(stage, ctx)
                st["output"] = text
            elif stype == "extract_urls":
                urls = await _run_extract_urls(stage, ctx)
                st["outputs"] = urls
                st["output"] = "\n".join(urls)
            elif stype == "parallel_browse":
                outputs = await _run_parallel_browse(stage, ctx)
                st["outputs"] = outputs
                st["output"] = "\n\n---\n\n".join(outputs)
            else:
                raise ValueError(f"Unknown stage type: {stype}")

            st["cost_kop"] = cost
            st["finished_at"] = datetime.utcnow().isoformat()
            st["status"] = "done"
            elapsed = round(time.monotonic() - t0, 1)
            log.info(f"[orchestra] run={run_id} stage={sid} type={stype} ok in {elapsed}s cost={cost}коп")

            # Списываем стоимость stage'а (если есть). Если баланс кончился —
            # помечаем error и останавливаемся.
            if cost > 0 and user_id:
                with db_session() as db:
                    if not deduct_strict(db, user_id, cost):
                        st["status"] = "error"
                        st["error"] = "Недостаточно средств для продолжения"
                        ctx["status"] = "error"
                        _persist(run_id, ctx)
                        return {"status": "error",
                                "error": "Insufficient balance",
                                "total_cost_kop": total_cost}
                    db.add(Transaction(
                        user_id=user_id, type="usage", tokens_delta=-cost,
                        description=f"{(solution.title or 'Решение')[:80]} · {stage.get('label', sid)[:60]}",
                        model=stage.get("model", default_model),
                    ))
                    db.commit()
                total_cost += cost

        except Exception as e:
            log.error(f"[orchestra] run={run_id} stage={sid} error: {type(e).__name__}: {e}")
            st["status"] = "error"
            st["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            st["finished_at"] = datetime.utcnow().isoformat()
            ctx["status"] = "error"
            _persist(run_id, ctx)
            return {"status": "error", "error": st["error"],
                    "total_cost_kop": total_cost}

        _persist(run_id, ctx)

    # Финальный вывод — output финального stage'а (или последнего)
    final_id = final_stage_id or stages[-1]["id"]
    final_text = ctx["stages"].get(final_id, {}).get("output") or ""
    ctx["status"] = "done"
    ctx["finished_at"] = datetime.utcnow().isoformat()
    ctx["final_output"] = final_text

    # Сохраняем итог + пытаемся сделать PDF (как в legacy flow)
    pdf_url = None
    try:
        from server.pdf_builder import markdown_to_pdf
        import os, uuid
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        upload_dir = os.path.join(base, "uploads", "solutions")
        os.makedirs(upload_dir, exist_ok=True)
        fid = f"sol_{run_id}_{uuid.uuid4().hex[:8]}.pdf"
        out_path = os.path.join(upload_dir, fid)
        ok = markdown_to_pdf(
            md_text=final_text,
            title=(solution.title if solution else "Бизнес-решение"),
            out_path=out_path,
            subtitle=(solution.description if solution else ""),
        )
        if ok:
            pdf_url = f"/uploads/solutions/{fid}"
    except Exception as e:
        log.warning(f"[orchestra] PDF gen failed: {type(e).__name__}: {e}")

    with db_session() as db:
        run = db.query(SolutionRun).filter_by(id=run_id).first()
        if run:
            run.final_output = final_text
            run.pdf_path = pdf_url
            run.total_cost_kop = total_cost
            run.status = "done"
            run.stages_state = json.dumps({
                "stages": ctx["stages"],
                "status": "done",
                "started_at": ctx["started_at"],
                "finished_at": ctx["finished_at"],
            }, ensure_ascii=False)
            db.commit()
    _notify_run(run_id, {
        "stages": ctx["stages"],
        "status": "done",
        "final_output": final_text,
        "pdf_url": pdf_url,
    })

    log.info(f"[orchestra] run={run_id} DONE total_cost={total_cost}коп stages={len(stages)}")
    return {"status": "done", "final_output": final_text,
            "pdf_url": pdf_url, "total_cost_kop": total_cost}
