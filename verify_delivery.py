"""Verify authenticated OTLP storage. Sends synthetic records unless --empty."""
import argparse
import os
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import requests
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--empty', action='store_true', help='Test authentication without creating records')
    args = parser.parse_args()
    endpoint = os.environ.get('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', '')
    key = os.environ.get('OBSERVAGENT_API_KEY', '')
    if not endpoint or not key:
        parser.error('Set OTEL_EXPORTER_OTLP_TRACES_ENDPOINT and OBSERVAGENT_API_KEY in this terminal')
    parsed = urlsplit(endpoint)
    if not parsed.path.endswith('/v1/traces'):
        parser.error('Trace endpoint must end in /v1/traces')
    logs_endpoint = os.environ.get('OTEL_EXPORTER_OTLP_LOGS_ENDPOINT') or urlunsplit(
        parsed._replace(path=parsed.path[:-len('traces')] + 'logs'))
    metrics_endpoint = os.environ.get('OTEL_EXPORTER_OTLP_METRICS_ENDPOINT') or urlunsplit(
        parsed._replace(path=parsed.path[:-len('traces')] + 'metrics'))
    traces, logs, metrics = ExportTraceServiceRequest(), ExportLogsServiceRequest(), ExportMetricsServiceRequest()
    if not args.empty:
        trace_resource = traces.resource_spans.add()
        log_resource = logs.resource_logs.add()
        for resource in (trace_resource.resource, log_resource.resource):
            for name, value in {'service.name': 'ollama-cli', 'agent.id': 'ollama-cli',
                                'session.id': 'ollama-delivery-test'}.items():
                attr = resource.attributes.add()
                attr.key = name
                attr.value.string_value = value
        span = trace_resource.scope_spans.add().spans.add()
        span.name = 'ollama.delivery-test'
        span.trace_id, span.span_id = uuid4().bytes, os.urandom(8)
        span.start_time_unix_nano = time.time_ns()
        span.end_time_unix_nano = span.start_time_unix_nano + 1_000_000
        record = log_resource.scope_logs.add().log_records.add()
        record.time_unix_nano = record.observed_time_unix_nano = time.time_ns()
        record.trace_id, record.span_id = span.trace_id, span.span_id
        record.severity_number, record.severity_text = 9, 'INFO'
        record.body.string_value = 'Synthetic Ollama delivery check'
        attr = record.attributes.add()
        attr.key, attr.value.string_value = 'event.name', 'ollama.delivery-test'
        metric_resource = metrics.resource_metrics.add()
        for name, value in {'service.name': 'ollama-cli', 'agent.id': 'ollama-cli',
                            'session.id': 'ollama-delivery-test'}.items():
            attr = metric_resource.resource.attributes.add()
            attr.key = name
            attr.value.string_value = value
        metric = metric_resource.scope_metrics.add().metrics.add()
        metric.name = 'ollama.delivery.test'
        point = metric.sum.data_points.add()
        point.as_int = 1
        point.time_unix_nano = time.time_ns()
    failed = False
    for signal, url, batch in [('traces', endpoint, traces), ('logs', logs_endpoint, logs),
                               ('metrics', metrics_endpoint, metrics)]:
        try:
            response = requests.post(url, headers={'Authorization': f'Bearer {key}',
                'Content-Type': 'application/x-protobuf'}, data=batch.SerializeToString(),
                timeout=30, allow_redirects=False)
            print(f'{signal}: HTTP {response.status_code}')
            print(response.text)
            response.raise_for_status()
            result = response.json()
            if result.get('status') != 'ok' or result.get('count') != (0 if args.empty else 1):
                print('Unexpected ingestion response; verify the endpoint.')
                failed = True
        except (requests.RequestException, ValueError) as exc:
            print(f'{signal}: verification failed ({type(exc).__name__})')
            failed = True
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
