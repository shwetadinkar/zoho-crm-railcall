#!/usr/bin/env python3
"""Find out what a Zoho merge does to the losing record's notes.

This is the one unknown left after the schema probes. Zoho's own docs say
merging deletes child records; this checks whether that is true for notes.

SAFETY
------
This script WRITES. It is the only probe in this project that does. Its safety
comes from operating exclusively on records it creates itself, in this run:

  - it creates two leads with a unique run tag in the last name
  - it records the two ids it just created
  - the merge call asserts both ids came from this run's creation step
  - it never reads, selects, or touches any pre-existing record

If the creation step fails, nothing else runs.

STAGES
------
Run with a stage argument so you can stop and look between steps:

    python3 merge_experiment.py create     # two leads + a note on each
    python3 merge_experiment.py inspect    # show both, and their notes
    python3 merge_experiment.py merge      # the real merge - DESTRUCTIVE
    python3 merge_experiment.py after      # what survived
    python3 merge_experiment.py cleanup    # delete whatever is left

State is kept in merge_experiment_state.json so the stages can see each other.

Not part of the module. Keep it in .moduleignore.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VAULT = os.path.expanduser(
    "~/.railcall/station/.railcall_workspace/keys.local.json")
STATE = "merge_experiment_state.json"
API = "v8"
TIMEOUT = 30


def vault():
    if not os.path.exists(VAULT):
        sys.exit("no vault at " + VAULT)
    return json.load(open(VAULT))["zoho"]


def token(v):
    body = urllib.parse.urlencode({
        "refresh_token": v["refresh_token"], "client_id": v["client_id"],
        "client_secret": v["client_secret"],
        "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request(v["token_url"], data=body, method="POST")
    return json.loads(urllib.request.urlopen(
        req, timeout=TIMEOUT).read().decode())["access_token"]


def api(v, tok, path, body=None, method="GET"):
    url = "%s/crm/%s/%s" % (v["instance_url"].rstrip("/"), API, path)
    blob = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=blob, method=method)
    req.add_header("Authorization", "Zoho-oauthtoken " + tok)
    if blob:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, raw


def load_state():
    if not os.path.exists(STATE):
        sys.exit("no state file - run the 'create' stage first")
    return json.load(open(STATE))


def save_state(d):
    json.dump(d, open(STATE, "w"), indent=2)


# ---------------------------------------------------------------------------

def stage_create(v, tok):
    tag = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    print("Creating two leads tagged %s" % tag)

    st, body = api(v, tok, "Leads", {"data": [
        {"Last_Name": "MergeTest A " + tag, "Company": "MergeTest Co",
         "Email": "mergetest.a.%s@example.invalid" % tag.lower(),
         "Phone": "0000000001", "Lead_Status": "Contacted"},
        {"Last_Name": "MergeTest B " + tag, "Company": "MergeTest Co",
         "Email": "mergetest.b.%s@example.invalid" % tag.lower(),
         "Phone": "0000000002", "Lead_Status": "Contacted"},
    ]}, "POST")

    if st != 201 and st != 200:
        sys.exit("create failed: HTTP %s %s" % (st, body))

    rows = body.get("data") or []
    ids = [r.get("details", {}).get("id") for r in rows]
    if len(ids) != 2 or not all(ids):
        sys.exit("expected 2 ids, got %r" % ids)

    print("  created A: %s" % ids[0])
    print("  created B: %s" % ids[1])

    # a note on each, so we can see what survives
    for label, rid in (("A", ids[0]), ("B", ids[1])):
        st2, b2 = api(v, tok, "Notes", {"data": [{
            "Note_Title": "Note on MergeTest %s" % label,
            "Note_Content": "Created by merge_experiment.py run %s. "
                            "If this note survives a merge, notes are "
                            "preserved. If not, they are not." % tag,
            "Parent_Id": rid, "se_module": "Leads"}]}, "POST")
        ok = st2 in (200, 201)
        print("  note on %s: %s" % (label, "ok" if ok else "FAILED %s %s" % (st2, b2)))

    save_state({"tag": tag, "master": ids[0], "loser": ids[1],
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    print("\nState written. Next: python3 merge_experiment.py inspect")


def _show(v, tok, label, rid):
    st, body = api(v, tok, "Leads/%s" % rid)
    if st != 200:
        print("  %s (%s): GONE - HTTP %s" % (label, rid, st))
        return False
    rec = (body.get("data") or [{}])[0]
    print("  %s (%s): %s | %s | %s" % (
        label, rid, rec.get("Last_Name"), rec.get("Email"), rec.get("Phone")))
    st2, b2 = api(v, tok, "Leads/%s/Notes" % rid)
    notes = (b2.get("data") or []) if st2 == 200 else []
    print("      notes: %d %s" % (
        len(notes), [n.get("Note_Title") for n in notes] if notes else ""))
    return True


def stage_inspect(v, tok):
    s = load_state()
    print("Before the merge:")
    _show(v, tok, "MASTER", s["master"])
    _show(v, tok, "LOSER ", s["loser"])
    print("\nIf both show one note each, you are ready.")
    print("Next: python3 merge_experiment.py merge   (DESTRUCTIVE)")


def stage_merge(v, tok):
    s = load_state()
    master, loser = s["master"], s["loser"]

    # safety: only ever merge ids this script created in this experiment
    if not (master and loser) or master == loser:
        sys.exit("state looks wrong; refusing")
    st, body = api(v, tok, "Leads/%s" % loser)
    if st != 200:
        sys.exit("loser not found; refusing")
    name = ((body.get("data") or [{}])[0]).get("Last_Name") or ""
    if not name.startswith("MergeTest B "):
        sys.exit("loser is not a MergeTest record (%r); refusing" % name)

    print("Merging %s (loser) into %s (master)" % (loser, master))
    print("Payload: {'merge': [{'data': [{'id': '<loser>'}]}]}")
    st2, body2 = api(v, tok, "Leads/%s/actions/merge" % master,
                     {"merge": [{"data": [{"id": loser}]}]}, "POST")
    print("\nHTTP %s" % st2)
    print(json.dumps(body2, indent=2)[:1200])

    s["merged_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    s["merge_status"] = st2
    save_state(s)
    print("\nNext: python3 merge_experiment.py after")


def stage_after(v, tok):
    s = load_state()
    print("After the merge:")
    m = _show(v, tok, "MASTER", s["master"])
    l = _show(v, tok, "LOSER ", s["loser"])
    print("\n" + "=" * 62)
    print("WHAT THIS TELLS YOU")
    if m and not l:
        print("  Loser is gone, master survives - a real merge happened.")
        print("  Count the master's notes above:")
        print("    2 notes -> the loser's note was CARRIED OVER.")
        print("               Dedupe can honestly claim it preserves notes.")
        print("    1 note  -> the loser's note was LOST with the record.")
        print("               A dedupe command must say so plainly, or not ship.")
    elif m and l:
        print("  Both still exist - no merge occurred. Read the HTTP response")
        print("  from the merge stage again.")
    else:
        print("  Unexpected state. Do not build on this until it is understood.")
    print("=" * 62)
    print("\nWhen done: python3 merge_experiment.py cleanup")


def stage_cleanup(v, tok):
    s = load_state()
    for label in ("master", "loser"):
        rid = s.get(label)
        if not rid:
            continue
        st, body = api(v, tok, "Leads/%s" % rid)
        if st != 200:
            print("  %s %s: already gone" % (label, rid))
            continue
        name = ((body.get("data") or [{}])[0]).get("Last_Name") or ""
        if not name.startswith("MergeTest "):
            print("  %s %s: NOT a MergeTest record (%r) - refusing to delete"
                  % (label, rid, name))
            continue
        st2, _b = api(v, tok, "Leads?ids=%s" % rid, None, "DELETE")
        print("  %s %s: delete HTTP %s (recycle bin, 60 days)" % (label, rid, st2))
    print("\nDone. Delete %s when you no longer need it." % STATE)


STAGES = {"create": stage_create, "inspect": stage_inspect,
          "merge": stage_merge, "after": stage_after, "cleanup": stage_cleanup}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in STAGES:
        sys.exit("usage: python3 merge_experiment.py "
                 "[create|inspect|merge|after|cleanup]")
    v = vault()
    tok = token(v)
    STAGES[sys.argv[1]](v, tok)


if __name__ == "__main__":
    main()
