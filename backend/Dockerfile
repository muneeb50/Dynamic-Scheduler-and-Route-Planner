FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt backend/requirements-postgres.txt backend/
RUN pip install --no-cache-dir -r backend/requirements.txt -r backend/requirements-postgres.txt
COPY backend backend
COPY computer_vision computer_vision
COPY simulation simulation
COPY data data
COPY admin_dashboard/dist admin_dashboard/dist
WORKDIR /app/backend
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
