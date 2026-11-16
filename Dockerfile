FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py .

RUN useradd --create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /srv/app
USER appuser

ENV DB_PATH=/data/app.db
VOLUME /data
EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "run:app"]
