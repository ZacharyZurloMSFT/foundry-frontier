# Build from repository root: docker build -f agents/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY agents/hosted/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY agents/__init__.py /app/agents/__init__.py
COPY agents/hosted/*.py /app/agents/hosted/
COPY agents/*.yaml /app/agents/
COPY agents/orchestrator/instructions.yaml /app/agents/orchestrator/instructions.yaml
COPY agents/insurance/*.py /app/agents/insurance/
COPY agents/insurance/*.yaml /app/agents/insurance/
COPY apps/api/app/insurance_domain.py /app/agents/insurance/domain.py
COPY data/northwind/insurance-fixtures.json /app/data/northwind/insurance-fixtures.json
RUN useradd --create-home --uid 10001 agent && chown agent:agent /app
USER agent
EXPOSE 8088
CMD ["python", "-m", "agents.hosted.main"]
