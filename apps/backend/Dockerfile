FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# System deps: build essentials for any wheels that fall back to source
# (tree-sitter language packages occasionally need a C compiler). Cleaned
# up in the same layer to keep the image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so docker can cache this layer across rebuilds
# whenever requirements.txt is unchanged.
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Copy app source. We deliberately do NOT include tests/ here — the image
# is meant for runtime. Tests are run via the dev compose target or CI.
COPY app ./app
COPY scripts ./scripts

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
