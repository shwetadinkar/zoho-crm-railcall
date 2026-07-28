"""shweta/zoho-crm 0.2.2

Vault entry `zoho`:

    {"refresh_token": "1000....",
     "client_id":     "1000....",
     "client_secret": "....",
     "token_url":     "https://accounts.zoho.in/oauth/v2/token",
     "instance_url":  "https://www.zohoapis.in"}

Zoho is region-sharded. A token minted on the India DC will not authenticate
against .com, so the two URLs above carry the routing and there is no
datacenter table in this file.

Credentials come from the vault helper. Nothing here touches os.environ.

The retry policy in _call is deliberately asymmetric. Reasoning is there.

PUT: __rc_helpers__ ships GET, POST, PATCH and DELETE. Zoho's record update
wants PUT, so _put does that one by hand.
"""

import json as _json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API_VERSION = "v8"
MAX_ATTEMPTS = 5
TIMEOUT = 30

# Zoho has no idempotency header. A create that 5xx'd may still have landed, so
# retrying it duplicates the record.
_NON_IDEMPOTENT = {"POST", "PUT", "DELETE"}

# v8 makes `fields` mandatory on record reads. These are the sensible defaults;
# anything not listed falls back to asking the metadata API.
_DEFAULT_FIELDS = {
    "Leads":    ["Last_Name", "First_Name", "Email", "Company", "Phone",
                 "Lead_Status", "Lead_Source", "Owner", "Modified_Time"],
    "Contacts": ["Last_Name", "First_Name", "Email", "Phone", "Account_Name",
                 "Title", "Owner", "Modified_Time"],
    "Accounts": ["Account_Name", "Website", "Phone", "Industry",
                 "Billing_City", "Billing_Country", "Owner", "Modified_Time"],
    "Deals":    ["Deal_Name", "Account_Name", "Stage", "Amount",
                 "Closing_Date", "Probability", "Owner", "Modified_Time"],
}


def _origin(stamp):
    """What initiated this write, copied onto the receipt.

    A receipt saying a record changed is worth less than one saying who or what
    changed it. The stamp's shape isn't documented, so take whatever keys are
    there and don't fall over when it's empty.
    """
    info = {"initiated_via": "railcall-airlock"}
    if isinstance(stamp, dict):
        for key in ("actor", "agent", "origin", "source", "initiated_by",
                    "approved_by", "run_id", "trace_id", "ts", "timestamp"):
            value = stamp.get(key)
            if value not in (None, "", [], {}):
                info[key] = value
    elif isinstance(stamp, str) and stamp.strip():
        info["stamp"] = stamp.strip()
    return info


def _auth():
    """Access token plus the API host for this org.

    oauth_refresh does the minting, caching and clock skew. Don't reimplement it.
    """
    helpers = __rc_helpers__  # noqa: F821
    token = helpers["oauth_refresh"]("zoho")
    access = str(token.get("access_token") or "").strip()
    if not access:
        raise RuntimeError("zoho oauth_refresh returned no access_token")
    base = str(token.get("instance_url") or "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            "zoho vault entry has no instance_url. Set it to your datacenter's "
            "API host, e.g. https://www.zohoapis.in for an Indian org.")
    return access, base, {"Authorization": "Zoho-oauthtoken " + access}


def _url(base, path, params=None):
    url = "%s/crm/%s/%s" % (base, API_VERSION, path.lstrip("/"))
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    return url


def _decode(status, raw, label):
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    if status == 204 or not text.strip():
        return {}
    try:
        parsed = _json.loads(text)
    except ValueError:
        raise RuntimeError("Zoho returned non-JSON for %s (HTTP %s): %s"
                           % (label, status, text[:300]))
    if status >= 400:
        raise RuntimeError("Zoho API returned HTTP %s for %s: %s"
                           % (status, label, text[:400]))
    return parsed


def _put(url, obj, headers):
    data = _json.dumps(obj).encode("utf-8")
    hdrs = {"Content-Type": "application/json",
            "User-Agent": "RailCall-Module/zoho-crm"}
    hdrs.update(headers or {})
    request = urllib.request.Request(url, data=data, method="PUT", headers=hdrs)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _status_from_error(error):
    """Dig an HTTP status out of a helper exception.

    The helpers raise instead of returning when the status is bad. Without this
    a 401 looks exactly like a dropped connection and gets retried five times,
    which is what the first live run did before this existed.
    """
    code = getattr(error, "code", None)
    if isinstance(code, int) and 100 <= code < 600:
        return code
    match = re.search(r"\bHTTP\s*(?:Error\s*)?(\d{3})\b", str(error))
    return int(match.group(1)) if match else None


