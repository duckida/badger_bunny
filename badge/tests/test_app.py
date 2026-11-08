import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import fakes  # noqa: E402

T0 = 1_762_600_000  # 2025-11-08 ~11:46 UTC
MS = 1000


def game(blocks=None, legends=None, as_string=False):
    stats = {
        "sessionBlocks": blocks if blocks is not None else [],
        "monthLegends": legends if legends is not None else [],
    }
    return {"coins": 77, "carrots": 3, "ownedItems": [1, 2], "stats": json.dumps(stats) if as_string else stats}


def legend(month_ms, rows):
    return {"monthYear": month_ms, "colorsAndSubjectsOfThisMonth": [{"color": c, "subject": s} for c, s in rows]}


NOV = 1761955200000  # 2025-11-01 UTC in ms


class Base(unittest.TestCase):
    def boot(self, gd, epoch=T0, **kw):
        self.db = fakes.FakeDB(gd)
        self.mod, self.clock, self.pad, self.drawn = fakes.install(self.db, epoch, **kw)
        self.mod.STATE_FILE = os.path.join(self._tmp(), "state.json")
        self.app = self.mod.App()
        return self.app

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        return d

    def press(self, *btns):
        self.app.handle(list(btns))

    def stats(self):
        return self.db.root["users"][fakes.UID]["gameData"]["stats"]

    def writes(self):
        return [e for e in self.db.log if e[0] in ("put", "patch")]


class TestTimeMaths(Base):
    def test_epoch_matches_python(self):
        import calendar
        self.boot(game())
        for t in (0, 86399, 951782400, T0, 4102444800):
            import datetime as dt
            d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            self.assertEqual(self.mod.epoch_from_tuple(d.timetuple()[:6]), calendar.timegm(d.timetuple()))

    def test_month_start(self):
        self.boot(game())
        self.assertEqual(self.mod.month_start_ms(T0 * MS, 0), NOV)
        self.assertEqual(self.mod.month_start_ms(NOV - 1, 0), NOV - 31 * 86400 * MS)

    def test_month_start_honours_timezone(self):
        self.boot(game())
        # 00:30 UTC on Nov 1 is still Oct 31 in UTC-5
        ms = NOV + 30 * 60 * MS
        self.assertEqual(self.mod.month_start_ms(ms, -5 * 3600), NOV - 31 * 86400 * MS + 5 * 3600 * MS)


class TestTracker(Base):
    def test_pause_excludes_paused_time(self):
        self.boot(game())
        t = self.mod.Tracker()
        t.begin(1000)
        t.pause(1600)
        self.assertEqual(t.elapsed(5000), 600)
        t.resume(5000)
        self.assertEqual(t.elapsed(5400), 1000)
        self.assertEqual(t.finish(5400), (1000, 1000))
        self.assertEqual(t.status, "idle")

    def test_survives_dump_load(self):
        self.boot(game())
        t = self.mod.Tracker()
        t.begin(100)
        u = self.mod.Tracker()
        u.load(json.loads(json.dumps(t.dump())))
        self.assertEqual(u.elapsed(700), 600)


