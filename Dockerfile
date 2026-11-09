FROM python:3.13-slim

WORKDIR /app

# ping for router 1 checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Default location of LOG_FILE / STATS_FILE (mount a host dir here to persist them)
RUN mkdir -p /opt/folder-monitor

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY power_monitor.py dtek.py ./

ENV TIMEZONE=Europe/Kyiv \
    HEARTBEAT_FILE=/tmp/power_monitor.heartbeat

# The main loop touches HEARTBEAT_FILE every iteration (~10-20s); a stale file means it is stuck.
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(0 if time.time() - os.path.getmtime(os.environ['HEARTBEAT_FILE']) < 180 else 1)"

CMD ["python", "-u", "power_monitor.py"]
