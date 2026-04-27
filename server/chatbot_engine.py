"""
Chatbot Engine — ядро постоянных ботов.

Два режима:
1. Простой: system_prompt + AI модель (если нет графа)
2. Граф: исполняет воркфлоу из конструктора (nodes + edges)

RAM-контекст диалогов, отправка сообщений, webhook setup.
"""
import os, json, logging, secrets, time, asyncio
from collections import deque, defaultdict
from datetime import datetime, timedelta

import httpx

from server.ai import generate_response
from server.db import SessionLocal
from server.models import User, Transaction

log = logging.getLogger("chatbot")

# ── Persistent conversation memory ──────────────────────────────────────────
# Раньше: in-memory `_conversations` dict — терялся при рестарте, не работал
# на multi-worker (каждый uvicorn worker имел свою копию). Сейчас — SQLite
# таблица BotConversationTurn, переживает рестарт, общая для всех воркеров.
# Lookup делаем «по запросу» — нет hot-cache, но при нашем RPS это не bottleneck.

_CONV_HISTORY_LIMIT = 20  # столько последних тёрнов берём для контекста


def conv_history(bot_id: int, chat_id: str, limit: int = _CONV_HISTORY_LIMIT) -> list[dict]:
    """Последние N тёрнов диалога из БД, в порядке возрастания id."""
    from server.db import db_session
    from server.models import BotConversationTurn
    try:
        with db_session() as db:
            rows = (db.query(BotConversationTurn)
                    .filter_by(bot_id=bot_id, chat_id=str(chat_id))
                    .order_by(BotConversationTurn.id.desc())
                    .limit(limit).all())
            # вернули в DESC, разворачиваем в ASC чтобы AI видел в хронологии
            return [{"role": r.role, "content": r.content} for r in reversed(rows)]
    except Exception as e:
        log.warning(f"[conv_history] bot={bot_id} chat={chat_id}: {e}")
        return []


def conv_append(bot_id: int, chat_id: str, role: str, content: str) -> None:
    """Добавить один тёрн в историю диалога."""
    from server.db import db_session
    from server.models import BotConversationTurn
    if not content:
        return
    try:
        with db_session() as db:
            db.add(BotConversationTurn(
                bot_id=bot_id, chat_id=str(chat_id),
                role=role, content=content[:8000],  # safety-cap чтобы не раздувать
            ))
            db.commit()
    except Exception as e:
        log.warning(f"[conv_append] bot={bot_id} chat={chat_id}: {e}")


_recent_chats: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))

HTTP = httpx.AsyncClient(timeout=30)


# ── SSRF защита для http_request node ─────────────────────────────────────────
# Блокирует обращения к localhost / link-local / приватным сетям / cloud metadata.
# Без этого владелец бота мог бы через http_request читать http://localhost:8000/admin,
# http://169.254.169.254/ (AWS/Яндекс/GCP/Hetzner Cloud metadata), сканировать внутреннюю сеть.
_SSRF_BLOCKED_HOSTS = {
    "localhost", "0.0.0.0", "ip6-localhost", "ip6-loopback",
    "metadata.google.internal", "metadata", "metadata.goog",
}

# Дополнительные CIDR-блоки сверх ipaddress.is_private/is_reserved
import ipaddress as _ipaddr
_SSRF_BLOCKED_CIDRS = [
    _ipaddr.ip_network("169.254.0.0/16"),    # link-local + cloud metadata (AWS, GCP, Azure, Yandex)
    _ipaddr.ip_network("100.64.0.0/10"),     # CG-NAT (часто используется в k8s/docker)
    _ipaddr.ip_network("fd00::/8"),          # IPv6 ULA
    _ipaddr.ip_network("fe80::/10"),         # IPv6 link-local
    _ipaddr.ip_network("::ffff:0:0/96"),     # IPv4-mapped IPv6 — иначе можно обойти через ::ffff:127.0.0.1
    _ipaddr.ip_network("64:ff9b::/96"),      # NAT64
]


def _ssrf_ip_blocked(ip_str: str) -> str | None:
    """Возвращает причину блокировки или None."""
    try:
        ip = _ipaddr.ip_address(ip_str)
    except ValueError:
        return f"invalid IP: {ip_str}"
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        return f"private/reserved IP: {ip_str}"
    for net in _SSRF_BLOCKED_CIDRS:
        try:
            if ip in net:
                return f"blocked CIDR: {ip_str} in {net}"
        except TypeError:
            # IPv4 vs IPv6 mismatch — пропускаем
            continue
    return None


def _ssrf_validate(url: str) -> str | None:
    """
    Возвращает текст ошибки если URL ведёт в запрещённую сеть, иначе None.

    Двойной резолв (getaddrinfo возвращает все записи) + расширенный блок-лист
    защищают от:
      - прямого указания приватных IP / cloud metadata
      - DNS round-robin с одной публичной + одной приватной A-записью
      - IPv4-mapped IPv6 (::ffff:127.0.0.1)
    Полная защита от DNS-rebinding требует pin'а IP в HTTP-клиенте — это
    делается отдельно в _ssrf_safe_request().
    """
    import socket
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:
        return "invalid URL"
    if parsed.scheme not in ("http", "https"):
        return "только http/https"
    host = (parsed.hostname or "").lower().strip().rstrip(".")
    if not host:
        return "нет хоста"
    if host in _SSRF_BLOCKED_HOSTS:
        return "internal host blocked"
    # Попытка трактовать host как литеральный IP (включая IPv6 в скобках)
    try:
        reason = _ssrf_ip_blocked(host)
        if reason:
            return reason
        # Если это валидный IP-литерал — резолв не нужен
        return None
    except ValueError:
        pass
    # Резолв ВСЕХ адресов через getaddrinfo. Если хоть один в блок-листе — отказ.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return "DNS-резолв не удался"
    seen: set[str] = set()
    for info in infos:
        ip_str = info[4][0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        reason = _ssrf_ip_blocked(ip_str)
        if reason:
            return reason
    return None


# ── Резолв public-asset URL в локальный путь ────────────────────────────────
# В воркфлоу юзер задаёт public-link на свой загруженный лидмагнит:
#   https://aiche.ru/assets/public/<token>  ИЛИ  /assets/public/<token>
# Эта функция превращает такой URL в локальный путь /uploads/assets/<uuid>.<ext>
# который понимает send_telegram_document/send_max_photo.
import re as _re_assets
_ASSETS_PUBLIC_RE = _re_assets.compile(
    r'(?:https?://[^/\s]+)?/assets/public/([A-Za-z0-9_\-]{16,})'
)


def _resolve_asset_url_to_path(url_or_path: str) -> str:
    """Если url ведёт на /assets/public/<token> — возвращает StoredAsset.path,
    иначе возвращает вход как есть (legacy file_path)."""
    if not url_or_path:
        return url_or_path
    m = _ASSETS_PUBLIC_RE.search(url_or_path)
    if not m:
        return url_or_path
    token = m.group(1)
    try:
        from server.db import db_session
        from server.models import StoredAsset
        with db_session() as db:
            a = db.query(StoredAsset).filter_by(
                public_token=token, is_active=True).first()
            if a:
                return a.path  # /uploads/assets/<uuid>.<ext>
    except Exception as e:
        log.warning(f"[asset-resolve] {token}: {e}")
    return url_or_path


# ── Маппинг типов нод конструктора на модели AI ──────────────────────────────
NODE_MODEL_MAP = {
    "node_gpt":     "gpt-4o",
    "node_claude":  "claude",
    "node_gemini":  "gemini",
    "node_grok":    "grok",
    "orchestrator": "gpt",
    "prompt":       None,  # не вызывает AI, просто передаёт system prompt дальше
    "agent_smm":    "gpt",
    "agent_copywriter": "gpt",
}


# ══════════════════════════════════════════════════════════════════════════════
#  ОСНОВНАЯ ОБРАБОТКА СООБЩЕНИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def handle_message(bot, chat_id: str, user_text: str,
                         platform: str, user_name: str = "",
                         extra_ctx: dict | None = None) -> str | None:
    """
    Обработать входящее сообщение.
    Списание — по РЕАЛЬНЫМ токенам модели (из ctx['_usage']).
    """
    if not _check_daily_limit(bot):
        return None

    # Проверим что у владельца есть хоть какой-то баланс (минимум 1 CH)
    if not _owner_has_balance(bot, minimum=1):
        log.warning(f"[Bot {bot.id}] У владельца {bot.user_id} закончились токены")
        return None

    workflow = _get_bot_workflow(bot)
    # Инициализируем usage в ctx чтобы провайдеры могли его заполнить
    usage_acc = {"input": 0, "output": 0, "cached": 0, "model": bot.model or "gpt"}

    # Свои API-ключи юзера для AI-провайдеров (для скидки + использования
    # его лимитов/квоты вместо наших). Загружаем один раз для всего диалога.
    user_keys = _load_user_api_keys(bot.user_id)

    base_ctx = {**(extra_ctx or {}), "_usage": usage_acc,
                "_user_keys": user_keys, "_bot": bot}

    if workflow:
        answer = await _execute_workflow(bot, chat_id, user_text, platform, user_name, workflow,
                                         extra_ctx=base_ctx)
    else:
        answer = await _simple_reply(bot, chat_id, user_text, platform, user_name, usage_acc, user_keys)

    if answer:
        _save_for_summary(bot.id, chat_id, user_text, answer, user_name, platform)
        _deduct_bot_usage(bot, usage_acc)
        _increment_replies(bot)

    return answer


def _owner_has_balance(bot, minimum: int = 1) -> bool:
    from server.db import db_session
    with db_session() as db:
        owner = db.query(User).filter_by(id=bot.user_id).first()
        return bool(owner and (owner.tokens_balance or 0) >= minimum)


def _load_user_api_keys(user_id: int) -> dict[str, str]:
    """
    Возвращает {provider: api_key} — все собственные API-ключи юзера.
    Используется в AI-нодах для подмены наших ключей юзерскими (скидка 80%
    + его квота, не наша). Кешируется на 60 секунд per-user.
    """
    if not user_id:
        return {}
    import time as _t
    now = _t.time()
    cached = _USER_KEYS_CACHE.get(user_id)
    if cached and (now - cached[0]) < _USER_KEYS_TTL:
        return dict(cached[1])
    from server.db import db_session
    from server.models import UserApiKey
    out: dict[str, str] = {}
    try:
        with db_session() as db:
            rows = db.query(UserApiKey).filter_by(user_id=user_id).all()
            for r in rows:
                if r.api_key and r.provider:
                    out[r.provider] = r.api_key
    except Exception as e:
        log.warning(f"[user_keys] load failed for user {user_id}: {type(e).__name__}")
    _USER_KEYS_CACHE[user_id] = (now, dict(out))
    return out


_USER_KEYS_CACHE: dict[int, tuple[float, dict[str, str]]] = {}
_USER_KEYS_TTL = 60   # секунд


def invalidate_user_keys_cache(user_id: int | None = None) -> None:
    """Сбросить кэш user-ключей. Вызывать при добавлении/удалении ключа."""
    if user_id is None:
        _USER_KEYS_CACHE.clear()
    else:
        _USER_KEYS_CACHE.pop(user_id, None)


def _user_key_for_model(ctx: dict, model_id: str) -> str | None:
    """Возвращает user-ключ для модели если у юзера он привязан, иначе None."""
    user_keys = ctx.get("_user_keys") or {}
    if not user_keys:
        return None
    provider = _model_to_provider(model_id)
    if not provider:
        return None
    return user_keys.get(provider)


# ── Прайс-контекст для AI ──────────────────────────────────────────────────
# Чтобы не таскать весь прайс при каждом сообщении (дорого по токенам),
# подключаем его только когда вопрос связан с ценой/услугой.
# Алгоритм:
#   1. detect price-keywords в user_text — если нет, прайс НЕ инжектим
#   2. ищем релевантные позиции (substring match по name/category/description)
#   3. inject топ-15 в system_prompt компактным форматом
#
# Без vector embeddings — простой текстовый поиск. Для большинства прайсов
# (10–200 позиций) этого хватает + быстро + бесплатно.

_PRICE_KEYWORDS = (
    "сколько", "стоит", "стоимост", "цена", "цены", "ценник", "ценам",
    "прайс", "тариф", "оплат", "руб", "₽", "₽,", "rub", "дорог", "дешев",
    "бюджет", "сколько будет", "сколько стоит", "сколько по",
    "сколько это", "почём", "почем", "по чем", "сколько за",
    "стоимость", "услуг", "товар",
)


def _price_keyword_in_text(text: str) -> bool:
    """Содержит ли user-text триггер на показ прайса."""
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in _PRICE_KEYWORDS)


# ── Vector search для прайса (OpenAI text-embedding-3-small) ───────────────
# Cosine similarity между embedding'ом запроса и embedding'ами позиций прайса.
# Возвращает топ-K релевантных. Цена: ~$0.02 за 1M токенов = ~0.0001₽ за запрос.

import json as _json_emb
import math as _math
import time as _time
from collections import OrderedDict
import threading as _threading

_EMBED_MODEL = "text-embedding-3-small"   # 1536 dim, $0.02/1M токенов
# OrderedDict вместо dict — для FIFO eviction вместо clear-всё-при-переполнении.
# Раньше при >500 записей кэш сбрасывался полностью → thrashing на наплыве.
_EMBED_QUERY_CACHE: "OrderedDict[str, tuple[float, list]]" = OrderedDict()
_EMBED_QUERY_TTL = 600   # 10 минут — частые запросы «сколько стоит» дешёвые
_EMBED_QUERY_MAX = 1000
_EMBED_QUERY_LOCK = _threading.Lock()


def _compute_embedding(text: str) -> list[float] | None:
    """
    Возвращает 1536-мерный вектор для text через OpenAI embedding API.
    None при ошибке (недоступность / нет ключа). Caller использует fallback.
    """
    if not text or not text.strip():
        return None
    try:
        from openai import OpenAI
        from server.ai import _get_api_keys
        keys = _get_api_keys("openai")
        if not keys:
            return None
        client = OpenAI(api_key=keys[0])
        resp = client.embeddings.create(
            model=_EMBED_MODEL,
            input=text[:8000],   # 8000 chars = ~2000 токенов с запасом до лимита
            encoding_format="float",
        )
        return list(resp.data[0].embedding)
    except Exception as e:
        log.warning(f"[embedding] failed: {type(e).__name__}")
        return None


