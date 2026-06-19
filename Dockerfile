FROM python:3.11-slim

# v4 - LibreOffice + unlimited timeout
RUN apt-get update && apt-get install -y \
    libreoffice-draw \
    --no-install-recommends && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app.py .

CMD gunicorn --bind 0.0.0.0:$PORT --timeout 0 --workers 1 app:app
