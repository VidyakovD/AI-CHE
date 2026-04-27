"""Site project endpoints — extracted from main.py."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
import json
import os
import logging

from server.routes.deps import get_db, current_user, optional_user
from server.models import SiteProject, User, Transaction, ChatBot
from server.ai import generate_response
from server.billing import deduct_strict

log = logging.getLogger(__name__)

router = APIRouter(tags=["sites"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Цены теперь динамические — берутся из БД через server.pricing.get_price().
# DEFAULTS остаются для:
#   - first-deploy seed (server.pricing.DEFAULTS)
#   - fallback если запрос к БД упал
# Менять цены живьём — через /admin/pricing (UI + API).
from server.pricing import get_price as _get_price

SPEC_CONVERSATION_CH_COST = 0    # бесплатное обсуждение ТЗ — заложено в фикс цену сайта


def _site_tier_config():
    """Свежий конфиг tier'ов с актуальными ценами из БД."""
    return {
        "standard": {
            "model": "claude",
            "max_tokens": 16000,
            "max_continues": 3,
            "cost": _get_price("site.standard", default=150_000),
            "label": "Стандарт (Sonnet)",
        },
        "premium": {
            "model": "claude-opus",
            "max_tokens": 16000,
            "max_continues": 6,
            "cost": _get_price("site.premium", default=199_000),
            "label": "Премиум (Opus)",
        },
    }


# Динамическое свойство-словарь для back-compat. Каждое обращение
# (`SITE_QUALITY_TIERS["standard"]`) читает свежий get_price.
class _DynamicTierMap(dict):
    def __getitem__(self, k):
        return _site_tier_config()[k]
    def get(self, k, default=None):
        cfg = _site_tier_config()
        return cfg.get(k, default)
    def items(self):
        return _site_tier_config().items()
    def values(self):
        return _site_tier_config().values()
    def keys(self):
        return _site_tier_config().keys()
    def __iter__(self):
        return iter(_site_tier_config())
    def __contains__(self, k):
        return k in _site_tier_config()


SITE_QUALITY_TIERS = _DynamicTierMap()

# Legacy aliases (где-то могут импортироваться из других модулей)
def _legacy_const(key, default):
    return _get_price(key, default=default)


# Используются только как fallback в редких местах
SITE_CREATE_FIX_COST    = 150_000  # legacy alias — get_price('site.standard') предпочтительнее
CODE_GEN_CH_COST        = 150_000  # legacy alias
CODE_GEN_PREMIUM_COST   = 199_000  # legacy alias
CODE_ITER_CH_COST       = 500      # legacy alias

_sites_host_base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "uploads", "sites")

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class CreateSiteProjectRequest(BaseModel):
    name: str
    creation_mode: str = "create_together"  # "have_spec" or "create_together"
    spec_text: str | None = None             # for "have_spec" mode


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
@router.get("/sites/templates")
def list_site_templates(db: Session = Depends(get_db)):
    """Deprecated -- empty for new flow."""
    return []


# ---------------------------------------------------------------------------
# Projects CRUD
# ---------------------------------------------------------------------------
@router.get("/sites/projects")
def list_sites(db: Session = Depends(get_db), user=Depends(optional_user)):
    if not user:
        return []
    projects = db.query(SiteProject).filter_by(user_id=user.id).order_by(SiteProject.updated_at.desc()).all()
    result = []
    for p in projects:
        result.append({
            "id": p.id, "name": p.name, "status": p.status,
            "price_tokens": p.price_tokens,
            "creation_mode": p.creation_mode or "create_together",
            "conversation_phase": p.conversation_phase or "idle",
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        })
    return result