def _cached_query_embedding(query: str) -> list[float] | None:
    """Кэш на 10 мин — частые «сколько стоит» не плодят повторные API-вызовы.
    LRU-eviction (FIFO самого старого), thread-safe — на наплыве запросов
    кэш не сбрасывается полностью, а вытесняет один элемент за раз."""
    key = (query or "").strip().lower()[:200]
    if not key:
        return None
    now = _time.time()
    with _EMBED_QUERY_LOCK:
        cached = _EMBED_QUERY_CACHE.get(key)
        if cached and (now - cached[0]) < _EMBED_QUERY_TTL:
            # LRU touch: переносим в конец как «недавно использованный»
            _EMBED_QUERY_CACHE.move_to_end(key)
            return cached[1]
    # Сетевой вызов вне лока — может занять 100-500мс.
    vec = _compute_embedding(key)
    if vec is not None:
        with _EMBED_QUERY_LOCK:
            _EMBED_QUERY_CACHE[key] = (now, vec)
            _EMBED_QUERY_CACHE.move_to_end(key)
            while len(_EMBED_QUERY_CACHE) > _EMBED_QUERY_MAX:
                _EMBED_QUERY_CACHE.popitem(last=False)
    return vec


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine sim двух одинаковых-размерности векторов. Без numpy."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0; na = 0.0; nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (_math.sqrt(na) * _math.sqrt(nb))


def _item_to_embedding_text(item) -> str:
    """Формирует текст для embedding из полей позиции прайса."""
    parts = [item.name or ""]
    if item.category:
        parts.append(f"({item.category})")
    if item.description:
        parts.append(item.description)
    if item.price_text:
        parts.append(item.price_text)
    return " ".join(parts).strip()


def update_price_item_embedding(item) -> None:
    """
    Обновить embedding одной позиции прайса. Зовётся при POST/PATCH.
    Не raise'ит — fallback на substring если OpenAI недоступен.
    """
    text = _item_to_embedding_text(item)
    vec = _compute_embedding(text)
    if vec is not None:
        item.embedding_json = _json_emb.dumps(vec, separators=(",", ":"))


def batch_update_price_embeddings(items: list) -> int:
    """
    Batch вычисление embeddings для нескольких позиций прайса в один
    API-call (OpenAI принимает массив input). Дешевле и быстрее чем по одной
    при импорте CSV.

    Returns: количество успешно обновлённых.
    """
    if not items:
        return 0
    texts = [_item_to_embedding_text(it) for it in items]
    if not any(texts):
        return 0
    try:
        from openai import OpenAI
        from server.ai import _get_api_keys
        keys = _get_api_keys("openai")
        if not keys:
            return 0
        client = OpenAI(api_key=keys[0])
        # OpenAI batch limit — 2048 текстов или 300k токенов. Дробим если больше.
        BATCH = 1000
        updated = 0
        for start in range(0, len(items), BATCH):
            chunk_items = items[start:start + BATCH]
            chunk_texts = [t[:8000] for t in texts[start:start + BATCH]]
            resp = client.embeddings.create(
                model=_EMBED_MODEL,
                input=chunk_texts,
                encoding_format="float",
            )
            for it, emb in zip(chunk_items, resp.data):
                it.embedding_json = _json_emb.dumps(list(emb.embedding),
                                                     separators=(",", ":"))
                updated += 1
        return updated
    except Exception as e:
        log.warning(f"[embedding-batch] failed: {type(e).__name__}")
        return 0


def _substring_score(item, words: list[str]) -> int:
    """Fallback search когда embeddings недоступны."""
    hay = " ".join(filter(None, [
        (item.name or "").lower(),
        (item.category or "").lower(),
        (item.description or "").lower(),
    ]))
    return sum(1 for w in words if w in hay)


def _price_context_for_question(bot, user_text: str, max_items: int = 15) -> str:
    """
    Возвращает компактный prompt-фрагмент с релевантными позициями прайса
    либо пустую строку (когда вопрос не про цены ИЛИ прайса нет).

    Алгоритм:
    1. Detect price-keywords — иначе сразу пусто.
    2. Vector search через cosine similarity между embedding'ом вопроса
       и embedding'ами позиций. Top-K по similarity, threshold 0.30
       (отсекаем совсем нерелевантное).
    3. Fallback на substring matching если embeddings недоступны.

    Стоимость: ~$0.000002 за вопрос (text-embedding-3-small, 50 токенов).
    Кэш на 10 мин для частых запросов «сколько стоит».
    """
    if not _price_keyword_in_text(user_text):
        return ""
    from server.db import db_session
    from server.models import BotPriceItem
    try:
        with db_session() as db:
            rows = (db.query(BotPriceItem)
                      .filter_by(bot_id=bot.id, is_active=True)
                      .order_by(BotPriceItem.sort_order, BotPriceItem.id)
                      .all())
            if not rows:
                return ""

            # Vector search (если у позиций есть embedding'и И мы можем
            # посчитать embedding запроса)
            scored: list[tuple[float, object, str]] = []
            with_emb = [r for r in rows if r.embedding_json]
            use_vector = bool(with_emb)
            query_vec = None
            if use_vector:
                query_vec = _cached_query_embedding(user_text)
                if query_vec is None:
                    use_vector = False

            if use_vector:
                # cosine sim каждой позиции с embedding'ом запроса
                for r in rows:
                    if not r.embedding_json:
                        continue
                    try:
                        v = _json_emb.loads(r.embedding_json)
                    except Exception:
                        continue
                    sim = _cosine_similarity(query_vec, v)
                    if sim >= 0.30:   # threshold — ниже скорее шум
                        scored.append((sim, r, "vec"))
                # Если ни одна позиция не прошла threshold — это «общий» вопрос
                # типа «покажи прайс» → берём top-N по sort_order
                if not scored:
                    items = rows[:max_items]
                else:
                    scored.sort(key=lambda x: -x[0])
                    items = [s[1] for s in scored[:max_items]]
            else:
                # Fallback: substring matching по словам из вопроса
                words = [w for w in user_text.lower().split() if len(w) >= 3]
                ssub = [(_substring_score(r, words), r) for r in rows]
                ssub.sort(key=lambda x: (-x[0], x[1].sort_order or 0, x[1].id))
                if ssub and ssub[0][0] == 0:
                    items = [s[1] for s in ssub[:max_items]]
                else:
                    items = [s[1] for s in ssub if s[0] > 0][:max_items]

            if not items:
                return ""

            lines = ["", "ПРАЙС-ЛИСТ (только релевантные позиции):"]
            current_cat = None
            for it in items:
                cat = it.category or ""
                if cat != current_cat:
                    if cat:
                        lines.append(f"\n{cat}:")
                    current_cat = cat
                price_str = ""
                if it.price_kop:
                    price_str = f"{it.price_kop / 100:,.0f} ₽".replace(",", " ")
                elif it.price_text:
                    price_str = it.price_text
                else:
                    price_str = "цена по запросу"
                desc = f" — {it.description}" if it.description else ""
                lines.append(f"• {it.name}: {price_str}{desc}")
            lines.append("")
            lines.append("Если клиент спрашивает цену — отвечай по этому прайсу. "
                          "Если позиции нет в прайсе — скажи что уточнишь и спроси контакт.")
            return "\n".join(lines)
    except Exception as e:
        log.warning(f"[price_context] failed for bot {bot.id}: {type(e).__name__}")
        return ""


def _call_ai_with_fallback(model: str, messages: list, user_key: str | None,
                            extra: dict | None = None) -> dict:
    """
    Универсальный вызов AI с поддержкой user-ключа + fallback.
    Если user_key передан — пробуем сначала его. При ошибке (401/403/quota) —
    откатываемся на наши ключи (если allow_fallback=True у этого ключа,
    но пока всегда True — будущая колонка).

    Возвращает {content, input_tokens, output_tokens, cached_tokens, ...}.
    """
    if user_key:
        try:
            result = generate_response(model, messages, extra=extra,
                                       user_api_key=user_key)
            content = result.get("content", "") if isinstance(result, dict) else ""
            # generate_response при ошибке провайдера может вернуть fallback-stub
            # «Сервис временно недоступен» — это значит user-ключ упал.
            # В таком случае пробуем наш ключ.
            if "временно недоступен" not in content and content:
                return result
            log.warning(f"[user_key] {model}: упал на user-ключе, fallback на наш")
        except Exception as e:
            log.warning(f"[user_key] {model}: exception на user-ключе ({type(e).__name__}), fallback")
    # Наш ключ
    return generate_response(model, messages, extra=extra)


def _get_bot_workflow(bot) -> dict | None:
    """Извлечь граф воркфлоу из настроек бота (AgentConfig или ChatBot)."""
    # ChatBot может иметь связанный AgentConfig через settings JSON
    if not hasattr(bot, 'settings') or not bot.settings:
        # Для ChatBot — ищем связанный AgentConfig
        if hasattr(bot, 'workflow_json') and bot.workflow_json:
            try:
                return json.loads(bot.workflow_json)
            except Exception:
                pass
        return None
    try:
        settings = json.loads(bot.settings) if isinstance(bot.settings, str) else bot.settings
        if settings.get("wfc_nodes") and settings.get("wfc_edges"):
            return settings
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  ПРОСТОЙ РЕЖИМ (без графа)
# ══════════════════════════════════════════════════════════════════════════════

async def _simple_reply(bot, chat_id, user_text, platform, user_name,
                        usage_acc: dict | None = None,
                        user_keys: dict[str, str] | None = None) -> str:
    """Простой режим: system_prompt → AI модель → ответ.

    user_keys — собственные API-ключи юзера {provider: key}. Если у юзера
    есть ключ для провайдера выбранной модели — AI-вызов идёт через него
    (его квота, скидка 80% на маржу). При ошибке user-key — fallback на наш.
    """
    history = conv_history(bot.id, chat_id)

    # Базовый system_prompt + умный inject прайса (только при вопросе о цене)
    sys_prompt = bot.system_prompt or "Ты полезный AI-ассистент. Отвечай кратко и по делу."
    price_ctx = _price_context_for_question(bot, user_text)
    if price_ctx:
        sys_prompt = sys_prompt + "\n" + price_ctx

    messages = [{"role": "system", "content": sys_prompt}]
    for msg in history:
        messages.append(msg)
    messages.append({"role": "user", "content": user_text})

    try:
        model = bot.model or "gpt"
        # user_keys приходят из _load_user_api_keys(bot.user_id) в handle_message
        user_key = None
        if user_keys:
            provider = _model_to_provider(model)
            user_key = user_keys.get(provider) if provider else None
        result = _call_ai_with_fallback(model, messages, user_key)
        answer = result.get("content", "") if isinstance(result, dict) else str(result)
        if usage_acc and isinstance(result, dict):
            usage_acc["input"] += result.get("input_tokens", 0) or 0
            usage_acc["output"] += result.get("output_tokens", 0) or 0
            usage_acc["cached"] += result.get("cached_tokens", 0) or 0
    except Exception as e:
        log.error(f"[Bot {bot.id}] AI error: {type(e).__name__}")
        return "Произошла ошибка. Попробуйте позже."

    if not answer:
        return "Не удалось получить ответ."

    # Persist в БД — заменяет старый in-memory deque
    conv_append(bot.id, chat_id, "user", user_text)
    conv_append(bot.id, chat_id, "assistant", answer)
    return answer


# ══════════════════════════════════════════════════════════════════════════════
#  ИСПОЛНЕНИЕ ГРАФА ВОРКФЛОУ
# ══════════════════════════════════════════════════════════════════════════════

async def _execute_workflow(bot, chat_id, user_text, platform, user_name,
                            workflow: dict, extra_ctx: dict | None = None) -> str:
    """
    Исполнить граф воркфлоу.
    nodes = [{id, type, cfg: {...}}, ...]
    edges = [{id, from, to}, ...]
    """
    nodes = workflow.get("wfc_nodes", [])
    edges = workflow.get("wfc_edges", [])

    if not nodes:
        return await _simple_reply(bot, chat_id, user_text, platform, user_name)

    # Построить граф: node_id → [connected node_ids]
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        adjacency[edge["from"]].append(edge["to"])

    # Найти точку входа (триггер)
    trigger_types = {"trigger_tg", "trigger_vk", "trigger_avito", "trigger_max",
                     "trigger_manual", "trigger_webhook", "trigger_schedule",
                     "trigger_imap"}
    trigger_node = None
    for n in nodes:
        if n["type"] in trigger_types:
            trigger_node = n
            break

    if not trigger_node:
        # Если нет триггера — берём первую ноду
        trigger_node = nodes[0] if nodes else None

    if not trigger_node:
        return await _simple_reply(bot, chat_id, user_text, platform, user_name)

    # Контекст выполнения. История диалога — из persistent SQLite-стора.
    history = conv_history(bot.id, chat_id)

    ctx = {
        "input_text": user_text,
        "user_name": user_name,
        "platform": platform,
        "chat_id": chat_id,
        "history": list(history),
        "results": {},      # node_id → output text
        "bot": bot,
        "final_output": "",
    }
    if extra_ctx:
        ctx.update(extra_ctx)

    # Топологическая сортировка
    order = _topo_sort(nodes, edges)
    if not order:
        log.warning(f"[Bot {bot.id}] Cycle in workflow graph, falling back to simple")
        return await _simple_reply(bot, chat_id, user_text, platform, user_name)

    # Индекс нод
    node_map = {n["id"]: n for n in nodes}
    # Прокидываем для оркестратора и switch
    ctx["_edges"] = edges
    ctx["_nodes_map"] = node_map
    # Набор "отключённых" нод (оркестратор выбрал другую ветку)
    skipped_by_routing: set[str] = set()

    # Исполнение по порядку
    for nid in order:
        if nid in skipped_by_routing:
            continue

        node = node_map.get(nid)
        if not node:
            continue

        # Собираем входные данные от предыдущих нод
        inputs = []
        for edge in edges:
            if edge["to"] == nid and edge["from"] in ctx["results"]:
                inputs.append(ctx["results"][edge["from"]])

        input_text = "\n\n".join(inputs) if inputs else ctx["input_text"]

        try:
            output = await _execute_node(node, input_text, ctx)
            ctx["results"][nid] = output

            # После оркестратора — отключаем все ветки кроме выбранной.
            # Если choice не установлен (редкий случай, напр. LLM упал) — не скипаем ничего,
            # пропускаем сообщение дальше ко всем веткам (безопасный fallback).
            if node.get("type") == "orchestrator":
                chosen = ctx.get("orchestrator_choice")
                if chosen:
                    my_id = node.get("id")
                    all_downstream = [e["to"] for e in edges if e["from"] == my_id]
                    for branch_id in all_downstream:
                        if branch_id != chosen:
                            # Рекурсивно отключаем всё ниже этой ветки
                            _collect_downstream(branch_id, edges, skipped_by_routing)
                            skipped_by_routing.add(branch_id)

            # После switch — отключаем ветки чьё имя не совпало с ctx["switch_branch"]
            if node.get("type") == "switch":
                active_branch = ctx.get("switch_branch", "default")
                my_id = node.get("id")
                for edge in edges:
                    if edge["from"] != my_id:
                        continue
                    branch_node = node_map.get(edge["to"])
                    if not branch_node:
                        continue
                    # Имя ветки хранится в cfg.branch_name дочерней ноды (опц.)
                    branch_name = (branch_node.get("cfg", {}) or {}).get("branch_name", "")
                    if branch_name and branch_name != active_branch:
                        _collect_downstream(edge["to"], edges, skipped_by_routing)
                        skipped_by_routing.add(edge["to"])
        except Exception as e:
            log.error(f"[Bot {bot.id}] Node {nid} ({node['type']}) error: {e}")
            ctx["results"][nid] = f"[Ошибка: {e}]"

    # Persist diaglog в БД (заменяет старый in-memory deque).
    # local list — для read-через-ctx внутри текущего вызова, без второго SELECT.
    history.append({"role": "user", "content": user_text})
    conv_append(bot.id, chat_id, "user", user_text)
    if ctx["final_output"]:
        history.append({"role": "assistant", "content": ctx["final_output"]})
        conv_append(bot.id, chat_id, "assistant", ctx["final_output"])

    return ctx["final_output"] or ctx["results"].get(order[-1], "")


