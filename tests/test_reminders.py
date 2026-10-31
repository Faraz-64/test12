import os, sys, tempfile, threading, time, unittest
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reminders as R

NOW = datetime(2026, 10, 24, 10, 0, 0)


class ParseTests(unittest.TestCase):
    def test_relative_reminders(self):
        r = R.parse("Remind me in 10 minutes to stretch", NOW)
        self.assertEqual((r["kind"], r["text"], r["due"]), ("reminder", "stretch", NOW + timedelta(minutes=10)))
        r = R.parse("remind me to drink water in 2 hours", NOW)
        self.assertEqual((r["text"], r["due"]), ("drink water", NOW + timedelta(hours=2)))
        self.assertEqual(R.parse("remind me in 30 seconds to blink", NOW)["due"], NOW + timedelta(seconds=30))
        self.assertEqual(R.parse("remind me in 1.5 hours to call mum", NOW)["due"], NOW + timedelta(minutes=90))

    def test_timers(self):
        r = R.parse("Set a 25-minute timer", NOW)
        self.assertEqual((r["kind"], r["due"]), ("timer", NOW + timedelta(minutes=25)))
        self.assertEqual(R.parse("set a timer for 5 minutes", NOW)["due"], NOW + timedelta(minutes=5))
        r = R.parse("set a 10 min timer for tea", NOW)
        self.assertEqual((r["text"], r["due"]), ("tea", NOW + timedelta(minutes=10)))
        self.assertEqual(R.parse("timer 90 seconds", NOW)["due"], NOW + timedelta(seconds=90))

    def test_clock_times(self):
        self.assertEqual(R.parse("remind me at 5pm to eat", NOW)["due"], datetime(2026, 10, 24, 17, 0))
        self.assertEqual(R.parse("remind me at 9am to jog", NOW)["due"], datetime(2026, 10, 25, 9, 0))      # already past today -> tomorrow
        self.assertEqual(R.parse("remind me to submit the form at 17:30", NOW)["due"], datetime(2026, 10, 24, 17, 30))
        self.assertEqual(R.parse("remind me tomorrow at 8am to pack", NOW)["due"], datetime(2026, 10, 25, 8, 0))
        self.assertEqual(R.parse("remind me at 12am to sleep", NOW)["due"], datetime(2026, 10, 25, 0, 0))

    def test_invalid_numbers_raise(self):
        for bad in ["remind me in 0 minutes to x", "set a 0 minute timer", "remind me in 9999 days to x",
                    "remind me at 25:00 to x", "remind me at 13pm to x", "remind me at 10:75 to x"]:
            with self.assertRaises(R.ReminderError, msg=bad):
                R.parse(bad, NOW)

    def test_not_a_reminder(self):
        for t in ["what time is it", "remind me", "tell me a joke", "set the volume to 5", "I need a reminder app"]:
            self.assertIsNone(R.parse(t, NOW), t)

    def test_management_phrases(self):
        self.assertEqual(R.parse_management("list my reminders"), ("list",))
        self.assertEqual(R.parse_management("show reminders"), ("list",))
        self.assertEqual(R.parse_management("cancel reminder 3"), ("cancel", 3))
        self.assertEqual(R.parse_management("delete timer #2"), ("cancel", 2))
        self.assertEqual(R.parse_management("done with reminder 4"), ("done", 4))   # the hint shown in the listing must work
        self.assertEqual(R.parse_management("I'm done with timer 2"), ("done", 2))
        self.assertEqual(R.parse_management("mark reminder 4 as done"), ("done", 4))
        self.assertEqual(R.parse_management("complete reminder 1"), ("done", 1))
        self.assertIsNone(R.parse_management("hello"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "reminders.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_reminders_survive_a_restart(self):
        s1 = R.ReminderStore(self.path)
        a = s1.add("reminder", "stretch", NOW + timedelta(minutes=10), NOW)
        s1.add("timer", "tea", NOW + timedelta(minutes=5), NOW)
        s2 = R.ReminderStore(self.path)                       # simulates closing and reopening Jarvis
        self.assertEqual([i["text"] for i in s2.active()], ["tea", "stretch"])
        self.assertEqual(s2.add("reminder", "later", NOW + timedelta(hours=1), NOW)["id"], 3)   # ids keep counting

    def test_fire_once_only_even_across_restarts(self):
        s = R.ReminderStore(self.path)
        s.add("reminder", "stretch", NOW + timedelta(minutes=10), NOW)
        self.assertEqual(s.take_due(NOW + timedelta(minutes=9)), [])
        fired = s.take_due(NOW + timedelta(minutes=10, seconds=1))
        self.assertEqual([f["text"] for f in fired], ["stretch"])
        self.assertEqual(s.take_due(NOW + timedelta(hours=1)), [])
        self.assertEqual(R.ReminderStore(self.path).take_due(NOW + timedelta(hours=1)), [])       # a restart doesn't repeat it

    def test_missed_while_closed_is_flagged_late(self):
        s = R.ReminderStore(self.path)
        s.add("reminder", "stretch", NOW + timedelta(minutes=10), NOW)
        fired = R.ReminderStore(self.path).take_due(NOW + timedelta(hours=3))                    # reopened 3 hours later
        self.assertTrue(fired[0]["late"])
        on_time = R.ReminderStore(Path(self.dir.name) / "b.json")
        on_time.add("reminder", "x", NOW + timedelta(seconds=5), NOW)
        self.assertFalse(on_time.take_due(NOW + timedelta(seconds=6))[0]["late"])

    def test_cancel_and_complete(self):
        s = R.ReminderStore(self.path)
        a = s.add("reminder", "a", NOW + timedelta(minutes=1), NOW)["id"]
        b = s.add("reminder", "b", NOW + timedelta(minutes=2), NOW)["id"]
        self.assertEqual(s.cancel(a)["status"], "cancelled")
        self.assertEqual([i["id"] for i in s.active()], [b])
        s.take_due(NOW + timedelta(minutes=5))                 # b fires but stays listed until you clear it
        self.assertEqual([i["status"] for i in s.active()], ["fired"])
        self.assertEqual(s.complete(b)["status"], "done")
        self.assertEqual(s.active(), [])
        for bad in (a, b, 99):                                 # already cancelled/done/unknown
            with self.assertRaises(R.ReminderError):
                s.cancel(bad)

    def test_corrupt_file_is_kept_not_overwritten(self):
        self.path.write_text("{ nope", encoding="utf-8")
        s = R.ReminderStore(self.path)
        self.assertIn("unreadable", s.warning)
        self.assertEqual(s.active(), [])
        self.assertEqual(len(list(Path(self.dir.name).glob("reminders.corrupt-*"))), 1)

    def test_describe(self):
        s = R.ReminderStore(self.path)
        item = s.add("timer", "tea", NOW + timedelta(minutes=5), NOW)
        self.assertIn("in 5m 00s", R.describe(item, NOW))
        fired = s.take_due(NOW + timedelta(hours=2))[0]
        self.assertIn("while Jarvis was closed", R.describe(fired, NOW + timedelta(hours=2)))


class SchedulerTests(unittest.TestCase):
    def test_check_once_notifies_and_survives_a_broken_notifier(self):
        with tempfile.TemporaryDirectory() as d:
            store = R.ReminderStore(Path(d) / "r.json")
            store.add("reminder", "x", NOW + timedelta(seconds=1), NOW)
            seen = []
            def bad_notify(item):
                seen.append(item["text"]); raise RuntimeError("boom")
            sched = R.ReminderScheduler(store, bad_notify, clock=lambda: NOW + timedelta(seconds=5))
            self.assertEqual(len(sched.check_once()), 1)
            self.assertEqual(seen, ["x"])

    def test_background_thread_fires_and_shuts_down_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            store = R.ReminderStore(Path(d) / "r.json")
            store.add("timer", "soon", datetime.now() + timedelta(seconds=0.3))
            got = threading.Event()
            sched = R.ReminderScheduler(store, lambda item: got.set(), poll_seconds=0.05)
            sched.start()
            self.assertTrue(got.wait(3), "the reminder never fired")
            self.assertTrue(sched.stop(), "scheduler thread did not stop")
            self.assertFalse(sched.thread.is_alive())
            sched.start(); self.assertTrue(sched.stop())      # can restart and stop again


if __name__ == "__main__":
    unittest.main()
