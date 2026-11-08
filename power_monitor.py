"""
Power Monitor - electricity availability monitoring with Telegram notifications.

The script checks availability of two routers:
- Router 1: connected to internet, runs on battery (checked via ping)
- Router 2: runs on mains power, accessible via forwarded port

State detection logic:
- Router 1 OK + Router 2 OK = Power is ON
- Router 1 OK + Router 2 FAIL = Power is OFF
- Router 1 FAIL = Internet issues (power state unknown, previous one is kept)

Features:
- Telegram notifications on state changes
- Debounce to avoid false triggers (the change is dated to its first observation)
- Daily (00:00-00:00) and weekly (Mon 00:00 - Mon 00:00) reports in local time (TIMEZONE),
  sent at REPORT_HOUR; DST-aware (a DST day is honestly 23h or 25h long)
- Reports are never lost: a report that was due while the monitor was down is sent on startup
- Persistent statistics between restarts (downtime is credited to the last known state)
- Graceful shutdown (SIGTERM/SIGINT), heartbeat file for container health checks

Time handling: every timestamp is kept as an aware UTC datetime; the local timezone is used only
to find day/week boundaries, the report time and for display.
"""

import asyncio
import json
import logging
import os
import signal
import socket
from datetime import date, datetime, time as dtime, timedelta, timezone
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# httpx ships as a python-telegram-bot dependency — no extra requirement needed.
import httpx
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

TIMEZONE = os.environ.get("TIMEZONE", "Europe/Kyiv")
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "9"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "10"))
PING_TIMEOUT = int(os.environ.get("PING_TIMEOUT", "2"))
DEBOUNCE_COUNT = int(os.environ.get("DEBOUNCE_COUNT", "6"))
LOG_FILE = os.environ.get("LOG_FILE", "/opt/folder-monitor/power_monitor.log")
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(5 * 1024 * 1024)))  # 5MB
LOG_BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_COUNT", "3"))
TELEGRAM_RETRIES = int(os.environ.get("TELEGRAM_RETRIES", "3"))
STATS_FILE = os.environ.get("STATS_FILE", "/opt/folder-monitor/power_monitor_stats.json")
STATS_SAVE_INTERVAL = int(os.environ.get("STATS_SAVE_INTERVAL", "6"))  # Save every N iterations (~1 min)
HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/tmp/power_monitor.heartbeat")

# Optional: push current state to the Mango Home dashboard. Unset/empty = feature off.
# Pushes fire on every state change plus a heartbeat on the stats-save cadence
# (~1 min), so the dashboard can treat a silent gap as "monitor down".
MANGO_WEBHOOK_URL = os.environ.get("MANGO_WEBHOOK_URL")
MANGO_WEBHOOK_TOKEN = os.environ.get("MANGO_WEBHOOK_TOKEN")
MANGO_WEBHOOK_TIMEOUT = int(os.environ.get("MANGO_WEBHOOK_TIMEOUT", "5"))
# ==========================================

UTC = timezone.utc
TZ = ZoneInfo(TIMEZONE)
STATS_VERSION = 2
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


class PowerState(Enum):
    ON = "POWER_ON"
    OFF = "POWER_OFF"


class InternetState(Enum):
    ONLINE = "NET_ONLINE"
    OFFLINE = "NET_OFFLINE"


STATE_MESSAGES = {
    PowerState.ON: "🟢 Світло зʼявилось",
    PowerState.OFF: "🔴 Світло зникло",
    InternetState.ONLINE: "📶 Інтернет зʼявився",
    InternetState.OFFLINE: "📵 Інтернет зник ({})",
}
POWER_HINT = {PowerState.ON: "світло є", PowerState.OFF: "світла немає"}

KEYS = ("POWER_ON", "POWER_OFF", "NET_ONLINE", "NET_OFFLINE")


# ================= TIME HELPERS =================
def utcnow() -> datetime:
    return datetime.now(UTC)


def to_utc(value: str) -> datetime:
    """Parse an ISO timestamp. Naive values (stats format v1) were written in the container's
    timezone, which was UTC."""
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def local_date(t: datetime) -> date:
    return t.astimezone(TZ).date()


