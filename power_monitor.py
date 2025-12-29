import asyncio
import json
import os
import signal
import socket
import time
import logging
from logging.handlers import RotatingFileHandler
from enum import Enum
from typing import Optional
from datetime import datetime
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

# ================= CONFIG =================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

ROUTER_1_IP = os.environ.get("ROUTER_IP")
ROUTER_2_PORT = int(os.environ.get("ROUTER_2_PORT"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "10"))
PING_TIMEOUT = int(os.environ.get("PING_TIMEOUT", "2"))
DEBOUNCE_COUNT = int(os.environ.get("DEBOUNCE_COUNT", "6"))
LOG_FILE = os.environ.get("LOG_FILE", "/opt/folder-monitor/power_monitor.log")
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(5 * 1024 * 1024)))  # 5MB
LOG_BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_COUNT", "3"))
TELEGRAM_RETRIES = int(os.environ.get("TELEGRAM_RETRIES", "3"))
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "7"))
STATS_FILE = os.environ.get("STATS_FILE", "/opt/folder-monitor/power_monitor_stats.json")
# ==========================================


def setup_logging():
    """Configure logging with rotation."""
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT
    )
    handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(handler)

    # Also log to console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(console_handler)


def validate_config():
    """Validate configuration at startup."""
    errors = []
    
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN environment variable is not set")
    
    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID environment variable is not set")
    
    if not ROUTER_1_IP:
        errors.append("ROUTER_IP is not configured")
    
    if ROUTER_2_PORT <= 0 or ROUTER_2_PORT > 65535:
        errors.append(f"Invalid ROUTER_2_PORT: {ROUTER_2_PORT}")
    
    if CHECK_INTERVAL <= 0:
        errors.append(f"Invalid CHECK_INTERVAL: {CHECK_INTERVAL}")
    
    if errors:
        for error in errors:
            logging.error(error)
        raise ValueError("Configuration validation failed: " + "; ".join(errors))
    
    logging.info("Configuration validated successfully")


class State(Enum):
    POWER_ON = "POWER_ON"
    POWER_OFF = "POWER_OFF"
    NETWORK_ISSUE = "NETWORK_ISSUE"


STATE_MESSAGES = {
    State.POWER_ON: "🟢 Світло зʼявилось",
    State.POWER_OFF: "🔴 Світло зникло",
    State.NETWORK_ISSUE: "🟡 Проблеми з інтернетом",
}


DURATION_MESSAGES = {
    State.POWER_ON: "⏱ Світло було: {}",
    State.POWER_OFF: "⏱ Світла не було: {}",
    State.NETWORK_ISSUE: "⏱ Інтернета не було: {}",
}


