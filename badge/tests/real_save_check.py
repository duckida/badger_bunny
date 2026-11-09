"""Check the badge's save editing against a real save blob, fully offline.

Usage:
    python3 tests/real_save_check.py [path/to/save.json]

The default path is the local backup at ../../.work/backup/free_load_raw.txt.
The blob is the raw body of a `load` reply from account.php: one whole
ExportData JSON object with `stats` as a nested JSON string.

The real FreeCloud.submit runs against the fake server from fakes.py, so no
network is touched. Then the edited save is checked end to end: the session
count grows by one, the original blocks are untouched and still in order,
everything outside `stats` and `coins` is unchanged, coins grow by the
session's earnings, the subject's colour for the month is reused (or added to
the legend), resubmitting the same session is a no-op and an overlapping
session is refused.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fakes  # noqa: E402

TICKS_EPOCH = 621355968000000000
DEFAULT = os.path.abspath(os.path.join(HERE, "..", "..", ".work", "backup", "free_load_raw.txt"))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    with open(path) as f:
        raw = f.read()

    before = json.loads(raw)
    stats = json.loads(before["stats"])
    blocks = stats["sessionBlocks"]
    legends = stats["monthLegends"]

    # A session just after the last one, so it lands in the same month.
    start = (blocks[-1]["sessionEnded"] - TICKS_EPOCH) // 10_000_000 + 60
    end = start + 1800

    # File the session under the subject the last block's colour already
    # means this month, if we can tell; any other subject otherwise.
    newest = max(legends, key=lambda l: int(l.get("monthYear") or 0))
    rows = newest.get("colorsAndSubjectsOfThisMonth") or []
    subject = None
    for row in rows:
        if int(row.get("color") or -1) == blocks[-1]["color"]:
            subject = row.get("subject")
            break
    if not subject and rows:
        subject = rows[0].get("subject")
    subject = subject or "Study"

    db = fakes.FakeDB(raw)
    mod, _, _, _ = fakes.install(db, start)
    cloud = mod.FreeCloud(fakes.USERNAME)

    s_ticks, e_ticks = mod.to_ticks(start), mod.to_ticks(end)
    result = cloud.submit(start, end, subject, 0)
    new_raw = db.blob
    after = json.loads(new_raw)
    new_stats = json.loads(after["stats"])
    new_blocks = new_stats["sessionBlocks"]
    new_block = new_blocks[-1]

    # The month the session lands in, and what the legend said about the
    # subject there before the edit.
    y, m = mod.local_ym(start)
    old_leg = None
    old_colour = None
    for leg in legends:
        my = int(leg.get("monthYear") or 0)
        if my >= TICKS_EPOCH and mod.local_ym(mod.from_ticks(my)) == (y, m):
            old_leg = leg
            for row in leg.get("colorsAndSubjectsOfThisMonth") or []:
                if row.get("subject") == subject:
                    old_colour = int(row.get("color") or 0)
            break

    failed = []
    total = [0]

    def check(name, ok, detail=""):
        total[0] += 1
        print(("PASS " if ok else "FAIL ") + name + (("  " + detail) if detail else ""))
        if not ok:
            failed.append(name)

    check("submit returned ok", result == "ok", repr(result))
    check("exactly one save was posted", db.saved == 1, str(db.saved))
    check("edited blob is valid JSON", isinstance(after, dict))
    check("stats is still a JSON string", isinstance(after["stats"], str))
    check("session count %d -> %d" % (len(blocks), len(blocks) + 1),
          len(new_blocks) == len(blocks) + 1, str(len(new_blocks)))
    check("new block is exactly the submitted session",
          new_block == {"color": new_block.get("color"),
                        "sessionStarted": s_ticks, "sessionEnded": e_ticks},
          repr(new_block))
    check("original blocks are a untouched prefix",
          new_blocks[:-1] == blocks)
    check("blocks still sorted by start",
          all(new_blocks[i]["sessionStarted"] <= new_blocks[i + 1]["sessionStarted"]
              for i in range(len(new_blocks) - 1)))
    check("no overlapping or empty blocks",
          all(b["sessionEnded"] > b["sessionStarted"] for b in new_blocks)
          and all(new_blocks[i]["sessionEnded"] <= new_blocks[i + 1]["sessionStarted"]
                  for i in range(len(new_blocks) - 1)))
    check("no duplicate blocks",
          len({(b["sessionStarted"], b["sessionEnded"]) for b in new_blocks}) == len(new_blocks))
    for key in before:
        if key != "stats" and key != "coins":
            check("field untouched: " + key, after.get(key) == before[key])
    check("coins grow by the session's earnings",
          after.get("coins") == int(before.get("coins") or 0) + mod.coins_for(end - start, 0),
          "%s -> %s" % (before.get("coins"), after.get("coins")))
    # The blob is re-serialised from a parse (key order and formatting may
    # change; values must not), so compare parsed content, not raw bytes.
    check("no keys lost or added in the re-serialised blob",
          set(after) == set(before))
    check("stats re-parses with the same shape",
          set(new_stats) == set(json.loads(before["stats"])))

    # The colour of the new block must be the colour the session month's
    # legend gives the subject afterwards.
    new_leg = None
    new_colour = None
    for leg in new_stats["monthLegends"]:
        my = int(leg.get("monthYear") or 0)
        if my >= TICKS_EPOCH and mod.local_ym(mod.from_ticks(my)) == (y, m):
            new_leg = leg
            for row in leg.get("colorsAndSubjectsOfThisMonth") or []:
                if row.get("subject") == subject:
                    new_colour = int(row.get("color") or 0)
            break
    check("legend now maps the subject to the block colour",
          new_leg is not None and new_colour == new_block["color"],
          "colour %s" % new_block["color"])
    if old_colour is not None:
        check("existing subject colour reused (%d)" % old_colour,
              new_block["color"] == old_colour)
        check("monthLegends untouched", new_stats["monthLegends"] == legends)

    # Resubmitting is a no-op; an overlapping session is refused.
    check("resubmit is a no-op", cloud.submit(start, end, subject, 0) == "dup")
    check("resubmit did not save again", db.saved == 1, str(db.saved))
    check("resubmit left the blob unchanged", db.blob == new_raw)
    try:
        cloud.submit(start + 600, end + 600, subject, 0)
        check("overlap refused", False)
    except mod.Conflict:
        check("overlap refused", True)
    check("overlap refusal did not save", db.saved == 1, str(db.saved))

    print("\n%d checks, %d failed" % (total[0], len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
