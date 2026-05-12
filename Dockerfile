FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Install ffmpeg for yt-dlp audio extraction
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Always upgrade yt-dlp to latest — YouTube breaks older versions frequently
RUN pip install --no-cache-dir --upgrade yt-dlp

# Playwright + Chromium are pre-installed in the base image — just run deps
RUN playwright install chromium

# Pre-download Whisper base model into the image
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')"

COPY . .

# Default command — overridden per-service in Railway
CMD ["python3", "watcher.py", "a"]
