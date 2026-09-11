#!/usr/bin/env python3
"""
Generate realistic mock data into the LiteLLM_SpendLogs postgres table.

Rows are modeled directly off a real row pulled from a running litellm proxy
instance (see GOLDEN_METADATA_TEMPLATE below) - only the fields that
legitimately vary per-request are patched, everything else (the large
model-pricing/capability blob, guardrail fields, etc.) is left byte-for-byte
identical to the golden example, since that's what real rows look like.

Traffic volume is distributed non-uniformly across the given time range to
mimic real usage: much higher volume during weekday work hours, and much
lower volume nights/weekends.

Usage:
    python generate_mock_spendlogs.py \
        --start 2026-08-01T00:00:00 \
        --end 2026-09-11T00:00:00 \
        --rows 10000

    # optional:
    --database-url postgresql://user:pass@host:port/dbname
    --batch-size 1000
    --seed 42
"""

import argparse
import copy
import json
import os
import random
import sys
import uuid
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras

DEFAULT_DATABASE_URL = "postgresql://llmproxy:dbpassword9090@localhost:5432/litellm"

# ---------------------------------------------------------------------------
# Golden example data, pulled from a real row in LiteLLM_SpendLogs.
# ---------------------------------------------------------------------------

GOLDEN_MODEL_MAP_VALUE_TEMPLATE = {
    "key": "openai/fake-gpt-4",
    "rpm": None,
    "tpm": None,
    "mode": None,
    "max_tokens": None,
    "tiered_pricing": None,
    "supports_vision": None,
    "litellm_provider": "openai",
    "max_input_tokens": None,
    "max_output_tokens": None,
    "ocr_cost_per_page": None,
    "output_vector_size": None,
    "supports_pdf_input": None,
    "supports_reasoning": None,
    "uses_embed_content": None,
    "ocr_cost_per_credit": None,
    "supports_image_size": None,
    "supports_web_search": None,
    "input_cost_per_image": None,
    "input_cost_per_query": None,
    "input_cost_per_token": 0,
    "supports_audio_input": None,
    "supports_tool_choice": None,
    "supports_tool_search": None,
    "supports_url_context": None,
    "input_cost_per_second": None,
    "output_cost_per_image": None,
    "output_cost_per_token": 0,
    "supports_audio_output": None,
    "supports_computer_use": None,
    "output_cost_per_second": None,
    "citation_cost_per_token": None,
    "prompt_cache_min_tokens": None,
    "provider_specific_entry": None,
    "supported_openai_params": [
        "frequency_penalty", "logit_bias", "logprobs", "top_logprobs",
        "max_tokens", "max_completion_tokens", "modalities", "prediction",
        "n", "presence_penalty", "seed", "stop", "stream", "stream_options",
        "temperature", "top_p", "tools", "tool_choice", "function_call",
        "functions", "max_retries", "extra_headers", "parallel_tool_calls",
        "audio", "web_search_options", "service_tier", "safety_identifier",
        "prompt_cache_key", "prompt_cache_retention", "store", "response_format",
    ],
    "supports_prompt_caching": None,
    "web_search_billing_unit": None,
    "annotation_cost_per_page": None,
    "input_cost_per_character": None,
    "supports_response_schema": None,
    "supports_system_messages": None,
    "input_cost_per_token_flex": None,
    "output_cost_per_character": None,
    "supports_function_calling": None,
    "supports_native_streaming": None,
    "input_cost_per_audio_token": None,
    "input_cost_per_image_token": None,
    "input_cost_per_video_token": None,
    "output_cost_per_token_flex": None,
    "supports_adaptive_thinking": None,
    "supports_assistant_prefill": None,
    "cache_read_input_token_cost": None,
    "output_cost_per_audio_token": None,
    "output_cost_per_image_token": None,
    "output_cost_per_video_token": None,
    "input_cost_per_token_batches": None,
    "output_cost_per_second_1080p": None,
    "input_cost_per_token_priority": None,
    "output_cost_per_token_batches": None,
    "search_context_cost_per_query": None,
    "supports_low_reasoning_effort": None,
    "supports_max_reasoning_effort": None,
    "output_cost_per_token_priority": None,
    "supports_embedding_image_input": None,
    "supports_none_reasoning_effort": None,
    "cache_creation_input_token_cost": None,
    "input_cost_per_audio_per_second": None,
    "input_cost_per_video_per_second": None,
    "output_cost_per_reasoning_token": None,
    "supports_xhigh_reasoning_effort": None,
    "cache_read_input_token_cost_flex": None,
    "output_cost_per_video_per_second": None,
    "supports_mid_conversation_system": None,
    "supports_minimal_reasoning_effort": None,
    "supports_native_structured_output": None,
    "bedrock_output_config_effort_ceiling": None,
    "cache_creation_input_token_cost_flex": None,
    "cache_read_input_token_cost_priority": None,
    "output_cost_per_reasoning_token_flex": None,
    "bedrock_converse_supports_strict_tools": None,
    "input_cost_per_token_above_128k_tokens": None,
    "input_cost_per_token_above_200k_tokens": None,
    "input_cost_per_token_above_272k_tokens": None,
    "input_cost_per_token_above_512k_tokens": None,
    "output_cost_per_token_above_128k_tokens": None,
    "output_cost_per_token_above_200k_tokens": None,
    "output_cost_per_token_above_272k_tokens": None,
    "output_cost_per_token_above_512k_tokens": None,
    "cache_creation_input_token_cost_priority": None,
    "output_cost_per_reasoning_token_priority": None,
    "regional_processing_uplift_multiplier_eu": None,
    "regional_processing_uplift_multiplier_us": None,
    "cache_creation_input_token_cost_above_1hr": None,
    "input_cost_per_token_above_272k_tokens_flex": None,
    "output_cost_per_character_above_128k_tokens": None,
    "output_cost_per_token_above_272k_tokens_flex": None,
    "cache_read_input_token_cost_above_200k_tokens": None,
    "cache_read_input_token_cost_above_272k_tokens": None,
    "cache_read_input_token_cost_above_512k_tokens": None,
    "input_cost_per_token_above_200k_tokens_priority": None,
    "input_cost_per_token_above_272k_tokens_priority": None,
    "output_cost_per_token_above_200k_tokens_priority": None,
    "output_cost_per_token_above_272k_tokens_priority": None,
    "cache_creation_input_token_cost_above_200k_tokens": None,
    "cache_creation_input_token_cost_above_272k_tokens": None,
    "cache_read_input_token_cost_above_272k_tokens_flex": None,
    "cache_creation_input_token_cost_above_272k_tokens_flex": None,
    "cache_read_input_token_cost_above_200k_tokens_priority": None,
    "cache_read_input_token_cost_above_272k_tokens_priority": None,
    "cache_creation_input_token_cost_above_272k_tokens_priority": None,
}

