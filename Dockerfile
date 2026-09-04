# The ngrok binary is lifted from the official image so the dashboard can run
# the agent as a child process -- no Docker socket, no privileged access.
# Development convenience: the views refuse to start it for non-local requests.
FROM ngrok/ngrok:latest AS ngrok

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# libpq is needed by psycopg; curl backs the compose healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ngrok /bin/ngrok /usr/local/bin/ngrok

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x entrypoint.sh \
    && DJANGO_SECRET_KEY=build-only DJANGO_DEBUG=false \
       python manage.py collectstatic --noinput

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--access-logfile", "-"]
