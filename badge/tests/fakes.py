"""Offline stand-ins for the Badgeware runtime and the Firebase REST API."""
import builtins
import json
import sys
import types
import urllib.parse

UID = "UID123"


class FakeDB:
    def __init__(self, game_data):
        self.root = {"users": {UID: {"gameData": game_data}}}
        self.log = []

    def _walk(self, parts, create=False):
        cur = self.root
        for p in parts:
            if isinstance(cur, list):
                i = int(p)
                if i >= len(cur):
                    if not create:
                        return None
                    while len(cur) <= i:
                        cur.append(None)
                if cur[i] is None and create:
                    cur[i] = {}
                cur = cur[i]
            elif isinstance(cur, dict):
                if p not in cur:
                    if not create:
                        return None
                    cur[p] = {}
                cur = cur[p]
            else:
                return None
        return cur

    def handle(self, method, url, body):
        u = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(u.query)
        assert u.path.endswith(".json"), u.path
        parts = [p for p in u.path[:-5].split("/") if p]
        assert q["auth"] == ["TOKEN"], "missing/incorrect auth"
        self.log.append((method, "/".join(parts), body))
        if method == "get":
            val = self._walk(parts)
            if "shallow" in q and isinstance(val, (dict, list)):
                keys = val.keys() if isinstance(val, dict) else [str(i) for i, x in enumerate(val) if x is not None]
                return {k: True for k in keys}
            if "limitToLast" in q and isinstance(val, list):
                n = int(q["limitToLast"][0])
                items = [(i, x) for i, x in enumerate(val) if x is not None][-n:]
                return {str(i): x for i, x in items}
            return val
        body = json.loads(body)
        if method == "put":
            parent = self._walk(parts[:-1], create=True)
            k = parts[-1]
            if isinstance(parent, list):
                i = int(k)
                while len(parent) <= i:
                    parent.append(None)
                parent[i] = body
            else:
                parent[k] = body
            return body
        if method == "patch":
            node = self._walk(parts, create=True)
            node.update(body)
            return body
        raise AssertionError(method)


class Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data

    def close(self):
        pass


def make_requests(db, accounts=None, down=False):
    accounts = accounts or {"me@x.com": "pw"}
    mod = types.ModuleType("requests")
    calls = {"n": 0}

    def make(method):
        def fn(url, data=None, headers=None):
            calls["n"] += 1
            if down:
                raise OSError("network down")
            if "signInWithPassword" in url:
                b = json.loads(data)
                if accounts.get(b["email"]) != b["password"]:
                    return Resp(400, {"error": {"message": "INVALID_LOGIN_CREDENTIALS"}})
                return Resp(200, {"idToken": "TOKEN", "localId": UID})
            return Resp(200, db.handle(method, url, data))
        return fn

    for m in ("get", "post", "put", "patch"):
        setattr(mod, m, make(m))
    mod.calls = calls
    return mod


class Clock:
    def __init__(self, epoch):
        self.epoch = epoch

    def datetime(self):
        import datetime as dt
        d = dt.datetime.fromtimestamp(self.epoch, dt.timezone.utc)
        return (d.year, d.month, d.day, d.hour, d.minute, d.second, d.weekday())

    def set_alarm(self, **k):
        pass

    def clear_alarm(self):
        pass

    def time_from_ntp(self):
        pass


class Pad:
    def __init__(self):
        self.next = []

    def pressed(self, b=None):
        return b in self.next


def install(db, epoch, secrets_attrs=None, down=False, accounts=None):
    for name in list(sys.modules):
        if name in ("study_bunny", "requests", "wifi", "secrets", "time_shim"):
            del sys.modules[name]
    sec = types.ModuleType("secrets")
    sec.STUDYBUNNY_EMAIL = "me@x.com"
    sec.STUDYBUNNY_PASSWORD = "pw"
    sec.TIMEZONE = 0
    for k, v in (secrets_attrs or {}).items():
        setattr(sec, k, v)
    sys.modules["secrets"] = sec
    sys.modules["requests"] = make_requests(db, accounts, down)
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
    drawn = []

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

    class Color:
        white, black = "w", "k"

    class Badge:
        @staticmethod
        def update():
            pass

        @staticmethod
        def mode(*a):
            pass

        pressed = staticmethod(pad.pressed)

    ns = dict(
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
    return mod, clock, pad, drawn