class TestSubmit(Base):
    def cloud(self):
        return self.mod.Cloud("me@x.com", "pw")

    def test_appends_block_and_only_touches_new_paths(self):
        old = {"color": 1, "sessionStarted": (T0 - 9000) * MS, "sessionEnded": (T0 - 7200) * MS}
        self.boot(game([old], [legend(NOV, [(1, "Math")])]))
        before = copy.deepcopy(self.db.root)
        res = self.cloud().submit((T0 - 3600) * MS, T0 * MS, "Math", 1, 0)
        self.assertEqual(res, "ok")
        blocks = self.stats()["sessionBlocks"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[1], {"color": 1, "sessionStarted": (T0 - 3600) * MS, "sessionEnded": T0 * MS})
        gd = self.db.root["users"][fakes.UID]["gameData"]
        self.assertEqual((gd["coins"], gd["carrots"], gd["ownedItems"]), (77, 3, [1, 2]))
        self.assertEqual(blocks[0], before["users"][fakes.UID]["gameData"]["stats"]["sessionBlocks"][0])
        for method, path, _ in self.writes():
            self.assertTrue(path.endswith("sessionBlocks/1"), path)

    def test_never_replaces_whole_blob(self):
        self.boot(game())
        self.cloud().submit((T0 - 3600) * MS, T0 * MS, "Math", 2, 0)
        for method, path, _ in self.writes():
            self.assertIn("/stats/", path + "/")
            self.assertNotEqual(path, "users/%s/gameData" % fakes.UID)

    def test_overlap_refused_and_nothing_written(self):
        b = {"color": 1, "sessionStarted": (T0 - 3600) * MS, "sessionEnded": T0 * MS}
        self.boot(game([b]))
        with self.assertRaises(self.mod.Conflict):
            self.cloud().submit((T0 - 1800) * MS, (T0 + 1800) * MS, "Math", 1, 0)
        self.assertEqual(self.writes(), [])

    def test_resubmit_is_idempotent(self):
        self.boot(game())
        c = self.cloud()
        self.assertEqual(c.submit((T0 - 3600) * MS, T0 * MS, "Math", 1, 0), "ok")
        self.assertEqual(c.submit((T0 - 3600) * MS, T0 * MS, "Math", 1, 0), "dup")
        self.assertEqual(len(self.stats()["sessionBlocks"]), 1)

    def test_new_subject_gets_unused_color_in_existing_month(self):
        self.boot(game([], [legend(NOV, [(0, "Math"), (1, "Art")])]))
        self.cloud().submit((T0 - 3600) * MS, T0 * MS, "Bio", 0, 0)
        rows = self.stats()["monthLegends"][0]["colorsAndSubjectsOfThisMonth"]
        self.assertEqual(rows[-1], {"color": 2, "subject": "Bio"})
        self.assertEqual(self.stats()["sessionBlocks"][0]["color"], 2)

    def test_new_month_creates_legend(self):
        self.boot(game([], [legend(NOV - 40 * 86400 * MS, [(3, "Math")])]))
        self.cloud().submit((T0 - 3600) * MS, T0 * MS, "Math", 3, 0)
        legs = self.stats()["monthLegends"]
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[1]["monthYear"], NOV)

    def test_empty_stats_node(self):
        self.boot({"coins": 1, "stats": {"monthLegends": [], "sessionBlocks": []}})
        self.assertEqual(self.cloud().submit((T0 - 3600) * MS, T0 * MS, "Math", 0, 0), "ok")

    def test_string_encoded_stats_stays_string(self):
        self.boot(game([], [legend(NOV, [(1, "Math")])], as_string=True))
        self.cloud().submit((T0 - 3600) * MS, T0 * MS, "Math", 1, 0)
        raw = self.db.root["users"][fakes.UID]["gameData"]["stats"]
        self.assertIsInstance(raw, str)
        self.assertEqual(len(json.loads(raw)["sessionBlocks"]), 1)
        self.assertEqual(self.db.root["users"][fakes.UID]["gameData"]["coins"], 77)

    def test_no_cloud_save_is_refused_without_writing(self):
        self.boot(None)
        with self.assertRaises(self.mod.CloudError):
            self.cloud().submit((T0 - 3600) * MS, T0 * MS, "Math", 1, 0)
        self.assertEqual(self.writes(), [])

    def test_bad_password(self):
        self.boot(game())
        with self.assertRaises(self.mod.CloudError) as cm:
            self.mod.Cloud("me@x.com", "nope").login()
        self.assertIn("INVALID_LOGIN", str(cm.exception))

    def test_only_tail_is_fetched_for_large_history(self):
        blocks = [{"color": 0, "sessionStarted": (T0 - 900000 + i * 1000) * MS,
                   "sessionEnded": (T0 - 900000 + i * 1000 + 600) * MS} for i in range(400)]
        self.boot(game(blocks))
        self.cloud().submit((T0 - 3600) * MS, T0 * MS, "Math", 0, 0)
        self.assertEqual(len(self.stats()["sessionBlocks"]), 401)
        full = [e for e in self.db.log if e[0] == "get" and e[1].endswith("sessionBlocks")]
        self.assertTrue(full, "tail query expected")

    def test_subjects_listed_newest_month_first(self):
        self.boot(game([], [legend(NOV - 40 * 86400 * MS, [(0, "Old")]), legend(NOV, [(1, "Math"), (2, "Art")])]))
        self.assertEqual(self.cloud().subjects(), [["Math", 1], ["Art", 2], ["Old", 0]])


