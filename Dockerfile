# The copilot itself can also run on Liara (platform: docker, port 8000)
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY copilot ./copilot
COPY victim-app ./victim-app
ENV PORT=8000
EXPOSE 8000
CMD ["python", "-m", "copilot.server"]
