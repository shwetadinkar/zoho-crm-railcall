#!/usr/bin/env python3
"""Probe the Zoho merge endpoint's payload shape without merging anything.

WHY THIS IS SAFE
----------------
Merge is destructive: it collapses two records into one and the loser is gone.
So this never sends a real record id. Every id below is fabricated - a
well-formed 19-digit string that does not exist in the org. Zoho validates ids
before it merges, so there is nothing for it to act on.

That is a stronger guarantee than a backup. A backup means damage can be undone;
fabricated ids mean the API has no record to damage.

WHAT IT CAN ANSWER
------------------
  - the request body shape (field names, nesting)
  - whether the endpoint is edition-gated
  - whether it is synchronous or returns a job id
  - which fields are required

WHAT IT CANNOT ANSWER
---------------------
  - what happens to the losing record's notes and attachments
  - whether the merge is reversible
Both need a real merge on throwaway records. That is a separate decision, made
deliberately, on records created for the purpose.

    python3 probe_merge.py

Read-only in effect. Not part of the module; keep it in .moduleignore.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

VAULT = os.path.expanduser(
    "~/.railcall/station/.railcall_workspace/keys.local.json")
API = "v8"
TIMEOUT = 30

# Fabricated. Well-formed shape, guaranteed not to exist. Never replace these
# with real ids: the whole safety argument rests on them being fake.
FAKE_A = "9999999999999999901"
FAKE_B = "9999999999999999902"


def vault():
    if not os.path.exists(VAULT):
        sys.exit("no vault at " + VAULT)
    v = json.load(open(VAULT))["zoho"]
    return v


def token(v):
    body = urllib.parse.urlencode({
        "refresh_token": v["refresh_token"], "client_id": v["client_id"],
        "client_secret": v["client_secret"],
        "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request(v["token_url"], data=body, method="POST")
    return json.loads(urllib.request.urlopen(
        req, timeout=TIMEOUT).read().decode())["access_token"]


def call(v, tok, path, body, method="POST"):
    """Send a deliberately-fake payload and read the rejection."""
    assert FAKE_A[:6] == "999999", "safety: fabricated ids were modified"
    blob = json.dumps(body).encode() if body is not None else None
    url = "%s/crm/%s/%s" % (v["instance_url"].rstrip("/"), API, path)
    req = urllib.request.Request(url, data=blob, method=method)
    req.add_header("Authorization", "Zoho-oauthtoken " + tok)
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
    except Exception as e:                                    # noqa: BLE001
        return 0, str(e)


def show(label, status, body):
    print("\n  --- %s" % label)
    print("      HTTP %s" % status)
    text = json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body)
    for line in text.splitlines()[:14]:
        print("      " + line)


def main():
    v = vault()
    print("Probing merge on %s" % v["instance_url"])
    print("Using FABRICATED ids only: %s / %s" % (FAKE_A, FAKE_B))
    print("Nothing in the org can be merged by this script.\n")
    tok = token(v)
    print("token OK")

    # 1. Empty body. Zoho usually names the field it wanted.
    show("empty body", *call(v, tok, "Leads/actions/merge", {}))

    # 2. The shape the docs imply: ids nested under a master record.
    show("data[] with master + child ids",
         *call(v, tok, "Leads/actions/merge",
               {"data": [{"master": {"id": FAKE_A},
                          "child_ids": [FAKE_B]}]}))

    # 3. Flat variant, in case the wrapper differs.
    show("flat master/child",
         *call(v, tok, "Leads/actions/merge",
               {"master": {"id": FAKE_A}, "child_ids": [FAKE_B]}))

    # 4. Per-record path form, which some Zoho actions use.
    show("per-record path form",
         *call(v, tok, "Leads/%s/actions/merge" % FAKE_A,
               {"data": [{"child_ids": [FAKE_B]}]}))

    # 5. Wrong types on purpose - forces a schema complaint rather than a
    #    lookup failure, which often names every expected field at once.
    show("deliberately wrong types",
         *call(v, tok, "Leads/actions/merge",
               {"data": [{"master": FAKE_A, "child_ids": FAKE_B}]}))

    # 6. Contacts, to see whether the shape is per-module.
    show("Contacts, same shape",
         *call(v, tok, "Contacts/actions/merge",
               {"data": [{"master": {"id": FAKE_A},
                          "child_ids": [FAKE_B]}]}))

    print("\n" + "=" * 68)
    print("HOW TO READ THIS")
    print("  INVALID_DATA / 'id given seems to be invalid'")
    print("      -> the payload was understood; it failed on the fake id.")
    print("         That shape is correct.")
    print("  REQUIRED_PARAM_MISSING / MANDATORY_NOT_FOUND")
    print("      -> shape is wrong; the message usually names what it wanted.")
    print("  OAUTH_SCOPE_MISMATCH")
    print("      -> needs a scope the module does not have.")
    print("  FEATURE_NOT_SUPPORTED / upgrade wording")
    print("      -> edition-gated, like mass_change_owner. Not usable.")
    print("  A job id or 'scheduled' in any response")
    print("      -> asynchronous, which changes the design entirely.")
    print("=" * 68)


if __name__ == "__main__":
    main()