async def _execute_node(node: dict, input_text: str, ctx: dict) -> str:
    """Исполнить одну ноду графа."""
    ntype = node["type"]
    cfg = node.get("cfg", {})

    # ── Триггеры (входные точки, просто прокидывают текст)
    if ntype.startswith("trigger_"):
        return ctx["input_text"]

    # ── AI модели ──────────────────────────────────────────────────────────
    if ntype in ("node_gpt", "node_claude", "node_gemini", "node_grok"):
        model = NODE_MODEL_MAP.get(ntype, "gpt")
        system = cfg.get("system", "Ты полезный ассистент.")
        if ctx.get("active_role_prompt"):
            system = ctx["active_role_prompt"] + "\n\n" + system
        # Умный inject прайса: только если вопрос содержит price-keywords
        # (иначе не тратим токены). Берётся из ctx['_bot'] что лежит во фрейме.
        _bot = ctx.get("_bot")
        if _bot:
            price_ctx = _price_context_for_question(_bot, input_text)
            if price_ctx:
                system = system + "\n" + price_ctx
        messages = [{"role": "system", "content": system}]
        for msg in ctx.get("history", [])[-10:]:
            messages.append(msg)
        messages.append({"role": "user", "content": input_text})
        # Свой ключ юзера → AI-вызов идёт от его имени (его квота, скидка 80%
        # на нашу маржу). Если ключ упал — fallback на наш (наши ключи).
        user_key = _user_key_for_model(ctx, model)
        result = _call_ai_with_fallback(model, messages, user_key)
        answer = result.get("content", "") if isinstance(result, dict) else str(result)
        # Копим usage для финального списания
        usage_acc = ctx.get("_usage")
        if usage_acc and isinstance(result, dict):
            usage_acc["input"] += result.get("input_tokens", 0) or 0
            usage_acc["output"] += result.get("output_tokens", 0) or 0
            usage_acc["cached"] += result.get("cached_tokens", 0) or 0
            usage_acc["model"] = model
        # Пост-обработка RAG-маркеров в ответе
        answer = await _resolve_rag_markers(answer, ctx, model)
        return answer

    # ── Оркестратор: LLM-классификатор выбирает куда направить ─────────────
    if ntype == "orchestrator":
        # Найдём все downstream ноды (куда можно направить)
        edges = ctx.get("_edges", [])
        nodes_map = ctx.get("_nodes_map", {})
        my_id = node.get("id")
        downstream_ids = [e["to"] for e in edges if e["from"] == my_id]
        downstream_nodes = [nodes_map.get(nid) for nid in downstream_ids if nodes_map.get(nid)]

        # Если всего одна нода дальше — выбираем её автоматически
        # (иначе _execute_workflow отключит все ветки через routing)
        if len(downstream_nodes) <= 1:
            if downstream_nodes:
                ctx["orchestrator_choice"] = downstream_nodes[0].get("id")
            return input_text

        # Строим описание вариантов для классификатора
        options = []
        for n in downstream_nodes:
            n_type = n.get("type", "")
            n_cfg = n.get("cfg", {})
            # Первые 100 символов из system prompt или label
            hint = n_cfg.get("system") or n_cfg.get("check") or n_cfg.get("label") or n_type
            options.append({
                "id": n.get("id"),
                "type": n_type,
                "hint": (hint or "")[:150],
            })

        options_text = "\n".join(
            f'{i+1}. id="{o["id"]}" тип={o["type"]}: {o["hint"]}'
            for i, o in enumerate(options)
        )

        model = cfg.get("model", "gpt-4o-mini")
        # Маппинг на наши алиасы
        model_alias = {"gpt-4o-mini": "gpt", "gpt-4o": "gpt-4o",
                       "claude-sonnet-4-6": "claude"}.get(model, "gpt")

        classifier_prompt = (
            "Ты классификатор сообщений для маршрутизации. Определи какой вариант лучше подходит "
            "для обработки входящего запроса. Варианты:\n\n"
            f"{options_text}\n\n"
            f"ЗАПРОС ПОЛЬЗОВАТЕЛЯ: {input_text[:500]}\n\n"
            'Ответь СТРОГО в JSON-формате: {"chosen_id": "id_выбранной_ноды", "reason": "краткое объяснение"}. '
            'Выбирай только ОДИН вариант (id из списка выше). Никакого текста кроме JSON.'
        )
        try:
            result = generate_response(model_alias, [
                {"role": "system", "content": "Ты классификатор. Отвечаешь только JSON."},
                {"role": "user", "content": classifier_prompt},
            ])
            raw = result.get("content", "") if isinstance(result, dict) else str(result)
            # Копим usage
            usage_acc = ctx.get("_usage")
            if usage_acc and isinstance(result, dict):
                usage_acc["input"] += result.get("input_tokens", 0) or 0
                usage_acc["output"] += result.get("output_tokens", 0) or 0
            # Парсим JSON (robust: balanced-brace extraction, не ломается на вложенных)
            import re as _re, json as _json
            def _parse_json_block(text: str) -> dict | None:
                t = text.strip()
                if t.startswith("```"):
                    t = _re.sub(r'^```(?:json)?\s*', '', t)
                    t = _re.sub(r'\s*```\s*$', '', t)
                try:
                    return _json.loads(t)
                except Exception:
                    pass
                start = t.find('{')
                if start == -1:
                    return None
                depth, end = 0, start
                for i in range(start, len(t)):
                    if t[i] == '{': depth += 1
                    elif t[i] == '}': depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
                try:
                    return _json.loads(t[start:end])
                except Exception:
                    return None
            data = _parse_json_block(raw)
            if data and data.get("chosen_id"):
                chosen = data.get("chosen_id")
                reason = data.get("reason", "")
                log.info(f"[Orchestrator] выбрал {chosen}: {str(reason)[:80]}")
                ctx["orchestrator_choice"] = chosen
            else:
                # Fallback: первая нода
                ctx["orchestrator_choice"] = downstream_nodes[0].get("id")
        except Exception as e:
            log.error(f"[Orchestrator] error: {e}")
            ctx["orchestrator_choice"] = downstream_nodes[0].get("id")
        return input_text

    # ── Инструкция / Prompt Builder ────────────────────────────────────────
    if ntype == "prompt":
        system_text = cfg.get("system", "")
        if system_text:
            return f"{system_text}\n\n{input_text}"
        return input_text

    # ── Условие ────────────────────────────────────────────────────────────
    if ntype == "condition":
        import re as _re_cond
        check_words = [w.strip().lower() for w in (cfg.get("check", "")).split(",") if w.strip()]
        text_lower = input_text.lower()
        # Word-boundary match: "фер" не должен срабатывать на "оферте" (фикс из 79157e9)
        def _kw_match(kw: str, text: str) -> bool:
            if not kw:
                return False
            return _re_cond.search(r'(?<!\w)' + _re_cond.escape(kw) + r'(?!\w)', text) is not None
        matched = any(_kw_match(w, text_lower) for w in check_words) if check_words else True
        return input_text if matched else ""

    # ── Switch / Router ────────────────────────────────────────────────────
    if ntype == "switch":
        field = cfg.get("field", "text")
        check_value = input_text.lower() if field == "text" else str(ctx.get(field, ""))
        # Парсим ветки: "name=keyword1,keyword2"
        branches_raw = cfg.get("branches", "")
        matched_branch = None
        for line in branches_raw.splitlines():
            if "=" not in line: continue
            name, keywords = line.split("=", 1)
            name = name.strip()
            for kw in keywords.split(","):
                kw = kw.strip().lower()
                if not kw: continue
                if kw == "*" or (kw == "__voice__" and ctx.get("is_voice")) \
                   or (kw == "__file__" and ctx.get("is_file")) \
                   or (kw == "__callback__" and ctx.get("is_callback")) \
                   or kw in check_value:
                    matched_branch = name
                    break
            if matched_branch: break
        ctx["switch_branch"] = matched_branch or "default"
        return input_text

    # ── Задержка ───────────────────────────────────────────────────────────
    if ntype == "delay":
        secs = min(int(cfg.get("secs", 2)), 30)  # макс 30 сек
        await asyncio.sleep(secs)
        return input_text

    # ── HTTP Request ───────────────────────────────────────────────────────
    if ntype == "http_request":
        import json as _json
        method = (cfg.get("method") or "GET").upper()
        if method not in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"):
            return f"[HTTP blocked: метод {method} запрещён]"
        url = cfg.get("url", "")
        if not url: return ""
        # Подстановка переменных {{input}} в url/body/headers
        def subst(s): return s.replace("{{input}}", input_text) if s else s
        url = subst(url)
        # SSRF защита: блокируем internal/metadata/private IPs
        err = _ssrf_validate(url)
        if err:
            log.warning(f"[HTTP] SSRF blocked: {url} ({err})")
            return f"[HTTP blocked: {err}]"
        try:
            headers = _json.loads(subst(cfg.get("headers") or "{}"))
        except Exception:
            headers = {}
        # Запрещаем юзеру переопределить Host (защита от DNS-rebinding через Host header).
        headers = {k: v for k, v in headers.items()
                   if isinstance(k, str) and k.lower() != "host"}
        body_raw = subst(cfg.get("body") or "")
        kwargs = {"headers": headers, "timeout": 15.0, "follow_redirects": False}
        if body_raw:
            try:
                kwargs["json"] = _json.loads(body_raw)
            except Exception:
                kwargs["content"] = body_raw
        try:
            if method == "GET":
                r = await HTTP.get(url, headers=headers, timeout=15.0,
                                   follow_redirects=False)
            else:
                r = await HTTP.request(method, url, **kwargs)
            # Если 3xx — ре-валидируем Location против SSRF (один шаг следования).
            if 300 <= r.status_code < 400:
                loc = r.headers.get("location", "")
                if loc:
                    err2 = _ssrf_validate(loc)
                    if err2:
                        log.warning(f"[HTTP] redirect SSRF blocked: {loc} ({err2})")
                        return f"[HTTP blocked redirect: {err2}]"
                # Не следуем редиректам автоматически — возвращаем как есть.
            # Лимит размера ответа: 1 МБ, чтобы не сожрать память.
            text = r.text[:1_048_576]
            # Попытка извлечь JSONPath
            extract = cfg.get("extract", "")
            if extract and extract.startswith("$"):
                try:
                    data = r.json()
                    # Простая реализация: $.key.key или $.key[0].key
                    path = extract[2:].split(".")
                    cur = data
                    for p in path:
                        if "[" in p and "]" in p:
                            name, idx = p.split("[", 1)
                            idx = int(idx.rstrip("]"))
                            cur = cur[name][idx] if name else cur[idx]
                        else:
                            cur = cur[p]
                    return str(cur)
                except Exception:
                    pass
            return text
        except Exception as e:
            # Не логируем сам URL/exception целиком — может содержать токены/пароли в query.
            log.error(f"[HTTP] request failed: {type(e).__name__}")
            return f"[HTTP error: {type(e).__name__}]"

    # ── Storage ────────────────────────────────────────────────────────────
    if ntype == "storage_get":
        key = cfg.get("key", "")
        return _storage_get(ctx["bot"].id, key)

    if ntype == "storage_set":
        key = cfg.get("key", "")
        val = (cfg.get("value", "{{input}}") or "{{input}}").replace("{{input}}", input_text)
        _storage_set(ctx["bot"].id, key, val)
        return val

    if ntype == "storage_push":
        key = cfg.get("key", "")
        val = (cfg.get("value", "{{input}}") or "{{input}}").replace("{{input}}", input_text)
        max_items = int(cfg.get("max", 100) or 100)
        _storage_push(ctx["bot"].id, key, val, max_items)
        return val

    # ── RSS ────────────────────────────────────────────────────────────────
    if ntype == "rss":
        urls = [u.strip() for u in (cfg.get("urls", "")).splitlines() if u.strip()]
        hours = int(cfg.get("hours", 48) or 48)
        limit = int(cfg.get("limit", 30) or 30)
        articles = await _fetch_rss(urls, hours, limit)
        return "\n\n".join(f"[{a['date']}] {a['title']}\n{a['link']}\n{a['summary']}" for a in articles)

    # ── Extract text (PDF/DOCX/TXT) ────────────────────────────────────────
    if ntype == "extract_text":
        path = (cfg.get("file_path", "{{input}}") or "").replace("{{input}}", input_text)
        return _extract_text_from_file(path)

    # ── Whisper STT ────────────────────────────────────────────────────────
    if ntype == "whisper":
        path = (cfg.get("file_path", "{{input}}") or "").replace("{{input}}", input_text)
        return await _whisper_transcribe(path)

    # ── TTS ────────────────────────────────────────────────────────────────
    if ntype == "tts":
        voice = cfg.get("voice", "onyx")
        audio_path = await _tts_generate(input_text, voice)
        ctx["audio_path"] = audio_path
        return audio_path

    # ── База знаний ────────────────────────────────────────────────────────
    if ntype == "kb_add":
        from server.knowledge import add_file
        import os as _os
        base = _os.path.dirname(_os.path.abspath(__file__))
        # path может быть из cfg или ctx (если файл только что прилетел)
        path = (cfg.get("path") or ctx.get("file_path") or "").replace("{{input}}", input_text)
        name = (cfg.get("name") or ctx.get("file_name") or _os.path.basename(path) or "file").replace("{{input}}", input_text)
        abs_path = _os.path.join(base, path.lstrip("/")) if not _os.path.isabs(path) else path
        content = _extract_text_from_file(path)
        import json as _j
        result = add_file(bot_id=ctx["bot"].id, name=name, path=path,
                          size=_os.path.getsize(abs_path) if _os.path.exists(abs_path) else 0,
                          content_text=content)
        return _j.dumps(result, ensure_ascii=False)

    if ntype == "kb_search_file":
        from server.knowledge import search_file
        query = (cfg.get("query", "{{input}}") or "{{input}}").replace("{{input}}", input_text)
        top = int(cfg.get("top", 5) or 5)
        results = search_file(ctx["bot"].id, query, top)
        if not results: return "[Файл не найден]"
        lines = [f"{r['name']} — {r['description']}" for r in results]
        ctx["found_files"] = results  # для последующего output_tg_file
        return "\n".join(lines)

    if ntype == "kb_search":
        from server.knowledge import search_kb
        query = (cfg.get("query", "{{input}}") or "{{input}}").replace("{{input}}", input_text)
        top = int(cfg.get("top", 5) or 5)
        results = search_kb(ctx["bot"].id, query, top)
        if not results: return "[В базе знаний ничего не найдено]"
        return "\n\n".join(
            f"### {r['name']}\n{r['summary']}\n\nФакты: {r['facts']}"
            for r in results
        )

    if ntype == "kb_rag":
        from server.knowledge import search_kb
        query = (cfg.get("query", "{{input}}") or "{{input}}").replace("{{input}}", input_text)
        top = int(cfg.get("top", 5) or 5)
        model = cfg.get("model", "claude")
        results = search_kb(ctx["bot"].id, query, top)
        if not results:
            return "В базе знаний нет релевантной информации по запросу."
        context_text = "\n\n".join(
            f"[Файл: {r['name']}]\nОписание: {r['description']}\nРезюме: {r['summary']}\nФакты: {r['facts']}"
            for r in results
        )
        prompt = (
            "Ответь на вопрос пользователя используя ТОЛЬКО контекст ниже. "
            "Если ответа нет в контексте — скажи об этом прямо.\n\n"
            f"КОНТЕКСТ:\n{context_text}\n\n"
            f"ВОПРОС: {query}"
        )
        result = generate_response(model, [
            {"role": "system", "content": "Ты ассистент, отвечающий строго по предоставленному контексту."},
            {"role": "user", "content": prompt},
        ])
        return result.get("content", "") if isinstance(result, dict) else str(result)

    # ── Роль / Секция ──────────────────────────────────────────────────────
    if ntype == "role_switch":
        field = cfg.get("field", "chat_id")
        default_role = cfg.get("default", "chat")
        # Карта roles: "name=prompt" (prompt может быть многострочным, но парсим по строкам с =)
        roles_map = {}
        current_role = None
        for line in (cfg.get("roles") or "").splitlines():
            if "=" in line and line.split("=")[0].strip().replace("_","").replace("-","").isalnum():
                name, prompt = line.split("=", 1)
                current_role = name.strip()
                roles_map[current_role] = prompt.strip()
            elif current_role:
                roles_map[current_role] += "\n" + line

        # Определяем активную роль
        if field == "text_first_word":
            words = input_text.strip().split()
            key = words[0].lower() if words else default_role
        else:
            # Роль хранится per-user в storage
            sk = f"role_{field}_{ctx.get(field, ctx.get('chat_id', 'default'))}"
            key = _storage_get(ctx["bot"].id, sk) or default_role

        active_prompt = roles_map.get(key, roles_map.get(default_role, ""))
        ctx["active_role"] = key
        ctx["active_role_prompt"] = active_prompt
        return input_text

    # ── Code (Python sandbox) ──────────────────────────────────────────────
    if ntype == "code_python":
        code = cfg.get("code", "output = input_text")
        return _run_python_sandbox(code, input_text, ctx)

    # ── Yandex.Disk ────────────────────────────────────────────────────────
    if ntype == "yd_list":
        from server.yandex_disk import yd_list_recent
        token = cfg.get("token") or None
        limit = int(cfg.get("limit", 30) or 30)
        items = await yd_list_recent(token, limit)
        return "\n".join(f"{i.get('path','?')} [{i.get('modified','?')}]" for i in items)

    if ntype == "yd_upload":
        from server.yandex_disk import yd_upload
        import os as _os
        token = cfg.get("token") or None
        remote = (cfg.get("remote") or "").replace("{{input}}", input_text)
        local = (cfg.get("local") or input_text).replace("{{input}}", input_text)
        base = _os.path.dirname(_os.path.abspath(__file__))
        local_abs = _os.path.join(base, local.lstrip("/")) if not _os.path.isabs(local) else local
        r = await yd_upload(token, remote, local_abs)
        import json as _j
        return _j.dumps(r, ensure_ascii=False)

    # ── Grok Search (Web + X) ─────────────────────────────────────────────
    if ntype == "grok_search":
        from server.ai import grok_search_response
        prompt = (cfg.get("prompt") or input_text).replace("{{input}}", input_text)
        enable_web = (cfg.get("enable_web", "да") == "да")
        enable_x = (cfg.get("enable_x", "да") == "да")
        model = cfg.get("model", "grok-4-fast-reasoning")
        # grok_search_response синхронная, оборачиваем в thread
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        result = await loop.run_in_executor(None,
            lambda: grok_search_response(prompt, enable_web, enable_x, model))
        return result.get("content", "") if isinstance(result, dict) else str(result)

    # ── Выходные ноды ──────────────────────────────────────────────────────
    if ntype == "output_tg":
        ctx["final_output"] = input_text
        tg_token = cfg.get("tg_token") or (ctx["bot"].tg_token if hasattr(ctx["bot"], "tg_token") else None)
        tg_chat = cfg.get("tg_chat_id") or ctx.get("chat_id")
        parse_mode = cfg.get("parse_mode", "Markdown")
        if parse_mode == "None": parse_mode = None
        if tg_token and tg_chat and tg_chat != ctx["chat_id"]:
            await send_telegram(tg_token, tg_chat, input_text, parse_mode=parse_mode)
        return input_text

    if ntype == "output_tg_buttons":
        ctx["final_output"] = input_text
        tg_token = cfg.get("tg_token") or (ctx["bot"].tg_token if hasattr(ctx["bot"], "tg_token") else None)
        tg_chat = cfg.get("tg_chat_id") or ctx.get("chat_id")
        buttons = []
        for line in (cfg.get("buttons") or "").splitlines():
            line = line.strip()
            if "=" in line:
                text, data = line.split("=", 1)
                buttons.append({"text": text.strip(), "callback_data": data.strip()})
        if tg_token and tg_chat and buttons:
            await send_telegram_with_buttons(tg_token, tg_chat, input_text, buttons)
        return input_text

    if ntype == "output_tg_file":
        tg_token = cfg.get("tg_token") or (ctx["bot"].tg_token if hasattr(ctx["bot"], "tg_token") else None)
        tg_chat = cfg.get("tg_chat_id") or ctx.get("chat_id")
        path = (cfg.get("file_path") or "").replace("{{input}}", input_text)
        caption = (cfg.get("caption") or "").replace("{{input}}", input_text)
        # Резолв public-asset URL в локальный путь.
        # Юзер задаёт в воркфлоу: https://aiche.ru/assets/public/<token> или /assets/public/<token>
        # → ищем StoredAsset по public_token и подставляем .path
        path = _resolve_asset_url_to_path(path)
        # Если path пуст и есть found_files в ctx — отправить их
        if not path and ctx.get("found_files"):
            for f in ctx["found_files"][:3]:
                if tg_token and tg_chat and f.get("path"):
                    await send_telegram_document(tg_token, tg_chat, f["path"], f.get("name", ""))
        elif tg_token and tg_chat and path:
            await send_telegram_document(tg_token, tg_chat, path, caption)
        return input_text

    if ntype == "output_tg_audio":
        tg_token = cfg.get("tg_token") or (ctx["bot"].tg_token if hasattr(ctx["bot"], "tg_token") else None)
        tg_chat = cfg.get("tg_chat_id") or ctx.get("chat_id")
        path = (cfg.get("file_path") or ctx.get("audio_path") or "").replace("{{input}}", input_text)
        if tg_token and tg_chat and path:
            await send_telegram_audio(tg_token, tg_chat, path)
        return input_text

    if ntype == "output_save":
        ctx["final_output"] = input_text
        return input_text

    if ntype == "output_hook":
        url = cfg.get("url", "")
        if url:
            try:
                await HTTP.post(url, json={
                    "text": input_text,
                    "chat_id": ctx["chat_id"],
                    "platform": ctx["platform"],
                }, timeout=10)
            except Exception as e:
                log.warning(f"[Webhook output] {url}: {e}")
        ctx["final_output"] = input_text
        return input_text

    if ntype == "output_vk":
        ctx["final_output"] = input_text
        vk_token = cfg.get("vk_token") or (ctx["bot"].vk_token if hasattr(ctx["bot"], "vk_token") else None)
        vk_uid = cfg.get("vk_user_id")
        if vk_token and vk_uid:
            await send_vk(vk_token, vk_uid, input_text)
        return input_text

    if ntype == "output_max":
        ctx["final_output"] = input_text
        max_token = cfg.get("max_token") or (ctx["bot"].max_token if hasattr(ctx["bot"], "max_token") else None)
        max_uid = cfg.get("max_user_id") or ctx.get("max_user_id") or ctx.get("chat_id")
        if max_token and max_uid:
            await send_max(max_token, max_uid, input_text)
        return input_text

    # ── Новые ноды для богатого UX в TG/MAX ───────────────────────────────
    if ntype == "request_contact":
        # Просим юзера поделиться телефоном через reply-keyboard.
        # Когда юзер нажмёт «Поделиться номером» — мессенджер пришлёт
        # сообщение с contact объектом, движок сохранит телефон в ctx.
        ctx["final_output"] = input_text
        prompt_text = cfg.get("prompt") or "Поделитесь, пожалуйста, номером телефона:"
        button_text = cfg.get("button") or "📞 Поделиться номером"
        platform = ctx.get("platform")
        if platform == "tg":
            tg_token = cfg.get("tg_token") or ctx["bot"].tg_token
            tg_chat = ctx.get("chat_id")
            if tg_token and tg_chat:
                await send_telegram_with_reply_keyboard(
                    tg_token, tg_chat, prompt_text,
                    [{"text": button_text, "request_contact": True}],
                )
        elif platform == "max":
            max_token = cfg.get("max_token") or ctx["bot"].max_token
            max_uid = ctx.get("max_user_id") or ctx.get("chat_id")
            if max_token and max_uid:
                await send_max_with_reply_keyboard(
                    max_token, max_uid, prompt_text,
                    [{"text": button_text, "request_contact": True}],
                )
        return prompt_text

    if ntype == "request_location":
        ctx["final_output"] = input_text
        prompt_text = cfg.get("prompt") or "Поделитесь, пожалуйста, геолокацией:"
        button_text = cfg.get("button") or "📍 Отправить локацию"
        platform = ctx.get("platform")
        if platform == "tg":
            tg_token = cfg.get("tg_token") or ctx["bot"].tg_token
            tg_chat = ctx.get("chat_id")
            if tg_token and tg_chat:
                await send_telegram_with_reply_keyboard(
                    tg_token, tg_chat, prompt_text,
                    [{"text": button_text, "request_location": True}],
                )
        elif platform == "max":
            max_token = cfg.get("max_token") or ctx["bot"].max_token
            max_uid = ctx.get("max_user_id") or ctx.get("chat_id")
            if max_token and max_uid:
                await send_max_with_reply_keyboard(
                    max_token, max_uid, prompt_text,
                    [{"text": button_text, "request_geolocation": True}],
                )
        return prompt_text

    if ntype == "output_photo":
        # Отправить картинку с подписью. URL или путь /uploads/... ИЛИ asset-public-URL.
        # Работает в TG и MAX, в widget — отправляем как обычное сообщение со ссылкой.
        ctx["final_output"] = input_text
        photo_url = (cfg.get("photo_url") or cfg.get("url") or "").strip()
        # Если это public-asset URL — резолвим в локальный путь
        photo_url = _resolve_asset_url_to_path(photo_url)
        caption = (cfg.get("caption") or input_text or "")
        if not photo_url:
            return input_text
        platform = ctx.get("platform")
        if platform == "tg":
            tg_token = cfg.get("tg_token") or ctx["bot"].tg_token
            tg_chat = cfg.get("tg_chat_id") or ctx.get("chat_id")
            if tg_token and tg_chat:
                await send_telegram_photo(tg_token, tg_chat, photo_url, caption)
        elif platform == "max":
            max_token = cfg.get("max_token") or ctx["bot"].max_token
            max_uid = ctx.get("max_user_id") or ctx.get("chat_id")
            if max_token and max_uid:
                await send_max_photo(max_token, max_uid, photo_url, caption)
        return input_text

    if ntype == "edit_message":
        # Заменить текст ранее отправленного сообщения (только TG; в MAX no-op).
        # cfg.message_id или ctx["last_message_id"] — id сообщения для редактирования.
        # cfg.text — новый текст (плейсхолдер {{input}} → input_text).
        ctx["final_output"] = input_text
        if ctx.get("platform") != "tg":
            return input_text  # MAX не поддерживает editMessageText
        tg_token = cfg.get("tg_token") or ctx["bot"].tg_token
        tg_chat = ctx.get("chat_id")
        msg_id = cfg.get("message_id") or ctx.get("last_message_id")
        new_text = (cfg.get("text") or input_text).replace("{{input}}", input_text)
        if tg_token and tg_chat and msg_id:
            await edit_telegram_message(tg_token, tg_chat, msg_id, new_text)
        return input_text

    if ntype == "save_record":
        # Сохраняет заявку/бронь/заказ/опрос в bot_records.
        # cfg:
        #   record_type: "lead" | "booking" | "order" | "quiz" | "ticket" | "subscriber"
        #   notify_owner: bool — уведомить владельца в TG
        #   payload_keys: список ключей из ctx, которые попадут в payload
        #                 (по умолчанию — всё, что начинается с "form_")
        # ctx-ключи берутся: customer_name, customer_phone, customer_email,
        # + ctx["form_*"] (например form_service, form_date) для payload.

        # В preview-режиме НЕ сохраняем — тесты не должны засорять bot_records.
        if ctx.get("is_preview"):
            return cfg.get("ack_text") or "✓ (превью) Заявка была бы сохранена."

        from server.models import BotRecord as _BR
        rec_type = (cfg.get("record_type") or "lead").strip()
        # Собираем payload — либо явный список ключей, либо все form_*
        payload_keys = cfg.get("payload_keys") or [
            k for k in ctx.keys() if isinstance(k, str) and k.startswith("form_")
        ]
        payload = {k.replace("form_", "", 1): ctx.get(k) for k in payload_keys if ctx.get(k) is not None}
        # Если пусто — кладём само сообщение юзера для контекста
        if not payload:
            payload = {"text": (input_text or "")[:500]}

        try:
            with SessionLocal() as _db:
                rec = _BR(
                    bot_id=ctx["bot"].id,
                    user_id=ctx["bot"].user_id,
                    chat_id=str(ctx.get("chat_id", "")),
                    record_type=rec_type,
                    customer_name=(ctx.get("customer_name") or ctx.get("user_name") or "")[:200],
                    customer_phone=(ctx.get("customer_phone") or "")[:50],
                    customer_email=(ctx.get("customer_email") or "")[:200],
                    payload=json.dumps(payload, ensure_ascii=False),
                    status="new",
                )
                _db.add(rec); _db.commit(); _db.refresh(rec)
                ctx["last_record_id"] = rec.id
                log.info(f"[save_record] bot={ctx['bot'].id} type={rec_type} id={rec.id}")
        except Exception as e:
            log.error(f"[save_record] failed: {e}")
            return f"⚠ Не удалось сохранить заявку. Попробуйте ещё раз."

        # Уведомление владельцу: только если у бота есть TG-токен
        # (даже если бот работает в MAX — владелец чаще всего хочет в TG).
        if cfg.get("notify_owner"):
            owner_chat = (cfg.get("owner_tg_chat_id") or "").strip()
            owner_token = ctx["bot"].tg_token if hasattr(ctx["bot"], "tg_token") else None
            if owner_token and owner_chat:
                try:
                    pretty_payload = "\n".join(f"  • *{k}:* {v}" for k, v in payload.items())
                    msg = (
                        f"🔔 *Новая {rec_type}* от бота «{ctx['bot'].name}»\n\n"
                        f"👤 {ctx.get('customer_name') or '(имя не указано)'}\n"
                        f"📞 {ctx.get('customer_phone') or '(телефон не указан)'}\n"
                        f"{pretty_payload}\n\n"
                        f"_id записи: {ctx.get('last_record_id')}_"
                    )
                    await send_telegram(owner_token, owner_chat, msg, parse_mode="Markdown")
                except Exception as e:
                    log.warning(f"[save_record] owner notify failed: {e}")

        return cfg.get("ack_text") or "✓ Заявка принята! Мы скоро свяжемся."

    if ntype == "chat_action_typing":
        # Показать «бот печатает…» перед длинным AI-вызовом.
        # Не блокирует — просто шлёт action и идём дальше.
        if ctx.get("platform") == "tg":
            tg_token = ctx["bot"].tg_token if hasattr(ctx["bot"], "tg_token") else None
            tg_chat = ctx.get("chat_id")
            if tg_token and tg_chat:
                await send_telegram_chat_action(tg_token, tg_chat, "typing")
        return input_text

    if ntype == "output_max_buttons":
        # Inline-кнопки в MAX — аналог output_tg_buttons.
        # cfg.buttons формат: «Да=yes\nНет=no» (по строке на кнопку).
        ctx["final_output"] = input_text
        max_token = cfg.get("max_token") or (ctx["bot"].max_token if hasattr(ctx["bot"], "max_token") else None)
        max_uid = cfg.get("max_user_id") or ctx.get("max_user_id") or ctx.get("chat_id")
        buttons = []
        for line in (cfg.get("buttons") or "").splitlines():
            line = line.strip()
            if "=" in line:
                text, data = line.split("=", 1)
                buttons.append({"text": text.strip(), "callback_data": data.strip()})
        if max_token and max_uid and buttons:
            await send_max(max_token, max_uid, input_text, buttons=buttons)
        elif max_token and max_uid:
            # Если кнопки не заданы — отправим обычным сообщением
            await send_max(max_token, max_uid, input_text)
        return input_text

    # ── Bot-constructor: создаёт дочерний бот по диалогу с клиентом ──────
    if ntype == "bot_constructor":
        # Юзер платформы (например, владелец салона) общается с этим
        # «бот-конструктором» в TG/MAX, описывает задачу. Когда говорит
        # «готово»/«создавай»/«/build» — мы зовём workflow_builder, делаем
        # дочерний ChatBot и возвращаем юзеру ссылку/инструкцию.
        # Триггеры build (case-insensitive): /build, /готово, «давай создавай», «всё, создай»
        text_lower = (input_text or "").strip().lower()
        BUILD_TRIGGERS = ("/build", "/готово", "/done", "создавай", "давай создадим",
                          "поехали", "всё готово", "всё, создай", "создай бота")
        is_build_cmd = any(t in text_lower for t in BUILD_TRIGGERS) or text_lower.startswith("/build")
        if not is_build_cmd:
            # Просто продолжаем диалог — отдаём управление обычному AI-ноду дальше по графу.
            return input_text

        # Достаём всю историю диалога — это и есть «описание задачи»
        history = ctx.get("history") or []
        if not history:
            return ("Расскажите подробнее, какого бота вам нужно: тематика, "
                    "услуги, как принимать заявки. Когда расскажете — напишите «/build».")

        # Строим текстовый дайджест диалога для workflow_builder
        digest_lines = []
        for turn in history[-30:]:
            role = turn.get("role", "user")
            content = (turn.get("content") or "").strip()
            if not content:
                continue
            digest_lines.append(f"{'Клиент' if role == 'user' else 'Конструктор'}: {content}")
        digest = "\n".join(digest_lines)
        if len(digest) > 4000:
            digest = digest[-4000:]

        # Зовём workflow_builder + создаём ChatBot. Владелец = владелец parent-бота
        # (для MVP без OAuth-привязки клиента-салона к платформе).
        try:
            from server.workflow_builder import build_from_task
            from server.billing import deduct_atomic, get_balance
            from server.models import ChatBot as _CB

            owner_user_id = ctx["bot"].user_id
            with SessionLocal() as _db:
                if get_balance(_db, owner_user_id) < 500:
                    return "❌ У владельца платформы закончились средства (минимум 5 ₽ на сборку). Пополните баланс."
                # Лимит дочерних ботов
                from server.models import User as _User
                owner = _db.query(_User).filter_by(id=owner_user_id).first()
                if owner:
                    max_auto = int(getattr(owner, "max_auto_bots", 5) or 5)
                    cnt = _db.query(_CB).filter_by(user_id=owner_user_id, auto_generated=True).count()
                    if cnt >= max_auto:
                        return f"❌ Лимит AI-сгенеренных ботов исчерпан ({max_auto}). Удалите ненужных в /chatbots.html."

                wf = build_from_task(digest)
                # Списываем за сборку по реальным токенам
                usage = wf.get("usage") or {}
                cost_kop = max(50, int(usage.get("input_tokens", 0) / 1000 * 80
                                    + usage.get("output_tokens", 0) / 1000 * 300))
                deduct_atomic(_db, owner_user_id, cost_kop)
                from server.models import Transaction as _Tx
                _db.add(_Tx(user_id=owner_user_id, type="usage", tokens_delta=-cost_kop,
                            description=f"Конструктор-бот: создание дочернего ({cost_kop/100:.2f} ₽)",
                            model="claude"))

                bot_name = (wf.get("name") or "AI-бот")[:60]
                child = _CB(
                    user_id=owner_user_id,
                    name=bot_name,
                    model="gpt",
                    workflow_json=json.dumps(wf, ensure_ascii=False),
                    parent_bot_id=ctx["bot"].id,
                    auto_generated=True,
                    status="off",  # пока не подключены каналы — спит
                )
                _db.add(child); _db.commit(); _db.refresh(child)
                child_id = child.id
                expl = wf.get("explanation", "")

            return (
                f"✅ Бот «{bot_name}» создан (id={child_id})!\n\n"
                f"Что собрал AI: {expl[:300]}\n\n"
                f"⚙️ Чтобы запустить — откройте в личном кабинете "
                f"https://aiche.ru/chatbots.html, привяжите токен Telegram "
                f"(от @BotFather) или MAX (от @MasterBot) — webhook поднимется автоматически.\n\n"
                f"Списано: {cost_kop/100:.2f} ₽ за сборку workflow."
            )
        except Exception as e:
            log.error(f"[bot_constructor] failed: {e}")
            return f"❌ Не удалось собрать бота: {e}. Опишите задачу подробнее и попробуйте /build снова."

    # ── Агенты из библиотеки (agent_smm, agent_copywriter, etc.) ──────────
    if ntype.startswith("agent_"):
        agent_id = ntype.replace("agent_", "")
        from server.agent_runner import AGENT_REGISTRY
        agent_def = AGENT_REGISTRY.get(agent_id, {})
        system_prompt = agent_def.get("system_prompt", "Ты полезный ассистент.")
        # Добавляем настройки из cfg
        extra_cfg = "\n".join(f"• {k}: {v}" for k, v in cfg.items() if v and str(v).strip())
        if extra_cfg:
            system_prompt += f"\n\nНастройки:\n{extra_cfg}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": input_text},
        ]
        result = generate_response("gpt", messages)
        return result.get("content", "") if isinstance(result, dict) else str(result)

    # ── Генерация изображений ──────────────────────────────────────────────
    if ntype == "generate_image":
        result = generate_response("dalle", [
            {"role": "user", "content": input_text}
        ])
        return result.get("content", "") if isinstance(result, dict) else str(result)

    # ── Веб-поиск ──────────────────────────────────────────────────────────
    if ntype == "web_search":
        result = generate_response("perplexity", [
            {"role": "user", "content": input_text}
        ])
        return result.get("content", "") if isinstance(result, dict) else str(result)

    # ── Text filter ────────────────────────────────────────────────────────
    if ntype == "text_filter":
        return input_text

    # По умолчанию — прокидываем текст дальше
    log.warning(f"Unknown node type: {ntype}, passing through")
    return input_text


