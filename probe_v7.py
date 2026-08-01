#!/usr/bin/env python3
"""Probe the live Zoho org for facts a future version would depend on.

READ-ONLY. Every request here is a GET or a COQL SELECT. There is an assertion
that refuses any query not starting with `select`, and no POST is made to any
endpoint that could mutate.

This file is NOT part of the module. It runs outside the station sandbox and
reads the vault directly, the same way capture_fixtures.py does. Keep it in
.moduleignore so it never enters the signed tree.

    python3 probe_v7.py

Nothing is read from the process environment.
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

FINDINGS = []


def note(topic, verdict, detail=""):
    FINDINGS.append((topic, verdict, detail))
    print("  %-9s %s%s" % (verdict, topic, ("  " + detail) if detail else ""))


def vault():
    if not os.path.exists(VAULT):
        sys.exit("no vault at " + VAULT)
    v = json.load(open(VAULT))["zoho"]
    for k in ("refresh_token", "client_id", "client_secret",
              "token_url", "instance_url"):
        if not v.get(k):
            sys.exit("vault entry 'zoho' missing " + k)
    return v


def token(v):
    body = urllib.parse.urlencode({
        "refresh_token": v["refresh_token"],
        "client_id": v["client_id"],
        "client_secret": v["client_secret"],
        "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request(v["token_url"], data=body, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        out = json.loads(r.read().decode())
    if not out.get("access_token"):
        sys.exit("no access_token: %r" % out)
    return out["access_token"]


def get(v, tok, path, params=None):
    """GET only. Returns (status, parsed_or_text). Never raises on 4xx."""
    url = "%s/crm/%s/%s" % (v["instance_url"].rstrip("/"), API, path)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Zoho-oauthtoken " + tok)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode() or "{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, raw
    except Exception as e:                                    # noqa: BLE001
        return 0, str(e)


def coql(v, tok, query):
    """SELECT only. Asserts rather than trusting the caller."""
    assert query.strip().lower().startswith("select"), \
        "probe attempted a non-SELECT: %r" % query
    url = "%s/crm/%s/coql" % (v["instance_url"].rstrip("/"), API)
    req = urllib.request.Request(url, data=json.dumps(
        {"select_query": query}).encode(), method="POST")
    req.add_header("Authorization", "Zoho-oauthtoken " + tok)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode() or "{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, raw
    except Exception as e:                                    # noqa: BLE001
        return 0, str(e)


def why(p):
    if isinstance(p, dict):
        return ("%s %s" % (p.get("code", ""), p.get("message", "")))[:100]
    return str(p)[:100]


# ---------------------------------------------------------------------------

def probe_settings_modules(v, tok):
    """A list_modules command needs this. Current scope set probably lacks it."""
    print("\n[1] settings/modules  (would back a zoho.list_modules command)")
    st, body = get(v, tok, "settings/modules")
    if st == 200:
        mods = body.get("modules") or []
        names = sorted(m.get("api_name") for m in mods
                       if m.get("api_supported") and m.get("api_name"))
        note("settings/modules", "OK", "%d api-supported modules" % len(names))
        print("      " + ", ".join(names[:30]))
        if len(names) > 30:
            print("      ... and %d more" % (len(names) - 30))
    else:
        note("settings/modules", "BLOCKED",
             "HTTP %d - %s" % (st, why(body)))
        print("      Likely needs ZohoCRM.settings.modules.READ, which is not")
        print("      in the module's scope set. Adding a scope is a breaking")
        print("      change for every existing install - worth knowing now.")


def probe_merge(v, tok):
    """Dedupe is only honest if a real merge exists."""
    print("\n[2] Merge endpoint  (dedupe depends on it)")
    print("      Probed with GET on purpose. 404 = route absent.")
    print("      405 = route exists, wrong verb. No POST is made.")
    for path in ("Leads/actions/merge",
                 "Contacts/actions/merge",
                 "settings/modules/Leads/merge"):
        st, body = get(v, tok, path)
        if st == 404:
            note(path, "ABSENT", "404")
        elif st == 405:
            note(path, "EXISTS", "405 Method Not Allowed")
        else:
            note(path, "HTTP %d" % st, why(body))
    print("      If all ABSENT: dedupe can only delete the loser, which orphans")
    print("      its notes and attachments. That is the opposite of what this")
    print("      module stands for, so it must not claim to merge.")


def probe_notes(v, tok):
    """Erasure and dedupe both need to reach a record's notes."""
    print("\n[3] Notes  (erasure and dedupe both need them)")
    st, body = coql(v, tok,
                    "select id, Note_Title from Notes where id is not null limit 1")
    if st in (200, 204):
        note("COQL on Notes", "OK", "%d rows" % len(body.get("data") or []))
    else:
        note("COQL on Notes", "FAIL", "HTTP %d - %s" % (st, why(body)))

    st2, body2 = get(v, tok, "Notes",
                     {"fields": "Note_Title,Parent_Id,se_module", "per_page": 1})
    if st2 in (200, 204):
        rows = body2.get("data") or []
        keys = sorted(rows[0].keys()) if rows else []
        note("GET Notes + parent link", "OK" if rows else "EMPTY",
             "fields: %s" % (keys or "no rows to inspect"))
    else:
        note("GET Notes + parent link", "FAIL",
             "HTTP %d - %s" % (st2, why(body2)))


