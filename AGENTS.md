# AGENTS.md

Guidance for coding agents (and humans) working in this repository. Read this
before changing code, tests, or anything on the badge.

## What this is

`study-bunny-open` is a **Study Bunny focus-timer app for the Pimoroni Badger
2350** (Badgeware firmware, MicroPython on RP2350), plus host-side tooling for
the game's cloud data.

- The badge app lives in `badge/study_bunny/` and appends completed study
  sessions to the **free** Study Bunny cloud save at
  `https://www.superbyte.site/account.php` — a username-only account with no
  password. That is the save most players have, and the one the badge writes.
- `studybunny.py` is a host CLI for the app's *other* cloud feature (Firebase
  Auth + Realtime Database, the paid "cloud perk"). The badge does **not** use
  it. See `FINDINGS.md` for both APIs.
- Everything is host Python 3 stdlib plus MicroPython 1.27 on the device.

## Repo map

```
badge/                          the badge app (installed at /system/apps/study_bunny)
  study_bunny/__init__.py       the whole app; badgeware calls run(update) at import
  study_bunny/icon.png          menu icon
  study_bunny/assets/*.png      bunny face layers (outline/fill/face composited)
  tests/test_app.py             offline unit tests (56 as of 2026-11)
  tests/fakes.py                FakeDB save server + fakes for requests/wifi/rtc/
                                badge/screen; installs them and imports the app
  tests/real_save_check.py      offline end-to-end check against a real save blob
                                (29 checks)
  secrets.example.py            template for the badge's /secrets.py
  README.md                     user-facing docs — keep true, update with changes
studybunny.py                   host CLI for the Firebase save (dry-run by default)
FINDINGS.md                     reverse-engineering notes: free save vs Firebase save
.work/                          gitignored scratch: real save blob, APK dump,
                                sprites, tools (large; never commit)
backups/                        gitignored: studybunny.py backups
```

`.work/backup/free_load_raw.txt` is the **real** save: 62,195 chars, 628
sessions, 14 month legends, last block colour 8. It is the canonical fixture for
`tests/real_save_check.py`. It is personal data: never commit it, never paste it
into issues or chat.

## Non-negotiable safety rules

1. **The user's real cloud save is sacred.** Any test that calls
   `FreeCloud.save()` / `submit()` must run against `tests/fakes.py`'s fake
   server. Never POST `action=save` to the real server from a bench script. When
   probing the live save on-device, only `load()` (read-only) and local
   parse/edit/verify code — never `submit()`. The only intended writer is the
   app itself when the user presses Save/Sync.
2. **Never commit or print the username** (`STUDYBUNNY_USERNAME`) or Wi-Fi
   credentials. They live in the badge's `/secrets.py`; the repo has
   `secrets.example.py`. Anyone with the username can read *and overwrite* the
   save.
3. **Do not remount or rewrite the badge's internal flash outside the documented
   procedure** (below). `/system` is mounted read-only on purpose. Never
   `os.umount`/`os.mount` internal volumes, never flash firmware.
4. **Don't drive the badge's UI or press its buttons** on the user's badge
   unless asked. Leave the badge at its menu when you finish.
5. **Badge source stays pure ASCII with LF line endings.** Verify before
   deploying.
6. **Tests must pass offline.** No network in the test suite.

## Quick commands

```bash
cd badge
python3 -m unittest discover -s tests          # 56 tests, ~0.2 s, no network
python3 tests/real_save_check.py               # 29 checks vs .work/backup/free_load_raw.txt
python3 tests/real_save_check.py path/to/blob  # any raw `load` reply body

# Host CLI (Firebase save). Reads are safe; writes are dry-run unless --apply.
export STUDYBUNNY_EMAIL=you@example.com STUDYBUNNY_PASSWORD=...
python3 studybunny.py whoami
python3 studybunny.py stats
python3 studybunny.py sessions --limit 20
python3 studybunny.py dump -o cloud.json
```

## App architecture

`badge/study_bunny/__init__.py` is a single module; badgeware executes it with
`run(update)` at the bottom. The loop looks like:

```
update()
  App.draw()                        # e-ink render of the current view
  badge.update()
  wait_for_button_or_alarm(...)     # 10 s RTC alarm while studying, 120 s idle
  App.handle([pressed buttons])     # state machine
```

`App`'s UI state is `view`: `None` = home (idle / running / paused),
`"confirm"` = Stop? screen, `"msg"` = message panel. `Tracker` stores wall-clock
start plus active seconds, survives sleep via `/study_bunny.json`. Stop → Save
appends `[start_s, end_s, subject, paused_s]` to `App.queue`, persists it, and
immediately tries `flush()`; a failed submit leaves the item queued for the next
Sync.

