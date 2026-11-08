"""Offline stand-ins for the Badgeware runtime and the free cloud save."""
import builtins
import json
import sys
import types
import urllib.parse

USERNAME = "tester"


class FakeDB:
    """Fake https://www.superbyte.site/account.php holding one save blob."""

    def __init__(self, blob, username=USERNAME):
        self.username = username
        self.blob = blob if isinstance(blob, str) or blob is None else json.dumps(blob)
        self.log = []
        self.saved = 0

    def handle(self, body):
        if isinstance(body, bytes):
            body = body.decode()
        q = urllib.parse.parse_qs(body or "", keep_blank_values=True)
        action = q.get("action", [""])[0]
        user = q.get("username", [""])[0]
        self.log.append((action, user))
        if action == "load":
            if user != self.username or self.blob is None:
                return "notfound"
            return self.blob
        if action == "save":
            self.blob = q.get("data", [""])[0]
            self.saved += 1
            return "saved"
        if action == "login":
            return "Logged in"
        return "?"


class Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text

    def close(self):
        pass


def make_requests(db, down=False):
    mod = types.ModuleType("requests")
    calls = {"n": 0}

    def post(url, data=None, headers=None):
        calls["n"] += 1
        if down:
            raise OSError("network down")
        return Resp(200, db.handle(data))

    mod.post = post
    mod.calls = calls
    return mod


class Clock:
    def __init__(self, epoch):
        self.epoch = epoch
        self.alarms = []

    def datetime(self):
        import datetime as dt
        d = dt.datetime.fromtimestamp(self.epoch, dt.timezone.utc)
        return (d.year, d.month, d.day, d.hour, d.minute, d.second, d.weekday())

    def set_alarm(self, **k):
        self.alarms.append(k)

    def clear_alarm(self):
        pass

    def time_from_ntp(self):
        pass


class Pad:
    def __init__(self):
        self.next = []

    def pressed(self, b=None):
        return b in self.next


def install(db, epoch, secrets_attrs=None, down=False):
    for name in list(sys.modules):
        if name in ("study_bunny", "requests", "wifi", "secrets"):
            del sys.modules[name]
    sec = types.ModuleType("secrets")
    sec.STUDYBUNNY_USERNAME = USERNAME
    sec.TIMEZONE = 0
    for k, v in (secrets_attrs or {}).items():
        setattr(sec, k, v)
    sys.modules["secrets"] = sec
    sys.modules["requests"] = make_requests(db, down)
    wf = types.ModuleType("wifi")
    wf.connect = lambda *a, **k: True
    wf.disconnect = lambda: None
    wf.is_connected = lambda: True
    sys.modules["wifi"] = wf

    import time as _t
    if not hasattr(_t, "ticks_ms"):
        _t.ticks_ms = lambda: int(_t.monotonic() * 1000)
        _t.ticks_diff = lambda a, b: a - b

    clock = Clock(epoch)
    pad = Pad()

    class Drawn(list):
        pass

    drawn = Drawn()
    blits = []
    loads = []

    class Screen:
        width, height = 264, 176
        pen = None
        font = None

        def clear(self):
            drawn.clear()

        def text(self, m, x, y, size=1):
            drawn.append(m)

        def measure_text(self, m, size=1):
            return (len(m) * 6 * size, 8 * size)

        def rectangle(self, *a):
            pass

        def shape(self, *a):
            pass

        def blit(self, *a):
            blits.append(a)

    class Color:
        white, black, dark_grey = "w", "k", "g"

    class Shape:
        def __getattr__(self, name):
            class S:
                def __init__(self, *a):
                    pass

                def stroke(self, *a):
                    return self
            return S

    class Image:
        @staticmethod
        def load(path):
            if "assets/bunny_" not in path:
                raise OSError(path)
            loads.append(path)
            return path

    class Brush:
        @staticmethod
        def pattern(*a):
            return "pat"

    class Badge:
        @staticmethod
        def update():
            pass

        @staticmethod
        def mode(*a):
            pass

        pressed = staticmethod(pad.pressed)

    ns = dict(
        shape=Shape(),
        image=Image,
        brush=Brush,
        vec2=lambda x, y: (x, y),
        rtc=clock,
        badge=Badge,
        screen=Screen(),
        color=Color,
        run=lambda f: None,
        wait_for_button_or_alarm=lambda **k: None,
        BUTTON_A="A", BUTTON_B="B", BUTTON_C="C", BUTTON_UP="UP", BUTTON_DOWN="DOWN",
        FAST_UPDATE=1,
    )
    for k, v in ns.items():
        setattr(builtins, k, v)
    sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
    import importlib
    mod = importlib.import_module("study_bunny")
    drawn.blits, drawn.loads = blits, loads
    return mod, clock, pad, drawn
