"""Интеграционные проверки живого пайплайна на изолированных источниках."""

from __future__ import annotations

import pytest

from src.config import Settings
from src.models import Evidence, SearchRequest, SourceRecord, SourceStatus
from src.pipeline import STAGES, SupplierSearchPipeline
from src.search import ProviderResult, SupplierProvider
from src.storage import Storage


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp_path / "cache.db",
        export_dir=tmp_path / "exports",
        clear_spending_enabled=False,
        productcenter_enabled=False,
        eis_enabled=False,
        duckduckgo_fallback_enabled=False,
    )


@pytest.fixture
def request_frozen() -> SearchRequest:
    return SearchRequest(
        category="замороженные сырники",
        region="Екатеринбург",
        required_volume_kg=50,
        delivery_required=True,
        documents_required=True,
    )


class StaticProvider(SupplierProvider):
    name = "static"

    def __init__(self, result: ProviderResult | None = None) -> None:
        self.result = result or ProviderResult()

    def collect(self, request, queries, progress):
        return self.result


def test_progress_reports_every_stage(settings, request_frozen):
    messages: list[str] = []
    pipeline = SupplierSearchPipeline(
        settings=settings,
        provider_factory=lambda request, config: StaticProvider(),
    )

    pipeline.run(request_frozen, progress=messages.append)

    for index, stage in enumerate(STAGES):
        assert any(message.startswith(f"{index + 1}/6 {stage}") for message in messages)


def test_broken_provider_does_not_stop_run(settings, request_frozen):
    class BrokenProvider(StaticProvider):
        name = "broken"

        def collect(self, request, queries, progress):
            raise RuntimeError("сеть недоступна")

    pipeline = SupplierSearchPipeline(
        settings=settings,
        provider_factory=lambda request, config: BrokenProvider(),
    )

    result = pipeline.run(request_frozen)

    assert result.suppliers == []
    assert any("сеть недоступна" in warning for warning in result.warnings)


def test_live_search_without_external_sources_still_finishes(settings):
    result = SupplierSearchPipeline(settings=settings).run(
        SearchRequest(category="замороженные сырники")
    )

    assert result.suppliers == []
    assert result.stats.provider_stats["2ГИС"].startswith("отключен")


def test_run_is_saved_to_storage(settings, request_frozen):
    source = SourceRecord(
        url="https://supplier.example/product",
        domain="supplier.example",
        status=SourceStatus.OK,
        discovery_query="сырники оптом",
        provider="test-provider",
    )
    storage = Storage(settings.database_path)
    pipeline = SupplierSearchPipeline(
        settings=settings,
        provider_factory=lambda request, config: StaticProvider(
            ProviderResult(sources=[source])
        ),
        storage=storage,
    )

    result = pipeline.run(request_frozen)
    runs = storage.list_runs()
    sources = storage.list_sources(result.stats.run_id)

    assert runs[0]["id"] == result.stats.run_id
    assert runs[0]["request"]["category"] == "замороженные сырники"
    assert sources[0]["status"] == "ok"
    assert sources[0]["provider"] == "test-provider"


def test_unverified_organization_is_visible_as_candidate(settings, make_supplier):
    supplier = make_supplier(
        product_categories=["Мясо птицы и полуфабрикаты"],
        discovery_queries=["филе куриное Екатеринбург"],
    )
    storage = Storage(settings.database_path)
    pipeline = SupplierSearchPipeline(
        settings=settings,
        provider_factory=lambda request, config: StaticProvider(
            ProviderResult(suppliers=[supplier])
        ),
        storage=storage,
    )

    result = pipeline.run(SearchRequest(category="филе куриное", region="Екатеринбург"))

    assert result.suppliers == []
    assert [item.supplier.name for item in result.candidates] == ["Тестовый поставщик"]
    assert result.stats.candidates_found == 1
    assert storage.list_suppliers(result.stats.run_id)[0]["result_group"] == "candidate"


def test_page_evidence_promotes_candidate_to_verified(settings, make_supplier):
    url = "https://supplier.example/product"
    quote = "Филе куриное замороженное оптом"
    supplier = make_supplier(
        product_categories=["Мясо птицы"],
        matched_products=[quote],
        discovery_queries=["филе куриное"],
        source_urls=[url],
        evidence=[
            Evidence(
                field_name="matched_products",
                value=quote,
                source_url=url,
                quote=quote,
                confidence=0.9,
            )
        ],
    )
    pipeline = SupplierSearchPipeline(
        settings=settings,
        provider_factory=lambda request, config: StaticProvider(
            ProviderResult(suppliers=[supplier])
        ),
    )

    result = pipeline.run(SearchRequest(category="филе куриное"))

    assert [item.supplier.name for item in result.suppliers] == ["Тестовый поставщик"]
    assert result.candidates == []
