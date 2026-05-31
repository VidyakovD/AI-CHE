"""YAML manifest loader для модулей ИИ Агентов.

Альтернатива Python-вызовам `register_agent()` — третьи разработчики
смогут писать модули как YAML файлы без знания Python (под marketplace v2.0).

═══ ФОРМАТ MANIFEST.YAML ═══

```yaml
agent_id: lawyer                # обязательное — уникальный slug
name: Юрист                     # обязательное — отображаемое имя
description: Юр. консультации   # обязательное — для классификатора
category: docs                  # опц. — content|marketing|docs|analytics|...
version: "1.0"                  # опц. — для будущего marketplace
keywords:                       # обязательное — keyword-routing
  - юрист
  - договор
allowed_tools:                  # опц. — whitelist tool-имён
  - run_llm
  - web_search
  - write_output
  - finish
system_prompt: |                # обязательное (literal multiline)
  Полный системный промпт
  на несколько строк
skills:                         # опц. — Итерация 4
  - slug: contract_review
    name: Ревью договора
    description: Проверка договора на риски
    price_delta_kop: 200
    tools: [file_extract]
    prompt_addon: "Если есть файл договора..."
settings_schema:                # опц. — UI рендерит форму
  - key: jurisdiction
    label: Юрисдикция
    type: select
    options: [RU, EU, US]
    default: RU
```

═══ ИСПОЛЬЗОВАНИЕ ═══

Loader вызывается из `server/agents/registry.py` после всех Python
register_agent() — YAML манифесты регистрируются последними и могут
переопределять Python-блоки (по slug совпадение).

```python
from server.agents.yaml_loader import load_manifests_from_dir
count = load_manifests_from_dir("server/agents/manifests")
```

Loader устойчив к ошибкам: битый YAML / отсутствие обязательных полей
→ skip с warning, остальные манифесты загружаются.
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_REQUIRED_FIELDS = ("agent_id", "name", "description", "keywords")


def _load_manifest_file(path: str) -> dict | None:
    """Парсит один YAML-файл. Возвращает dict или None при ошибке."""
    try:
        import yaml
    except ImportError:
        log.error(f"[manifest] PyYAML не установлен — пропуск {path}")
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as e:
        log.warning(f"[manifest] {path}: parse error: {type(e).__name__}: {e}")
        return None
    if not isinstance(data, dict):
        log.warning(f"[manifest] {path}: top-level не dict — skip")
        return None
    return data


def _register_from_manifest(data: dict, source: str = "") -> bool:
    """Зарегистрировать модуль из распарсенного манифеста. True если успех."""
    # Валидация обязательных полей
    missing = [f for f in _REQUIRED_FIELDS if not data.get(f)]
    if missing:
        log.warning(f"[manifest] {source}: missing required fields "
                    f"{missing} — skip")
        return False

    # keywords должен быть списком
    kws = data.get("keywords")
    if not isinstance(kws, list) or not kws:
        log.warning(f"[manifest] {source}: keywords must be non-empty list — skip")
        return False

    # Импорт здесь (а не наверху) — избегаем circular import при тестах
    from server.agent_runner import register_agent

    try:
        register_agent(
            agent_id=str(data["agent_id"]),
            name=str(data["name"]),
            description=str(data["description"]),
            keywords=[str(k) for k in kws],
            system_prompt=data.get("system_prompt"),
            allowed_tools=(list(data["allowed_tools"])
                           if isinstance(data.get("allowed_tools"), list)
                           else None),
            skills=(list(data["skills"])
                    if isinstance(data.get("skills"), list) else None),
            settings_schema=(list(data["settings_schema"])
                             if isinstance(data.get("settings_schema"), list)
                             else None),
            category=data.get("category"),
        )
        return True
    except Exception as e:
        log.warning(f"[manifest] {source}: register_agent failed: "
                    f"{type(e).__name__}: {e}")
        return False


def load_manifests_from_dir(directory: str) -> int:
    """Прочитать все *.yaml/*.yml в директории, зарегистрировать каждый.

    Returns: количество успешно загруженных манифестов.
    Битые файлы / без обязательных полей → skip + warning (не raise).
    """
    if not os.path.isdir(directory):
        log.info(f"[manifest] dir {directory} not found — skip")
        return 0

    loaded = 0
    for fname in sorted(os.listdir(directory)):
        if not (fname.endswith(".yaml") or fname.endswith(".yml")):
            continue
        path = os.path.join(directory, fname)
        if not os.path.isfile(path):
            continue
        data = _load_manifest_file(path)
        if data is None:
            continue
        if _register_from_manifest(data, source=fname):
            loaded += 1

    log.info(f"[manifest] loaded {loaded} manifest(s) from {directory}")
    return loaded