def local_at(d: date, hour: int = 0) -> datetime:
    """UTC instant of the local wall-clock time `hour`:00 on day `d` (midnight and 09:00 always
    exist in Europe/Kyiv; DST switches happen at 03:00/04:00)."""
    return datetime.combine(d, dtime(hour), tzinfo=TZ).astimezone(UTC)


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ================= STATISTICS =================
class Period:
    """Statistics of one calendar period: a local day or a local ISO week."""

    def __init__(self, kind: str, first_day: date,
                 initial_power: Optional[str] = None, initial_internet: Optional[str] = None):
        self.kind = kind  # "daily" | "weekly"
        self.first_day = first_day
        self.days = 1 if kind == "daily" else 7
        self.start = local_at(first_day)
        self.end = local_at(first_day + timedelta(days=self.days))
        self.durations = {k: 0.0 for k in KEYS}
        self.counts = {k: 0 for k in KEYS}
        # State at the period start and at its end; used to fill time with no observations
        # (e.g. the very first run) so a report always covers the whole period.
        self.initial_power = initial_power
        self.initial_internet = initial_internet
        self.final_power: Optional[str] = None
        self.final_internet: Optional[str] = None

    @property
    def last_day(self) -> date:
        return self.first_day + timedelta(days=self.days - 1)

    @property
    def length(self) -> float:
        return (self.end - self.start).total_seconds()

    def report_due(self) -> datetime:
        return local_at(self.first_day + timedelta(days=self.days), REPORT_HOUR)

    def contains(self, t: datetime) -> bool:
        return self.start <= t < self.end

    def _overlap(self, t0: datetime, t1: datetime) -> float:
        return max(0.0, (min(t1, self.end) - max(t0, self.start)).total_seconds())

    def credit(self, key: str, t0: datetime, t1: datetime):
        self.durations[key] += self._overlap(t0, t1)

    def transfer(self, from_key: str, to_key: str, t0: datetime, t1: datetime):
        """Re-attribute time already credited to `from_key` within [t0, t1) to `to_key`."""
        moved = min(self._overlap(t0, t1), self.durations[from_key])
        self.durations[from_key] -= moved
        self.durations[to_key] += moved

    def next(self, power: Optional[str], internet: Optional[str]) -> "Period":
        return Period(self.kind, self.first_day + timedelta(days=self.days), power, internet)

    def report_durations(self) -> dict:
        """Durations that add up to exactly the period length for each pair."""
        d = dict(self.durations)
        length = self.length
        pairs = (("POWER_ON", "POWER_OFF", self.initial_power or self.final_power),
                 ("NET_ONLINE", "NET_OFFLINE", self.initial_internet or self.final_internet))
        for a, b, fill_key in pairs:
            total = d[a] + d[b]
            if total > length:
                d[a], d[b] = d[a] * length / total, d[b] * length / total
            elif total < length and fill_key in (a, b):
                d[fill_key] += length - total
        return d

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "first_day": self.first_day.isoformat(),
            "durations": self.durations,
            "counts": self.counts,
            "initial_power": self.initial_power,
            "initial_internet": self.initial_internet,
            "final_power": self.final_power,
            "final_internet": self.final_internet,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Period":
        p = cls(data["kind"], date.fromisoformat(data["first_day"]),
                data.get("initial_power"), data.get("initial_internet"))
        p.durations = {k: float(data["durations"].get(k, 0.0)) for k in KEYS}
        p.counts = {k: int(data["counts"].get(k, 0)) for k in KEYS}
        p.final_power = data.get("final_power")
        p.final_internet = data.get("final_internet")
        return p


