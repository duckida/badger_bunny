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

try:
    import gc
except ImportError:  # CPython under test
    gc = None

FREE_URL = "https://www.superbyte.site/account.php"
STATE_FILE = "/study_bunny.json"
BACKUP_FILE = "/study_bunny_backup.json"
MIN_SESSION_S = 5 * 60
MIN_VALID_YEAR = 2025
DEFAULT_SUBJECTS = [["Study", 0]]
IDLE_SLEEP_MS = 2 * 60 * 1000
TICK_S = 10
BUNNY_W = 86
NO_SAVE = "No save for this username. Press Cloud Save in the app first"
POST_TIMEOUT_S = 20

# With STUDYBUNNY_DEBUG = True in secrets.py, log() prints each phase, every
# request and every error to the badge's serial console, prefixed so it is easy
# to tell apart from the system's own output.
DEBUG = bool(secrets and getattr(secrets, "STUDYBUNNY_DEBUG", False))


def log(msg):
    if DEBUG:
        try:
            print("[study_bunny]", msg)
        except Exception:
            pass


_LAP = 0


def lap(label):
    """Log a step and how many ms the previous one took (debug builds only)."""
    global _LAP
    if not DEBUG:
        return
    now = time.ticks_ms()
    log("%s (%d ms)" % (label, time.ticks_diff(now, _LAP) if _LAP else 0))
    _LAP = now


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
# The free save stores .NET DateTime ticks: 100 ns units of *local* wall-clock
# time counted from 0001-01-01. Unix epoch is 621355968000000000 ticks.
#
# The blob is parsed whole with json.loads and rebuilt with json.dumps. Both
# are C-level and finish in well under a second even for a 60 KB history,
# while a Python-level text scan costs about a millisecond *per character* on
# the badge (a 60 KB scan looks like a hang). The badge has ~8 MB of heap, so
# holding the parsed save is no problem. json.dumps re-orders keys (MicroPython
# dicts are unordered) and drops the original formatting; the app's parser does
# not care, and every value round-trips exactly.
TICKS_EPOCH = 621355968000000000


class CloudError(Exception):
    pass


class Conflict(CloudError):
    pass


def to_ticks(local_s):
    return TICKS_EPOCH + int(local_s) * 10000000


def from_ticks(t):
    return (int(t) - TICKS_EPOCH) // 10000000


