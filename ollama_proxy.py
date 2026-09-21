"""Local Ollama CLI proxy exporting usage traces and request logs to Observagent."""
import asyncio
import json
import logging
import os
import time
from urllib.parse import urlsplit, urlunsplit
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode, set_span_in_context

log = logging.getLogger("uvicorn.error")
UPSTREAM = "http://127.0.0.1:11434"
HOP_HEADERS = {"host", "connection", "keep-alive", "transfer-encoding",
               "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate"}


def forward_headers(headers):
    excluded = HOP_HEADERS | {v.strip().lower() for v in headers.get("connection", "").split(",")}
    return {k: v for k, v in headers.items() if k.lower() not in excluded}


@asynccontextmanager
async def lifespan(app):
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    key = os.environ.get("OBSERVAGENT_API_KEY")
    if not endpoint or not key:
        raise RuntimeError("Set OTEL_EXPORTER_OTLP_TRACES_ENDPOINT and OBSERVAGENT_API_KEY first")
    parsed = urlsplit(endpoint)
    logs_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
    if not logs_endpoint:
        if not parsed.path.endswith('/v1/traces'):
            raise RuntimeError("Trace endpoint must end in /v1/traces, or set OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
        logs_endpoint = urlunsplit(parsed._replace(path=parsed.path[:-len('traces')] + 'logs'))
    resource = Resource.create({
        "service.name": "ollama-cli", "agent.id": "ollama-cli",
        "session.id": os.environ.get("OBSERVAGENT_SESSION_ID", str(uuid4())),
    })
    provider = TracerProvider(resource=resource)
    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(
        endpoint=logs_endpoint, headers={"Authorization": f"Bearer {key}"}, timeout=10,
    ), schedule_delay_millis=1000))
    app.state.telemetry_logger = log_provider.get_logger("observagent.ollama-proxy")
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=endpoint, headers={"Authorization": f"Bearer {key}"}, timeout=10,
    ), schedule_delay_millis=1000))
    app.state.tracer = provider.get_tracer("observagent.ollama-proxy")
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10), trust_env=False) as client:
        app.state.client = client
        try:
            yield
        finally:
            await asyncio.to_thread(provider.shutdown)
            await asyncio.to_thread(log_provider.shutdown)


app = FastAPI(lifespan=lifespan)


def record_usage(span, line):
    try:
        data = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(data, dict):
        return
    if data.get("error"):
        span.set_status(StatusCode.ERROR, "Ollama returned an error")
    if data.get("done") is not True:
        return
    span.set_attribute("ollama.done", True)
    for source, target in (("prompt_eval_count", "gen_ai.usage.input_tokens"),
                           ("eval_count", "gen_ai.usage.output_tokens")):
        if isinstance(data.get(source), int):
            span.set_attribute(target, data[source])
    for name in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
        if isinstance(data.get(name), (int, float)):
            span.set_attribute(f"ollama.{name}_ms", data[name] / 1_000_000)


def finish_request(span, logger, started):
    """Emit one metadata-only request log, correlated with the existing trace."""
    attrs = dict(span.attributes)
    failed = span.status.status_code == StatusCode.ERROR
    complete = attrs.get("ollama.done", False)
    event = "ollama.request.failed" if failed else (
        "ollama.request.completed" if complete else "ollama.request.incomplete")
    attrs.update({"event.name": event, "duration_ms": (time.monotonic() - started) * 1000})
    for source, target in (("gen_ai.usage.input_tokens", "input_token_count"),
                           ("gen_ai.usage.output_tokens", "output_token_count")):
        if source in attrs:
            attrs[target] = attrs[source]
    try:
        logger.emit(
            timestamp=time.time_ns(), context=set_span_in_context(span),
            severity_number=SeverityNumber.ERROR if failed else SeverityNumber.INFO,
            severity_text="ERROR" if failed else "INFO",
            body=event, attributes=attrs,
        )
        log.info("%s: input=%s output=%s; trace and log queued for export",
                 event, attrs.get("input_token_count"), attrs.get("output_token_count"))
    except Exception:
        log.exception("Could not queue Ollama request log")
    finally:
        span.end()


@app.api_route("/{path:path}", methods=["GET", "POST", "HEAD", "DELETE", "PUT", "OPTIONS", "PATCH"])
async def proxy(path: str, request: Request):
    body = await request.body()
    span = None
    started = time.monotonic()
    if path in ("api/chat", "api/generate") and request.method == "POST":
        span = request.app.state.tracer.start_span("ollama.generate", kind=SpanKind.CLIENT)
        span.set_attribute("gen_ai.provider.name", "ollama")
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and isinstance(payload.get("model"), str):
                span.set_attribute("gen_ai.request.model", payload["model"])
        except ValueError:
            pass
    headers = forward_headers(request.headers)
    headers["accept-encoding"] = "identity"
    url = httpx.URL(UPSTREAM + "/" + path).copy_with(query=request.scope["query_string"])
    client = request.app.state.client
    try:
        upstream = await client.send(client.build_request(request.method, url, content=body, headers=headers), stream=True)
    except BaseException as exc:
        if span:
            span.set_status(StatusCode.ERROR, type(exc).__name__)
            finish_request(span, request.app.state.telemetry_logger, started)
        if isinstance(exc, httpx.HTTPError):
            return JSONResponse({"error": "Cannot reach Ollama on 127.0.0.1:11434"}, status_code=502)
        raise
    if span:
        span.set_attribute("http.response.status_code", upstream.status_code)
        if upstream.status_code >= 400:
            span.set_status(StatusCode.ERROR, "Ollama HTTP error")

    async def stream():
        pending = b""
        inspect = span is not None
        try:
            async for chunk in upstream.aiter_raw():
                if inspect:
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        record_usage(span, line)
                    if len(pending) > 8 * 1024 * 1024:
                        inspect = False
                        pending = b""
                        span.set_attribute("ollama.usage_capture_truncated", True)
                yield chunk
            if inspect and pending:
                record_usage(span, pending)
        except BaseException as exc:
            if span:
                span.set_status(StatusCode.ERROR, type(exc).__name__)
            raise
        finally:
            if span:
                finish_request(span, request.app.state.telemetry_logger, started)
            await upstream.aclose()

    return StreamingResponse(stream(), status_code=upstream.status_code,
                             headers=forward_headers(upstream.headers))
