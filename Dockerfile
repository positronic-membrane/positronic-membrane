FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency configs
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all repository files (relying on .dockerignore to exclude databases, venv, secrets)
COPY . .

EXPOSE 8000

# Entrypoint script to start Web Server and background Swarm Daemon
CMD ["sh", "-c", "python -m src.web_server & python -m src.daemon"]
