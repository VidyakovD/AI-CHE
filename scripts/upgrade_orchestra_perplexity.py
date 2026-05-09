"""
Усиливает существующие orchestra-пилоты через Perplexity:
заменяет первый stage type=web_search на type=perplexity_research
с большими лимитами и recency-фильтрами. Остальные stages
(parallel_llm, synthesize) остаются без изменений — они работают
с тем же placeholder'ом {{<stage_id>.output}}, а данные от Perplexity
будут более свежими и с цитатами.

Идемпотентен: если уже мигрировано (нашли perplexity_research stage
с пометкой `_v2_perplexity: true` в meta) — пропускаем.

Использование:
    python scripts/upgrade_orchestra_perplexity.py [--force]
      --force — перенакатить даже если уже мигрировано

Какие пилоты усиливаем:
1. Конкурентный анализ ниши — recency=year (компании / сайты / отзывы)
2. SWOT-анализ — recency=year (рыночные тренды и угрозы)
3. Аудит лендинга — НЕ трогаем (анализирует HTML, не нужен веб-поиск)
4. Юр. проверка договора — добавляем НОВЫЙ stage perplexity_research
   "свежая судебная практика" (recency=year)
5. Контент-план месяц — recency=month (тренды свежие)
6. Аудит соцсети — НЕ трогаем (анализирует посты)
7. Финансовый аудит Excel — НЕ трогаем (анализирует файл)
8. Холодная email-рассылка — добавляем stage с контекстом получателя
   (recency=month)
"""
import os, sys, json, argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import db_session
from server.models import Solution


# Какие пилоты усиливаем + конфиг для их перепаковки
UPGRADES = {
    "Конкурентный анализ ниши": {
        # Заменяем первый web_search stage целиком на perplexity_research.
        # Остальные stages (analyzer_X / synthesizer / mermaid) работают как было.
        "replace_first_websearch": True,
        "perplexity_query": (
            "Найди топ-7 конкурентов в нише: {{input}}. По каждому: "
            "сайт, цена, целевая аудитория, УТП, основные слабости из отзывов, "
            "недавние новости/изменения. Минимум 15 источников с ссылками."
        ),
        "recency": "year",
    },
    "Полный SWOT-анализ бизнеса": {
        "replace_first_websearch": True,
        "perplexity_query": (
            "Проведи рыночный анализ для бизнеса: {{input}}. "
            "Опиши 5 главных трендов отрасли за последний год, "
            "5 главных рисков/угроз, 3-5 регуляторных изменений если есть, "
            "ключевых конкурентов и их движения. Минимум 12 источников с ссылками."
        ),
        "recency": "year",
    },
    "Контент-план на месяц для соцсетей": {
        "replace_first_websearch": True,
        "perplexity_query": (
            "Найди топ-15 трендов в нише {{input}} за последний месяц: "
            "обсуждаемые темы в соцсетях, новости, события, мемы, "
            "реакции аудитории. Что взлетает, что устарело. "
            "С источниками (VC.ru, телеграм-каналы, Pikabu, тематические СМИ)."
        ),
        "recency": "month",
    },
    "Юридическая проверка договора": {
        # У этого пилота web_search возможно нет — добавляем НОВЫЙ stage
        # в начало, помеченный как perplexity для свежей практики.
        "prepend_stage": {
            "id": "fresh_practice",
            "type": "perplexity_research",
            "label": "⚖️ Свежая судебная практика",
            "model": "sonar-pro",
            "search_context": "high",
            "max_tokens": 6000,
            "search_recency_filter": "year",
            "fix_price_kop": 0,  # стоимость уже включена в общую цену пилота
            "query": (
                "Найди свежую (последний год) судебную практику РФ "
                "по ключевым типам рисков в договорах вида: {{input}}. "
                "Цитаты из решений ВС РФ и арбитражных судов с ссылками. "
                "Какие пункты договоров суды толкуют чаще всего против слабой стороны."
            ),
        },
    },
    "Холодная email-рассылка под список компаний": {
        "prepend_stage": {
            "id": "company_context",
            "type": "perplexity_research",
            "label": "🔍 Контекст получателей",
            "model": "sonar-pro",
            "search_context": "medium",
            "max_tokens": 4000,
            "search_recency_filter": "month",
            "fix_price_kop": 0,
            "query": (
                "Найди свежий контекст для холодной рассылки в нише: {{input}}. "
                "Что обсуждается в индустрии за последний месяц, "
                "точки боли клиентов, типичные возражения, что цепляет внимание. "
                "С 8+ источниками."
            ),
        },
    },
}


