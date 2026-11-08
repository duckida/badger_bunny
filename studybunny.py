#!/usr/bin/env python3
"""
studybunny.py - read (and optionally write) Study Bunny cloud data.

The app is Unity/IL2CPP (com.superbyte.studybunny). Its "Cloud Save" feature
does, in effect:

    ref = FirebaseDatabase.DefaultInstance
            .GetReference("users/" + FirebaseAuth.CurrentUser.UserId + "/gameData")
    ref.SetRawJsonValueAsync(JsonUtility.ToJson(exportData))     # save
    snap = ref.GetValueAsync()                                   # load
    JsonUtility.FromJson<ExportData>(snap.GetRawJsonValue())

So there is no dedicated stats REST API: your study history lives in the
Firebase Realtime Database under users/<uid>/gameData, and the same node is
both readable and writable by the account owner.

Auth: Firebase email/password REST API with the API key baked into the APK.

Usage:
    python3 studybunny.py whoami  --email you@example.com
    python3 studybunny.py stats   --email you@example.com
    python3 studybunny.py dump    --email you@example.com -o cloud.json
    python3 studybunny.py sessions --email you@example.com

    # writes are dry-run unless --apply is passed
    python3 studybunny.py set-stats --email you@example.com --file stats.json --apply
    python3 studybunny.py patch     --email you@example.com --key coins --value 999 --apply

Password is read from $STUDYBUNNY_PASSWORD or prompted with getpass, so it
never has to be typed on the command line.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_KEY = "AIzaSyDj1RU2NlVEZxgniTO3KICD50-CVZlMjjw"
RTDB = "https://study-bunny-default-rtdb.firebaseio.com"
IDENTITY = "https://identitytoolkit.googleapis.com/v1/accounts"
TOKEN_TOOL = "https://securetoken.googleapis.com/v1/token"

UA = "StudyBunnyTool/1.0"


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def _request(url: str, data: bytes | None = None, method: str | None = None,
             timeout: int = 30) -> tuple[int, dict | list | str]:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = body
        return e.code, parsed


class AuthError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Firebase Auth (email/password)
# --------------------------------------------------------------------------
def sign_in(email: str, password: str) -> dict:
    """Return {idToken, localId, refreshToken, email, expiresIn}."""
    payload = json.dumps({
        "email": email,
        "password": password,
        "returnSecureToken": True,
    }).encode()
    status, body = _request(f"{IDENTITY}:signInWithPassword?key={API_KEY}", payload)
    if status != 200:
        msg = (body or {}).get("error", {}).get("message") if isinstance(body, dict) else body
        raise AuthError(f"sign-in failed ({status}): {msg}")
    return body


def refresh_id_token(refresh_token: str) -> dict:
    payload = f"grant_type=refresh_token&refresh_token={refresh_token}".encode()
    req = urllib.request.Request(f"{TOKEN_TOOL}?key={API_KEY}", data=payload)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read().decode())
    return {"idToken": body["id_token"], "localId": body["user_id"],
            "refreshToken": body["refresh_token"], "expiresIn": body["expires_in"]}


# --------------------------------------------------------------------------
# Realtime Database helpers
# --------------------------------------------------------------------------
def db_url(path: str, token: str) -> str:
    path = path.strip("/")
    return f"{RTDB}/{path}.json?auth={urllib.parse.quote(token)}"


def db_get(path: str, token: str) -> object:
    status, body = _request(db_url(path, token))
    if status == 401:
        raise AuthError(f"database read denied (401) for /{path.strip('/')}: {body}")
    if status != 200:
        raise RuntimeError(f"database read failed ({status}) for /{path.strip('/')}: {body}")
    return body


def db_put(path: str, token: str, value: object) -> object:
    data = json.dumps(value).encode()
    status, body = _request(db_url(path, token), data=data, method="PUT")
    if status != 200:
        raise RuntimeError(f"database write failed ({status}) for /{path.strip('/')}: {body}")
    return body


def db_patch(path: str, token: str, value: dict) -> object:
    data = json.dumps(value).encode()
    status, body = _request(db_url(path, token), data=data, method="PATCH")
    if status != 200:
        raise RuntimeError(f"database patch failed ({status}) for /{path.strip('/')}: {body}")
    return body


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
# ExportData (Cloud Save payload) -> users/<uid>/gameData
#   coins, carrots, redeemedRefPrize, refUserRedeemed, playerowneditems,
#   todo, musicowned, savedtimeandusesleft, stats, flashcards
# stats is a *string* holding JSON for StatsData:
#   {"sessionBlocks":[{"color":int,"sessionStarted":ms,"sessionEnded":ms}, ...],
#    "monthLegends":[{"monthYear":ms,"colorsAndSubjectsOfThisMonth":
#                      [{"color":int,"subject":str}, ...]}, ...]}


def parse_stats(game_data: dict) -> dict:
    raw = game_data.get("stats")
    if raw is None:
        return {"sessionBlocks": [], "monthLegends": []}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {"sessionBlocks": [], "monthLegends": []}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"'stats' field is not valid JSON: {e}") from e
    raise RuntimeError(f"unexpected 'stats' type: {type(raw).__name__}")


def encode_stats(original: object, stats: dict) -> object:
    """Keep the stored shape: string-encoded stats stay strings, objects stay objects."""
    if isinstance(original, str):
        return json.dumps(stats, separators=(",", ":"))
    return stats


def _dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone()


def summarise(stats: dict) -> dict:
    blocks = stats.get("sessionBlocks") or []
    legends = stats.get("monthLegends") or []

    # color -> subject (per month; fall back to the whole legend list)
    subject_by_color: dict[int, str] = {}
    for m in legends:
        for row in (m.get("colorsAndSubjectsOfThisMonth") or []):
            c = row.get("color")
            s = row.get("subject")
            if c is not None and s:
                subject_by_color.setdefault(int(c), s)

    total_ms = 0
    per_day: dict[str, int] = {}
    per_subject: dict[str, int] = {}
    per_month: dict[str, int] = {}
    longest = 0
    sess = []

    for b in blocks:
        start, end = b.get("sessionStarted"), b.get("sessionEnded")
        if start is None or end is None:
            continue
        dur = int(end) - int(start)
        if dur <= 0:
            continue
        d = _dt(start)
        day = d.strftime("%Y-%m-%d")
        month = d.strftime("%Y-%m")
        subj = subject_by_color.get(int(b.get("color", -1)), "(unlabelled)")
        total_ms += dur
        per_day[day] = per_day.get(day, 0) + dur
        per_month[month] = per_month.get(month, 0) + dur
        per_subject[subj] = per_subject.get(subj, 0) + dur
        longest = max(longest, dur)
        sess.append({"day": day, "start": d.isoformat(), "seconds": dur // 1000,
                     "subject": subj, "color": b.get("color")})

    sess.sort(key=lambda x: x["start"])
    hrs = lambda ms: round(ms / 3_600_000, 2)
    return {
        "sessions": len(sess),
        "totalHours": hrs(total_ms),
        "longestSessionMinutes": round(longest / 60_000, 1),
        "hoursPerDay": {k: hrs(v) for k, v in sorted(per_day.items())},
        "hoursPerMonth": {k: hrs(v) for k, v in sorted(per_month.items())},
        "hoursPerSubject": dict(sorted(((k, hrs(v)) for k, v in per_subject.items()),
                                       key=lambda kv: -kv[1])),
        "sessionList": sess,
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def get_password(args) -> str:
    pw = os.environ.get("STUDYBUNNY_PASSWORD")
    if pw:
        return pw
    if not sys.stdin.isatty():
        sys.exit("No password: set STUDYBUNNY_PASSWORD or run interactively.")
    return getpass.getpass(f"Password for {args.email}: ")


def load_session(args) -> tuple[str, str]:
    auth = sign_in(args.email, get_password(args))
    print(f"signed in as {auth.get('email')} (uid {auth['localId']})", file=sys.stderr)
    return auth["idToken"], auth["localId"]


def cmd_whoami(args) -> int:
    token, uid = load_session(args)
    for path in (f"users/{uid}", f"users/{uid}/gameData"):
        try:
            v = db_get(path, token)
        except AuthError as e:
            print(f"/{path}: {e}")
            continue
        print(f"/{path}: {'present' if v is not None else 'empty'} "
              f"({len(json.dumps(v)) if v is not None else 0} bytes)")
    return 0


def fetch_game_data(token: str, uid: str) -> dict:
    data = db_get(f"users/{uid}/gameData", token)
    if data is None:
        raise RuntimeError("no gameData node: has this account ever Cloud Saved?")
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected gameData type: {type(data).__name__}")
    return data


def cmd_stats(args) -> int:
    token, uid = load_session(args)
    gd = fetch_game_data(token, uid)
    s = summarise(parse_stats(gd))
    s.pop("sessionList", None)
    if args.json:
        print(json.dumps(s, indent=2))
        return 0
    print(f"total study time : {s['totalHours']} h over {s['sessions']} sessions")
    print(f"longest session  : {s['longestSessionMinutes']} min")
    print("\nby subject:")
    for k, v in s["hoursPerSubject"].items():
        print(f"  {v:8.2f} h  {k}")
    print("\nby month:")
    for k, v in s["hoursPerMonth"].items():
        print(f"  {k}  {v:8.2f} h")
    return 0


def cmd_sessions(args) -> int:
    token, uid = load_session(args)
    gd = fetch_game_data(token, uid)
    s = summarise(parse_stats(gd))
    rows = s["sessionList"][-args.limit:]
    for r in rows:
        print(f"{r['start']}  {r['seconds']/60:7.1f} min  {r['subject']}")
    return 0


def cmd_dump(args) -> int:
    token, uid = load_session(args)
    gd = fetch_game_data(token, uid)
    out = json.dumps(gd, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(out + "\n")
        print(f"wrote {args.out} ({len(out)} bytes)")
    else:
        print(out)
    return 0


def cmd_set_stats(args) -> int:
    token, uid = load_session(args)
    with open(args.file) as f:
        new_stats = json.load(f)
    if not isinstance(new_stats, dict):
        sys.exit("stats file must contain a JSON object")
    gd = fetch_game_data(token, uid)
    gd["stats"] = encode_stats(gd.get("stats"), new_stats)
    if not args.apply:
        print(json.dumps(gd, indent=2))
        print("\n[dry run] pass --apply to write this to "
              f"/users/{uid}/gameData", file=sys.stderr)
        return 0
    db_put(f"users/{uid}/gameData", token, gd)
    print(f"wrote /users/{uid}/gameData")
    return 0


MIN_SESSION_MS = 5 * 60_000
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")


def _month_start_ms(ms: int) -> int:
    d = _dt(ms)
    return int(datetime(d.year, d.month, 1, tzinfo=d.tzinfo).timestamp() * 1000)


def _backup(uid: str, gd: dict) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUP_DIR, f"{uid}-{stamp}.json")
    with open(path, "w") as f:
        json.dump(gd, f, indent=2)
    return path


def cmd_inject(args) -> int:
    if args.minutes * 60_000 < MIN_SESSION_MS:
        sys.exit("refusing: the app never records sessions under 5 minutes")
    start = datetime.fromisoformat(args.start)
    if start.tzinfo is None:
        start = start.astimezone()
    start_ms = int(start.timestamp() * 1000)
    end_ms = start_ms + int(args.minutes * 60_000)
    if end_ms > int(time.time() * 1000):
        sys.exit("refusing: session would end in the future")

    token, uid = load_session(args)
    gd = fetch_game_data(token, uid)
    stats = parse_stats(gd)
    blocks = stats.setdefault("sessionBlocks", [])
    legends = stats.setdefault("monthLegends", [])

    for b in blocks:
        if int(b["sessionStarted"]) < end_ms and start_ms < int(b["sessionEnded"]):
            sys.exit("refusing: overlaps existing block starting "
                     f"{_dt(b['sessionStarted']).isoformat()}")

    color = args.color
    if color is None:
        for m in legends:
            for row in m.get("colorsAndSubjectsOfThisMonth") or []:
                if row.get("subject") == args.subject:
                    color = int(row["color"])
                    break
            if color is not None:
                break
    if color is None:
        sys.exit(f"subject {args.subject!r} has no existing color; pass --color N")

    month = _month_start_ms(start_ms)
    legend = next((m for m in legends if m.get("monthYear") == month), None)
    if legend is None:
        legend = {"monthYear": month, "colorsAndSubjectsOfThisMonth": []}
        legends.append(legend)
    rows = legend["colorsAndSubjectsOfThisMonth"]
    if not any(int(r.get("color", -1)) == color for r in rows):
        rows.append({"color": color, "subject": args.subject})

    blocks.append({"color": color, "sessionStarted": start_ms, "sessionEnded": end_ms})
    blocks.sort(key=lambda b: int(b["sessionStarted"]))

    new_value = encode_stats(gd.get("stats"), stats)
    print(f"block: {start.isoformat()} -> +{args.minutes} min, "
          f"subject={args.subject!r} color={color}")
    if not args.apply:
        print(f"[dry run] would PATCH /users/{uid}/gameData stats "
              f"({len(blocks)} blocks). Pass --apply to write.", file=sys.stderr)
        return 0

    path = _backup(uid, gd)
    print(f"backup: {path}", file=sys.stderr)
    db_patch(f"users/{uid}/gameData", token, {"stats": new_value})
    check = summarise(parse_stats(fetch_game_data(token, uid)))
    found = any(s["start"] == start.isoformat() for s in check["sessionList"])
    print(f"written. re-read shows block: {found}; total {check['totalHours']} h")
    return 0 if found else 1


def cmd_rollback(args) -> int:
    with open(args.file) as f:
        backup = json.load(f)
    if "stats" not in backup:
        sys.exit("backup has no 'stats' key")
    token, uid = load_session(args)
    print(f"[{'apply' if args.apply else 'dry run'}] restore stats from {args.file}")
    if args.apply:
        db_patch(f"users/{uid}/gameData", token, {"stats": backup["stats"]})
        print("restored")
    return 0


def cmd_patch(args) -> int:
    token, uid = load_session(args)
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError:
        value = args.value
    body = {args.key: value}
    if not args.apply:
        print(f"[dry run] PATCH /users/{uid}/gameData {json.dumps(body)}",
              file=sys.stderr)
        return 0
    db_patch(f"users/{uid}/gameData", token, body)
    print(f"patched {args.key} on /users/{uid}/gameData")
    return 0


def cmd_multiplayer(args) -> int:
    token, uid = load_session(args)
    v = db_get(f"multiplayer/{uid}", token)
    print(json.dumps(v, indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--email", help="account email (or $STUDYBUNNY_EMAIL)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=fn)
        return sp

    add("whoami", cmd_whoami, "show uid and which nodes exist")
    sp = add("stats", cmd_stats, "summarise study hours")
    sp.add_argument("--json", action="store_true")
    sp = add("sessions", cmd_sessions, "list recent sessions")
    sp.add_argument("--limit", type=int, default=20)
    sp = add("dump", cmd_dump, "dump raw gameData JSON")
    sp.add_argument("-o", "--out")
    sp = add("set-stats", cmd_set_stats, "replace the stats blob (writes!)")
    sp.add_argument("--file", required=True)
    sp.add_argument("--apply", action="store_true")
    sp = add("patch", cmd_patch, "patch one gameData key (writes!)")
    sp.add_argument("--key", required=True)
    sp.add_argument("--value", required=True)
    sp.add_argument("--apply", action="store_true")
    add("multiplayer", cmd_multiplayer, "read the public multiplayer snapshot")
    sp = add("inject", cmd_inject, "append one finished session to stats (writes!)")
    sp.add_argument("--subject", required=True)
    sp.add_argument("--start", required=True, help="local ISO time, e.g. 2026-11-07T19:00")
    sp.add_argument("--minutes", type=float, required=True)
    sp.add_argument("--color", type=int, help="color index if subject has none yet")
    sp.add_argument("--apply", action="store_true")
    sp = add("rollback", cmd_rollback, "restore stats from a backup file (writes!)")
    sp.add_argument("--file", required=True)
    sp.add_argument("--apply", action="store_true")

    args = p.parse_args(argv)
    if not args.email:
        args.email = os.environ.get("STUDYBUNNY_EMAIL")
    if not args.email:
        p.error("--email is required (or set $STUDYBUNNY_EMAIL)")
    try:
        return args.func(args)
    except (AuthError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
