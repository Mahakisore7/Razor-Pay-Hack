"""Logging must never leak PII, and must be joinable to a trace (TR-73, TR-74)."""

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from recoup.platform.logging import configure_logging, current_trace_id, get_logger, redact_pii


def test_pii_keyed_fields_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    logger = get_logger("test")
    logger.info("contacted_customer", phone="+919876543210", customer_email="a@b.com", case_id="c1")

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["phone"] == "***REDACTED***"
    assert line["customer_email"] == "***REDACTED***"
    assert line["case_id"] == "c1"  # non-PII fields pass through untouched


def test_log_line_carries_the_active_trace_id(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    logger = get_logger("test")
    with tracer.start_as_current_span("do_work") as span:
        logger.info("something_happened")
        expected_trace_id = format(span.get_span_context().trace_id, "032x")

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["trace_id"] == expected_trace_id


def test_log_line_has_no_trace_id_outside_a_span(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    logger = get_logger("test")
    logger.info("no_span_active")

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "trace_id" not in line


def test_redact_pii_masks_pii_keyed_fields_only() -> None:
    masked = redact_pii({"phone": "+919876543210", "customer_email": "a@b.com", "case_id": "c1"})

    assert masked == {
        "phone": "***REDACTED***",
        "customer_email": "***REDACTED***",
        "case_id": "c1",
    }


def test_current_trace_id_matches_the_active_span() -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("do_work") as span:
        found = current_trace_id()
        expected = format(span.get_span_context().trace_id, "032x")

    assert found == expected


def test_current_trace_id_generates_one_outside_a_span() -> None:
    first = current_trace_id()
    second = current_trace_id()

    assert len(first) == 32
    int(first, 16)  # is valid hex
    assert first != second  # never reused across calls
