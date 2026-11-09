"""Unit tests for time accounting and formatting. Run: python -m unittest discover -s tests"""
import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")
os.environ.setdefault("ROUTER_IP", "127.0.0.1")
os.environ.setdefault("ROUTER_2_PORT", "1")
os.environ["TIMEZONE"] = "Europe/Kyiv"
os.environ["REPORT_HOUR"] = "9"
os.environ["DEBOUNCE_COUNT"] = "6"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import power_monitor as pm  # noqa: E402
from power_monitor import InternetState, PowerState  # noqa: E402

UTC = timezone.utc


def kyiv(y, m, d, h=0, mi=0, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=pm.TZ).astimezone(UTC)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "stats.json")

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self, now, power=PowerState.ON, net=InternetState.ONLINE):
        m = pm.StatsManager(self.path, now=now)
        m.set_initial(power, now)
        m.set_initial(net, now)
        m.last_tick = now
        return m

    @staticmethod
    def run_ticks(m, start, end, step=13.7):
        t = start
        while t < end:
            t = min(t + timedelta(seconds=step), end)
            m.advance(t)


class DayBoundaries(Base):
    def test_day_is_exactly_24h_and_closes_at_local_midnight(self):
        start = kyiv(2026, 11, 10, 0, 0, 0)
        m = self.manager(start)
        self.run_ticks(m, start, kyiv(2026, 11, 11, 0, 5))  # loop period does not divide evenly
        daily = [p for p in m.pending if p.kind == "daily"]
        self.assertEqual(len(daily), 1)
        p = daily[0]
        self.assertEqual(p.first_day, date(2026, 11, 10))
        self.assertAlmostEqual(p.durations["POWER_ON"], 86400, places=3)
        self.assertAlmostEqual(p.durations["NET_ONLINE"], 86400, places=3)
        self.assertEqual(m.daily.first_day, date(2026, 11, 11))
        self.assertAlmostEqual(m.daily.durations["POWER_ON"], 300, places=3)
        text = pm.create_report(p)
        self.assertIn("10.11 (вт)", text)
        self.assertIn("*Світло було:* 24г (100.0%)", text)
        self.assertIn("*Світла не було:* 0с (0.0%)", text)

    def test_dst_spring_day_is_23h_and_autumn_day_is_25h(self):
        for day, expected in ((date(2027, 3, 28), 23 * 3600), (date(2026, 10, 25), 25 * 3600)):
            start = kyiv(day.year, day.month, day.day)
            m = self.manager(start)
            self.run_ticks(m, start, start + timedelta(hours=26), step=60)
            p = [p for p in m.pending if p.kind == "daily"][0]
            self.assertEqual(p.length, expected)
            self.assertAlmostEqual(p.durations["POWER_ON"], expected, places=3)

    def test_report_due_at_09_local_in_summer_and_winter(self):
        summer = pm.Period("daily", date(2026, 7, 1))
        winter = pm.Period("daily", date(2026, 12, 1))
        self.assertEqual(summer.report_due(), datetime(2026, 7, 2, 6, 0, tzinfo=UTC))
        self.assertEqual(winter.report_due(), datetime(2026, 12, 2, 7, 0, tzinfo=UTC))
        # Report right after the autumn DST switch day
        dst = pm.Period("daily", date(2026, 10, 25))
        self.assertEqual(dst.report_due(), datetime(2026, 10, 26, 7, 0, tzinfo=UTC))

    def test_week_closes_on_monday_midnight(self):
        start = kyiv(2026, 11, 9)  # Monday
        m = self.manager(start)
        self.run_ticks(m, start, kyiv(2026, 11, 16, 0, 1), step=600)
        weekly = [p for p in m.pending if p.kind == "weekly"]
        self.assertEqual(len(weekly), 1)
        self.assertAlmostEqual(weekly[0].durations["POWER_ON"], 7 * 86400, places=3)
        self.assertIn("09.11 — 15.11", pm.create_report(weekly[0]))
        self.assertEqual(weekly[0].report_due(), kyiv(2026, 11, 16, 9))