def _collect_downstream(start_id: str, edges: list, out: set):
    """Собрать все ноды ниже start_id по графу (BFS)."""
    stack = [start_id]
    while stack:
        cur = stack.pop()
        for e in edges:
            if e["from"] == cur and e["to"] not in out:
                out.add(e["to"])
                stack.append(e["to"])


def _topo_sort(nodes: list, edges: list) -> list[str] | None:
    """Топологическая сортировка графа. Возвращает None если есть цикл."""
    node_ids = {n["id"] for n in nodes}
    in_degree = {nid: 0 for nid in node_ids}
    adj = defaultdict(list)
    for e in edges:
        if e["from"] in node_ids and e["to"] in node_ids:
            adj[e["from"]].append(e["to"])
            in_degree[e["to"]] = in_degree.get(e["to"], 0) + 1

    queue = [nid for nid in node_ids if in_degree[nid] == 0]
    result = []
    while queue:
        nid = queue.pop(0)
        result.append(nid)
        for neighbor in adj[nid]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    return result if len(result) == len(node_ids) else None


# ══════════════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════════════════════

def _check_daily_limit(bot) -> bool:
    now = datetime.utcnow()
    if bot.replies_reset_at and now >= bot.replies_reset_at:
        bot.replies_today = 0
        bot.replies_reset_at = now.replace(hour=0, minute=0, second=0) + timedelta(days=1)
    if not bot.replies_reset_at:
        bot.replies_reset_at = now.replace(hour=0, minute=0, second=0) + timedelta(days=1)
    return (bot.replies_today or 0) < (bot.max_replies_day or 100)