class TestFlow(Base):
    def run_session(self, minutes, pause_min=0):
        a = self.app
        self.press("A")
        self.assertEqual(a.tracker.status, "running")
        self.clock.epoch += minutes * 30
        self.press("A")
        self.assertEqual(a.tracker.status, "paused")
        self.clock.epoch += pause_min * 60
        self.press("A")
        self.clock.epoch += minutes * 30
        self.press("C")
        self.assertEqual(a.view, "confirm")
        self.press("C")

    def test_start_pause_stop_uploads(self):
        a = self.boot(game([], [legend(NOV, [])]), secrets_attrs={})
        a.subjects = [["Math", 1]]
        self.run_session(40, pause_min=15)
        blocks = self.stats()["sessionBlocks"]
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertEqual((b["sessionEnded"] - b["sessionStarted"]) // 60000, 40)
        self.assertEqual(a.queue, [])
        self.assertEqual(a.tracker.status, "idle")

    def test_short_session_not_recorded(self):
        a = self.boot(game())
        self.press("A")
        self.clock.epoch += 120
        self.press("C")
        self.press("C")
        self.assertEqual(self.writes(), [])
        self.assertEqual(a.queue, [])

    def test_confirm_back_resumes_running_session(self):
        a = self.boot(game())
        self.press("A")
        self.clock.epoch += 600
        self.press("C")
        self.clock.epoch += 100  # time on the confirm screen must not count
        self.press("A")
        self.assertEqual(a.tracker.status, "running")
        self.clock.epoch += 60
        self.assertEqual(a.tracker.elapsed(self.mod.now_epoch()), 660)

    def test_discard(self):
        a = self.boot(game())
        self.press("A")
        self.clock.epoch += 900
        self.press("C")
        self.press("B")
        self.assertEqual(a.tracker.status, "idle")
        self.assertEqual(self.writes(), [])
        self.assertEqual(a.queue, [])

    def test_offline_queues_then_retries(self):
        a = self.boot(game([], [legend(NOV, [(0, "Study")])]), down=True)
        self.press("A")
        self.clock.epoch += 1800
        self.press("C")
        self.press("C")
        self.assertEqual(len(a.queue), 1)
        self.assertIn("queued", " ".join(a.msg))
        # restart with network back: queue survives a reset
        self.db = fakes.FakeDB(game([], [legend(NOV, [(0, "Study")])]))
        sys.modules["requests"].__dict__.update(fakes.make_requests(self.db).__dict__)
        b = self.mod.App()
        self.assertEqual(len(b.queue), 1)
        self.app = b
        b.view = None
        self.press("C")
        self.assertEqual(b.queue, [])
        self.assertEqual(len(self.stats()["sessionBlocks"]), 1)

    def test_conflict_is_dropped_not_retried_forever(self):
        blk = {"color": 0, "sessionStarted": (T0 - 600) * MS, "sessionEnded": (T0 + 600) * MS}
        a = self.boot(game([blk]))
        a.queue = [[(T0 - 60) * MS, (T0 + 1200) * MS, "Study", 0]]
        self.press("C")
        self.assertEqual(a.queue, [])
        self.assertIn("overlaps", " ".join(a.msg))

    def test_transient_failure_keeps_queue(self):
        a = self.boot(game(), down=True)
        a.queue = [[(T0 - 3600) * MS, T0 * MS, "Study", 0]]
        self.press("C")
        self.assertEqual(len(a.queue), 1)

    def test_clock_unset_blocks_start(self):
        a = self.boot(game(), epoch=946684800)  # year 2000
        self.press("A")
        self.assertEqual(a.tracker.status, "idle")
        self.assertIn("Clock not set", a.msg)

    def test_state_persists_across_reset_mid_session(self):
        a = self.boot(game())
        self.press("A")
        self.clock.epoch += 300
        b = self.mod.App()
        self.assertEqual(b.tracker.status, "running")
        self.assertEqual(b.tracker.elapsed(self.mod.now_epoch()), 300)

    def test_subject_cycling(self):
        a = self.boot(game())
        a.subjects = [["A", 0], ["B", 1], ["C", 2]]
        self.press("DOWN")
        self.press("DOWN")
        self.assertEqual(a.subject, "C")
        self.press("DOWN")
        self.assertEqual(a.subject, "A")
        self.press("UP")
        self.assertEqual(a.subject, "C")

    def test_draw_all_views_without_error(self):
        a = self.boot(game())
        a.draw()
        self.press("A")
        a.draw()
        self.press("A")
        a.draw()
        self.press("C")
        a.draw()
        a.say("hello world this is long", "x")
        a.draw()


if __name__ == "__main__":
    unittest.main()
