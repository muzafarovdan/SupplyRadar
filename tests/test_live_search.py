"""Сквозные сценарии живого поиска на изолированных источниках."""

from __future__ import annotations

from pathlib import Path

from src.config import Settings
from src.fetching import FetchedPage, FetchOutcome, clean_html
from src.models import SearchRequest, SourceStatus
from src.pipeline import SupplierSearchPipeline
from src.search.live import LiveSupplierProvider
from src.storage import Storage
from src.web_search import SearchHit, WebSearchOutcome

FIXTURES = Path(__file__).parent / "fixtures"


class BrokenPlaces:
    name = "broken-2gis"

    def collect(self, request, queries, progress):
        raise RuntimeError("VPN blocks API")


class StaticWebSearch:
    def __init__(self, hit: SearchHit) -> None:
        self._hit = hit

    async def search(self, queries, target):
        return WebSearchOutcome(
            hits=[self._hit], provider_stats={"DuckDuckGo": "ссылок 1, ошибок 0"}
        )


class StaticFetcher:
    def __init__(self, fixture: str) -> None:
        html = (FIXTURES / fixture).read_text(encoding="utf-8")
        self._page = FetchedPage(
            url="https://supplier.example/product",
            final_url="https://supplier.example/product",
            status=SourceStatus.OK,
            http_status=200,
            mime_type="text/html",
            html=html,
            text=clean_html(html),
        )

    def bind_storage(self, storage):
        self.storage = storage

    async def fetch(self, urls, deadline_seconds=None):
        return FetchOutcome(pages=[self._page])


def _pipeline(tmp_path, fixture: str) -> SupplierSearchPipeline:
    settings = Settings(
        _env_file=None,
        two_gis_api_key=None,
        clear_spending_enabled=False,
        productcenter_enabled=False,
        eis_enabled=False,
        duckduckgo_fallback_enabled=False,
        database_path=tmp_path / "cache.db",
    )
    live = LiveSupplierProvider(
        settings,
        places_provider=BrokenPlaces(),
        web_search=StaticWebSearch(
            SearchHit(
                url="https://supplier.example/product",
                title="Морозко — поставщик продуктов",
                snippet="поисковый сниппет",
                query='"филе куриное" оптом Екатеринбург',
                provider="DuckDuckGo",
                rank=1,
            )
        ),
        fetcher=StaticFetcher(fixture),
    )
    return SupplierSearchPipeline(
        settings=settings,
        provider_factory=lambda request, config: live,
        storage=Storage(settings.database_path),
    )


def _request() -> SearchRequest:
    return SearchRequest(
        category="филе куриное",
        region="Екатеринбург",
    )


def test_web_search_continues_when_two_gis_is_unavailable(tmp_path):
    result = _pipeline(tmp_path, "live_supplier_product.html").run(_request())

    assert [item.supplier.name for item in result.suppliers] == ["Морозко"]
    assert result.candidates == []
    assert any("2ГИС недоступен" in warning for warning in result.warnings)
    assert result.stats.web_hits_found == 1
    assert result.stats.provider_stats["DuckDuckGo"].startswith("ссылок 1")


def test_unconfirmed_web_result_remains_visible_as_candidate(tmp_path):
    result = _pipeline(tmp_path, "live_supplier_general.html").run(_request())

    assert result.suppliers == []
    assert [item.supplier.name for item in result.candidates] == ["Морозко"]
    assert "не подтверждено" in (result.candidates[0].verification_reason or "")
