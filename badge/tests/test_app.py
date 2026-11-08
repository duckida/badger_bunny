import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import fakes  # noqa: E402

T0 = 1_762_600_000  # 2025-11-08 ~11:46 UTC
TICKS_EPOCH = 621355968000000000
TICK = 10 ** 7
NOV = 1761955200  # 2025-11-01 00:00 UTC


def tk(seconds):
    """Unix seconds -> .NET ticks, as the free save stores them."""
    return TICKS_EPOCH + int(seconds) * TICK


def game(blocks=None, legends=None):
    stats = {
        "sessionBlocks": blocks if blocks is not None else [],
        "monthLegends": legends if legends is not None else [],
    }
    return {
        "coins": 77,
        "carrots": 3,
        "redeemedRefPrize": 0,
        "refUserRedeemed": 0,
        "playerowneditems": '{"ownedList":[]}',
        "todo": "{}",
        "musicowned": "{}",
        "savedtimeandusesleft": 5,
        "flashcards": "{}",
        "stats": json.dumps(stats),
    }


def legend(month_s, rows):
    return {
        "monthYear": tk(month_s),
        "colorsAndSubjectsOfThisMonth": [
            {"color": c, "subject": s} for c, s in rows
        ],
    }


class Base(unittest.TestCase):
    def boot(self, blob, epoch=T0, **kw):
        self.db = fakes.FakeDB(blob)
        self.mod, self.clock, self.pad, self.drawn = fakes.install(self.db, epoch, **kw)
        self.mod.STATE_FILE = os.path.join(self._tmp(), "state.json")
        self.mod.BACKUP_FILE = os.path.join(self._tmp(), "backup.json")
        self.app = self.mod.App()
        return self.app

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        return d

    def press(self, *btns):
        self.app.handle(list(btns))

    def blob(self):
        return json.loads(self.db.blob)

    def stats(self):
        return json.loads(self.blob()["stats"])

    def saves(self):
        return [e for e in self.db.log if e[0] == "save"]


