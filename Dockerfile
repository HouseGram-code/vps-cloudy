FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# lxd = LXD client (`lxc` CLI), lxc-utils = LXC helpers, iputils-ping = ping for status cmd
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        lxd lxc-utils iputils-ping ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
