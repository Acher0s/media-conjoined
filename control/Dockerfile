FROM python:3.12-slim

# ffmpeg/ffprobe for the delay players, inspect and stitching; rsync + ssh for the archive copy
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg rsync openssh-client \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mediactl ./mediactl

CMD ["python", "-m", "mediactl.server"]
