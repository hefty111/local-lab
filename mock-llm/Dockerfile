FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn
# Build context is the repo root (see docker-compose.yml)
COPY ./local-setup/mock-llm/mock_llm_server.py /app/server.py
EXPOSE 8090
# server.py's __main__ binds 127.0.0.1; run via uvicorn so it listens on all interfaces
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]
