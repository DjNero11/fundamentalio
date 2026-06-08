from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI         
from tavily import AsyncTavilyClient
import pymupdf4llm

from fundamentalio.services.research_helpers.shared.api import fetch_fundamentals
from fundamentalio.services.research_helpers.shared.json_parse import extract_data

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Paths and constants
# -----------------------------------------------------------------------------
_MODULE_DIR = Path(__file__).resolve().parent
DEEP_SYSTEM_PROMPT_PATH = _MODULE_DIR / "prompts" / "deep_research_system_prompt.md"
DEEP_METHODOLOGY_PATH   = _MODULE_DIR / "prompts" / "deep_research_methodology.md"
DISCLAIMER_PATH         = _MODULE_DIR.parent / "shared" / "disclaimer.md"

MAX_LOADED_TEXT_TOKENS = 200_000
_CHARS_PER_TOKEN = 3.0
_MAX_LOADED_TEXT_CHARS = int(MAX_LOADED_TEXT_TOKENS * _CHARS_PER_TOKEN)  # e.g. 200k * 3.0 = 600k chars

# -----------------------------------------------------------------------------
# Custom exceptions
# -----------------------------------------------------------------------------

class DeepResearchError(Exception):
    """Base error for deep research flow."""

class CompanyDataParseError(DeepResearchError):
    """Raised when fundamentals data is invalid or missing required structure."""

class MissingRequiredFieldError(CompanyDataParseError):
    """Raised when a required field is missing from the fundamentals payload."""

class LLMOutputError(DeepResearchError):
    """Raised when LLM returns invalid JSON, missing markdown, or refusal."""

class AnnualReportFileNotFoundError(DeepResearchError):
    """Raised when the annual report PDF file is missing or unreadable."""

class AnnualReportExtractionError(DeepResearchError):
    """Raised when the annual report PDF cannot be parsed into text."""

# -----------------------------------------------------------------------------
# Usage accounting
# -----------------------------------------------------------------------------

def _extract_openai_usage(step_name: str, response: Any, model: str) -> Dict[str, Any]:
    input_tokens        = 0
    output_tokens       = 0
    cached_input_tokens = 0
    usage_obj = getattr(response, "usage", None)
    if usage_obj is not None:
        input_tokens  = int(getattr(usage_obj, "input_tokens",  0) or 0)
        output_tokens = int(getattr(usage_obj, "output_tokens", 0) or 0)
        details = getattr(usage_obj, "input_tokens_details", None)
        if details is not None:
            cached_input_tokens = int(getattr(details, "cached_tokens", 0) or 0)
    request_id = getattr(response, "id", None) or getattr(response, "request_id", None)
    step_usage: Dict[str, Any] = {
        "model":               model,
        "input_tokens":        input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens":       output_tokens,
        "request_id":          request_id,
    }
    logger.info(
        "Usage for %s — input: %s (cached: %s), output: %s",
        step_name, input_tokens, cached_input_tokens, output_tokens,
    )
    return step_usage


def _extract_tavily_usage(response_json: Dict[str, Any]) -> Dict[str, Any]:
    usage   = response_json.get("usage") or {}
    credits = int(usage.get("credits") or 0)
    return {"credits": credits, "raw_usage": usage or None}

# -----------------------------------------------------------------------------
# Sync helpers — these are fast (disk / small HTTP) so they're fine as sync.
# The caller wraps them in asyncio.to_thread() to avoid blocking the loop.
# -----------------------------------------------------------------------------

def _ensure_existing_file(path: Path, *, description: str) -> Path:
    if not path.exists() or not path.is_file():
        raise DeepResearchError(
            f"Required {description} file not found at path: {path}."
        )
    return path


def _truncate_to_token_limit(text: str, *, source: str = "<text>") -> str:
    """Trim text to MAX_LOADED_TEXT_TOKENS using a chars-per-token heuristic (_CHARS_PER_TOKEN).

    Truncates from the end so the beginning of the document is preserved.
    Quick comment:
        Cutting annual report at the end is not the most efficient way to preserve all important informaiton but 
        1) Majority of annual reports are short enough and so cutting is not needed. 
        2) Creating workflow where the annual report is split into sections so LLM can make short summaries would be much less token efficient.
        3) Even if the long annaul report is cut, in most cases the most important information is preserved as it's usually in the beginning or middle of the report.
    """
    if len(text) <= _MAX_LOADED_TEXT_CHARS:
        return text
    logger.warning(
        "Loaded text from %s exceeds ~%s tokens (%s chars); truncating end.",
        source, MAX_LOADED_TEXT_TOKENS, len(text),
    )
    return text[:_MAX_LOADED_TEXT_CHARS]


