FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only what pip needs to resolve dependencies before the rest of the source.
# This keeps the install layer cached as long as pyproject.toml is unchanged.
COPY pyproject.toml .
COPY src/ src/

# Install package with dev extras (pulls in pytest, pytest-asyncio, ruff).
RUN pip install --no-cache-dir ".[dev]"

# Copy remaining files (tests/, alembic.ini, static assets, etc.)
COPY . .

EXPOSE 5005

CMD ["sh", "-c", "python -m src.web_server & python -m src.daemon"]