def _increment_replies(bot):
    from server.db import db_session
    from server.models import ChatBot
    with db_session() as db:
        db_bot = db.query(ChatBot).filter_by(id=bot.id).first()
        if db_bot:
            db_bot.replies_today = (db_bot.replies_today or 0) + 1
            if not db_bot.replies_reset_at:
                db_bot.replies_reset_at = datetime.utcnow().replace(
                    hour=0, minute=0, second=0) + timedelta(days=1)
            db.commit()


def _deduct_bot_usage(bot, usage: dict):
    """Списать стоимость ответа бота по реальным токенам модели (атомарно).

    Применяется маржа ×3 (pricing.ai.reply_margin_pct) — это B2B-наценка
    на API-провайдера. Если у юзера привязан свой API-ключ
    (UserApiKey для нужного провайдера) — берётся скидка
    (pricing.ai.user_key_discount_pct, по умолчанию 20% от обычной цены).
    """
    from server.models import ModelPricing, UsageLog, UserApiKey
    from server.db import db_session
    from server.billing import deduct_atomic
    from server.pricing import get_price
    input_tokens = usage.get("input", 0)
    output_tokens = usage.get("output", 0)
    cached_tokens = usage.get("cached", 0)
    model = usage.get("model", bot.model or "gpt")

    with db_session() as db:
        # Базовая цена (как в chat.py calculate_cost)
        pricing = db.query(ModelPricing).filter_by(model_id=model).first()
        if pricing and (pricing.ch_per_1k_input > 0 or pricing.ch_per_1k_output > 0):
            base_cost = (input_tokens / 1000.0) * pricing.ch_per_1k_input + \
                        (output_tokens / 1000.0) * pricing.ch_per_1k_output
            base_cost = max(int(round(base_cost)), pricing.min_ch_per_req or 1)
        elif pricing and pricing.cost_per_req:
            base_cost = pricing.cost_per_req
        else:
            base_cost = 1  # fallback

        # Маржа: ×3 от базовой цены (или сколько админ задал в pricing).
        margin_pct = int(get_price("ai.reply_margin_pct", default=300))
        cost = max(1, int(base_cost * margin_pct / 100))

        # Скидка если юзер привязал свой API-ключ для этого провайдера
        provider = _model_to_provider(model)
        has_user_key = False
        if provider:
            has_user_key = db.query(UserApiKey).filter_by(
                user_id=bot.user_id, provider=provider).first() is not None
        if has_user_key:
            discount_pct = int(get_price("ai.user_key_discount_pct", default=20))
            cost = max(1, int(cost * discount_pct / 100))

        charged = deduct_atomic(db, bot.user_id, cost)

        own_marker = " [свой ключ]" if has_user_key else ""
        desc = f"Бот «{bot.name}» [{model}]{own_marker}: {input_tokens}→{output_tokens} ток. ({charged/100:.2f} ₽)"
        if charged < cost:
            desc += f" (списано {charged/100:.2f}/{cost/100:.2f} ₽)"

        db.add(Transaction(
            user_id=bot.user_id, type="usage",
            tokens_delta=-charged,
            description=desc, model=model,
        ))
        db.add(UsageLog(
            user_id=bot.user_id, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=cached_tokens, ch_charged=charged,
        ))
        db.commit()