`flush(sync_subjects, ntp)`:

```
Wi-Fi  -> connect_wifi() (~1 s after the first initializing call)
Clock  -> rtc.time_from_ntp() (Sync only; failure is non-fatal)
queue  -> FreeCloud.submit(start, end, subject, tz, paused) for each item
          Conflict (overlap/changed save) -> dropped, reported, popped
          CloudError (network)           -> stays queued, sync aborts
Subjects -> FreeCloud.subjects() (Sync only) refreshes the picker
```

`FreeCloud.submit` is the whole edit path:

```
load       whole blob from account.php                      ~3–4 s
parse_save json.loads(blob)                                 ~0.5 s
parse_stats json.loads(stats string)                        ~0.3 s
blocks_of / find_conflict  dup -> "dup"; overlap -> Conflict
plan_colour  reuse this month's colour for the subject, or add the next free one
add_block    insert into sessionBlocks in start order
coins        coins_for(end - start, paused): +1/10 min studied, -1/7 min paused
dumps        json.dumps(stats), then json.dumps(blob), separators=(",",":")
backup       /study_bunny_backup.json (previous blob; guarded write)
change check reload and compare text exactly (C-level !=)
save         POST action=save
verify       reload, parse, confirm (start, end) is present
```

### Save format (free save)

- The blob is one `ExportData` JSON object. `stats` is a **string** containing
  JSON — keep it a string on write.
- `sessionBlocks`: `[{"color": 0–15, "sessionStarted": ticks,
  "sessionEnded": ticks}]`, sorted by start, non-overlapping, one entry per
  completed session.
- Ticks are .NET `DateTime` ticks (100 ns units since 0001-01-01) of **local
  wall-clock** time. Unix epoch = `621355968000000000` ticks. Session length is
  `end - start`; paused time is excluded by construction.
- `monthLegends`: `[{"monthYear": ticks, "colorsAndSubjectsOfThisMonth":
  [{"color": int, "subject": str}]}]`. The newest `monthYear` drives the
  subject list (12 max in the UI). Colours are per subject per month; a new
  subject gets the next unused colour.
- `coins` is credited per session the way the app's accumulation page does it:
  one coin per 10 minutes studied (rounded half up) minus one coin per 7 minutes
  paused (floored), clamped to 0–100 for that session. A dup adds nothing; a
  conflict writes nothing. The badge takes the pause length from the queue
  entry because the block itself only stores active time.
- Everything else must round-trip untouched: `carrots`,
  `redeemedRefPrize`, `refUserRedeemed`, `playerowneditems`, `todo`,
  `musicowned`, `savedtimeandusesleft`, `flashcards`.
- Protocol: form POST to `https://www.superbyte.site/account.php`.
  `action=load&username=...` returns the blob or `notfound`;
  `action=save&username=...&data=<urlencoded blob>` returns `saved`. No partial
  updates, no auth. `load`/`save` are idempotent-safe by our own checks, not the
  server's.
- Re-serialising changes key order/whitespace (MicroPython dicts are unordered).
  That is accepted: values are exact (18-digit ticks included) and the app's
  parser does not care. Compare parsed values, not bytes — except the
  change-guard, which compares two strings from the same producer.

## Hardware and firmware constraints (learned the hard way)

Badge: Pimoroni Badger 2350, RP2350, ~150 MHz, 264×176 e-ink, buttons
A/B/C/UP/DOWN, firmware `bw-1.27.0 badger on 2026-08-09` (MicroPython 1.27).
Heap ~8.3 MB free at boot.

**Performance numbers measured on the real badge** — design around these:

| operation | time |
|---|---|
| `while i < len(s): c = s[i]` loop | **~1.1 ms/char**, superlinear (10 K chars = 11 s) |
| `for c in s` loop | ~32 µs/char |
| `json.loads` of the 62 KB blob | ~514 ms |
| `json.loads` of the stats string | ~262 ms |
| `json.dumps` of stats (56 KB) | ~456 ms |
| `json.dumps` of the blob | ~597 ms |
| POST load round trip (62 KB download) | ~3.2–3.7 s |
| full parse→edit→re-serialize | ~2.3 s CPU, 7 MB heap left |

That asymmetry is the historical root cause of the "Syncing…" hang: the first
build edited the save by scanning text character by character, which on this
badge is effectively a hang. **Never loop over strings/blobs in Python when a
C-level primitive exists** (`json`, `find`, `replace`, `split`, `%`, slicing).
This badge has memory to spare; optimise for CPU, not heap.

Other firmware behaviour:

- `wifi.connect()` returns `False` on its **first** (initializing) call, then
  `True`. The retry loop handles it — don't treat the first `False` as failure.
