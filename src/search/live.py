"""Оркестрация живого поиска: организации, веб-ссылки и проверка страниц."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

from src.config import Settings
from src.extraction import enrich_supplier, supplier_from_hit
from src.fetching import FetchOutcome, PageFetcher
from src.llm_extraction import EXTRACTION_SCHEMA_VERSION, LlmExtractor
from src.models import (
    CompanyType,
    SearchRequest,
    SourceKind,
    SourceRecord,
    SourceStatus,
    Supplier,
)
from src.normalization import normalize_domain, normalize_url
from src.search.base import ProgressCallback, ProviderResult, SupplierProvider
from src.search.clear_spending import ClearSpendingProvider
from src.search.eis import EisContractProvider
from src.search.two_gis import TwoGisSupplierProvider
from src.storage import Storage
from src.web_search import (
    CompositeWebSearch,
    ProductCenterSearchProvider,
    SearchHit,
    create_web_search,
    deduplicate_hits,
    is_catalog_url,
)


class LiveSupplierProvider(SupplierProvider):
    """Объединяет независимые источники, поэтому один сбой не обнуляет выдачу."""

    name = "живой поиск"

    def __init__(
        self,
        settings: Settings,
        *,
        storage: Storage | None = None,
        places_provider: SupplierProvider | None = None,
        contracts_provider: SupplierProvider | None = None,
        eis_provider: SupplierProvider | None = None,
        web_search: CompositeWebSearch | None = None,
        catalog_search: ProductCenterSearchProvider | None = None,
        fetcher: PageFetcher | None = None,
        llm_extractor: LlmExtractor | None = None,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._places = places_provider
        if (
            self._places is None
            and settings.two_gis_enabled
            and settings.two_gis_api_key
        ):
            self._places = TwoGisSupplierProvider(
                settings.two_gis_api_key,
                base_url=settings.two_gis_base_url,
                timeout_seconds=min(settings.request_timeout_seconds, 10),
            )
        self._contracts = contracts_provider
        if self._contracts is None and settings.clear_spending_enabled:
            self._contracts = ClearSpendingProvider(
                base_url=settings.clear_spending_base_url,
                timeout_seconds=settings.clear_spending_timeout_seconds,
                max_contracts=settings.clear_spending_max_contracts,
                max_suppliers=settings.clear_spending_max_suppliers,
            )
        self._eis = eis_provider
        if self._eis is None and settings.eis_enabled:
            self._eis = EisContractProvider(
                base_url=settings.eis_base_url,
                timeout_seconds=settings.eis_timeout_seconds,
                deadline_seconds=settings.eis_deadline_seconds,
                max_queries=settings.eis_max_queries,
                max_contracts=settings.eis_max_contracts,
                ca_bundle=settings.eis_ca_bundle,
            )
        self._web = web_search or create_web_search(settings)
        self._catalog_web = catalog_search
        if self._catalog_web is None and settings.productcenter_enabled:
            self._catalog_web = ProductCenterSearchProvider()
        self._fetcher = fetcher or PageFetcher(
            storage=storage,
            timeout_seconds=settings.page_timeout_seconds,
            deadline_seconds=settings.live_search_deadline_seconds,
            concurrency=settings.max_concurrent_requests,
            max_domains=settings.max_live_domains,
            max_pages_per_domain=settings.max_pages_per_domain,
            max_response_bytes=settings.max_response_bytes,
            cache_ttl_hours=settings.cache_ttl_hours,
        )
        self._llm = llm_extractor
        if self._llm is None and settings.llm_api_key:
            self._llm = LlmExtractor(
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                base_url=settings.llm_base_url,
                timeout_seconds=settings.llm_timeout_seconds,
                max_suppliers=settings.llm_max_suppliers,
                max_pages_per_supplier=settings.llm_max_pages_per_supplier,
                max_chars_per_supplier=settings.llm_max_chars_per_supplier,
                concurrency=settings.llm_max_concurrent_requests,
            )

    def bind_storage(self, storage: Storage | None) -> None:
        self._storage = storage
        self._fetcher.bind_storage(storage)

    def collect(
        self,
        request: SearchRequest,
        queries: list[str],
        progress: ProgressCallback,
    ) -> ProviderResult:
        result = ProviderResult()
        suppliers: list[Supplier] = []
        started = monotonic()

        if self._places:
            places_queries = _places_queries(request)
            progress("2ГИС: ищем организации короткими запросами")
            try:
                places = self._places.collect(request, places_queries, progress)
                suppliers.extend(places.suppliers)
                result.sources.extend(places.sources)
                result.warnings.extend(places.warnings)
                result.pages_failed += places.pages_failed
                result.provider_stats["2ГИС"] = (
                    f"организаций {len(places.suppliers)}, ошибок {places.pages_failed}"
                )
            except Exception as error:  # noqa: BLE001 - источник изолирован намеренно
                safe_error = str(error).replace(
                    self._settings.two_gis_api_key or "__missing_key__", "***"
                )
                result.warnings.append(
                    f"2ГИС недоступен; веб-поиск продолжен: {safe_error}"
                )
                result.provider_stats["2ГИС"] = "недоступен"
        elif self._settings.two_gis_enabled:
            result.warnings.append(
                "Живой поиск: TWO_GIS_API_KEY не задан, организации ищутся "
                "через реестр контрактов и веб-поиск."
            )
            result.provider_stats["2ГИС"] = "не настроен"
        else:
            result.provider_stats["2ГИС"] = "отключен; используются B2B-источники"

        catalog_hits: list[SearchHit] = []
        discovery_jobs = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            if self._contracts:
                discovery_jobs["contracts"] = executor.submit(
                    self._contracts.collect, request, queries, progress
                )
            if self._catalog_web:
                progress("B2B-каталог: ищем товарные карточки производителей")
                discovery_jobs["catalog"] = executor.submit(
                    asyncio.run,
                    self._catalog_web.search(
                        request.category, max(request.max_results * 2, 12)
                    ),
                )

            try:
                contracts = (
                    discovery_jobs["contracts"].result()
                    if "contracts" in discovery_jobs
                    else None
                )
            except Exception as error:  # noqa: BLE001 - источник изолирован намеренно
                result.warnings.append(
                    "Госзатраты недоступны; остальные источники продолжены: "
                    f"{_safe_error(error)}"
                )
                result.provider_stats["Госзатраты (данные ЕИС)"] = "недоступны"
            else:
                if contracts is not None:
                    suppliers.extend(contracts.suppliers)
                    result.sources.extend(contracts.sources)
                    result.warnings.extend(contracts.warnings)
                    result.pages_failed += contracts.pages_failed
                    result.provider_stats.update(contracts.provider_stats)

            try:
                catalog_hits = (
                    discovery_jobs["catalog"].result()
                    if "catalog" in discovery_jobs
                    else []
                )
            except Exception as error:  # noqa: BLE001 - каталог не роняет поиск
                result.warnings.append(
                    "B2B-каталог ProductCenter временно недоступен: "
                    f"{_safe_error(error)}"
                )
                result.provider_stats["ProductCenter"] = "недоступен"
            else:
                if self._catalog_web:
                    result.provider_stats["ProductCenter"] = (
                        f"товарных карточек {len(catalog_hits)}"
                    )

        if self._eis:
            try:
                eis = self._eis.collect(request, queries, progress)
                suppliers.extend(eis.suppliers)
                result.sources.extend(eis.sources)
                result.warnings.extend(eis.warnings)
                result.pages_failed += eis.pages_failed
                result.provider_stats.update(eis.provider_stats)
            except Exception as error:  # noqa: BLE001 - ЕИС изолирована намеренно
                result.warnings.append(
                    f"ЕИС недоступна; остальные источники продолжены: {_safe_error(error)}"
                )
                result.provider_stats["ЕИС"] = "недоступна"

        search_queries = _web_queries(request, suppliers)
        progress(f"веб-поиск: выполняем {len(search_queries)} запросов")
        try:
            web = asyncio.run(
                asyncio.wait_for(
                    self._web.search(search_queries, max(request.max_results * 3, 15)),
                    timeout=_remaining(
                        started, self._settings.live_search_deadline_seconds
                    ),
                )
            )
        except Exception as error:  # noqa: BLE001 - fallback не должен ронять запуск
            web = None
            result.warnings.append(f"Веб-поиск недоступен: {_safe_error(error)}")

        hits: list[SearchHit] = list(catalog_hits)
        if web:
            hits = deduplicate_hits([*hits, *web.hits])
            result.warnings.extend(web.warnings)
            result.provider_stats.update(web.provider_stats)
            if web.fallback_used:
                result.provider_stats["fallback"] = "использован DuckDuckGo"
        result.web_hits_found = len(hits)
        _attach_hits(suppliers, hits)
        known_ids = {supplier.id for supplier in suppliers}
        for hit in hits:
            if hit.supplier_id not in known_ids:
                suppliers.append(supplier_from_hit(hit))
                known_ids.add(suppliers[-1].id)
        result.sources.extend(_hit_sources(hits, suppliers))

        fetch_urls = _fetch_urls(suppliers, hits)
        fetch_outcome = FetchOutcome()
        if fetch_urls:
            progress(
                f"проверяем сайты: {len(fetch_urls)} начальных страниц, "
                f"до {self._settings.max_live_domains} доменов"
            )
            try:
                fetch_outcome = asyncio.run(
                    self._fetcher.fetch(
                        fetch_urls,
                        deadline_seconds=_fetch_budget(started, self._settings),
                    )
                )
            except Exception as error:  # noqa: BLE001 - сбой сайта не ломает поиск
                fetch_outcome.warnings.append(f"Загрузка сайтов не выполнена: {error}")
        result.warnings.extend(fetch_outcome.warnings)

        pages_by_domain: dict[str, list] = {}
        for page in fetch_outcome.pages:
            domain = normalize_domain(page.final_url) or ""
            pages_by_domain.setdefault(domain, []).append(page)
            owner = next(
                (
                    item.name
                    for item in suppliers
                    if _page_belongs_to_supplier(item, page.url, page.final_url)
                ),
                None,
            )
            result.sources.append(page.source_record(owner))
            if page.status is SourceStatus.FAILED:
                result.pages_failed += 1
            if page.status is SourceStatus.CACHED:
                result.cache_hits += 1

        for supplier in suppliers:
            pages_by_domain[supplier.id] = [
                page
                for page in fetch_outcome.pages
                if _page_belongs_to_supplier(supplier, page.url, page.final_url)
            ]
        enriched = [
            enrich_supplier(supplier, pages_by_domain.get(supplier.id, []), request)
            for supplier in suppliers
        ]
        if self._llm and enriched:
            progress(
                f"LLM: заполняем пропуски по цитатам со страниц "
                f"(до {self._settings.llm_max_suppliers} компаний)"
            )
            try:
                llm_outcome = asyncio.run(
                    self._llm.enrich(
                        enriched,
                        pages_by_domain,
                        request,
                        deadline_seconds=_remaining(
                            started, self._settings.live_search_deadline_seconds
                        ),
                    )
                )
                enriched = llm_outcome.suppliers
                result.warnings.extend(llm_outcome.warnings)
                result.extraction_model = self._llm.model
                result.extraction_schema_version = EXTRACTION_SCHEMA_VERSION
                result.provider_stats["LLM"] = (
                    f"модель {self._llm.model}, запросов {llm_outcome.requests}, "
                    f"успешно {llm_outcome.succeeded}, принято фактов "
                    f"{llm_outcome.accepted_facts}, отклонено {llm_outcome.rejected_facts}"
                )
            except Exception as error:  # noqa: BLE001 - обычное извлечение сохраняется
                safe_error = str(error).replace(
                    self._settings.llm_api_key or "__missing_key__", "***"
                )
                result.warnings.append(
                    f"LLM-извлечение недоступно; сохранены результаты правил: {safe_error}"
                )
                result.provider_stats["LLM"] = "недоступна"
        else:
            result.provider_stats["LLM"] = "не настроена; использованы правила"
            if not self._settings.llm_api_key:
                result.warnings.append(
                    "LLM_API_KEY не задан: сведения извлечены только "
                    "детерминированными правилами."
                )
        result.suppliers = enriched
        progress(
            f"готово к оценке: {len(result.suppliers)} компаний, "
            f"веб-ссылок {len(hits)}, страниц {len(fetch_outcome.pages)}"
        )
        return result


def _places_queries(request: SearchRequest) -> list[str]:
    category = request.category.strip()
    region = (request.region or "").strip()
    values = [
        f"{category} {region}",
        f"{category} оптом {region}",
        f"продукты оптом {region}",
    ]
    return list(dict.fromkeys(" ".join(value.split()) for value in values))


def _web_queries(
    request: SearchRequest, suppliers: list[Supplier]
) -> list[tuple[str, str | None]]:
    category = request.category.strip()
    region = (request.region or "").strip()
    values: list[tuple[str, str | None]] = [
        (f'"{category}" оптом поставщик производитель {region}', None),
        (f'"{category}" прайс доставка HoReCa {region}', None),
    ]
    candidates = [
        supplier
        for supplier in suppliers
        if supplier.company_type not in {CompanyType.RETAILER, CompanyType.MARKETPLACE}
    ]
    for supplier in candidates[:3]:
        values.append((f'"{supplier.name}" "{category}"', supplier.id))
    return [(" ".join(query.split()), supplier_id) for query, supplier_id in values]


def _attach_hits(suppliers: list[Supplier], hits: Iterable[SearchHit]) -> None:
    by_id = {supplier.id: supplier for supplier in suppliers}
    for hit in hits:
        supplier = by_id.get(hit.supplier_id or "")
        if supplier is None:
            continue
        if hit.query not in supplier.discovery_queries:
            supplier.discovery_queries.append(hit.query)
        if hit.url not in supplier.source_urls:
            supplier.source_urls.append(hit.url)
        for related_url in hit.related_urls:
            if related_url not in supplier.source_urls:
                supplier.source_urls.append(related_url)
        if not supplier.website and not is_catalog_url(hit.url):
            parsed = normalize_url(hit.url)
            if parsed:
                supplier.website = (
                    f"{parsed.split('://', 1)[0]}://{normalize_domain(parsed)}"
                )
                supplier.domain = normalize_domain(parsed)


def _hit_sources(
    hits: list[SearchHit], suppliers: list[Supplier]
) -> list[SourceRecord]:
    names = {supplier.id: supplier.name for supplier in suppliers}
    domain_names = {
        supplier.domain: supplier.name for supplier in suppliers if supplier.domain
    }
    return [
        SourceRecord(
            url=hit.url,
            domain=normalize_domain(hit.url) or "",
            status=SourceStatus.SKIPPED,
            note="ссылка обнаружена; сниппет не использован как подтверждение",
            supplier_name=names.get(hit.supplier_id or "")
            or domain_names.get(normalize_domain(hit.url)),
            discovery_query=hit.query,
            provider=hit.provider,
            source_kind=SourceKind.SEARCH_RESULT,
        )
        for hit in hits
    ]


def _fetch_urls(suppliers: list[Supplier], hits: list[SearchHit]) -> list[str]:
    values = [url for hit in hits for url in (hit.url, *hit.related_urls)]
    values.extend(supplier.website for supplier in suppliers if supplier.website)
    return [
        url
        for url in dict.fromkeys(values)
        if url and normalize_domain(url) not in {"2gis.ru", "catalog.api.2gis.com"}
    ]


def _page_belongs_to_supplier(
    supplier: Supplier, requested_url: str, final_url: str
) -> bool:
    normalized_sources = {
        normalize_url(url) for url in supplier.source_urls if normalize_url(url)
    }
    if normalize_url(requested_url) in normalized_sources:
        return True
    if normalize_url(final_url) in normalized_sources:
        return True
    if not supplier.domain or is_catalog_url(final_url):
        return False
    return normalize_domain(final_url) == supplier.domain


def _remaining(started: float, deadline: float) -> float:
    return max(0.5, deadline - (monotonic() - started))


def _fetch_budget(started: float, settings: Settings) -> float:
    """Оставляет часть общего дедлайна для LLM, если она включена."""
    remaining = _remaining(started, settings.live_search_deadline_seconds)
    if not settings.llm_enabled:
        return remaining
    reserve = min(float(settings.llm_timeout_seconds), remaining / 2)
    return max(0.5, remaining - reserve)


def _safe_error(error: BaseException) -> str:
    message = str(error).strip()
    return message[:300] if message else type(error).__name__
