FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY measure.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "measure.py"]