def probe_attachments(v, tok):
    """Attachments hold PII. Erasure is incomplete without them."""
    print("\n[4] Attachments")
    st, body = coql(v, tok,
                    "select id from Leads where id is not null limit 1")
    rows = (body.get("data") or []) if st == 200 else []
    if not rows:
        note("attachments", "SKIP", "no Leads record to probe against")
        return
    st2, body2 = get(v, tok, "Leads/%s/Attachments" % rows[0]["id"])
    if st2 in (200, 204):
        note("GET Attachments", "OK",
             "%d on the sample record" % len(body2.get("data") or []))
    else:
        note("GET Attachments", "FAIL", "HTTP %d - %s" % (st2, why(body2)))


def probe_hygiene_fields(v, tok):
    """Every one of these field names is a guess until it returns 200."""
    print("\n[5] Hygiene field names  (each is a guess until it answers)")
    probes = [
        ("Leads.Last_Activity_Time",
         "select id from Leads where Last_Activity_Time < "
         "'2026-01-01T00:00:00+05:30' limit 1"),
        ("Leads.Modified_Time",
         "select id from Leads where Modified_Time < "
         "'2026-01-01T00:00:00+05:30' limit 1"),
        ("Deals past close, still open",
         "select id from Deals where Closing_Date < '2026-01-01' and Stage "
         "not in ('Closed Won','Closed Lost') limit 1"),
        ("Leads.Email is null",
         "select id from Leads where Email is null limit 1"),
        ("Leads.Converted__s",
         "select id from Leads where Converted__s = 'false' limit 1"),
    ]
    for label, q in probes:
        st, body = coql(v, tok, q)
        if st == 200:
            note(label, "OK", "%d rows" % len(body.get("data") or []))
        elif st == 204:
            note(label, "OK", "valid, no rows")
        else:
            note(label, "FAIL", "HTTP %d - %s" % (st, why(body)))


def probe_subform_shape(v, tok):
    """Quotes line items are a subform. The module has only handled flat records."""
    print("\n[6] Quote subform shape  (Quoted_Items is type=subform)")
    st, body = coql(v, tok,
                    "select id, Grand_Total from Quotes where id is not null limit 1")
    rows = (body.get("data") or []) if st == 200 else []
    if not rows:
        note("live quote", "NONE",
             "org has no quotes; subform nesting cannot be observed")
        print("      Create one quote by hand in Zoho and re-run to see the")
        print("      real nesting. Until then any subform handling would rest")
        print("      on assumptions - the pattern that hid two bugs before.")
        return
    st2, body2 = get(v, tok, "Quotes/%s" % rows[0]["id"])
    if st2 == 200:
        rec = (body2.get("data") or [{}])[0]
        items = rec.get("Quoted_Items")
        note("live quote", "OK", "Quoted_Items is %s" % type(items).__name__)
        if isinstance(items, list) and items:
            print("      one line item's keys:")
            print("      " + ", ".join(sorted(items[0].keys())))
    else:
        note("GET one quote", "FAIL", "HTTP %d - %s" % (st2, why(body2)))


def main():
    v = vault()
    print("Probing %s  (read-only)" % v["instance_url"])
    tok = token(v)
    print("token OK")

    probe_settings_modules(v, tok)
    probe_merge(v, tok)
    probe_notes(v, tok)
    probe_attachments(v, tok)
    probe_hygiene_fields(v, tok)
    probe_subform_shape(v, tok)

    bad = [f for f in FINDINGS if f[1] in ("FAIL", "ABSENT", "BLOCKED", "NONE")]
    print("\n%d probes, %d came back FAIL / ABSENT / BLOCKED / NONE."
          % (len(FINDINGS), len(bad)))
    for topic, verdict, _d in bad:
        print("  %-9s %s" % (verdict, topic))
    print("\nEach one above is a feature that cannot be built as imagined.")
    print("Paste this whole output before anything gets designed on top of it.")


if __name__ == "__main__":
    main()
