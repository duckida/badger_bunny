# Study Bunny: cloud API notes

App: `com.superbyte.studybunny` ("Study Bunny: Focus Timer") v60.09, Unity IL2CPP.
Analyzed APK: XAPK from APKPure, base + `arm64-v8a`, dumped with Il2CppDumper.

## TL;DR

| Question | Answer |
| --- | --- |
| Read-only API for study time / stats? | **Yes.** Firebase Auth (email+password) → Firebase RTDB REST read of `users/<uid>/gameData`. No scraping needed. |
| Writable API to start a study session? | **No.** The timer is entirely client-side. There is no server endpoint that starts/stops sessions. You can only read or overwrite the saved history afterwards. |

Both answers rely on the same thing: the app's "Cloud Save" feature is a thin
wrapper over the Firebase Realtime Database, and the Firebase API key is baked
into the APK. There is no private backend API and no server-side session state.

## Firebase project (from `assets/google-services-desktop.json`)

- project id: `study-bunny`
- RTDB: `https://study-bunny-default-rtdb.firebaseio.com`
- API key: `AIzaSyDj1RU2NlVEZxgniTO3KICD50-CVZlMjjw`
- Auth: email/password enabled (bad credentials return `EMAIL_NOT_FOUND`, not
  `OPERATION_NOT_ALLOWED`)

Unauthenticated reads of `/`, `/users`, `/multiplayer` return
`401 {"error":"Permission denied"}`. Reads are owner-scoped by database rules
(`auth.uid === $uid`), so you need to sign in as the account that owns the data.

## Auth flow (REST, no SDK)

```http
POST https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=<API_KEY>
Content-Type: application/json

{"email":"you@example.com","password":"...","returnSecureToken":true}
```

Response contains `idToken` (≈1 h), `localId` (the uid), `refreshToken`.
Refresh with `POST https://securetoken.googleapis.com/v1/token?key=<API_KEY>`,
body `grant_type=refresh_token&refresh_token=...`.

## Read path

```
GET https://study-bunny-default-rtdb.firebaseio.com/users/<uid>/gameData.json?auth=<idToken>
```

Decompiled evidence (`Cloud.<CloudLoad>d__43::MoveNext`):

```
FirebaseAuth.DefaultInstance.CurrentUser.UserId
FirebaseDatabase.DefaultInstance.GetReference("users/" + uid + "/gameData")
Query.GetValueAsync() -> DataSnapshot.GetRawJsonValue()
JsonUtility.FromJson<ExportData>(...)
```

`gameData` is the serialized `ExportData` object:

| field | type | notes |
| --- | --- | --- |
| `coins`, `carrots` | int | currency |
| `redeemedRefPrize`, `refUserRedeemed` | int | referral flags |
| `playerowneditems`, `todo`, `musicowned`, `flashcards` | string | nested JSON strings |
| `savedtimeandusesleft` | int | |
| `stats` | string | **double-encoded JSON**, see below |

`stats` is `StatsData`. Correction from disassembly: `Cloud.<CloudSave>` serializes the static `Script.state` (class `State`, which has `public StatsData stats`), not `ExportData`, so `stats` is a nested object in `gameData`. `studybunny.py` preserves whichever shape it finds (string or object):

```json
{
  "sessionBlocks": [
    {"color": 1, "sessionStarted": 1751500800000, "sessionEnded": 1751504400000}
  ],
  "monthLegends": [
    {"monthYear": 1751328000000,
     "colorsAndSubjectsOfThisMonth": [{"color": 1, "subject": "Math"}]}
  ]
}
```

- times are Unix milliseconds (UTC)
- `color` is the per-session color index chosen in the app; `monthLegends`
  maps color → subject name for the month
- only completed sessions ≥ 5 minutes are recorded (`RecordStats`)
- sessions are written to `PlayerPrefs` locally first; Cloud Save uploads the
  whole blob only when the user presses the button (or the auto-save path runs)

There is also `multiplayer/<uid>` with the public snapshot (`studyhours`,
`publicToggle`, room preview). It is only readable when the user opted in and
the rules allow it; unauthenticated requests are denied.

## Write path

`Cloud.<CloudSave>d__42::MoveNext`:

```
GetReference("users/" + uid + "/gameData")
.SetRawJsonValueAsync(JsonUtility.ToJson(exportData))
```

So the owner can `PUT` a new `gameData` object (or `PATCH` single fields) to the
same URL. That is a genuine write API, but it only edits *stored data*:

- it cannot start, stop, pause, or extend a session;
- the app reads the blob only on "Cloud Load" (and it then restarts);
- if the app later Cloud Saves, local state overwrites your edit;
- writing malformed JSON can break that account's cloud restore (keep a backup).

`RestorePurchase`/`GrantPurchase` additionally write sibling nodes
(`/coins`, `/carrots`, `/perks`), and `MultiplayerScript.SaveSnapshot` writes
`multiplayer/<uid>`; none of these control the timer either.

## Tool

`studybunny.py` (stdlib only) implements the above:

```bash
export STUDYBUNNY_EMAIL=you@example.com        # password is prompted

python3 studybunny.py whoami                   # uid + which nodes exist
python3 studybunny.py stats                    # hours total / per subject / per month
python3 studybunny.py sessions --limit 30      # session list
python3 studybunny.py dump -o cloud.json       # raw gameData backup

# writes are dry-run unless --apply
python3 studybunny.py patch --key coins --value 999 --apply
python3 studybunny.py set-stats --file stats.json --apply
```

Password comes from `$STUDYBUNNY_PASSWORD` or a `getpass` prompt; do not put it
on the command line.

## Caveats

- Tested against the live project only for *unauthenticated* behavior
  (`401`) and auth provider state; no real account credentials were available
  during this analysis, so the authenticated read/write calls are derived from
  the decompiled code, not from a live successful round-trip.
- Firebase RTDB rules are not public; if the owner rules changed, writes may
  return `401`/`403`.
- Nothing here bypasses auth: you still need the account's email/password (or a
  valid refresh token).

## Two cloud saves, not one

The app has two separate cloud features. Do not confuse them:

- **Paid "cloud perk"**: Firebase Auth + RTDB `users/<uid>/gameData`, as described
  above. Guarded by `Cloud.DetectCloudPerk`. Not what most free users have.
- **Free cloud save**: `https://www.superbyte.site/account.php`, form-encoded POST with
  `action=load|save|login|register`, `username`, `password` (login only) and `data`
  (save only). It is one whole `ExportData` JSON blob. `load` and `save` take the
  username with **no password**, so the username alone can read or overwrite a save.

Free save differences from Firebase:

- `stats` is always a JSON **string**, same `sessionBlocks` / `monthLegends` shape.
- Times are .NET `DateTime` ticks (100 ns since 0001-01-01) of **local** wall-clock
  time, not Unix ms. Unix epoch is `621355968000000000` ticks.
- `monthLegends[i].monthYear` is the ticks when that month's legend was created, i.e.
  roughly the first session of the month, not the first of the month.
- Sessions are stored in time order; there is no partial update, so a writer must load
  the whole blob, edit, and save it back.
- `load` returns `notfound` when the account has no save. `save` returns `saved`.

The badge app in `badge/` writes to this free save. Its whole edit path (load, add one
block and one legend row, credit coins, save, verify) has been round-tripped offline
against a real 62 KB, 628-session blob with only `stats` and `coins` changed; see
`badge/tests/real_save_check.py` and `badge/README.md`.
