FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY status_page.py .
COPY templates templates
COPY static static
USER 65534
EXPOSE 8080
# One worker: the Splunk response cache is per process. Threads handle concurrent viewers.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--worker-tmp-dir", "/dev/shm", "--access-logfile", "-", "status_page:app"]
