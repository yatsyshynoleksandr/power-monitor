"""
DTEK planned outage schedule for one queue (group), posted to Telegram.

Source: a JSON mirror of dtek-krem.com.ua (https://github.com/Baskerville42/outage-data-ua,
data/kyiv-region.json), polled with ETag. For every date one message is published; when the
schedule for that date changes, the message is edited silently: the "current" block is replaced
and a line with the time and the diff is appended to its history. Messages are never deleted.

Schedule model: 48 half-hour slots per day, each "0" (power), "1" (no power) or "2" (maybe).
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

UTC = timezone.utc
ON, OFF, MAYBE = "0", "1", "2"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
# Hourly value -> (first half-hour, second half-hour)
HALVES = {
    "yes": (ON, ON), "no": (OFF, OFF), "first": (OFF, ON), "second": (ON, OFF),
    "maybe": (MAYBE, MAYBE), "mfirst": (MAYBE, ON), "msecond": (ON, MAYBE),
}
MAX_TEXT = 4000      # Telegram limit is 4096
KEEP_DAYS = 14       # how long per-date state is kept (plan vs fact in reports)


def normalize_group(group: str) -> str:
    """"2.1" / "GPV2.1" -> "GPV2.1"."""
    g = group.strip()
    return g if g.upper().startswith("GPV") else f"GPV{g}"


def group_label(group: str) -> str:
    return f"черга {group[3:]}"


def slots_from_hours(hours: dict) -> str:
    out = []
    for h in range(1, 25):
        value = hours.get(str(h), "yes")
        if value not in HALVES:
            logging.warning(f"DTEK: unknown slot value {value!r}, treated as 'yes'")
        out.extend(HALVES.get(value, (ON, ON)))
    return "".join(out)


def intervals(slots: str, kind: str) -> list:
    """Half-hour index intervals [start, end) where slots == kind."""
    res, start = [], None
    for i, s in enumerate(slots + ON):
        if s == kind and start is None:
            start = i
        elif s != kind and start is not None:
            res.append((start, i))
            start = None
    return res


def fmt_slot(i: int) -> str:
    h, m = divmod(i * 30, 60)
    return f"{h:02d}:{m:02d}"


def fmt_interval(iv: tuple) -> str:
    return f"{fmt_slot(iv[0])}–{fmt_slot(iv[1])}"


def fmt_minutes(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    parts = ([f"{h}г"] if h else []) + ([f"{m}хв"] if m else [])
    return " ".join(parts) or "0хв"


def has_outages(slots: str) -> bool:
    return OFF in slots or MAYBE in slots


def summary(slots: str) -> str:
    parts = [fmt_interval(iv) for iv in intervals(slots, OFF)]
    parts += [f"{fmt_interval(iv)} (можливо)" for iv in intervals(slots, MAYBE)]
    return ", ".join(parts) or "без відключень"


def diff_text(old: str, new: str) -> str:
    """Human-readable diff of two schedules, e.g. '➕ 10:00–11:00 · 17:30–20:30 → 18:00–20:30'."""
    parts = []
    for kind, mark in ((OFF, ""), (MAYBE, "🟡 ")):
        items = sorted([(a, b, "o") for a, b in intervals(old, kind)] +
                       [(a, b, "n") for a, b in intervals(new, kind)])
        groups, end = [], -1
        for a, b, src in items:  # connected components of overlapping intervals
            if groups and a < end:
                groups[-1].append((a, b, src))
                end = max(end, b)
            else:
                groups.append([(a, b, src)])
                end = b
        for g in groups:
            olds = [fmt_interval((a, b)) for a, b, s in g if s == "o"]
            news = [fmt_interval((a, b)) for a, b, s in g if s == "n"]
            if olds == news:
                continue
            if not olds:
                parts.append(f"➕ {mark}{', '.join(news)}")
            elif not news:
                parts.append(f"➖ {mark}{', '.join(olds)}")
            else:
                parts.append(f"{mark}{', '.join(olds)} → {', '.join(news)}")
    return " · ".join(parts) or "без змін"


class DayState:
    def __init__(self, slots: str, message_id: Optional[int] = None,
                 history: Optional[list] = None, last_text: Optional[str] = None):
        self.slots = slots
        self.message_id = message_id
        self.history = history or []
        self.last_text = last_text

    def to_dict(self) -> dict:
        return {"slots": self.slots, "message_id": self.message_id,
                "history": self.history, "last_text": self.last_text}

    @classmethod
    def from_dict(cls, d: dict) -> "DayState":
        return cls(d["slots"], d.get("message_id"), d.get("history", []), d.get("last_text"))


class DtekWatcher:
    """Polls the schedule and keeps one Telegram message per date in sync with it."""

    def __init__(self, url: str, group: str, state_file: str, tz,
                 send: Callable[[str], Awaitable[Optional[int]]],
                 edit: Callable[[int, str], Awaitable[str]],
                 poll_interval: int = 300, stale_hours: float = 2.0,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.url = url
        self.group = normalize_group(group)
        self.path = Path(state_file)
        self.tz = tz
        self.send = send            # async (text) -> message_id | None
        self.edit = edit            # async (message_id, text) -> "ok" | "not_modified" | "gone" | "error"
        self.poll_interval = poll_interval
        self.stale_hours = stale_hours
        self.now = now
        self.etag: Optional[str] = None
        self.source_updated: Optional[datetime] = None
        self.days: dict = {}  # "YYYY-MM-DD" -> DayState
        self._missing_group_logged = False
        self._load()

    # ---------- persistence ----------
    def _load(self):
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text())
                self.etag = d.get("etag")
                if d.get("source_updated"):
                    self.source_updated = datetime.fromisoformat(d["source_updated"])
                self.days = {k: DayState.from_dict(v) for k, v in d.get("days", {}).items()}
        except Exception as e:
            logging.warning(f"DTEK state load failed: {e}")

    def save(self):
        try:
            data = {
                "etag": self.etag,
                "source_updated": self.source_updated.isoformat() if self.source_updated else None,
                "days": {k: v.to_dict() for k, v in sorted(self.days.items())},
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            tmp.replace(self.path)
        except Exception as e:
            logging.error(f"DTEK state save failed: {e}")

    # ---------- helpers ----------
    def today(self) -> date:
        return self.now().astimezone(self.tz).date()

    def is_stale(self) -> bool:
        if self.source_updated is None:
            return False
        return (self.now() - self.source_updated) > timedelta(hours=self.stale_hours)

    def planned(self, d: date) -> Optional[tuple]:
        """(off_minutes, maybe_minutes) of the last known schedule for date d, or None."""
        day = self.days.get(d.isoformat())
        if day is None:
            return None
        return day.slots.count(OFF) * 30, day.slots.count(MAYBE) * 30

    def render(self, d: date, day: DayState) -> str:
        lines = [f"📅 *Графік на {d:%d.%m} ({WEEKDAYS[d.weekday()]})* · {group_label(self.group)}", ""]
        off, maybe = intervals(day.slots, OFF), intervals(day.slots, MAYBE)
        if not off and not maybe:
            lines.append("✅ Відключень не заплановано")
        else:
            lines += [f"🔴 {fmt_interval(iv)}" for iv in off]
            lines += [f"🟡 {fmt_interval(iv)} (можливо)" for iv in maybe]
            total = f"⏱ Без світла: {fmt_minutes(day.slots.count(OFF) * 30)}"
            if maybe:
                total += f" (+ можливо {fmt_minutes(day.slots.count(MAYBE) * 30)})"
            lines.append(total)
        if self.is_stale() and d >= self.today():
            upd = self.source_updated.astimezone(self.tz)
            lines.append(f"⚠️ Дані могли застаріти (джерело оновлювалось {upd:%d.%m %H:%M})")
        history = list(day.history)
        while True:
            text = "\n".join(lines + ["", "📝 *Історія:*"] + [f"• {h}" for h in history])
            if len(text) <= MAX_TEXT or len(history) <= 2:
                return text
            # keep the first ("published") and the newest lines
            skipped = history[1].startswith("… ще ")
            n = int(history[1].split()[2]) + 1 if skipped else 1
            history = [history[0], f"… ще {n} змін"] + history[3 if skipped else 2:]

    # ---------- core ----------
    def _parse(self, data: dict) -> tuple:
        fact = data.get("fact") or {}
        when = None
        try:
            when = datetime.strptime(fact.get("update", ""), "%d.%m.%Y %H:%M").replace(tzinfo=self.tz)
        except ValueError:
            pass
        try:
            src = datetime.fromisoformat(data["lastUpdated"].replace("Z", "+00:00"))
            self.source_updated = src.astimezone(UTC)
        except Exception:
            pass
        schedules = {}
        for key, groups in (fact.get("data") or {}).items():
            d = datetime.fromtimestamp(int(key), self.tz).date()
            hours = groups.get(self.group)
            if hours is None:
                if not self._missing_group_logged:
                    logging.warning(f"DTEK: group {self.group} not found in the source")
                    self._missing_group_logged = True
                continue
            schedules[d] = slots_from_hours(hours)
        return schedules, when

    async def apply(self, data: dict):
        """Process a fresh copy of the source document."""
        schedules, when = self._parse(data)
        now_local = self.now().astimezone(self.tz)
        stamp = (when or now_local).astimezone(self.tz).strftime("%d.%m %H:%M")
        today = self.today()
        for d, slots in sorted(schedules.items()):
            if d < today:
                continue
            key = d.isoformat()
            day = self.days.get(key)
            if day is None:
                day = DayState(slots)
                self.days[key] = day
                if has_outages(slots):
                    day.history.append(f"{stamp} — опубліковано: {summary(slots)}")
            elif slots != day.slots:
                if day.message_id is None and not day.history:
                    if has_outages(slots):  # first schedule with outages for this date
                        day.history.append(f"{stamp} — опубліковано: {summary(slots)}")
                else:
                    day.history.append(f"{stamp} — {diff_text(day.slots, slots)}")
                day.slots = slots
        await self.sync_messages()
        self._prune(today)
        self.save()

    async def sync_messages(self):
        """Make every current/future published message match its rendered text (also retries
        failed edits and refreshes the "stale data" note)."""
        today = self.today()
        for key, day in sorted(self.days.items()):
            d = date.fromisoformat(key)
            if d < today or not day.history:
                continue
            text = self.render(d, day)
            if text == day.last_text and day.message_id is not None:
                continue
            if day.message_id is not None:
                result = await self.edit(day.message_id, text)
                if result in ("ok", "not_modified"):
                    day.last_text = text
                    continue
                if result == "error":
                    continue  # transient, retry on the next poll
                logging.warning(f"DTEK: message {day.message_id} for {key} is gone, sending a new one")
            mid = await self.send(text)
            if mid is not None:
                day.message_id, day.last_text = mid, text

    def _prune(self, today: date):
        cutoff = today - timedelta(days=KEEP_DAYS)
        for key in [k for k in self.days if date.fromisoformat(k) < cutoff]:
            del self.days[key]

    async def poll(self, client: httpx.AsyncClient):
        headers = {"If-None-Match": self.etag} if self.etag else {}
        resp = await client.get(self.url, headers=headers)
        if resp.status_code == 304:
            await self.sync_messages()
            self.save()
            return
        resp.raise_for_status()
        self.etag = resp.headers.get("ETag")
        await self.apply(resp.json())

    async def run(self, stop_event):
        import asyncio
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            while not stop_event.is_set():
                try:
                    await self.poll(client)
                except Exception as e:
                    logging.warning(f"DTEK poll failed: {e}")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
