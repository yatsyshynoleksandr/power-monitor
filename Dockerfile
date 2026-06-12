FROM python:3.13-slim

WORKDIR /app

# Install system dependencies for ping
RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Create data directory
RUN mkdir -p /opt/folder-monitor

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY power_monitor.py .

CMD ["python", "-u", "power_monitor.py"]