- After a **soft reboot** (Thonny Ctrl-D, stopping the running app) the CYW43
  chip can come back half-initialized: every call raises `EPERM`, and the
  console shows `[CYW43] core not in reset` / `HT not ready`. Retrying cannot
  fix it; the app raises `CloudError("Wi-Fi chip stuck: power-cycle the badge,
  then Sync")`. A hard RESET clears it (verified).
- `requests.post(..., timeout=20)` is used, with a fallback call without
  `timeout` if the module rejects the argument. Keep the 20 s bound.
- The running screen redraws on a 10 s RTC alarm (`TICK_S`); idle waits 120 s.
  The app must not deep-sleep while a session runs.
- `rtc.time_from_ntp()` can raise; Sync tolerates it, but a clock before 2025
  (`clock_ok()`) blocks sessions.

## Badge filesystem, deployment, and on-device work

Layout on the badge:

```
/                      writable VFS (root): /study_bunny.json, /study_bunny_backup.json,
                       /state/ (badgeware), .fsbackup* (firmware — leave alone)
/system               13 MiB FAT, mounted **read-only** at runtime (writes: OSError 30, EROFS)
  main.py             launcher: launches state["running"], then resets to the menu
  secrets.py          the `secrets` module (Wi-Fi, STUDYBUNNY_USERNAME, TIMEZONE, DEBUG)
  apps/menu/           menu scans /system/apps only (hardcoded)
  apps/mass_storage/   exposes /system as a USB drive: clear_running(); import _msc.py
  apps/study_bunny/    this app (__init__.py, icon.png, assets/)
```

**Deployment = the badge's mass-storage mode** (how the app was first installed
and the only supported write path):

1. On the badge: pick **mass storage** from the menu (or over serial, run
   `/system/apps/mass_storage/__init__.py` verbatim — do not guess at
   `import _msc`). The drive is a ~13 MiB FAT labelled `BADGER`; the badge's
   CDC serial stays available.
2. On the host: mount it (it is unpartitioned; `/dev/sda` grew from 0 B to
   13 MiB). Beware the desktop automounter also mounts it (`/media/pi/BADGER`).
3. **The drive root maps to `/system`.** The app is `apps/study_bunny/` on the
   drive. A copy at the drive root becomes `/system/study_bunny/`, which the
   menu ignores — this happened before and left an empty stray folder.
4. Copy, then `cmp` against the local file, `sync`, and unmount **every**
   mountpoint.
5. Reboot: press RESET, or over serial `import badgeware; badgeware.reset()`.
   The badge boots to the menu (the mass-storage app cleared `running` first).
6. Verify from the badge (read-only): file length and
   `binascii.crc32(open(path,"rb").read())` must equal the host's.

Recovery while in mass-storage mode: the serial console survives, so after
unmounting, Ctrl-C interrupts the blocked `import _msc.py`, then
`badgeware.reset()`. Never interrupt while a host write is in flight.

**Serial console** (for testing, not required to run the app): `/dev/ttyACM0`
at 115200, pyserial. Raw REPL protocol: `\x03` interrupt (prints a traceback of
whatever is running), `\x01` raw mode, send code + `\x04`, read `OK` + output,
`\x02` back. Handy pattern: copy a candidate module to a writable path (e.g.
`/sb_new.py`) in base64 chunks, then `exec(src, ns)` with
`src.replace("run(update)", "pass").replace("badge.mode(FAST_UPDATE)", "pass")`
and call pieces directly. This is how the app was hardware-tested read-only:
`connect_wifi()`, `load()`, parse, `plan_colour`, `add_block`, dumps, verify —
never save.

## Testing conventions

- `tests/fakes.py` `install()` clears `study_bunny`/`requests`/`wifi`/`secrets`
  from `sys.modules`, builds fakes, puts badgeware names into `builtins`
  (`screen`, `badge`, `rtc`, `run`, `wait_for_button_or_alarm`, `BUTTON_*`,
  `FAST_UPDATE`), then imports the module. Import the app **only** through it.
- `Base.boot(blob, epoch=...)` in `test_app.py` sets temp state/backup paths and
  returns an `App`. Press buttons with `self.press("A")`; inspect with
  `self.db.blob` / `self.stats()` / `self.db.log`.
- `FakeDB` records every `(action, username)` and counts saves — use it to prove
  *no* write happened on refusal paths.
- Keep tests deterministic: fixed epochs (`T0`, `NOV`), no clock/network/FS
  beyond `tempfile`.
- `real_save_check.py` is the user-invariant gate for anything touching the data
  model: session count +1, original blocks untouched and ordered, coins grow by
  the session's formula, non-stats/coin fields identical, key set preserved,
  colour reused, resubmit no-op, overlap refused with no save. Run it before
  committing.