class StatsManager:
    """Current states, the running daily/weekly periods and finished periods awaiting a report."""

    def __init__(self, path: str = STATS_FILE, now: Optional[datetime] = None):
        now = now or utcnow()
        self.path = Path(path)
        self.power_state: Optional[PowerState] = None
        self.internet_state: Optional[InternetState] = None
        self.last_power_change: Optional[datetime] = None
        self.last_internet_change: Optional[datetime] = None
        self.last_tick: Optional[datetime] = None  # up to when durations have been credited
        today = local_date(now)
        self.daily = Period("daily", today)
        self.weekly = Period("weekly", week_monday(today))
        self.pending: list[Period] = []
        self._load()

    # ---------- persistence ----------
    def _load(self):
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text())
            if data.get("version") == STATS_VERSION:
                self._load_v2(data)
            else:
                self._load_v1(data)
        except Exception as e:
            logging.warning(f"Stats load failed: {e}")

    def _load_states(self, data: dict, prefix: str = ""):
        p = data.get(f"{prefix}power_state")
        i = data.get(f"{prefix}internet_state")
        self.power_state = PowerState(p) if p else None
        self.internet_state = InternetState(i) if i else None
        if data.get("last_power_change"):
            self.last_power_change = to_utc(data["last_power_change"])
        if data.get("last_internet_change"):
            self.last_internet_change = to_utc(data["last_internet_change"])

    def _load_v2(self, data: dict):
        self._load_states(data)
        self.daily = Period.from_dict(data["daily"])
        self.weekly = Period.from_dict(data["weekly"])
        self.pending = [Period.from_dict(p) for p in data.get("pending", [])]
        if data.get("last_tick"):
            self.last_tick = to_utc(data["last_tick"])

    def _load_v1(self, data: dict):
        """Migrate the v1 format (naive UTC timestamps, UTC-midnight periods)."""
        self._load_states(data, prefix="current_")
        cur_p = self.power_state.value if self.power_state else None
        cur_i = self.internet_state.value if self.internet_state else None

        def convert(old: dict, kind: str) -> Period:
            start = to_utc(old["start_date"])
            first = local_date(start) if kind == "daily" else week_monday(local_date(start))
            p = Period(kind, first, cur_p, cur_i)
            p.durations = {k: float(old["durations"].get(k, 0.0)) for k in KEYS}
            p.counts = {k: int(old["counts"].get(k, 0)) for k in KEYS}
            return p, start

        self.daily, d_start = convert(data["daily"], "daily")
        self.weekly, _ = convert(data["weekly"], "weekly")
        d = data["daily"]["durations"]
        recorded = max(d["POWER_ON"] + d["POWER_OFF"], d["NET_ONLINE"] + d["NET_OFFLINE"])
        self.last_tick = d_start + timedelta(seconds=recorded)  # ~ time of the last v1 save
        logging.info("Stats migrated from format v1")

    def save(self):
        """Persist stats to file atomically."""
        try:
            data = {
                "version": STATS_VERSION,
                "power_state": self.power_state.value if self.power_state else None,
                "internet_state": self.internet_state.value if self.internet_state else None,
                "last_power_change": self.last_power_change.isoformat() if self.last_power_change else None,
                "last_internet_change": self.last_internet_change.isoformat() if self.last_internet_change else None,
                "last_tick": self.last_tick.isoformat() if self.last_tick else None,
                "daily": self.daily.to_dict(),
                "weekly": self.weekly.to_dict(),
                "pending": [p.to_dict() for p in self.pending],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            tmp.replace(self.path)
        except Exception as e:
            logging.error(f"Stats save failed: {e}")

    # ---------- accounting ----------
    def _periods(self) -> list:
        return [self.daily, self.weekly] + self.pending

    def advance(self, now: datetime):
        """Credit [last_tick, now) to the current states, closing periods exactly at local
        midnight. After a restart this credits the downtime to the last known state."""
        t0 = self.last_tick if self.last_tick and self.last_tick < now else None
        p_key = self.power_state.value if self.power_state else None
        i_key = self.internet_state.value if self.internet_state else None
        for attr in ("daily", "weekly"):
            period = getattr(self, attr)
            while True:
                if t0 is not None:
                    if p_key:
                        period.credit(p_key, t0, now)
                    if i_key:
                        period.credit(i_key, t0, now)
                if now < period.end:
                    break
                period.final_power, period.final_internet = p_key, i_key
                self.pending.append(period)
                period = period.next(p_key, i_key)
            setattr(self, attr, period)
        self.pending.sort(key=lambda p: (p.end, p.kind != "daily"))
        if self.last_tick is None or now > self.last_tick:
            self.last_tick = now

    def set_initial(self, state, now: datetime):
        """First observation ever (no persisted state)."""
        for p in (self.daily, self.weekly):
            if isinstance(state, PowerState) and p.initial_power is None:
                p.initial_power = state.value
            if isinstance(state, InternetState) and p.initial_internet is None:
                p.initial_internet = state.value
        if isinstance(state, PowerState):
            self.power_state, self.last_power_change = state, now
        else:
            self.internet_state, self.last_internet_change = state, now

    def change(self, new, since: datetime):
        """Confirmed state change that actually happened at `since` (first observation).
        Call after advance(now): the [since, now) time is moved from the old state to the new."""
        old = self.power_state if isinstance(new, PowerState) else self.internet_state
        for p in self._periods():
            if old is not None:
                p.transfer(old.value, new.value, since, self.last_tick or since)
            if p.contains(since):
                p.counts[new.value] += 1
        if isinstance(new, PowerState):
            self.power_state, self.last_power_change = new, since
        else:
            self.internet_state, self.last_internet_change = new, since

    def due_reports(self, now: datetime) -> list:
        return [p for p in self.pending if now >= p.report_due()]

    def next_report_at(self, now: datetime) -> Optional[datetime]:
        """Earliest report that becomes due in the future (already-due ones are retried on the
        normal CHECK_INTERVAL cadence)."""
        return min((p.report_due() for p in self.pending if p.report_due() > now), default=None)


# ================= FORMATTING =================
def format_duration(seconds: float) -> str:
    """Human-readable Ukrainian duration. Up to 48h it is shown in hours (a full day = "24г")."""
    total = int(round(seconds))
    if total < 60:
        return f"{total}с"
    days = 0
    if total >= 2 * 86400:
        days, total = divmod(total, 86400)
    hours, rem = divmod(total, 3600)
    minutes, sec = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}г")
    if minutes:
        parts.append(f"{minutes}хв")
    if sec:
        parts.append(f"{sec}с")
    return " ".join(parts)


