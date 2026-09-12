# LiteLLM Load Benchmark — Design

Date: 2026-09-12
Location: `benchmark-litellm/`

## 1. Goal

An automated benchmark that verifies **correctness** and measures **performance** of the LiteLLM proxy by replaying realistic traffic against a deterministic mock LLM server. One command produces an Excel report comparing:

- **baseline** — client -> mock LLM directly
- **proxy/chat** — client -> LiteLLM `/v1/chat/completions` -> mock
- **proxy/messages** — client -> LiteLLM `/v1/messages` -> mock

The headline metric is `overhead_ms = observed_latency - requested_duration`, i.e. what LiteLLM adds on top of the backend.

## 2. Prior art (why we build)

Evaluated (Sep 2026): llm-d-inference-sim, BerriAI example_openai_endpoint, mockai, AIPerf, GuideLLM, inference-perf, vLLM `bench serve`, AIBrix, llmperf (archived), genai-perf (deprecated), k6 + xk6-sse, locust, vegeta.

No tool provides all of: per-request latency control on the mock, response-content verification, and exact-timestamp trace replay for both OpenAI chat and Anthropic messages streaming from one client. Using two load generators would confound the baseline-vs-proxy comparison.

Reused libraries: `httpx`, `psycopg`, `xlsxwriter`, `typer`, `numpy`, `orjson`. Built: mock server extensions, replayer, report.

## 3. Layout

```
benchmark-litellm/
  README.md
  pyproject.toml
  bench/                    # CLI entry `bench`
    cli.py                  # trace | run | report | all
    trace/
      schema.py             # TraceRequest + JSONL I/O
      from_spendlogs.py     # Postgres -> trace.jsonl
      synthetic.py          # constant | ramp | burst | poisson
    client/
      scheduler.py          # open-loop firing at t_ms * time_scale
      openai_chat.py        # /v1/chat/completions build + SSE parse
      anthropic_messages.py # /v1/messages build + SSE parse
      verify.py             # expected content derivation + comparison
      results.py            # per-request record -> results.jsonl
    report/
      stats.py              # percentiles, deltas
      excel.py              # XlsxWriter workbook
  mock-llm/                 # moved from stack-litellm/mock-llm
    server.py
    detgen.py               # deterministic generator shared by mock and client
    Dockerfile
  tests/
```

`stack-litellm/docker-compose.yml` is updated to build the mock from `benchmark-litellm/mock-llm/` (fixing the stale `local-setup/` paths) and to expose the mock on a host port so the baseline run can reach it.

**Data flow:** `trace.jsonl` -> `bench run` (x3 targets) -> `results/<label>.jsonl` + `.meta.json` -> `bench report` -> `report.xlsx`. Baseline and proxy runs consume the identical trace file.

**Scale hook (not built now):** `run --workers N --worker-index i` partitions rows by `i % N`. Mock replicas via compose `--scale` behind multiple `model_list` entries.

## 4. Trace

One JSONL line per request:

```json
{"i": 0, "t_ms": 0, "seed": "a1b2c3", "api": "chat", "stream": true,
 "model": "fake-gpt-4", "in_tokens": 412, "out_tokens": 87,
 "duration_ms": 2310, "ttft_ms": 380}
```

- `t_ms`: offset from trace start (first row 0).
- `seed`: unique hex per row; embedded in the prompt so the proxy's Redis response cache never hits.
- `api`: `chat` | `messages`; `stream`: bool. `run --api` may override for a whole run.
- `ttft_ms`: optional; mock defaults to 15% of `duration_ms`.

### 4.1 `bench trace spendlogs`

Args: `--dsn`, `--start`, `--end`, `--limit`, `--sample <fraction>`, `--model-map old=new,...`, `--from-csv <file>`.

Large-table workaround: a server-side named cursor (`itersize=5000`) over
`SELECT "startTime","endTime","completionStartTime",prompt_tokens,completion_tokens,call_type,model,request_id FROM "LiteLLM_SpendLogs" WHERE "startTime" BETWEEN %s AND %s ORDER BY "startTime"` — bounded by the existing `startTime` index, streamed, never materialised. Sampling is deterministic: keep row iff `blake2b(request_id) % 10000 < sample*10000`. `--from-csv` accepts a `\copy` export for offline use.

Mapping: `t_ms = startTime - first.startTime`; `duration_ms = endTime - startTime`; `ttft_ms = completionStartTime - startTime` if present; `stream = completionStartTime is not null`; `api = messages` if `call_type` in {`anthropic_messages`, `aanthropic_messages`} else `chat`; `model` via `--model-map`, default `fake-gpt-4`. Rows with zero tokens or non-positive duration are dropped and counted on stderr.

