from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Optional

from django.db import close_old_connections
from django.utils import timezone

from fundamentalio.models import Report
from fundamentalio.services.research_helpers.deep_research.deep_research_main import run_deep_research_async
from fundamentalio.services.research_helpers.quick_research.quick_research_main import run_quick_research_async

logger = logging.getLogger(__name__)


class ReportGenerationMode:
    QUICK = "quick"
    DEEP = "deep"


# ---------------------------------------------------------------------------
# Single background event loop
#
# One thread runs an asyncio event loop forever. All report generation jobs
# are coroutines scheduled onto this loop via run_coroutine_threadsafe().
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Return the shared event loop, creating it on first call."""
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            try:
                _loop = asyncio.new_event_loop()
                t = threading.Thread(
                    target=_loop.run_forever,
                    daemon=True,
                    name="async-report-worker",
                )
                t.start()
                logger.info("Async report worker event loop started")
            except Exception:
                logger.exception("Failed to start async report worker event loop")
                _loop = None
                raise
    return _loop


# ---------------------------------------------------------------------------
# Called from report_start_api view 
# ---------------------------------------------------------------------------

def enqueue_report_generation(
    *,
    report_id: str,
    mode: str,
    annual_report_pdf_path: Optional[str] = None,
) -> None:
    """
    Schedule a report generation job on the shared async event loop.

    Returns immediately. The job runs as a coroutine — while it waits for
    OpenAI the event loop handles all other in-flight jobs concurrently.

    Best-effort: if the process restarts, in-flight jobs are lost.
    Call active_job_count() before deploying to check for in-flight jobs.
    """
    try:
        loop = _get_or_create_loop()
        future = asyncio.run_coroutine_threadsafe(
            _generate_report_async(
                report_id=report_id,
                mode=mode,
                annual_report_pdf_path=annual_report_pdf_path,
            ),
            loop,
        )
    except Exception:
        logger.exception(
            "Failed to enqueue report generation",
            extra={"report_id": report_id, "mode": mode},
        )
        try:
            Report.objects.filter(pk=report_id).update(status=Report.STATUS_ERROR)
        except Exception:
            logger.exception(
                "Failed to set report status=error after enqueue failure",
                extra={"report_id": report_id, "mode": mode},
            )
        raise

    def _log_future_failure(done_future: object) -> None:
        try:
            done_future.result()
        except Exception:
            logger.exception(
                "Report generation task failed",
                extra={"report_id": report_id, "mode": mode},
            )

    future.add_done_callback(_log_future_failure)


# ---------------------------------------------------------------------------
# Worker coroutine
# ---------------------------------------------------------------------------

async def _generate_report_async(
    *,
    report_id: str,
    mode: str,
    annual_report_pdf_path: Optional[str],
) -> None:
    """
    Async equivalent of the old _generate_report_worker.

    The only behavioural difference: I/O waits (OpenAI, Tavily) are truly
    non-blocking, so the event loop runs other jobs while this one waits.
    """
    # Django DB connections are not async-safe across threads; close stale ones.
    # Use to_thread for any DB call so it runs in a thread pool, not the loop.
    await asyncio.to_thread(close_old_connections)

    try:
        # DB read — sync, but fast; run in thread so the loop stays free.
        report = await asyncio.to_thread(Report.objects.get, pk=report_id)

        if report.status != Report.STATUS_IN_PROCESS:
            return

        started_at = timezone.now()
        logger.info(
            "Starting report generation",
            extra={
                "report_id": str(report.id),
                "mode": mode,
                "started_at": started_at.isoformat(),
            },
        )

        if mode == ReportGenerationMode.QUICK:
            # run_quick_research_async is a native coroutine — await directly.
            markdown = await run_quick_research_async(
                report.company_symbol,
                report.exchange_code,
            )
            usage_info = ""

        elif mode == ReportGenerationMode.DEEP:
            # run_deep_research_async is a native coroutine — await directly.
            markdown, usage_info = await run_deep_research_async(
                report.company_symbol,
                report.exchange_code,
                annual_report_path=annual_report_pdf_path,
            )

        else:
            raise ValueError(f"Invalid report generation mode: {mode!r}")

        report.status = Report.STATUS_DONE
        report.markdown = markdown
        report.usage_info = usage_info if (mode == ReportGenerationMode.DEEP and usage_info) else ""

        await asyncio.to_thread(
            report.save,
            update_fields=["status", "markdown", "usage_info"],
        )

        finished_at = timezone.now()
        logger.info(
            "Report generation done",
            extra={
                "report_id": str(report.id),
                "mode": mode,
                "elapsed_s": (finished_at - started_at).total_seconds(),
            },
        )

    except Exception:
        logger.exception("Report generation error", extra={"report_id": report_id, "mode": mode})
        try:
            await asyncio.to_thread(
                Report.objects.filter(pk=report_id).update,
                status=Report.STATUS_ERROR,
            )
        except Exception:
            logger.exception(
                "Failed to set report status=error",
                extra={"report_id": report_id, "mode": mode},
            )

    finally:
        await asyncio.to_thread(close_old_connections)

        if annual_report_pdf_path:
            try:
                Path(annual_report_pdf_path).unlink(missing_ok=True)
            except Exception:
                logger.warning("Failed to delete temp PDF", extra={"path": annual_report_pdf_path})
