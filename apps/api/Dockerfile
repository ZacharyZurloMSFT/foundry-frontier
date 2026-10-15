FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
ENV COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime \
    COPILOT_HARNESS_RUNTIME_DIR=/var/lib/northwind/harness
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -m copilot download-runtime \
    && groupadd --system northwind \
    && useradd --system --gid northwind --home-dir /var/lib/northwind northwind \
    && mkdir -p /var/lib/northwind/harness \
    && chown -R northwind:northwind /var/lib/northwind /opt/copilot-runtime
ENV COPILOT_SKIP_CLI_DOWNLOAD=1

COPY app ./app
USER northwind

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