### 4.2 `bench trace synthetic`

`--profile constant|ramp|burst|poisson --rps --duration-s --in-tokens LO:HI --out-tokens LO:HI --latency-ms LO:HI [--latency-lognormal] --stream-ratio --messages-ratio --rng-seed`. Ranges are uniform unless flagged.

### 4.3 Run-time knobs

`--time-scale <f>` multiplies `t_ms`. `--rate-mode replay` (default) honours `t_ms`; `--rate-mode rps:<n>` ignores `t_ms` and fires at a Poisson rate, using the trace only for sizes/durations. `--max-inflight` is a safety cap (requests beyond it are delayed and flagged via `sched_lag_ms`).

## 5. Mock server

### 5.1 Request control

Top-level body key (sent via OpenAI SDK `extra_body`, forwarded by LiteLLM to OpenAI-compatible providers):

```json
"mock": {"seed": "a1b2c3", "out_tokens": 87, "duration_ms": 2310,
         "ttft_ms": 380, "jitter": 0.2}
```

Fallback: the client also writes the same params as the first line of the user message: `[mock seed=a1b2c3 out=87 dur=2310 ttft=380 jitter=0.2]`. The mock reads the body key first, then the message marker. The marker doubles as the cache-buster and pads the prompt with `in_tokens` deterministic words for realistic byte size.

**Spike first:** confirm LiteLLM forwards `mock` through both `/v1/chat/completions` and `/v1/messages`. If not, the marker path is used.

**Spike finding (Task 6, run against the live `litellm-local` stack):** on `/v1/chat/completions`, the `mock` top-level body key **is** forwarded end-to-end through LiteLLM to the mock backend — the response content exactly matched `bench.detgen.text(seed, out_tokens)` for the given seed, confirming pass-through rather than the default canned response. `/v1/messages` initially failed with a 404 (`litellm.NotFoundError: ... Received Model Group=fake-gpt-4`) regardless of the `mock` key. The root cause is not a missing model registration (the model is registered fine) but that LiteLLM's `/v1/messages` handler defaults to routing `openai/`-provider models (like `fake-gpt-4`) to the OpenAI Responses API (`/v1/responses`), which the mock server does not implement, so the backend request itself 404s and LiteLLM surfaces it as `NotFoundError`. This was fixed by adding `use_chat_completions_url_for_anthropic_messages: true` under `litellm_settings` in `stack-litellm/proxy_config.yaml`, which forces `/v1/messages` requests for `openai/`-provider models through the chat/completions translation path instead, landing on the mock server's existing `/v1/chat/completions` endpoint. Conclusion: `mock` is confirmed forwarded on chat; after the config fix, `/v1/messages` is functional and can be exercised, and per Step 3 the client still sends the in-prompt marker on both endpoints regardless, so no conditional logic is required.

### 5.2 Timing model

Streaming: sleep `ttft_ms`, emit first token; spread `out_tokens - 1` chunks over `duration_ms - ttft_ms` with multiplicative gap jitter `U(1-j, 1+j)`, gaps renormalised so the sum is exact; the last gap absorbs rounding. Non-streaming: sleep `duration_ms`, respond once. Target: `end - start == duration_ms` within one scheduler tick.

### 5.3 Deterministic content (`detgen.py`)

Word list of ~2000 words. `token_i = words[blake2b(seed + i) % len]`, one word per chunk, space-joined. `text(seed, n)` is the only public function; client and mock both import it. Usage: `prompt_tokens = in_tokens` from the request, `completion_tokens = out_tokens`, `total = sum`.

### 5.4 Endpoints

`POST /v1/chat/completions` and `/chat/completions`; `GET /v1/models`, `/models`, `/health`; embeddings retained from the original mock; `GET /admin/stats` -> `{inflight, total, errors}`. Uvicorn workers configurable via `UVICORN_WORKERS`.

### 5.5 Verification (client side)

A request is `ok` iff: HTTP 200; assembled content == `detgen.text(seed, out_tokens)` (stream: joined deltas / `content_block_delta.text`; non-stream: `choices[0].message.content` / `content[0].text`); stream terminated with `[DONE]` / `message_stop`. Chunk count is recorded but informational (proxy may coalesce). Otherwise `ok=false` with `reason`.