def _load_text(path: Path) -> str:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return _truncate_to_token_limit(text, source=str(path))


def fetch_and_parse_company_data(company_symbol: str, exchange_symbol: str) -> Dict[str, Any]:
    """Sync fundamentals fetch + parse. Caller wraps in to_thread."""
    logger.info("Loading Yahoo Finance fundamentals for %s", company_symbol)
    raw = fetch_fundamentals(company_symbol, exchange_symbol)
    if not isinstance(raw, dict):
        raise CompanyDataParseError("Fundamentals response is not a JSON object")
    if "General" not in raw:
        raise MissingRequiredFieldError("Fundamentals response missing required 'General' field")
    return extract_data(raw)


def _get_company_name(parsed_data: Dict[str, Any]) -> str:
    name = (parsed_data.get("General") or {}).get("Name") or ""
    if not name:
        raise MissingRequiredFieldError("Parsed company data missing General.Name")
    return name


def get_annual_report_pdf_path(override_path: Optional[str | Path]) -> Path:
    if override_path is None:
        raise AnnualReportFileNotFoundError(
            "Annual report PDF path was not provided."
        )
    path = Path(override_path)
    if not path.exists() or not path.is_file():
        raise AnnualReportFileNotFoundError(
            f"Annual report PDF file not found at path: {path}"
        )
    return path


def extract_pdf_text_with_pymupdf(pdf_path: Path) -> str:
    try:
        md_text = pymupdf4llm.to_markdown(str(pdf_path))
    except Exception as exc:
        logger.exception("Failed to extract text from PDF at %s", pdf_path)
        raise AnnualReportExtractionError(
            f"Failed to extract text from annual report PDF at path: {pdf_path}"
        ) from exc

    stripped = md_text.strip()
    if not stripped:
        raise AnnualReportExtractionError(
            f"No content extracted from annual report PDF at path: {pdf_path}"
        )
    logger.info("Extracted text from annual report PDF at path: %s", pdf_path)
    return _truncate_to_token_limit(stripped, source=str(pdf_path))

# -----------------------------------------------------------------------------
# Async helpers
# -----------------------------------------------------------------------------

def _parse_json_from_output_text(output_text: str) -> Dict[str, Any]:
    text = output_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMOutputError(f"Model output is not valid JSON: {e}") from e


