# Запуск из папки backend, где находится pyproject.toml:
# uv sync --frozen
# uv run --frozen pytest tests --junitxml=reports/ci.xml --alluredir=reports/allure/ci
# Открыть отчёт в браузере (нужны Allure CLI и Java; остановка через Ctrl+C):
# allure serve reports/allure/ci
# После короткой команды uv run --frozen pytest tests:
# allure serve reports/allure/local
# HTML-отчёт при необходимости (нужны Allure CLI и Java):
# allure generate reports/allure/ci --single-file --clean -o reports/allure-report
# Это проверки конфигурации. Сценарии проверки runner описаны в tests/README.md.

from typing import Any

import allure
import pytest

pytestmark = pytest.mark.ci


@allure.feature("Конфигурация GitLab CI")
class TestPipeline:
    @allure.story("Запуск pipeline")
    def test_workflow_rules(self, pipeline: dict[str, Any]) -> None:
        with allure.step("Проверяем запуск MR, ветки и тега и приоритет запрета дубликата"):
            assert pipeline["workflow"]["rules"] == [
                {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'},
                {"if": "$CI_COMMIT_BRANCH && $CI_OPEN_MERGE_REQUESTS", "when": "never"},
                {"if": "$CI_COMMIT_BRANCH"},
                {"if": "$CI_COMMIT_TAG"},
            ]

    @allure.story("Обязательные этапы")
    def test_required_jobs(self, pipeline: dict[str, Any]) -> None:
        with allure.step("Проверяем порядок quality → test → build → report"):
            assert pipeline["stages"] == ["quality", "test", "build", "report"]
        for name, stage in (
            ("quality", "quality"),
            ("ci-checks", "test"),
            ("build-package", "build"),
        ):
            with allure.step(f"Job {name} обязателен и ожидает успеха предыдущих этапов"):
                job = pipeline[name]
                assert job["stage"] == stage
                assert job.get("allow_failure", False) is False
                assert job.get("when", "on_success") == "on_success"
                assert "needs" not in job
                assert "rules" not in job

    @allure.story("Зафиксированные зависимости")
    def test_dependency_setup(self, pipeline: dict[str, Any]) -> None:
        for name in ("quality", "ci-checks", "build-package"):
            with allure.step(f"Перед {name} lock проверяется, зависимости ставятся с --frozen"):
                setup = pipeline[name].get("before_script", pipeline["default"]["before_script"])
                assert "pip install uv==0.9.5" in setup
                assert setup.index("cd backend") < setup.index("uv lock --check")
                assert setup.index("uv lock --check") < setup.index("uv sync --frozen")

    @allure.story("Проверки качества")
    def test_quality_commands(self, pipeline: dict[str, Any]) -> None:
        with allure.step("Линтер, форматирование и типы проверяются с передачей ошибок job"):
            assert pipeline["quality"]["script"] == [
                "uv run --frozen ruff check . ../ops --config pyproject.toml",
                "uv run --frozen ruff format --check . ../ops --config pyproject.toml",
                "uv run --frozen mypy app",
            ]

    @allure.story("Подготовка тестовой БД")
    def test_database_preparation(self, pipeline: dict[str, Any]) -> None:
        job = pipeline["ci-checks"]
        with allure.step("Проверяем отдельный PostGIS service и подключение к gis_test"):
            assert {"name": "postgis/postgis:16-3.5", "alias": "db"} in job["services"]
            variables = job["variables"]
            assert variables["POSTGRES_DB"] == "gis_test"
            expected_url = (
                f"postgresql+asyncpg://{variables['POSTGRES_USER']}:"
                f"{variables['POSTGRES_PASSWORD']}@db:5432/gis_test"
            )
            assert variables["TEST_DATABASE_URL"] == expected_url
            assert variables["MIGRATION_DATABASE_URL"] == expected_url
        with allure.step("БД ожидается, затем миграции применяются дважды перед тестами"):
            assert job["script"][:3] == [
                "uv run --frozen python ../ops/wait_for_db.py",
                "uv run --frozen alembic upgrade head",
                "uv run --frozen alembic upgrade head",
            ]

    @allure.story("Запуск всех тестов и доступность результатов")
    def test_tests_and_artifacts(self, pipeline: dict[str, Any]) -> None:
        job = pipeline["ci-checks"]
        with allure.step("Pytest запускает всю папку tests и сохраняет результаты при ошибке"):
            assert job["script"][3:] == [
                "uv run --frozen pytest tests --junitxml=reports/ci.xml "
                "--alluredir=reports/allure/ci"
            ]
            artifacts = job["artifacts"]
            assert artifacts["when"] == "always"
            assert artifacts["reports"]["junit"] == "backend/reports/ci.xml"
            assert {"backend/reports/ci.xml", "backend/reports/allure/ci/"} <= set(
                artifacts["paths"]
            )
        with allure.step("Отчётный job получает результаты тестов и выполняется после ошибки"):
            report = pipeline["allure-report"]
            assert report["stage"] == "report"
            assert report["when"] == "always"
            assert report["dependencies"] == ["ci-checks"]
            assert report["artifacts"]["when"] == "always"
            assert "backend/reports/allure-report/" in report["artifacts"]["paths"]
            script = "\n".join(report["script"])
            assert "allure generate backend/reports/allure/ci --single-file" in script
            assert "-o backend/reports/allure-report" in script

    @allure.story("Сборка и проверка установленного пакета")
    def test_package_build(self, pipeline: dict[str, Any]) -> None:
        job = pipeline["build-package"]
        with allure.step("Пакет собирается, устанавливается отдельно и проходит smoke"):
            assert job["script"] == [
                "uv build --no-sources",
                "uv export --frozen --no-dev --no-emit-project -o /tmp/gis-runtime.txt",
                "uv venv /tmp/gis-package-check",
                "uv pip install --python /tmp/gis-package-check/bin/python "
                "--constraint /tmp/gis-runtime.txt dist/*.whl",
                "cd /tmp",
                '/tmp/gis-package-check/bin/python "$CI_PROJECT_DIR/ops/smoke.py"',
            ]
            assert "backend/dist/" in job["artifacts"]["paths"]
