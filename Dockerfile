FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy backend requirements & install
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Copy application files
COPY backend /app/backend
COPY frontend /app/frontend

# Create data volume directory
RUN mkdir -p /app/data

EXPOSE 8080

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/backend

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
