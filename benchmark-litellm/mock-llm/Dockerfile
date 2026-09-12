FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn orjson
COPY ./benchmark-litellm/mock-llm/mock_llm /app/mock_llm
COPY ./benchmark-litellm/bench/detgen.py /app/bench/detgen.py
RUN touch /app/bench/__init__.py
ENV PYTHONPATH=/app
EXPOSE 8090
CMD ["uvicorn", "mock_llm.server:app", "--host", "0.0.0.0", "--port", "8090"]
