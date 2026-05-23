#!/usr/bin/env python3
"""CI-страж: проверяет что в каждом `worker_lock(...)` ttl_sec > sleep_duration.

Зачем: worker_lock защищает cron-loop от мульти-воркер дублей. Если ttl_sec
меньше последующего `await asyncio.sleep(N)`, есть окно когда лок истёк
(safety net на crash), но воркер ещё работает — другие могут параллельно
acquire'нуть и сделать дубль работы. Особенно опасно если tick делает
billing/API-вызовы (creators_prepare, agents_modules_cron, storage_billing).

Закрыто массово 2026-05-23 (коммит 5c7176e). Этот скрипт защищает от регрессии.

Парсер по AST с поддержкой выражений (3600 * 23, 86400 + 300).
Печатает в формате GitHub Actions для inline-аннотаций в PR.

Запуск:
    python tools/check_worker_locks.py
Exit code 0 если все OK, 1 если есть mismatch.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CRON_DIR = REPO_ROOT / "server" / "cron"


def _eval_int(node: ast.expr) -> int | None:
    """Преобразует AST в int. Поддерживает: 60, 3600 * 23, 86400 + 300."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return int(node.value)
    if isinstance(node, ast.BinOp):
        left = _eval_int(node.left)
        right = _eval_int(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
    return None


def _find_worker_lock_calls(tree: ast.AST) -> list[tuple[int, int]]:
    """Возвращает [(lineno, ttl_sec), ...] для всех worker_lock(...) calls."""
    out: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name != "worker_lock":
            continue
        for kw in node.keywords:
            if kw.arg == "ttl_sec":
                val = _eval_int(kw.value)
                if val is not None:
                    out.append((node.lineno, val))
                break
    return out


def _find_following_sleep(source: str, after_lineno: int) -> int | None:
    """Ищет первый `asyncio.sleep(N)` ПОСЛЕ строки after_lineno (в пределах 30 строк).
    Возвращает int(N) или None.
    """
    lines = source.splitlines()
    pattern = re.compile(r"asyncio\.sleep\(\s*([^)]+?)\s*\)")
    for i in range(after_lineno, min(after_lineno + 30, len(lines))):
        line = lines[i] if i < len(lines) else ""
        m = pattern.search(line)
        if not m:
            continue
        expr = m.group(1)
        try:
            val = _eval_int(ast.parse(expr, mode="eval").body)
            if val is not None:
                return val
        except Exception:
            pass
        m2 = re.match(r"^\s*(\d+)\s*$", expr)
        if m2:
            return int(m2.group(1))
        return None
    return None


def check_file(path: pathlib.Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return [f"::error file={path}::SyntaxError: {e}"]

    issues: list[str] = []
    for lineno, ttl in _find_worker_lock_calls(tree):
        sleep_dur = _find_following_sleep(source, lineno)
        if sleep_dur is None:
            continue
        if ttl <= sleep_dur:
            rel = path.relative_to(REPO_ROOT)
            issues.append(
                f"::error file={rel},line={lineno}::"
                f"worker_lock TTL race: ttl_sec={ttl} <= sleep({sleep_dur}). "
                f"Поднять ttl_sec > {sleep_dur} "
                f"(рекомендуется +60 для секундных, +300 для часовых)."
            )
    return issues


def main() -> int:
    if not CRON_DIR.exists():
        print(f"::warning::CRON_DIR not found: {CRON_DIR}")
        return 0

    all_issues: list[str] = []
    files_checked = 0
    for py in sorted(CRON_DIR.rglob("*.py")):
        if py.name == "__init__.py":
            continue
        files_checked += 1
        all_issues.extend(check_file(py))

    if all_issues:
        print(f"\n[FAIL] {len(all_issues)} worker_lock TTL races in {files_checked} files:\n")
        for issue in all_issues:
            print(issue)
        return 1

    print(f"[OK] {files_checked} cron files checked, no TTL races.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
