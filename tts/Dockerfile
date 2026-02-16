FROM python:3.12-slim

WORKDIR /app

# System dependency for soundfile
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 && rm -rf /var/lib/apt/lists/*

# Install CPU-only PyTorch first to avoid pulling CUDA libs
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu

# Install remaining dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download model at build time so startup is fast
RUN python -c "from qwen_tts import QwenTTS; QwenTTS.from_pretrained('Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice')"

COPY server.py .

EXPOSE 8002

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8002"]
