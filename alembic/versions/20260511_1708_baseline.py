"""baseline — точка отсчёта для всех будущих миграций.

Намеренно пустая: схема уже собрана через server/db.py:Base.metadata.create_all()
+ LIGHTWEIGHT_MIGRATIONS. Baseline просто маркирует «отсюда работаем».

На уже-задеплоенной БД (где LIGHTWEIGHT_* отработали и схема актуальна):
    alembic stamp head     # помечает текущую БД как up-to-date

На свежей dev-БД:
    server.db.init_db()    # создаёт схему через ORM (как раньше)
    alembic stamp head     # маркирует head ревизию

После этого все НОВЫЕ изменения схемы — через
`alembic revision --autogenerate -m "slug"`.

Revision ID: 506fa9eb9a82
Create Date: 2026-05-11
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401


revision: str = '506fa9eb9a82'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Намеренно пусто. См. docstring.
    pass


def downgrade() -> None:
    # Откат baseline невозможен (нечего откатывать).
    pass