GOLDEN_METADATA_TEMPLATE = {
    "status": None,
    "max_retries": 2,
    "batch_models": None,
    "usage_object": {
        "total_tokens": 14,
        "prompt_tokens": 8,
        "completion_tokens": 6,
        "prompt_tokens_details": None,
        "completion_tokens_details": {
            "text_tokens": None,
            "audio_tokens": None,
            "image_tokens": None,
            "video_tokens": None,
            "reasoning_tokens": 0,
            "accepted_prediction_tokens": None,
            "rejected_prediction_tokens": None,
        },
    },
    "user_api_key": "44c5342e7e4accb24ac3cc3fa053ea540047cc396002256d085d95051607c21b",
    "cost_breakdown": {
        "input_cost": 0.0,
        "total_cost": 0.0,
        "output_cost": 0.0,
        "service_tier": None,
        "original_cost": 0.0,
        "data_residency": None,
        "margin_percent": 0.0,
        "discount_amount": 0.0,
        "tool_usage_cost": 0.0,
        "discount_percent": 0.0,
        "margin_fixed_amount": 0.0,
        "margin_total_amount": 0.0,
    },
    "litellm_call_id": "b393ddd0-89ef-431b-b3ef-ad28eb61f1a5",
    "eval_information": None,
    "routing_decision": None,
    "attempted_retries": 0,
    "error_information": None,
    "applied_guardrails": [],
    "user_api_key_alias": None,
    "compression_savings": None,
    "spend_logs_metadata": None,
    "user_api_key_org_id": None,
    "internal_call_origin": None,
    "proxy_server_request": None,
    "requester_ip_address": "172.20.0.1",
    "user_api_key_team_id": "litellm-dashboard",
    "user_api_key_user_id": "default_user_id",
    "guardrail_information": None,
    "model_map_information": {
        "model_map_key": "openai/fake-gpt-4",
        "model_map_value": GOLDEN_MODEL_MAP_VALUE_TEMPLATE,
    },
    "mcp_tool_call_metadata": None,
    "additional_usage_values": {
        "prompt_tokens_details": None,
        "completion_tokens_details": {
            "text_tokens": None,
            "audio_tokens": None,
            "image_tokens": None,
            "video_tokens": None,
            "reasoning_tokens": 0,
            "accepted_prediction_tokens": None,
            "rejected_prediction_tokens": None,
        },
    },
    "cold_storage_object_key": None,
    "user_api_key_project_id": None,
    "user_api_key_team_alias": None,
    "litellm_overhead_time_ms": None,
    "user_api_key_project_alias": None,
    "vector_store_request_metadata": None,
}

