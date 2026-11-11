"""Tests for the DTEK schedule watcher. Run: python -m unittest discover -s tests"""
import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dtek  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
UTC = timezone.utc


def hours(**overrides):
    """24 hourly values, 'yes' unless overridden: hours(h4='first', h11='no')."""
    return {str(h): overrides.get(f"h{h}", "yes") for h in range(1, 25)}


def doc(days: dict, update="09.11.2026 18:42", last_updated="2026-11-09T16:45:00.000Z", group="GPV2.1"):
    data = {}
    for d, hrs in days.items():
        ts = int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())
        data[str(ts)] = {group: hrs, "GPV1.1": hours()}
    return {"lastUpdated": last_updated, "fact": {"data": data, "update": update}}


class FakeTelegram:
    def __init__(self):
        self.messages = {}
        self.next_id = 100
        self.sent = []
        self.edits = []
        self.edit_result = None
        self.pinned = set()
        self.pin_calls = 0
        self.pin_result = None

    async def pin(self, mid):
        self.pin_calls += 1
        if self.pin_result:
            return self.pin_result
        if mid not in self.messages:
            return "gone"
        self.pinned.add(mid)
        return "ok"

    async def unpin(self, mid):
        if mid not in self.pinned:
            return "gone"
        self.pinned.discard(mid)
        return "ok"

    async def send(self, text):
        self.next_id += 1
        self.messages[self.next_id] = text
        self.sent.append(self.next_id)
        return self.next_id

    async def edit(self, mid, text):
        if self.edit_result:
            return self.edit_result
        if mid not in self.messages:
            return "gone"
        if self.messages[mid] == text:
            return "not_modified"
        self.messages[mid] = text
        self.edits.append(mid)
        return "ok"


class Parsing(unittest.TestCase):
    def test_slots_and_intervals(self):
        s = dtek.slots_from_hours(hours(h1="no", h2="no", h3="no", h4="first", h10="second",
                                        h11="no", h18="first", h21="mfirst", h24="no"))
        self.assertEqual([dtek.fmt_interval(i) for i in dtek.intervals(s, dtek.OFF)],
                         ["00:00–03:30", "09:30–11:00", "17:00–17:30", "23:00–24:00"])
        self.assertEqual([dtek.fmt_interval(i) for i in dtek.intervals(s, dtek.MAYBE)], ["20:00–20:30"])
        self.assertEqual(s.count(dtek.OFF) * 30, 6 * 60 + 30)

    def test_group_names(self):
        self.assertEqual(dtek.normalize_group("2.1"), "GPV2.1")
        self.assertEqual(dtek.normalize_group(" GPV3.2 "), "GPV3.2")
        self.assertEqual(dtek.group_label("GPV2.1"), "черга 2.1")

    def test_diff(self):
        old = dtek.slots_from_hours(hours(h4="no", h5="first", h18="second", h19="no", h20="no"))
        new = dtek.slots_from_hours(hours(h11="no", h19="no", h20="no"))
        # old: 03:00–04:30, 17:30–20:00 ; new: 10:00–11:00, 18:00–20:00
        self.assertEqual(dtek.diff_text(old, new),
                         "➖ 03:00–04:30 · ➕ 10:00–11:00 · 17:30–20:00 → 18:00–20:00")
        self.assertEqual(dtek.diff_text(old, old), "без змін")
        maybe = dtek.slots_from_hours(hours(h4="no", h5="first", h18="second", h19="no", h20="no", h23="maybe"))
        self.assertEqual(dtek.diff_text(old, maybe), "➕ 🟡 22:00–23:00")

    def test_fmt_minutes(self):
        self.assertEqual(dtek.fmt_minutes(450), "7г 30хв")
        self.assertEqual(dtek.fmt_minutes(60), "1г")
        self.assertEqual(dtek.fmt_minutes(0), "0хв")


