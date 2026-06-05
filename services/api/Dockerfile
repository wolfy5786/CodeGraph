FROM python:3.12-slim

WORKDIR /srv

COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/api/app/ ./app/
COPY services/ingestion-worker/src/ ./ingestion-worker/src/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
