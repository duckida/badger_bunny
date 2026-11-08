# Study Bunny for Badger 2350

A small study timer for the Badgeware Badger. Start, pause and stop a session on the
badge, and it adds the finished session to your Study Bunny cloud save.

Status: the logic passes offline tests against a fake Firebase and fake badge. It has
**not** been run on a real badge or against a real account yet.

## Install

1. Double-tap RESET so the badge mounts as a USB drive.
2. Copy the `study_bunny/` folder into `apps/` on the drive (`apps/study_bunny/__init__.py`, `icon.png` and `assets/`).
   Do not copy `.git` files or `__pycache__`.
3. Open `secrets.py` on the drive. It should already have your Wi-Fi details. Add the
   lines from `secrets.example.py`:
   ```python
   STUDYBUNNY_EMAIL = "you@example.com"
   STUDYBUNNY_PASSWORD = "your-password"
   TIMEZONE = 0   # hours from UTC; used for the clock and for which month a session counts in
   ```
4. Eject, reset, and pick **Study Bunny** from the menu.

Your password lives only in the badge's `secrets.py`, never in this repo.

## Buttons

| State | A | B | C | UP / DOWN |
|---|---|---|---|---|
| Ready | Start | | Sync (Wi-Fi, clock, subjects, retry queue) | Choose subject |
| Studying | Pause | | Stop | Change subject |
| Paused | Resume | | Stop | Change subject |
| Stop? | Back | Drop | Save | Change subject |

You can change the subject at any point, including on the Stop? screen right before
Save. The session is filed under whichever subject is selected when you press Save.

Stopping pauses the timer first, so time spent on the confirm screen is not counted.

## How a session is recorded

- Time is wall-clock (RTC), so it survives sleep and resets. The running session is
  saved to `/study_bunny.json` on every button press.
- Paused time is excluded. The cloud stores one block per session (start and end only),
  so the block is `start` to `start + active time`. If you paused for 15 min in a
  40 min session, the block ends 40 min after it started, not 55.
- Sessions under 5 minutes are discarded, same as the app.
- Finished sessions go into a queue on the badge. If Wi-Fi or the cloud fails they stay
  queued and upload on the next **Sync** (button C on the ready screen) or next save.
- Subjects come from your cloud `monthLegends`, newest month first. Press Sync to load
  them. With no cloud subjects the badge uses a single "Study" subject.

## What gets written

For each session the badge writes only new paths under
`users/<uid>/gameData/stats`:

- `sessionBlocks/<next index>`
- a `monthLegends` entry, only if the subject has no color this month

It never rewrites `gameData`, so coins, items, todo and flashcards are untouched. It
reads back the new block to confirm the write. Resubmitting the same session is a no-op,
and a session that overlaps an existing block is dropped (not retried).

## Limits

- **You must press Cloud Load in the Study Bunny app, then restart it, to see badge
  sessions.** The app never polls the cloud.
- If the app does a Cloud Save before you Cloud Load, it overwrites the badge's sessions
  with its local copy. Cloud Load first.
- Badge sessions earn no coins, carrots or streaks. Those come from the app's own timer.
- There must already be a cloud save (press Cloud Save in the app once).
- Cloud Save/Load in the app needs its premium cloud perk.
- While a session is running the badge redraws the timer every 10 seconds (FAST_UPDATE,
  woken by the RTC alarm) and does not deep sleep, so expect more battery use than
  idle. The shown time is rounded to the nearest 10 s. Idle screens sleep after
  2 minutes; a running session is kept in flash, so a reset does not lose it.

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
check the logic and the REST calls but not the real e-ink drawing or on-device TLS.
