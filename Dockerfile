FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 workbench

WORKDIR /app/agent-orchestrator
COPY agent-orchestrator/ /app/agent-orchestrator/
RUN chown -R workbench:workbench /app/agent-orchestrator

USER workbench
EXPOSE 8766
CMD ["python", "-m", "orchestrator.cli", "dashboard", "--config", "kiroween-demo.example.toml", "--host", "0.0.0.0", "--origin", "http://localhost:8766", "--port", "8766", "--lang", "en"]