GOLDEN_REQUEST_TAGS = ["User-Agent: s_", "User-Agent: s_/JS 4.104.0"]

# ---------------------------------------------------------------------------
# Realistic pools of models / keys / users / teams.
# ---------------------------------------------------------------------------

# (model_name, custom_llm_provider, input_cost_per_token, output_cost_per_token, weight)
MODEL_POOL = [
    ("gpt-4o", "openai", 0.0000025, 0.00001, 20),
    ("gpt-4o-mini", "openai", 0.00000015, 0.0000006, 30),
    ("gpt-4-turbo", "openai", 0.00001, 0.00003, 5),
    ("claude-3-5-sonnet-20241022", "anthropic", 0.000003, 0.000015, 20),
    ("claude-3-5-haiku-20241022", "anthropic", 0.0000008, 0.000004, 10),
    ("gemini-1.5-pro", "gemini", 0.00000125, 0.000005, 6),
    ("gemini-1.5-flash", "gemini", 0.000000075, 0.0000003, 8),
    ("mistral-large-latest", "mistral", 0.000002, 0.000006, 3),
    ("command-r-plus", "cohere", 0.0000025, 0.00001, 2),
    ("meta.llama3-1-70b-instruct-v1:0", "bedrock", 0.00000265, 0.0000035, 2),
]

# (user_id, team_id, team_alias, weight)
USER_TEAM_POOL = [
    ("user_alice", "team-growth", "growth", 15),
    ("user_bob", "team-growth", "growth", 10),
    ("user_carol", "team-ml-research", "ml-research", 12),
    ("user_dave", "team-ml-research", "ml-research", 8),
    ("user_erin", "team-platform", "platform", 10),
    ("user_frank", "team-platform", "platform", 8),
    ("user_grace", "team-data-science", "data-science", 12),
    ("user_heidi", "team-data-science", "data-science", 7),
]


def _fake_hashed_key(seed_str: str) -> str:
    """Deterministic 64-hex-char fake token, sha256-like, per user."""
    import hashlib

    return hashlib.sha256(seed_str.encode()).hexdigest()


# one stable fake api_key (hashed token) per user
API_KEY_POOL = {user_id: _fake_hashed_key(user_id) for user_id, _, _, _ in USER_TEAM_POOL}

CALL_TYPES = [
    ("acompletion", 90),
    ("aembedding", 7),
    ("aimage_generation", 3),
]


# ---------------------------------------------------------------------------
# Work-hours traffic distribution.
# ---------------------------------------------------------------------------

def hour_weight(dt: datetime) -> float:
    """Relative traffic weight for the hour bucket starting at `dt`."""
    weekday = dt.weekday()  # 0=Mon .. 6=Sun
    hour = dt.hour

    is_weekend = weekday >= 5

    if 9 <= hour < 18:
        base = 1.0  # peak work hours
    elif 7 <= hour < 9 or 18 <= hour < 21:
        base = 0.4  # ramp up / wind down
    else:
        base = 0.07  # night

    if is_weekend:
        base *= 0.15

    return base


def build_hour_buckets(start: datetime, end: datetime):
    """Return list of (bucket_start_datetime, weight) for each hour in [start, end)."""
    buckets = []
    cur = start.replace(minute=0, second=0, microsecond=0)
    if cur < start:
        pass
    while cur < end:
        buckets.append((cur, hour_weight(cur)))
        cur += timedelta(hours=1)
    if not buckets:
        # range shorter than an hour - single bucket
        buckets.append((start, 1.0))
    return buckets


