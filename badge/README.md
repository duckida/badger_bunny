# Study Bunny for Badger 2350

A small study timer for the Badgeware Badger. Start, pause and stop a session on the
badge, and it adds the finished session to your **free** Study Bunny cloud save.

Status: the logic passes 56 offline tests against a fake save server, and the save
editing has been checked offline against a real 62 KB save: 628 sessions became 629,
`stats` and `coins` were the only fields that changed, and the subject's existing
colour was reused. The installed app has also been run live on a real Badger 2350
against the live save: Wi-Fi, download, parse, edit and verify all complete in a few
seconds. The one thing the test bench never fired is the cloud write itself, so the
very first real Sync does that part.

## Install

1. Pick **mass storage** from the badge's menu. The badge appears as a ~13 MB USB
   drive labelled `BADGER` and shows the files its menu scans.
2. Copy the `study_bunny/` folder into `apps/` on the drive, so the app lands at
   `apps/study_bunny/__init__.py` (plus `icon.png` and `assets/`). The badge only
   scans `apps/` — a copy dropped at the drive root is ignored. Updating the app
   later means the same trip: overwrite its files in place.
   Do not copy `.git` files or `__pycache__`.
3. Open `secrets.py` at the drive root. It should already have your Wi-Fi details. Add
   the lines from `secrets.example.py`:
   ```python
   STUDYBUNNY_USERNAME = "your-username"
   TIMEZONE = 0   # hours from UTC
   ```
   That is the username on the app's account screen, not your email and not your
   password. The free save needs no password.
4. Eject the drive, then press RESET. The badge boots back to its menu — pick
   **Study Bunny** from the list.

## Buttons

| State | A | B | C | UP / DOWN |
|---|---|---|---|---|
| Ready | Start | | Sync (Wi-Fi, clock, subjects, retry queue) | Choose subject |
| Studying | Pause | | Stop | Change subject |
| Paused | Resume | | Stop | Change subject |
| Stop? | Back | Drop | Save | Change subject |

You can change the subject at any point, including on the Stop? screen right before
Save. The session is filed under whichever subject is selected when you press Save.

## How a session is recorded

- Time is wall-clock (RTC), so it survives sleep and resets. The running session is
  saved to `/study_bunny.json` on every button press.
- Paused time is excluded. The save stores one block per session (start and end only),
  so the block is `start` to `start + active time`.
- The free save stores .NET ticks of **local** time, so the badge converts using your
  `TIMEZONE`. A session between 12:20 and 12:53 local is stored as 12:20-12:53.
- Sessions under 5 minutes are discarded, same as the app.
- Each saved session also changes `coins`, using the app's own accumulation
  formula: one coin per 10 minutes studied (rounded half up) minus one coin per
  7 minutes paused (rounded down). A long pause can only take that session's own
  earnings, exactly like the app's accumulation page, so it is capped at 0 to
  100 coins per session and never drives the balance negative.
- Subjects come from the newest month in your save's month legends. Press Sync to load
  them. With no subjects the badge uses a single "Study" subject.
- The colour for a session is the colour your subject already has this month, or the
  next unused colour if it is a new subject this month.

## What gets written

The free save is one JSON blob behind `https://www.superbyte.site/account.php`, with
`action=load` and `action=save`. There is no partial update, so for each session the
badge:

1. loads the blob and keeps a copy at `/study_bunny_backup.json` on the badge,
2. adds one block to `stats.sessionBlocks`, one row to this month's legend if needed,
   and credits the session's coins,
3. writes the whole blob back with `action=save`,
4. loads it again and checks the new session is there.

Before writing it also reloads the save and compares it byte for byte, so if you used
the app in the meantime it refuses instead of overwriting your newer progress. It never
touches carrots, items, todo or flashcards. The whole blob is parsed and rebuilt
with the C-level `json` module — about half a second for 62 KB on the badge — rather
than edited as text, because a Python-level text scan on this badge costs around a
millisecond per character, and 60,000 characters of that looks exactly like a hang.

Resubmitting the same session is a no-op, and a session that overlaps an existing
block is dropped (not retried).

## Limits

- **You must press Cloud Load in the Study Bunny app to see badge sessions.**
- Username-only access: `load` and `save` take just the username, with no password.
  Anyone who knows your username can read or overwrite the save. Keep it to yourself
  and do not share the badge.
- The whole blob is overwritten on each save, so the badge's byte-for-byte check is
  the only guard against clobbering an app save made at the same moment. If the badge
  says the save changed, press Sync again.
- Badge sessions credit coins with the app's own formula (10 minutes studied =
  1 coin, 7 minutes paused = 1 coin off). Carrots and streaks still come from the
  app's own timer.
- While a session is running the badge redraws the timer every 10 seconds (FAST_UPDATE,
  woken by the RTC alarm) and does not deep sleep, so expect more battery use than
  idle. The shown time is rounded to the nearest 10 s.

## Look