async def run_tavily_research(
    company_name: str,
    symbol: str,
    exchange: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Unchanged — already async."""
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
    if not TAVILY_API_KEY:
        raise DeepResearchError("TAVILY_API_KEY environment variable is not set")

    client = AsyncTavilyClient(api_key=TAVILY_API_KEY)
    strategic_q   = (
        f"Have there been any major strategic shifts in {company_name} "
        f"({symbol}) business model in the last few years?"
    )
    competitors_q = (
        f"Describe {company_name} ({symbol}) main competitors and how they compete."
    )
    logger.info("Running Tavily research for %s (%s.%s)", company_name, symbol, exchange)
    common_kwargs1 = dict(
        search_depth="basic",
        max_results=15,
        topic="finance",
        time_range="year",
        include_usage=True,
    )
    common_kwargs2 = dict(
        search_depth="basic",
        max_results=15,
        include_usage=True,
    )
    try:
        strategic_json, competitors_json = await asyncio.gather(
            asyncio.wait_for(
                client.search(query=strategic_q, **common_kwargs1),
                timeout=30,
            ),
            asyncio.wait_for(
                client.search(query=competitors_q, **common_kwargs2),
                timeout=30,
            ),
        )
    except asyncio.TimeoutError:
        raise DeepResearchError("Tavily request timed out")
    return strategic_json, competitors_json

# -----------------------------------------------------------------------------
# OpenAI schema 
# -----------------------------------------------------------------------------

DEEP_REPORT_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Output of the deep stock research. Contains the report body in Markdown.",
    "properties": {
        "markdown": {
            "type": "string",
            "description": "The deep research report body in GitHub-Flavored Markdown.",
        }
    },
    "required": ["markdown"],
    "additionalProperties": False,
}


def _build_deep_user_input(
    methodology_text: str,
    parsed_company_data: Dict[str, Any],
    tavily_strategic: Dict[str, Any],
    tavily_competitors: Dict[str, Any],
    annual_report_label: str,
) -> Tuple[str, str]:
    data_str        = json.dumps(parsed_company_data, indent=2, ensure_ascii=False)
    strategic_str   = json.dumps(tavily_strategic,    indent=2, ensure_ascii=False)
    competitors_str = json.dumps(tavily_competitors,  indent=2, ensure_ascii=False)
    base_1 = (
        "<Deep_Research_Methodology>\n\n"
        f"{methodology_text}\n\n"
        "</Deep_Research_Methodology>\n\n"
        "---\n\n"
        f"<Up_To_Date_Financial_Data>\n\n"
        f"```json\n{data_str}\n```\n\n"
        f"</Up_To_Date_Financial_Data>\n\n"
        "---\n\n"
        "<Latest_Annual_Report>\n\n"
    )
    base_2 = (
        "</Latest_annual_report>\n\n"
        "---\n\n"

        "<Web_Search_Strategic_Shifts>\n\n"
        f"```json\n{strategic_str}\n```\n\n"
        "</Web_Search_Strategic_Shifts>\n\n"
        "<Web_Search_Competitors>\n\n"
        f"```json\n{competitors_str}\n```\n\n"
        "</Web_Search_Competitors>\n\n"
    )
    return base_1, base_2


def _resolve_model(model: str | None) -> str:
    if model is not None:
        return model
    resolved = os.getenv("DEEP_RESEARCH_MODEL")
    if not resolved:
        raise RuntimeError("DEEP_RESEARCH_MODEL environment variable is not set.")
    return resolved

# -----------------------------------------------------------------------------
# Core OpenAI call — now async
# -----------------------------------------------------------------------------

async def _call_deep_research_llm_async(
    system_text: str,
    base_1: str,
    base_2: str,
    annual_report_text: str,
    *,
    model: str | None = None,
) -> Tuple[str, Any]:
    """
    Async OpenAI call.  Replaces the old sync _call_deep_research_llm.

    The key change is a single word: AsyncOpenAI instead of OpenAI, and
    `await` before client.responses.create().  Everything else is identical.
    """
    model = _resolve_model(model)
    logger.info("Calling OpenAI deep research model (%s)", model)

    client = AsyncOpenAI(timeout=1000)   

    content_items: List[Dict[str, Any]] = [
        {"type": "input_text", "text": base_1},
        {"type": "input_text", "text": annual_report_text},
        {"type": "input_text", "text": base_2},
    ]

    response = await client.responses.create( 
        model=model,
        instructions=system_text,
        input=[{"role": "user", "content": content_items}],
        text={
            "format": {
                "type":        "json_schema",
                "description": "Generate deep research report",
                "name":        "deep_research_report",
                "strict":      True,
                "schema":      DEEP_REPORT_JSON_SCHEMA,
            }
        },
        max_output_tokens=100000,
        prompt_cache_retention="24h",
        reasoning={"effort": "high", "summary": "auto"},
        store=True,
    )

    refusal = getattr(response, "refusal", None)
    if refusal:
        raise LLMOutputError(f"Model refused the request: {refusal}")

    output_text = getattr(response, "output_text", None)
    if output_text is None and hasattr(response, "output"):
        parts: List[str] = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) == "message":
                for content in getattr(item, "content", []) or []:
                    if getattr(content, "type", None) == "output_text":
                        text = getattr(content, "text", None)
                        if text:
                            parts.append(text)
        output_text = "\n".join(parts) if parts else None

    if not output_text or not str(output_text).strip():
        raise LLMOutputError("Deep research model returned no output text")

    data     = _parse_json_from_output_text(str(output_text))
    markdown = data.get("markdown")
    if not isinstance(markdown, str):
        raise LLMOutputError("Deep research JSON missing 'markdown' string field")

    return markdown, response

# -----------------------------------------------------------------------------
# Report assembly
# -----------------------------------------------------------------------------

def build_deep_research_report(markdown: str) -> str:
    if not DISCLAIMER_PATH.exists():
        logger.error(
            "Disclaimer file not found",
            extra={"path": str(DISCLAIMER_PATH)},
        )
        raise DeepResearchError(
            f"Disclaimer file not found at path: {DISCLAIMER_PATH}"
        )
        
    disclaimer_text = _load_text(DISCLAIMER_PATH)
    return f"{markdown.strip()}\n\n---\n\n{disclaimer_text.strip()}"


def _build_usage_summary(
    step_usages:   Dict[str, Dict[str, Any]],
    tavily_usages: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    totals: Dict[str, Any] = {
        "input_tokens":        0,
        "cached_input_tokens": 0,
        "output_tokens":       0,
    }
    for usage in step_usages.values():
        if usage.get("model"):
            totals["input_tokens"]        += usage.get("input_tokens",        0)
            totals["cached_input_tokens"] += usage.get("cached_input_tokens", 0)
            totals["output_tokens"]       += usage.get("output_tokens",       0)

    totals["tavily_credits"] = sum(int(u.get("credits", 0)) for u in tavily_usages.values())

    steps: Dict[str, Dict[str, Any]] = dict(step_usages)
    for name, usage in tavily_usages.items():
        steps[name] = {"credits": int(usage.get("credits", 0)), "raw_usage": usage.get("raw_usage")}

    return {"steps": steps, "totals": totals}

# -----------------------------------------------------------------------------
# Main orchestration — now a coroutine
#
# Called with `await` from the executor's _generate_report_async.
# The old sync run_deep_research is kept as a thin wrapper for any code
# (tests, management commands) that still calls it synchronously.
# -----------------------------------------------------------------------------

async def run_deep_research_async(
    company_symbol:  str,
    exchange_symbol: str,
    *,
    model:               str | None           = None,
    annual_report_path:  Optional[str | Path] = None,
) -> Tuple[str, str]:
    """
    Async orchestration of the full deep research pipeline.

    Steps that are sync but fast (fundamentals fetch, PDF extraction) are wrapped in
    asyncio.to_thread() so they run in a thread pool without blocking the event
    loop.  The long wait (OpenAI ~5 min) is a native coroutine await — the
    loop is free to run all other in-flight jobs during that time.
    """
    logger.info("Starting deep research for %s", company_symbol)
    model = _resolve_model(model)
    step_usages:   Dict[str, Dict[str, Any]] = {}
    tavily_usages: Dict[str, Dict[str, Any]] = {}

    # 1) Validate annual report path — fast, sync, no need for to_thread
    annual_report_pdf_path = get_annual_report_pdf_path(annual_report_path)

    # 2) Validate prompt/methodology files — fast disk check
    system_prompt_path = _ensure_existing_file(DEEP_SYSTEM_PROMPT_PATH, description="deep research system prompt")
    methodology_path   = _ensure_existing_file(DEEP_METHODOLOGY_PATH,   description="deep research methodology")

    # 3) Yahoo Finance fundamentals — sync call, wrap so loop stays free
    parsed_company_data = await asyncio.to_thread(
        fetch_and_parse_company_data, company_symbol, exchange_symbol
    )
    company_name = _get_company_name(parsed_company_data)

    # 4) Tavily research — already async, await directly
    strategic_json, competitors_json = await run_tavily_research(
        company_name, company_symbol, exchange_symbol
    )
    tavily_usages["tavily_strategic_shifts"] = _extract_tavily_usage(strategic_json)
    tavily_usages["tavily_competitors"]      = _extract_tavily_usage(competitors_json)

    # 5) Load prompt files (disk) and extract PDF text (CPU) — both sync, wrap them
    system_text, methodology_text, annual_report_text = await asyncio.gather(
        asyncio.to_thread(_load_text, system_prompt_path),
        asyncio.to_thread(_load_text, methodology_path),
        asyncio.to_thread(extract_pdf_text_with_pymupdf, annual_report_pdf_path),
    )

    # Delete the uploaded PDF as soon as we've extracted its text.
    # Cleanup is best-effort — the executor's finally block also attempts this.
    try:
        annual_report_pdf_path.unlink(missing_ok=True)
    except Exception:
        logger.warning("Failed to delete annual report PDF after extraction", extra={"path": str(annual_report_pdf_path)})

    # 6) Build prompt payload
    base_1, base_2 = _build_deep_user_input(
        methodology_text,
        parsed_company_data,
        strategic_json,
        competitors_json,
        annual_report_pdf_path.name,
    )

    # 7) OpenAI call — the long wait; truly non-blocking as a coroutine
    markdown, deep_response = await _call_deep_research_llm_async(
        system_text, base_1, base_2, annual_report_text, model=model,
    )
    step_usages["deep_research"] = _extract_openai_usage("deep_research", deep_response, model)

    # 8) Assemble final report
    final_report  = build_deep_research_report(markdown)
    usage_summary = _build_usage_summary(step_usages, tavily_usages)
    usage_json    = json.dumps(usage_summary, ensure_ascii=False, indent=2)

    logger.info("Finished deep research for %s", company_symbol)
    return final_report, usage_json


def run_deep_research(
    company_symbol:  str,
    exchange_symbol: str,
    *,
    model:              str | None           = None,
    annual_report_path: Optional[str | Path] = None,
) -> Tuple[str, str]:
    """
    Sync wrapper kept for tests and management commands.
    Do not call from the executor — use run_deep_research_async instead.
    """
    return asyncio.run(
        run_deep_research_async(
            company_symbol,
            exchange_symbol,
            model=model,
            annual_report_path=annual_report_path,
        )
    )


__all__ = [
    "DeepResearchError",
    "CompanyDataParseError",
    "MissingRequiredFieldError",
    "LLMOutputError",
    "AnnualReportFileNotFoundError",
    "AnnualReportExtractionError",
    "run_deep_research_async",
    "run_deep_research",
]
