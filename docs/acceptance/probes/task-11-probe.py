"""Probe for Task 11 (A7): the save-path guard + the degenerate identity_key.

Not a pytest test (the suite stays 7 passed / 1 skipped); a behaviour harness.
Two subcommands:

    export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs \
      AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 \
      PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false

    P=docs/acceptance/probes/task-11-probe.py

    .venv/bin/python $P save
        The real HTTP path through TestClient with the auth dependency
        overridden onto a throwaway trainer row (deleted at the end; the
        collection rows go with it via ON DELETE CASCADE). One binder scan of
        the committed synthetic fixture, then four confirms:
          (a) every card explicitly `confirmed: false`
          (b) one flagged card `confirmed: true`
          (c) an OLD-CLIENT payload — the `confirmed` field absent everywhere
          (d) a second scan + confirm, checking qty increments only for the
              cards that were actually saved.

    .venv/bin/python $P collision
        The degenerate key, at both consumers:
          A1. identity_key() against the expression it replaced, over every
              shape — the divergence is a printed table, not a claim. The only
              REACHABLE change is degenerate -> None; the one other changed
              string ("<set>:" -> "<set>:unknown", for a set with no number and
              a name that normalizes to nothing) is unreachable from
              resolve_identity and is labelled as such.
          A2. the same through resolve_identity itself.
          B. live dedup (live_session.add_frame_result, called directly): two
             DIFFERENT unidentified cards must occupy two rows; two identical
             identified cards must still dedup / raise the duplicate prompt.
             The pre-change behaviour is reproduced in the same run by feeding
             the literal "?:unknown" the old code emitted — which shows the cost
             as well as the win, since the merge and the mis-merge were one path.
          C. the collection upsert: no cell with an empty key right-hand side may
             be filed (whatever its set), and two identical identified cards must
             still dedup.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]     # docs/acceptance/probes/<this> -> repo
PAGE = REPO / "tests" / "corpus" / "binder" / "synthetic_3x3.jpg"

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


# ── trainer fixture (a real row: collection_card.trainer_id is a FK) ──────────
# Each helper builds and disposes its OWN engine: the app's shared one is bound to
# whichever loop first used it, and these run in their own asyncio.run().
async def _sql(stmt: str, params: dict) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.config import db_settings
    url = db_settings().database_url.replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as c:
            from sqlalchemy import text
            await c.execute(text(stmt), params)
    finally:
        await engine.dispose()


def _make_trainer() -> uuid.UUID:
    tid = uuid.uuid4()
    asyncio.run(_sql(
        "INSERT INTO trainer (id, email, hashed_password, is_active, "
        "is_superuser, is_verified, handle, role) VALUES "
        "(:id, :email, 'x', true, false, true, :handle, 'trainer')",
        {"id": tid, "email": f"probe-{tid.hex[:8]}@example.invalid",
         "handle": f"probe_{tid.hex[:8]}"}))
    return tid


def _drop_trainer(tid: uuid.UUID) -> None:
    asyncio.run(_sql("DELETE FROM trainer WHERE id = :id", {"id": tid}))


# ── A. the save guard, over HTTP ─────────────────────────────────────────────
def cmd_save() -> None:
    from fastapi.testclient import TestClient

    import app.main as main_mod
    from app.db.users import current_active_user

    tid = _make_trainer()

    class _T:
        id = tid
    main_mod.app.dependency_overrides[current_active_user] = lambda: _T()
    try:
        with TestClient(main_mod.app) as client:
            print("=== POST /scan/binder (synthetic_3x3.jpg) ===")
            page = PAGE.read_bytes()
            r = client.post("/scan/binder",
                            files={"page": ("page.jpg", page, "image/jpeg")})
            assert r.status_code == 200, r.text
            cards = r.json()["cards"]

            def flagged(c: dict) -> bool:
                return bool(c.get("needs_review")) or c.get("low_confidence_reason") is not None

            flags = [c["row_index"] for c in cards if flagged(c)]
            confident = [c["row_index"] for c in cards if not flagged(c)]
            print(f"  {len(cards)} cells: confident={confident} flagged={flags}")
            for c in cards:
                if flagged(c):
                    print(f"    flagged row {c['row_index']}: name={c['name']!r} "
                          f"number={c['card_number']!r} set={c['set_code']!r} "
                          f"reason={c['low_confidence_reason']!r}")

            def strip(cs: list[dict]) -> list[dict]:
                """An OLD-CLIENT payload: exactly the scan response, no new field."""
                return [dict(c) for c in cs]

            def get_rows() -> list[dict]:
                g = client.get("/collection")
                assert g.status_code == 200, g.text
                return g.json()["cards"]

            # (a) every card explicitly unconfirmed ---------------------------
            print("\n=== (a) confirm with every card confirmed:false ===")
            body = {"cards": [dict(c, confirmed=False) for c in cards]}
            out = client.post("/collection", json=body).json()
            rows = get_rows()
            print(f"  {out['added']} added, {out['incremented']} incremented, "
                  f"skipped={out['skipped']} rows={out['skipped_rows']}")
            check(out["added"] == len(confident),
                  f"(a) confident cells saved ({out['added']} == {len(confident)})")
            check(sorted(out["skipped_rows"]) == sorted(flags),
                  f"(a) skipped rows are exactly the flagged ones {out['skipped_rows']}")
            check(out["skipped"] == len(flags), "(a) skipped count reported")
            check(len(rows) == len(confident), "(a) collection holds only the confident cells")
            check(all(r["qty"] == 1 for r in rows), "(a) every saved row qty=1")

            # (b) one flagged card confirmed ---------------------------------
            print("\n=== (b) one flagged card confirmed:true ===")
            # Pick a flagged card that actually has an identity to file under —
            # a cell with no set, no number and no name is refused whatever the
            # user says, which is section C's subject, not this one.
            target = next((c for c in cards if flagged(c)
                           and (c["name"] or c["card_number"] or c["set_code"])), None)
            if target is None:
                check(False, "(b) no flagged cell with any identity on this fixture")
            else:
                print(f"  confirming row {target['row_index']} ({target['name']!r})")
                body = {"cards": [dict(c, confirmed=(c["row_index"] == target["row_index"]))
                                  for c in cards]}
                out = client.post("/collection", json=body).json()
                rows = get_rows()
                print(f"  {out['added']} added, {out['incremented']} incremented, "
                      f"skipped={out['skipped']} rows={out['skipped_rows']}")
                check(out["added"] == 1, "(b) the confirmed flagged card was ADDED")
                check(out["incremented"] == len(confident),
                      "(b) the already-saved confident cells incremented")
                check(target["row_index"] not in out["skipped_rows"],
                      "(b) the confirmed card is not in skipped_rows")
                check(len(rows) == len(confident) + 1,
                      "(b) collection grew by exactly the confirmed card")

            # (c) old-client payload: no `confirmed` field anywhere -----------
            print("\n=== (c) old-client payload (no confirmed field) ===")
            before = {r["identity_key"]: r["qty"] for r in get_rows()}
            out = client.post("/collection", json={"cards": strip(cards)}).json()
            after = {r["identity_key"]: r["qty"] for r in get_rows()}
            print(f"  {out['added']} added, {out['incremented']} incremented, "
                  f"skipped={out['skipped']} rows={out['skipped_rows']}")
            check(out["added"] == 0, "(c) nothing new added by an old client")
            check(sorted(out["skipped_rows"]) == sorted(flags),
                  "(c) flagged cells skipped (absent field reads as not-confirmed)")
            check(out["incremented"] == len(confident),
                  "(c) the confident cells still saved")
            # The row (b) added is flagged and unconfirmed here, so it must NOT move.
            moved = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
            check(len(moved) == len(confident),
                  f"(c) only the confident rows' qty moved ({len(moved)})")

            # (d) re-scan + confirm again ------------------------------------
            print("\n=== (d) re-scan the same page, confirm again ===")
            r2 = client.post("/scan/binder",
                             files={"page": ("page.jpg", page, "image/jpeg")})
            assert r2.status_code == 200, r2.text
            cards2 = r2.json()["cards"]
            before = {r["identity_key"]: r["qty"] for r in get_rows()}
            out = client.post("/collection", json={"cards": strip(cards2)}).json()
            after = {r["identity_key"]: r["qty"] for r in get_rows()}
            bumped = [k for k in before if after[k] == before[k] + 1]
            still = [k for k in before if after[k] == before[k]]
            print(f"  {out['added']} added, {out['incremented']} incremented, "
                  f"skipped={out['skipped']}")
            print(f"  qty +1: {len(bumped)}   unchanged: {len(still)} {still}")
            check(out["added"] == 0, "(d) re-scan adds no new rows")
            check(len(bumped) == len(confident),
                  "(d) qty incremented for the confident (saved) cards only")
            check(len(still) == 1,
                  "(d) the flagged row saved in (b) did NOT increment (unconfirmed now)")
    finally:
        main_mod.app.dependency_overrides.pop(current_active_user, None)
        _drop_trainer(tid)


# ── B. the degenerate key ────────────────────────────────────────────────────
def _old_key(set_code, set_name, numerator, name) -> str:
    """The expression this task replaced, verbatim, for the A/B below."""
    from app.pack.name_index import normalize_name
    return f"{set_code or set_name or '?'}:{numerator or normalize_name(name or 'unknown')}"


async def _keys() -> None:
    from app.pack.identify_core import identity_key, resolve_identity
    from app.pack.ocr import NumberReading

    # A1. The key builder itself, against the expression it replaced, over every
    # shape — so the "unchanged except for the degenerate class" claim is a
    # measured table rather than an assertion.
    print("=== A1. identity_key() vs the old expression, all shapes ===")
    shapes = [
        # (set_code, set_name, numerator, name, reachable from resolve_identity?)
        (None, None, None, None, True),          # nothing at all
        (None, None, None, "???", True),         # name normalizes to empty
        (None, None, None, "Pikachu", True),     # name only
        (None, None, "126", None, True),         # number only
        ("SVI", None, "25", "Pikachu", True),    # the everyday confident cell
        ("SVI", None, "25", None, True),         # prior rung: set + number, no name
        ("SVI", None, None, "Pikachu", True),    # set + name
        ("SVI", None, None, None, False),        # set only
        ("SVI", None, None, "???", False),       # set + empty-normalizing name
        (None, "Scarlet & Violet", None, None, False),
    ]
    diverged = []
    for set_code, set_name, num, name, reachable in shapes:
        new = identity_key(set_code, set_name, num, name)
        old = _old_key(set_code, set_name, num, name)
        mark = "" if new == old else ("  <- CHANGED" + ("" if reachable else ", UNREACHABLE"))
        print(f"  set={str(set_code or set_name):18s} num={str(num):5s} "
              f"name={str(name):10s} -> {str(new):20s} (was {old!r}){mark}")
        if new != old:
            diverged.append((set_code or set_name, num, name, old, new, reachable))
    # The degenerate class is SUPPOSED to change; everything else that changed
    # must be unreachable from resolve_identity (see identity_key's docstring).
    surprises = [d for d in diverged if d[4] is not None and d[5]]
    check(not surprises, f"A1 every reachable non-degenerate key is unchanged {surprises}")
    check(all(d[4] is None for d in diverged if d[5]),
          "A1 the only reachable change is degenerate -> None")

    print("\n=== A2. resolve_identity identity_key ===")
    # (name_texts, reading) -> what the key must be
    nothing = await resolve_identity([], None, None)
    print(f"  no name, no number, no set -> {nothing.identity_key!r} "
          f"(was {_old_key(None, None, None, None)!r})")
    check(nothing.identity_key is None, "A2 degenerate cell keys to None")

    garbage = await resolve_identity([("!!! ???", 0.9)], None, None)
    print(f"  unmatchable name        -> {garbage.identity_key!r} "
          f"(was {_old_key(None, None, None, garbage.fields.get('name'))!r})")
    check(garbage.identity_key is None, "A2 unmatched-name cell keys to None")

    # A real name resolves and the key is UNCHANGED from the old expression.
    named = await resolve_identity([("Bronzong", 0.95)], None, None)
    old = _old_key(named.set_code, named.set_name, named.numerator,
                   named.fields.get("name"))
    print(f"  'Bronzong'              -> {named.identity_key!r} (was {old!r})")
    check(named.identity_key == old, "A2 a resolved cell's key is byte-identical")

    # A number, no name: keyed on the number, unchanged.
    numbered = await resolve_identity(
        [], NumberReading(numerator="25", denominator="198", confidence=0.9,
                          pattern_ok=True), None)
    old = _old_key(numbered.set_code, numbered.set_name, numbered.numerator,
                   numbered.fields.get("name"))
    print(f"  number 25/198, no name  -> {numbered.identity_key!r} (was {old!r})")
    check(numbered.identity_key == old, "A2 a numbered cell's key is byte-identical")


def _live() -> None:
    from app.schemas import PackCard
    from app.pack.live_identify import FrameResult
    from app.pack.live_session import DUP_WINDOW_S, LiveSession

    def card(name: str | None, num: str | None) -> PackCard:
        return PackCard(row_index=-1, name=name, card_number=num, confidence=0.3,
                        needs_review=name is None)

    def sess() -> LiveSession:
        return LiveSession(f"probe-{uuid.uuid4().hex[:8]}", str(uuid.uuid4()))

    print("\n=== B. live dedup (add_frame_result, called directly) ===")

    # Two frames, no identity on either, INSIDE the dup window. This one case is
    # both halves of the trade and the print says which:
    #   * the win — if they are two different cards, they are now two rows
    #     instead of one silently merged row;
    #   * the cost — if they were the SAME card held up steady, the refining
    #     merge (keep the better confidence, no new row) is gone and the user
    #     gets a second row to deal with. Accepted: a visible extra row beats a
    #     card that silently disappeared into another card's row, and the merge
    #     was never a same-card guarantee — it was an assumption keyed on a
    #     string that meant "unreadable".
    s = sess()
    e1 = s.add_frame_result(FrameResult("card", card(None, None), None, None, False), b"")
    e2 = s.add_frame_result(FrameResult("card", card(None, None), None, None, False), b"")
    print(f"  two identity-less frames, in-window -> events {e1.event}/{e2.event}, "
          f"{len(s.cards)} rows, keys={[lc.identity_key for lc in s.cards]}")
    print("     = two different cards are kept apart (the fix); two frames of ONE")
    print("       card no longer refine-merge (the accepted cost)")
    check(len(s.cards) == 2 and e2.event == "card",
          "B two identity-less frames occupy two rows (no false dedup; no merge)")
    shutil.rmtree(s._dir(), ignore_errors=True)

    # The SAME inputs with the key the old code produced: one row. That is the
    # old refining merge AND the old false dedup — indistinguishable, which is
    # exactly why the merge could not be kept.
    s = sess()
    s.add_frame_result(FrameResult("card", card(None, None), None, "?:unknown", False), b"")
    e = s.add_frame_result(FrameResult("card", card(None, None), None, "?:unknown", False), b"")
    print(f"  ...the pre-change key               -> event {e.event}, {len(s.cards)} row(s)  "
          f"<- merge and mis-merge were the same code path")
    check(len(s.cards) == 1 and e.event == "card",
          "B (control) the old shared key merged them — the behaviour being fixed")
    shutil.rmtree(s._dir(), ignore_errors=True)

    # Legitimate dedup of the SAME identified card is untouched: inside the
    # window it refines in place, outside it asks the user.
    s = sess()
    s.add_frame_result(FrameResult("card", card("Pikachu", "25/198"), None, "SVI:25", False), b"")
    e = s.add_frame_result(FrameResult("card", card("Pikachu", "25/198"), None, "SVI:25", False), b"")
    print(f"  same identified card    -> event {e.event}, {len(s.cards)} row(s)")
    check(len(s.cards) == 1 and e.event == "card", "B identical identified cards still dedup")
    s.cards[0].captured_at -= DUP_WINDOW_S + 1
    e = s.add_frame_result(FrameResult("card", card("Pikachu", "25/198"), None, "SVI:25", False), b"")
    print(f"  ...a later hold-up      -> event {e.event}, {len(s.cards)} rows")
    check(e.event == "duplicate_prompt", "B a later same-identity hold-up still prompts")
    shutil.rmtree(s._dir(), ignore_errors=True)

    # Two different unidentified cards, the second one arriving LATE: still two
    # rows, and no duplicate prompt about two cards that share nothing.
    s = sess()
    s.add_frame_result(FrameResult("card", card(None, None), None, None, False), b"")
    s.cards[0].captured_at -= DUP_WINDOW_S + 1
    e = s.add_frame_result(FrameResult("card", card(None, None), None, None, False), b"")
    print(f"  unidentified, late      -> event {e.event}, {len(s.cards)} rows")
    check(e.event == "card" and len(s.cards) == 2,
          "B no spurious duplicate prompt between two unidentified cards")
    shutil.rmtree(s._dir(), ignore_errors=True)

    # The row the user explicitly asked to replace still accepts the next card.
    s = sess()
    s.add_frame_result(FrameResult("card", card(None, None), None, None, False), b"")
    s.mark_replaceable(0)
    e = s.add_frame_result(FrameResult("card", card(None, None), None, None, False), b"")
    print(f"  after /replace          -> event {e.event}, {len(s.cards)} row(s)")
    check(len(s.cards) == 1, "B an explicit replace still overwrites the row in place")
    shutil.rmtree(s._dir(), ignore_errors=True)


def _collection() -> None:
    from fastapi.testclient import TestClient

    import app.main as main_mod
    from app.db.users import current_active_user

    print("\n=== C. the collection upsert ===")
    tid = _make_trainer()

    class _T:
        id = tid
    main_mod.app.dependency_overrides[current_active_user] = lambda: _T()
    try:
        with TestClient(main_mod.app) as client:
            def post(cards: list[dict]) -> dict:
                r = client.post("/collection", json={"cards": cards})
                assert r.status_code == 200, r.text
                return r.json()

            def rows() -> list[dict]:
                return client.get("/collection").json()["cards"]

            # Two DIFFERENT cells with no usable identity. Their old keys collide.
            a = {"row_index": 0, "name": "???", "confirmed": True}
            b = {"row_index": 1, "name": "—", "confirmed": True}
            print(f"  old keys: {_old_key(None, None, None, '???')!r} and "
                  f"{_old_key(None, None, None, '—')!r}")
            out = post([a, b])
            print(f"  -> added={out['added']} skipped={out['skipped']} "
                  f"rows={out['skipped_rows']}, collection={len(rows())}")
            check(out["added"] == 0 and out["skipped"] == 2,
                  "C two unusable cells are refused (they cannot share a row)")
            check(len(rows()) == 0, "C nothing was filed under the degenerate key")

            # The literal "unknown" name, which keys to "?:unknown".
            out = post([{"row_index": 0, "name": "unknown", "confirmed": True},
                        {"row_index": 1, "name": "Unknown", "confirmed": True}])
            print(f"  '?:unknown' cells    -> skipped={out['skipped']}, "
                  f"collection={len(rows())}")
            check(out["skipped"] == 2 and len(rows()) == 0,
                  "C the '?:unknown' shape is refused too")

            # A KNOWN set with nothing else: "SVI:" / "Scarlet & Violet:" are
            # per-set shared buckets, the same damage one set down.
            out = post([{"row_index": 0, "set_code": "SVI", "confirmed": True},
                        {"row_index": 1, "set_code": "SVI", "name": "???",
                         "confirmed": True},
                        {"row_index": 2, "set_name": "Scarlet & Violet",
                         "confirmed": True}])
            print(f"  set-only cells ('SVI:', 'Scarlet & Violet:') -> "
                  f"skipped={out['skipped']} rows={out['skipped_rows']}, "
                  f"collection={len(rows())}")
            check(out["skipped"] == 3 and len(rows()) == 0,
                  "C an empty right-hand side is refused whatever the set is")

            # ...but the set is not required: a partial identity still saves.
            out = post([{"row_index": 0, "name": "Pikachu", "confirmed": True}])
            r = rows()
            print(f"  name-only cell        -> added={out['added']} "
                  f"keys={[x['identity_key'] for x in r]}")
            check(len(r) == 1 and r[0]["identity_key"] == "?:pikachu",
                  "C a partial identity is still persistable (not degenerate)")
            client.delete(f"/collection/{r[0]['id']}")

            # Legitimate dedup: the same identified card twice -> one row, qty 2.
            same = {"row_index": 0, "name": "Pikachu", "card_number": "025/198",
                    "set_code": "SVI", "set_name": "Scarlet & Violet"}
            out = post([same, dict(same, row_index=1)])
            r = rows()
            print(f"  same identified card x2 -> added={out['added']} "
                  f"incremented={out['incremented']} rows={len(r)} qty={r[0]['qty']}")
            check(len(r) == 1 and r[0]["qty"] == 2,
                  "C two identical identified cards still dedup to one row, qty 2")

            # ...and a DIFFERENT card does not join it.
            other = dict(same, row_index=0, name="Bulbasaur", card_number="001/198")
            post([other])
            r = rows()
            print(f"  a different card        -> rows={len(r)} "
                  f"keys={[x['identity_key'] for x in r]}")
            check(len(r) == 2, "C a different identified card gets its own row")
    finally:
        main_mod.app.dependency_overrides.pop(current_active_user, None)
        _drop_trainer(tid)


def cmd_collision() -> int:
    """Each section in its OWN process: the app's async engine binds to the first
    event loop that uses it, and these sections run on three different ones
    (asyncio.run for the ladder, TestClient's for the HTTP one)."""
    import subprocess
    rc = 0
    for section in ("keys", "live", "collection"):
        rc |= subprocess.call([sys.executable, __file__, section])
    return rc


_COMMANDS = {
    "save": cmd_save,
    "collision": cmd_collision,
    "keys": lambda: asyncio.run(_keys()),
    "live": _live,
    "collection": _collection,
}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "save"
    t0 = time.perf_counter()
    rc = _COMMANDS[cmd]() or 0
    if cmd != "collision":
        print(f"\n{'FAILURES: ' + str(failures) if failures else 'ALL CHECKS PASS'} "
              f"({time.perf_counter() - t0:.1f}s)")
    sys.exit(rc or (1 if failures else 0))
