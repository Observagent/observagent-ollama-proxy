"""Offline checks; no Ollama server or backend credentials required."""
import unittest
import os
import time
from unittest.mock import patch

import httpx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor, InMemoryLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from ollama_proxy import app, lifespan, finish_request
from opentelemetry.trace import StatusCode


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"message":{"content":"hello"},"done":false}\n'
        yield b'{"done":true,"prompt_eval_'
        yield b'count":7,"eval_count":3,"total_duration":2000000}\n'


class Version(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"version":"test"}'


class ProxyTest(unittest.IsolatedAsyncioTestCase):
    async def test_exporter_configuration_and_shutdown(self):
        with patch.dict(os.environ, {
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://example.test/v1/traces",
            "OBSERVAGENT_API_KEY": "test-key",
            "OBSERVAGENT_SESSION_ID": "test-session",
        }, clear=True), patch("ollama_proxy.OTLPSpanExporter") as traces, \
                patch("ollama_proxy.OTLPLogExporter") as logs, \
                patch("ollama_proxy.OTLPMetricExporter") as metrics:
            async with lifespan(app):
                logs.assert_called_once_with(endpoint="https://example.test/v1/logs",
                    headers={"Authorization": "Bearer test-key"}, timeout=10)
                metrics.assert_called_once_with(endpoint="https://example.test/v1/metrics",
                    headers={"Authorization": "Bearer test-key"}, timeout=10)
                self.assertEqual(app.state.telemetry_logger.resource.attributes["session.id"], "test-session")
            logs.return_value.shutdown.assert_called_once()
            traces.return_value.shutdown.assert_called_once()
            metrics.return_value.shutdown.assert_called_once()

    async def test_failed_request_log(self):
        provider = TracerProvider()
        logs = InMemoryLogRecordExporter()
        log_provider = LoggerProvider()
        log_provider.add_log_record_processor(SimpleLogRecordProcessor(logs))
        span = provider.get_tracer("test").start_span("ollama.generate")
        span.set_status(StatusCode.ERROR, "ConnectError")
        finish_request(span, log_provider.get_logger("test"), time.monotonic())
        record = logs.get_finished_logs()[0].log_record
        self.assertEqual(record.attributes["event.name"], "ollama.request.failed")
        self.assertEqual(record.severity_text, "ERROR")
        self.assertNotIn("input_token_count", record.attributes)
        provider.shutdown()
        log_provider.shutdown()

    async def test_forwarding_and_split_usage(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        app.state.tracer = provider.get_tracer("test")
        logs = InMemoryLogRecordExporter()
        log_provider = LoggerProvider()
        log_provider.add_log_record_processor(SimpleLogRecordProcessor(logs))
        app.state.telemetry_logger = log_provider.get_logger("test")
        metric_reader = InMemoryMetricReader()
        metric_provider = MeterProvider(metric_readers=[metric_reader])
        meter = metric_provider.get_meter("test")
        app.state.telemetry_metrics = {
            "requests": meter.create_counter("ollama.request.count", unit="{request}"),
            "duration": meter.create_histogram("ollama.request.duration", unit="s"),
            "tokens": meter.create_counter("ollama.token.count", unit="{token}"),
        }

        def upstream(request):
            self.assertEqual(request.url.host, "127.0.0.1")
            self.assertEqual(request.url.port, 11434)
            if request.url.path == "/api/version":
                return httpx.Response(200, stream=Version())
            self.assertEqual(request.url.query, b"test=1")
            return httpx.Response(200, stream=Chunks(), headers={"content-type": "application/x-ndjson"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as backend:
            app.state.client = backend
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as cli:
                response = await cli.get("/api/version")
                self.assertEqual(response.json(), {"version": "test"})
                response = await cli.post("/api/chat?test=1", json={"model": "test-model"})
                self.assertEqual(response.status_code, 200)
                self.assertIn('"content":"hello"', response.text)
                self.assertEqual(len(response.text.splitlines()), 2)
        spans = exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].attributes["gen_ai.usage.input_tokens"], 7)
        self.assertEqual(spans[0].attributes["gen_ai.usage.output_tokens"], 3)
        self.assertEqual(spans[0].attributes["ollama.total_duration_ms"], 2)
        records = logs.get_finished_logs()
        self.assertEqual(len(records), 1)
        record = records[0].log_record
        self.assertEqual(record.attributes["event.name"], "ollama.request.completed")
        self.assertEqual(record.attributes["input_token_count"], 7)
        self.assertEqual(record.attributes["output_token_count"], 3)
        self.assertEqual(record.attributes["gen_ai.request.model"], "test-model")
        self.assertEqual(record.trace_id, spans[0].context.trace_id)
        self.assertNotIn("hello", str(record.attributes))
        metric_data = metric_reader.get_metrics_data()
        metrics = {m.name: m for rm in metric_data.resource_metrics
                   for sm in rm.scope_metrics for m in sm.metrics}
        self.assertEqual(metrics["ollama.request.count"].data.data_points[0].value, 1)
        self.assertEqual(metrics["ollama.request.duration"].data.data_points[0].count, 1)
        self.assertEqual(sorted(point.value for point in
            metrics["ollama.token.count"].data.data_points), [3, 7])
        log_provider.shutdown()
        provider.shutdown()
        metric_provider.shutdown()


if __name__ == "__main__":
    unittest.main()
