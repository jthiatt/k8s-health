FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Standard library only: no pip install, nothing else in the image.
COPY agent.py /app/agent.py
USER 65534
EXPOSE 8080
ENTRYPOINT ["python", "/app/agent.py"]
