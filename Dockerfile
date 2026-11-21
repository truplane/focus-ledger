FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml pricing.yaml README.md ./
COPY src ./src
COPY examples ./examples

RUN pip install --no-cache-dir .

ENV FOCUS_LEDGER_DB=/data/focus_ledger.duckdb
RUN mkdir -p /data

EXPOSE 9110

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:9110/health || exit 1

ENTRYPOINT ["focus-ledger"]
CMD ["serve", "--host", "0.0.0.0", "--port", "9110", "--db", "/data/focus_ledger.duckdb"]
