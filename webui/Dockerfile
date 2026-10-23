FROM python:3.11-slim

# apt cache mount — packages stay on host between builds, never re-downloaded.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends sox tcpdump

WORKDIR /app

# pip cache mount — wheels cached on host; only re-installs when requirements.txt changes.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# App code last — changes here don't bust the pip layer.
COPY . .
EXPOSE 8087
CMD ["python", "app.py"]
