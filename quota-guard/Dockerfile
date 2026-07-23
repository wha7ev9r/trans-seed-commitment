ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-alpine AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:${PYTHON_VERSION}-alpine

LABEL org.opencontainers.image.source="trans-commitment"
LABEL description="quota-guard: Transmission monthly quota monitor & unified control panel"

RUN addgroup -g 1000 -S guard && adduser -u 1000 -S guard -G guard

COPY --from=builder /install /usr/local

WORKDIR /app
COPY guard.py .

RUN mkdir -p /data && chown -R guard:guard /data /app

USER guard

EXPOSE 9092
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9092/healthz', timeout=5)" || exit 1

ENTRYPOINT ["python", "guard.py"]
