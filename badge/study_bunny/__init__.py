import json
import time

try:
    import requests
except ImportError:
    import urequests as requests
import wifi

try:
    import secrets
except ImportError:
    secrets = None

API_KEY = "AIzaSyDj1RU2NlVEZxgniTO3KICD50-CVZlMjjw"
DB_URL = "https://study-bunny-default-rtdb.firebaseio.com"
AUTH_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=" + API_KEY
)

STATE_FILE = "/study_bunny.json"
MIN_SESSION_S = 5 * 60
MIN_VALID_YEAR = 2025
TAIL_QUERY = "orderBy=%22%24key%22&limitToLast=20&"
DEFAULT_SUBJECTS = [["Study", 0]]
IDLE_SLEEP_MS = 2 * 60 * 1000


# ---------------------------------------------------------------- time maths
def days_from_civil(y, m, d):
    if m <= 2:
        y -= 1
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def civil_from_days(z):
    z += 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + (3 if mp < 10 else -9)
    if m <= 2:
        y += 1
    return y, m, d


def epoch_from_tuple(t):
    return days_from_civil(t[0], t[1], t[2]) * 86400 + t[3] * 3600 + t[4] * 60 + t[5]


def now_epoch():
    return epoch_from_tuple(rtc.datetime())


def clock_ok():
    return rtc.datetime()[0] >= MIN_VALID_YEAR