def local_ym(local_s):
    y, m, _ = civil_from_days(local_s // 86400)
    return y, m


def parse_save(raw):
    """The whole save blob as a dict."""
    try:
        d = json.loads(raw)
    except ValueError:
        raise CloudError("save is not valid JSON")
    if not isinstance(d, dict):
        raise CloudError("save has an odd shape")
    return d


def parse_stats(stats):
    """The stats object as a dict."""
    try:
        st = json.loads(stats)
    except ValueError:
        raise CloudError("stats is not valid JSON")
    if not isinstance(st, dict):
        raise CloudError("stats has an odd shape")
    return st


def stats_of(raw):
    """The stats JSON *string* out of a whole save blob."""
    stats = parse_save(raw).get("stats")
    if stats is None:
        raise CloudError("save has no stats")
    return stats if isinstance(stats, str) else json.dumps(stats, separators=(",", ":"))


def blocks_of(st):
    blocks = st.get("sessionBlocks")
    if blocks is None:
        raise CloudError("save has no sessionBlocks")
    return blocks


def find_conflict(blocks, s_ticks, e_ticks):
    for b in blocks:
        if b.get("sessionStarted") == s_ticks and b.get("sessionEnded") == e_ticks:
            return "dup"
    for b in blocks:
        bs, be = b.get("sessionStarted"), b.get("sessionEnded")
        if bs is not None and be is not None and bs < e_ticks and s_ticks < be:
            return "overlap"
    return None


def add_block(st, s_ticks, e_ticks, color):
    """Insert the session block in time order. Mutates the stats dict."""
    block = {"color": color, "sessionStarted": s_ticks, "sessionEnded": e_ticks}
    blocks = blocks_of(st)
    at = len(blocks)
    for i, b in enumerate(blocks):
        if (b.get("sessionStarted") or 0) > s_ticks:
            at = i
            break
    blocks.insert(at, block)
    return block


def plan_colour(st, local_s, subject):
    """Colour for this subject this month, added to the legend if it is new.

    Mutates the stats dict's monthLegends in place and returns the colour.
    """
    legends = st.get("monthLegends")
    if legends is None:
        legends = st["monthLegends"] = []
    y, m = local_ym(local_s)
    hit = None
    for leg in legends:
        my = leg.get("monthYear") or 0
        if my >= TICKS_EPOCH and local_ym(from_ticks(my)) == (y, m):
            hit = leg
            break
    if hit is None:
        color = 0
        legends.append({
            "monthYear": to_ticks(local_s),
            "colorsAndSubjectsOfThisMonth": [{"color": color, "subject": subject}],
        })
    else:
        rows = hit.get("colorsAndSubjectsOfThisMonth") or []
        color = None
        for row in rows:
            if row.get("subject") == subject:
                color = int(row.get("color") or 0)
                break
        if color is None:
            used = [int(r.get("color") or 0) for r in rows]
            color = 0
            while color in used and color < 15:
                color += 1
            rows.append({"color": color, "subject": subject})
            hit["colorsAndSubjectsOfThisMonth"] = rows
    return color


def subjects_from_stats(st):
    """[name, colour] pairs from the newest month that has a legend."""
    best, best_m = None, -1
    for leg in st.get("monthLegends") or []:
        my = leg.get("monthYear") or 0
        if my > best_m:
            best, best_m = leg, my
    out = []
    for row in (best or {}).get("colorsAndSubjectsOfThisMonth") or []:
        name = row.get("subject")
        if name:
            out.append([name, int(row.get("color") or 0)])
    return out[:12]


HEX = b"0123456789ABCDEF"


def urlencode(s):
    # Fast path: a pure-ASCII string needs only a handful of escapes, and
    # str.replace is C-level. The per-character loop below costs about a
    # millisecond per character on the badge, i.e. minutes for a save blob.
    if len(s.encode()) == len(s):
        return (s.replace("%", "%25")
                .replace("&", "%26")
                .replace("+", "%2B")
                .replace("=", "%3D")
                .replace("#", "%23")
                .replace(" ", "%20")
                .replace("\n", "%0A")
                .replace("\r", "%0D")
                .replace("\t", "%09")
                .encode())
    out = bytearray()
    for ch in s:
        o = ord(ch)
        if o < 33 or o > 126 or ch in "%&+=#":
            for b in ch.encode("utf-8"):
                out.append(37)
                out.append(HEX[b >> 4])
                out.append(HEX[b & 15])
        else:
            out.append(o)
    return bytes(out)


# -------------------------------------------------------------------- cloud
def _post(fields):
    body = b"&".join(
        key.encode() + b"=" + urlencode(value) for key, value in fields.items()
    )
    log("%s: sending %d bytes" % (fields.get("action", "?"), len(body)))
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        r = requests.post(FREE_URL, data=body, headers=headers, timeout=POST_TIMEOUT_S)
    except TypeError:  # this requests has no timeout argument
        r = requests.post(FREE_URL, data=body, headers=headers)
    try:
        status = r.status_code
        text = r.text
    finally:
        r.close()
    log("reply HTTP %d, %d chars" % (status, len(text or "")))
    if status >= 400:
        raise CloudError("HTTP %d from the save server" % status)
    return (text or "").strip()


class FreeCloud:
    """The app's free cloud save: a whole JSON blob kept on superbyte.site."""

    def __init__(self, username):
        self.username = username

    def _user(self):
        if not self.username:
            raise CloudError("add STUDYBUNNY_USERNAME to secrets.py")
        return self.username

    def load(self):
        text = _post({"action": "load", "username": self._user()})
        if text == "notfound":
            raise CloudError(NO_SAVE)
        if not text.startswith("{"):
            raise CloudError("odd reply from the save server")
        return text

    def save(self, blob):
        text = _post({"action": "save", "username": self._user(), "data": blob})
        if text != "saved":
            raise CloudError("save rejected: %s" % text[:40])

    def submit(self, start_utc, end_utc, subject, tz_s, progress=None):
        """Append one session. Returns "ok" or "dup"; raises Conflict/CloudError."""
        stage = progress if progress else log
        stage("Loading save")
        raw = self.load()
        lap("save loaded, %d chars" % len(raw))
        d = parse_save(raw)
        stats = d.get("stats")
        if stats is None:
            raise CloudError("save has no stats")
        st = parse_stats(stats if isinstance(stats, str) else json.dumps(stats))
        lap("stats parsed")
        s_local, e_local = start_utc + tz_s, end_utc + tz_s
        s_ticks, e_ticks = to_ticks(s_local), to_ticks(e_local)
        blocks = blocks_of(st)
        log("%d sessions in save" % len(blocks))
        found = find_conflict(blocks, s_ticks, e_ticks)
        if found == "dup":
            return "dup"
        if found:
            raise Conflict("overlaps a session already in your save")
        color = plan_colour(st, e_local, subject)
        log("colour %d for %s this month" % (color, subject.strip() or "?"))
        add_block(st, s_ticks, e_ticks, color)
        d["stats"] = json.dumps(st, separators=(",", ":"))
        blob = json.dumps(d, separators=(",", ":"))
        lap("edited, %d chars" % len(blob))
        backup(raw)
        if gc:
            gc.collect()
        # Someone using the app in the meantime would be overwritten by us.
        # Comparing the reloaded text with the one we based the edit on is
        # exact, and C-level: no hashing loop over the blob.
        stage("Checking for changes")
        if self.load() != raw:
            raise Conflict("save changed in the app; try Sync again")
        stage("Uploading")
        self.save(blob)
        stage("Verifying")
        back = parse_stats(stats_of(self.load()))
        for b in blocks_of(back):
            if b.get("sessionStarted") == s_ticks and b.get("sessionEnded") == e_ticks:
                lap("verified")
                return "ok"
        raise CloudError("write could not be verified")

    def subjects(self):
        return subjects_from_stats(parse_stats(stats_of(self.load())))


def backup(text):
    try:
        with open(BACKUP_FILE, "w") as f:
            f.write(text)
        log("backup written, %d chars" % len(text))
    except OSError as e:
        # The badge's filesystem can be read-only, e.g. while its flash is
        # mounted on a computer. Losing the backup is survivable.
        log("backup failed: %s" % e)


def connect_wifi(timeout_ms=30000):
    start = time.ticks_ms()
    while True:
        try:
            ok = wifi.connect()
        except OSError as e:
            # After a soft reboot (Thonny's Ctrl-D) the CYW43 radio can be
            # left half-initialised and refuses every call until the badge
            # is power-cycled. Retrying cannot fix that, so say so plainly.
            if "EPERM" in str(e):
                log("wifi chip stuck: %s" % e)
                raise CloudError("Wi-Fi chip stuck: power-cycle the badge, then Sync")
            log("wifi connect error: %s" % e)
            ok = False
        if ok:
            log("wifi: connected")
            return True
        if time.ticks_diff(time.ticks_ms(), start) > timeout_ms:
            log("wifi: gave up after %d ms" % timeout_ms)
            return False
        time.sleep(0.2)


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
        self.images = {}
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
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(d, f)
        except OSError as e:
            # A read-only or full filesystem must not kill the app.
            log("state write failed: %s" % e)

    @property
    def subject(self):
        return self.subjects[self.sel][0]

    # -------------------------------------------------------------- drawing
    def mood(self):
        if self.view == "confirm":
            return "happy" if self.tracker.elapsed(now_epoch()) >= MIN_SESSION_S else "frown"
        return {"idle": "neutral", "running": "happy", "paused": "sleep"}[self.tracker.status]

    def bunny(self, mood):
        if mood not in self.images:
            self.images = {}
            for base in ("", "/system/apps/study_bunny/"):
                try:
                    self.images[mood] = image.load("%sassets/bunny_%s.png" % (base, mood))
                    break
                except Exception:
                    pass
            else:
                self.images[mood] = None
        return self.images[mood]

    def backdrop(self):
        try:
            screen.pen = brush.pattern(color.white, color.dark_grey, 12)
        except Exception:
            screen.pen = color.white
        screen.clear()

    def panel(self, x, y, w, h):
        screen.pen = color.white
        screen.shape(shape.rounded_rectangle(x, y, w, h, 5))
        screen.pen = color.black
        screen.shape(shape.rounded_rectangle(x, y, w, h, 5).stroke(2))

    def draw_bunny(self, mood):
        img = self.bunny(mood)
        x, y = (screen.width - BUNNY_W) // 2, 6
        if img is not None:
            screen.blit(img, vec2(x, y))
            return
        screen.pen = color.dark_grey
        screen.shape(shape.ellipse(x + 20, y + 25, 9, 24))
        screen.shape(shape.ellipse(x + 66, y + 25, 9, 24))
        screen.shape(shape.ellipse(x + 43, y + 88, 42, 42))
        screen.pen = color.black
        screen.shape(shape.circle(x + 30, y + 70, 3))
        screen.shape(shape.circle(x + 56, y + 70, 3))

    def fit(self, msg, width, size):
        while size > 1 and text_width(msg, size) > width:
            size -= 1
        while len(msg) > 1 and text_width(msg, size) > width:
            msg = msg[:-1]
        return msg, size

    def draw(self):
        ui, big = _rom("nope"), _rom("absolute")
        now = now_epoch()
        t = self.tracker
        self.backdrop()
        screen.pen = color.black
        if ui:
            screen.font = ui
        if self.view == "msg":
            self.draw_message()
            return

        self.draw_bunny(self.mood())
        secs = t.elapsed(now)

        # status and clock, top left
        if self.view == "confirm":
            label = "STOP?"
        else:
            label = {"idle": "READY", "running": "STUDYING", "paused": "PAUSED"}[t.status]
        self.panel(4, 6, 80, 40)
        screen.pen = color.black
        label, size = self.fit(label, 72, 2)
        centered(label, 11, size, left=4, width=80)
        clock = "--:--" if not clock_ok() else fmt_hm((now + tz_seconds()) % 86400)
        centered(clock, 29, 1, left=4, width=80)

        # subject, top right
        multi = len(self.subjects) > 1
        shown = self.subject.strip()
        name = "< %s >" % shown if multi else shown
        self.panel(180, 6, 80, 40)
        screen.pen = color.black
        centered("SUBJECT", 11, 1, left=180, width=80)
        name, size = self.fit(name, 72, 2)
        centered(name, 25, size, left=180, width=80)

        # timer, over the bunny's tummy
        self.panel(52, 108, 160, 36)
        screen.pen = color.black
        if big:
            screen.font = big
        face = fmt_hms((secs + TICK_S // 2) // TICK_S * TICK_S) if t.status == "running" else fmt_hms(secs)
        face, size = self.fit(face, 150, 3)
        centered(face, 114, size, left=52, width=160)
        if ui:
            screen.font = ui

        # hint bar
        screen.pen = color.white
        fill(0, 150, screen.width, 26)
        screen.pen = color.black
        fill(0, 150, screen.width, 2)
        if self.view == "confirm":
            if secs < MIN_SESSION_S:
                self.hints("Back", "", "Drop")
            else:
                self.hints("Back", "Drop", "Save")
        elif t.status == "idle":
            self.hints("Start", "", "Sync")
        elif t.status == "running":
            self.hints("Pause", "", "Stop")
        else:
            self.hints("Resume", "", "Stop")

    def hints(self, a, b, c):
        screen.pen = color.black
        third = screen.width // 3
        for i, label in enumerate((a, b, c)):
            if label:
                centered(label, 158, 2, left=i * third, width=third)

    def draw_message(self):
        lines = []
        for line in self.msg:
            lines.extend(wrap(line, 20))
        h = 22 * len(lines) + 16
        y = max(4, (150 - h) // 2)
        self.panel(12, y, screen.width - 24, h)
        screen.pen = color.black
        y += 10
        for line in lines:
            centered(line, y, 2)
            y += 22
        centered("press any button", 158, 1)

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

        if BUTTON_UP in btns or BUTTON_DOWN in btns:
            step = -1 if BUTTON_UP in btns else 1
            self.sel = (self.sel + step) % len(self.subjects)
            self.save()
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
            if BUTTON_A in btns:
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
        name = self.subject
        self.queue.append([start, start + total, name])
        self.save()
        self.notice("Saving...", "%d min %s" % (total // 60, name.strip()))
        self.say(*self.flush(sync_subjects=False, progress=self.notice))

    # ---------------------------------------------------------------- cloud
    def sync(self):
        self.notice("Syncing...")
        self.say(*self.flush(sync_subjects=True, ntp=True, progress=self.notice))

    def flush(self, sync_subjects, ntp=False, progress=None):
        stage = progress if progress else log
        lines = []
        try:
            stage("Wi-Fi")
            if not connect_wifi():
                return self.offline("No Wi-Fi")
            if ntp:
                stage("Clock")
                try:
                    rtc.time_from_ntp()
                except Exception:
                    log("ntp failed")
                    lines.append("NTP failed")
            cloud = FreeCloud(_conf("STUDYBUNNY_USERNAME"))
            saved = 0
            while self.queue:
                start, end, name = self.queue[0][:3]
                if start > 100000000000:  # queue written by an older version
                    start, end = start // 1000, end // 1000
                try:
                    cloud.submit(start, end, name, tz_seconds(), progress)
                    saved += 1
                except Conflict as e:
                    log("dropped: %s" % e)
                    lines.append("Dropped: %s" % e)
                self.queue.pop(0)
                self.save()
            if saved:
                lines.insert(0, "Uploaded %d" % saved)
            if sync_subjects:
                stage("Subjects")
                found = cloud.subjects()
                if found:
                    self.subjects = found
                    self.sel = min(self.sel, len(found) - 1)
                    self.save()
                    lines.append("%d subjects" % len(found))
            lines = lines or ["Up to date"]
            log("sync done: %s" % " | ".join(lines))
            return lines
        except Exception as e:
            log("sync failed: %s" % e)
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
        rtc.set_alarm(seconds=TICK_S - t.elapsed(now_epoch()) % TICK_S)
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
