FROM python:3.10-slim

# 1. Install System Dependencies
RUN apt-get update && \
    apt-get install -y ffmpeg curl unzip gcc libffi-dev && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 2. Install deno — required by yt-dlp for YouTube JS token evaluation
RUN curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip \
    -o /tmp/deno.zip && \
    unzip /tmp/deno.zip -d /usr/local/bin && \
    rm /tmp/deno.zip && \
    chmod +x /usr/local/bin/deno

# 3. Setup App
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 4. Start Server
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "300", "app:app"]
