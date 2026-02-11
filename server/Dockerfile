FROM python:3.10-slim

# 1. Install System Dependencies (FFmpeg is required for yt-dlp)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean

# 2. Setup App
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 3. Start Server
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]