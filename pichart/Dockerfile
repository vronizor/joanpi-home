FROM python:3.12-slim

WORKDIR /app
COPY pichart ./pichart
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=60s --timeout=5s \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PICHART_PORT', '8040')}/api/now\", timeout=4)"]

CMD ["python", "-m", "pichart"]