**Token-count check is soft, not exact-equality.** Streaming requests always set
`stream_options: {"include_usage": true}` so the backend/proxy reports final
usage. But `usage.completion_tokens` is *not* required to equal `out_tokens`
exactly: the mock's "tokens" are space-separated dictionary words from the
deterministic generator, not real subword tokens. Talking to the mock
directly, `completion_tokens == out_tokens` holds exactly (the mock reports
what it was told to generate). Talking through a real LiteLLM proxy, LiteLLM
recomputes `completion_tokens` by re-tokenizing the actual response content
with its own tokenizer (e.g. tiktoken) rather than passing through the
mock's count — for a 15-word mock response this might report 46 completion
tokens. That is expected tokenizer disagreement, not a bug, and is not
grounds for `ok=false`: content equality is the actual correctness
ground-truth for this benchmark (byte-exact match against
`detgen.text(seed, out_tokens)`), and it's unaffected by which tokenizer
counted the result.

The token-count field is instead used only as an instrumentation sanity
check, failing with `reason=token_count_missing` if `usage` was not reported
at all (despite requesting it) when `out_tokens > 0`, or
`reason=token_count_mismatch` if the reported `completion_tokens` is `<= 0`
while output was expected — both indicate broken telemetry rather than a
different but valid token count. A plausible-but-different count (proxy
tokenizer vs. mock word count) is not flagged; only a missing or structurally
nonsensical count is.


## 6. Client (`bench run`)

Single asyncio loop; one `httpx.AsyncClient` with keep-alive pool = `--max-inflight`. Scheduler sleeps until `t0 + t_ms * time_scale` then spawns a task.

Per request: `i, seed, api, stream, target, t_sched, t_sent, t_first_byte, t_first_token, t_done, status, ok, reason, chunks, bytes, out_tokens_seen, x-litellm-* headers`. Derived: `latency_ms`, `ttft_ms`, `sched_lag_ms = t_sent - t_sched`, `overhead_ms = latency_ms - duration_ms`.

Outputs `results/<label>.jsonl` and `results/<label>.meta.json` (CLI args, git sha, target URL, LiteLLM version from `/health/readiness` when target is the proxy, start/end, CPU count, `aborted` flag).

Errors: per-request `--timeout` default `duration_ms * 3 + 10s` -> `reason=timeout`; connection errors -> `reason=connect`; never abort the run. Ctrl-C flushes partial results with `aborted=true`.

## 7. Report (`bench report base.jsonl [others...] -o report.xlsx`)

Sheets:
1. **Summary** — per run: requests, ok%, error breakdown, achieved RPS, p50/p90/p99/max of latency, TTFT, overhead; tokens/s; delta-vs-baseline columns with red/green conditional formatting; verdict row: PASS iff ok == 100% and `sched_lag_ms` p99 <= 5 ms (else the client, not the target, was saturated).
2. **By API x Stream** — same stats split by `api`, `stream`.
3. **Timeline** — 1 s buckets: RPS, in-flight, p99 latency; line charts.
4. **Failures** — every `ok=false` row: reason, seed, expected vs got (truncated 200 chars).
5. **Raw-<label>** — one sheet per run, capped at 1,000,000 rows.

## 8. `bench all`

`trace` (or `--trace existing.jsonl`) -> preflight -> run baseline -> run proxy chat -> run proxy messages -> report.

Preflight: `GET mock/health`, `GET proxy/health/liveliness`, one verified canary request per target, mock `/admin/stats.inflight == 0` before each run. Any failure aborts before load starts.

## 9. Testing

`pytest` in `benchmark-litellm/tests/`:
- `detgen` determinism and cross-process stability.
- SSE parsers against recorded OpenAI and Anthropic fixtures (incl. coalesced chunks).
- Scheduler timing with an injected clock.
- `verify.py` positive and each negative case.
- Mock timing accuracy: in-process uvicorn, assert duration within 10%.
- Spend-logs mapper on a small fixture (marked `integration`, requires running stack).
- Report builder produces the five sheets from a fixture results set.

## 10. Out of scope

Native Anthropic endpoint on the mock; multi-process workers (hook only); HTML report; mock replicas; separate no-cache proxy config (unique prompts instead); Prometheus scraping.

Deferred during implementation (not built in v1, no design change intended — re-scope for a fast-follow):
- `.meta.json` sidecar (args, git sha, target URL, LiteLLM version, aborted flag) per results file.
- `bench all` preflight (health checks, canary request, `admin/stats.inflight == 0` gate before each run).
- `--rate-mode rps:<n>` CLI flag (only `replay` timing is wired into the CLI today).
- Graceful Ctrl-C handling with an `aborted` marker.
- Timeline sheet's in-flight column and charts (only RPS/p99 columns are written today).
- `--api` CLI flag validation (typos silently route to the Anthropic builder).
