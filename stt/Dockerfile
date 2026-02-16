FROM python:3.12-slim

WORKDIR /app

# System dependency for audio processing
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 ffmpeg && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download model at build time so startup is fast
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('openai/whisper-large-v3-turbo', device='cpu', compute_type='int8')"

COPY server.py .

EXPOSE 8003

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8003"]
