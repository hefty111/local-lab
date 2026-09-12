# run once
.venv/bin/python query_spend.py --database-url "postgresql://llmproxy:dbpassword9090@localhost:6432/litellm"
# run 20 times concurrently (one wave)
.venv/bin/python query_spend.py --concurrency 20 --database-url "postgresql://llmproxy:dbpassword9090@localhost:6432/litellm"
# run 100 total queries, 20 at a time
.venv/bin/python query_spend.py --concurrency 20 --total 100 --database-url "postgresql://llmproxy:dbpassword9090@localhost:6432/litellm"