def _model_to_provider(model_id: str) -> str | None:
    """Маппинг model_id → имя провайдера (для лукапа UserApiKey)."""
    m = (model_id or "").lower()
    if "claude" in m: return "anthropic"
    if "gemini" in m or "imagen" in m or "veo" in m or "nano" in m: return "gemini"
    if "grok" in m: return "grok"
    if "perplex" in m: return "perplexity"
    if m.startswith(("gpt", "dalle")) or "dall-e" in m or m == "openai": return "openai"
    return None


def _save_for_summary(bot_id, chat_id, user_text, answer, user_name, platform):
    _recent_chats[bot_id][chat_id].append({
        "user": user_name or chat_id,
        "text": user_text,
        "answer": answer[:200],
        "ts": datetime.utcnow().isoformat(),
        "platform": platform,
    })
    if len(_recent_chats[bot_id][chat_id]) > 50:
        _recent_chats[bot_id][chat_id] = _recent_chats[bot_id][chat_id][-50:]


# ── Пересказ диалогов ────────────────────────────────────────────────────────

async def get_summary(bot) -> dict:
    chats = _recent_chats.get(bot.id, {})
    if not chats:
        return {"summary": "Нет диалогов с момента последнего запуска.", "total_chats": 0, "total_messages": 0}

    total_msgs = sum(len(v) for v in chats.values())
    lines = []
    for chat_id, msgs in list(chats.items())[-10:]:
        user_name = msgs[-1]["user"] if msgs else chat_id
        platform = msgs[-1].get("platform", "?") if msgs else "?"
        lines.append(f"[{platform}] {user_name}: {len(msgs)} сообщ. Последнее: {msgs[-1]['text'][:60]}")

    if total_msgs > 20:
        try:
            prompt = "Дай краткий пересказ диалогов бота:\n\n" + "\n".join(lines)
            result = generate_response("gpt", [
                {"role": "system", "content": "Пиши на русском. Кратко, по делу."},
                {"role": "user", "content": prompt},
            ])
            ai_summary = result.get("content", "") if isinstance(result, dict) else str(result)
        except Exception:
            ai_summary = "\n".join(lines)
    else:
        ai_summary = "\n".join(lines) if lines else "Нет данных."

    return {"summary": ai_summary, "total_chats": len(chats), "total_messages": total_msgs}


# ══════════════════════════════════════════════════════════════════════════════
#  ОТПРАВКА СООБЩЕНИЙ В МЕССЕНДЖЕРЫ
# ══════════════════════════════════════════════════════════════════════════════

async def setup_telegram_webhook(tg_token: str, webhook_url: str) -> dict:
    from server.security import tg_webhook_secret
    secret = tg_webhook_secret(tg_token)
    payload = {"url": webhook_url, "allowed_updates": ["message", "callback_query"]}
    if secret:
        payload["secret_token"] = secret
    try:
        r = await HTTP.post(
            f"https://api.telegram.org/bot{tg_token}/setWebhook",
            json=payload,
        )
        data = r.json()
        log.info(f"[TG] setWebhook → {data}")
        return data
    except Exception as e:
        log.error(f"[TG] setWebhook error: {e}")
        return {"ok": False, "description": str(e)}


async def delete_telegram_webhook(tg_token: str) -> dict:
    try:
        r = await HTTP.post(f"https://api.telegram.org/bot{tg_token}/deleteWebhook")
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


async def send_telegram(token: str, chat_id: str, text: str,
                        reply_to: int = None, parse_mode: str = "Markdown") -> dict:
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode: payload["parse_mode"] = parse_mode
    if reply_to: payload["reply_to_message_id"] = reply_to
    try:
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        return r.json()
    except Exception as e:
        log.error(f"[TG] send error: {e}")
        try:
            payload.pop("parse_mode", None)
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            return r.json()
        except Exception:
            return {"ok": False}


async def send_telegram_with_buttons(token: str, chat_id: str, text: str,
                                     buttons: list) -> dict:
    """Отправить сообщение с inline-кнопками. buttons = [{text, callback_data}, ...]"""
    # Располагаем кнопки по одной в ряд
    keyboard = [[b] for b in buttons]
    payload = {
        "chat_id": chat_id, "text": text,
        "reply_markup": {"inline_keyboard": keyboard},
    }
    try:
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        return r.json()
    except Exception as e:
        log.error(f"[TG buttons] {e}")
        return {"ok": False}


async def send_telegram_with_reply_keyboard(token: str, chat_id: str, text: str,
                                             buttons: list[dict],
                                             one_time: bool = True,
                                             resize: bool = True) -> dict:
    """Reply-keyboard в TG (постоянная клавиатура внизу).

    buttons: list[dict] — каждый элемент:
      {text: "...", request_contact: True} — попросить телефон
      {text: "...", request_location: True} — попросить геолокацию
      {text: "..."} — обычная кнопка-текст (бот получит как text)
    one_time=True — клавиатура исчезнет после первого нажатия.
    """
    try:
        # MAX/TG: кнопки разложены по 1 в ряд для вертикального меню
        keyboard = [[b] for b in buttons]
        payload = {
            "chat_id": str(chat_id),
            "text": text[:4096] or "Выберите вариант:",
            "reply_markup": {
                "keyboard": keyboard,
                "resize_keyboard": resize,
                "one_time_keyboard": one_time,
            },
        }
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        return r.json()
    except Exception as e:
        log.error(f"[TG reply-kb] {e}")
        return {"ok": False}


async def send_telegram_photo(token: str, chat_id: str, photo: str,
                               caption: str = "", parse_mode: str = "Markdown") -> dict:
    """Отправить фото. photo — URL или относительный путь /uploads/...
    Если файл локальный — multipart upload, если URL — TG сам скачает."""
    import os as _os
    try:
        # URL → передаём как поле photo (TG скачает)
        if photo.startswith(("http://", "https://")):
            payload = {"chat_id": str(chat_id), "photo": photo}
            if caption:
                payload["caption"] = caption[:1024]
                payload["parse_mode"] = parse_mode
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendPhoto", json=payload)
            return r.json()
        # Локальный путь
        base = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))  # корень проекта
        abs_path = _os.path.join(base, photo.lstrip("/"))
        if not _os.path.exists(abs_path):
            log.error(f"[TG photo] file not found: {abs_path}")
            return {"ok": False, "description": "file not found"}
        with open(abs_path, "rb") as f:
            files = {"photo": (_os.path.basename(abs_path), f)}
            data = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption[:1024]
                data["parse_mode"] = parse_mode
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                                files=files, data=data)
        return r.json()
    except Exception as e:
        log.error(f"[TG photo] {e}")
        return {"ok": False}


async def edit_telegram_message(token: str, chat_id: str, message_id: int,
                                 text: str, parse_mode: str = "Markdown",
                                 buttons: list[dict] | None = None) -> dict:
    """Заменить текст ранее отправленного сообщения. Когда юзер нажимает
    кнопку «Выбрать дату», заменяем «выбери услугу» на «✓ Услуга: Маникюр»
    вместо нового спама. UX становится приличным."""
    try:
        payload = {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "text": text[:4096],
            "parse_mode": parse_mode,
        }
        if buttons:
            keyboard = [[{"text": b.get("text", "")[:64],
                          "callback_data": str(b.get("callback_data", ""))[:64]}]
                        for b in buttons[:10]]
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/editMessageText",
                            json=payload)
        return r.json()
    except Exception as e:
        log.error(f"[TG edit] {e}")
        return {"ok": False}


async def set_telegram_commands(token: str, commands: list[dict]) -> dict:
    """Установить меню команд бота — то что показывается в меню «/».
    commands: list of {"command": "start", "description": "Начать работу"}.
    Вызывается при деплое, не в каждом ответе."""
    try:
        payload = {"commands": [
            {"command": c.get("command", "").lstrip("/")[:32],
             "description": (c.get("description", "") or "")[:256]}
            for c in commands[:10]
        ]}
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/setMyCommands",
                            json=payload)
        return r.json()
    except Exception as e:
        log.error(f"[TG commands] {e}")
        return {"ok": False}


async def send_telegram_chat_action(token: str, chat_id: str,
                                     action: str = "typing") -> dict:
    """«Бот печатает…» — показывается до 5 сек или до следующего сообщения.
    Вызываем перед длинным AI-вызовом, чтобы юзер не думал что бот завис."""
    try:
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendChatAction",
                            json={"chat_id": str(chat_id), "action": action})
        return r.json()
    except Exception:
        return {"ok": False}


async def send_telegram_document(token: str, chat_id: str, file_path: str,
                                 caption: str = "") -> dict:
    """Отправить документ. file_path — относительно корня проекта."""
    import os as _os
    base = _os.path.dirname(_os.path.abspath(__file__))
    abs_path = _os.path.join(base, file_path.lstrip("/"))
    if not _os.path.exists(abs_path):
        log.error(f"[TG doc] file not found: {abs_path}")
        return {"ok": False}
    try:
        with open(abs_path, "rb") as f:
            files = {"document": (_os.path.basename(abs_path), f)}
            data = {"chat_id": str(chat_id)}
            if caption: data["caption"] = caption
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendDocument",
                                files=files, data=data)
        return r.json()
    except Exception as e:
        log.error(f"[TG doc] {e}")
        return {"ok": False}


# ── MAX (https://max.ru) ─────────────────────────────────────────────────────
# API: https://botapi.max.ru. Auth: Authorization: Bearer <token> (header).
# Раньше было ?access_token=<token> но MAX deprecated этот способ —
# возвращает 401 с code='verify.token'. Docs: https://dev.max.ru/docs-api
# Webhook: POST /subscriptions с {url}. Send: POST /messages?user_id=<>&text=...

MAX_API = "https://botapi.max.ru"


def _max_headers(max_token: str) -> dict:
    """Auth-header для MAX API. Заменил query ?access_token=... после
    deprecation в апреле 2026.

    ВАЖНО: MAX ожидает голый токен в Authorization БЕЗ префикса 'Bearer '
    (несмотря на формулировку их error 'use Authorization header').
    Проверено живьём: с 'Bearer ' → 401, без префикса → 200 OK.
    """
    return {"Authorization": max_token}


async def setup_max_webhook(max_token: str, webhook_url: str) -> dict:
    """Подписать MAX-бота на webhook. Возвращает {ok, description}.
    Требует HTTPS — иначе MAX откажет."""
    if not webhook_url.startswith("https://"):
        log.error(f"[MAX] webhook URL must be HTTPS: {webhook_url[:60]}")
        return {"ok": False, "description": "Webhook URL должен быть HTTPS"}
    try:
        r = await HTTP.post(
            f"{MAX_API}/subscriptions",
            headers=_max_headers(max_token),
            json={"url": webhook_url, "update_types": ["message_created", "message_callback"]},
        )
        try:
            data = r.json() if r.content else {}
        except Exception:
            data = {"raw": r.text[:200]}
        ok = r.status_code == 200
        log.info(f"[MAX] subscribe → {r.status_code} {data}")
        return {"ok": ok, "description": data.get("message", "") if isinstance(data, dict) else "",
                "status_code": r.status_code}
    except Exception as e:
        log.error(f"[MAX] subscribe error: {type(e).__name__}")
        return {"ok": False, "description": type(e).__name__}


async def delete_max_webhook(max_token: str, webhook_url: str | None = None) -> dict:
    """Отписать webhook. Если webhook_url не задан — снимает все подписки бота."""
    try:
        params = {}
        if webhook_url:
            params["url"] = webhook_url
        r = await HTTP.delete(f"{MAX_API}/subscriptions",
                               headers=_max_headers(max_token), params=params)
        return {"ok": r.status_code == 200, "status_code": r.status_code}
    except Exception as e:
        return {"ok": False, "description": type(e).__name__}


