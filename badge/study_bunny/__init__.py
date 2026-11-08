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


def _number(s, i):
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    n = 0
    while i < len(s) and "0" <= s[i] <= "9":
        n = n * 10 + ord(s[i]) - 48
        i += 1
    return n


def _array_span(s, key):
    """Span of the [ ... ] belonging to "key":[...] in JSON text, or None."""
    i = s.find('"%s"' % key)
    if i < 0:
        return None
    a = s.find("[", i)
    if a < 0:
        return None
    depth, j, string, esc = 0, a, False, False
    while j < len(s):
        c = s[j]
        if string:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                string = False
        elif c == '"':
            string = True
        elif c == "[" or c == "{":
            depth += 1
        elif c == "]" or c == "}":
            depth -= 1
            if depth == 0:
                return a, j
        j += 1
    return None


def blocks_from(stats):
    """[(start, end)] in ticks, in stored order. Scans text, no full parse."""
    span = _array_span(stats, "sessionBlocks")
    if span is None:
        raise CloudError("save has no sessionBlocks")
    body = stats[span[0] + 1:span[1]]
    out, pos = [], 0
    while True:
        k = body.find('"sessionStarted":', pos)
        if k < 0:
            return out
        start = _number(body, k + 17)
        k2 = body.find('"sessionEnded":', k + 17)
        end = _number(body, k2 + 15) if k2 >= 0 else 0
        out.append((start, end))
        pos = k + 1


def find_conflict(blocks, s_ticks, e_ticks):
    for bs, be in blocks:
        if bs == s_ticks and be == e_ticks:
            return "dup"
    for bs, be in blocks:
        if bs < e_ticks and s_ticks < be:
            return "overlap"
    return None


def add_block(stats, blocks, s_ticks, e_ticks, color):
    span = _array_span(stats, "sessionBlocks")
    block = '{"color":%d,"sessionStarted":%d,"sessionEnded":%d}' % (
        color, s_ticks, e_ticks
    )
    body = stats[span[0] + 1:span[1]]
    if not body.strip():
        return stats[:span[0] + 1] + block + stats[span[1]:]
    if s_ticks >= blocks[-1][0]:
        return stats[:span[1]] + "," + block + stats[span[1]:]
    # Older session (uploaded late): rebuild the list in order.
    parts, depth, start = [], 0, 0
    for i, c in enumerate(body):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                parts.append(body[start:i + 1])
    at = len(parts)
    for i, part in enumerate(parts):
        k = part.find('"sessionStarted":')
        if k >= 0 and _number(part, k + 17) > s_ticks:
            at = i
            break
    parts.insert(at, block)
    return stats[:span[0] + 1] + ",".join(parts) + stats[span[1]:]


def plan_subject(stats, local_s, subject):
    """Return (color, stats) with this month's legend carrying `subject`."""
    span = _array_span(stats, "monthLegends")
    if span is None:
        raise CloudError("save has no monthLegends")
    body = stats[span[0] + 1:span[1]]
    legs = json.loads("[" + body + "]") if body.strip() else []
    y, m = local_ym(local_s)
    hit = None
    for leg in legs:
        my = int(leg.get("monthYear") or 0)
        if my < TICKS_EPOCH:
            continue
        if local_ym(from_ticks(my)) == (y, m):
            hit = leg
            break
    if hit is None:
        color = 0
        legs.append({
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
            used = set(int(r.get("color") or 0) for r in rows)
            color = 0
            while color in used and color < 15:
                color += 1
            rows.append({"color": color, "subject": subject})
            hit["colorsAndSubjectsOfThisMonth"] = rows
    return color, stats[:span[0]] + json.dumps(legs) + stats[span[1] + 1:]


def subjects_from_stats(stats):
    span = _array_span(stats, "monthLegends")
    if span is None:
        raise CloudError("save has no monthLegends")
    body = stats[span[0] + 1:span[1]]
    legs = json.loads("[" + body + "]") if body.strip() else []
    best, best_m = None, -1
    for leg in legs:
        my = int(leg.get("monthYear") or 0)
        if my > best_m:
            best, best_m = leg, my
    out = []
    for row in (best or {}).get("colorsAndSubjectsOfThisMonth") or []:
        name = row.get("subject")
        if name:
            out.append([name, int(row.get("color") or 0)])
    return out[:12]


def _string_span(s, key):
    """Span including quotes of the value of "key":"..." in JSON text."""
    i = s.find('"%s"' % key)
    if i < 0:
        return None
    j = s.find(":", i)
    if j < 0:
        return None
    j += 1
    while j < len(s) and s[j] in " \t\r\n":
        j += 1
    if j >= len(s) or s[j] != '"':
        return None
    a = j + 1
    j, esc = a, False
    while j < len(s):
        c = s[j]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            return a - 1, j + 1
        j += 1
    return None


def stats_of(raw):
    span = _string_span(raw, "stats")
    if span is None:
        raise CloudError("save has no stats")
    stats = json.loads(raw[span[0]:span[1]])
    return stats if isinstance(stats, str) else json.dumps(stats)


def splice_stats(raw, stats):
    span = _string_span(raw, "stats")
    if span is None:
        raise CloudError("save has no stats")
    return raw[:span[0]] + json.dumps(stats) + raw[span[1]:]


def stats_hash(s):
    h = 2166136261
    for ch in s:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return h


HEX = b"0123456789ABCDEF"


def urlencode(s):
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
    r = requests.post(
        FREE_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        status = r.status_code
        text = r.text
    finally:
        r.close()
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

    def submit(self, start_utc, end_utc, subject, tz_s):
        """Append one session. Returns "ok" or "dup"; raises Conflict/CloudError."""
        raw = self.load()
        digest = stats_hash(raw)
        stats = stats_of(raw)
        s_local, e_local = start_utc + tz_s, end_utc + tz_s
        s_ticks, e_ticks = to_ticks(s_local), to_ticks(e_local)
        blocks = blocks_from(stats)
        found = find_conflict(blocks, s_ticks, e_ticks)
        if found == "dup":
            return "dup"
        if found:
            raise Conflict("overlaps a session already in your save")
        color, stats = plan_subject(stats, e_local, subject)
        stats = add_block(stats, blocks, s_ticks, e_ticks, color)
        blob = splice_stats(raw, stats)
        backup(raw)
        # Free the copies we no longer need before the upload allocates its body.
        raw, stats, blocks = None, None, None
        # Someone using the app in the meantime would be overwritten by us.
        if stats_hash(self.load()) != digest:
            raise Conflict("save changed in the app; try Sync again")
        self.save(blob)
        back = stats_of(self.load())
        if (s_ticks, e_ticks) not in blocks_from(back):
            raise CloudError("write could not be verified")
        return "ok"

    def subjects(self):
        return subjects_from_stats(stats_of(self.load()))


def backup(text):
    try:
        with open(BACKUP_FILE, "w") as f:
            f.write(text)
    except OSError:
        pass


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
        with open(STATE_FILE, "w") as f:
            json.dump(d, f)

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
            cloud = FreeCloud(_conf("STUDYBUNNY_USERNAME"))
            saved = 0
            while self.queue:
                start, end, name = self.queue[0][:3]
                if start > 100000000000:  # queue written by an older version
                    start, end = start // 1000, end // 1000
                try:
                    cloud.submit(start, end, name, tz_seconds())
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