class StatsCollector:
    def __init__(self, start_date: Optional[datetime] = None):
        self.durations = {s.value: 0.0 for s in State}
        self.counts = {s.value: 0 for s in State}
        self.start_date = start_date or datetime.now()

    def update(self, state: State, seconds: float):
        if state:
            self.durations[state.value] += seconds

    def increment(self, state: State):
        if state:
            self.counts[state.value] += 1

    def to_dict(self) -> dict:
        return {
            "durations": self.durations,
            "counts": self.counts,
            "start_date": self.start_date.isoformat()
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StatsCollector":
        collector = cls(start_date=datetime.fromisoformat(data["start_date"]))
        collector.durations = data["durations"]
        collector.counts = data["counts"]
        return collector

    def get_duration(self, state: State) -> float:
        return self.durations[state.value]

    def get_count(self, state: State) -> int:
        return self.counts[state.value]


class StatsManager:
    def __init__(self):
        self.daily = StatsCollector()
        self.weekly = StatsCollector()
        self.last_daily_report_day = datetime.now().date()
        self.last_weekly_report_week = datetime.now().isocalendar()[1]
        self.last_weekly_report_year = datetime.now().isocalendar()[0]
        self._load()

    def _load(self):
        """Load stats from file if exists."""
        try:
            path = Path(STATS_FILE)
            if path.exists():
                with open(path, "r") as f:
                    data = json.load(f)
                self.daily = StatsCollector.from_dict(data["daily"])
                self.weekly = StatsCollector.from_dict(data["weekly"])
                self.last_daily_report_day = datetime.fromisoformat(data["last_daily_report_day"]).date()
                self.last_weekly_report_week = data["last_weekly_report_week"]
                self.last_weekly_report_year = data.get("last_weekly_report_year", datetime.now().isocalendar()[0])
                logging.info(f"Stats loaded from {STATS_FILE}")
        except Exception as e:
            logging.warning(f"Could not load stats: {e}. Starting fresh.")

    def save(self):
        """Persist stats to file."""
        try:
            data = {
                "daily": self.daily.to_dict(),
                "weekly": self.weekly.to_dict(),
                "last_daily_report_day": datetime.combine(self.last_daily_report_day, datetime.min.time()).isoformat(),
                "last_weekly_report_week": self.last_weekly_report_week,
                "last_weekly_report_year": self.last_weekly_report_year
            }
            # Write atomically
            path = Path(STATS_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
            tmp_path.replace(path)
        except Exception as e:
            logging.error(f"Could not save stats: {e}")

    def update(self, state: State, seconds: float):
        self.daily.update(state, seconds)
        self.weekly.update(state, seconds)

    def increment_event(self, state: State):
        self.daily.increment(state)
        self.weekly.increment(state)

    def should_send_daily(self) -> bool:
        now = datetime.now()
        return now.hour >= REPORT_HOUR and now.date() > self.last_daily_report_day

    def should_send_weekly(self) -> bool:
        now = datetime.now()
        current_year, current_week, _ = now.isocalendar()
        # Monday, after REPORT_HOUR, and week/year changed
        if now.hour >= REPORT_HOUR and now.weekday() == 0:
            if current_year != self.last_weekly_report_year or current_week != self.last_weekly_report_week:
                return True
        return False

    def reset_daily(self):
        self.daily = StatsCollector()
        self.last_daily_report_day = datetime.now().date()
        self.save()

    def reset_weekly(self):
        self.weekly = StatsCollector()
        now = datetime.now()
        self.last_weekly_report_week = now.isocalendar()[1]
        self.last_weekly_report_year = now.isocalendar()[0]
        self.save()


def format_duration(seconds: float) -> str:
    """Format duration in human-readable Ukrainian format."""
    total_sec = int(seconds)
    days, rem = divmod(total_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, sec = divmod(rem, 60)
    parts = []
    if days > 0:
        parts.append(f"{days}д")
    if hours > 0:
        parts.append(f"{hours}г")
    if minutes > 0:
        parts.append(f"{minutes}хв")
    parts.append(f"{sec}с")
    return " ".join(parts)


async def ping(ip: str, timeout: int = PING_TIMEOUT) -> bool:
    """Ping an IP address asynchronously."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(timeout), ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await process.communicate()
        return process.returncode == 0
    except Exception as e:
        logging.error(f"Ping error: {e}")
        return False


def check_port(ip: str, port: int, timeout: float = 2.0) -> bool:
    """Check if a TCP port is open on the given IP."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


async def detect_state() -> State:
    """Detect current power state by checking network devices."""
    # 1. Check if 1st router (Internet) is alive
    r1_ok = await ping(ROUTER_1_IP)
    if not r1_ok:
        return State.NETWORK_ISSUE

    # 2. Check if 2nd router (Power) is on via forwarded port
    r2_online = await asyncio.to_thread(check_port, ROUTER_1_IP, ROUTER_2_PORT)
    if not r2_online:
        await asyncio.sleep(3)
        r2_online = await asyncio.to_thread(check_port, ROUTER_1_IP, ROUTER_2_PORT)

    
    return State.POWER_ON if r2_online else State.POWER_OFF


async def send_telegram(
    bot: Bot,
    state: State,
    duration: Optional[float] = None,
    previous_state: Optional[State] = None,
    retries: int = TELEGRAM_RETRIES
) -> bool:
    """Send Telegram notification with retry logic."""
    text = STATE_MESSAGES[state]
    if duration is not None and previous_state is not None:
        duration_text = DURATION_MESSAGES[previous_state].format(format_duration(duration))
        text += f"\n\n{duration_text}"

    for attempt in range(retries):
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
            logging.info(f"Sent: {text}")
            return True
        except TelegramError as e:
            logging.warning(f"Telegram attempt {attempt + 1}/{retries} failed: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
    
    logging.error(f"Failed to send Telegram message after {retries} attempts")
    return False


async def send_message(bot: Bot, text: str, retries: int = TELEGRAM_RETRIES) -> bool:
    """Send a simple text message to Telegram with retry logic."""
    for attempt in range(retries):
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
            logging.info(f"Sent: {text}")
            return True
        except TelegramError as e:
            logging.warning(f"Telegram attempt {attempt + 1}/{retries} failed: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
    
    logging.error(f"Failed to send Telegram message after {retries} attempts")
    return False


def create_report_text(title: str, stats: StatsCollector) -> str:
    """Generate report."""
    return (
        f"📊 *{title}*\n"
        f"📅 {stats.start_date.strftime('%d.%m')} — {datetime.now().strftime('%d.%m')}\n\n"
        f"🟢 *Світло було:* {format_duration(stats.get_duration(State.POWER_ON))}\n"
        f"└ Увімкнень: {stats.get_count(State.POWER_ON)}\n\n"
        f"🔴 *Світла не було:* {format_duration(stats.get_duration(State.POWER_OFF))}\n"
        f"└ Вимкнень: {stats.get_count(State.POWER_OFF)}\n\n"
        f"🟡 *Інтернет не працював:* {format_duration(stats.get_duration(State.NETWORK_ISSUE))}\n"
        f"└ Інцидентів: {stats.get_count(State.NETWORK_ISSUE)}"
    )


async def main():
    """Main monitoring loop."""
    # Setup
    setup_logging()
    validate_config()
    
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    stats_mgr = StatsManager()
    
    # Graceful shutdown handling
    shutdown_event = asyncio.Event()
    
    def signal_handler():
        logging.info("Shutdown signal received")
        shutdown_event.set()
    
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)
    
    # State tracking
    current_state: Optional[State] = None
    last_state_change_time = time.monotonic()  # When state last changed (for duration notifications)
    last_stats_update_time = time.monotonic()  # For accurate stats tracking each iteration
    candidate_state: Optional[State] = None
    candidate_count = 0
    save_counter = 0  # Save stats every N iterations

    # Startup notification
    start_text = "🚀 *Моніторинг запущено*\n\nНаблюдаєм 👀"
    logging.info(start_text)
    await send_message(bot, start_text)

    # Main loop
    while not shutdown_event.is_set():
        try:
            loop_start = time.monotonic()
            
            # Check for reports (daily and weekly)
            if stats_mgr.should_send_daily():
                await send_message(bot, create_report_text("Щоденний звіт", stats_mgr.daily))
                stats_mgr.reset_daily()

            if stats_mgr.should_send_weekly():
                await send_message(bot, create_report_text("📈 Щотижневий підсумок", stats_mgr.weekly))
                stats_mgr.reset_weekly()

            # Detect state
            new_state = await detect_state()
            
            # Calculate actual elapsed time since last stats update
            now = time.monotonic()
            elapsed_seconds = now - last_stats_update_time
            last_stats_update_time = now
            
            # Update stats with actual elapsed time for current confirmed state
            if current_state is not None:
                stats_mgr.update(current_state, elapsed_seconds)
            
            # Log state only if it changed (to avoid flooding logs)
            if new_state != candidate_state:
                logging.debug(f"Candidate state: {new_state}")

            if new_state != current_state:
                if new_state == candidate_state:
                    candidate_count += 1
                else:
                    candidate_state = new_state
                    candidate_count = 1

                if candidate_count >= DEBOUNCE_COUNT:
                    # First state detection (startup) - don't send duration
                    if current_state is None:
                        # Just set the state without notification about duration
                        logging.info(f"Initial state detected: {new_state}")
                    else:
                        # Calculate duration from when state actually changed
                        duration = now - last_state_change_time
                        await send_telegram(bot, new_state, duration, current_state)
                    
                    # Update state change time
                    last_state_change_time = now
                    stats_mgr.increment_event(new_state)
                    current_state = new_state
                    candidate_state = None
                    candidate_count = 0
            else:
                candidate_state = None
                candidate_count = 0

            # Periodically save stats (every 6 iterations = ~1 minute)
            save_counter += 1
            if save_counter >= 6:
                stats_mgr.save()
                save_counter = 0

        except Exception as e:
            logging.error(f"Error in main loop: {e}")

        # Wait for next check or shutdown signal
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=CHECK_INTERVAL)
        except asyncio.TimeoutError:
            pass

    # Graceful shutdown - save stats
    stats_mgr.save()
    shutdown_text = "🛑 *Моніторинг зупинено*"
    logging.info(shutdown_text)
    await send_message(bot, shutdown_text)


if __name__ == "__main__":
    asyncio.run(main())

