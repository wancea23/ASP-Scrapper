# ASP Exam Checker - imagine pentru Render/Railway (Chromium headless inclus)
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "web.py"]
