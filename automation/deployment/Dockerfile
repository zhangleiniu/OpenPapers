FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    SCRAPER_DATA_ROOT=/tmp/openpapers-data

WORKDIR /app

COPY automation/deployment/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py utils.py ./
COPY scrapers ./scrapers
COPY automation ./automation

CMD ["python", "-m", "automation.run_monitor_flow"]
