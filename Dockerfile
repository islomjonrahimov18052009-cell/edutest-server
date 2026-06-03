FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y libreoffice --no-install-recommends && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .

ENV PORT=5000
EXPOSE 5000

CMD gunicorn --bind 0.0.0.0:$PORT app:app
