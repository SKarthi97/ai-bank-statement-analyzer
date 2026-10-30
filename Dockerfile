# Test and run environment for the Offline Bank Statement Analyzer.
#
#   docker compose --profile test run --rm test      # full test suite, with real Streamlit
#   docker compose up app                            # the app on http://127.0.0.1:8501
#
# Building needs the internet (base image, apt, pip). Running does not. Python 3.11 is the tested version.

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Tesseract is for scanned-PDF OCR; poppler is not needed (pdfplumber/pypdfium2 render pages).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app \
    && mkdir /data && chown app:app /data

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
# Streamlit and pandas are not in requirements.txt (never installed where the pins were made).
# After the first build, record the versions with: docker compose run --rm app pip freeze
RUN pip install -r requirements-dev.txt streamlit pandas

COPY . .
RUN pip install --no-deps -e . && chown -R app:app /app
USER app

ENV BANKAN_DATA_DIR=/data \
    BANKAN_OLLAMA_URL=http://127.0.0.1:11434 \
    BANKAN_TESSERACT_CMD=/usr/bin/tesseract

# ---- test image: the whole suite, including pages under real Streamlit --------------------------
FROM base AS test
CMD ["python", "-m", "pytest", "-q"]

# ---- app image ------------------------------------------------------------------------------------
FROM base AS app
VOLUME ["/data"]
EXPOSE 8501
# 0.0.0.0 is the container's own network; compose publishes the port to the host's 127.0.0.1 only.
CMD ["python", "scripts/run_app.py", "--address", "0.0.0.0", "--allow-non-loopback"]
