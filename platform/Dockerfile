FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-optimizer.lock ./
RUN pip install --no-cache-dir -r requirements-optimizer.lock
COPY . .
RUN pip install --no-cache-dir --no-deps .
ENTRYPOINT ["startlux"]
