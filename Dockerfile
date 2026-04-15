FROM python:3.11-slim

# Metadata
LABEL maintainer="polybot"
LABEL description="Polymarket autonomous trading bot — paper mode"

# Create non-root user for security
RUN groupadd -r polybot && useradd -r -g polybot polybot

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY polymarket_bot/ ./polymarket_bot/
COPY config/ ./config/
COPY pyproject.toml .

# Create log directory
RUN mkdir -p logs && chown polybot:polybot logs

# Switch to non-root user
USER polybot

# Default: paper mode only
ENV ENABLE_LIVE_TRADING=false
ENV CONFIG_PATH=config/default.yaml
ENV LOG_LEVEL=INFO

ENTRYPOINT ["python", "-m", "polymarket_bot.cli.control"]
CMD ["run", "--paper"]
