# Observagent Ollama telemetry proxy

This repository contains the public Ollama telemetry proxy for Observagent. It lets Ollama clients keep using the local API while exporting metadata-only request logs and traces to an Observagent project. The setup examples use Windows PowerShell on the computer running Ollama.

## Repository contents

| File | Purpose |
| --- | --- |
| `ollama_proxy.py` | Local FastAPI proxy that forwards Ollama requests and exports OTLP logs and traces. |
| `requirements.txt` | Python runtime dependencies. |
| `verify_delivery.py` | Sends synthetic telemetry to verify endpoint access and project-key authentication. |
| `test_ollama_proxy.py` | Unit tests for exporter configuration, forwarding, streaming, and usage metadata. |
| `README.md` | Installation, operation, privacy, and troubleshooting guide. |

Clone or download this repository, open PowerShell in its root directory, and follow the steps below. Do not commit `.env` files or project keys.

## License and trademarks

Licensed under the Apache License 2.0. See `LICENSE` and `NOTICE`.

Observagent and the Observagent logo are trademarks of Observagent. The Apache License 2.0 does not grant permission to use Observagent trademarks except as required to describe the origin of this software.

## How it works

The CLI connects to the proxy at 127.0.0.1:11435. The proxy forwards requests to Ollama at 127.0.0.1:11434 and exports telemetry to your backend over OTLP/HTTP protobuf. Keep two PowerShell windows open: Window 1 runs the proxy; Window 2 runs the Ollama CLI.

Each chat or generate request produces a trace and a structured log with agent and service name `ollama-cli`, a session ID, model, available token counts, timing, and status. Prompts and generated text are not exported. Model-loading requests may omit token counts. There is no historical backfill and no separate metric export.

## 1. Prepare Python and Ollama

Run these checks on your Windows computer:

```powershell
python --version
ollama list
```

If Python is not found, try `py --version`. If neither command works, install Python, then close and reopen PowerShell:

```powershell
winget install --exact --id Python.Python.3.13 --source winget
```

