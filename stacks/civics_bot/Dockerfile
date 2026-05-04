FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY civics_bot.py /app/civics_bot.py
COPY questions.csv /app/questions.csv

CMD ["python", "/app/civics_bot.py"]
