FROM python:3.9-slim

# 1. Install FFmpeg (Critical for yt-dlp)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean

# 2. Set up App
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

# 3. Run Server
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]