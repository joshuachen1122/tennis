FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_URL=sqlite+aiosqlite:////data/tennis.db PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY tennisbot ./tennisbot
COPY scripts ./scripts
COPY main.py pytest.ini ./
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "main.py"]
