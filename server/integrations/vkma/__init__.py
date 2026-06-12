"""VKMA surface — VK Mini App интеграция (aichevk.ru) в aiche-кодбейзе.

Импортируем модели чтобы Base.metadata знал про них (важно для
create_all() в init_db и для autogenerate в alembic).

См. vk_saas/MERGE_INTO_AICHE.md для деталей переноса.
"""
from server.integrations.vkma import models  # noqa: F401 — register tables
from server.integrations.vkma import auth    # noqa: F401
