FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt README.md pyproject.toml ./
COPY src ./src
COPY docs ./docs
COPY AGENTS.md ./

RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && python -m pip install .

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

CMD ["casemail-imap-mcp"]