def sample_timestamps(start: datetime, end: datetime, n: int, rng: random.Random):
    buckets = build_hour_buckets(start, end)
    bucket_starts = [b[0] for b in buckets]
    weights = [b[1] for b in buckets]

    chosen_buckets = rng.choices(bucket_starts, weights=weights, k=n)

    timestamps = []
    for bstart in chosen_buckets:
        bend = min(bstart + timedelta(hours=1), end)
        bend = max(bend, bstart + timedelta(seconds=1))
        span_seconds = max(int((bend - bstart).total_seconds()), 1)
        offset = rng.uniform(0, span_seconds)
        ts = bstart + timedelta(seconds=offset)
        ts = max(start, min(ts, end))
        timestamps.append(ts)

    timestamps.sort()
    return timestamps


# ---------------------------------------------------------------------------
# Row generation.
# ---------------------------------------------------------------------------

SPEND_LOGS_COLUMNS = [
    "request_id", "call_type", "api_key", "spend", "total_tokens",
    "prompt_tokens", "completion_tokens", "startTime", "endTime",
    "request_duration_ms", "completionStartTime", "model", "model_id",
    "model_group", "custom_llm_provider", "api_base", "user", "metadata",
    "cache_hit", "cache_key", "request_tags", "team_id", "organization_id",
    "end_user", "requester_ip_address", "messages", "response", "session_id",
    "status", "mcp_namespaced_tool_name", "agent_id", "proxy_server_request",
]


def weighted_choice(rng: random.Random, pool, weight_index):
    weights = [item[weight_index] for item in pool]
    return rng.choices(pool, weights=weights, k=1)[0]


def build_metadata(rng, api_key, user_id, team_id, model_name, provider,
                    prompt_tokens, completion_tokens, total_tokens,
                    spend, input_cost, output_cost, ip_address, call_id):
    md = copy.deepcopy(GOLDEN_METADATA_TEMPLATE)

    md["usage_object"]["total_tokens"] = total_tokens
    md["usage_object"]["prompt_tokens"] = prompt_tokens
    md["usage_object"]["completion_tokens"] = completion_tokens
    md["additional_usage_values"]["prompt_tokens_details"] = None

    md["user_api_key"] = api_key
    md["user_api_key_user_id"] = user_id
    md["user_api_key_team_id"] = team_id
    md["requester_ip_address"] = ip_address
    md["litellm_call_id"] = call_id

    input_total = round(prompt_tokens * input_cost, 8)
    output_total = round(completion_tokens * output_cost, 8)
    md["cost_breakdown"]["input_cost"] = input_total
    md["cost_breakdown"]["output_cost"] = output_total
    md["cost_breakdown"]["total_cost"] = round(input_total + output_total, 8)
    md["cost_breakdown"]["original_cost"] = md["cost_breakdown"]["total_cost"]

    full_model_key = f"{provider}/{model_name}"
    md["model_map_information"]["model_map_key"] = full_model_key
    md["model_map_information"]["model_map_value"]["key"] = full_model_key
    md["model_map_information"]["model_map_value"]["litellm_provider"] = provider
    md["model_map_information"]["model_map_value"]["input_cost_per_token"] = input_cost
    md["model_map_information"]["model_map_value"]["output_cost_per_token"] = output_cost

    return md


def build_request_tags(rng):
    # small variations on the golden example
    js_version = f"{rng.randint(3, 4)}.{rng.randint(0, 120)}.{rng.randint(0, 9)}"
    return ["User-Agent: s_", f"User-Agent: s_/JS {js_version}"]


