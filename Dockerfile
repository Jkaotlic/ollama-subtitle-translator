FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg for video subtitle extraction
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py translate_srt.py video_utils.py ./
COPY templates templates/

ENV PYTHONUNBUFFERED=1

EXPOSE 8847

CMD ["python", "app.py"]
