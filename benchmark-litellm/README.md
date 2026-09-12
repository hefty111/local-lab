# benchmark-litellm

Trace-driven correctness + performance benchmark for the LiteLLM proxy.

## Install

    cd benchmark-litellm
    python3 -m venv .venv && . .venv/bin/activate
    pip install -e .

## Quick start (synthetic trace, 60s, 20 rps)

    bench all --profile constant --rps 20 --duration-s 60 \
      --proxy-base http://localhost:4000 --mock-base http://localhost:8090 \
      --api-key sk-1234 -o report.xlsx

See `docs/superpowers/specs/2026-09-12-litellm-load-benchmark-design.md` for design details.