def generate_row(rng, ts_start: datetime):
    user_id, team_id, team_alias, _ = weighted_choice(rng, USER_TEAM_POOL, 3)
    model_name, provider, input_cost, output_cost, _ = weighted_choice(rng, MODEL_POOL, 4)
    call_type, _ = weighted_choice(rng, CALL_TYPES, 1)
    api_key = API_KEY_POOL[user_id]

    is_failure = rng.random() < 0.03  # ~3% failures
    is_cache_hit = (not is_failure) and rng.random() < 0.05  # ~5% cache hits

    if call_type == "aembedding":
        prompt_tokens = rng.randint(5, 500)
        completion_tokens = 0
    elif call_type == "aimage_generation":
        prompt_tokens = rng.randint(5, 50)
        completion_tokens = 0
    else:
        prompt_tokens = rng.randint(20, 2000)
        completion_tokens = 0 if is_failure else rng.randint(5, 800)

    total_tokens = prompt_tokens + completion_tokens

    if is_failure:
        spend = 0.0
        duration_ms = rng.randint(50, 1500)
        status = "failure"
    else:
        input_total = prompt_tokens * input_cost
        output_total = completion_tokens * output_cost
        spend = round(input_total + output_total, 8)
        duration_ms = rng.randint(200, 8000)
        status = "success"

    end_ts = ts_start + timedelta(milliseconds=duration_ms)
    completion_start_ts = ts_start + timedelta(
        milliseconds=min(duration_ms, rng.randint(20, max(20, duration_ms)))
    )

    request_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    call_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    ip_address = f"172.20.{rng.randint(0, 255)}.{rng.randint(1, 254)}"

    model_id = uuid.uuid4().hex + uuid.uuid4().hex[:32]

    metadata = build_metadata(
        rng, api_key, user_id, team_id, model_name, provider,
        prompt_tokens, completion_tokens, total_tokens,
        spend, input_cost, output_cost, ip_address, call_id,
    )

    cache_key = uuid.uuid4().hex + uuid.uuid4().hex if is_cache_hit else ""

    row = {
        "request_id": request_id,
        "call_type": call_type,
        "api_key": api_key,
        "spend": spend,
        "total_tokens": total_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "startTime": ts_start,
        "endTime": end_ts,
        "request_duration_ms": duration_ms,
        "completionStartTime": completion_start_ts,
        "model": model_name,
        "model_id": model_id,
        "model_group": model_name,
        "custom_llm_provider": provider,
        "api_base": f"https://api.{provider}.com/v1",
        "user": user_id,
        "metadata": metadata,
        "cache_hit": "True" if is_cache_hit else "False",
        "cache_key": cache_key,
        "request_tags": build_request_tags(rng),
        "team_id": team_id,
        "organization_id": None,
        "end_user": "",
        "requester_ip_address": ip_address,
        "messages": {},
        "response": {},
        "session_id": session_id,
        "status": status,
        "mcp_namespaced_tool_name": None,
        "agent_id": None,
        "proxy_server_request": {},
    }
    return row


def row_to_tuple(row):
    values = []
    for col in SPEND_LOGS_COLUMNS:
        val = row[col]
        if col in ("metadata", "request_tags", "messages", "response", "proxy_server_request"):
            val = psycopg2.extras.Json(val) if val is not None else None
        values.append(val)
    return tuple(values)


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="ISO8601 start datetime, e.g. 2026-08-01T00:00:00")
    parser.add_argument("--end", required=True, help="ISO8601 end datetime, e.g. 2026-09-11T00:00:00")
    parser.add_argument("--rows", type=int, required=True, help="Number of mock rows to generate")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="Postgres connection string (defaults to $DATABASE_URL or local-setup default)",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=None)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    if end <= start:
        print("ERROR: --end must be after --start", file=sys.stderr)
        return 1
    if args.rows <= 0:
        print("ERROR: --rows must be positive", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)

    print(f"Generating {args.rows} mock rows between {start} and {end}...")
    timestamps = sample_timestamps(start, end, args.rows, rng)

    print(f"Connecting to database...")
    conn = psycopg2.connect(args.database_url)
    conn.autocommit = False

    quoted_columns = ", ".join('"{}"'.format(c) for c in SPEND_LOGS_COLUMNS)
    insert_sql = f'INSERT INTO "LiteLLM_SpendLogs" ({quoted_columns}) VALUES %s'

    try:
        with conn.cursor() as cur:
            batch = []
            inserted = 0
            for i, ts in enumerate(timestamps):
                row = generate_row(rng, ts)
                batch.append(row_to_tuple(row))

                if len(batch) >= args.batch_size or i == len(timestamps) - 1:
                    psycopg2.extras.execute_values(cur, insert_sql, batch)
                    conn.commit()
                    inserted += len(batch)
                    print(f"Inserted {inserted}/{args.rows}...")
                    batch = []
        print("Done.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
