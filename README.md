# local-setup

Local Docker Compose stack: LiteLLM v1.83.7 + PgBouncer (transaction mode) + Postgres 16,
Redis (cache), MinIO (S3-compatible spend log export), pgAdmin (DB browsing), with a
one-shot Prisma migration job and a containerized mock LLM upstream.

## Run

```bash
docker compose -f local-setup/docker-compose.yml up -d --build --wait
local-setup/verify.sh
```

Proxy: http://localhost:4000 (master key `sk-1234`, model `fake-gpt-4`).
PgBouncer is also exposed on host port 6432.
MinIO console: http://localhost:9001 (minioadmin/minioadmin); S3 API on host port 9000.
pgAdmin: http://localhost:5050 (login `admin@example.com` / `admin`); a "litellm (via pgbouncer)"
server is pre-registered (host `pgbouncer`, port 5432, db `litellm`, user `llmproxy`) — enter
password `dbpassword9090` on first connect (not stored, per pgAdmin's passfile permission model).

## Tear down

```bash
docker compose -f local-setup/docker-compose.yml down -v
```

## Notes

- Image: `ghcr.io/berriai/litellm-database:v1.83.7-stable` (v1.83.7 is only published with the `-stable` suffix).
- `litellm-migrate` runs `python litellm/proxy/prisma_migration.py` (same as the Helm migrations Job) through PgBouncer.
  `DATABASE_URL` carries `?pgbouncer=true` so Prisma disables prepared statements (required in transaction pool mode).
- `litellm` runs with `DISABLE_SCHEMA_UPDATE=true`; only the migration job touches the schema.
- Proxy config is `proxy_config.yaml` (the repo `.gitignore` ignores `config.yaml` / `litellm_config.yaml`).
- The mock LLM is `tests/ui_e2e_tests/fixtures/mock_llm_server/server.py` wrapped in `mock-llm/Dockerfile`;
  `mock-llm/Dockerfile.dockerignore` overrides the repo-root `.dockerignore` (which excludes `tests/`).
- `litellm` runs without `--detailed_debug` (normal INFO-level logging).
- Redis (`redis:7-alpine`) backs the proxy's response cache and router settings
  (`router_settings.redis_host`/`redis_port`, `litellm_settings.cache_params`).
- Spend logs are written to Postgres (`SpendLogs` table, always-on with a configured `DATABASE_URL`)
  **and** exported to MinIO/S3 via the `s3` logging callback (`litellm_settings.success_callback`,
  `s3_callback_params`). The `minio-init` service uses the `mc` CLI to create the
  `litellm-spend-logs` bucket on startup.
- pgAdmin connects through PgBouncer (not directly to Postgres), matching how `litellm` itself
  connects. Its server list is preloaded from `pgadmin/servers.json`.
