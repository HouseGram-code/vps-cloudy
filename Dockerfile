FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# iputils-ping for the !status ping check, ca-certificates for HTTPS (Discord API)
RUN apt-get update && \
    apt-get install -y --no-install-recommends iputils-ping ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