class Reports(Base):
    def test_missed_reports_are_kept_until_sent(self):
        start = kyiv(2026, 11, 15, 12)  # Sunday
        m = self.manager(start)
        m.save()
        # Monitor is down from Sunday noon until Tuesday 10:00.
        m2 = pm.StatsManager(self.path, now=kyiv(2026, 11, 17, 10))
        m2.advance(kyiv(2026, 11, 17, 10))
        due = m2.due_reports(kyiv(2026, 11, 17, 10))
        kinds = [(p.kind, p.first_day) for p in due]
        self.assertEqual(kinds, [("daily", date(2026, 11, 15)), ("weekly", date(2026, 11, 9)),
                                 ("daily", date(2026, 11, 16))])
        # Downtime credited to the last known state -> full days.
        self.assertAlmostEqual(due[2].durations["POWER_ON"], 86400, places=3)

    def test_next_report_wakeup(self):
        m = self.manager(kyiv(2026, 11, 10, 23, 59))
        m.advance(kyiv(2026, 11, 11, 0, 0, 5))
        self.assertEqual(m.next_report_at(kyiv(2026, 11, 11, 0, 0, 5)), kyiv(2026, 11, 11, 9))
        self.assertEqual(m.due_reports(kyiv(2026, 11, 11, 8, 59, 59)), [])
        self.assertEqual(len(m.due_reports(kyiv(2026, 11, 11, 9))), 1)

    def test_first_run_fills_period_with_initial_state(self):
        start = kyiv(2026, 11, 10, 15)
        m = self.manager(start, power=PowerState.OFF)
        self.run_ticks(m, start, kyiv(2026, 11, 11, 0, 1), step=60)
        p = [p for p in m.pending if p.kind == "daily"][0]
        d = p.report_durations()
        self.assertAlmostEqual(d["POWER_OFF"], 86400, places=3)
        self.assertIn("*Світла не було:* 24г (100.0%)", pm.create_report(p))


class Debounce(Base):
    def test_change_is_dated_to_first_observation(self):
        start = kyiv(2026, 11, 10, 10)
        m = self.manager(start)
        db = pm.Debouncer()
        t = start
        confirmed = None
        for _ in range(6):
            t += timedelta(seconds=12)
            m.advance(t)
            confirmed = db.observe(PowerState.OFF, m.power_state, t) or confirmed
        self.assertEqual(confirmed, start + timedelta(seconds=12))
        m.change(PowerState.OFF, confirmed)
        self.assertAlmostEqual(m.daily.durations["POWER_ON"], 12, places=3)
        self.assertAlmostEqual(m.daily.durations["POWER_OFF"], 60, places=3)
        self.assertEqual(m.daily.counts["POWER_OFF"], 1)
        self.assertEqual(m.last_power_change, confirmed)

    def test_flapping_resets_candidate(self):
        db = pm.Debouncer()
        t = kyiv(2026, 11, 10)
        for i in range(5):
            self.assertIsNone(db.observe(PowerState.OFF, PowerState.ON, t + timedelta(seconds=i)))
        self.assertIsNone(db.observe(PowerState.ON, PowerState.ON, t + timedelta(seconds=6)))
        self.assertEqual(db.count, 0)

    def test_change_across_midnight_counts_in_previous_day(self):
        start = kyiv(2026, 11, 10, 23, 59, 30)
        m = self.manager(start)
        m.advance(kyiv(2026, 11, 11, 0, 0, 40))
        m.change(PowerState.OFF, kyiv(2026, 11, 10, 23, 59, 50))
        prev = [p for p in m.pending if p.kind == "daily"][0]
        self.assertAlmostEqual(prev.durations["POWER_ON"], 20, places=3)
        self.assertAlmostEqual(prev.durations["POWER_OFF"], 10, places=3)
        self.assertEqual(prev.counts["POWER_OFF"], 1)
        self.assertAlmostEqual(m.daily.durations["POWER_OFF"], 40, places=3)


class Persistence(Base):
    def test_roundtrip(self):
        start = kyiv(2026, 11, 10, 10)
        m = self.manager(start)
        self.run_ticks(m, start, kyiv(2026, 11, 11, 1))
        m.save()
        m2 = pm.StatsManager(self.path, now=kyiv(2026, 11, 11, 1))
        self.assertEqual(m2.power_state, PowerState.ON)
        self.assertEqual(m2.daily.first_day, date(2026, 11, 11))
        self.assertEqual(len(m2.pending), 1)
        self.assertEqual(m2.last_tick, kyiv(2026, 11, 11, 1))

    def test_v1_migration(self):
        v1 = {
            "daily": {"durations": {"POWER_ON": 3600.0, "POWER_OFF": 1800.0, "NET_ONLINE": 5400.0,
                                    "NET_OFFLINE": 0.0},
                      "counts": {"POWER_ON": 1, "POWER_OFF": 1, "NET_ONLINE": 0, "NET_OFFLINE": 0},
                      "start_date": "2026-11-10T00:00:08.228242", "end_date": None},
            "weekly": {"durations": {"POWER_ON": 90000.0, "POWER_OFF": 1800.0, "NET_ONLINE": 91800.0,
                                     "NET_OFFLINE": 0.0},
                       "counts": {"POWER_ON": 1, "POWER_OFF": 1, "NET_ONLINE": 0, "NET_OFFLINE": 0},
                       "start_date": "2026-11-09T00:00:05", "end_date": None},
            "last_daily_report_day": "2026-11-10T00:00:00", "last_weekly_report_week": 46,
            "pending_daily_report": None, "pending_weekly_report": None,
            "current_power_state": "POWER_OFF", "current_internet_state": "NET_ONLINE",
            "last_power_change": "2026-11-10T01:00:08", "last_internet_change": "2026-11-09T12:00:00",
        }
        Path(self.path).write_text(json.dumps(v1))
        m = pm.StatsManager(self.path, now=kyiv(2026, 11, 10, 4))
        self.assertEqual(m.power_state, PowerState.OFF)
        self.assertEqual(m.daily.first_day, date(2026, 11, 10))
        self.assertEqual(m.weekly.first_day, date(2026, 11, 9))
        self.assertEqual(m.last_power_change, datetime(2026, 11, 10, 1, 0, 8, tzinfo=UTC))
        self.assertEqual(m.last_tick, datetime(2026, 11, 10, 1, 30, 8, 228242, tzinfo=UTC))
        self.assertAlmostEqual(m.daily.durations["POWER_ON"], 3600)


