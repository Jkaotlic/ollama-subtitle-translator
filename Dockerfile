FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py translate_srt.py ./
COPY templates templates/

ENV PYTHONUNBUFFERED=1

EXPOSE 8847

CMD ["python", "app.py"]
