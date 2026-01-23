"""
Power Monitor - electricity availability monitoring with Telegram notifications.

The script checks availability of two routers:
- Router 1: connected to internet, runs on battery (checked via ping)
- Router 2: runs on mains power, accessible via forwarded port

State detection logic:
- Router 1 OK + Router 2 OK = Power is ON
- Router 1 OK + Router 2 FAIL = Power is OFF
- Router 1 FAIL = Internet issues (power state unknown)

Features:
- Telegram notifications on state changes
- Debounce to avoid false triggers
- Daily and weekly reports with statistics
- Persistent statistics between restarts
- Graceful shutdown (SIGTERM/SIGINT)
"""

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

# ================= CONFIG =================
def get_required_env(name: str) -> str:
    """Get required environment variable or raise error."""
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Required environment variable {name} is not set")
    return value

TELEGRAM_BOT_TOKEN = get_required_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = get_required_env("TELEGRAM_CHAT_ID")
ROUTER_1_IP = get_required_env("ROUTER_IP")
ROUTER_2_PORT = int(get_required_env("ROUTER_2_PORT"))

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "10"))
PING_TIMEOUT = int(os.environ.get("PING_TIMEOUT", "2"))
DEBOUNCE_COUNT = int(os.environ.get("DEBOUNCE_COUNT", "6"))
LOG_FILE = os.environ.get("LOG_FILE", "/opt/folder-monitor/power_monitor.log")
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(5 * 1024 * 1024)))  # 5MB
LOG_BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_COUNT", "3"))
TELEGRAM_RETRIES = int(os.environ.get("TELEGRAM_RETRIES", "3"))
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "7"))
STATS_FILE = os.environ.get("STATS_FILE", "/opt/folder-monitor/power_monitor_stats.json")
STATS_SAVE_INTERVAL = int(os.environ.get("STATS_SAVE_INTERVAL", "6"))  # Save every N iterations (~1 min)
# ==========================================

class PowerState(Enum):
    ON = "POWER_ON"
    OFF = "POWER_OFF"

class InternetState(Enum):
    ONLINE = "NET_ONLINE"
    OFFLINE = "NET_OFFLINE"

STATE_MESSAGES = {
    PowerState.ON: "🟢 Світло зʼявилось",
    PowerState.OFF: "🔴 Світло зникло",
    InternetState.ONLINE: "🌐 Інтернет відновився",
    InternetState.OFFLINE: "☁️ Інтернет пропав (світло {})",
}

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
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(console)

