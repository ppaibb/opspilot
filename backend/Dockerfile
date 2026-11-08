FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app
RUN groupadd --system opspilot \
    && useradd --system --gid opspilot --home-dir /app opspilot \
    && mkdir -p /app/storage /app/logs \
    && chown -R opspilot:opspilot /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=opspilot:opspilot app ./app
COPY --chown=opspilot:opspilot alembic ./alembic
COPY --chown=opspilot:opspilot alembic.ini pytest.ini ./

USER opspilot
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/settings/login-page', timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
