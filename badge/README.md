# Study Bunny for Badger 2350

A small study timer for the Badgeware Badger. Start, pause and stop a session on the
badge, and it adds the finished session to your **free** Study Bunny cloud save.

Status: the logic passes offline tests against a fake save server, and adding a
session to a real 628-session save works offline in those tests. It has **not** been
run on a real badge yet.

## Install

1. Double-tap RESET so the badge mounts as a USB drive.
2. Copy the `study_bunny/` folder into `apps/` on the drive
   (`apps/study_bunny/__init__.py`, `icon.png` and `assets/`).
   Do not copy `.git` files or `__pycache__`.
3. Open `secrets.py` on the drive. It should already have your Wi-Fi details. Add the
   line from `secrets.example.py`:
   ```python
   STUDYBUNNY_USERNAME = "your-username"
   TIMEZONE = 0   # hours from UTC
   ```
   That is the username on the app's account screen, not your email and not your
   password. The free save needs no password.
4. Eject, reset, and pick **Study Bunny** from the menu.

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
- Subjects come from the newest month in your save's month legends. Press Sync to load
  them. With no subjects the badge uses a single "Study" subject.
- The colour for a session is the colour your subject already has this month, or the
  next unused colour if it is a new subject this month.

## What gets written

The free save is one JSON blob behind `https://www.superbyte.site/account.php`, with
`action=load` and `action=save`. There is no partial update, so for each session the
badge:

1. loads the blob and keeps a copy at `/study_bunny_backup.json` on the badge,
2. adds one block to `stats.sessionBlocks` and, if needed, one row to this month's
   legend,
3. writes the whole blob back with `action=save`,
4. loads it again and checks the new session is there.

Before writing it also reloads and compares a checksum, so if you used the app in the
meantime it refuses instead of overwriting your newer progress. It never touches coins,
carrots, items, todo or flashcards, and it only parses the small month legend rather
than the whole history, to fit in the badge's memory.

Resubmitting the same session is a no-op, and a session that overlaps an existing
block is dropped (not retried).

## Limits

- **You must press Cloud Load in the Study Bunny app to see badge sessions.**
- Username-only access: `load` and `save` take just the username, with no password.
  Anyone who knows your username can read or overwrite the save. Keep it to yourself
  and do not share the badge.
- The whole blob is overwritten on each save, so the badge's checksum check is the only
  guard against clobbering an app save made at the same moment. If the badge says the
  save changed, press Sync again.
- Badge sessions earn no coins, carrots or streaks. Those come from the app's own timer.
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

## Tests

```
cd badge && python3 -m unittest discover -s tests
```

The tests replace `requests`, `wifi`, `rtc`, `badge` and `screen` with fakes, so they
check the logic, the form posts and the JSON edits, but not on-device TLS or the
e-ink drawing.
