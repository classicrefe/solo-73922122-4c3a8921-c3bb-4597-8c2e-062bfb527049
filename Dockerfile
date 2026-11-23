FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CITATION_DB_PATH=/data/citations.db \
    CITATION_SEED_FILE=/app/seed/papers.json

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY seed ./seed
COPY entrypoint.py ./

RUN groupadd --system app \
    && useradd --system --gid app --no-create-home --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown -R app:app /data /app \
    && chmod 755 /app/entrypoint.py

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# No USER directive: the entrypoint fixes /data ownership when started as
# root, then drops to the unprivileged app user before exec'ing uvicorn.
ENTRYPOINT ["python", "/app/entrypoint.py"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
