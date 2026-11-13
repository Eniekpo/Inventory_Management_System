# ==============================================================================
# Dockerfile - Automate Inventory Monitoring with Python
# Supports running the FastAPI API, APScheduler daemon, or Streamlit Dashboard.
# ==============================================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system dependencies required for psycopg2 and healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy project source
COPY . .

# Create runtime directories and non-root user
RUN mkdir -p /app/logs /app/data/watch/processed /app/data/watch/failed \
    && useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

