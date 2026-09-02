FROM python:3.12-slim

# ติดตั้ง ffmpeg (เล่นเสียง) + libopus (เข้ารหัสเสียงสำหรับ Discord voice)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libopus0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
