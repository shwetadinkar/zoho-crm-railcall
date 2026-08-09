#!/usr/bin/env python3
"""Capture real Zoho responses and save them as test fixtures.

Every test in test_handler.py currently runs against mocks I wrote by hand,
which means they assert my assumptions rather than Zoho's behaviour. Two bugs
survived that: 4xx errors were retried five times because I assumed the
platform helpers return a status code when they raise, and convert_lead read
the wrong level of the response. Both were only caught by a live call.

This records the shapes Zoho actually returns, so those assumptions get pinned
to reality once and stop drifting.

Read-only. Nothing here writes to your CRM.

    python3 capture_fixtures.py            # writes fixtures/*.json
    python3 capture_fixtures.py --show     # print instead of writing
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

VAULT = os.path.expanduser("~/.railcall/station/.railcall_workspace/keys.local.json")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Anything matching these key names gets replaced before the fixture is saved.
# Fixtures live in a public repo; org identifiers, addresses and person names
# do not belong in one. This machine's user must never appear in output that
# ships publicly, so first_name/last_name/full_name/name are scrubbed
# unconditionally rather than only where a name happens to say "Indra" -
# leaves profile/role labels like "Administrator" also redacted, which is an
# acceptable shape loss against the alternative of a name check that could
# miss a spelling.
SCRUB_KEYS = {"email", "primary_email", "Email", "Secondary_Email", "phone",
              "Phone", "Mobile", "company_name", "Website", "zgid", "zuid",
              "mc_status", "Owner_Email", "name", "first_name", "last_name",
              "full_name", "domain_name", "primary_zuid", "next_page_token"}


def _token(v):
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": v["refresh_token"],
        "client_id": v["client_id"], "client_secret": v["client_secret"]}).encode()
    r = urllib.request.urlopen(urllib.request.Request(v["token_url"], data=body), timeout=30)
    return json.loads(r.read())["access_token"]


def _scrub(node, id_map=None):
    """Replace identifying values, keep the shape.

    Real record ids get a stable placeholder per unique value (id_1, id_2,
    ...) rather than one placeholder per nesting depth - callers like
    _summarise and scan_changes pair ids with other per-record fields
    (Modified_Time, cursor keys), so two different records collapsing to the
    same fake id would make the fixture useless for exactly the code it's
    meant to pin down.
    """
    if id_map is None:
        id_map = {}
    if isinstance(node, dict):
        out = {}
        for k, val in node.items():
            if k in SCRUB_KEYS and isinstance(val, str):
                if "mail" in k.lower():
                    out[k] = "redacted@example.com"
                elif k == "next_page_token":
                    # A live pagination cursor from a real query - opaque,
                    # unverifiable, and no test needs the actual bytes.
                    out[k] = "token_1"
                else:
                    out[k] = "REDACTED"
            elif k == "id" and isinstance(val, str) and val.isdigit():
                if val not in id_map:
                    id_map[val] = "id_%d" % (len(id_map) + 1)
                out[k] = id_map[val]
            else:
                out[k] = _scrub(val, id_map)
        return out
    if isinstance(node, list):
        return [_scrub(x, id_map) for x in node[:3]]
    return node


def main():
    show = "--show" in sys.argv
    if not os.path.exists(VAULT):
        sys.exit("no vault at " + VAULT)
    v = json.load(open(VAULT))["zoho"]
    tok = _token(v)
    H = {"Authorization": "Zoho-oauthtoken " + tok, "Content-Type": "application/json"}

    def _body_json(raw):
        # A 204 (e.g. GET on a deleted/bad record id) and some error
        # responses come back with an empty or whitespace-only body, which
        # json.loads rejects outright rather than treating as "no body".
        text = raw.decode()
        return json.loads(text) if text.strip() else {}

    def get(path):
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(v["instance_url"] + path, headers=H), timeout=30)
            return r.status, _body_json(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _body_json(e.read())

    def coql(q):
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                v["instance_url"] + "/crm/v8/coql", method="POST",
                data=json.dumps({"select_query": q}).encode(), headers=H), timeout=30)
            return r.status, _body_json(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _body_json(e.read())

    cases = {
        "org":              lambda: get("/crm/v8/org"),
        "fields_leads":     lambda: get("/crm/v8/settings/fields?module=Leads"),
        "users":            lambda: get("/crm/v8/users?type=ActiveConfirmedUsers"),
        "list_leads":       lambda: get("/crm/v8/Leads?fields=Last_Name,Owner&per_page=2"),
        "coql_ok":          lambda: coql("select Last_Name from Leads where Last_Name is not null limit 2"),
        "coql_paged":       lambda: coql("select Last_Name from Contacts where Last_Name is not null limit 0, 2"),
        # the error shapes matter as much as the success ones
        "err_no_where":     lambda: coql("select Last_Name from Leads limit 2"),
        "err_chained_ne":   lambda: coql("select Owner from Deals where Stage != 'Closed Won' and Stage != 'Closed Lost' and Stage != 'X' limit 2"),
        "err_bad_module":   lambda: get("/crm/v8/NotAModule?fields=id"),
        "err_bad_record":   lambda: get("/crm/v8/Leads/1"),
    }

    if not show:
        os.makedirs(OUT, exist_ok=True)

    for name, fn in cases.items():
        try:
            status, body = fn()
        except Exception as e:
            print("%-18s EXCEPTION %s" % (name, e))
            continue
        fixture = {"status": status, "body": _scrub(body)}
        if show:
            print("=== %s (HTTP %s)" % (name, status))
            print(json.dumps(fixture["body"], indent=2)[:700])
        else:
            with open(os.path.join(OUT, name + ".json"), "w") as f:
                json.dump(fixture, f, indent=2, sort_keys=True)
                f.write("\n")
            print("%-18s HTTP %-4s -> fixtures/%s.json" % (name, status, name))

    if not show:
        print("\nCheck the files before committing. The scrubber replaces "
              "emails, phones, company names and ids, but read them anyway.")


if __name__ == "__main__":
    main()