If winget is unavailable, use the Windows installer from [python.org](https://www.python.org/downloads/windows/). Enable Add Python to PATH if offered. After reopening PowerShell, verify `python --version`; use `py` for the initial environment-creation command if only that launcher works.

Open the Ollama application and check that the server is listening:

```powershell
Invoke-RestMethod "http://127.0.0.1:11434/api/version"
```

Expect a version number. If the connection fails, start Ollama before continuing. If using `ollama serve` manually, keep it in a separate terminal without the proxy's OLLAMA_HOST setting.

## 2. Create the Python environment

Open PowerShell in the repository root, then create the virtual environment. If you cloned the repository elsewhere, replace the example path with your clone location.

```powershell
Set-Location "$HOME\observagent-ollama-telemetry-proxy"
python -m venv .venv
```

Run each installation command separately. The last package name is one complete name; do not insert a newline inside it. Calling the environment's Python directly means activation is optional.

```powershell
.\.venv\Scripts\python.exe -m pip install fastapi uvicorn httpx
.\.venv\Scripts\python.exe -m pip install opentelemetry-sdk
.\.venv\Scripts\python.exe -m pip install opentelemetry-exporter-otlp-proto-http
```

Alternatively, install the same dependencies from the repository file:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

You may activate with `.\.venv\Scripts\Activate.ps1` and use `python`, but all remaining commands work without activation. If activation is blocked by execution policy, use the direct commands below; no policy change is needed.

## 3. Create or select an Observagent project

Sign in to your Observagent dashboard. Open Settings / Projects, create an Ollama project or select an existing one, enable both Logs and Traces, and turn monitoring on. Choose Connect source, then Generate / replace project key. Copy the full key when shown; it needs `telemetry:write` permission.

A separate Ollama project can make its telemetry easier to identify and filter.

If you replace a project key, update every sender that used the previous key.

If project-key creation is unavailable, contact your Observagent administrator or support channel. Do not use your dashboard password or a model-provider key.

## 4. Configure Window 1 and start the proxy

Run these commands in the same PowerShell window that will start the proxy. Enter your real endpoint at the prompt, such as `https://dashboard.observagent.cloud/v1/traces` for that deployment. Use the full /v1/traces URL, not the marketing site or documentation URL.

```powershell
Set-Location "$HOME\observagent-ollama-telemetry-proxy"
$env:OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = Read-Host "Full backend trace URL ending in /v1/traces"
$env:OBSERVAGENT_API_KEY = (Read-Host "Paste the complete raw Observagent project API key").Trim()
$env:OBSERVAGENT_SESSION_ID = "ollama-cli-test"
```

Supply the complete project key to `OBSERVAGENT_API_KEY`; a shortened key prefix cannot authenticate requests. Do not include `Authorization=Bearer`, `%20`, quotes, or a leading `#`. In PowerShell, `#` comments out the assignment. The proxy derives the logs endpoint by replacing /v1/traces with /v1/logs. You can override it with `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` if your deployment uses a different address. Both exporters use the same project API key.

Check that the settings exist without printing the key:

```powershell
Write-Host "Endpoint:" $env:OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
Write-Host "API key configured:" (-not [string]::IsNullOrWhiteSpace($env:OBSERVAGENT_API_KEY))
$env:OBSERVAGENT_API_KEY -match '^obs_(live|test)_[^_]+_[A-Za-z0-9_-]+$'
```

Run the offline tests, then start the proxy:

```powershell
.\.venv\Scripts\python.exe test_ollama_proxy.py
.\.venv\Scripts\python.exe -m uvicorn ollama_proxy:app --host 127.0.0.1 --port 11435
```

Expect both Boolean checks to return `True`, then Application startup complete and Uvicorn running on http://127.0.0.1:11435. Leave this window open. Settings are local to this PowerShell process and disappear when you close it. Variables set in Window 2, a different terminal, or an unloaded `.env` file do not configure the proxy. Restarting within the same window keeps them. The session ID groups activity until you change it and restart; it is not automatically a unique ID for each CLI conversation. Start Uvicorn directly as shown; do not use a launcher that hard-codes or overwrites the API key.

## 5. Connect Window 2 and generate activity

Open a second PowerShell window on the same computer:

```powershell
$env:OLLAMA_HOST = "http://127.0.0.1:11435"
ollama list
```

Choose an installed model name from the list:

```powershell
$model = Read-Host "Installed model name from ollama list"
ollama run $model
```

Send a prompt, for example: Explain telemetry in two sentences. Let the answer finish. OLLAMA_HOST applies to the CLI in this window; keep the real Ollama server on 11434. Requests from other windows that bypass the proxy are not captured.

Other Ollama HTTP clients must also target the proxy. For `login-audit`, either leave `OLLAMA_HOST` set in that same window or pass the proxy explicitly:

```powershell
login-audit https://authorized.example/login `
  --authorized `
  --allow-host authorized.example `
  --ollama-url http://127.0.0.1:11435
```

If the client produces a model response but Window 1 shows no `POST /api/chat`, the client bypassed the proxy and probably connected directly to port 11434.

## 6. Verify logs and traces in Observagent

Switch to Window 1 with Alt+Tab or select its terminal tab. After a completed response, expect `ollama.request.completed: input=... output=...; trace and log queued for export`. A preliminary model-load call can show input=None and output=None. Queued means buffered for export, not confirmed backend storage.

In the dashboard, sign in with the account associated with the key. Open Telemetry Data, choose All sources and All available, select Logs or All, clear old filters, and refresh. Search for `agent:ollama-cli` or `event:ollama.request.completed`. Inspect a log's original attributes to see the model, input_token_count, output_token_count, and durations.

Check the selected project's recent activity for both logs and traces. If a dashboard view does not update, confirm that recent Ollama events appear in Telemetry Data and review the active filters.

Log events are `ollama.request.completed`, `ollama.request.failed`, and `ollama.request.incomplete`. Errors before connection produce a failed request log. There are no logs for health checks or model-list requests. Earlier trace-only versions of the proxy do not send logs; replace the script and restart to use this version.

## 7. Diagnose delivery directly

In Window 1, press Ctrl+C to stop the proxy while keeping its environment settings. Run the supplied diagnostic file to send one synthetic trace and one synthetic log with session `ollama-delivery-test`:

```powershell
.\.venv\Scripts\python.exe verify_delivery.py
```

This creates test records. Expect HTTP 200 and count: 1 for each signal. That confirms authenticated delivery for the configured key independently of Ollama. A successful test does not prove that automatic proxy exports are working; restart the proxy and generate another real request to check that too.

To test only routing and authorization without storing records, use the empty-batch mode:

```powershell
.\.venv\Scripts\python.exe verify_delivery.py --empty
```

Expect count: 0. HTTP 200 with HTML is not an ingestion confirmation. HTTP 401 means the complete raw key was not accepted; generate a fresh project key and copy it exactly. The prefix alone can never authenticate. HTTP 403 means the key or project is not permitted to send that signal. A 404 usually indicates a wrong path. A timeout or TLS error needs a connectivity, domain, or certificate check. Keep certificate verification enabled.

If the test stores records but they are missing from project history, check the key's project, signed-in account, filters, and whether the website and ingestion URL refer to the same deployment. Do not regenerate keys repeatedly to diagnose a display issue.

## 8. Change keys, restart, or stop collection

To route new data to a different project, stop the proxy in Window 1 with Ctrl+C, then update the key and restart:

```powershell
$env:OBSERVAGENT_API_KEY = Read-Host "Paste the new project API key"
.\.venv\Scripts\python.exe -m uvicorn ollama_proxy:app --host 127.0.0.1 --port 11435
```

To start in a new PowerShell window, repeat the endpoint, key, and session settings from step 4. Do not paste keys into chats, screenshots, committed files, or public documentation.

To bypass the proxy, exit the interactive Ollama session and run this in Window 2 before starting another CLI session:

```powershell
Remove-Item Env:OLLAMA_HOST -ErrorAction SilentlyContinue
```

Press Ctrl+C in Window 1 to stop the proxy and flush queued exports during normal shutdown. Stopping collection does not delete already stored records.

## Troubleshooting the local setup

- Python was not found: install Python, reopen PowerShell, and verify python or py before creating the environment. Activation cannot work until environment creation succeeds.
- Invalid requirement ending in otlp-: the package name was split. Run the complete opentelemetry-exporter-otlp-proto-http installation line from step 2.
- Set OTEL_EXPORTER_OTLP_TRACES_ENDPOINT and OBSERVAGENT_API_KEY first: one or both variables are missing in the window starting the proxy. Run step 4 again there. A blank endpoint in another terminal tells you nothing about the proxy process.
- Boolean environment checks return false: remove any leading `#` from the assignment and run it in the same PowerShell window that starts the relevant process. A `.env` file is not automatically loaded by this proxy.
- Address already in use on 11435: another proxy instance may still be running. Return to its window and stop it before restarting.
- Cannot reach Ollama on 11434: start the Ollama application. Do not start the Ollama server using the CLI window's proxy address.
- Proxy receives no request: configure the client with `$env:OLLAMA_HOST = "http://127.0.0.1:11435"` or its explicit Ollama base-URL option. A local report proves inference completed, not that the proxy saw it.
- Export returns 401: run `verify_delivery.py` first. Confirm `OBSERVAGENT_API_KEY` contains the complete raw key, not the prefix or an `Authorization=Bearer%20...` header. Revoke any key pasted into source code, logs, screenshots, shell history, or support messages.
- Trace captured but no logs: pull the latest repository version, restart the proxy, enable Logs for the project, and send a new request.
- Telemetry arrives but a dashboard total stays unchanged: confirm that the selected view and filters include recent Ollama events.
- PowerShell remains at the >> prompt after a pasted Python block: press Ctrl+C. A here-string closing marker must start at column one and Python imports must not be split incorrectly. Use verify_delivery.py instead of pasting a multiline Python here-string.

## Escalate an unresolved connection problem

If a newly generated key still fails `verify_delivery.py`, contact your Observagent administrator or support channel. Share only the request timestamp, endpoint hostname, HTTP status, project name, and non-secret key prefix. Never share the complete raw key. Server configuration, account validation, and storage diagnostics belong in the private operator runbook and are intentionally excluded from this public guide.

## Files and reference

- ollama_proxy.py: local forwarding service and batched log/trace exporters.
- requirements.txt: runtime dependencies.
- test_ollama_proxy.py: offline regression checks.
- verify_delivery.py: direct synthetic or empty-batch delivery checks.
- README.md: this setup and troubleshooting guide.

Ollama reports usage in the final streaming response; see [Ollama usage documentation](https://docs.ollama.com/api/usage). See the [Observagent Ollama integration guide](https://dashboard.observagent.cloud/documentation/ollama-integration) for the public walkthrough.