A big grey Study Bunny sits in the middle on a light dither, using the app's own
artwork (outline, fill and face layers composited and shrunk to 86x131 in
`assets/bunny_*.png`). The face changes with state: neutral when ready, happy while
studying, eyes closed when paused, and frowning on Stop? if the session is too short
to count. Status and clock are top left, subject is top right, timer across the
bunny's tummy, and button hints along the bottom. If the images fail to load the app
draws a plain shape bunny instead.

## Sync speed

The first build really did hang right after the 62 KB download, and live-debugging
the badge over its USB serial console found the cause: it edited the save by scanning
the text character by character, and a Python-level loop with indexing costs about
1.1 ms per character on this badge — a 60,000-character scan takes *minutes*, which
looks exactly like a hang. (The interrupt traceback pinned it to the scan loop, and
`for c in s` is still 32 µs per character; only C-level primitives are fast.)

The current build parses the whole blob with the C-level `json` module instead.
Measured on the badge against the real 62,195-character save: `json.loads` takes
514 ms, `json.dumps` of the edited blob about 600 ms, and a complete load → parse →
plan → edit → re-verify pass runs in a few seconds with over 7 MB of heap free.

A sync is still slow by nature: up to 30 s waiting for Wi-Fi, NTP, and then several
HTTPS round trips that each carry the whole save (~62 KB: load, re-read to check
nobody changed it, upload, verify, subjects). Each request has a 20 s timeout, so a
dead connection ends in an error message ("queued, C on home screen retries") rather
than an endless Syncing... — as long as the badge's `requests` takes a `timeout`
argument. If it does not, the app falls back to no timeout and a stalled connection
can only be escaped with a reset. The screen shows each phase as it goes — Wi-Fi,
Clock, Loading save, Checking for changes, Uploading, Verifying, Subjects — so a
sync that is merely slow keeps changing phase, and one stuck on the same phase for
much longer is hanging.

Nothing is printed normally; the e-ink is the only output. To see what it is doing,
add `STUDYBUNNY_DEBUG = True` to `secrets.py` and open the badge's serial console
(USB, e.g. Thonny). Each phase, every request with its size, each parsing/editing
step with how long it took, and all errors are printed prefixed `[study_bunny]`:

```
[study_bunny] wifi: connected
[study_bunny] load: sending 32 bytes
[study_bunny] reply HTTP 200, 62195 chars
[study_bunny] save loaded, 62195 chars (3670 ms)
[study_bunny] stats parsed (779 ms)
[study_bunny] 628 sessions in save
[study_bunny] colour 7 for Business this month
[study_bunny] edited, 62283 chars (1054 ms)
[study_bunny] backup written, 62195 chars
[study_bunny] load: sending 32 bytes
[study_bunny] reply HTTP 200, 62195 chars
[study_bunny] save: sending 63283 bytes
[study_bunny] load: sending 32 bytes
[study_bunny] reply HTTP 200, 62283 chars
[study_bunny] verified (7912 ms)
[study_bunny] sync done: Uploaded 1 | 9 subjects
```

The four round trips are the download, the nobody-changed-it check, the upload and
the verify read. Every step is logged with its duration, so if a sync ever stalls
again, the console shows exactly where.

## Wi-Fi stuck after a soft reboot

If the badge is soft-rebooted (Thonny's Ctrl-D, or stopping the running app), the
CYW43 Wi-Fi chip can come back half-initialized and every connect call raises
`EPERM` — the console shows `[CYW43] core not in reset` and `HT not ready` lines.
Retrying cannot fix that, so the app refuses immediately with "Wi-Fi chip stuck:
power-cycle the badge, then Sync" instead of hanging through retries. A proper
reset clears the chip; power-cycle the badge if a reset alone does not.

## What the app writes on the badge itself

Only two files: `/study_bunny.json` (small, on every button press) and
`/study_bunny_backup.json` (the 62 KB copy kept before each upload). The rest of
the badge is read-only to the app. If the filesystem is read-only anyway — most
often because the badge's flash is mounted on a computer — both writes are skipped
with a `state write failed` / `backup failed` log line rather than crashing, though
on some firmwares a blocked write can stall instead. Eject the badge's drive from
the computer before syncing.

## Tests

```
cd badge && python3 -m unittest discover -s tests
```

The tests replace `requests`, `wifi`, `rtc`, `badge` and `screen` with fakes, so they
check the logic, the form posts and the JSON edits, but not on-device TLS or the
e-ink drawing.

For a real save blob there is also an offline checker, no network at all:

```
python3 tests/real_save_check.py /path/to/save.json
```

It is the raw body of a `load` reply from `account.php`. The checker feeds it to the
real `FreeCloud.submit` through the fake server, then verifies the edited save: the
session count grows by one, the original blocks are untouched and still in order,
`coins` grows by the session's earnings, everything else (carrots / items / todo /
flashcards) is identical, every key survives the re-serialisation with its value
intact (key order and spacing can change), the subject's colour for the month is
reused or added to the legend, resubmitting the same session is a no-op, and an
overlapping session is refused.
