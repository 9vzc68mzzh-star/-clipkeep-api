FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home clipkeep && mkdir /tmp/clipkeep && chown clipkeep:clipkeep /tmp/clipkeep
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=clipkeep:clipkeep server.py media.py source_utils.py fast_media.py stream_queue.py .
COPY --chown=clipkeep:clipkeep dist ./dist
USER clipkeep
ENV CLIPKEEP_PUBLIC_DOWNLOADS=true
EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
