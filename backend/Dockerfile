# Minimal image for the GreenCharge backend.
# NOT used by docker-compose.yml: the app runs on the host via uvicorn (see BUILD_SPEC Phase 0).
FROM python:3.11-slim

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