class StatsCollector:
    """Collects duration statistics and state change counts."""
    
    def __init__(self, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None):
        self.durations = {
            "POWER_ON": 0.0, "POWER_OFF": 0.0,
            "NET_ONLINE": 0.0, "NET_OFFLINE": 0.0
        }
        self.counts = {
            "POWER_ON": 0, "POWER_OFF": 0,
            "NET_ONLINE": 0, "NET_OFFLINE": 0
        }
        self.start_date = start_date or datetime.now()
        self.end_date = end_date  # Set when stats period ends (for reports)

    def update_power(self, p_state: PowerState, seconds: float):
        """Update power state duration."""
        self.durations[p_state.value] += seconds

    def update_internet(self, i_state: InternetState, seconds: float):
        """Update internet state duration."""
        self.durations[i_state.value] += seconds

    def increment(self, state_val: str):
        self.counts[state_val] += 1

    def to_dict(self) -> dict:
        return {
            "durations": self.durations,
            "counts": self.counts,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat() if self.end_date else None
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StatsCollector":
        end_date = datetime.fromisoformat(data["end_date"]) if data.get("end_date") else None
        coll = cls(start_date=datetime.fromisoformat(data["start_date"]), end_date=end_date)
        coll.durations = data["durations"]
        coll.counts = data["counts"]
        return coll

class StatsManager:
    """Manages daily and weekly statistics, persists to/loads from file."""
    
    def __init__(self):
        self.daily = StatsCollector()
        self.weekly = StatsCollector()
        # Track when stats were last reset (at midnight)
        self.last_daily_reset_day = datetime.now().date()
        self.last_weekly_reset_week = datetime.now().isocalendar()[1]
        self.last_weekly_reset_year = datetime.now().isocalendar()[0]
        # Track when reports were last sent (at REPORT_HOUR)
        self.last_daily_report_day = datetime.now().date()
        self.last_weekly_report_week = datetime.now().isocalendar()[1]
        self.last_weekly_report_year = datetime.now().isocalendar()[0]
        # Pending reports (stats saved at midnight, sent at REPORT_HOUR)
        self.pending_daily_report: Optional[StatsCollector] = None
        self.pending_weekly_report: Optional[StatsCollector] = None
        # Current states and last change times (persisted for accurate duration after restart)
        self.current_power_state: Optional[str] = None
        self.current_internet_state: Optional[str] = None
        self.last_power_change: Optional[datetime] = None
        self.last_internet_change: Optional[datetime] = None
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
                self.last_daily_reset_day = datetime.fromisoformat(data.get("last_daily_reset_day", data["last_daily_report_day"])).date()
                self.last_weekly_reset_week = data.get("last_weekly_reset_week", data["last_weekly_report_week"])
                self.last_weekly_reset_year = data.get("last_weekly_reset_year", data.get("last_weekly_report_year", datetime.now().isocalendar()[0]))
                self.last_daily_report_day = datetime.fromisoformat(data["last_daily_report_day"]).date()
                self.last_weekly_report_week = data["last_weekly_report_week"]
                self.last_weekly_report_year = data.get("last_weekly_report_year", datetime.now().isocalendar()[0])
                # Load pending reports if exist
                if "pending_daily_report" in data and data["pending_daily_report"]:
                    self.pending_daily_report = StatsCollector.from_dict(data["pending_daily_report"])
                if "pending_weekly_report" in data and data["pending_weekly_report"]:
                    self.pending_weekly_report = StatsCollector.from_dict(data["pending_weekly_report"])
                # Load current states and last change times
                self.current_power_state = data.get("current_power_state")
                self.current_internet_state = data.get("current_internet_state")
                if data.get("last_power_change"):
                    self.last_power_change = datetime.fromisoformat(data["last_power_change"])
                if data.get("last_internet_change"):
                    self.last_internet_change = datetime.fromisoformat(data["last_internet_change"])
        except Exception as e:
            logging.warning(f"Stats load failed: {e}")

    def save(self):
        """Persist stats to file."""
        try:
            data = {
                "daily": self.daily.to_dict(),
                "weekly": self.weekly.to_dict(),
                "last_daily_reset_day": datetime.combine(self.last_daily_reset_day, datetime.min.time()).isoformat(),
                "last_weekly_reset_week": self.last_weekly_reset_week,
                "last_weekly_reset_year": self.last_weekly_reset_year,
                "last_daily_report_day": datetime.combine(self.last_daily_report_day, datetime.min.time()).isoformat(),
                "last_weekly_report_week": self.last_weekly_report_week,
                "last_weekly_report_year": self.last_weekly_report_year,
                "pending_daily_report": self.pending_daily_report.to_dict() if self.pending_daily_report else None,
                "pending_weekly_report": self.pending_weekly_report.to_dict() if self.pending_weekly_report else None,
                "current_power_state": self.current_power_state,
                "current_internet_state": self.current_internet_state,
                "last_power_change": self.last_power_change.isoformat() if self.last_power_change else None,
                "last_internet_change": self.last_internet_change.isoformat() if self.last_internet_change else None
            }
            # Write atomically
            path = Path(STATS_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w") as f: json.dump(data, f, indent=2)
            tmp.replace(path)
        except Exception as e: logging.error(f"Stats save failed: {e}")

    def update_power(self, p_state: PowerState, seconds: float):
        """Update power state duration in daily and weekly stats."""
        self.daily.update_power(p_state, seconds)
        self.weekly.update_power(p_state, seconds)

    def update_internet(self, i_state: InternetState, seconds: float):
        """Update internet state duration in daily and weekly stats."""
        self.daily.update_internet(i_state, seconds)
        self.weekly.update_internet(i_state, seconds)

    def increment(self, state_val: str):
        self.daily.increment(state_val)
        self.weekly.increment(state_val)

    def prepare_daily_report(self):
        """Save current daily stats for report and reset (called at midnight)."""
        self.daily.end_date = datetime.now()  # Mark end of stats period
        self.pending_daily_report = self.daily
        self.daily = StatsCollector()
        self.last_daily_reset_day = datetime.now().date()

    def prepare_weekly_report(self):
        """Save current weekly stats for report and reset (called at Monday midnight)."""
        self.weekly.end_date = datetime.now()  # Mark end of stats period
        self.pending_weekly_report = self.weekly
        self.weekly = StatsCollector()
        self.last_weekly_reset_week = datetime.now().isocalendar()[1]
        self.last_weekly_reset_year = datetime.now().isocalendar()[0]

    def mark_daily_report_sent(self):
        """Mark daily report as sent and clear pending."""
        self.pending_daily_report = None
        self.last_daily_report_day = datetime.now().date()

    def mark_weekly_report_sent(self):
        """Mark weekly report as sent and clear pending."""
        self.pending_weekly_report = None
        self.last_weekly_report_week = datetime.now().isocalendar()[1]
        self.last_weekly_report_year = datetime.now().isocalendar()[0]

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

async def check_connectivity() -> (InternetState, PowerState):
    """Determine internet and power states independently."""
    net_ok = await ping_async(ROUTER_1_IP)
    i_state = InternetState.ONLINE if net_ok else InternetState.OFFLINE
    
    # If there's no internet, we can't check the port.
    # In this case, return None for power to maintain the previous state.
    p_state = None
    if net_ok:
        p_online = await asyncio.to_thread(check_port, ROUTER_1_IP, ROUTER_2_PORT)
        if not p_online:
            await asyncio.sleep(2)  # Double check
            p_online = await asyncio.to_thread(check_port, ROUTER_1_IP, ROUTER_2_PORT)
        p_state = PowerState.ON if p_online else PowerState.OFF
        
    return i_state, p_state

async def ping_async(ip: str) -> bool:
    """Async ping to IP address. Returns True if host is reachable."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(PING_TIMEOUT), ip,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        return proc.returncode == 0
    except: return False

def check_port(ip: str, port: int) -> bool:
    """Check if TCP port is open on the given IP address."""
    try:
        with socket.create_connection((ip, port), timeout=2.0): return True
    except: return False

async def send_msg(bot: Bot, text: str) -> bool:
    """Send message to Telegram with retry and exponential backoff."""
    for i in range(TELEGRAM_RETRIES):
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
            logging.info(f"Telegram message sent: {text[:50]}...")
            return True
        except Exception as e:
            logging.warning(f"Telegram send failed (attempt {i+1}/{TELEGRAM_RETRIES}): {e}")
            await asyncio.sleep(2**i)
    logging.error(f"Failed to send Telegram message after {TELEGRAM_RETRIES} attempts")
    return False

def normalize_durations(dur_a: float, dur_b: float, expected_total: float) -> tuple[float, float]:
    """Normalize two durations to sum exactly to expected_total (e.g., 24h or 7d)."""
    actual_total = dur_a + dur_b
    if actual_total == 0:
        return 0.0, 0.0
    ratio = expected_total / actual_total
    return dur_a * ratio, dur_b * ratio

def create_report(title: str, stats: StatsCollector) -> str:
    """Generate statistics report text for Telegram."""
    # Calculate expected duration based on actual elapsed time
    # Use end_date if available (for pending reports), otherwise current time
    end_time = stats.end_date or datetime.now()
    elapsed = (end_time - stats.start_date).total_seconds()
    
    # Normalize durations to match elapsed time exactly
    p_on, p_off = normalize_durations(
        stats.durations["POWER_ON"], 
        stats.durations["POWER_OFF"], 
        elapsed
    )
    i_on, i_off = normalize_durations(
        stats.durations["NET_ONLINE"], 
        stats.durations["NET_OFFLINE"], 
        elapsed
    )
    
    # Calculate percentages (will sum to exactly 100%)
    p_total = p_on + p_off
    pct_on = (p_on / p_total * 100) if p_total > 0 else 0
    pct_off = (p_off / p_total * 100) if p_total > 0 else 0
    
    i_total = i_on + i_off
    pct_i_on = (i_on / i_total * 100) if i_total > 0 else 0
    pct_i_off = (i_off / i_total * 100) if i_total > 0 else 0
    
    # Ensure percentages sum to exactly 100% (adjust larger value for rounding)
    pct_off = 100.0 - pct_on
    pct_i_off = 100.0 - pct_i_on
    
    return (
        f"📊 *{title}*\n"
        f"📅 {stats.start_date.strftime('%d.%m')} — {end_time.strftime('%d.%m')}\n\n"
        f"💡 *Світло було:* {format_duration(p_on)} ({pct_on:.1f}%)\n"
        f"🔌 *Світла не було:* {format_duration(p_off)} ({pct_off:.1f}%)\n"
        f"📈 Вимкнень світла: {stats.counts['POWER_OFF']}\n\n"
        f"🌐 *Інтернет був:* {format_duration(i_on)} ({pct_i_on:.1f}%)\n"
        f"☁️ *Інтернету не було:* {format_duration(i_off)} ({pct_i_off:.1f}%)\n"
    )

async def main():
    """Main monitoring loop."""
    setup_logging()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    stats_mgr = StatsManager()
    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    # Restore states from saved data or initialize
    cur_p = PowerState(stats_mgr.current_power_state) if stats_mgr.current_power_state else None
    cur_i = InternetState(stats_mgr.current_internet_state) if stats_mgr.current_internet_state else None
    
    # Candidates for debounce
    cand_p, cand_i = None, None
    count_p, count_i = 0, 0
    
    last_update = time.monotonic()
    # For duration calculation, we need both monotonic (for delta) and datetime (for persistence)
    # last_*_change_dt is used for duration display, updated when state changes
    last_p_change_dt = stats_mgr.last_power_change or datetime.now()
    last_i_change_dt = stats_mgr.last_internet_change or datetime.now()

    if cur_p or cur_i:
        logging.info(f"Restored state: power={cur_p}, internet={cur_i}")
    await send_msg(bot, "🚀 *Моніторинг запущено*")

    iteration_count = 0
    
    while not shutdown_event.is_set():
        try:
            now_dt = datetime.now()
            
            # === MIDNIGHT: Reset stats and prepare reports ===
            # Daily reset at midnight (new day started)
            if now_dt.date() > stats_mgr.last_daily_reset_day:
                stats_mgr.prepare_daily_report()
                logging.info("Daily stats reset at midnight")
            
            # Weekly reset at Monday midnight (or first run of new week)
            current_week = now_dt.isocalendar()[1]
            current_year = now_dt.isocalendar()[0]
            is_new_week = (current_year > stats_mgr.last_weekly_reset_year or 
                          (current_year == stats_mgr.last_weekly_reset_year and 
                           current_week > stats_mgr.last_weekly_reset_week))
            if is_new_week:
                stats_mgr.prepare_weekly_report()
                logging.info("Weekly stats reset (new week started)")
            
            # === REPORT_HOUR: Send pending reports ===
            # Daily report: send at REPORT_HOUR if we have pending report
            if (now_dt.hour >= REPORT_HOUR and 
                now_dt.date() > stats_mgr.last_daily_report_day and 
                stats_mgr.pending_daily_report):
                await send_msg(bot, create_report("Щоденний звіт", stats_mgr.pending_daily_report))
                stats_mgr.mark_daily_report_sent()
            
            # Weekly report: send on Monday at REPORT_HOUR if we have pending report
            if (now_dt.hour >= REPORT_HOUR and 
                now_dt.weekday() == 0 and
                stats_mgr.pending_weekly_report):
                is_report_week_new = (current_year > stats_mgr.last_weekly_report_year or 
                                     (current_year == stats_mgr.last_weekly_report_year and 
                                      current_week > stats_mgr.last_weekly_report_week))
                if is_report_week_new:
                    await send_msg(bot, create_report("Щотижневий звіт", stats_mgr.pending_weekly_report))
                    stats_mgr.mark_weekly_report_sent()

            new_i, new_p = await check_connectivity()
            
            now = time.monotonic()
            delta = now - last_update
            last_update = now

            # 1. Update statistics independently for each known state
            if cur_p is not None:
                stats_mgr.update_power(cur_p, delta)
            if cur_i is not None:
                stats_mgr.update_internet(cur_i, delta)

            # 2. Debounce for POWER (if internet is present)
            if new_p is not None:
                if new_p != cur_p:
                    if new_p == cand_p: count_p += 1
                    else: cand_p, count_p = new_p, 1
                    
                    if count_p >= DEBOUNCE_COUNT:
                        # Calculate duration using datetime (persisted across restarts)
                        dur_seconds = (now_dt - last_p_change_dt).total_seconds()
                        msg = STATE_MESSAGES[new_p]
                        if cur_p:
                            # Power ON = was OFF before, Power OFF = was ON before
                            dur_label = "Світла не було" if new_p == PowerState.ON else "Світло було"
                            msg += f"\n\n⏱ {dur_label}: {format_duration(dur_seconds)}"
                        await send_msg(bot, msg)
                        logging.info(f"Power state changed: {cur_p} -> {new_p}")
                        
                        cur_p = new_p
                        last_p_change_dt = now_dt
                        stats_mgr.current_power_state = new_p.value
                        stats_mgr.last_power_change = now_dt
                        stats_mgr.increment(new_p.value)
                else: cand_p, count_p = None, 0
            
            # 3. Debounce for INTERNET
            if new_i != cur_i:
                if new_i == cand_i: count_i += 1
                else: cand_i, count_i = new_i, 1
                
                if count_i >= DEBOUNCE_COUNT:
                    # Calculate duration using datetime (persisted across restarts)
                    dur_seconds = (now_dt - last_i_change_dt).total_seconds()
                    if new_i == InternetState.OFFLINE:
                        p_label = "є" if cur_p == PowerState.ON else "немає"
                        msg = STATE_MESSAGES[new_i].format(p_label)
                        if cur_i:
                            msg += f"\n\n⏱ Інтернет був: {format_duration(dur_seconds)}"
                    else:
                        msg = STATE_MESSAGES[new_i]
                        if cur_i:
                            msg += f"\n\n⏱ Інтернету не було: {format_duration(dur_seconds)}"
                    
                    await send_msg(bot, msg)
                    logging.info(f"Internet state changed: {cur_i} -> {new_i}")
                    cur_i = new_i
                    last_i_change_dt = now_dt
                    stats_mgr.current_internet_state = new_i.value
                    stats_mgr.last_internet_change = now_dt
                    stats_mgr.increment(new_i.value)
            else: cand_i, count_i = None, 0

            # First initialization, if power was not present
            if cur_p is None and new_p is not None:
                cur_p = new_p
                stats_mgr.current_power_state = new_p.value
                stats_mgr.last_power_change = now_dt
                last_p_change_dt = now_dt
            if cur_i is None:
                cur_i = new_i
                stats_mgr.current_internet_state = new_i.value
                stats_mgr.last_internet_change = now_dt
                last_i_change_dt = now_dt

            # Save stats periodically (not every iteration)
            iteration_count += 1
            if iteration_count >= STATS_SAVE_INTERVAL:
                stats_mgr.save()
                iteration_count = 0

        except Exception as e: logging.error(f"Loop error: {e}")
        
        try: await asyncio.wait_for(shutdown_event.wait(), timeout=CHECK_INTERVAL)
        except asyncio.TimeoutError: pass

    stats_mgr.save()
    await send_msg(bot, "🛑 *Моніторинг зупинено*")

if __name__ == "__main__":
    asyncio.run(main())