async def send_max(max_token: str, user_id: str | int, text: str,
                   format_: str = "markdown",
                   buttons: list[dict] | None = None) -> dict:
    """Отправить сообщение в MAX. user_id — int из update.message.sender.user_id.

    buttons (опц): список dict {text, callback_data} — отправляются как
    inline keyboard (attachment type=inline_keyboard, по докам MAX).
    """
    try:
        params = {"user_id": str(user_id)}
        body = {"text": text[:4000]}
        if format_:
            body["format"] = format_
        if buttons:
            # MAX inline-buttons: payload = массив рядов кнопок.
            # Пока кладём по одной кнопке в ряд (вертикальный список) —
            # MAX-API поддерживает payload как [[btn1, btn2], [btn3]].
            body["attachments"] = [{
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [[{
                        "type": "callback",
                        "text": b.get("text", "")[:64],
                        "payload": str(b.get("callback_data", ""))[:64],
                    }] for b in buttons[:10]]
                }
            }]
        r = await HTTP.post(f"{MAX_API}/messages",
                            headers=_max_headers(max_token),
                            params=params, json=body)
        try:
            data = r.json() if r.content else {}
        except Exception:
            data = {"raw": r.text[:200]}
        if r.status_code != 200:
            log.warning(f"[MAX] send failed {r.status_code}: {data}")
            # 401/403 = токен мёртв (отозван в MAX или удалён бот). Помечаем
            # max_webhook_set=False чтобы UI показал «требует переподключения»
            # и фоновые tick'и не молотили API в холостую.
            if r.status_code in (401, 403):
                _disable_max_bot_for_token(max_token,
                                            f"max_send {r.status_code}")
        return {"ok": r.status_code == 200, "data": data, "status_code": r.status_code}
    except Exception as e:
        log.error(f"[MAX] send error: {type(e).__name__}")
        return {"ok": False, "description": type(e).__name__}


def _disable_max_bot_for_token(max_token: str, reason: str) -> None:
    """Помечает все боты с этим max_token как отвалившиеся."""
    from server.db import db_session
    try:
        with db_session() as db:
            bots = db.query(ChatBot).all()
            updated = 0
            for b in bots:
                if b.max_token == max_token and b.max_webhook_set:
                    b.max_webhook_set = False
                    updated += 1
            if updated:
                db.commit()
                log.warning(f"[MAX] disabled {updated} bot(s) by token: {reason}")
                from server.audit_log import log_action
                log_action("bot.max_disconnected", target_type="bot",
                           level="warn", success=False,
                           details={"reason": reason, "bots_affected": updated})
    except Exception as e:
        log.error(f"[MAX] disable_max_bot error: {type(e).__name__}")


async def send_max_with_reply_keyboard(max_token: str, user_id: str | int, text: str,
                                         buttons: list[dict]) -> dict:
    """Reply-keyboard в MAX (постоянная клавиатура).

    buttons элементы:
      {text: "...", request_contact: True} — попросить телефон
      {text: "...", request_geolocation: True} — попросить локацию
      {text: "..."} — обычная кнопка-текст
    MAX-API использует attachments: type=request_keyboard.
    """
    try:
        params = {"user_id": str(user_id)}
        # Конвертируем в MAX-формат buttons (по 1 в ряд)
        mx_buttons = []
        for b in buttons[:10]:
            row = {"text": b.get("text", "")[:64], "type": "text"}
            if b.get("request_contact"):
                row["type"] = "request_contact"
            elif b.get("request_geolocation") or b.get("request_location"):
                row["type"] = "request_geolocation"
            mx_buttons.append([row])
        body = {
            "text": (text or "Выберите вариант:")[:4000],
            "attachments": [{
                "type": "request_keyboard",
                "payload": {"buttons": mx_buttons},
            }],
        }
        r = await HTTP.post(f"{MAX_API}/messages",
                            headers=_max_headers(max_token),
                            params=params, json=body)
        return {"ok": r.status_code == 200, "status_code": r.status_code}
    except Exception as e:
        log.error(f"[MAX reply-kb] {type(e).__name__}")
        return {"ok": False}


async def send_max_photo(max_token: str, user_id: str | int, photo: str,
                          caption: str = "") -> dict:
    """Отправить фото в MAX. photo — URL (в идеале) или путь /uploads/...
    Для локальных файлов сначала загружаем через POST /uploads → получаем url."""
    import os as _os
    try:
        photo_url = photo
        if not photo.startswith(("http://", "https://")):
            # MAX не принимает multipart напрямую в /messages —
            # нужно сначала залить файл и взять url. Пока fallback на нашу
            # публичную раздачу /uploads — APP_URL должен быть настроен.
            app_url = _os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
            photo_url = f"{app_url}{photo if photo.startswith('/') else '/' + photo}"
        params = {"user_id": str(user_id)}
        body = {
            "text": (caption or "")[:1000],
            "attachments": [{"type": "image", "payload": {"url": photo_url}}],
        }
        r = await HTTP.post(f"{MAX_API}/messages",
                            headers=_max_headers(max_token),
                            params=params, json=body)
        return {"ok": r.status_code == 200, "status_code": r.status_code}
    except Exception as e:
        log.error(f"[MAX photo] {type(e).__name__}")
        return {"ok": False}


async def get_max_me(max_token: str) -> dict:
    """Возвращает {user_id, name, username, ...} бота. Используем для валидации токена."""
    try:
        r = await HTTP.get(f"{MAX_API}/me", headers=_max_headers(max_token))
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.error(f"[MAX] me error: {type(e).__name__}")
    return {}


async def send_telegram_audio(token: str, chat_id: str, file_path: str) -> dict:
    import os as _os
    base = _os.path.dirname(_os.path.abspath(__file__))
    abs_path = _os.path.join(base, file_path.lstrip("/"))
    if not _os.path.exists(abs_path):
        log.error(f"[TG audio] file not found: {abs_path}")
        return {"ok": False}
    try:
        with open(abs_path, "rb") as f:
            files = {"voice": (_os.path.basename(abs_path), f)}
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendVoice",
                                files=files, data={"chat_id": str(chat_id)})
        return r.json()
    except Exception as e:
        log.error(f"[TG audio] {e}")
        return {"ok": False}


# ══════════════════════════════════════════════════════════════════════════════
#  STORAGE (key-value per bot)
# ══════════════════════════════════════════════════════════════════════════════

def _storage_get(bot_id: int, key: str) -> str:
    from server.db import db_session
    from server.models import WorkflowStore
    with db_session() as db:
        row = db.query(WorkflowStore).filter_by(bot_id=bot_id, key=key).first()
        return row.value if row else ""


def _storage_set(bot_id: int, key: str, value: str):
    from server.db import db_session
    from server.models import WorkflowStore
    with db_session() as db:
        row = db.query(WorkflowStore).filter_by(bot_id=bot_id, key=key).first()
        if row:
            row.value = value
        else:
            db.add(WorkflowStore(bot_id=bot_id, key=key, value=value))
        db.commit()


def _storage_push(bot_id: int, key: str, value: str, max_items: int = 100):
    """Пушит в массив (JSON-list), обрезает по max."""
    import json as _json
    from server.db import db_session
    from server.models import WorkflowStore
    with db_session() as db:
        row = db.query(WorkflowStore).filter_by(bot_id=bot_id, key=key).first()
        arr = []
        if row:
            try:
                arr = _json.loads(row.value) if row.value else []
                if not isinstance(arr, list): arr = []
            except Exception:
                arr = []
        arr.insert(0, value)
        arr = arr[:max_items]
        payload = _json.dumps(arr, ensure_ascii=False)
        if row:
            row.value = payload
        else:
            db.add(WorkflowStore(bot_id=bot_id, key=key, value=payload))
        db.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  RSS
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_rss(urls: list, hours: int = 48, limit: int = 30) -> list:
    """Забирает и парсит RSS-ленты. Возвращает отсортированные статьи."""
    import re as _re
    from email.utils import parsedate_to_datetime
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    cutoff = _dt.now(_tz.utc) - _td(hours=hours)
    results = []
    for url in urls:
        try:
            r = await HTTP.get(url, headers={"User-Agent": "Mozilla/5.0 AICHE-bot"},
                               timeout=15, follow_redirects=True)
            if r.status_code != 200: continue
            xml = r.text
            # Очень простой парсер: ищем <item>...</item> (RSS) или <entry> (Atom)
            items = _re.findall(r"<(?:item|entry)[^>]*>(.*?)</(?:item|entry)>", xml, _re.DOTALL | _re.IGNORECASE)
            for item in items[:20]:
                def extract(tag):
                    m = _re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item, _re.DOTALL | _re.IGNORECASE)
                    if not m: return ""
                    txt = m.group(1)
                    txt = _re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", txt, flags=_re.DOTALL)
                    txt = _re.sub(r"<[^>]+>", "", txt)
                    return txt.strip()
                title = extract("title")
                link = extract("link") or _re.search(r'<link[^>]+href="([^"]+)"', item)
                if hasattr(link, "group"): link = link.group(1) if link else ""
                pubdate = extract("pubDate") or extract("published") or extract("updated")
                summary = extract("description") or extract("summary") or extract("content")
                article_date = None
                if pubdate:
                    try:
                        article_date = parsedate_to_datetime(pubdate)
                    except Exception:
                        try:
                            article_date = _dt.fromisoformat(pubdate.replace("Z", "+00:00"))
                        except Exception:
                            pass
                if article_date and article_date < cutoff:
                    continue
                if title and link:
                    results.append({
                        "title": title[:200], "link": link[:300],
                        "summary": summary[:400],
                        "date": (article_date or _dt.now(_tz.utc)).strftime("%Y-%m-%d %H:%M"),
                        "_sort": (article_date or _dt.now(_tz.utc)).timestamp(),
                    })
        except Exception as e:
            log.warning(f"[RSS] {url}: {e}")
            continue
    results.sort(key=lambda x: x["_sort"], reverse=True)
    return results[:limit]


# ══════════════════════════════════════════════════════════════════════════════
#  FILE EXTRACT (TXT / PDF / DOCX)
# ══════════════════════════════════════════════════════════════════════════════

