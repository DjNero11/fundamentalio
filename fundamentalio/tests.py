import asyncio
import json
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone

from .models import Report


def _create_report(**kwargs):
    """Create a Report with sensible defaults."""
    defaults = {
        'type': Report.TYPE_QUICK_RESEARCH,
        'status': Report.STATUS_DONE,
        'company_symbol': 'AAPL',
        'exchange_code': 'US',
        'company_name': 'Apple Inc.',
        'markdown': '# Hello\n\nSome **markdown** content.',
    }
    defaults.update(kwargs)
    return Report.objects.create(**defaults)


class HomeViewTests(TestCase):
    def test_home_redirects_to_search(self):
        response = self.client.get(reverse('fundamentalio:home'))
        self.assertRedirects(response, reverse('fundamentalio:search'))


class SearchApiViewTests(TestCase):
    """Tests for the /api/search/ endpoint."""

    def setUp(self):
        cache.clear()

    @patch('fundamentalio.views.search_companies')
    def test_search_api_returns_results_without_authentication(self, mock_search_companies):
        mock_search_companies.return_value = [
            {
                "name": "Apple Inc",
                "code": "AAPL",
                "previous_close": 229.65,
                "currency": "USD",
            }
        ]
        url = reverse('fundamentalio:api_search')
        response = self.client.get(url, {'q': 'AAPL'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'], mock_search_companies.return_value)

    @patch('fundamentalio.views.search_companies')
    def test_search_api_returns_empty_list(self, mock_search_companies):
        mock_search_companies.return_value = []
        url = reverse('fundamentalio:api_search')
        response = self.client.get(url, {'q': 'UNKNOWN'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'], [])

    def test_search_api_requires_query_param(self):
        url = reverse('fundamentalio:api_search')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.json())
        response_blank = self.client.get(url, {'q': '   '})
        self.assertEqual(response_blank.status_code, 400)

    @patch('fundamentalio.views.search_companies')
    def test_search_api_handles_external_error(self, mock_search_companies):
        from fundamentalio.services.stock_search_service import ExternalAPIError
        mock_search_companies.side_effect = ExternalAPIError('Upstream failed')
        url = reverse('fundamentalio:api_search')
        response = self.client.get(url, {'q': 'AAPL'})
        self.assertEqual(response.status_code, 502)

    @patch('fundamentalio.views.search_companies')
    def test_search_api_handles_unexpected_exception(self, mock_search_companies):
        mock_search_companies.side_effect = RuntimeError('boom')
        url = reverse('fundamentalio:api_search')
        response = self.client.get(url, {'q': 'AAPL'})
        self.assertEqual(response.status_code, 500)

    @patch('fundamentalio.views.search_companies')
    def test_search_api_rate_limiting_returns_429(self, mock_search_companies):
        mock_search_companies.return_value = []
        url = reverse('fundamentalio:api_search')
        for _ in range(30):
            response = self.client.get(url, {'q': 'AAPL'})
            self.assertNotEqual(response.status_code, 429)
        limited_response = self.client.get(url, {'q': 'AAPL'})
        self.assertEqual(limited_response.status_code, 429)


class SearchPageViewTests(TestCase):
    def test_search_page_renders_for_anonymous_user(self):
        response = self.client.get(reverse('fundamentalio:search'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="search-input"')

    def test_search_page_uses_alpine_js(self):
        response = self.client.get(reverse('fundamentalio:search'))
        self.assertContains(response, static('js/alpine-3.15.8.min.js'))
        self.assertContains(response, 'x-data="searchCompanies()"')


class ReportModelTests(TestCase):
    def test_report_created_with_all_required_fields(self):
        report = _create_report()
        self.assertIsNotNone(report.id)
        self.assertEqual(report.type, Report.TYPE_QUICK_RESEARCH)
        self.assertEqual(report.company_symbol, 'AAPL')

    def test_report_type_accepts_only_defined_choices(self):
        report = _create_report(type=Report.TYPE_DEEP_RESEARCH)
        report.full_clean()
        report.type = 'invalid_type'
        with self.assertRaises(ValidationError):
            report.full_clean()

    def test_report_markdown_max_length_validation(self):
        report = Report(
            type=Report.TYPE_QUICK_RESEARCH,
            status=Report.STATUS_IN_PROCESS,
            company_symbol='X',
            exchange_code='US',
            company_name='Test',
            markdown='x' * 200_001,
        )
        with self.assertRaises(ValidationError):
            report.full_clean()


class ReportDetailViewTests(TestCase):
    def test_anyone_can_access_done_report_detail(self):
        report = _create_report()
        response = self.client.get(reverse('fundamentalio:report_detail', kwargs={'id': report.id}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, report.company_name)

    def test_visiting_report_detail_marks_unread_report_as_read(self):
        report = _create_report(read=False)
        response = self.client.get(reverse('fundamentalio:report_detail', kwargs={'id': report.id}))
        self.assertEqual(response.status_code, 200)
        report.refresh_from_db()
        self.assertTrue(report.read)

    def test_in_process_report_detail_returns_403(self):
        report = _create_report(status=Report.STATUS_IN_PROCESS, markdown='')
        response = self.client.get(reverse('fundamentalio:report_detail', kwargs={'id': report.id}))
        self.assertEqual(response.status_code, 403)

    def test_missing_report_returns_404(self):
        response = self.client.get(reverse('fundamentalio:report_detail', kwargs={'id': uuid.uuid4()}))
        self.assertEqual(response.status_code, 404)


class ReportHistoryViewTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_history_is_public_and_lists_all_reports(self):
        report_a = _create_report(company_name='Company A')
        report_b = _create_report(company_name='Company B')
        response = self.client.get(reverse('fundamentalio:history'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, report_a.company_name)
        self.assertContains(response, report_b.company_name)

    def test_history_orders_reports_newest_first(self):
        older = _create_report(company_name='Older')
        newer = _create_report(company_name='Newer')
        older.created_at = timezone.now() - timedelta(days=2)
        older.save(update_fields=['created_at'])
        newer.created_at = timezone.now()
        newer.save(update_fields=['created_at'])
        response = self.client.get(reverse('fundamentalio:history'))
        objects = list(response.context['page_obj'].object_list)
        self.assertEqual(objects[0].company_name, 'Newer')

    def test_history_paginates_15_per_page(self):
        for i in range(20):
            _create_report(company_name=f'Company {i}')
        response_page_1 = self.client.get(reverse('fundamentalio:history'), {'page': 1})
        self.assertEqual(len(response_page_1.context['page_obj'].object_list), 15)
        response_page_2 = self.client.get(reverse('fundamentalio:history'), {'page': 2})
        self.assertEqual(len(response_page_2.context['page_obj'].object_list), 5)


class ReportListStatusRenderingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.history_url = reverse('fundamentalio:history')

    def test_in_process_shows_generating_label_without_link_or_dot(self):
        report = _create_report(
            company_name='Generating Co',
            status=Report.STATUS_IN_PROCESS,
            markdown='',
            read=False,
        )
        detail_url = reverse('fundamentalio:report_detail', kwargs={'id': report.id})
        response = self.client.get(self.history_url)
        self.assertContains(response, 'data-testid="generating-label"')
        self.assertNotContains(response, f'href="{detail_url}"')


class ReportStatusApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse('fundamentalio:api_report_status')

    def test_returns_status_without_authentication(self):
        report = _create_report()
        response = self.client.get(self.url, {'ids': str(report.id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['reports']), 1)

    def test_returns_all_matching_reports_globally(self):
        report_a = _create_report(company_name='A')
        report_b = _create_report(company_name='B')
        response = self.client.get(self.url, {'ids': f'{report_a.id},{report_b.id}'})
        self.assertEqual(len(response.json()['reports']), 2)


class SearchPageRecentReportsTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_search_page_shows_latest_15_reports_globally(self):
        for i in range(20):
            _create_report(company_name=f'Company {i}')
        response = self.client.get(reverse('fundamentalio:search'))
        recent_reports = list(response.context['recent_reports'])
        self.assertEqual(len(recent_reports), 15)

    def test_search_page_renders_links_to_report_detail_and_history(self):
        report = _create_report(company_name='Recent Co')
        response = self.client.get(reverse('fundamentalio:search'))
        detail_url = reverse('fundamentalio:report_detail', kwargs={'id': report.id})
        history_url = reverse('fundamentalio:history')
        self.assertContains(response, detail_url)
        self.assertContains(response, history_url)

    def test_search_page_sets_csrf_cookie_for_report_start_api(self):
        response = self.client.get(reverse('fundamentalio:search'))
        self.assertIn('csrftoken', response.cookies)


class ReportStartApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse('fundamentalio:api_report_start')

    @patch('fundamentalio.views.enqueue_report_generation')
    def test_quick_mode_creates_report_and_enqueues_job(self, mock_enqueue):
        response = self.client.post(
            self.url,
            {
                'mode': 'quick',
                'company_name': 'Apple Inc.',
                'symbol_name': 'AAPL',
                'exchange_code': 'US',
            },
        )
        self.assertEqual(response.status_code, 200)
        report = Report.objects.get(pk=response.json()['report_id'])
        self.assertEqual(report.type, Report.TYPE_QUICK_RESEARCH)
        mock_enqueue.assert_called_once()

    @patch('fundamentalio.views.enqueue_report_generation')
    def test_deep_mode_with_pdf_creates_report_and_enqueues_job(self, mock_enqueue):
        uploaded_pdf = SimpleUploadedFile(
            'annual_report.pdf',
            b'%PDF-1.4\nfake content\n',
            content_type='application/pdf',
        )
        response = self.client.post(
            self.url,
            {
                'mode': 'deep',
                'company_name': 'Apple Inc.',
                'symbol_name': 'AAPL',
                'exchange_code': 'US',
                'annual_report_pdf': uploaded_pdf,
            },
        )
        self.assertEqual(response.status_code, 200)
        kwargs = mock_enqueue.call_args.kwargs
        Path(kwargs['annual_report_pdf_path']).unlink(missing_ok=True)

    def test_invalid_mode_returns_400(self):
        response = self.client.post(self.url, {'mode': 'invalid', 'symbol_name': 'AAPL'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Report.objects.count(), 0)


class ReportGenerationWorkerTests(TransactionTestCase):
    @staticmethod
    def _create_in_process_report(**kwargs):
        defaults = {
            'type': Report.TYPE_QUICK_RESEARCH,
            'status': Report.STATUS_IN_PROCESS,
            'company_symbol': 'AAPL',
            'exchange_code': 'US',
            'company_name': 'Apple Inc.',
            'markdown': '',
            'usage_info': '',
        }
        defaults.update(kwargs)
        return Report.objects.create(**defaults)

    @patch('fundamentalio.services.report_generation.run_quick_research_async', new_callable=AsyncMock)
    def test_generate_report_async_quick_success_sets_done(self, mock_quick):
        from fundamentalio.services.report_generation import ReportGenerationMode, _generate_report_async
        report = self._create_in_process_report()
        mock_quick.return_value = '# Quick markdown'
        asyncio.run(
            _generate_report_async(
                report_id=str(report.id),
                mode=ReportGenerationMode.QUICK,
                annual_report_pdf_path=None,
            )
        )
        report.refresh_from_db()
        self.assertEqual(report.status, Report.STATUS_DONE)
        self.assertEqual(report.markdown, '# Quick markdown')

    @patch('fundamentalio.services.report_generation.run_deep_research_async', new_callable=AsyncMock)
    def test_generate_report_async_deep_success_sets_done_and_usage(self, mock_deep):
        from fundamentalio.services.report_generation import ReportGenerationMode, _generate_report_async

        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
            temp_pdf_path = tmp.name

        report = self._create_in_process_report(type=Report.TYPE_DEEP_RESEARCH)
        mock_deep.return_value = ('# Deep markdown', '{"totals": {"input_tokens": 42}}')

        asyncio.run(
            _generate_report_async(
                report_id=str(report.id),
                mode=ReportGenerationMode.DEEP,
                annual_report_pdf_path=temp_pdf_path,
            )
        )

        report.refresh_from_db()
        self.assertEqual(report.status, Report.STATUS_DONE)
        self.assertEqual(report.markdown, '# Deep markdown')
        self.assertEqual(report.usage_info, '{"totals": {"input_tokens": 42}}')
        self.assertFalse(Path(temp_pdf_path).exists())

    def test_generate_report_async_invalid_mode_sets_error(self):
        from fundamentalio.services.report_generation import _generate_report_async

        report = self._create_in_process_report()
        asyncio.run(
            _generate_report_async(
                report_id=str(report.id),
                mode='not-valid',
                annual_report_pdf_path=None,
            )
        )
        report.refresh_from_db()
        self.assertEqual(report.status, Report.STATUS_ERROR)

    @patch('fundamentalio.services.report_generation.run_quick_research_async', new_callable=AsyncMock)
    def test_generate_report_async_helper_exception_sets_error(self, mock_quick):
        from fundamentalio.services.report_generation import ReportGenerationMode, _generate_report_async

        report = self._create_in_process_report()
        mock_quick.side_effect = RuntimeError('upstream failed')
        asyncio.run(
            _generate_report_async(
                report_id=str(report.id),
                mode=ReportGenerationMode.QUICK,
                annual_report_pdf_path=None,
            )
        )
        report.refresh_from_db()
        self.assertEqual(report.status, Report.STATUS_ERROR)


class StockSearchServiceTests(TestCase):
    """Tests for Yahoo Finance-backed stock search."""

    @patch("fundamentalio.services.stock_search_service._fast_info_snapshot")
    @patch("fundamentalio.services.stock_search_service.yf.Search")
    def test_search_companies_keeps_equities_and_enriches_prices(
        self, mock_search_cls, mock_fast_snap
    ):
        from fundamentalio.services.stock_search_service import search_companies

        mock_fast_snap.return_value = ("AAPL", 229.65, "USD")
        inst = MagicMock()
        mock_search_cls.return_value = inst
        inst.search.return_value = None
        inst.quotes = [
            {
                "quoteType": "EQUITY",
                "symbol": "AAPL",
                "longname": "Apple Inc",
                "exchange": "NMS",
            },
            {
                "quoteType": "ETF",
                "symbol": "QQQ",
                "longname": "Invesco QQQ Trust",
                "exchange": "NMS",
            },
        ]

        results = search_companies("AAPL")

        self.assertEqual(len(results), 1)
        primary = results[0]
        self.assertEqual(primary["code"], "AAPL")
        self.assertEqual(primary["name"], "Apple Inc")
        self.assertEqual(primary["exchange_code"], "NMS")
        self.assertEqual(primary["previous_close"], 229.65)
        self.assertEqual(primary["currency"], "USD")
        mock_search_cls.assert_called_once()
        self.assertEqual(mock_search_cls.call_args[0][0], "AAPL")
        self.assertEqual(mock_search_cls.call_args[1]["max_results"], 15)

    @patch("fundamentalio.services.stock_search_service._fast_info_snapshot")
    @patch("fundamentalio.services.stock_search_service.yf.Search")
    def test_search_companies_deduplicates_symbols(self, mock_search_cls, mock_fast_snap):
        from fundamentalio.services.stock_search_service import search_companies

        mock_fast_snap.return_value = ("AAPL", 1.0, "USD")
        inst = MagicMock()
        mock_search_cls.return_value = inst
        inst.search.return_value = None
        inst.quotes = [
            {"quoteType": "EQUITY", "symbol": "AAPL", "longname": "A", "exchange": "NMS"},
            {"quoteType": "EQUITY", "symbol": "AAPL", "longname": "A", "exchange": "NMS"},
        ]

        self.assertEqual(len(search_companies("aapl")), 1)

    @patch("fundamentalio.services.stock_search_service.yf.Search", side_effect=RuntimeError("boom"))
    def test_search_companies_raises_external_error_on_search_failure(self, _mock_search):
        from fundamentalio.services.stock_search_service import search_companies, ExternalAPIError

        with self.assertRaises(ExternalAPIError):
            search_companies("AAPL")

    def test_search_companies_requires_non_empty_query(self):
        from fundamentalio.services.stock_search_service import search_companies

        with self.assertRaises(ValueError):
            search_companies("   ")


class YahooFundamentalsResolverTests(TestCase):
    def test_resolve_yahoo_ticker_returns_symbol_without_exchange_mapping(self):
        from fundamentalio.services.research_helpers.shared.yfinance_fundamentals import (
            resolve_yahoo_ticker,
        )

        self.assertEqual(resolve_yahoo_ticker("vod.l", "LSE"), "VOD.L")
        self.assertEqual(resolve_yahoo_ticker("aapl", "NYSE"), "AAPL")


class YahooFundamentalsGeneralShapeTests(SimpleTestCase):
    def test_build_general_excludes_unneeded_fields_and_keeps_core_fields(self):
        from fundamentalio.services.research_helpers.shared.yfinance_fundamentals import _build_general

        info = {
            "symbol": "AAPL",
            "quoteType": "EQUITY",
            "longName": "Apple Inc.",
            "exchange": "NMS",
            "currency": "USD",
            "country": "United States",
            "lastFiscalYearEnd": 1735603200,
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "delisted": False,
            "longBusinessSummary": "Makes devices and software.",
            "website": "https://www.apple.com",
            "fullTimeEmployees": 161000,
        }

        general = _build_general(info)

        for removed_key in (
            "Type",
            "CurrencyName",
            "InternationalDomestic",
            "IsDelisted",
            "LogoURL",
            "UpdatedAt",
        ):
            self.assertNotIn(removed_key, general)

        self.assertEqual(general["Code"], "AAPL")
        self.assertEqual(general["Name"], "Apple Inc.")
        self.assertEqual(general["Exchange"], "NMS")
        self.assertEqual(general["CurrencyCode"], "USD")
        self.assertEqual(general["Sector"], "Technology")
        self.assertEqual(general["Industry"], "Consumer Electronics")

class SharedApiTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fundamentalio.services.research_helpers.shared import api as shared_api
        cls.shared_api = shared_api

    @patch(
        "fundamentalio.services.research_helpers.shared.yfinance_fundamentals.build_fundamentals"
    )
    def test_fetch_fundamentals_raises_on_value_error_from_builder(self, mock_build):
        mock_build.side_effect = ValueError("No Yahoo Finance data for ticker 'ZZZ'.")

        with self.assertRaises(self.shared_api.FundamentalsAPIError):
            self.shared_api.fetch_fundamentals("ZZZ", "US", api_token=None)

    @patch(
        "fundamentalio.services.research_helpers.shared.yfinance_fundamentals.build_fundamentals"
    )
    def test_fetch_fundamentals_returns_payload_on_success(self, mock_build):
        mock_build.return_value = {"General": {"Code": "AAPL"}}

        result = self.shared_api.fetch_fundamentals("AAPL", "US", api_token="ignored")

        self.assertEqual(result, {"General": {"Code": "AAPL"}})
        mock_build.assert_called_once_with("AAPL", "US")

    @patch(
        "fundamentalio.services.research_helpers.shared.yfinance_fundamentals.build_fundamentals"
    )
    def test_fetch_fundamentals_wraps_unexpected_exception(self, mock_build):
        mock_build.side_effect = RuntimeError("network down")

        with self.assertRaises(self.shared_api.FundamentalsAPIError):
            self.shared_api.fetch_fundamentals("AAPL", "US", api_token="token")


class SharedJsonParseTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fundamentalio.services.research_helpers.shared import json_parse
        cls.json_parse = json_parse

    def test_safe_first_n_from_dict_returns_empty_for_non_dict(self):
        self.assertEqual(self.json_parse.safe_first_n_from_dict([], 3), [])

    def test_extract_data_limits_collections_and_maps_sections(self):
        insider = {str(i): {"date": f"2024-01-{i:02d}"} for i in range(1, 30)}
        annual = {str(i): {"date": f"202{i}-12-31", "sharesMln": i} for i in range(12)}
        earnings = {str(i): {"year": 2010 + i} for i in range(30)}
        quarterly = {str(i): {"q": i} for i in range(10)}
        yearly = {str(i): {"y": i} for i in range(10)}
        payload = {
            "General": {"Code": "AAPL", "Name": "Apple", "Officers": {"1": {"Name": "CEO", "Title": "Chief"}}},
            "Highlights": {"MarketCapitalization": 100},
            "Valuation": {"TrailingPE": 20},
            "SharesStats": {"SharesOutstanding": 10},
            "InsiderTransactions": insider,
            "outstandingShares": {"annual": annual},
            "Earnings": {"Annual": earnings},
            "Financials": {
                "Balance_Sheet": {"quarterly": quarterly, "yearly": yearly},
                "Cash_Flow": {"quarterly": quarterly, "yearly": yearly},
                "Income_Statement": {"quarterly": quarterly, "yearly": yearly},
            },
        }

        extracted = self.json_parse.extract_data(payload)

        self.assertIn("General", extracted)
        self.assertIn("FundamentalsMeta", extracted)
        self.assertEqual(extracted["FundamentalsMeta"], {})
        self.assertIn("Highlights", extracted)
        self.assertIn("Valuation", extracted)
        self.assertIn("SharesStats", extracted)
        self.assertEqual(len(extracted["InsiderTransactions"]), 20)
        self.assertEqual(len(extracted["OutstandingSharesAnnual"]), 10)
        self.assertEqual(len(extracted["EarningsAnnual"]), 15)
        self.assertEqual(len(extracted["BalanceSheetQuarterly"]), 6)
        self.assertEqual(len(extracted["BalanceSheetYearly"]), 5)
        self.assertEqual(extracted["BalanceSheetQuarterly"][0]["date"], "0")
        self.assertEqual(extracted["BalanceSheetQuarterly"][0]["q"], 0)
        self.assertEqual(extracted["EarningsAnnual"][0]["date"], "0")
        self.assertEqual(extracted["EarningsAnnual"][0]["year"], 2010)

    def test_extract_data_forwards_fundamentals_meta(self):
        payload = {
            "General": {"Code": "X"},
            "FundamentalsMeta": {"financialCurrency": "USD", "statementLineItemsSource": "YahooFinance"},
        }
        extracted = self.json_parse.extract_data(payload)
        self.assertEqual(
            extracted["FundamentalsMeta"],
            {"financialCurrency": "USD", "statementLineItemsSource": "YahooFinance"},
        )

    def test_extract_data_period_rows_omit_none_values(self):
        payload = {
            "General": {"Code": "X"},
            "Financials": {
                "Balance_Sheet": {
                    "quarterly": {
                        "2025-12-31": {"Total Assets": 100, "Empty": None},
                    },
                    "yearly": {},
                },
                "Cash_Flow": {"quarterly": {}, "yearly": {}},
                "Income_Statement": {"quarterly": {}, "yearly": {}},
            },
            "Earnings": {"Annual": {"2024-12-31": {"Net Income": 1, "skip": None}}},
        }
        extracted = self.json_parse.extract_data(payload)
        row = extracted["BalanceSheetQuarterly"][0]
        self.assertEqual(row, {"date": "2025-12-31", "Total Assets": 100})
        earn = extracted["EarningsAnnual"][0]
        self.assertEqual(earn, {"date": "2024-12-31", "Net Income": 1})

    def test_extract_data_handles_malformed_optional_sections(self):
        payload = {
            "General": {"Name": "Apple", "Officers": "not-a-dict"},
            "InsiderTransactions": "bad-data",
        }

        extracted = self.json_parse.extract_data(payload)

        self.assertEqual(extracted["General"]["Officers"], [])
        self.assertEqual(extracted["InsiderTransactions"], [])
        self.assertEqual(extracted["FundamentalsMeta"], {})


class QuickResearchMainTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fundamentalio.services.research_helpers.quick_research import quick_research_main
        cls.quick_research_main = quick_research_main

    def test_parse_llm_response_accepts_fenced_json(self):
        output = '```json\n{"markdown":"# Report"}\n```'

        result = self.quick_research_main.parse_llm_response(output)

        self.assertEqual(result, "# Report")

    def test_parse_llm_response_rejects_missing_markdown(self):
        with self.assertRaises(self.quick_research_main.LLMOutputError):
            self.quick_research_main.parse_llm_response('{"no_markdown":"value"}')

    def test_build_report_raises_when_disclaimer_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_path = Path(tmp_dir) / "missing_disclaimer.md"
            with self.assertRaises(Exception):
                self.quick_research_main.build_report("# Content", disclaimer_path=missing_path)

    @patch("fundamentalio.services.research_helpers.quick_research.quick_research_main.build_report")
    @patch("fundamentalio.services.research_helpers.quick_research.quick_research_main._call_llm_async", new_callable=AsyncMock)
    @patch("fundamentalio.services.research_helpers.quick_research.quick_research_main.fetch_and_parse_company_data")
    @patch("fundamentalio.services.research_helpers.quick_research.quick_research_main.load_prompts")
    def test_run_quick_research_async_happy_path(
        self,
        mock_load_prompts,
        mock_fetch_company_data,
        mock_call_llm_async,
        mock_build_report,
    ):
        mock_load_prompts.return_value = ("SYSTEM", "METHODOLOGY")
        mock_fetch_company_data.return_value = {"General": {"Name": "Apple"}}
        mock_call_llm_async.return_value = ('{"markdown":"# Quick"}', SimpleNamespace())
        mock_build_report.return_value = "# Quick\n\n---\n\nDisclaimer"

        result = asyncio.run(
            self.quick_research_main.run_quick_research_async("AAPL", "US")
        )

        self.assertEqual(result, "# Quick\n\n---\n\nDisclaimer")
        mock_load_prompts.assert_called_once()
        mock_fetch_company_data.assert_called_once_with("AAPL", "US")
        mock_call_llm_async.assert_awaited_once()
        mock_build_report.assert_called_once_with("# Quick", None)


class DeepResearchMainTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fundamentalio.services.research_helpers.deep_research import deep_research_main
        cls.deep_research_main = deep_research_main

    def test_get_annual_report_pdf_path_raises_for_missing_argument(self):
        with self.assertRaises(self.deep_research_main.AnnualReportFileNotFoundError):
            self.deep_research_main.get_annual_report_pdf_path(None)

    def test_get_annual_report_pdf_path_raises_for_missing_file(self):
        with self.assertRaises(self.deep_research_main.AnnualReportFileNotFoundError):
            self.deep_research_main.get_annual_report_pdf_path("/tmp/does-not-exist.pdf")

    @patch("fundamentalio.services.research_helpers.deep_research.deep_research_main.pymupdf4llm.to_markdown")
    def test_extract_pdf_text_with_pymupdf_raises_on_failure(self, mock_to_markdown):
        mock_to_markdown.side_effect = RuntimeError("parse error")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            with self.assertRaises(self.deep_research_main.AnnualReportExtractionError):
                self.deep_research_main.extract_pdf_text_with_pymupdf(Path(tmp.name))

    @patch("fundamentalio.services.research_helpers.deep_research.deep_research_main.pymupdf4llm.to_markdown")
    def test_extract_pdf_text_with_pymupdf_truncates_oversized_output(self, mock_to_markdown):
        max_chars = self.deep_research_main._MAX_LOADED_TEXT_CHARS
        mock_to_markdown.return_value = "A" * (max_chars + 100)
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            result = self.deep_research_main.extract_pdf_text_with_pymupdf(Path(tmp.name))
        self.assertEqual(len(result), max_chars)
        self.assertEqual(result, "A" * max_chars)

    @patch("fundamentalio.services.research_helpers.deep_research.deep_research_main.build_deep_research_report")
    @patch("fundamentalio.services.research_helpers.deep_research.deep_research_main._call_deep_research_llm_async", new_callable=AsyncMock)
    @patch("fundamentalio.services.research_helpers.deep_research.deep_research_main.run_tavily_research", new_callable=AsyncMock)
    @patch("fundamentalio.services.research_helpers.deep_research.deep_research_main.fetch_and_parse_company_data")
    @patch("fundamentalio.services.research_helpers.deep_research.deep_research_main.extract_pdf_text_with_pymupdf")
    @patch("fundamentalio.services.research_helpers.deep_research.deep_research_main._load_text")
    def test_run_deep_research_async_happy_path(
        self,
        mock_load_text,
        mock_extract_pdf_text,
        mock_fetch_company_data,
        mock_run_tavily_research,
        mock_call_llm_async,
        mock_build_report,
    ):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
            tmp_pdf_path = Path(tmp_pdf.name)
        try:
            mock_load_text.side_effect = ["SYSTEM", "METHODOLOGY"]
            mock_extract_pdf_text.return_value = "ANNUAL REPORT TEXT"
            mock_fetch_company_data.return_value = {"General": {"Name": "Apple"}}
            mock_run_tavily_research.return_value = (
                {"usage": {"credits": 1}, "results": []},
                {"usage": {"credits": 2}, "results": []},
            )
            usage = SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                input_tokens_details=SimpleNamespace(cached_tokens=2),
            )
            deep_response = SimpleNamespace(usage=usage, id="resp_123")
            mock_call_llm_async.return_value = ("# Deep markdown", deep_response)
            mock_build_report.return_value = "# Deep report\n\n---\n\nDisclaimer"

            report, usage_json = asyncio.run(
                self.deep_research_main.run_deep_research_async(
                    "AAPL",
                    "US",
                    annual_report_path=tmp_pdf_path,
                )
            )

            self.assertEqual(report, "# Deep report\n\n---\n\nDisclaimer")
            payload = json.loads(usage_json)
            self.assertEqual(payload["totals"]["input_tokens"], 10)
            self.assertEqual(payload["totals"]["cached_input_tokens"], 2)
            self.assertEqual(payload["totals"]["output_tokens"], 5)
            self.assertEqual(payload["totals"]["tavily_credits"], 3)
            mock_run_tavily_research.assert_awaited_once_with("Apple", "AAPL", "US")
            mock_call_llm_async.assert_awaited_once()
            self.assertFalse(tmp_pdf_path.exists())
        finally:
            tmp_pdf_path.unlink(missing_ok=True)

    def test_truncate_to_token_limit_returns_input_when_under_limit(self):
        text = "hello world"
        self.assertEqual(
            self.deep_research_main._truncate_to_token_limit(text),
            text,
        )

    def test_truncate_to_token_limit_trims_end_when_over_limit(self):
        max_chars = self.deep_research_main._MAX_LOADED_TEXT_CHARS
        text = "A" * max_chars + "BBBB"
        result = self.deep_research_main._truncate_to_token_limit(text, source="test")
        self.assertEqual(len(result), max_chars)
        self.assertEqual(result, "A" * max_chars)

    def test_load_text_truncates_oversized_files(self):
        max_chars = self.deep_research_main._MAX_LOADED_TEXT_CHARS
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("X" * (max_chars + 100))
            tmp_path = Path(tmp.name)
        try:
            result = self.deep_research_main._load_text(tmp_path)
            self.assertEqual(len(result), max_chars)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_run_deep_research_async_raises_when_annual_report_missing(self):
        with self.assertRaises(self.deep_research_main.AnnualReportFileNotFoundError):
            asyncio.run(
                self.deep_research_main.run_deep_research_async(
                    "AAPL",
                    "US",
                    annual_report_path=None,
                )
            )
