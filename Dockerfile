# Use the official Python base image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the dependency files
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn

# Copy the entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Copy the application code
COPY . .

# Create the necessary directories
RUN mkdir -p logs app/database

# Expose the port
EXPOSE 8087

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV ENV=production
ENV PORT=8087
ENV HOST=0.0.0.0

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "app.main:app", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8087", "--workers", "4", "--timeout", "300", "--keep-alive", "30", "--max-requests", "10000", "--max-requests-jitter", "1000", "--access-logfile", "-", "--log-level", "warning"]