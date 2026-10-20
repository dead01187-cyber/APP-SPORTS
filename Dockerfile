FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY leadflow ./leadflow
COPY rules.yaml .

EXPOSE 8000
CMD ["uvicorn", "leadflow.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