def format_pct(value: float, total: float) -> str:
    if total <= 0:
        return "0%"
    pct = value / total * 100
    if value > 0 and pct < 0.1:
        return "<0.1%"
    if value < total and pct > 99.9:
        return ">99.9%"
    return f"{pct:.1f}%"


def create_report(p: Period) -> str:
    """Generate statistics report text for Telegram."""
    d = p.report_durations()
    length = round(p.length)
    if p.kind == "daily":
        title = "Щоденний звіт"
        when = f"{p.first_day:%d.%m} ({WEEKDAYS[p.first_day.weekday()]})"
    else:
        title = "Щотижневий звіт"
        when = f"{p.first_day:%d.%m} — {p.last_day:%d.%m}"

    def pair(on_key: str, off_key: str):
        on = round(d[on_key])
        off = max(0, length - on) if d[on_key] + d[off_key] > 0 else 0
        return on, off, on + off

    p_on, p_off, p_total = pair("POWER_ON", "POWER_OFF")
    i_on, i_off, i_total = pair("NET_ONLINE", "NET_OFFLINE")
    return (
        f"📊 *{title}*\n"
        f"📅 {when}\n\n"
        f"💡 *Світло було:* {format_duration(p_on)} ({format_pct(p_on, p_total)})\n"
        f"🔌 *Світла не було:* {format_duration(p_off)} ({format_pct(p_off, p_total)})\n"
        f"📉 Вимкнень світла: {p.counts['POWER_OFF']}\n\n"
        f"📶 *Інтернет був:* {format_duration(i_on)} ({format_pct(i_on, i_total)})\n"
        f"📵 *Інтернету не було:* {format_duration(i_off)} ({format_pct(i_off, i_total)})\n"
        f"📉 Відключень інтернету: {p.counts['NET_OFFLINE']}\n"
    )


def power_message(new: PowerState, duration: Optional[float]) -> str:
    msg = STATE_MESSAGES[new]
    if duration is not None:
        label = "Світла не було" if new == PowerState.ON else "Світло було"
        msg += f"\n\n⏱ {label}: {format_duration(duration)}"
    return msg


def internet_message(new: InternetState, power: Optional[PowerState], duration: Optional[float]) -> str:
    if new == InternetState.OFFLINE:
        msg = STATE_MESSAGES[new].format(POWER_HINT.get(power, "стан світла невідомий"))
        label = "Інтернет був"
    else:
        msg = STATE_MESSAGES[new]
        label = "Інтернету не було"
    if duration is not None:
        msg += f"\n\n⏱ {label}: {format_duration(duration)}"
    return msg


# ================= I/O =================
def setup_logging():
    """Configure logging with rotation."""
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    except OSError as e:
        print(f"File logging disabled: {e}")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)


async def check_connectivity() -> tuple:
    """Determine internet and power states independently."""
    net_ok = await ping_async(ROUTER_1_IP)
    i_state = InternetState.ONLINE if net_ok else InternetState.OFFLINE

    # Without internet the port can't be checked: power state is unknown (None).
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
    except Exception:
        return False


def check_port(ip: str, port: int) -> bool:
    """Check if TCP port is open on the given IP address."""
    try:
        with socket.create_connection((ip, port), timeout=2.0):
            return True
    except OSError:
        return False


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