def month_key(ms, tz_s):
    y, m, _ = civil_from_days((ms // 1000 + tz_s) // 86400)
    return y, m


def month_start_ms(ms, tz_s):
    y, m = month_key(ms, tz_s)
    return (days_from_civil(y, m, 1) * 86400 - tz_s) * 1000


def tz_seconds():
    try:
        return int(float(getattr(secrets, "TIMEZONE", 0)) * 3600)
    except Exception:
        return 0


def fmt_hm(sec):
    sec = max(0, int(sec))
    return "%02d:%02d" % (sec // 3600, sec // 60 % 60)


def fmt_hms(sec):
    sec = max(0, int(sec))
    return "%02d:%02d:%02d" % (sec // 3600, sec // 60 % 60, sec % 60)


def wrap(text, width):
    lines, line = [], ""
    for word in text.split(" "):
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = word if not line else line + " " + word
    if line:
        lines.append(line)
    return lines


# ------------------------------------------------------- session bookkeeping
class Tracker:
    """Wall-clock stopwatch. Stores epoch seconds so it survives sleep and resets."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.status = "idle"
        self.start = 0
        self.active = 0
        self.seg = 0

    def begin(self, now):
        self.status = "running"
        self.start = now
        self.active = 0
        self.seg = now

    def pause(self, now):
        if self.status == "running":
            self.active += max(0, now - self.seg)
            self.status = "paused"

    def resume(self, now):
        if self.status == "paused":
            self.seg = now
            self.status = "running"

    def elapsed(self, now):
        if self.status == "running":
            return self.active + max(0, now - self.seg)
        if self.status == "paused":
            return self.active
        return 0

    def finish(self, now):
        result = (self.start, self.elapsed(now))
        self.reset()
        return result

    def dump(self):
        return {"s": self.status, "start": self.start, "active": self.active, "seg": self.seg}

    def load(self, d):
        self.status = d.get("s", "idle")
        self.start = d.get("start", 0)
        self.active = d.get("active", 0)
        self.seg = d.get("seg", 0)


# ------------------------------------------------------ Study Bunny data model
def indexed(v):
    """Firebase returns arrays as lists or, when sparse or queried, as dicts."""
    if isinstance(v, list):
        return [(i, x) for i, x in enumerate(v) if x is not None]
    if isinstance(v, dict):
        out = []
        for k, x in v.items():
            try:
                out.append((int(k), x))
            except ValueError:
                pass
        out.sort(key=lambda p: p[0])
        return out
    return []


def find_conflict(blocks, s_ms, e_ms):
    for b in blocks:
        if int(b.get("sessionStarted") or 0) == s_ms and int(b.get("sessionEnded") or 0) == e_ms:
            return "dup"
    for b in blocks:
        bs = int(b.get("sessionStarted") or 0)
        be = int(b.get("sessionEnded") or 0)
        if bs < e_ms and s_ms < be:
            return "overlap"
    return None


def plan_legend(legends, at_ms, tz_s, subject, preferred):
    """Pick a color for `subject` this month and the writes that label it."""
    ym = month_key(at_ms, tz_s)
    items = indexed(legends)
    nxt = items[-1][0] + 1 if items else 0
    for idx, leg in items:
        if month_key(int(leg.get("monthYear") or 0), tz_s) != ym:
            continue
        rows = indexed(leg.get("colorsAndSubjectsOfThisMonth"))
        for _, r in rows:
            if r.get("subject") == subject:
                return int(r.get("color", preferred)), []
        used = set(int(r.get("color", -1)) for _, r in rows)
        color = preferred
        while color in used:
            color += 1
        n = rows[-1][0] + 1 if rows else 0
        path = "monthLegends/%d/colorsAndSubjectsOfThisMonth/%d" % (idx, n)
        return color, [(path, {"color": color, "subject": subject})]
    entry = {
        "monthYear": month_start_ms(at_ms, tz_s),
        "colorsAndSubjectsOfThisMonth": [{"color": preferred, "subject": subject}],
    }
    return preferred, [("monthLegends/%d" % nxt, entry)]


def subjects_from_legends(legends, limit=10):
    items = [leg for _, leg in indexed(legends)]
    items.sort(key=lambda leg: int(leg.get("monthYear") or 0), reverse=True)
    out, seen = [], set()
    for leg in items:
        for _, r in indexed(leg.get("colorsAndSubjectsOfThisMonth")):
            name = r.get("subject")
            if name and name not in seen:
                seen.add(name)
                out.append([name, int(r.get("color", 0))])
    return out[:limit]


def apply_op(root, path, value):
    parts = path.split("/")
    cur = root
    for p in parts[:-1]:
        cur = cur[int(p)] if isinstance(cur, list) else cur[p]
    last = parts[-1]
    if isinstance(cur, list):
        i = int(last)
        if i >= len(cur):
            cur.append(value)
        else:
            cur[i] = value
    else:
        cur[last] = value


# -------------------------------------------------------------------- cloud
class CloudError(Exception):
    pass


class Conflict(CloudError):
    pass


def _err_text(data):
    if isinstance(data, dict):
        e = data.get("error")
        if isinstance(e, dict):
            return str(e.get("message", e))
        if e:
            return str(e)
    return ""


def _call(method, url, body=None):
    fn = getattr(requests, method)
    if body is None:
        r = fn(url)
    else:
        r = fn(url, data=json.dumps(body), headers={"Content-Type": "application/json"})
    try:
        status = r.status_code
        try:
            data = r.json()
        except Exception:
            data = None
    finally:
        r.close()
    if status >= 400:
        raise CloudError("%s %s" % (status, _err_text(data)))
    return data


class Cloud:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.token = None
        self.uid = None

    def login(self):
        if not self.email or not self.password:
            raise CloudError("add STUDYBUNNY_EMAIL and STUDYBUNNY_PASSWORD to secrets.py")
        d = _call(
            "post",
            AUTH_URL,
            {"email": self.email, "password": self.password, "returnSecureToken": True},
        )
        self.token = d["idToken"]
        self.uid = d["localId"]

    def _url(self, path, query=""):
        return "%s/users/%s/gameData%s.json?%sauth=%s" % (
            DB_URL, self.uid, path, query, self.token
        )

    def get(self, path="", query=""):
        return _call("get", self._url(path, query))

    def put(self, path, value):
        return _call("put", self._url(path), value)

    def patch(self, path, value):
        return _call("patch", self._url(path), value)

    def subjects(self):
        if self.token is None:
            self.login()
        return subjects_from_legends(self.get("/stats/monthLegends"))

    def submit(self, s_ms, e_ms, subject, preferred, tz_s):
        """Append one finished session. Returns "ok" or "dup"; raises Conflict/CloudError."""
        if self.token is None:
            self.login()
        if not self.get("", "shallow=true&"):
            raise CloudError("no cloud save yet: press Cloud Save in the app first")
        shape = self.get("/stats", "shallow=true&")
        if isinstance(shape, str):
            return self._submit_string(s_ms, e_ms, subject, preferred, tz_s)

        tail = indexed(self.get("/stats/sessionBlocks", TAIL_QUERY))
        found = find_conflict([b for _, b in tail], s_ms, e_ms)
        if found == "dup":
            return "dup"
        if found:
            raise Conflict("overlaps an existing session")
        nxt = tail[-1][0] + 1 if tail else 0

        color, ops = plan_legend(self.get("/stats/monthLegends"), s_ms, tz_s, subject, preferred)
        for path, value in ops:
            self.put("/stats/" + path, value)
        block = {"color": color, "sessionStarted": s_ms, "sessionEnded": e_ms}
        self.put("/stats/sessionBlocks/%d" % nxt, block)
        back = self.get("/stats/sessionBlocks/%d" % nxt)
        if not back or int(back.get("sessionStarted") or 0) != s_ms:
            raise CloudError("write could not be verified")
        return "ok"

    def _submit_string(self, s_ms, e_ms, subject, preferred, tz_s):
        stats = json.loads(self.get("/stats") or "{}")
        blocks = stats.setdefault("sessionBlocks", [])
        legends = stats.setdefault("monthLegends", [])
        found = find_conflict([b for _, b in indexed(blocks)], s_ms, e_ms)
        if found == "dup":
            return "dup"
        if found:
            raise Conflict("overlaps an existing session")
        color, ops = plan_legend(legends, s_ms, tz_s, subject, preferred)
        for path, value in ops:
            apply_op(stats, path, value)
        blocks.append({"color": color, "sessionStarted": s_ms, "sessionEnded": e_ms})
        blocks.sort(key=lambda b: int(b.get("sessionStarted") or 0))
        self.patch("", {"stats": json.dumps(stats)})
        again = json.loads(self.get("/stats") or "{}")
        if not any(int(b.get("sessionStarted") or 0) == s_ms for b in again.get("sessionBlocks", [])):
            raise CloudError("write could not be verified")
        return "ok"


def connect_wifi(timeout_ms=30000):
    start = time.ticks_ms()
    while not wifi.connect():
        if time.ticks_diff(time.ticks_ms(), start) > timeout_ms:
            return False
        time.sleep(0.2)
    return True


def _conf(name):
    return getattr(secrets, name, None) if secrets else None


# ---------------------------------------------------------------- app state
def _rom(name):
    try:
        return getattr(font, name)
    except (NameError, AttributeError):
        pass
    try:
        return getattr(rom_font, name)
    except (NameError, AttributeError):
        return None


def put_text(msg, x, y, size=1):
    try:
        screen.text(msg, x, y, size)
    except TypeError:
        screen.text(msg, x, y)


def text_width(msg, size=1):
    try:
        return screen.measure_text(msg, size)[0]
    except TypeError:
        return screen.measure_text(msg)[0] * size


def centered(msg, y, size=1, left=0, width=None):
    width = width or screen.width
    put_text(msg, left + (width - text_width(msg, size)) // 2, y, size)


def fill(x, y, w, h):
    screen.rectangle(x, y, w, h)


class App:
    def __init__(self):
        self.tracker = Tracker()
        self.subjects = [list(s) for s in DEFAULT_SUBJECTS]
        self.sel = 0
        self.queue = []
        self.view = None
        self.msg = []
        self.resume_on_back = False
        self.load()

    def load(self):
        try:
            with open(STATE_FILE, "r") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.tracker.load(d.get("t", {}))
        self.subjects = d.get("subjects") or self.subjects
        self.sel = min(d.get("sel", 0), len(self.subjects) - 1)
        self.queue = d.get("queue", [])

    def save(self):
        d = {
            "t": self.tracker.dump(),
            "subjects": self.subjects,
            "sel": self.sel,
            "queue": self.queue,
        }
        with open(STATE_FILE, "w") as f:
            json.dump(d, f)

    @property
    def subject(self):
        return self.subjects[self.sel][0]

    # -------------------------------------------------------------- drawing
    def draw(self):
        ui, big = _rom("nope"), _rom("absolute")
        now = now_epoch()
        t = self.tracker
        screen.pen = color.white
        screen.clear()

        screen.pen = color.black
        fill(0, 0, screen.width, 22)
        screen.pen = color.white
        if ui:
            screen.font = ui
        put_text("STUDY BUNNY", 6, 3, 2)
        local = (now + tz_seconds()) % 86400
        clock = "--:--" if not clock_ok() else fmt_hm(local)
        put_text(clock, screen.width - text_width(clock, 2) - 6, 3, 2)

        screen.pen = color.black
        if self.view == "msg":
            self.draw_message(ui)
            return

        secs = t.elapsed(now)
        if self.view == "confirm":
            label = "STOP?"
        else:
            label = {"idle": "READY", "running": "STUDYING", "paused": "PAUSED"}[t.status]
        centered(label, 32, 2)

        if big:
            screen.font = big
        face = fmt_hm(secs) if t.status == "running" else fmt_hms(secs)
        size = 4
        while size > 1 and text_width(face, size) > screen.width - 8:
            size -= 1
        centered(face, 56, size)

        if ui:
            screen.font = ui
        if self.view == "confirm":
            if secs < MIN_SESSION_S:
                centered("Under 5 min is not recorded", 112, 1)
            else:
                centered("Save %d min of %s?" % (secs // 60, self.subject), 112, 1)
            self.hints("Back", "Drop" if secs >= MIN_SESSION_S else "", "Save" if secs >= MIN_SESSION_S else "Drop")
            return

        subject = self.subject
        if t.status == "idle":
            subject = "< %s >" % subject if len(self.subjects) > 1 else subject
        centered(subject, 108, 2)
        if t.status == "idle" and not clock_ok():
            centered("Clock not set: press C to sync", 132, 1)
        elif self.queue:
            centered("%d session(s) waiting to upload" % len(self.queue), 132, 1)

        if t.status == "idle":
            self.hints("Start", "", "Sync")
        elif t.status == "running":
            self.hints("Pause", "", "Stop")
        else:
            self.hints("Resume", "", "Stop")

    def hints(self, a, b, c):
        screen.pen = color.black
        fill(0, 148, screen.width, 1)
        third = screen.width // 3
        for i, label in enumerate((a, b, c)):
            if label:
                centered(label, 156, 2, left=i * third, width=third)

    def draw_message(self, ui):
        y = 40
        for line in self.msg:
            for part in wrap(line, 20):
                centered(part, y, 2)
                y += 22
        centered("press any button", 156, 1)

    def notice(self, *lines):
        self.view = "msg"
        self.msg = list(lines)
        self.draw()
        badge.update()

    def say(self, *lines):
        self.view = "msg"
        self.msg = list(lines)

    # ---------------------------------------------------------------- input
    def handle(self, btns):
        if not btns:
            return
        t = self.tracker
        now = now_epoch()

        if self.view == "msg":
            self.view = None
            return

        if self.view == "confirm":
            if BUTTON_A in btns:
                if self.resume_on_back:
                    t.resume(now)
                self.view = None
                self.save()
            elif BUTTON_B in btns:
                self.discard()
            elif BUTTON_C in btns:
                self.finish()
            return

        if t.status == "idle":
            if BUTTON_UP in btns:
                self.sel = (self.sel - 1) % len(self.subjects)
                self.save()
            elif BUTTON_DOWN in btns:
                self.sel = (self.sel + 1) % len(self.subjects)
                self.save()
            elif BUTTON_A in btns:
                if clock_ok():
                    t.begin(now)
                    self.save()
                else:
                    self.say("Clock not set", "Press C to sync")
            elif BUTTON_C in btns:
                self.sync()
            return

        if BUTTON_A in btns:
            if t.status == "running":
                t.pause(now)
            else:
                t.resume(now)
            self.save()
        elif BUTTON_C in btns:
            self.resume_on_back = t.status == "running"
            t.pause(now)
            self.view = "confirm"
            self.save()

    def discard(self):
        self.tracker.reset()
        self.view = None
        self.save()

    def finish(self):
        t = self.tracker
        start, total = t.finish(now_epoch())
        self.view = None
        if total < MIN_SESSION_S:
            self.save()
            self.say("Under 5 minutes", "not recorded")
            return
        name, col = self.subjects[self.sel]
        self.queue.append([start * 1000, (start + total) * 1000, name, col])
        self.save()
        self.notice("Saving...", "%d min %s" % (total // 60, name))
        self.say(*self.flush(sync_subjects=False))

    # ---------------------------------------------------------------- cloud
    def sync(self):
        self.notice("Syncing...")
        self.say(*self.flush(sync_subjects=True, ntp=True))

    def flush(self, sync_subjects, ntp=False):
        lines = []
        try:
            if not connect_wifi():
                return self.offline("No Wi-Fi")
            if ntp:
                try:
                    rtc.time_from_ntp()
                except Exception:
                    lines.append("NTP failed")
            cloud = Cloud(_conf("STUDYBUNNY_EMAIL"), _conf("STUDYBUNNY_PASSWORD"))
            saved = 0
            while self.queue:
                s_ms, e_ms, name, col = self.queue[0]
                try:
                    cloud.submit(s_ms, e_ms, name, col, tz_seconds())
                    saved += 1
                except Conflict as e:
                    lines.append("Dropped: %s" % e)
                self.queue.pop(0)
                self.save()
            if saved:
                lines.insert(0, "Uploaded %d" % saved)
            if sync_subjects:
                found = cloud.subjects()
                if found:
                    self.subjects = found
                    self.sel = min(self.sel, len(found) - 1)
                    self.save()
                    lines.append("%d subjects" % len(found))
            return lines or ["Up to date"]
        except Exception as e:
            return self.offline(str(e)[:60])
        finally:
            try:
                wifi.disconnect()
            except Exception:
                pass

    def offline(self, why):
        n = len(self.queue)
        return [why, "%d queued" % n if n else "nothing queued", "C on home screen retries"]


app = None


def update():
    global app
    if app is None:
        app = App()
    app.draw()
    badge.update()

    t = app.tracker
    if t.status == "running" and app.view is None:
        rtc.set_alarm(minutes=1)
        wait_for_button_or_alarm(timeout=None)
    else:
        wait_for_button_or_alarm(timeout=IDLE_SLEEP_MS)
    rtc.clear_alarm()

    btns = [b for b in (BUTTON_A, BUTTON_B, BUTTON_C, BUTTON_UP, BUTTON_DOWN) if badge.pressed(b)]
    app.handle(btns)


def on_exit():
    rtc.clear_alarm()


try:
    badge.mode(FAST_UPDATE)
except NameError:
    pass

run(update)
