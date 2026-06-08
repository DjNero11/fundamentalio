"""
Quick Stock Research module.

Fetches company fundamentals from Yahoo Finance (yfinance), runs GPT-based quick
research with strict JSON output, and produces a Markdown report.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI         

from fundamentalio.services.research_helpers.shared.api import fetch_fundamentals
from fundamentalio.services.research_helpers.shared.json_parse import extract_data

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Custom exceptions 
# -----------------------------------------------------------------------------

class CompanyDataParseError(Exception):
    """Raised when fundamentals data is invalid or missing required structure."""

class MissingRequiredFieldError(CompanyDataParseError):
    """Raised when a required field is missing from the fundamentals payload."""

class LLMOutputError(Exception):
    """Raised when LLM returns invalid JSON, missing markdown, or refusal."""

# -----------------------------------------------------------------------------
# Default paths 
# -----------------------------------------------------------------------------

_MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_SYSTEM_PROMPT_PATH = _MODULE_DIR / "prompts" / "quick_research_system_prompt.md"
DEFAULT_METHODOLOGY_PATH   = _MODULE_DIR / "prompts" / "quick_research_methodology.md"
DEFAULT_DISCLAIMER_PATH    = _MODULE_DIR.parent / "shared" / "disclaimer.md"

# -----------------------------------------------------------------------------
# OpenAI structured output schema 
# -----------------------------------------------------------------------------

REPORT_JSON_SCHEMA = {
    "type": "object",
    "description": (
        "Output of the quick stock research. Contains a single field with the "
        "report body in Markdown."
    ),
    "properties": {
        "markdown": {
            "type": "string",
            "description": "The quick research report body in GitHub-compatible Markdown.",
        }
    },
    "required": ["markdown"],
    "additionalProperties": False,
}

# -----------------------------------------------------------------------------
# Sync helpers — fast disk/parse work, fine to call directly or via to_thread
# -----------------------------------------------------------------------------

def _load_text(path: Path) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_prompts(
    system_prompt_path: Path | None = None,
    methodology_path: Path | None = None,
) -> tuple[str, str]:
    system_path    = system_prompt_path or DEFAULT_SYSTEM_PROMPT_PATH
    methodology_path = methodology_path or DEFAULT_METHODOLOGY_PATH
    return _load_text(system_path), _load_text(methodology_path)


def build_user_prompt(methodology_text: str, company_data: dict) -> str:
    data_str = json.dumps(company_data, indent=2, ensure_ascii=False)
    downloaded_today = date.today().strftime("%d-%m-%y")
    return (
        f"{methodology_text}\n\n---\n\nData downloaded today: {downloaded_today}\n\n"
        f"## Company data \n\n```json\n{data_str}\n```"
    )


def fetch_and_parse_company_data(company_symbol: str, exchange_symbol: str) -> dict:
    """Sync fundamentals fetch. Caller wraps in to_thread if needed."""
    logger.info("Loading Yahoo Finance fundamentals for %s", company_symbol)
    raw = fetch_fundamentals(company_symbol, exchange_symbol)
    logger.info("Parsing fundamentals payload")
    if not isinstance(raw, dict):
        raise CompanyDataParseError("Fundamentals response is not a JSON object")
    if "General" not in raw:
        raise MissingRequiredFieldError("Fundamentals response missing required 'General' field")
    return extract_data(raw)


def parse_llm_response(output_text: str) -> str:
    """Parse JSON from model output and return the 'markdown' field."""
    output_text = output_text.strip()
    if output_text.startswith("```"):
        lines = output_text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        output_text = "\n".join(lines)
    try:
        data = json.loads(output_text)
    except json.JSONDecodeError as e:
        raise LLMOutputError(f"Model output is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LLMOutputError("Model output JSON is not an object")
    if "markdown" not in data:
        raise LLMOutputError("Model output JSON missing required 'markdown' field")
    markdown = data["markdown"]
    if not isinstance(markdown, str):
        raise LLMOutputError("Model output 'markdown' field is not a string")
    return markdown


def build_report(markdown: str, disclaimer_path: Path | None = None) -> str:
    disclaimer_path = disclaimer_path or DEFAULT_DISCLAIMER_PATH

    if not disclaimer_path.exists():
        logger.error("Disclaimer file not found at path: %s", disclaimer_path)
        raise Exception(f"Disclaimer file missing: {disclaimer_path}")

    disclaimer_text = _load_text(disclaimer_path)
    return f"{markdown.strip()}\n\n---\n\n{disclaimer_text.strip()}"


def _resolve_model(model: str | None) -> str:
    if model is not None:
        return model
    resolved = os.getenv("QUICK_RESEARCH_MODEL")
    if not resolved:
        raise RuntimeError("QUICK_RESEARCH_MODEL environment variable is not set.")
    return resolved

# -----------------------------------------------------------------------------
# Async LLM call — matches the pattern used in deep_research_main
# -----------------------------------------------------------------------------

async def _call_llm_async(
    instructions: str,
    input_text: str,
    model: str | None = None,
) -> tuple[str, Any]:
    """
    Async OpenAI call using AsyncOpenAI — consistent with deep_research_main.
    While awaiting the response the event loop is free to handle other jobs.
    """
    model = _resolve_model(model)
    logger.info("Calling OpenAI Responses API (model=%s)", model)
    client = AsyncOpenAI(timeout=600)

    response = await client.responses.create(
        model=model,
        instructions=instructions,
        input=input_text,
        max_output_tokens=15000,
        text={
            "format": {
                "type":        "json_schema",
                "description": "Generate quick research report",
                "name":        "quick_research_report",
                "strict":      True,
                "schema":      REPORT_JSON_SCHEMA,
            }
        },
        prompt_cache_retention="24h",
        reasoning={"effort": "medium", "summary": "auto"},
        store=True,
    )

    refusal = getattr(response, "refusal", None)
    if refusal:
        raise LLMOutputError(f"Model refused to fulfill the request: {refusal}")

    output_text = getattr(response, "output_text", None)
    if output_text is None and hasattr(response, "output"):
        parts = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) == "message":
                for content in getattr(item, "content", []) or []:
                    if getattr(content, "type", None) == "output_text":
                        text = getattr(content, "text", None)
                        if text:
                            parts.append(text)
        output_text = "\n".join(parts) if parts else None

    if not output_text or not output_text.strip():
        raise LLMOutputError("Model returned no output text")

    return output_text, response

# -----------------------------------------------------------------------------
# Main entry points
# -----------------------------------------------------------------------------

async def run_quick_research_async(
    company_symbol: str,
    exchange_symbol: str,
    *,
    system_prompt_path: Path | None = None,
    methodology_path:   Path | None = None,
    disclaimer_path:    Path | None = None,
    model: str | None = None,
) -> str:
    """
    Async version — called directly from the executor with await.
    Fundamentals fetch is sync but fast; wrapped in to_thread so the loop stays free.
    OpenAI call is fully async via AsyncOpenAI.
    """
    logger.info("Starting quick research for %s", company_symbol)
    model = _resolve_model(model)

    # Fast sync work — wrap in to_thread so the event loop isn't blocked
    system_text, methodology_text = await asyncio.to_thread(
        load_prompts, system_prompt_path, methodology_path
    )
    company_data = await asyncio.to_thread(
        fetch_and_parse_company_data, company_symbol, exchange_symbol
    )

    user_prompt = build_user_prompt(methodology_text, company_data)

    # Async OpenAI call — non-blocking
    raw_output, _ = await _call_llm_async(system_text, user_prompt, model=model)
    markdown = parse_llm_response(raw_output)

    report_content = build_report(markdown, disclaimer_path)
    logger.info("Finished quick research for %s", company_symbol)
    return report_content


def run_quick_research(
    company_symbol: str,
    exchange_symbol: str,
    *,
    system_prompt_path: Path | None = None,
    methodology_path:   Path | None = None,
    disclaimer_path:    Path | None = None,
    model: str | None = None,
) -> str:
    """
    Sync wrapper — kept for tests and management commands.
    Do not call from the executor — use run_quick_research_async instead.
    """
    return asyncio.run(
        run_quick_research_async(
            company_symbol,
            exchange_symbol,
            system_prompt_path=system_prompt_path,
            methodology_path=methodology_path,
            disclaimer_path=disclaimer_path,
            model=model,
        )
    )


__all__ = [
    "CompanyDataParseError",
    "MissingRequiredFieldError",
    "LLMOutputError",
    "run_quick_research_async",
    "run_quick_research",
]