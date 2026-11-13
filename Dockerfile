FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-ai.txt ./
RUN python -m pip install -r requirements.txt -r requirements-ai.txt
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /app/storage /app/cache && chown -R app:app /app/storage /app/cache
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app vendor/team-10_AI/services ./vendor/team-10_AI/services
COPY --chown=app:app vendor/team-10_AI/seed_quiz.json ./vendor/team-10_AI/seed_quiz.json
USER app
RUN python -c "from app.ai.engine import MODULES, module; [module(name) for name in MODULES]"
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "100.78.84.58"]