def _extract_text_from_file(file_path: str) -> str:
    """Извлечь текст из файла. Поддерживает txt, pdf, docx."""
    import os as _os
    base = _os.path.dirname(_os.path.abspath(__file__))
    abs_path = _os.path.join(base, file_path.lstrip("/")) if not _os.path.isabs(file_path) else file_path
    if not _os.path.exists(abs_path):
        return f"[Файл не найден: {file_path}]"
    ext = _os.path.splitext(abs_path)[1].lower()
    try:
        if ext in (".txt", ".md", ".csv", ".json"):
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()[:20000]
        if ext == ".pdf":
            try:
                from PyPDF2 import PdfReader
                reader = PdfReader(abs_path)
                text = "\n\n".join((p.extract_text() or "") for p in reader.pages)
                return text[:20000]
            except Exception as e:
                return f"[PDF error: {e}]"
        if ext == ".docx":
            try:
                import zipfile
                # XXE-защита: используем defusedxml если установлен, иначе строим
                # безопасный parser вручную (без external entities)
                try:
                    from defusedxml.ElementTree import parse as _safe_parse  # type: ignore
                except ImportError:
                    import xml.etree.ElementTree as ET
                    # Парсер с отключенными внешними сущностями
                    def _safe_parse(f):
                        parser = ET.XMLParser()
                        # Явно блокируем DOCTYPE — если встретим, бросим ошибку.
                        # xml.etree не expands external entities по умолчанию, но DTD
                        # всё равно парсится; отключаем на всякий случай.
                        try:
                            parser.parser.DefaultHandler = lambda data: None
                            parser.parser.EntityDeclHandler = lambda *a, **k: (_ for _ in ()).throw(ValueError("entities disabled"))
                        except Exception:
                            pass
                        return ET.parse(f, parser=parser)
                with zipfile.ZipFile(abs_path) as z:
                    with z.open("word/document.xml") as f:
                        tree = _safe_parse(f)
                text = "\n".join(el.text or "" for el in tree.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
                return text[:20000]
            except Exception as e:
                return f"[DOCX error: {e}]"
        return f"[Неподдерживаемый формат: {ext}]"
    except Exception as e:
        return f"[Ошибка извлечения: {e}]"


# ══════════════════════════════════════════════════════════════════════════════
#  WHISPER / TTS (OpenAI)
# ══════════════════════════════════════════════════════════════════════════════

async def _whisper_transcribe(file_path: str) -> str:
    """Транскрибировать аудио через OpenAI Whisper."""
    import os as _os
    from server.ai import _get_api_keys
    keys = _get_api_keys("openai")
    if not keys:
        return "[Whisper: нет OpenAI ключей]"
    base = _os.path.dirname(_os.path.abspath(__file__))
    abs_path = _os.path.join(base, file_path.lstrip("/")) if not _os.path.isabs(file_path) else file_path
    if not _os.path.exists(abs_path):
        return f"[Файл не найден: {file_path}]"
    try:
        with open(abs_path, "rb") as f:
            files = {"file": (_os.path.basename(abs_path), f, "audio/mpeg")}
            data = {"model": "whisper-1"}
            r = await HTTP.post(
                "https://api.openai.com/v1/audio/transcriptions",
                files=files, data=data,
                headers={"Authorization": f"Bearer {keys[0]}"},
                timeout=120,
            )
        if r.status_code == 200:
            return r.json().get("text", "")
        return f"[Whisper error {r.status_code}: {r.text[:200]}]"
    except Exception as e:
        return f"[Whisper exception: {e}]"


async def _tts_generate(text: str, voice: str = "onyx") -> str:
    """Генерирует речь через OpenAI TTS, возвращает путь к файлу."""
    import os as _os, uuid as _uuid
    from server.ai import _get_api_keys
    keys = _get_api_keys("openai")
    if not keys:
        return ""
    try:
        r = await HTTP.post(
            "https://api.openai.com/v1/audio/speech",
            json={"model": "tts-1", "voice": voice, "input": text[:4000], "response_format": "mp3"},
            headers={"Authorization": f"Bearer {keys[0]}"},
            timeout=60,
        )
        if r.status_code != 200:
            log.error(f"[TTS] {r.status_code}: {r.text[:200]}")
            return ""
        base = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        upload_dir = _os.path.join(base, "uploads")
        _os.makedirs(upload_dir, exist_ok=True)
        fname = f"tts_{_uuid.uuid4().hex[:12]}.mp3"
        path = _os.path.join(upload_dir, fname)
        with open(path, "wb") as f:
            f.write(r.content)
        return f"/uploads/{fname}"
    except Exception as e:
        log.error(f"[TTS] {e}")
        return ""


async def send_vk(token: str, user_id: str, text: str) -> dict:
    import random
    try:
        r = await HTTP.post("https://api.vk.com/method/messages.send", data={
            "user_id": user_id, "message": text,
            "random_id": random.randint(1, 2**31),
            "access_token": token, "v": "5.131",
        })
        return r.json()
    except Exception as e:
        log.error(f"[VK] send error: {e}")
        return {"error": str(e)}


_avito_tokens: dict[int, tuple[str, float]] = {}

async def _get_avito_token(bot) -> str | None:
    if not bot.avito_client_id or not bot.avito_client_secret:
        return None
    cached = _avito_tokens.get(bot.id)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        r = await HTTP.post("https://api.avito.ru/token/", data={
            "grant_type": "client_credentials",
            "client_id": bot.avito_client_id,
            "client_secret": bot.avito_client_secret,
        })
        data = r.json()
        token = data.get("access_token")
        expires_in = data.get("expires_in", 3600)
        if token:
            _avito_tokens[bot.id] = (token, time.time() + expires_in - 60)
        return token
    except Exception as e:
        log.error(f"[Avito] token error: {e}")
        return None


async def send_avito(bot, chat_id: str, text: str) -> dict:
    token = await _get_avito_token(bot)
    if not token:
        return {"error": "No Avito token"}
    try:
        r = await HTTP.post(
            f"https://api.avito.ru/messenger/v1/accounts/{bot.avito_user_id}/chats/{chat_id}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"message": {"text": text}, "type": "text"},
        )
        return r.json()
    except Exception as e:
        log.error(f"[Avito] send error: {e}")
        return {"error": str(e)}


def generate_widget_secret() -> str:
    return secrets.token_urlsafe(16)


# ══════════════════════════════════════════════════════════════════════════════
#  RAG MARKERS — автообработка [KB_SEARCH]/[FILE_SEARCH]/[EMAIL_CONTEXT] в AI-ответе
# ══════════════════════════════════════════════════════════════════════════════

async def _resolve_rag_markers(answer: str, ctx: dict, model: str) -> str:
    """
    Если в ответе AI содержится маркер [KB_SEARCH: query] / [FILE_SEARCH: query] /
    [EMAIL_CONTEXT: query] — делаем поиск и заменяем маркер на результат.
    Для KB — дополнительно просим AI переформулировать ответ с учётом контекста.
    """
    import re as _re
    if not answer: return answer

    bot_id = ctx["bot"].id if ctx.get("bot") else None
    if not bot_id: return answer

    # [FILE_SEARCH: ...] — найти файл и отметить для отправки
    file_match = _re.search(r"\[FILE_SEARCH:\s*(.+?)\]", answer)
    if file_match:
        from server.knowledge import search_file
        q = file_match.group(1).strip()
        results = search_file(bot_id, q, top=1)
        if results:
            ctx["found_files"] = results  # для отправки через output_tg_file
            replacement = f"Отправляю файл: {results[0]['name']}"
            answer = answer.replace(file_match.group(0), replacement)
        else:
            answer = answer.replace(file_match.group(0), f"Файл не найден по запросу «{q}».")

    # [KB_SEARCH: ...] — найти контекст и вызвать AI повторно
    kb_match = _re.search(r"\[KB_SEARCH:\s*(.+?)\]", answer)
    if kb_match:
        from server.knowledge import search_kb
        q = kb_match.group(1).strip()
        results = search_kb(bot_id, q, top=5)
        if results:
            context_text = "\n\n".join(
                f"[{r['name']}] {r['summary']}\nФакты: {r['facts']}" for r in results
            )
            prompt = (
                f"Используя ТОЛЬКО контекст ниже, ответь на исходный вопрос пользователя.\n\n"
                f"КОНТЕКСТ:\n{context_text}\n\n"
                f"ВОПРОС: {q}\n\n"
                f"Предыдущая заготовка ответа (замени её):\n{answer}"
            )
            try:
                result = generate_response(model, [
                    {"role": "system", "content": "Ты отвечаешь строго по контексту. Без маркеров."},
                    {"role": "user", "content": prompt},
                ])
                answer = result.get("content", "") if isinstance(result, dict) else str(result)
            except Exception as e:
                log.error(f"[KB_SEARCH resolve] {e}")
                answer = answer.replace(kb_match.group(0), context_text[:500])
        else:
            answer = answer.replace(kb_match.group(0), "[в базе знаний не найдено]")

    # [EMAIL_CONTEXT: ...] — найти письма через storage.emails
    em_match = _re.search(r"\[EMAIL_CONTEXT:\s*(.+?)\]", answer)
    if em_match:
        import json as _j
        q = em_match.group(1).strip()
        emails_raw = _storage_get(bot_id, "emails") or "[]"
        try:
            emails = _j.loads(emails_raw) if isinstance(emails_raw, str) else []
        except Exception:
            emails = []
        ql = q.lower()
        matched = [e for e in emails if isinstance(e, dict) and
                   (ql in (e.get("from","")).lower() or ql in (e.get("subject","")).lower()
                    or ql in (e.get("body","")).lower())][:5]
        if matched:
            ctx_text = "\n\n".join(
                f"From: {m.get('from','')}\nSubject: {m.get('subject','')}\n{(m.get('body','') or '')[:500]}"
                for m in matched
            )
            try:
                result = generate_response(model, [
                    {"role": "system", "content": "Отвечай по контексту писем."},
                    {"role": "user", "content": f"Контекст писем:\n{ctx_text}\n\nВопрос: {q}"},
                ])
                ai_resp = result.get("content", "") if isinstance(result, dict) else str(result)
                answer = answer.replace(em_match.group(0), ai_resp)
            except Exception:
                answer = answer.replace(em_match.group(0), ctx_text[:500])
        else:
            answer = answer.replace(em_match.group(0), "[писем не найдено]")

    return answer


# ══════════════════════════════════════════════════════════════════════════════
#  PYTHON SANDBOX (ограниченный)
# ══════════════════════════════════════════════════════════════════════════════

_PY_SANDBOX_FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "__import__",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "input", "breakpoint", "exit", "quit", "help",
    "memoryview", "bytearray", "bytes", "type", "object",
    "super", "classmethod", "staticmethod", "property",
}

# Whitelist разрешённых AST-узлов. Всё что НЕ в списке — отвергается.
# Намеренно убраны: ClassDef, FunctionDef, AsyncFunctionDef, Lambda
#   (можно скрыть в них escape: class X: __init_subclass__ etc.),
# While (бесконечные циклы), Yield/YieldFrom, AsyncFor/AsyncWith,
# GeneratorExp без bound, Global, Nonlocal, Try (можно поглотить ошибку
# и продолжить вредоносный код), Import*, JoinedStr/FormattedValue
# (через f-string легче проворачивать атаки), Starred (распаковка может
# взорвать память), MatchClass, MatchStar.
import ast as _ast
_PY_SANDBOX_ALLOWED_NODES = {
    _ast.Module, _ast.Expression, _ast.Expr,
    _ast.Assign, _ast.AugAssign, _ast.AnnAssign,
    _ast.For, _ast.If, _ast.Pass, _ast.Break, _ast.Continue,
    _ast.Return, _ast.BoolOp, _ast.BinOp, _ast.UnaryOp, _ast.Compare,
    _ast.Call, _ast.IfExp, _ast.Subscript, _ast.Attribute,
    _ast.Name, _ast.Load, _ast.Store, _ast.Del,
    _ast.Constant, _ast.List, _ast.Tuple, _ast.Set, _ast.Dict,
    _ast.ListComp, _ast.SetComp, _ast.DictComp,
    _ast.comprehension, _ast.Slice,
    _ast.And, _ast.Or, _ast.Not,
    _ast.Add, _ast.Sub, _ast.Mult, _ast.Div, _ast.FloorDiv, _ast.Mod,
    _ast.LShift, _ast.RShift, _ast.BitOr, _ast.BitAnd, _ast.BitXor,
    _ast.UAdd, _ast.USub, _ast.Invert,
    _ast.Eq, _ast.NotEq, _ast.Lt, _ast.LtE, _ast.Gt, _ast.GtE,
    _ast.Is, _ast.IsNot, _ast.In, _ast.NotIn,
    _ast.keyword, _ast.arguments, _ast.arg,
}

# Мягкие ограничения, чтобы остановить тривиальные ресурсные атаки
_PY_SANDBOX_MAX_CODE_LEN = 4000          # символов исходника
_PY_SANDBOX_MAX_NODES = 250              # узлов AST
_PY_SANDBOX_MAX_INT_LITERAL = 10**6      # литерал-числа
_PY_SANDBOX_TIMEOUT_SEC = 2              # wallclock timeout (Linux only)


def _ast_validate_python(code: str) -> str | None:
    """
    Возвращает текст ошибки, если код содержит запрещённые конструкции.

    Whitelist-подход: разрешаем только явно перечисленные узлы AST.
    Это резко режет поверхность атаки, но не делает sandbox безопасным.
    Полная безопасность достижима только через subprocess + seccomp.
    """
    import ast
    if len(code) > _PY_SANDBOX_MAX_CODE_LEN:
        return f"Код слишком длинный (>{_PY_SANDBOX_MAX_CODE_LEN} символов)"
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return f"Синтаксическая ошибка: {e}"

    nodes = list(ast.walk(tree))
    if len(nodes) > _PY_SANDBOX_MAX_NODES:
        return f"Слишком сложный код (>{_PY_SANDBOX_MAX_NODES} узлов AST)"

    for node in nodes:
        if type(node) not in _PY_SANDBOX_ALLOWED_NODES:
            return f"Запрещённая конструкция: {type(node).__name__}"
        if isinstance(node, ast.Attribute):
            # Запрещаем dunder-доступ (.__class__, .__bases__, ...)
            # и любые приватные атрибуты на всякий случай.
            if node.attr.startswith("_"):
                return f"Доступ к скрытым атрибутам ({node.attr}) запрещён"
        if isinstance(node, ast.Name) and node.id in _PY_SANDBOX_FORBIDDEN_NAMES:
            return f"Использование {node.id} запрещено"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _PY_SANDBOX_FORBIDDEN_NAMES:
                return f"Вызов {node.func.id}() запрещён"
        # Защита от 'a' * 10**9 / int литерал-бомб
        if isinstance(node, ast.Constant):
            if isinstance(node.value, int) and abs(node.value) > _PY_SANDBOX_MAX_INT_LITERAL:
                return f"Слишком большое число ({node.value})"
            if isinstance(node.value, str) and len(node.value) > 10000:
                return "Строковый литерал слишком длинный"
        # Запрещаем степень — `2 ** 100000` мгновенно съест CPU/память
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            return "Оператор ** запрещён в sandbox"
    return None


def _run_python_sandbox(code: str, input_text: str, ctx: dict) -> str:
    """
    Выполняет пользовательский Python код. По умолчанию ВЫКЛЮЧЕН
    (ENABLE_PYTHON_SANDBOX=true чтобы включить).

    Даже с AST-валидацией exec() не безопасен — это RCE-вектор.
    Включать только в доверенной среде, например для self-hosted
    инсталляций где владелец = единственный автор воркфлоу.
    """
    import json as _j
    import os as _os
    if _os.getenv("ENABLE_PYTHON_SANDBOX", "false").lower() not in ("1", "true", "yes"):
        return "[Python sandbox выключен. Установите ENABLE_PYTHON_SANDBOX=true]"

    err = _ast_validate_python(code)
    if err:
        return f"[Python sandbox: {err}]"

    log.warning(f"[Python sandbox] executing user code (len={len(code)})")
    safe_globals = {
        "__builtins__": {
            "len": len, "range": range, "str": str, "int": int, "float": float,
            "bool": bool, "list": list, "dict": dict, "tuple": tuple, "set": set,
            "sum": sum, "min": min, "max": max, "abs": abs, "round": round,
            "sorted": sorted, "reversed": reversed, "enumerate": enumerate, "zip": zip,
            "map": map, "filter": filter, "print": lambda *a, **k: None,
            "True": True, "False": False, "None": None,
            "isinstance": isinstance, "any": any, "all": all,
        },
        "json": _j,
        "re": __import__("re"),
        "datetime": __import__("datetime"),
    }
    ctx_copy = {k: v for k, v in ctx.items() if k not in ("bot", "history")}
    safe_locals = {
        "input_text": input_text,
        "ctx": ctx_copy,
        "output": "",
    }

    # Wallclock timeout через signal.alarm — работает только на Linux,
    # только в главном потоке. На прочих платформах просто выполняется без таймера.
    import signal as _sig
    _has_alarm = hasattr(_sig, "SIGALRM")
    _old_handler = None
    if _has_alarm:
        def _on_timeout(_sig_num, _frame):
            raise TimeoutError(f"Превышен лимит {_PY_SANDBOX_TIMEOUT_SEC}с")
        try:
            _old_handler = _sig.signal(_sig.SIGALRM, _on_timeout)
            _sig.alarm(_PY_SANDBOX_TIMEOUT_SEC)
        except (ValueError, OSError):
            # signal вне главного потока — не критично, продолжаем без таймера
            _has_alarm = False
            _old_handler = None
    try:
        exec(code, safe_globals, safe_locals)
        out = safe_locals.get("output", "")
        if not isinstance(out, str):
            try: out = _j.dumps(out, ensure_ascii=False)
            except Exception: out = str(out)
        return out[:10000]
    except TimeoutError as e:
        log.error(f"[Python sandbox] timeout: {e}")
        return f"[Python sandbox: {e}]"
    except Exception as e:
        log.error(f"[Python sandbox] error: {type(e).__name__}")
        return f"[Ошибка Python: {type(e).__name__}]"
    finally:
        if _has_alarm:
            try:
                _sig.alarm(0)
                if _old_handler is not None:
                    _sig.signal(_sig.SIGALRM, _old_handler)
            except (ValueError, OSError):
                pass
