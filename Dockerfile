FROM python:3.11-slim

# System deps: ffmpeg for audio extraction, curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's Chromium + all its Linux system deps
RUN playwright install --with-deps chromium

# Pre-download Whisper base model so cold starts don't hit HuggingFace
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')"

COPY . .

# Default command — overridden per-service in Railway
CMD ["python3", "watcher.py", "a"]
