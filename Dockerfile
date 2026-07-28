# NutriSnap Twin Bot — образ для локального теста и деплоя на VPS.
# Python 3.10: паритет с локальными тестами (3.10.10).
FROM python:3.10-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Сначала только зависимости — слой кэшируется, пока requirements.txt не меняется.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код бота (секреты .env в образ не попадают — см. .dockerignore + compose env_file).
COPY . .

CMD ["python", "main.py"]
