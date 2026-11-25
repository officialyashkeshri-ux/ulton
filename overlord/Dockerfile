# One image, two roles: API/dashboard (default CMD) and worker (`python -m overlord.worker`).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-noto-core espeak-ng ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY overlord ./overlord
COPY fonts ./fonts
COPY models ./models
COPY tests ./tests

ENV OVERLORD_DATA=/data PYTHONUNBUFFERED=1 HF_HOME=/data/hf-cache
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health').status==200 else 1)" || exit 1
CMD ["uvicorn", "overlord.api:app", "--host", "0.0.0.0", "--port", "8080"]