@router.post("/sites/projects")
def create_site_project(req: CreateSiteProjectRequest, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    phase = "idle"
    status = "draft"
    chat_history = None
    spec_text = None

    if req.creation_mode == "have_spec" and req.spec_text:
        # User already has specs
        spec_text = req.spec_text
        phase = "collecting_images"
        status = "has_spec"
        price = 0  # обсуждение/анализ ТЗ — бесплатное (заложено в фикс цену)
    elif req.creation_mode == "create_together":
        phase = "gathering_spec"
        chat_history = json.dumps([])
        price = 0
    else:
        phase = "gathering_spec"
        chat_history = json.dumps([])
        price = 0

    p = SiteProject(
        user_id=user.id, name=req.name,
        creation_mode=req.creation_mode,
        conversation_phase=phase,
        chat_history=chat_history,
        spec_text=spec_text,
        price_tokens=price,
        status=status,
    )
    db.add(p); db.commit(); db.refresh(p)
    return {"id": p.id, "status": p.status, "phase": p.conversation_phase}


@router.get("/sites/projects/{project_id}")
def get_site_project(project_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    return {
        "id": p.id, "name": p.name, "status": p.status,
        "spec_text": p.spec_text, "code_html": p.code_html,
        "price_tokens": p.price_tokens,
        "creation_mode": p.creation_mode,
        "conversation_phase": p.conversation_phase,
        "chat_history": p.chat_history,
        "image_paths": p.image_paths,
        "hosted_path": p.hosted_path,
        "attached_bot_id": p.attached_bot_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.delete("/sites/projects/{project_id}")
def delete_site_project(project_id: int, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    # Remove hosted files if any
    if p.hosted_path:
        import shutil
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "uploads", "sites", str(project_id))
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
    db.delete(p); db.commit()
    return {"status": "deleted"}


@router.post("/sites/projects/{project_id}/rename")
def rename_site_project(project_id: int, body: dict, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    p.name = body.get("name", p.name)
    db.commit()
    return {"status": "ok", "name": p.name}


# ---------------------------------------------------------------------------
# Spec-building chat
# ---------------------------------------------------------------------------
@router.post("/sites/projects/{project_id}/chat")
def site_project_chat(project_id: int, body: dict, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    """Conversational spec builder -- user talks to Claude to build the ТЗ."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if p.conversation_phase not in ("gathering_spec", "spec_ready", "collecting_images"):
        raise HTTPException(400, "Неверная фаза проекта")
    # Reset phase when user goes back to chat from spec_ready
    if p.conversation_phase == "spec_ready":
        p.conversation_phase = "gathering_spec"

    user_message = body.get("message", "").strip()
    if not user_message:
        raise HTTPException(400, "Пустое сообщение")

    # Обсуждение ТЗ бесплатно — стоимость заложена в фикс-цену генерации сайта.
    # Если в будущем нужно ограничивать злоупотребления — добавить rate-limit
    # или мин. баланс 1500 ₽ (стоимость самой генерации).
    cost = SPEC_CONVERSATION_CH_COST  # = 0 в новой модели
    if cost > 0:
        if not deduct_strict(db, user.id, cost):
            raise HTTPException(402, "Недостаточно средств")
        p.price_tokens += cost
        db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                           description="Чат по ТЗ сайта", model="claude"))

    # Load chat history
    try:
        history = json.loads(p.chat_history) if p.chat_history else []
    except Exception:
        history = []
    history.append({"role": "user", "content": user_message})

    # Build system prompt
    system = (
        "Ты -- профессиональный веб-аналитик. Твоя задача -- помочь пользователю создать "
        "подробное техническое задание (ТЗ) для создания сайта. Задавай вопросы по одному, "
        "узнавай тип сайта (лендинг, магазин, блог, портфолио, сервис), тематику, целевую аудиторию, "
        "желаемые разделы, цветовые предпочтения, функционал. "
        "Когда информации достаточно -- собери всё в единое структурированное ТЗ и отправь его "
        "одним сообщением, начиная со слова 'ТЗ ГОТОВО' чтобы система могла это распознать. "
        "Отвечай на русском языке. Будь лаконичен и дружелюбен."
    )
    if p.spec_text:
        system += f"\n\nТекущее ТЗ: {p.spec_text[:500]} (можешь предложить улучшения)"

    messages = [{"role": "system", "content": system}] + history[-20:]
    # ТЗ собираем через GPT-4o — быстрее (≈3 сек vs Claude 30+) и дешевле,
    # для короткого диалога вопрос-ответ это оптимальная модель.
    answer = generate_response("gpt-4o", messages)
    ai_text = answer.get("content", "") if isinstance(answer, dict) else ""
    history.append({"role": "assistant", "content": ai_text})
    p.chat_history = json.dumps(history, ensure_ascii=False)

    # Detect if spec is ready
    if "ТЗ ГОТОВО" in ai_text or "тз готово" in ai_text.lower():
        # Extract the spec content -- remove the trigger phrase
        spec_content = ai_text.replace("ТЗ ГОТОВО", "").strip()
        if not spec_content:
            spec_content = ai_text
        p.spec_text = spec_content
        p.conversation_phase = "spec_ready"
        p.status = "has_spec"
    db.commit()

    return {"response": ai_text, "phase": p.conversation_phase, "spec_text": p.spec_text}


@router.post("/sites/projects/{project_id}/approve-spec")
def site_project_approve_spec(project_id: int, db: Session = Depends(get_db),
                               user: User = Depends(current_user)):
    """User approves the spec, moves to image collection."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    p.conversation_phase = "collecting_images"
    p.status = "has_spec"
    if not p.image_paths:
        p.image_paths = json.dumps([])
    db.commit()
    return {"status": "ok", "phase": p.conversation_phase}


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------
@router.post("/sites/projects/{project_id}/upload-image")
def site_project_upload_image(project_id: int, db: Session = Depends(get_db),
                               user: User = Depends(current_user)):
    """Get upload URL for images/logos for this project."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    # Return a signed upload URL -- frontend will POST to /upload
    return {"upload_endpoint": "/upload", "project_id": project_id}


@router.post("/sites/projects/{project_id}/attach-image")
def site_project_attach_image(project_id: int, body: dict, db: Session = Depends(get_db),
                               user: User = Depends(current_user)):
    """Attach an uploaded image path to the project."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    try:
        imgs = json.loads(p.image_paths) if p.image_paths else []
    except Exception:
        imgs = []
    imgs.append(body["file_url"])
    p.image_paths = json.dumps(imgs)
    db.commit()
    return {"status": "ok"}


class AttachBotBody(BaseModel):
    bot_id: int | None = None  # None — отвязать бота от сайта


@router.post("/sites/projects/{project_id}/attach-bot")
def site_project_attach_bot(project_id: int, body: AttachBotBody,
                            db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    """Привязать чат-бот юзера к сайту. При генерации/публикации виджет
    бота вставится в HTML автоматически."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if body.bot_id is None:
        p.attached_bot_id = None
        db.commit()
        return {"status": "detached"}
    bot = db.query(ChatBot).filter_by(id=body.bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден или не ваш")
    if not bot.widget_enabled:
        raise HTTPException(400, "У бота не включён виджет — включите в /chatbots.html")
    p.attached_bot_id = bot.id
    db.commit()
    return {"status": "attached", "bot_id": bot.id, "bot_name": bot.name}


def _inject_chatbot_widget(html: str, bot_id: int, app_url: str) -> str:
    """Вставляет <script src='/widget/{bot_id}.js'></script> перед последним </body>.

    Старая версия делала html.replace("</body>", ..., count=1) — это вставляло
    скрипт перед ПЕРВЫМ </body>. Если в HTML несколько </body> (битый AI-вывод
    с auto-continue, или iframe внутри) — скрипт мог попасть не туда. Также
    если </body> вообще нет — пристёгивали в конец без проверки на </html>.

    Сейчас: ищем последнее вхождение </body> case-insensitive; если нет —
    вставляем перед </html>; если и его нет — добавляем оба тега.
    """
    widget_tag = f'<script src="{app_url}/widget/{bot_id}.js" async></script>\n'
    lower = html.lower()
    body_idx = lower.rfind("</body>")
    if body_idx >= 0:
        return html[:body_idx] + widget_tag + html[body_idx:]
    html_idx = lower.rfind("</html>")
    if html_idx >= 0:
        return html[:html_idx] + widget_tag + "</body>\n" + html[html_idx:]
    return html + "\n" + widget_tag + "</body></html>\n"


# ---------------------------------------------------------------------------
# Code generation: фоновая задача + polling (вместо синхронного long-running)
# ---------------------------------------------------------------------------
# Раньше: /generate-code блокирует HTTP-запрос на 1-3 минуты, клиент таймаутит,
# юзер видит «Ошибка генерации». Сейчас: эндпоинт сразу возвращает {status:running},
# фоновая asyncio-задача пишет result в БД, фронт polling'ит /generation-status.

import asyncio


def _build_site_prompt(spec_text: str, image_paths_json: str | None) -> tuple[str, list[str]]:
    """Строит финальный prompt для Claude по ТЗ + картинкам пользователя."""
    img_context = ""
    full_urls: list[str] = []
    try:
        imgs = json.loads(image_paths_json) if image_paths_json else []
        if imgs:
            base_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
            full_urls = [f"{base_url}{u}" if u.startswith("/") else u for u in imgs]
            lines = "\n".join(f"  {i+1}. {u}" for i, u in enumerate(full_urls))
            img_context = (
                f"\n\nЗАГРУЖЕННЫЕ ИЗОБРАЖЕНИЯ ПОЛЬЗОВАТЕЛЯ (используй ИХ, не placeholder'ы):\n{lines}\n"
                f"Обязательно вставь эти URL в <img src=\"...\"> в подходящих местах сайта. "
                f"Не придумывай несуществующие картинки."
            )
    except Exception:
        pass

    prompt = (
        f"Ты — опытный веб-разработчик. Создай полный HTML-код одностраничного сайта.\n\n"
        f"⚠️ ВАЖНО: строго следуй ТЗ ниже. Тематика, отрасль, продукт, целевая аудитория, "
        f"названия блоков и разделов — берутся ТОЛЬКО из ТЗ. Не придумывай шаблонные тексты "
        f"про рестораны, кофейни, меню если этого нет в ТЗ. Все заголовки, тексты, призывы — "
        f"строго по теме ТЗ.\n\n"
        f"=== ТЗ ===\n{spec_text}\n=== КОНЕЦ ТЗ ===\n"
        f"{img_context}\n"
        f"Технические требования:\n"
        f"- Чистый, современный адаптивный HTML+CSS (mobile-first)\n"
        f"- Без внешних фреймворков (только inline CSS или <style>)\n"
        f"- Семантичная разметка, доступность (alt у картинок, aria-label у иконок)\n"
        f"- Красивый современный дизайн под тематику ТЗ\n"
        f"- Тексты — строго по теме из ТЗ, на русском, без английского lorem ipsum\n"
        f"- Картинки: используй ТОЛЬКО URL из списка выше (если он есть). Иначе — CSS-плейсхолдеры\n"
        f"- Ответ: ТОЛЬКО HTML-код, без markdown-обёрток и объяснений\n"
    )
    return prompt, full_urls


def _enhance_spec_with_gpt(spec_text: str, premium: bool = False) -> str:
    """Pre-process сырого ТЗ через GPT-4o (mini для standard, основной для premium).

    Превращает «хочу сайт для барбершопа» → детальный бриф со структурой,
    цветами, текстами hero, CTA, social proof и техническими требованиями.
    Claude по такому брифу выдаёт сайт уровня агентства, а не «AI-шаблон».

    Premium tier использует gpt-4o (а не mini) — на 2-3 ₽ дороже но
    качество ТЗ заметно выше.

    Если GPT недоступен / упал — возвращаем исходный текст (не блокируем).
    """
    model = "gpt-4o" if premium else "gpt-4o-mini"
    enhance_prompt = f"""Ты — арт-директор и UX-копирайтер с 10-летним опытом
премиум landing-page'ей. Тебе дали СЫРОЕ ТЗ от клиента (часто 1-2 предложения).
Задача — превратить его в детальный технический бриф для верстальщика-Claude,
который сделает сайт уровня агентства Awwwards. НЕ верстай сам — это бриф.

Используй формат с разделами в markdown (## Заголовок). Включи ВСЕ блоки ниже:

## Бизнес-контекст
- Ниша, целевая аудитория (демография + боли + желания)
- Основной CTA (что клиент должен сделать на сайте)
- Уникальное торговое предложение в одной фразе

## Тон и стиль
- Эмоциональный регистр (премиум / дружелюбный / экспертный / молодёжный)
- 2-3 референса по стилю (например «как Linear.app», «как Apple iPhone page»)
- Тип шрифтов (sans-serif geometric / humanist / serif / display)

## Цветовая палитра
- 4-6 HEX-кодов с ролью каждого: primary, secondary, accent, neutral-dark, neutral-light, success/warn опционально
- Принцип контраста (WCAG AA минимум)
- Примеры применения: фон героя, акцент кнопок, hover-state

## Структура страницы (по порядку секций)
Для КАЖДОЙ секции дай:
- Название секции и её цель
- Конкретный заголовок (не плейсхолдер!)
- Подзаголовок / краткое описание
- 2-5 пунктов / карточек / преимуществ — С РЕАЛЬНЫМ ТЕКСТОМ
- Тип CTA-кнопки (если есть) с текстом

Обязательные секции (выбери релевантные нише):
1. Sticky-header с навигацией + основной CTA
2. Hero (полноэкранный, с конкретным заголовком, подзаголовком и 1-2 CTA)
3. Доверие/social proof (логотипы клиентов / цифры / отзывы / награды)
4. Преимущества или «как мы работаем» (3-6 пунктов)
5. Услуги/продукты (карточки с ценами или диапазоном цен)
6. Кейсы / результаты клиентов (с цифрами)
7. Отзывы (с фото, именами, должностями — придумай реалистичные)
8. FAQ (5-7 вопросов и развёрнутые ответы)
9. CTA-секция перед footer
10. Footer с контактами, соц-сетями, мини-навигацией

## UX-фишки
- Какие микро-анимации использовать (fade-in on scroll, hover-lift cards, smooth-scroll к якорям, parallax mild)
- Sticky-элементы (header, side-CTA, scroll-progress bar)
- Mobile UX (hamburger menu, swipeable testimonials, click-to-call)

## Технические требования
- Mobile-first responsive (320px+, 768px+, 1280px+)
- Семантический HTML5 (header, nav, main, section, article, footer)
- Accessibility: alt на картинки, aria-label на icon-buttons, контрастность
- Производительность: inline critical CSS, lazy-load картинок (loading="lazy")
- Без внешних JS-фреймворков (только vanilla JS если нужен)
- Tailwind CDN допустим, кастомный CSS в <style> в head

## Картинки и иконки
- Если в ТЗ упомянуты загруженные картинки клиента — используй их URL'ы
- Иначе — для hero/about используй unsplash.com или placehold.co с подписью что менять
- Иконки — inline SVG (Heroicons / Lucide / Phosphor стиль)

СТРОГО:
- Не меняй нишу/тематику из исходного ТЗ
- Все тексты должны быть на русском (если ТЗ на русском)
- Конкретика, не «пример пункта» — реалистичные тексты
- Длина итогового брифа: 1500-3500 слов
- Только сам бриф, без преамбул/комментариев

=== ИСХОДНОЕ ТЗ ОТ КЛИЕНТА ===
{spec_text}
=== КОНЕЦ ===

Выдай детальный бриф:"""
    try:
        from server.ai import generate_response as _gen
        ans = _gen(model, [{"role": "user", "content": enhance_prompt}],
                   extra={"max_tokens": 4000})
        text = ans.get("content", "") if isinstance(ans, dict) else ""
        if text and len(text.strip()) > 400:
            return text.strip()
    except Exception as e:
        log.warning(f"[Sites] enhance_spec failed (non-fatal): {e}")
    return spec_text


def _strip_markdown_code_fence(content: str) -> str:
    """Убирает ```html ... ``` обёртки если Claude всё-таки добавил."""
    for marker in ["```html\n", "```\n", "```html", "```"]:
        if content.startswith(marker):
            content = content[len(marker):]
            content = content.rsplit("```", 1)[0] if "```" in content else content
            break
    return content


def _refund_site_generation(project_id: int, reason: str) -> None:
    """
    Возвращает деньги пользователю если фоновая генерация сайта упала.
    Идемпотентно: проверяет SiteProject.gen_status — рефанд только если статус
    ещё не "refunded" (защита от двойного refund при повторных вызовах).
    """
    from server.db import db_session
    from server.billing import credit_atomic
    try:
        with db_session() as db:
            p = db.query(SiteProject).filter_by(id=project_id).first()
            if not p or not p.user_id:
                return
            # Идемпотентность: помечаем gen_status="refunded" перед credit.
            # Если параллельный вызов уже сделал refund — пропускаем.
            if p.gen_status == "refunded":
                return
            cost = int(p.price_tokens or 0)
            if cost <= 0:
                return
            # Атомарный credit с одновременным сбросом price_tokens
            credit_atomic(db, p.user_id, cost)
            db.add(Transaction(
                user_id=p.user_id, type="refund", tokens_delta=cost,
                description=f"Авто-возврат за неудачную генерацию сайта #{project_id}: {reason[:200]}",
            ))
            p.price_tokens = 0
            p.gen_status = "refunded"
            p.gen_error = (p.gen_error or "") + f" [возврат {cost/100:.0f} ₽]"
            db.commit()
            log.warning(f"[Sites/bg] project {project_id} refunded {cost} kop ({reason})")
            from server.audit_log import log_action
            log_action("site.generate_failed", user_id=p.user_id, target_type="site_project",
                       target_id=project_id, level="warn", success=False,
                       details={"refunded_kop": cost, "reason": reason[:500]})
    except Exception as ex:
        log.error(f"[Sites/bg] refund failed for {project_id}: {type(ex).__name__}: {ex}")


async def _run_site_generation(project_id: int, quality: str = "standard"):
    """Фоновая задача — генерит HTML сайта.

    quality: standard | premium — определяет модель (Sonnet vs Opus),
    кол-во auto-continue turns и max_tokens. Конфиг в SITE_QUALITY_TIERS.

    Все исключения ловим и пишем в БД (gen_status=failed + gen_error),
    чтобы фронт показал нормальное сообщение, а не виртуальный 500.
    При любой ошибке деньги возвращаются автоматически (см. _refund_site_generation).
    """
    from server.db import db_session
    tier = SITE_QUALITY_TIERS.get(quality, SITE_QUALITY_TIERS["standard"])
    model_id = tier["model"]
    max_tokens = tier["max_tokens"]
    max_continues = tier["max_continues"]
    tier_label = tier["label"]
    log.info(f"[Sites/bg] project {project_id} starting tier={quality} model={model_id}")

    try:
        # 1. Загружаем проект
        with db_session() as db:
            p = db.query(SiteProject).filter_by(id=project_id).first()
            if not p:
                log.error(f"[Sites/bg] project {project_id} not found")
                return
            spec = p.spec_text or ""
            image_paths_json = p.image_paths
            p.gen_progress = f"[{tier_label}] Улучшаю ТЗ через GPT-4o…"
            db.commit()

        # 2. Pre-process ТЗ через GPT-4o-mini (отдельный thread — sync вызов)
        loop = asyncio.get_event_loop()
        # Premium tier — enhance через gpt-4o (не mini), бриф детальнее
        is_premium = quality == "premium"
        enhanced = await loop.run_in_executor(
            None, _enhance_spec_with_gpt, spec, is_premium
        )

        with db_session() as db:
            p = db.query(SiteProject).filter_by(id=project_id).first()
            if p:
                p.enhanced_spec = enhanced
                p.gen_progress = f"[{tier_label}] Генерация HTML (1/{max_continues+1})…"
                db.commit()

        # 3. Основная генерация Claude (Sonnet или Opus, timeout=600s в SDK)
        prompt, _full_urls = _build_site_prompt(enhanced, image_paths_json)
        ans = await loop.run_in_executor(
            None,
            lambda: generate_response(model_id,
                                       [{"role": "user", "content": prompt}],
                                       {"max_tokens": max_tokens}),
        )
        content = (ans.get("content", "") if isinstance(ans, dict) else "").strip()

        if not content.startswith("<") or "временно недоступен" in content:
            with db_session() as db:
                p = db.query(SiteProject).filter_by(id=project_id).first()
                if p:
                    p.gen_status = "failed"
                    p.gen_error = "AI не вернул корректный HTML. Деньги возвращены."
                    p.conversation_phase = "spec_approved"
                    db.commit()
            _refund_site_generation(project_id, "AI returned non-HTML")
            return

        content = _strip_markdown_code_fence(content)

        # 4. Auto-continue до </html>. Premium → больше попыток (Opus умеет
        # генерить длинно — не будем останавливать его раньше времени).
        # После каждого turn пишем промежуточный HTML в БД, чтобы юзер видел
        # «уже сгенерировано 47 KB…» и был уверен что работа идёт.
        for attempt in range(max_continues):
            if "</html>" in content.lower():
                break
            kb = len(content) // 1024
            with db_session() as db:
                p = db.query(SiteProject).filter_by(id=project_id).first()
                if p:
                    p.code_html = content  # промежуточное сохранение
                    p.gen_progress = f"[{tier_label}] Готово {kb} KB, дописываю ({attempt+2}/{max_continues+1})…"
                    db.commit()

            cont_messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": content},
                {"role": "user", "content": (
                    "Ты не закончил — ответ обрезался. Продолжи строго с того места "
                    "где остановился (не повторяй уже написанное), до закрывающего "
                    "</html>. Не меняй тематику, следуй ТЗ выше. Ответ — только "
                    "продолжение HTML, без markdown и объяснений."
                )},
            ]
            cont = await loop.run_in_executor(
                None,
                lambda: generate_response(model_id, cont_messages, {"max_tokens": max_tokens}),
            )
            cont_text = (cont.get("content", "") if isinstance(cont, dict) else "")
            cont_text = _strip_markdown_code_fence(cont_text)
            if not cont_text.strip():
                log.warning(f"[Sites/bg] project {project_id}: empty continuation, stop")
                break
            content += cont_text

        # 5. Гарантируем закрытие тегов
        if "</html>" not in content.lower():
            log.warning(f"[Sites/bg] project {project_id}: HTML не закрылся за {max_continues} turns, добавляю теги")
            if "</body>" not in content.lower():
                content += "\n</body>"
            content += "\n</html>"

        # 6. Сохраняем результат
        with db_session() as db:
            p = db.query(SiteProject).filter_by(id=project_id).first()
            if p:
                p.code_html = content
                p.conversation_phase = "done"
                p.status = "done"
                p.gen_status = "done"
                p.gen_progress = f"Готово! {len(content)//1024} KB"
                p.gen_error = None
                db.commit()
        log.info(f"[Sites/bg] project {project_id} done tier={quality} ({len(content)} symbols)")

    except Exception as e:
        log.error(f"[Sites/bg] project {project_id} failed: {e}", exc_info=True)
        try:
            with db_session() as db:
                p = db.query(SiteProject).filter_by(id=project_id).first()
                if p:
                    p.gen_status = "failed"
                    # Не пишем сам exception в текст — может содержать proxy URL
                    # с креденшалами или API-ключ. Тип достаточен для UX.
                    p.gen_error = f"Ошибка генерации ({type(e).__name__}). Деньги возвращены."
                    p.conversation_phase = "spec_approved"
                    db.commit()
        except Exception:
            pass
        # Авто-возврат списанной суммы (идемпотентен, не дублируется)
        _refund_site_generation(project_id, f"{type(e).__name__}: {e}"[:200])


@router.post("/sites/projects/{project_id}/generate-code")
async def site_project_generate_code(project_id: int, body: dict | None = None,
                                      db: Session = Depends(get_db),
                                      user=Depends(optional_user)):
    """Запускает фоновую генерацию HTML сайта.

    Body: { quality: "standard" | "premium" } — выбор модели.
    standard = Sonnet за 1500 ₽, premium = Opus за 1990 ₽.

    Возвращает сразу {status:'running'} — фронт polling'ит /generation-status
    раз в несколько секунд. Избавляет от 60-сек client-timeout'а.
    """
    if not user:
        raise HTTPException(401, "Нужна авторизация")
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.spec_text:
        raise HTTPException(400, "Сначала создайте ТЗ")
    if p.gen_status == "running":
        return {"status": "running", "progress": p.gen_progress or "Уже генерируется…"}

    # Выбор tier'а: standard (Sonnet 1500₽) или premium (Opus 1990₽)
    quality = ((body or {}).get("quality") or "standard").strip().lower()
    if quality not in SITE_QUALITY_TIERS:
        quality = "standard"
    tier = SITE_QUALITY_TIERS[quality]
    cost = tier["cost"]

    if not deduct_strict(db, user.id, cost):
        raise HTTPException(402, f"Недостаточно средств (нужно {cost/100:.0f} ₽)")
    p.price_tokens = (p.price_tokens or 0) + cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                       description=f"Создание сайта — {tier['label']} ({cost/100:.0f} ₽)"))

    # Помечаем как «running» сразу, чтобы фронт увидел при первом polling
    from datetime import datetime as _dt
    p.gen_status = "running"
    p.gen_started_at = _dt.utcnow()
    p.gen_progress = f"Запускаю генерацию ({tier['label']})…"
    p.gen_error = None
    p.conversation_phase = "generating_code"
    db.commit()

    # Запускаем фоновую задачу с выбранным tier'ом
    asyncio.create_task(_run_site_generation(project_id, quality=quality))

    from server.audit_log import log_action
    log_action("site.generate_start", user_id=user.id, target_type="site_project",
               target_id=project_id, details={"quality": quality, "cost_kop": cost})

    return {"status": "running", "progress": p.gen_progress, "quality": quality, "tier": tier["label"]}


@router.get("/sites/quality-tiers")
def get_site_quality_tiers():
    """Список tier'ов для UI-селектора. Без авторизации — публичный прайс."""
    return [{
        "id": k,
        "label": v["label"],
        "cost_kopecks": v["cost"],
        "cost_rub": v["cost"] / 100,
        "max_continues": v["max_continues"],
    } for k, v in SITE_QUALITY_TIERS.items()]


@router.get("/sites/projects/{project_id}/generation-status")
def site_project_generation_status(project_id: int,
                                    db: Session = Depends(get_db),
                                    user: User = Depends(current_user)):
    """Polling статуса генерации. Фронт зовёт раз в 3-5 сек.
    Возвращает {status, progress, error, code_html (когда done)}."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    out = {
        "status": p.gen_status or "idle",
        "progress": p.gen_progress or "",
        "error": p.gen_error or None,
        "phase": p.conversation_phase,
    }
    if p.gen_status == "done":
        out["code_html"] = p.code_html
    return out


@router.post("/sites/projects/{project_id}/repair-code")
def site_project_repair_code(project_id: int, db: Session = Depends(get_db),
                             user: User = Depends(current_user)):
    """Бесплатно дописывает обрезанный HTML до закрывающего </html>.
    Используется когда генерация прошла, но Claude не успел дописать."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.code_html:
        raise HTTPException(400, "Сначала сгенерируйте код")
    if "</html>" in p.code_html.lower():
        return {"status": "ok", "note": "уже закрыт", "code_html": p.code_html}

    content = p.code_html
    base_prompt = (
        f"ТЗ сайта:\n\n=== ТЗ ===\n{p.spec_text or '(нет)'}\n=== КОНЕЦ ТЗ ===\n\n"
        "Сгенерируй полный HTML по этому ТЗ."
    )
    for attempt in range(2):
        if "</html>" in content.lower():
            break
        cont_messages = [
            {"role": "user", "content": base_prompt},
            {"role": "assistant", "content": content},
            {"role": "user", "content": (
                "Ответ обрезался. Продолжи строго с того места, до </html>. "
                "Не меняй тематику, следуй ТЗ. Только HTML."
            )},
        ]
        cont = generate_response("claude", cont_messages, extra={"max_tokens": 16000})
        ctxt = cont.get("content", "") if isinstance(cont, dict) else ""
        for marker in ["```html\n", "```\n", "```html", "```"]:
            if ctxt.startswith(marker):
                ctxt = ctxt[len(marker):]
                ctxt = ctxt.rsplit("```", 1)[0] if "```" in ctxt else ctxt
                break
        if not ctxt.strip():
            break
        content += ctxt
    if "</html>" not in content.lower():
        if "</body>" not in content.lower():
            content += "\n</body>"
        content += "\n</html>"
    p.code_html = content
    db.commit()
    return {"status": "ok", "code_html": content}


@router.put("/sites/projects/{project_id}/save-code")
def site_project_save_code(project_id: int, body: dict, db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    """Save manually edited code back to the project."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    p.code_html = body.get("code_html", p.code_html)
    if p.conversation_phase not in ("done", "generating_code"):
        p.conversation_phase = "done"
        p.status = "done"
    db.commit()
    return {"status": "ok"}


@router.post("/sites/projects/{project_id}/iterate")
def site_project_iterate(project_id: int, body: dict, db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    """Iterate on generated code with user instructions."""
    if not user:
        raise HTTPException(401, "Нужна авторизация")
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.code_html:
        raise HTTPException(400, "Сначала сгенерируйте код")

    instructions = body.get("instructions", "").strip()
    if not instructions:
        raise HTTPException(400, "Пустая инструкция")

    cost = CODE_ITER_CH_COST  # 5 ₽ за правку
    if not deduct_strict(db, user.id, cost):
        raise HTTPException(402, f"Недостаточно средств (нужно {cost/100:.0f} ₽)")
    p.price_tokens += cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                       description=f"Правка сайта ({cost/100:.0f} ₽)"))

    prompt = (
        f"Вот текущий HTML сайта:\n\n{p.code_html}\n\n"
        f"Пользователь просит: {instructions}\n"
        f"Верни ТОЛЬКО обновлённый полный HTML-код целиком, от <!DOCTYPE до </html>. "
        f"Без markdown-обёрток, без объяснений, без сокращений."
    )

    answer = generate_response("claude", [{"role": "user", "content": prompt}],
                               extra={"max_tokens": 16000})
    content = answer.get("content", "") if isinstance(answer, dict) else ""

    # Guard: if AI returned an error message instead of HTML — don't overwrite
    if not content.strip().startswith("<") or "временно недоступен" in content:
        raise HTTPException(503, "AI не вернул корректный HTML. Попробуйте ещё раз.")

    # Clean markdown
    for marker in ["```html\n", "```\n", "```html", "```"]:
        if content.startswith(marker):
            content = content[len(marker):]
            content = content.rsplit("```", 1)[0] if "```" in content else content
            break

    # Auto-continue с полным контекстом (тз + уже написанное)
    for attempt in range(2):
        if "</html>" in content.lower():
            break
        cont_messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": content},
            {"role": "user", "content": (
                "Ответ обрезался. Продолжи строго с того места где остановился, "
                "до </html>. Не меняй тематику. Только HTML, без объяснений."
            )},
        ]
        cont = generate_response("claude", cont_messages, extra={"max_tokens": 16000})
        cont_text = cont.get("content", "") if isinstance(cont, dict) else ""
        for marker in ["```html\n", "```\n", "```html", "```"]:
            if cont_text.startswith(marker):
                cont_text = cont_text[len(marker):]
                cont_text = cont_text.rsplit("```", 1)[0] if "```" in cont_text else cont_text
                break
        if not cont_text.strip():
            break
        content += cont_text
    if "</html>" not in content.lower():
        if "</body>" not in content.lower():
            content += "\n</body>"
        content += "\n</html>"

    p.code_html = content
    db.commit()
    return {"code_html": content}


# ---------------------------------------------------------------------------
# Точечная AI-правка одного блока сайта (5 ₽ за правку, экономно vs 1500 ₽
# за полную регенерацию). Юзер выделяет блок в превью → пишет инструкцию
# → backend просит Claude переписать ТОЛЬКО этот блок → подменяем в code_html.
# ---------------------------------------------------------------------------

class EditBlockBody(BaseModel):
    block_html: str          # текущий HTML блока (от <section> до </section>)
    instruction: str         # что юзер хочет изменить
    block_id: str | None = None   # data-edit-id блока (для логов/повторной подмены на фронте)


# Лимит размера блока — защита от того, что юзер пришлёт весь сайт в block_html.
# 64 KB достаточно для самого жирного hero-блока с inline SVG.
_MAX_BLOCK_BYTES = 64 * 1024


@router.post("/sites/projects/{project_id}/edit-block")
def site_project_edit_block(project_id: int, body: EditBlockBody,
                            db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    """Перегенерирует ТОЛЬКО переданный блок HTML по инструкции (5 ₽).

    Возвращает обновлённый HTML блока. Подмену в code_html делает фронт
    (через MutationObserver или явный /save-code), backend не парсит DOM.
    """
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.code_html:
        raise HTTPException(400, "Сначала сгенерируйте код сайта")

    block = (body.block_html or "").strip()
    instr = (body.instruction or "").strip()
    if not block:
        raise HTTPException(400, "Пустой block_html")
    if not instr:
        raise HTTPException(400, "Пустая инструкция")
    if len(block.encode("utf-8")) > _MAX_BLOCK_BYTES:
        raise HTTPException(413, f"Блок слишком большой (макс. {_MAX_BLOCK_BYTES//1024} KB)")

    # Pre-check баланса (не списываем заранее — ждём реальные токены AI).
    # Анти-зацикливание: минимум 1 ₽ должен быть, иначе халява в случае 0-токенов.
    from server.billing import deduct_atomic, get_balance
    from server.pricing import get_price
    if get_balance(db, user.id) < 100:
        raise HTTPException(402, "Недостаточно средств для AI-правки (минимум 1 ₽)")

    prompt = (
        "Ты — веб-разработчик. Тебе передан ОДИН блок HTML с одностраничного "
        "сайта (например <section>, <header>, <div>). Юзер хочет внести "
        "точечное изменение.\n\n"
        f"=== ИНСТРУКЦИЯ ОТ ЮЗЕРА ===\n{instr}\n\n"
        f"=== ТЕКУЩИЙ HTML БЛОКА ===\n{block}\n=== КОНЕЦ ===\n\n"
        "Верни ТОЛЬКО обновлённый HTML этого блока, без обёрток ```html, "
        "без объяснений, без рекомендаций. Сохрани внешний контейнер "
        "(тот же тег, те же id/class), меняй только то, о чём попросил юзер. "
        "Если правка не нужна или непонятна — верни исходный блок без "
        "изменений. Не добавляй <html>/<body>/<head>."
    )
    try:
        answer = generate_response("claude", [{"role": "user", "content": prompt}],
                                   extra={"max_tokens": 8000})
    except Exception as e:
        raise HTTPException(503, "AI временно недоступен")

    new_html = answer.get("content", "") if isinstance(answer, dict) else str(answer)
    # Снимаем markdown-обёртки если AI ослушался
    for marker in ["```html\n", "```\n", "```html", "```"]:
        if new_html.startswith(marker):
            new_html = new_html[len(marker):]
            new_html = new_html.rsplit("```", 1)[0] if "```" in new_html else new_html
            break
    new_html = new_html.strip()

    # Sanity: блок должен начинаться с тега. Если AI вернул мусор — НЕ
    # списываем (ничего не сделали).
    if not new_html.startswith("<"):
        raise HTTPException(503, "AI вернул некорректный HTML, средства не списаны")

    # Списание: real × improve_margin (×5). Без фикс-минимума.
    real_kop = 0
    if isinstance(answer, dict):
        real_kop = int(answer.get("input_tokens", 0) / 1000 * 80
                     + answer.get("output_tokens", 0) / 1000 * 300)
    margin_pct = int(get_price("ai.improve_margin_pct", default=500))
    cost = max(1, int(real_kop * margin_pct / 100))
    charged = deduct_atomic(db, user.id, cost)
    p.price_tokens = (p.price_tokens or 0) + charged
    db.add(Transaction(
        user_id=user.id, type="usage", tokens_delta=-charged,
        description=f"AI-правка блока сайта ({charged/100:.2f} ₽)",
        model="claude",
    ))
    db.commit()
    return {"new_html": new_html, "block_id": body.block_id, "cost_kop": charged}


# ---------------------------------------------------------------------------
# Hosting & serving
# ---------------------------------------------------------------------------
@router.post("/sites/projects/{project_id}/host")
def site_project_host(project_id: int, db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    """Publish site -- save files and return public URL."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.code_html:
        raise HTTPException(400, "Сначала сгенерируйте код")

    host_dir = os.path.join(_sites_host_base, str(project_id))
    os.makedirs(host_dir, exist_ok=True)
    final_html = p.code_html
    if p.attached_bot_id:
        from server.models import ChatBot
        b = db.query(ChatBot).filter_by(id=p.attached_bot_id, user_id=user.id).first()
        if b and b.widget_enabled:
            app_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
            final_html = _inject_chatbot_widget(final_html, b.id, app_url)
    with open(os.path.join(host_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(final_html)

    p.hosted_path = f"/sites/hosted/{project_id}/"
    db.commit()
    return {"url": p.hosted_path, "status": "hosted",
            "widget_attached": bool(p.attached_bot_id)}


@router.get("/sites/hosted/{project_id}/{full_path:path}")
def site_project_serve(project_id: int, full_path: str = ""):
    """Serve hosted site files.

    HTML отдаётся ЧЕРЕЗ SANDBOX IFRAME с null-origin:
      - sandbox="allow-scripts allow-forms allow-popups" — JS работает
      - но origin=null → document.cookie/localStorage основного домена НЕДОСТУПНЫ
      - strict CSP на обёртке как defense-in-depth
      - даже если AI сгенерировал XSS-вектор (`<img onerror=fetch(...)>`) —
        украсть токен пользователя не получится, т.к. sandbox в другом origin
    Path traversal: через Path.resolve() + relative_to (raises на `..` и symlinks).
    """
    from pathlib import Path
    from fastapi.responses import HTMLResponse as _HTMLResponse
    host_dir = Path(_sites_host_base, str(project_id)).resolve()
    try:
        file_path = (host_dir / (full_path or "index.html")).resolve()
        file_path.relative_to(host_dir)
    except (ValueError, OSError):
        raise HTTPException(403, "Доступ запрещён")
    if not file_path.is_file():
        raise HTTPException(404, "Файл не найден")
    ext = file_path.suffix.lower()
    # HTML — в sandbox iframe. Остальные (картинки/css) — напрямую.
    if ext in (".html", ".htm", ""):
        try:
            inner_html = file_path.read_text(encoding="utf-8")
        except Exception:
            raise HTTPException(500, "Не удалось прочитать файл")
        # Экранируем для вставки в srcdoc: кавычки и амперсанды
        escaped = (inner_html
                   .replace("&", "&amp;")
                   .replace('"', "&quot;"))
        wrapper = (
            '<!doctype html><html lang="ru"><head>'
            '<meta charset="utf-8"/>'
            '<title>Site</title>'
            '<style>html,body,iframe{margin:0;padding:0;border:0;width:100%;height:100vh;background:#fff}</style>'
            '</head><body>'
            '<iframe sandbox="allow-scripts allow-forms allow-popups" '
            'referrerpolicy="no-referrer" '
            f'srcdoc="{escaped}"></iframe>'
            '</body></html>'
        )
        return _HTMLResponse(wrapper, headers={
            # На ОБЁРТКЕ — strict CSP (никакого JS, только style и frame).
            # Пользовательский HTML запускается внутри iframe с null origin.
            "Content-Security-Policy": (
                "default-src 'none'; "
                "style-src 'unsafe-inline'; "
                "frame-src data: blob:; "
                "child-src data: blob:; "
                "frame-ancestors 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })
    # Картинки, css, прочие ассеты — как есть
    return FileResponse(str(file_path))


@router.post("/sites/projects/{project_id}/download")
def site_project_download(project_id: int, db: Session = Depends(get_db),
                           user: User = Depends(current_user)):
    """Trigger hosted save + return download URL."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.code_html:
        raise HTTPException(400, "Сначала сгенерируйте код")

    host_dir = os.path.join(_sites_host_base, str(project_id))
    os.makedirs(host_dir, exist_ok=True)
    with open(os.path.join(host_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(p.code_html)

    p.hosted_path = f"/sites/hosted/{project_id}/"
    db.commit()
    return {"url": f"/sites/hosted/{project_id}/", "status": "ready"}


@router.get("/sites/projects/{project_id}/zip")
def site_project_zip(project_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Скачать сайт целиком ZIP-ом: index.html + все картинки в /images/."""
    import io, zipfile, re
    from fastapi.responses import StreamingResponse

    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.code_html:
        raise HTTPException(400, "Сначала сгенерируйте код")

    html = p.code_html
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    project_root = os.path.dirname(base_dir)
    app_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")

    # Собираем все URL картинок из HTML (полные и /uploads/...)
    pattern = re.compile(r'(?:src|href)=["\']([^"\']+)["\']', re.IGNORECASE)
    found = set()
    for m in pattern.finditer(html):
        url = m.group(1)
        if "/uploads/" in url:
            # Извлекаем локальный путь
            idx = url.find("/uploads/")
            local_rel = url[idx:]  # /uploads/xxx.png
            found.add((url, local_rel))

    # Заменяем URL в HTML на images/filename
    html_zip = html
    for orig_url, local_rel in found:
        fname = os.path.basename(local_rel)
        html_zip = html_zip.replace(orig_url, f"images/{fname}")

    # Пакуем ZIP
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html_zip)
        for orig_url, local_rel in found:
            local_abs = os.path.join(project_root, local_rel.lstrip("/"))
            if os.path.exists(local_abs):
                fname = os.path.basename(local_rel)
                zf.write(local_abs, f"images/{fname}")
    buf.seek(0)

    safe_name = re.sub(r'[^\w\-]', '_', p.name or 'site')[:40]
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
@router.post("/sites/code")
def site_decode_code(body: dict, db: Session = Depends(get_db), user=Depends(optional_user)):
    """Utility: return clean code without markdown. Used internally."""
    content = body.get("content", "")
    for marker in ["```html\n", "```\n", "```html", "```"]:
        if content.startswith(marker):
            content = content[len(marker):]
            if "```" in content:
                content = content.rsplit("```", 1)[0]
            break
    return {"clean": content.strip()}
