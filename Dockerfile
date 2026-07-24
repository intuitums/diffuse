FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && addgroup --system diffuse \
    && adduser --system --ingroup diffuse --home /home/diffuse diffuse

COPY --chown=diffuse:diffuse . .
RUN pip install --no-cache-dir --no-deps . \
    && pip check \
    && chmod 700 /app/service/git_askpass.sh \
    && mkdir -p /var/lib/diffuse/repositories \
    && chown -R diffuse:diffuse /var/lib/diffuse

USER diffuse

EXPOSE 8000

STOPSIGNAL SIGTERM

CMD ["uvicorn", "service.webhook_server:app", "--host", "0.0.0.0", "--port", "8000"]
