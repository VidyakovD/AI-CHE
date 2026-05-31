"""Тесты YAML manifest loader + пилотных манифестов (kp_agent, lawyer).

См. server/agents/yaml_loader.py и server/agents/manifests/*.yaml.
"""
import os
import textwrap

import pytest


# ── register_agent: новый параметр category ────────────────────────────────


class TestRegisterAgentCategory:
    def test_category_param_stored(self):
        from server.agent_runner import register_agent, AGENT_REGISTRY
        register_agent(
            agent_id="_test_cat_module",
            name="Тестовый",
            description="—",
            keywords=["a"],
            category="dev",
        )
        try:
            assert AGENT_REGISTRY["_test_cat_module"].get("category") == "dev"
        finally:
            AGENT_REGISTRY.pop("_test_cat_module", None)

    def test_category_omitted_means_none(self):
        from server.agent_runner import register_agent, AGENT_REGISTRY
        register_agent(
            agent_id="_test_no_cat",
            name="Тестовый",
            description="—",
            keywords=["a"],
        )
        try:
            assert AGENT_REGISTRY["_test_no_cat"].get("category") is None
        finally:
            AGENT_REGISTRY.pop("_test_no_cat", None)


# ── Loader ─────────────────────────────────────────────────────────────────


def _write_yaml(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class TestYamlLoader:
    def test_loads_valid_manifest(self, tmp_path):
        from server.agents.yaml_loader import load_manifests_from_dir
        from server.agent_runner import AGENT_REGISTRY

        _write_yaml(str(tmp_path / "module_a.yaml"), textwrap.dedent("""
            agent_id: _test_loader_a
            name: A
            description: тест
            category: dev
            keywords:
              - test
            system_prompt: |
              Multi
              line
        """).strip())

        count = load_manifests_from_dir(str(tmp_path))
        try:
            assert count == 1
            entry = AGENT_REGISTRY["_test_loader_a"]
            assert entry["name"] == "A"
            assert entry["category"] == "dev"
            assert "test" in entry["keywords"]
            assert entry["system_prompt"].startswith("Multi")
        finally:
            AGENT_REGISTRY.pop("_test_loader_a", None)

    def test_skips_missing_required_fields(self, tmp_path, caplog):
        from server.agents.yaml_loader import load_manifests_from_dir
        _write_yaml(str(tmp_path / "bad.yaml"), "name: NoId\ndescription: x\n")
        count = load_manifests_from_dir(str(tmp_path))
        assert count == 0

    def test_handles_broken_yaml(self, tmp_path):
        from server.agents.yaml_loader import load_manifests_from_dir
        _write_yaml(str(tmp_path / "broken.yaml"),
                    "this is: not\n  - valid: [yaml broken")
        count = load_manifests_from_dir(str(tmp_path))
        assert count == 0  # не raise, просто 0 загружено

    def test_loads_multiple_files(self, tmp_path):
        from server.agents.yaml_loader import load_manifests_from_dir
        from server.agent_runner import AGENT_REGISTRY

        for slug in ("_test_multi_x", "_test_multi_y"):
            _write_yaml(str(tmp_path / f"{slug}.yaml"), textwrap.dedent(f"""
                agent_id: {slug}
                name: {slug}
                description: тест
                keywords: [k]
            """).strip())

        count = load_manifests_from_dir(str(tmp_path))
        try:
            assert count == 2
            assert "_test_multi_x" in AGENT_REGISTRY
            assert "_test_multi_y" in AGENT_REGISTRY
        finally:
            AGENT_REGISTRY.pop("_test_multi_x", None)
            AGENT_REGISTRY.pop("_test_multi_y", None)

    def test_missing_directory_returns_zero(self, tmp_path):
        from server.agents.yaml_loader import load_manifests_from_dir
        # Несуществующая директория — не raise
        count = load_manifests_from_dir(str(tmp_path / "does_not_exist"))
        assert count == 0

    def test_non_yaml_files_ignored(self, tmp_path):
        from server.agents.yaml_loader import load_manifests_from_dir
        _write_yaml(str(tmp_path / "readme.txt"), "agent_id: x")
        _write_yaml(str(tmp_path / "config.json"), '{"agent_id": "y"}')
        count = load_manifests_from_dir(str(tmp_path))
        assert count == 0


# ── Пилоты в проде (после import registry) ─────────────────────────────────


class TestPilotManifests:
    """Проверяет что kp_agent и lawyer реально зарегистрированы из YAML."""

    def test_kp_agent_loaded_with_category(self):
        import server.agents.registry  # noqa: F401 (ensure registry loaded)
        from server.agent_runner import AGENT_REGISTRY
        entry = AGENT_REGISTRY.get("kp_agent")
        assert entry is not None
        assert entry["name"] == "Агент КП"
        assert entry["category"] == "docs"
        assert "коммерческое предложение" in entry["keywords"]
        # system_prompt должен содержать ключевые маркеры из исходного блока
        assert "ЗАГОЛОВОК" in entry["system_prompt"]
        assert "CTA" in entry["system_prompt"]

    def test_lawyer_loaded_with_category(self):
        import server.agents.registry  # noqa: F401
        from server.agent_runner import AGENT_REGISTRY
        entry = AGENT_REGISTRY.get("lawyer")
        assert entry is not None
        assert entry["name"] == "Юрист"
        assert entry["category"] == "docs"
        assert "договор" in entry["keywords"]
        assert "АНАЛИЗ ДОГОВОРА" in entry["system_prompt"]
        assert "ПРЕТЕНЗИЯ" in entry["system_prompt"]
        # allowed_tools должен включать web_search
        assert "web_search" in (entry.get("allowed_tools") or [])

    def test_registry_total_count_31(self):
        """Регрессия: после миграции 2 модулей общее число всё ещё 31."""
        import server.agents.registry  # noqa: F401
        from server.agent_runner import AGENT_REGISTRY
        # 31 — текущее число модулей в проде. Если меняется — обнови число.
        # Считаем только не-тестовые (без префикса _test).
        real = [k for k in AGENT_REGISTRY if not k.startswith("_test")]
        assert len(real) >= 31, (
            f"Ожидаем минимум 31 модуля, найдено {len(real)}: "
            f"{sorted(real)}"
        )


# ── Категория fallback в routes ────────────────────────────────────────────


class TestCategoryFallback:
    def test_slug_category_from_manifest(self):
        """Пилот из YAML с category=docs → возвращает docs."""
        import server.agents.registry  # noqa: F401
        from server.routes.agents_modular import _slug_category
        assert _slug_category("lawyer") == "docs"
        assert _slug_category("kp_agent") == "docs"

    def test_slug_category_fallback_to_map(self):
        """Не-мигрированный модуль (smm) без category → из _CATEGORY_MAP."""
        import server.agents.registry  # noqa: F401
        from server.routes.agents_modular import _slug_category
        # smm есть в _CATEGORY_MAP с "content"
        assert _slug_category("smm") == "content"

    def test_slug_category_unknown_returns_other(self):
        from server.routes.agents_modular import _slug_category
        assert _slug_category("definitely_not_a_real_slug") == "other"