class TestTimeMaths(Base):
    def test_epoch_matches_python(self):
        import calendar
        self.boot(game())
        for t in (0, 86399, 951782400, T0, 4102444800):
            import datetime as dt
            d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            self.assertEqual(
                self.mod.epoch_from_tuple(d.timetuple()[:6]),
                calendar.timegm(d.timetuple()),
            )

    def test_ticks_round_trip(self):
        self.boot(game())
        for s in (0, T0, T0 + 12345):
            self.assertEqual(self.mod.from_ticks(self.mod.to_ticks(s)), s)
            self.assertEqual(self.mod.to_ticks(s), tk(s))

    def test_ticks_to_local_month(self):
        self.boot(game())
        # 2025-11-01 00:30 local
        self.assertEqual(self.mod.local_ym(NOV + 1800), (2025, 11))
        self.assertEqual(self.mod.local_ym(NOV - 1), (2025, 10))


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
        return self.mod.FreeCloud(fakes.USERNAME)

    def test_appends_block_in_ticks_and_keeps_everything_else(self):
        old = {"color": 1, "sessionStarted": tk(T0 - 9000), "sessionEnded": tk(T0 - 7200)}
        blob = game([old], [legend(NOV + 3600, [(1, "Math")])])
        self.boot(blob)
        before = copy.deepcopy(self.blob())
        self.assertEqual(self.cloud().submit(T0 - 3600, T0, "Math", 0), "ok")
        blocks = self.stats()["sessionBlocks"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual(
            blocks[1],
            {"color": 1, "sessionStarted": tk(T0 - 3600), "sessionEnded": tk(T0)},
        )
        after = self.blob()
        self.assertEqual(after["stats"] and json.loads(after["stats"])["sessionBlocks"][0], old)
        for key in before:
            if key != "stats":
                self.assertEqual(after[key], before[key], key)
        self.assertEqual(self.saves(), [("save", fakes.USERNAME)])

    def test_stats_stays_a_string(self):
        self.boot(game([], [legend(NOV + 3600, [(0, "Math")])]))
        self.cloud().submit(T0 - 3600, T0, "Math", 0)
        self.assertIsInstance(self.blob()["stats"], str)

    def test_overlap_refused_and_nothing_saved(self):
        b = {"color": 1, "sessionStarted": tk(T0 - 3600), "sessionEnded": tk(T0)}
        self.boot(game([b]))
        with self.assertRaises(self.mod.Conflict):
            self.cloud().submit(T0 - 1800, T0 + 1800, "Math", 0)
        self.assertEqual(self.saves(), [])

    def test_resubmit_is_idempotent(self):
        self.boot(game())
        c = self.cloud()
        self.assertEqual(c.submit(T0 - 3600, T0, "Math", 0), "ok")
        self.assertEqual(c.submit(T0 - 3600, T0, "Math", 0), "dup")
        self.assertEqual(len(self.stats()["sessionBlocks"]), 1)

    def test_subject_reuses_this_months_color(self):
        self.boot(game([], [legend(NOV + 3600, [(0, "Math"), (1, "Art")])]))
        self.cloud().submit(T0 - 3600, T0, "Art", 0)
        self.assertEqual(self.stats()["sessionBlocks"][0]["color"], 1)
        rows = self.stats()["monthLegends"][0]["colorsAndSubjectsOfThisMonth"]
        self.assertEqual(self.stats()["monthLegends"][0]["monthYear"], tk(NOV + 3600))
        self.assertEqual(len(rows), 2)

    def test_new_subject_gets_unused_color(self):
        self.boot(game([], [legend(NOV + 3600, [(0, "Math"), (1, "Art")])]))
        self.cloud().submit(T0 - 3600, T0, "Bio", 0)
        rows = self.stats()["monthLegends"][0]["colorsAndSubjectsOfThisMonth"]
        self.assertEqual(rows[-1], {"color": 2, "subject": "Bio"})
        self.assertEqual(self.stats()["sessionBlocks"][0]["color"], 2)

    def test_new_month_creates_legend(self):
        self.boot(game([], [legend(NOV - 20 * 86400, [(3, "Math")])]))
        self.cloud().submit(T0 - 3600, T0, "Math", 0)
        legs = self.stats()["monthLegends"]
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[1]["monthYear"], tk(T0))
        self.assertEqual(legs[1]["colorsAndSubjectsOfThisMonth"], [{"color": 0, "subject": "Math"}])

    def test_trailing_space_in_subject_is_preserved(self):
        self.boot(game([], [legend(NOV + 3600, [(8, "Physics ")])]))
        self.cloud().submit(T0 - 3600, T0, "Physics ", 0)
        rows = self.stats()["monthLegends"][0]["colorsAndSubjectsOfThisMonth"]
        self.assertEqual(rows[0]["subject"], "Physics ")
        self.assertEqual(self.stats()["sessionBlocks"][0]["color"], 8)

    def test_older_session_is_inserted_in_order(self):
        newer = {"color": 0, "sessionStarted": tk(T0), "sessionEnded": tk(T0 + 600)}
        self.boot(game([newer], [legend(NOV + 3600, [(0, "Math")])]))
        self.cloud().submit(T0 - 7200, T0 - 3600, "Math", 0)
        starts = [b["sessionStarted"] for b in self.stats()["sessionBlocks"]]
        self.assertEqual(starts, [tk(T0 - 7200), tk(T0)])

    def test_large_history_still_works(self):
        blocks = [
            {"color": 0, "sessionStarted": tk(T0 - 628 * 3600 + i * 60),
             "sessionEnded": tk(T0 - 628 * 3600 + i * 60 + 30)}
            for i in range(628)
        ]
        self.boot(game(blocks, [legend(NOV + 3600, [(0, "Math")])]))
        self.cloud().submit(T0 - 3600, T0, "Math", 0)
        self.assertEqual(len(self.stats()["sessionBlocks"]), 629)
        self.assertEqual(self.blob()["coins"], 77)

    def test_no_save_is_refused(self):
        self.boot(None)
        with self.assertRaises(self.mod.CloudError) as cm:
            self.cloud().submit(T0 - 3600, T0, "Math", 0)
        self.assertIn("No save", str(cm.exception))
        self.assertEqual(self.saves(), [])

    def test_missing_username_says_where_to_put_it(self):
        self.boot(game(), secrets_attrs={"STUDYBUNNY_USERNAME": ""})
        with self.assertRaises(self.mod.CloudError) as cm:
            self.mod.FreeCloud("").submit(T0 - 3600, T0, "Math", 0)
        self.assertIn("STUDYBUNNY_USERNAME", str(cm.exception))

    def test_server_rejecting_save_is_reported(self):
        self.boot(game())

        class NoSave(fakes.FakeDB):
            def handle(self, body):
                if b"action=save" in body:
                    return "?"
                return super().handle(body)

        self.db.__class__ = NoSave
        with self.assertRaises(self.mod.CloudError) as cm:
            self.cloud().submit(T0 - 3600, T0, "Math", 0)
        self.assertIn("rejected", str(cm.exception))

    def test_save_changed_in_the_app_between_read_and_write(self):
        class Racy(fakes.FakeDB):
            loads = 0

            def handle(self, body):
                if b"action=load" in body:
                    self.loads += 1
                    if self.loads == 2:
                        d = json.loads(self.blob)
                        d["coins"] = 999
                        self.blob = json.dumps(d)
                return super().handle(body)

        self.boot(game())
        self.db.__class__ = Racy
        with self.assertRaises(self.mod.Conflict) as cm:
            self.cloud().submit(T0 - 3600, T0, "Math", 0)
        self.assertIn("changed", str(cm.exception))
        self.assertEqual(self.saves(), [])

    def test_backup_written_before_save(self):
        self.boot(game())
        before = self.db.blob
        self.cloud().submit(T0 - 3600, T0, "Math", 0)
        with open(self.mod.BACKUP_FILE) as f:
            self.assertEqual(f.read(), before)

    def test_subjects_come_from_newest_month(self):
        self.boot(game([], [
            legend(NOV - 40 * 86400, [(0, "Old")]),
            legend(NOV + 3600, [(1, "Math"), (2, "Art")]),
        ]))
        self.assertEqual(self.cloud().subjects(), [["Math", 1], ["Art", 2]])


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
        a = self.boot(game([], [legend(NOV + 3600, [(0, "Math")])]))
        a.subjects = [["Math", 0]]
        self.run_session(40, pause_min=15)
        blocks = self.stats()["sessionBlocks"]
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertEqual((b["sessionEnded"] - b["sessionStarted"]) // TICK // 60, 40)
        self.assertEqual(a.queue, [])
        self.assertEqual(a.tracker.status, "idle")

    def test_short_session_not_recorded(self):
        a = self.boot(game())
        self.press("A")
        self.clock.epoch += 120
        self.press("C")
        self.press("C")
        self.assertEqual(self.saves(), [])
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
        self.assertEqual(self.saves(), [])
        self.assertEqual(a.queue, [])

    def test_offline_queues_then_retries(self):
        blob = game([], [legend(NOV + 3600, [(0, "Study")])])
        a = self.boot(blob, down=True)
        self.press("A")
        self.clock.epoch += 1800
        self.press("C")
        self.press("C")
        self.assertEqual(len(a.queue), 1)
        self.assertIn("queued", " ".join(a.msg))
        # restart with network back: queue survives a reset
        self.db = fakes.FakeDB(blob)
        sys.modules["requests"].__dict__.update(fakes.make_requests(self.db).__dict__)
        b = self.mod.App()
        self.assertEqual(len(b.queue), 1)
        self.app = b
        b.view = None
        self.press("C")
        self.assertEqual(b.queue, [])
        self.assertEqual(len(self.stats()["sessionBlocks"]), 1)

    def test_conflict_is_dropped_not_retried_forever(self):
        blk = {"color": 0, "sessionStarted": tk(T0 - 600), "sessionEnded": tk(T0 + 600)}
        a = self.boot(game([blk]))
        a.queue = [[T0 - 60, T0 + 1200, "Study"]]
        self.press("C")
        self.assertEqual(a.queue, [])
        self.assertIn("overlaps", " ".join(a.msg))

    def test_transient_failure_keeps_queue(self):
        a = self.boot(game(), down=True)
        a.queue = [[T0 - 3600, T0, "Study"]]
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

    def test_subject_can_be_changed_on_stop_screen_before_upload(self):
        a = self.boot(game([], [legend(NOV + 3600, [(0, "Math"), (1, "Art")])]))
        a.subjects = [["Math", 0], ["Art", 1]]
        self.press("A")
        self.clock.epoch += 1800
        self.press("C")
        self.assertEqual(a.view, "confirm")
        self.press("DOWN")
        self.assertEqual(a.subject, "Art")
        self.assertEqual(a.view, "confirm")
        self.press("C")
        rows = self.stats()["monthLegends"][0]["colorsAndSubjectsOfThisMonth"]
        self.assertEqual([r["subject"] for r in rows], ["Math", "Art"])
        self.assertEqual(self.stats()["sessionBlocks"][0]["color"], 1)

    def test_sync_loads_subjects_from_the_save(self):
        a = self.boot(game([], [legend(NOV + 3600, [(0, "Math"), (1, "Art")])]))
        self.press("C")
        self.assertEqual([s[0] for s in a.subjects], ["Math", "Art"])
        self.assertIn("2 subjects", " ".join(a.msg))

    def test_sync_says_so_when_there_is_no_save(self):
        self.boot(None)
        self.press("C")
        self.assertIn("No save", " ".join(self.app.msg))
        self.assertNotIn("Up to date", " ".join(self.app.msg))

    def test_running_timer_wakes_every_10_seconds(self):
        a = self.boot(game())
        self.press("A")
        self.mod.app = a
        self.clock.epoch += 3
        self.mod.update()
        self.assertEqual(self.clock.alarms[-1], {"seconds": 7})
        self.clock.epoch += 7
        self.mod.update()
        self.assertEqual(self.clock.alarms[-1], {"seconds": 10})

    def test_displayed_time_ticks_in_10_second_steps(self):
        a = self.boot(game())
        self.press("A")
        seen = []
        for _ in range(3):
            self.clock.epoch += 10
            a.draw()
            seen.append([m for m in self.drawn if m.count(":") == 2][0])
        self.assertEqual(seen, ["00:00:10", "00:00:20", "00:00:30"])

    def test_bunny_sprite_is_drawn_for_each_mood(self):
        a = self.boot(game())
        a.draw()
        self.press("A")
        a.draw()
        self.press("A")
        a.draw()
        names = [p.rsplit("bunny_", 1)[1] for p in self.drawn.loads]
        self.assertEqual(names, ["neutral.png", "happy.png", "sleep.png"])
        self.assertEqual(len(self.drawn.blits), 3)

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


class TestFeedback(Base):
    def cloud(self):
        return self.mod.FreeCloud(fakes.USERNAME)

    def test_post_passes_a_timeout(self):
        self.boot(game([], [legend(NOV + 3600, [(0, "Math")])]))
        self.cloud().subjects()
        self.assertEqual(
            sys.modules["requests"].calls["timeouts"], [self.mod.POST_TIMEOUT_S]
        )

    def test_post_falls_back_when_requests_has_no_timeout_kwarg(self):
        self.boot(game([], [legend(NOV + 3600, [(0, "Math")])]))
        req = sys.modules["requests"]
        real = req.post

        def post(url, data=None, headers=None):
            return real(url, data=data, headers=headers)

        req.post = post
        self.assertEqual(self.cloud().subjects(), [["Math", 0]])
        self.assertEqual(self.db.log, [("load", fakes.USERNAME)])

    def test_sync_shows_each_phase_and_the_result(self):
        a = self.boot(game([], [legend(NOV + 3600, [(0, "Math")])]))
        a.queue = [[T0 - 3600, T0, "Math"]]
        seen = []
        a.notice = lambda *lines: seen.extend(lines)
        self.press("C")
        for phase in ("Wi-Fi", "Clock", "Loading save",
                      "Checking for changes", "Uploading", "Verifying", "Subjects"):
            self.assertIn(phase, seen)
        self.assertIn("Uploaded 1", " ".join(a.msg))

    def test_debug_flag_prints_to_the_console(self):
        import contextlib
        import io
        self.boot(game(), secrets_attrs={"STUDYBUNNY_DEBUG": True})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.cloud().subjects()
        out = buf.getvalue()
        self.assertIn("[study_bunny]", out)
        self.assertIn("load", out)

    def test_silent_without_the_debug_flag(self):
        import contextlib
        import io
        self.boot(game())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.cloud().subjects()
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
