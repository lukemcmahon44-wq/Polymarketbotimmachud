FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]" 2>/dev/null || pip install --no-cache-dir .

COPY . .
RUN pip install --no-cache-dir -e .

# Non-root user for security
RUN useradd -m botuser
USER botuser

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "polymarket_bot.cli.main"]
CMD ["run", "--paper"]
