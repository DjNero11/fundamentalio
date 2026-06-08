import logging
import tempfile
import uuid
from pathlib import Path

from django.core.cache import cache
from django.core.paginator import Paginator
from django.http import HttpResponseForbidden, HttpResponseServerError, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .models import Report
from .services.markdown_sanitize import markdown_to_safe_html
from .services.report_generation import ReportGenerationMode, enqueue_report_generation
from .services.stock_search_service import ExternalAPIError, search_companies

logger = logging.getLogger(__name__)

_MAX_STATUS_POLL_IDS = 20


def home(request):
    return redirect('fundamentalio:search')


@ensure_csrf_cookie
def search_page(request):
    recent_reports = Report.objects.order_by('-created_at')[:15]
    return render(
        request,
        'fundamentalio/search.html',
        {'recent_reports': recent_reports},
    )


@require_GET
def history(request):
    """Paginated list of all reports (newest first)."""
    reports_qs = Report.objects.order_by('-created_at')
    paginator = Paginator(reports_qs, 15)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    return render(
        request,
        'fundamentalio/history.html',
        {
            'page_obj': page_obj,
            'paginator': paginator,
        },
    )


def report_detail(request, id):
    """Render report viewer for completed reports."""
    report = get_object_or_404(Report, pk=id)
    if report.status != Report.STATUS_DONE:
        return HttpResponseForbidden()
    if not report.read:
        report.read = True
        report.save(update_fields=['read'])
    try:
        report_html = markdown_to_safe_html(report.markdown)
    except Exception:
        logger.exception(
            "Failed to sanitize report markdown",
            extra={"report_id": report.id},
        )
        return HttpResponseServerError()
    return render(
        request,
        'fundamentalio/report_detail.html',
        {
            'report': report,
            'report_html': report_html,
        },
    )


def _is_rate_limited(request, key_prefix: str, limit: int, window_seconds: int) -> bool:
    """
    Simple cache-based rate limiter keyed by client IP.

    Returns True if the caller has exceeded `limit` requests within `window_seconds`.
    """
    remote_addr = request.META.get("REMOTE_ADDR", "") or "unknown"
    cache_key = f"{key_prefix}:ip:{remote_addr}"
    current = cache.get(cache_key)

    if current is None:
        cache.set(cache_key, 1, window_seconds)
        return False

    if current >= limit:
        return True

    try:
        cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, window_seconds)

    return False


@require_GET
def search_api(request):
    if _is_rate_limited(request, "search_api", limit=30, window_seconds=60):
        logger.warning(
            "Search API rate limit exceeded.",
            extra={"remote_addr": request.META.get("REMOTE_ADDR")},
        )
        return JsonResponse(
            {"error": "Too many search requests. Please slow down and try again shortly."},
            status=429,
        )

    query = (request.GET.get('q') or '').strip()
    if not query:
        return JsonResponse(
            {"error": "Query parameter 'q' is required."},
            status=400,
        )

    try:
        results = search_companies(query)
    except ExternalAPIError:
        logger.exception("Upstream stock search service failed.")
        return JsonResponse(
            {"error": "Upstream search service is temporarily unavailable."},
            status=502,
        )
    except Exception:
        logger.exception("Unexpected error while handling search API request.")
        return JsonResponse(
            {"error": "Unexpected error while searching."},
            status=500,
        )

    return JsonResponse({"results": results})


@require_POST
def report_start_api(request):
    mode = (request.POST.get("mode") or "").strip().lower()
    company_name = (request.POST.get("company_name") or "").strip()
    symbol_name = (request.POST.get("symbol_name") or "").strip()
    exchange_code = (request.POST.get("exchange_code") or "").strip()

    if mode not in (ReportGenerationMode.QUICK, ReportGenerationMode.DEEP):
        return JsonResponse({"error": "Invalid mode."}, status=400)
    if not symbol_name:
        return JsonResponse({"error": "Company symbol is required."}, status=400)

    annual_report_pdf_path = None
    if mode == ReportGenerationMode.DEEP:
        uploaded = request.FILES.get("annual_report_pdf")
        if uploaded is None:
            return JsonResponse({"error": "Annual report PDF is required for deep research."}, status=400)
        if uploaded.size is None or uploaded.size > 30 * 1024 * 1024:
            return JsonResponse({"error": "PDF file is too large (max 30MB)."}, status=400)
        filename = (uploaded.name or "").lower()
        if not filename.endswith(".pdf"):
            return JsonResponse({"error": "Only PDF files are allowed."}, status=400)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", prefix="annual_report_")
        try:
            for chunk in uploaded.chunks():
                tmp.write(chunk)
        finally:
            tmp.close()
        annual_report_pdf_path = tmp.name

    report_type = (
        Report.TYPE_QUICK_RESEARCH if mode == ReportGenerationMode.QUICK else Report.TYPE_DEEP_RESEARCH
    )
    report = Report.objects.create(
        type=report_type,
        status=Report.STATUS_IN_PROCESS,
        company_symbol=symbol_name,
        exchange_code=exchange_code,
        company_name=company_name,
        markdown="",
        read=False,
    )

    enqueue_report_generation(
        report_id=str(report.id),
        mode=mode,
        annual_report_pdf_path=annual_report_pdf_path,
    )
    return JsonResponse({"report_id": str(report.id)})


@require_GET
def report_status_api(request):
    """Return status and read flag for the given report IDs."""
    raw_ids = (request.GET.get("ids") or "").strip()
    if not raw_ids:
        return JsonResponse({"reports": []})

    valid_ids = []
    for part in raw_ids.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            valid_ids.append(uuid.UUID(part))
        except ValueError:
            continue
        if len(valid_ids) >= _MAX_STATUS_POLL_IDS:
            break

    if not valid_ids:
        return JsonResponse({"reports": []})

    reports = Report.objects.filter(pk__in=valid_ids).values("id", "status", "read")
    payload = [
        {
            "id": str(row["id"]),
            "status": row["status"],
            "read": row["read"],
        }
        for row in reports
    ]
    return JsonResponse({"reports": payload})
