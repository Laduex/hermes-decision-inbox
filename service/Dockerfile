FROM node:24-alpine@sha256:50c8e8ca1d27439048670df5883f32d57cf81cff6233222c893fd0d9884cbd81 AS web
WORKDIR /build/web
COPY web/package.json web/package-lock.json* ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DECISION_INBOX_WEB_DIST=/app/web/dist
COPY pyproject.toml README.md plugin.yaml ./
COPY src/ ./src/
COPY service/ ./service/
COPY skills/ ./skills/
COPY --from=web /build/web/dist ./web/dist
RUN pip install --no-cache-dir .
RUN useradd --system --uid 10001 --home /nonexistent decision-inbox && mkdir -p /state && chown decision-inbox:decision-inbox /state
USER decision-inbox
EXPOSE 8080
CMD ["uvicorn", "service.app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
