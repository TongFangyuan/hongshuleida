FROM node:22-alpine AS web
WORKDIR /build/web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RED_POTATO_RADAR_DATA_DIR=/data/red-potato-radar
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY core ./core
COPY server ./server
COPY --from=web /build/web/dist ./web/dist
VOLUME ["/data/red-potato-radar"]
EXPOSE 8000
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