def _error_body(error):
    read = getattr(error, "read", None)
    if callable(read):
        try:
            return read()
        except Exception:
            pass
    return str(error).encode("utf-8", "replace")


def _client_error(status, raw, label):
    """4xx is the server saying no. Explain it and move on; don't retry it."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    hint = ""
    if "OAUTH_SCOPE_MISMATCH" in text:
        hint = (" The refresh token lacks the scope this endpoint needs. Re-mint "
                "it with the scopes listed in the README.")
    elif status == 401:
        hint = (" Token rejected. Check that instance_url matches the datacenter "
                "the token was minted in.")
    elif status == 404:
        hint = " Check the module api_name and the record id."
    return "Zoho API returned HTTP %s for %s.%s Response: %s" % (
        status, label, hint, text[:300])


def _call(method, path, params=None, body=None):
    """Talk to Zoho, with retries that depend on what actually went wrong.

    Four cases, and they are not interchangeable:

    No status at all means we never got an answer, so a write might have landed
    anyway. 429 means Zoho threw it out before doing anything, so retrying is
    always safe. 5xx on a write might have committed, and with no idempotency
    key a retry duplicates, so reads retry and writes don't. Any other 4xx is a
    settled answer and retrying it just wastes five round trips.
    """
    helpers = __rc_helpers__  # noqa: F821
    label = "%s %s" % (method, path)
    idempotent = method not in _NON_IDEMPOTENT

    for attempt in range(MAX_ATTEMPTS):
        access, base, headers = _auth()
        url = _url(base, path, params)
        delay = 2 ** attempt + random.uniform(0, 0.5)
        status = raw = None
        failure = None

        try:
            if method == "GET":
                status, raw = helpers["http_get_json"](url, timeout=TIMEOUT, headers=headers)
            elif method == "POST":
                status, raw = helpers["http_post_json"](url, body or {}, timeout=TIMEOUT, headers=headers)
            elif method == "DELETE":
                status, raw = helpers["http_delete_json"](url, timeout=TIMEOUT, headers=headers)
            elif method == "PUT":
                status, raw = _put(url, body or {}, headers)
            else:
                raise RuntimeError("unsupported method " + method)
        except Exception as error:
            failure = error
            status = _status_from_error(error)
            raw = _error_body(error)

        if status is None:
            if not idempotent:
                raise RuntimeError(
                    "Network error during %s. This is a write so it was not "
                    "retried; the request may already have been applied. Check "
                    "Zoho before running it again. Underlying error: %s"
                    % (label, failure))
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(delay)
                continue
            raise RuntimeError(
                "Could not reach Zoho for %s after %d attempts: %s. Check network "
                "access and that instance_url matches your org's region."
                % (label, MAX_ATTEMPTS, failure))

        if status == 429:
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(delay)
                continue
            raise RuntimeError(
                "Zoho rate limit still failing after %d retries on %s. The org's "
                "daily API credits may be gone. Setup > Developer Space > APIs."
                % (MAX_ATTEMPTS, label))

        if 500 <= status < 600:
            if not idempotent:
                raise RuntimeError(
                    "Zoho returned HTTP %s on %s. This is a write and Zoho has no "
                    "idempotency key, so it was not retried; the change may have "
                    "been applied. Verify in Zoho before re-approving."
                    % (status, label))
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(delay)
                continue
            raise RuntimeError("Zoho returned HTTP %s on %s after %d attempts."
                               % (status, label, MAX_ATTEMPTS))

        if status >= 400:
            raise RuntimeError(_client_error(status, raw, label))

        return _decode(status, raw, label)

    raise RuntimeError("Zoho request failed after %d attempts: %s" % (MAX_ATTEMPTS, label))


def _module_name(inputs):
    name = str(inputs.get("module", "")).strip()
    if not name:
        raise RuntimeError("'module' is required, e.g. Leads, Contacts, Deals.")
    if not name.replace("_", "").isalnum():
        raise RuntimeError("'module' must be a Zoho module api_name, got %r." % name)
    return name


def _record_id(inputs, key="record_id"):
    value = str(inputs.get(key, "")).strip()
    if not value.isdigit():
        raise RuntimeError("'%s' must be a numeric Zoho record id, got %r." % (key, value))
    return value


def _records(inputs, need_id=False):
    records = inputs.get("records")
    if not isinstance(records, list) or not records:
        raise RuntimeError("'records' must be a non-empty array of objects.")
    if len(records) > 100:
        raise RuntimeError("Zoho takes at most 100 records per call, got %d." % len(records))
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise RuntimeError("records[%d] is not an object." % index)
        if need_id and not record.get("id"):
            raise RuntimeError(
                "records[%d] has no 'id'. Updates go by id; use "
                "zoho.search_records to find it first." % index)
    return records


def _summarise(response, action, stamp):
    """Split Zoho's per-record results into what worked and what didn't.

    Zoho sends HTTP 200 even when half the batch failed, so anything that only
    checks the status code silently loses records.
    """
    rows = response.get("data", []) or []
    ok = [r for r in rows if r.get("code") == "SUCCESS"]
    bad = [r for r in rows if r.get("code") != "SUCCESS"]
    if rows and not ok:
        first = bad[0]
        raise RuntimeError("Zoho rejected every record on %s. First error: %s, %s"
                           % (action, first.get("code", "UNKNOWN"),
                              first.get("message", "no message")))
    return {
        "ok": True,
        "action": action,
        "succeeded": len(ok),
        "failed": len(bad),
        "ids": [r.get("details", {}).get("id") for r in ok],
        "errors": [{"code": r.get("code"),
                    "message": r.get("message"),
                    "field": r.get("details", {}).get("api_name")} for r in bad],
        "origin": _origin(stamp),
    }


def _resolve_fields(module, requested):
    if requested:
        return ",".join(str(f) for f in requested)
    if module in _DEFAULT_FIELDS:
        return ",".join(_DEFAULT_FIELDS[module])
    meta = _call("GET", "settings/fields", params={"module": module})
    names = [f["api_name"] for f in meta.get("fields", []) if f.get("api_name")]
    if not names:
        raise RuntimeError("No readable fields on '%s'. Pass an explicit 'fields' "
                           "list." % module)
    return ",".join(names[:50])


# --- reads -----------------------------------------------------------------

def zoho_verify_connection(inputs, stamp):
    """Prove the token works and say what we can see.

    Probes a scope the module already needs, so it can't fail on an optional
    one. Org details want ZohoCRM.org.READ; without it this still succeeds and
    says why the org fields are blank.
    """
    _, base, _ = _auth()

    probe = _call("GET", "settings/fields", params={"module": "Leads"})
    field_count = len(probe.get("fields") or [])

    org_name = org_id = country = primary_email = ""
    org_note = ""
    try:
        response = _call("GET", "org")
        orgs = response.get("org", []) or []
        if orgs:
            org = orgs[0]
            org_name = org.get("company_name") or ""
            org_id = str(org.get("id") or "")
            country = org.get("country") or ""
            primary_email = org.get("primary_email") or ""
        else:
            org_note = "Zoho returned no org record."
    except RuntimeError as error:
        text = str(error)
        if "OAUTH_SCOPE_MISMATCH" in text or "HTTP 401" in text:
            org_note = ("org details unavailable, token lacks the optional "
                        "ZohoCRM.org.READ scope. Auth itself is fine.")
        else:
            raise

    return {
        "ok": True,
        "authenticated": True,
        "api_domain": base,
        "api_version": API_VERSION,
        "leads_field_count": field_count,
        "org_name": org_name,
        "org_id": org_id,
        "country": country,
        "primary_email": primary_email,
        "note": org_note,
    }, None


def zoho_describe_module(inputs, stamp):
    """Every field on a module, custom ones included. Call this before writing."""
    module = _module_name(inputs)
    response = _call("GET", "settings/fields", params={"module": module})
    fields = [{
        "api_name": f.get("api_name"),
        "label": f.get("field_label"),
        "type": f.get("data_type"),
        "required": bool(f.get("system_mandatory")),
        "read_only": bool(f.get("read_only")),
        "picklist_values": [p.get("actual_value")
                            for p in (f.get("pick_list_values") or [])] or None,
    } for f in response.get("fields", [])]
    return {"ok": True, "module": module, "field_count": len(fields),
            "fields": fields}, None


def zoho_search_records(inputs, stamp):
    """COQL, read only.

    Zoho requires a WHERE clause. Without one you get a bare 400 SYNTAX_ERROR
    that doesn't say so.
    """
    query = str(inputs.get("query", "")).strip()
    if not query:
        raise RuntimeError("'query' is required, a COQL SELECT statement.")
    if not query.lower().lstrip("( ").startswith("select"):
        raise RuntimeError("zoho.search_records is read-only and takes SELECT "
                           "only. Use the write commands to change data.")
    response = _call("POST", "coql", body={"select_query": query})
    rows = response.get("data", []) or []
    return {"ok": True, "records": rows, "count": len(rows),
            "more_records": bool((response.get("info") or {}).get("more_records"))}, None


def zoho_list_records(inputs, stamp):
    module = _module_name(inputs)
    per_page = int(inputs.get("per_page") or 50)
    if not 1 <= per_page <= 200:
        raise RuntimeError("'per_page' must be between 1 and 200.")
    response = _call("GET", module, params={
        "fields": _resolve_fields(module, inputs.get("fields")),
        "page": int(inputs.get("page") or 1),
        "per_page": per_page,
        "sort_by": inputs.get("sort_by"),
        "sort_order": inputs.get("sort_order") or "desc",
    })
    rows = response.get("data", []) or []
    info = response.get("info", {}) or {}
    return {"ok": True, "module": module, "records": rows, "count": len(rows),
            "more_records": bool(info.get("more_records")),
            "page": info.get("page")}, None


def zoho_get_record(inputs, stamp):
    module = _module_name(inputs)
    record_id = _record_id(inputs)
    params = {}
    if inputs.get("fields"):
        params["fields"] = ",".join(str(f) for f in inputs["fields"])
    response = _call("GET", "%s/%s" % (module, record_id), params=params)
    rows = response.get("data", []) or []
    if not rows:
        raise RuntimeError("No record %s in %s. It may be deleted or in the "
                           "recycle bin." % (record_id, module))
    return {"ok": True, "module": module, "record": rows[0]}, None


def zoho_list_users(inputs, stamp):
    """Users and their ids. Ownership fields take an id, not a name or email."""
    user_type = str(inputs.get("type") or "ActiveConfirmedUsers").strip()
    valid = {"AllUsers", "ActiveUsers", "DeactiveUsers", "ConfirmedUsers",
             "NotConfirmedUsers", "DeletedUsers", "ActiveConfirmedUsers",
             "AdminUsers", "ActiveConfirmedAdmins", "CurrentUser"}
    if user_type not in valid:
        raise RuntimeError("'type' must be one of: %s, got %r."
                           % (", ".join(sorted(valid)), user_type))
    per_page = int(inputs.get("per_page") or 200)
    if not 1 <= per_page <= 200:
        raise RuntimeError("'per_page' must be between 1 and 200.")
    try:
        response = _call("GET", "users", params={
            "type": user_type, "page": int(inputs.get("page") or 1),
            "per_page": per_page})
    except RuntimeError as error:
        # This endpoint needs a scope the record endpoints don't. Say so rather
        # than passing a bare 401 up.
        if "OAUTH_SCOPE_MISMATCH" in str(error) or "401" in str(error):
            raise RuntimeError(
                "Zoho rejected the users endpoint. The refresh token most likely "
                "lacks ZohoCRM.users.READ. Add it in the API console and mint a "
                "new refresh token. The other commands are unaffected.")
        raise
    users = [{"id": u.get("id"), "full_name": u.get("full_name"),
              "email": u.get("email"), "role": (u.get("role") or {}).get("name"),
              "profile": (u.get("profile") or {}).get("name"),
              "status": u.get("status")} for u in (response.get("users") or [])]
    return {"ok": True, "type": user_type, "users": users,
            "count": len(users)}, None


# --- writes, all gated by the airlock --------------------------------------

def zoho_create_record(inputs, stamp):
    module = _module_name(inputs)
    body = {"data": _records(inputs)}
    if isinstance(inputs.get("trigger"), list):
        body["trigger"] = inputs["trigger"]
    return _summarise(_call("POST", module, body=body),
                      "create " + module, stamp), None


def zoho_update_record(inputs, stamp):
    module = _module_name(inputs)
    body = {"data": _records(inputs, need_id=True)}
    if isinstance(inputs.get("trigger"), list):
        body["trigger"] = inputs["trigger"]
    return _summarise(_call("PUT", module, body=body),
                      "update " + module, stamp), None


def zoho_upsert_records(inputs, stamp):
    """Insert, or update in place when duplicate_check_fields match. Re-run safe."""
    module = _module_name(inputs)
    body = {"data": _records(inputs)}
    if inputs.get("duplicate_check_fields"):
        body["duplicate_check_fields"] = list(inputs["duplicate_check_fields"])
    return _summarise(_call("POST", "%s/upsert" % module, body=body),
                      "upsert " + module, stamp), None


def zoho_delete_record(inputs, stamp):
    """Recycle bin, not purge. Recoverable for 60 days."""
    module = _module_name(inputs)
    ids = inputs.get("record_ids")
    if not isinstance(ids, list) or not ids:
        raise RuntimeError("'record_ids' must be a non-empty array of ids.")
    if len(ids) > 100:
        raise RuntimeError("At most 100 ids per delete call, got %d." % len(ids))
    bad = [i for i in ids if not str(i).strip().isdigit()]
    if bad:
        raise RuntimeError("These record_ids are not numeric Zoho ids: %r" % bad[:5])
    response = _call("DELETE", module,
                     params={"ids": ",".join(str(i).strip() for i in ids)})
    return _summarise(response, "delete " + module, stamp), None


def zoho_convert_lead(inputs, stamp):
    lead_id = _record_id(inputs, "lead_id")
    payload = {}
    if inputs.get("assign_to"):
        payload["assign_to"] = str(inputs["assign_to"])
    deal = inputs.get("deal")
    if isinstance(deal, dict) and deal:
        missing = [k for k in ("Deal_Name", "Closing_Date", "Stage") if not deal.get(k)]
        if missing:
            raise RuntimeError(
                "'deal' needs %s. Closing_Date is YYYY-MM-DD and Stage has to "
                "match a stage in your pipeline." % ", ".join(missing))
        payload["Deals"] = deal

    response = _call("POST", "Leads/%s/actions/convert" % lead_id,
                     body={"data": [payload]})
    rows = response.get("data", []) or []
    if not rows:
        raise RuntimeError("Zoho returned no conversion result for lead %s. Check "
                           "it exists and hasn't already been converted." % lead_id)

    row = rows[0]
    if row.get("code") not in (None, "SUCCESS"):
        raise RuntimeError("Zoho refused to convert lead %s: %s, %s"
                           % (lead_id, row.get("code"), row.get("message", "")))

    # The new records come back under `details`, one object per module, each
    # with its own name and id. Deals is null unless a deal was requested.
    # Checked against the live v8 endpoint.
    details = row.get("details") or {}

    def _created(module):
        entry = details.get(module)
        if isinstance(entry, dict):
            return str(entry.get("id") or ""), str(entry.get("name") or "")
        return "", ""

    contact_id, contact_name = _created("Contacts")
    account_id, account_name = _created("Accounts")
    deal_id, deal_name = _created("Deals")

    return {"ok": True,
            "lead_id": lead_id,
            "contact_id": contact_id,
            "contact_name": contact_name,
            "account_id": account_id,
            "account_name": account_name,
            "deal_id": deal_id,
            "deal_name": deal_name,
            "message": row.get("message", ""),
            "origin": _origin(stamp)}, None


def zoho_add_note(inputs, stamp):
    module = _module_name(inputs)
    record_id = _record_id(inputs)
    content = str(inputs.get("content", "")).strip()
    if not content:
        raise RuntimeError("'content' is required, the note body can't be empty.")
    note = {"Note_Content": content[:32000]}
    if inputs.get("title"):
        note["Note_Title"] = str(inputs["title"])
    response = _call("POST", "%s/%s/Notes" % (module, record_id),
                     body={"data": [note]})
    return _summarise(response, "add note to %s/%s" % (module, record_id),
                      stamp), None