async def push_status_to_mango(client: httpx.AsyncClient, stats: StatsManager):
    """Best-effort push of the current state to the Mango Home dashboard. Never raises."""
    payload = {
        "power_state": stats.power_state.value if stats.power_state else None,
        "internet_state": stats.internet_state.value if stats.internet_state else None,
        "last_power_change": stats.last_power_change.isoformat() if stats.last_power_change else None,
        "last_internet_change": stats.last_internet_change.isoformat() if stats.last_internet_change else None,
    }
    try:
        resp = await client.post(MANGO_WEBHOOK_URL, json=payload,
                                 headers={"X-Report-Token": MANGO_WEBHOOK_TOKEN or ""})
        if resp.status_code != 200:
            logging.warning(f"Mango webhook returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logging.warning(f"Mango webhook push failed: {e}")


async def send_due_reports(bot: Bot, stats: StatsManager, now: datetime):
    for period in stats.due_reports(now):
        if not await send_msg(bot, create_report(period)):
            break  # keep it pending, retry on the next iteration
        stats.pending.remove(period)
        stats.save()


def touch_heartbeat():
    try:
        Path(HEARTBEAT_FILE).touch()
    except OSError:
        pass


class Debouncer:
    """Confirms a new state after DEBOUNCE_COUNT consecutive observations, remembering when it
    was first observed."""

    def __init__(self):
        self.candidate = None
        self.count = 0
        self.since: Optional[datetime] = None

    def observe(self, new, current, now: datetime) -> Optional[datetime]:
        """Returns the first-observation time when `new` is confirmed, otherwise None."""
        if new == current:
            self.candidate, self.count, self.since = None, 0, None
            return None
        if new == self.candidate:
            self.count += 1
        else:
            self.candidate, self.count, self.since = new, 1, now
        if self.count >= DEBOUNCE_COUNT:
            since = self.since
            self.candidate, self.count, self.since = None, 0, None
            return since
        return None


# ================= MAIN LOOP =================
async def main():
    setup_logging()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    now = utcnow()
    stats = StatsManager(now=now)
    gap = (now - stats.last_tick).total_seconds() if stats.last_tick else None
    stats.advance(now)  # downtime -> last known state; closes periods that ended meanwhile
    stats.save()
    logging.info(f"Restored state: power={stats.power_state}, internet={stats.internet_state}")

    start_msg = "🚀 *Моніторинг запущено*"
    if gap is not None and gap > 3 * CHECK_INTERVAL:
        start_msg += f"\n⏸ Перерва в моніторингу: {format_duration(gap)}"
    await send_msg(bot, start_msg)

    mango_client = httpx.AsyncClient(timeout=MANGO_WEBHOOK_TIMEOUT) if MANGO_WEBHOOK_URL else None
    if mango_client:
        await push_status_to_mango(mango_client, stats)

    power_db, net_db = Debouncer(), Debouncer()
    iteration_count = 0

    while not shutdown_event.is_set():
        try:
            await send_due_reports(bot, stats, utcnow())

            new_i, new_p = await check_connectivity()
            now = utcnow()
            stats.advance(now)
            changed = False

            # POWER (only known while there is internet)
            if new_p is not None:
                if stats.power_state is None:
                    stats.set_initial(new_p, now)
                else:
                    since = power_db.observe(new_p, stats.power_state, now)
                    if since is not None:
                        old = stats.power_state
                        dur = (since - stats.last_power_change).total_seconds() if stats.last_power_change else None
                        stats.change(new_p, since)
                        await send_msg(bot, power_message(new_p, dur))
                        logging.info(f"Power state changed: {old} -> {new_p} (since {since.isoformat()})")
                        changed = True

            # INTERNET
            if stats.internet_state is None:
                stats.set_initial(new_i, now)
            else:
                since = net_db.observe(new_i, stats.internet_state, now)
                if since is not None:
                    old = stats.internet_state
                    dur = (since - stats.last_internet_change).total_seconds() if stats.last_internet_change else None
                    stats.change(new_i, since)
                    await send_msg(bot, internet_message(new_i, stats.power_state, dur))
                    logging.info(f"Internet state changed: {old} -> {new_i} (since {since.isoformat()})")
                    changed = True

            if changed and mango_client:
                await push_status_to_mango(mango_client, stats)

            touch_heartbeat()
            iteration_count += 1
            if changed or iteration_count >= STATS_SAVE_INTERVAL:
                stats.save()
                if iteration_count >= STATS_SAVE_INTERVAL:
                    iteration_count = 0
                    # Heartbeat: keeps the dashboard's staleness check alive.
                    if mango_client:
                        await push_status_to_mango(mango_client, stats)

        except Exception as e:
            logging.exception(f"Loop error: {e}")

        # Sleep CHECK_INTERVAL, but wake up exactly when a report becomes due.
        timeout = float(CHECK_INTERVAL)
        now = utcnow()
        next_report = stats.next_report_at(now)
        if next_report is not None:
            timeout = max(0.0, min(timeout, (next_report - now).total_seconds()))
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    stats.advance(utcnow())
    stats.save()
    if mango_client:
        await mango_client.aclose()
    await send_msg(bot, "🛑 *Моніторинг зупинено*")


if __name__ == "__main__":
    asyncio.run(main())
