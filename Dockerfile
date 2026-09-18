FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# Lightweight tools only: ping for !status, curl + ca-cert for SSHX setup.
# The `lxc` client is mounted from the host in docker-compose.yml.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        iputils-ping ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
