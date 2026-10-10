FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

# 1 (default): Playwright + Chromium for auto-apply, hh.ru status sync and the browser fetcher (~+600 MB image).
# 0: smaller image without a browser — auto-apply and fetcher = "browser" will not work.
ARG WITH_BROWSER=1

COPY pyproject.toml README.md ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN if [ "$WITH_BROWSER" = "1" ]; then \
        pip install ".[browser]" && playwright install --with-deps chromium; \
    else \
        pip install .; \
    fi

RUN useradd --create-home appuser && mkdir -p /app/data && chown appuser /app/data
USER appuser

EXPOSE 8000
CMD ["worksaas", "run", "--host", "0.0.0.0", "--port", "8000"]