class Pinning(unittest.TestCase):
    """Variant B: today's and tomorrow's schedules are pinned; a day is unpinned after midnight."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "dtek_state.json")
        self.tg = FakeTelegram()
        self.now = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)  # 10:00 Kyiv

    def tearDown(self):
        self.tmp.cleanup()

    def watcher(self, pin=True):
        return dtek.DtekWatcher("http://x", "2.1", self.state, TZ, self.tg.send, self.tg.edit,
                                pin=self.tg.pin if pin else None, unpin=self.tg.unpin,
                                now=lambda: self.now)

    def test_today_and_tomorrow_pinned_then_yesterday_unpinned(self):
        w = self.watcher()
        today, tomorrow = date(2026, 11, 9), date(2026, 11, 10)
        asyncio.run(w.apply(doc({today: hours(h4="no")}, update="09.11.2026 07:56")))
        m_today = self.tg.sent[0]
        self.assertEqual(self.tg.pinned, {m_today})

        self.now = datetime(2026, 11, 9, 18, 30, tzinfo=UTC)  # 20:30, tomorrow published
        asyncio.run(w.apply(doc({today: hours(h4="no"), tomorrow: hours(h10="no")}, update="09.11.2026 20:04")))
        m_tomorrow = self.tg.sent[1]
        self.assertEqual(self.tg.pinned, {m_today, m_tomorrow})  # both in the evening

        self.now = datetime(2026, 11, 9, 22, 5, tzinfo=UTC)  # 00:05 on 10.11
        w2 = self.watcher()  # also across a restart
        asyncio.run(w2.sync_messages())
        self.assertEqual(self.tg.pinned, {m_tomorrow})
        self.assertFalse(w2.days["2026-11-09"].pinned)
        self.assertTrue(w2.days["2026-11-10"].pinned)

    def test_state_from_v130_gets_pinned(self):
        w = self.watcher(pin=False)
        asyncio.run(w.apply(doc({date(2026, 11, 9): hours(h4="no")})))
        self.assertEqual(self.tg.pinned, set())
        w2 = self.watcher()
        asyncio.run(w2.sync_messages())
        self.assertEqual(self.tg.pinned, {self.tg.sent[0]})

    def test_pin_failure_is_retried_hourly_not_every_poll(self):
        self.tg.pin_result = "error"
        w = self.watcher()
        asyncio.run(w.apply(doc({date(2026, 11, 9): hours(h4="no")})))
        asyncio.run(w.sync_messages())
        self.assertEqual(self.tg.pin_calls, 1)
        self.tg.pin_result = None
        self.now += timedelta(minutes=61)
        asyncio.run(w.sync_messages())
        self.assertEqual(self.tg.pinned, {self.tg.sent[0]})

    def test_replaced_message_is_pinned(self):
        w = self.watcher()
        d = date(2026, 11, 9)
        asyncio.run(w.apply(doc({d: hours(h4="no")})))
        old = self.tg.sent[0]
        del self.tg.messages[old]
        self.tg.pinned.discard(old)
        asyncio.run(w.apply(doc({d: hours(h4="no", h5="no")}, update="09.11.2026 11:00")))
        self.assertEqual(self.tg.pinned, {self.tg.sent[1]})

    def test_pinning_disabled_still_unpins_old(self):
        w = self.watcher()
        asyncio.run(w.apply(doc({date(2026, 11, 9): hours(h4="no")})))
        self.now = datetime(2026, 11, 9, 22, 5, tzinfo=UTC)
        asyncio.run(self.watcher(pin=False).sync_messages())
        self.assertEqual(self.tg.pinned, set())


class Watcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "dtek_state.json")
        self.tg = FakeTelegram()
        self.now = datetime(2026, 11, 9, 16, 50, tzinfo=UTC)  # 18:50 Kyiv

    def tearDown(self):
        self.tmp.cleanup()

    def watcher(self):
        return dtek.DtekWatcher("http://x", "2.1", self.state, TZ, self.tg.send, self.tg.edit,
                                now=lambda: self.now)

    def run_apply(self, w, document):
        asyncio.run(w.apply(document))

    def test_publish_then_append_changes_to_same_message(self):
        w = self.watcher()
        tomorrow = date(2026, 11, 10)
        self.run_apply(w, doc({tomorrow: hours(h4="no", h5="first", h18="second", h19="no", h20="no")}))
        self.assertEqual(len(self.tg.sent), 1)
        mid = self.tg.sent[0]
        text = self.tg.messages[mid]
        self.assertIn("📅 *Графік на 10.11 (вт)* · черга 2.1", text)
        self.assertIn("🔴 03:00–04:30\n🔴 17:30–20:00\n⏱ Без світла: 4г", text)
        self.assertIn("• 09.11 18:42 — опубліковано: 03:00–04:30, 17:30–20:00", text)

        self.run_apply(w, doc({tomorrow: hours(h4="no", h5="first", h11="no", h18="second", h19="no", h20="no")},
                              update="09.11.2026 21:15"))
        self.run_apply(w, doc({tomorrow: hours(h11="no", h19="no", h20="no")}, update="09.11.2026 23:05"))
        self.assertEqual(len(self.tg.sent), 1)  # still one message
        self.assertEqual(self.tg.edits, [mid, mid])
        text = self.tg.messages[mid]
        self.assertIn("🔴 10:00–11:00\n🔴 18:00–20:00\n⏱ Без світла: 3г", text)
        self.assertIn("• 09.11 21:15 — ➕ 10:00–11:00", text)
        self.assertIn("• 09.11 23:05 — ➖ 03:00–04:30 · 17:30–20:00 → 18:00–20:00", text)
        # history order is preserved
        self.assertLess(text.index("опубліковано"), text.index("21:15"))

    def test_same_message_survives_restart_and_midnight(self):
        w = self.watcher()
        d = date(2026, 11, 10)
        self.run_apply(w, doc({d: hours(h4="no")}))
        mid = self.tg.sent[0]
        self.now = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)  # next day, 10:00 Kyiv
        w2 = self.watcher()  # restart: state from file
        self.run_apply(w2, doc({d: hours(h4="no", h5="no")}, update="10.11.2026 09:58",
                               last_updated="2026-11-10T07:59:00.000Z"))
        self.assertEqual(self.tg.sent, [mid])
        self.assertIn("• 10.11 09:58 — 03:00–04:00 → 03:00–05:00", self.tg.messages[mid])

    def test_past_days_are_kept_untouched(self):
        w = self.watcher()
        d = date(2026, 11, 10)
        self.run_apply(w, doc({d: hours(h4="no")}))
        self.now = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
        self.run_apply(w, doc({d: hours(h4="no", h5="no"), date(2026, 11, 11): hours()},
                              last_updated="2026-11-11T07:59:00.000Z"))
        self.assertEqual(self.tg.edits, [])  # yesterday's message is history now
        self.assertEqual(w.planned(d), (60, 0))  # still available for the report

    def test_day_without_outages_is_not_published_until_it_gets_some(self):
        w = self.watcher()
        d = date(2026, 11, 10)
        self.run_apply(w, doc({d: hours()}))
        self.assertEqual(self.tg.sent, [])
        self.run_apply(w, doc({d: hours(h10="no")}, update="09.11.2026 20:00"))
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("• 09.11 20:00 — опубліковано: 09:00–10:00", self.tg.messages[self.tg.sent[0]])
        self.run_apply(w, doc({d: hours()}, update="09.11.2026 21:00"))
        text = self.tg.messages[self.tg.sent[0]]
        self.assertIn("✅ Відключень не заплановано", text)
        self.assertIn("• 09.11 21:00 — ➖ 09:00–10:00", text)

    def test_deleted_message_is_replaced(self):
        w = self.watcher()
        d = date(2026, 11, 10)
        self.run_apply(w, doc({d: hours(h4="no")}))
        old = self.tg.sent[0]
        del self.tg.messages[old]
        self.run_apply(w, doc({d: hours(h4="no", h5="no")}, update="09.11.2026 19:00"))
        self.assertEqual(len(self.tg.sent), 2)
        new_text = self.tg.messages[self.tg.sent[1]]
        self.assertIn("опубліковано", new_text)
        self.assertIn("• 09.11 19:00 —", new_text)

    def test_transient_edit_error_is_retried(self):
        w = self.watcher()
        d = date(2026, 11, 10)
        self.run_apply(w, doc({d: hours(h4="no")}))
        self.tg.edit_result = "error"
        self.run_apply(w, doc({d: hours(h4="no", h5="no")}, update="09.11.2026 19:00"))
        self.assertEqual(len(self.tg.sent), 1)
        self.tg.edit_result = None
        asyncio.run(w.sync_messages())
        self.assertIn("03:00–05:00", self.tg.messages[self.tg.sent[0]])

    def test_stale_note(self):
        w = self.watcher()
        d = date(2026, 11, 10)
        self.run_apply(w, doc({d: hours(h4="no")}))
        self.assertNotIn("⚠️", self.tg.messages[self.tg.sent[0]])
        self.now += timedelta(hours=3)
        asyncio.run(w.sync_messages())
        self.assertIn("⚠️ Дані могли застаріти (джерело оновлювалось 09.11 18:45)", self.tg.messages[self.tg.sent[0]])

    def test_long_history_is_folded(self):
        w = self.watcher()
        d = date(2026, 11, 10)
        day = dtek.DayState(dtek.slots_from_hours(hours(h4="no")),
                            history=["09.11 18:00 — опубліковано: 03:00–04:00"] +
                                    [f"09.11 18:{i % 60:02d} — " + "x" * 80 for i in range(80)])
        text = w.render(d, day)
        self.assertLessEqual(len(text), dtek.MAX_TEXT)
        self.assertIn("опубліковано", text)
        self.assertIn("… ще ", text)

    def test_missing_group_is_ignored(self):
        w = self.watcher()
        self.run_apply(w, doc({date(2026, 11, 10): hours(h4="no")}, group="GPV9.9"))
        self.assertEqual(self.tg.sent, [])


if __name__ == "__main__":
    unittest.main()