- The fixture is gitignored; a fresh clone must pass with an explicit path.

## Code style

- MicroPython 1.27 subset: `json`, `time`, `os`, optional `gc`; no typing, no
  dataclasses, no third-party imports. Imports must be defensive (`try import
  gc except: gc = None`) because host tests lack them.
- Pure ASCII, LF, 4 spaces, `snake_case` functions/consts, `CapWords` classes.
  `%`-formatting, not f-strings (house style).
- Debug: `log(msg)` prints prefixed `[study_bunny]` only when
  `STUDYBUNNY_DEBUG`; `lap(label)` logs the ms since the previous lap. Wrap any
  new slow step in `lap()` so future hangs self-report. Screen phases go through
  the `progress`/`stage` callable.
- Errors: `CloudError` for any cloud/IO failure, `Conflict` for
  overlap/changed-save. User-facing strings are short and actionable.
- Any filesystem write is guarded (`except OSError: log(...)`) — the FS can be
  read-only (flash mounted on a computer) and must never crash the app.
- Keep e-ink drawing cheap: panel/fill regions, no per-pixel Python loops.
  `badge.update()` flushes.

## Git and docs workflow

- Branch `main`, **direct pushes** (user preference), remote
  `github.com:duckida/badger_bunny.git`. No PRs unless asked.
- Commit summary: imperative, `Badge: ...` for app changes. The body explains
  the root cause, what changed, measured numbers, and test counts — match the
  existing history.
- Update `badge/README.md` in the **same milestone** as any user-facing change
  (install steps, behaviour, limits, sync speed, test counts). Update
  `FINDINGS.md` only for API discoveries.
- Never commit: `.work/`, `backups/`, `__pycache__`/`*.pyc`, `secrets.py`, the
  real save, or credentials.

## Known traps

1. **Python text scans on 60 KB blobs** — the original hang. Use `json`, `find`,
   `replace`, slicing. Numbers above.
2. **`/system` is EROFS at runtime** — writes fail even as root. Deploy via
   mass storage, never by trying to chmod/remount.
3. **MSC drive root = `/system`** — app goes in `apps/study_bunny/`; anything
   dropped at the root is invisible to the menu.
4. **udisks auto-mounts** the badge drive at `/media/pi/BADGER`; unmount every
   mountpoint before rebooting/interrupting MSC.
5. macOS `._*` AppleDouble files on the drive are harmless junk — ignore, don't
   ship.
6. `wifi.connect()` first call returns `False` (initializing) — not an error.
7. CYW43 `EPERM` after soft reboot — hard RESET, don't retry; prefer RESET over
   Thonny soft reboots before a Sync.
8. Re-serialised saves change key order/whitespace — compare parsed values
   (except the change-guard's exact text comparison of same-producer strings).
9. The mass-storage app's import is literally `import _msc.py`; replicate by
   exec'ing the app file, don't guess the module name.
10. `TICK_S` (10 s) is coupled to the draw rounding
    `(secs + TICK_S//2)//TICK_S*TICK_S` and the README; change them together.
11. `epoch_from_tuple` must match `calendar.timegm` (unit-tested) — don't
    "simplify" the civil-date maths.
12. Stopping the menu over serial leaves the REPL at an interrupted menu, not a
    fresh boot; `state["running"]` persists in `/state`. Use `badgeware.reset()`
    to return to a clean menu.
13. After power loss the RTC may be unset (`clock_ok()` false) — Sync sets it
    from NTP.
14. `urlencode`'s fast path requires pure ASCII; any non-ASCII character
    (e.g. an accented subject name) sends the 63 KB body through the slow
    per-character loop (~seconds, still correct). Improve with C-level
    primitives if it ever matters.
15. Queue entries from older builds may be in milliseconds; `flush()` converts
    them (`start > 100000000000` → `//1000`). Keep that migration.
16. Pre-coin-position state files have no `"paused"` key; loading a session that
    was paused then must not count the unknown pause. `Tracker.load` sets
    `seg = -1` for that case and `resume`/`finish` skip the accumulation. Keep
    that migration.

## Definition of done

- [ ] `python3 -m unittest discover -s tests` — all pass (56+)
- [ ] `python3 tests/real_save_check.py` — 29/29 (when the fixture is present)
- [ ] badge source pure ASCII; no new dependencies
- [ ] user-facing docs updated in the same commit (`badge/README.md`)
- [ ] anything touching runtime/device code verified on hardware, **or**
      explicitly reported as not hardware-tested
- [ ] no cloud write fired from the bench; badge left at its menu
- [ ] committed and pushed to `main`
