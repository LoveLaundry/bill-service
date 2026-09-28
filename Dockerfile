# syntax=docker/dockerfile:1
FROM python:3.13-slim

# tzdata + TZ so anything the host defaults to UTC still renders
# Sri Lankan time (see app_time.py, which does not rely on this).
ENV TZ=Asia/Colombo \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/var/www

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /var/www && chown -R app:app /var/www

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY .env.example ./.env.example

RUN printf '%s\n' \
  '#!/bin/sh' \
  'set -eu' \
  ': "${JWT_SECRET:?FATAL: JWT_SECRET is required}"' \
  'case "$JWT_SECRET" in' \
  '  CHANGE-ME-IN-PRODUCTION-love-laundry-2026|change-me-in-production) echo "FATAL: JWT_SECRET is still the default; set a real value" >&2; exit 1;;' \
  'esac' \
  'if [ -n "${MASTER_KEY:-}" ]; then' \
  '  case "$MASTER_KEY" in' \
  '    CHANGE-ME-IN-PRODUCTION-love-laundry-2026|change-me-in-production) echo "FATAL: MASTER_KEY is still the default; set a real value" >&2; exit 1;;' \
  '  esac' \
  'fi' \
  'exec "$@"' \
  > /usr/local/bin/run && chmod +x /usr/local/bin/run

USER app
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/run"]
CMD ["uvicorn", "src.bill_service.main:app", "--host", "0.0.0.0", "--port", "8000"]