class Logging(unittest.TestCase):
    def test_http_client_request_urls_are_not_logged(self):
        with tempfile.TemporaryDirectory() as d:
            pm.LOG_FILE = os.path.join(d, "pm.log")
            root = logging.getLogger()
            before = list(root.handlers)
            try:
                pm.setup_logging()
                self.assertEqual(logging.getLogger("httpx").getEffectiveLevel(), logging.WARNING)
                self.assertEqual(logging.getLogger("httpcore").getEffectiveLevel(), logging.WARNING)
            finally:
                for h in root.handlers[:]:
                    if h not in before:
                        root.removeHandler(h)
                        h.close()


class QuietHours(unittest.TestCase):
    def test_wraps_midnight(self):
        self.assertTrue(pm.is_quiet(kyiv(2026, 11, 10, 22, 0), "22-8"))
        self.assertTrue(pm.is_quiet(kyiv(2026, 11, 10, 3, 0), "22-8"))
        self.assertTrue(pm.is_quiet(kyiv(2026, 11, 10, 7, 59), "22-8"))
        self.assertFalse(pm.is_quiet(kyiv(2026, 11, 10, 8, 0), "22-8"))
        self.assertFalse(pm.is_quiet(kyiv(2026, 11, 10, 21, 59), "22-8"))
        self.assertTrue(pm.is_quiet(kyiv(2026, 11, 10, 13, 0), "12-14"))
        self.assertFalse(pm.is_quiet(kyiv(2026, 11, 10, 3, 0), ""))


class PlanVsFact(Base):
    def test_planned_line_in_daily_report(self):
        start = kyiv(2026, 11, 10)
        m = self.manager(start)
        self.run_ticks(m, start, kyiv(2026, 11, 11, 0, 1), step=600)
        p = [p for p in m.pending if p.kind == "daily"][0]
        text = pm.create_report(p, planned=(450, 60))
        self.assertIn("🔌 *Світла не було:* 0с (0.0%)\n🗓 За графіком без світла: 7г 30хв (+ можливо 1г)\n", text)
        self.assertNotIn("🗓", pm.create_report(p))


class Formatting(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(pm.format_duration(0), "0с")
        self.assertEqual(pm.format_duration(59.6), "1хв")
        self.assertEqual(pm.format_duration(86400), "24г")
        self.assertEqual(pm.format_duration(90061), "25г 1хв 1с")
        self.assertEqual(pm.format_duration(7 * 86400), "7д")
        self.assertEqual(pm.format_duration(7 * 86400 - 278), "6д 23г 55хв 22с")

    def test_percentages(self):
        self.assertEqual(pm.format_pct(278, 604800), "<0.1%")
        self.assertEqual(pm.format_pct(604800 - 278, 604800), ">99.9%")
        self.assertEqual(pm.format_pct(0, 86400), "0.0%")
        self.assertEqual(pm.format_pct(86400, 86400), "100.0%")

    def test_messages(self):
        self.assertEqual(pm.internet_message(InternetState.OFFLINE, PowerState.OFF, None),
                         "📵 Інтернет зник (світла немає)")
        self.assertEqual(pm.internet_message(InternetState.OFFLINE, PowerState.ON, 120),
                         "📵 Інтернет зник (світло є)\n\n⏱ Інтернет був: 2хв")
        self.assertEqual(pm.internet_message(InternetState.ONLINE, None, 61),
                         "📶 Інтернет зʼявився\n\n⏱ Інтернету не було: 1хв 1с")
        self.assertEqual(pm.power_message(PowerState.ON, 3600), "🟢 Світло зʼявилось\n\n⏱ Світла не було: 1г")


if __name__ == "__main__":
    unittest.main()