_PPL_DEFAULTS = {
    "type": "perplexity_research",
    "model": "sonar-reasoning-pro",
    "search_context": "high",
    "max_tokens": 12000,
    "temperature": 0.2,
    "fix_price_kop": 0,  # стоимость в общей цене пилота
}


def _is_already_upgraded(orch: dict) -> bool:
    """Проверка — уже мигрировано? (есть хотя бы один stage с
    type=perplexity_research помеченный _upgraded_v2)."""
    for s in orch.get("stages", []):
        if s.get("type") == "perplexity_research" and s.get("_upgraded_v2"):
            return True
    return False


def _replace_first_websearch(orch: dict, query: str, recency: str) -> bool:
    """Найти первый stage type=web_search и заменить его на perplexity_research.
    Сохраняет id и label, чтобы placeholder'ы из других stages не сломались.
    """
    for i, s in enumerate(orch.get("stages", [])):
        if s.get("type") == "web_search":
            new_stage = dict(_PPL_DEFAULTS)
            new_stage["id"] = s.get("id")
            new_stage["label"] = s.get("label", "🔬 Глубокий ресёрч")
            new_stage["query"] = query
            new_stage["search_recency_filter"] = recency
            new_stage["_upgraded_v2"] = True
            orch["stages"][i] = new_stage
            return True
    return False


def _prepend_stage(orch: dict, stage_cfg: dict) -> bool:
    """Добавить stage в начало списка. Если stage с таким id уже есть
    — пропускаем (идемпотентно)."""
    sid = stage_cfg.get("id")
    if any(s.get("id") == sid for s in orch.get("stages", [])):
        return False  # уже есть
    new_stage = dict(stage_cfg)
    new_stage["_upgraded_v2"] = True
    # Заполняем дефолты
    for k, v in _PPL_DEFAULTS.items():
        new_stage.setdefault(k, v)
    orch.setdefault("stages", []).insert(0, new_stage)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Перенакатить даже если уже мигрировано")
    args = parser.parse_args()

    upgraded = 0
    skipped = 0
    not_found = []

    with db_session() as db:
        for title, cfg in UPGRADES.items():
            sol = db.query(Solution).filter(Solution.title == title).first()
            if not sol:
                not_found.append(title)
                continue
            if not sol.orchestra_json:
                print(f"  ⊘ {title}: нет orchestra_json (legacy plain) — пропуск")
                skipped += 1
                continue
            orch = json.loads(sol.orchestra_json)
            if _is_already_upgraded(orch) and not args.force:
                print(f"  = {title}: уже мигрировано")
                skipped += 1
                continue

            applied = False
            if cfg.get("replace_first_websearch"):
                ok = _replace_first_websearch(
                    orch, cfg["perplexity_query"], cfg["recency"])
                if ok:
                    applied = True
                else:
                    print(f"  ⊘ {title}: не нашли web_search stage для замены")
            if cfg.get("prepend_stage"):
                ok = _prepend_stage(orch, cfg["prepend_stage"])
                if ok:
                    applied = True

            if applied:
                sol.orchestra_json = json.dumps(orch, ensure_ascii=False)
                # Тэгируем
                tags = set((sol.tags or "").split(","))
                tags.discard("")
                tags.add("perplexity-deep")
                sol.tags = ",".join(sorted(tags))
                upgraded += 1
                print(f"  ✓ {title}")
        db.commit()

    print()
    print(f"Готово: усилено {upgraded}, пропущено {skipped}.")
    if not_found:
        print(f"Не найдены ({len(not_found)}): {', '.join(not_found)}")


if __name__ == "__main__":
    main()
