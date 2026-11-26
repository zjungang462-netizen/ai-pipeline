FROM python:3.11-slim

WORKDIR /app/agent-orchestrator
COPY agent-orchestrator/ /app/agent-orchestrator/

# Default runs the zero-key showcase config; bind 0.0.0.0 only inside explicit container port mapping
EXPOSE 8766
CMD ["python", "-m", "orchestrator.cli", "doctor", "kiroween-demo.example.toml"]
