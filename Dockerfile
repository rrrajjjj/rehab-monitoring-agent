FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

COPY requirements-deploy.txt ./
RUN pip install --no-cache-dir -r requirements-deploy.txt

COPY . .

EXPOSE 8001

CMD ["uvicorn", "crtv.web.api:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2"]
