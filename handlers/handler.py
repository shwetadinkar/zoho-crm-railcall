"""shweta/zoho-crm 0.5.2

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

import hashlib
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
    """What the airlock told us about this invocation, recorded on every write.

    Observed on station v0.40: the stamp is an ISO timestamp string, so this
    ends up as {"initiated_via": ..., "stamp": "2026-07-29T15:22:15Z"}. That is
    less than it sounds. It marks a write as having come through the airlock,
    and nothing here identifies a human apart from an agent, because the
    platform does not currently pass that. Don't read it as attribution.

    The dict branch stays because the stamp is undocumented and may be enriched
    later; if it is, the extra keys land in the receipt without a code change.
    """
    info = {"initiated_via": "railcall-airlock"}
    if isinstance(stamp, dict):
        for key in ("actor", "agent", "origin", "initiated_by", "approved_by",
                    "run_id", "trace_id", "timestamp"):
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
    try:
        token = helpers["oauth_refresh"]("zoho")
    except Exception as error:
        raise RuntimeError(
            "Could not get a Zoho access token: %s\n\n"
            "The vault needs an entry named 'zoho' shaped like this, with the "
            "URLs matching your org's datacenter:\n"
            '  {"refresh_token": "1000....",\n'
            '   "client_id":     "1000....",\n'
            '   "client_secret": "....",\n'
            '   "token_url":     "https://accounts.zoho.in/oauth/v2/token",\n'
            '   "instance_url":  "https://www.zohoapis.in"}\n\n'
            "Swap .in for .com, .eu, .com.au, .jp, .ca or .sa as appropriate. "
            "A token minted in one datacenter will not work against another."
            % error)
    access = str(token.get("access_token") or "").strip()
    if not access:
        raise RuntimeError("zoho oauth_refresh returned no access_token")
    base = str(token.get("instance_url") or "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            "The zoho vault entry has no instance_url. Set it to your "
            "datacenter's API host, for example https://www.zohoapis.in for an "
            "Indian org or https://www.zohoapis.com for a US one.")
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

_SCOPE_PROBES = [
    # scope, what it gates, how to prove it, whether the module needs it
    ("ZohoCRM.settings.fields.READ", ["describe_module", "list_records"],
     ("GET", "settings/fields", {"module": "Leads"}), True),
    ("ZohoCRM.modules.ALL", ["everything that reads or writes records"],
     ("GET", "Leads", {"fields": "Last_Name", "per_page": 1}), True),
    ("ZohoCRM.coql.READ", ["search_records", "plan_update", "plan_delete",
                           "plan_handover", "and every apply_"],
     ("COQL", "select Last_Name from Leads where Last_Name is not null limit 1", None), True),
    ("ZohoCRM.users.READ", ["list_users", "plan_handover", "apply_handover"],
     ("GET", "users", {"type": "CurrentUser"}), True),
    ("ZohoCRM.org.READ", ["org details on verify_connection"],
     ("GET", "org", None), False),
]


def _probe(kind, path, params):
    """Run one capability probe. Returns (ok, detail)."""
    try:
        if kind == "COQL":
            _call("POST", "coql", body={"select_query": path})
        else:
            _call(kind, path, params=params)
        return True, ""
    except RuntimeError as error:
        text = str(error)
        if "OAUTH_SCOPE_MISMATCH" in text or "HTTP 401" in text:
            return False, "scope not granted"
        return False, text[:120]


def zoho_verify_connection(inputs, stamp):
    """Preflight. Checks every scope this module needs and says what is missing.

    Run this first. A missing scope otherwise shows up much later as an
    unrelated command failing with a bare 401, which is the single most
    expensive way to discover a setup problem.
    """
    _, base, _ = _auth()

    scopes, blocked, missing_required = {}, [], []
    for scope, gates, (kind, path, params), required in _SCOPE_PROBES:
        ok, detail = _probe(kind, path, params)
        scopes[scope] = "ok" if ok else (
            "MISSING" if required else "missing (optional)")
        if not ok:
            if required:
                missing_required.append(scope)
                blocked.extend(gates)
            elif detail and detail != "scope not granted":
                scopes[scope] = "error: " + detail

    org_name = org_id = country = ""
    if scopes.get("ZohoCRM.org.READ") == "ok":
        orgs = (_call("GET", "org").get("org") or [{}])
        org_name = orgs[0].get("company_name") or ""
        org_id = str(orgs[0].get("id") or "")
        country = orgs[0].get("country") or ""

    ready = not missing_required
    if ready:
        summary = ("Ready. All required scopes granted on %s%s."
                   % (base, " for " + org_name if org_name else ""))
    else:
        summary = ("Not ready. Missing %s. Re-mint the refresh token in the "
                   "Zoho API console with the full scope list from the README. "
                   "Until then these will fail: %s."
                   % (", ".join(missing_required), ", ".join(sorted(set(blocked)))))

    return {
        "ok": True,
        "ready": ready,
        "authenticated": True,
        "api_domain": base,
        "api_version": API_VERSION,
        "scopes": scopes,
        "blocked_commands": sorted(set(blocked)),
        "org_name": org_name,
        "org_id": org_id,
        "country": country,
        "summary": summary,
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


# --- plan / apply -----------------------------------------------------------
#
# The airlock binds an approval to the inputs a human saw. It cannot know
# whether the records those inputs point at changed while the approval was
# sitting there. On a bulk update that gap matters: you approve a diff over 80
# records, someone edits nine of them, and the write lands on state nobody
# reviewed.
#
# plan_update snapshots the fields it is about to change and hashes them.
# apply_update re-reads the same records, re-hashes, and refuses if the hash
# moved. The snapshot also doubles as the rollback record, since it holds every
# prior value.

_PLAN_FILE = "zoho_plans.json"
_PLAN_TTL = 3600  # a plan older than an hour is stale by definition


def _plan_key(module, query, changes):
    """Identify a plan by what it does, not by a token.

    Nothing about a plan can be handed back through a receipt: the platform
    redacts identifiers before sealing, so ids and hashes come back as
    '[account]'. Keying on the request itself means apply re-supplies the same
    module, query and changes a human already approved, and the module looks up
    its own stored fingerprint.
    """
    blob = _json.dumps({"module": module,
                        "query": " ".join(str(query).lower().split()),
                        "changes": changes},
                       sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _plans_path():
    helpers = __rc_helpers__  # noqa: F821
    return str(helpers.get("WS") or "").rstrip("/") + "/" + _PLAN_FILE


def _plan_save(key, record):
    helpers = __rc_helpers__  # noqa: F821
    path = _plans_path()
    store = helpers["jload"](path, {}) or {}
    cutoff = time.time() - _PLAN_TTL
    store = {k: v for k, v in store.items()
             if isinstance(v, dict) and float(v.get("ts") or 0) > cutoff}
    record["ts"] = time.time()
    store[key] = record
    helpers["jsave"](path, store)


def _plan_load(key):
    helpers = __rc_helpers__  # noqa: F821
    store = helpers["jload"](_plans_path(), {}) or {}
    plan = store.get(key)
    if not isinstance(plan, dict):
        return None
    if time.time() - float(plan.get("ts") or 0) > _PLAN_TTL:
        return None
    return plan


def _fingerprint(module, rows, fields):
    """Hash of the current values of just the fields being changed.

    Sorted by id and serialised canonically so the same state always produces
    the same digest.
    """
    snapshot = sorted(
        [{"id": str(r.get("id")),
          "before": {f: r.get(f) for f in fields}} for r in rows if r.get("id")],
        key=lambda x: x["id"])
    blob = _json.dumps({"module": module, "records": snapshot},
                       sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest(), snapshot


# Snapshotted before a bulk delete. If a record was touched since the plan,
# Modified_Time moves and the fingerprint stops matching.
_DELETE_GUARD_FIELDS = ["Modified_Time"]

_COQL_PAGE = 200          # Zoho's per-call ceiling
_SCAN_CAP = 2000          # refuse past this rather than half-report


def _strip_limit(query):
    """Remove a trailing LIMIT so _coql_all can page the same query.

    Callers write natural COQL with a limit; paging needs to control it.
    """
    return re.sub(r"\s+limit\s+\d+(\s*,\s*\d+)?\s*$", "", str(query).strip(),
                  flags=re.I)


def _coql_all(base_query, cap=_SCAN_CAP, label="query"):
    """Run a SELECT to completion instead of taking the first page.

    Zoho returns at most 200 rows per call and flags more_records. Reading one
    page and calling it the answer is how a handover reports 200 records,
    moves 200, and quietly leaves the other 140 behind. Anything that claims a
    set is complete has to page.

    `base_query` must have no LIMIT of its own; this appends one.
    """
    rows, offset = [], 0
    while True:
        page_q = "%s limit %d, %d" % (base_query, offset, _COQL_PAGE)
        response = _call("POST", "coql", body={"select_query": page_q})
        page = response.get("data") or []
        rows.extend(page)
        if len(rows) > cap:
            raise RuntimeError(
                "%s matches more than %d records. Narrow it rather than acting "
                "on a partial set; a half-complete result here is worse than an "
                "error." % (label, cap))
        if not (response.get("info") or {}).get("more_records"):
            return rows
        offset += _COQL_PAGE


def _read_by_ids(module, ids, fields):
    if not ids:
        return []
    columns = ", ".join(fields)
    id_list = ", ".join(str(i) for i in ids)
    return _coql_all("select %s from %s where id in (%s)" % (columns, module, id_list),
                     cap=len(ids) + _COQL_PAGE, label="id lookup on " + module)


def zoho_plan_update(inputs, stamp):
    """Work out what a bulk update would change, without changing anything.

    Returns a plan carrying the prior values and a fingerprint. Feed the plan
    straight into zoho.apply_update.
    """
    module = _module_name(inputs)
    query = str(inputs.get("query", "")).strip()
    changes = inputs.get("changes")

    if not query:
        raise RuntimeError("'query' is required, a COQL SELECT that picks the "
                           "records to change. Zoho needs a WHERE clause.")
    if not query.lower().lstrip("( ").startswith("select"):
        raise RuntimeError("'query' must be a SELECT.")
    if not isinstance(changes, dict) or not changes:
        raise RuntimeError("'changes' must be a non-empty object mapping field "
                           "api_name to the new value.")

    fields = sorted(str(k) for k in changes)
    bad = [f for f in fields if not f.replace("_", "").isalnum()]
    if bad:
        raise RuntimeError("These are not valid field api_names: %r" % bad)

    matched = _coql_all(_strip_limit(query), cap=200, label="plan query")
    if not matched:
        raise RuntimeError("The query matched no records, so there is nothing "
                           "to plan.")

    limit = int(inputs.get("max_records") or 100)
    if limit > 100:
        raise RuntimeError("Zoho writes at most 100 records per call, so a plan "
                           "cannot exceed 100.")
    if len(matched) > limit:
        raise RuntimeError(
            "The query matched %d records but max_records is %d. Narrow the "
            "query or raise the limit." % (len(matched), limit))

    ids = [str(r.get("id")) for r in matched if r.get("id")]
    # The caller's query selected whatever it liked. Read the fields we are
    # actually about to overwrite, so the snapshot covers the right columns.
    current = _read_by_ids(module, ids, fields)
    fingerprint, snapshot = _fingerprint(module, current, fields)

    would_change = sum(
        1 for row in snapshot
        if any(row["before"].get(f) != changes[f] for f in fields))

    _plan_save(_plan_key(module, query, changes),
               {"fingerprint": fingerprint, "count": len(snapshot),
                "records": snapshot})

    return {
        "ok": True,
        "module": module,
        "records": snapshot,
        "planned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(snapshot),
        "would_change": would_change,
        "fields": fields,
        "expires_in_minutes": _PLAN_TTL // 60,
        "summary": "%d records matched, %d would actually change on %s. Run "
                   "zoho.apply_update with the same module, query and changes "
                   "to commit." % (len(snapshot), would_change, ", ".join(fields)),
    }, None


def zoho_apply_update(inputs, stamp):
    """Re-run the plan's query, and write only if the state still matches.

    Re-supply the same module, query and changes that were planned. The stored
    fingerprint is looked up locally; nothing has to travel back through a
    receipt, which matters because the platform redacts identifiers before
    sealing one. Re-running the query also catches records that newly match the
    filter, which is drift too.
    """
    module = _module_name(inputs)
    query = str(inputs.get("query", "")).strip()
    changes = inputs.get("changes")
    if not query.lower().lstrip("( ").startswith("select"):
        raise RuntimeError("'query' must be the same COQL SELECT used for the plan.")
    if not isinstance(changes, dict) or not changes:
        raise RuntimeError("'changes' must match the plan's changes object.")

    # Field names are interpolated into COQL by _read_by_ids, so validate before
    # anything else. plan_update checks them too; the plan lookup below happens
    # to block a bad name today, but that is incidental and a refactor could
    # remove it.
    bad = [f for f in changes if not str(f).replace("_", "").isalnum()]
    if bad:
        raise RuntimeError("These are not valid field api_names: %r" % bad)

    stored = _plan_load(_plan_key(module, query, changes))
    if not stored:
        raise RuntimeError(
            "No current plan for this module, query and changes. Run "
            "zoho.plan_update first, review what it reports, then apply with "
            "exactly the same three inputs. Plans expire after %d minutes."
            % (_PLAN_TTL // 60))
    expected = str(stored.get("fingerprint") or "")

    fields = sorted(str(k) for k in changes)
    matched = _coql_all(_strip_limit(query), cap=200, label="apply query")
    if not matched:
        raise RuntimeError("The query now matches no records. Something changed "
                           "since the plan; re-run zoho.plan_update.")
    if len(matched) > 100:
        raise RuntimeError("The query now matches %d records, over Zoho's 100 "
                           "per write." % len(matched))

    ids = [str(r.get("id")) for r in matched if r.get("id")]
    current = _read_by_ids(module, ids, fields)
    actual, snapshot = _fingerprint(module, current, fields)

    if actual != expected:
        raise RuntimeError(
            "Refusing to apply. The records moved since the plan was made: "
            "%d now match the query and the state fingerprint is %s, not %s. "
            "Re-run zoho.plan_update and review the new plan."
            % (len(snapshot), actual[:23] + "...", expected[:23] + "..."))

    payload = []
    for row in snapshot:
        record = {"id": row["id"]}
        record.update(changes)
        payload.append(record)

    result = _summarise(_call("PUT", module, body={"data": payload}),
                        "apply plan to " + module, stamp)
    result["fingerprint_verified"] = expected
    result["records_applied"] = len(payload)
    return result, None


def zoho_plan_delete(inputs, stamp):
    """Work out what a bulk delete would remove, without removing anything.

    Snapshots Modified_Time on every match. If a record is touched between the
    plan and the approval, the fingerprint moves and apply_delete refuses.
    Deleting is the one write where reviewing a stale set matters most, so it
    gets the same treatment as an update.
    """
    module = _module_name(inputs)
    query = str(inputs.get("query", "")).strip()
    if not query.lower().lstrip("( ").startswith("select"):
        raise RuntimeError("'query' is required, a COQL SELECT picking the "
                           "records to delete. Zoho needs a WHERE clause.")

    matched = _coql_all(_strip_limit(query), cap=100, label="delete plan query")
    if not matched:
        raise RuntimeError("The query matched no records, so there is nothing "
                           "to delete.")

    ids = [str(r.get("id")) for r in matched if r.get("id")]
    current = _read_by_ids(module, ids, _DELETE_GUARD_FIELDS)
    fingerprint, snapshot = _fingerprint(module, current, _DELETE_GUARD_FIELDS)
    _plan_save(_plan_key("delete:" + module, query, {}),
               {"fingerprint": fingerprint, "count": len(snapshot)})

    return {
        "ok": True,
        "module": module,
        "count": len(snapshot),
        "records": snapshot,
        "expires_in_minutes": _PLAN_TTL // 60,
        "summary": "%d records in %s would go to the recycle bin, recoverable "
                   "for 60 days. Run zoho.apply_delete with the same module and "
                   "query to commit." % (len(snapshot), module),
    }, None


def zoho_apply_delete(inputs, stamp):
    """Commit a delete plan, refusing if any record moved since it was made."""
    module = _module_name(inputs)
    query = str(inputs.get("query", "")).strip()
    if not query.lower().lstrip("( ").startswith("select"):
        raise RuntimeError("'query' must be the same COQL SELECT used for the "
                           "plan.")

    stored = _plan_load(_plan_key("delete:" + module, query, {}))
    if not stored:
        raise RuntimeError(
            "No current plan for this delete. Run zoho.plan_delete first, "
            "review what it reports, then apply with the same module and query. "
            "Plans expire after %d minutes." % (_PLAN_TTL // 60))

    matched = _coql_all(_strip_limit(query), cap=100, label="delete apply query")
    if not matched:
        raise RuntimeError("The query now matches no records. Something changed "
                           "since the plan; re-run zoho.plan_delete.")

    ids = [str(r.get("id")) for r in matched if r.get("id")]
    current = _read_by_ids(module, ids, _DELETE_GUARD_FIELDS)
    actual, snapshot = _fingerprint(module, current, _DELETE_GUARD_FIELDS)

    if actual != stored.get("fingerprint"):
        raise RuntimeError(
            "Refusing to delete. The records moved since the plan was made: %d "
            "now match and the state fingerprint no longer agrees. Re-run "
            "zoho.plan_delete and review the new set." % len(snapshot))

    response = _call("DELETE", module,
                     params={"ids": ",".join(r["id"] for r in snapshot)})
    result = _summarise(response, "apply delete plan to " + module, stamp)
    result["records_deleted"] = len(snapshot)
    return result, None


# --- handover ---------------------------------------------------------------
#
# When someone leaves, their open pipeline has to move to whoever picks it up,
# and somebody has to be able to show it moved completely. That is usually an
# afternoon of manual reassignment across four modules with no record of what
# happened. Same plan/apply shape as above: snapshot, fingerprint, re-verify,
# then one approved write per module.

_HANDOVER_MODULES = ["Leads", "Deals", "Contacts", "Accounts"]
_CLOSED_STAGES = ("Closed Won", "Closed Lost", "Closed-Lost to Competition")


def _handover_modules(inputs):
    requested = inputs.get("modules")
    if requested is None:
        return list(_HANDOVER_MODULES)
    if not isinstance(requested, list) or not requested:
        raise RuntimeError("'modules' must be a non-empty array of module "
                           "api_names, or omitted for %s."
                           % ", ".join(_HANDOVER_MODULES))
    out = [str(m).strip() for m in requested]
    bad = [m for m in out if not m.replace("_", "").isalnum()]
    if bad:
        raise RuntimeError("Not valid module api_names: %r" % bad)
    return out


def _owned_query(module, user_id, include_closed):
    """Records in one module owned by a user.

    Deals get a stage filter unless closed ones were asked for. Reassigning
    closed business rewrites who is credited with it, which is rarely what
    someone means by handover.

    The stage filter is one NOT IN rather than chained != conditions. Zoho's
    COQL parser rejects three of those on the same column with a bare
    SYNTAX_ERROR near 'where'.
    """
    where = "Owner = '%s'" % user_id
    if module == "Deals" and not include_closed:
        stages = ", ".join("'%s'" % s for s in _CLOSED_STAGES)
        where += " and Stage not in (%s)" % stages
    return "select Owner from %s where %s" % (module, where)


def _resolve_user(users, who, label):
    who = str(who or "").strip()
    if not who:
        raise RuntimeError("'%s' is required: a Zoho user id, or the exact "
                           "email or full name of a user." % label)
    for u in users:
        if str(u.get("id")) == who:
            return u
    hits = [u for u in users
            if who.lower() in (str(u.get("email") or "") + " "
                               + str(u.get("full_name") or "")).lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise RuntimeError("No active user matches %r for '%s'. Run "
                           "zoho.list_users to see who is available."
                           % (who, label))
    raise RuntimeError("%r matches %d users for '%s'. Use the user id instead."
                       % (who, len(hits), label))


def _handover_scan(module_names, from_id, include_closed):
    """Per-module id lists for everything the leaver owns."""
    found = {}
    for module in module_names:
        rows = _coql_all(_owned_query(module, from_id, include_closed),
                         label="ownership scan on " + module)
        found[module] = [str(r.get("id")) for r in rows if r.get("id")]
    return found


def _handover_fingerprint(found):
    blob = _json.dumps({m: sorted(ids) for m, ids in found.items()},
                       sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def zoho_plan_handover(inputs, stamp):
    """Work out what a departing user owns, without moving anything."""
    users = (_call("GET", "users",
                   params={"type": "ActiveConfirmedUsers", "per_page": 200})
             .get("users") or [])
    leaver = _resolve_user(users, inputs.get("from_user"), "from_user")
    taker = _resolve_user(users, inputs.get("to_user"), "to_user")
    if str(leaver.get("id")) == str(taker.get("id")):
        raise RuntimeError("from_user and to_user are the same person.")

    closed = str(inputs.get("closed_deals") or "skip").strip().lower()
    if closed not in ("skip", "include"):
        raise RuntimeError("'closed_deals' must be 'skip' or 'include'.")
    include_closed = closed == "include"

    module_names = _handover_modules(inputs)
    found = _handover_scan(module_names, str(leaver["id"]), include_closed)
    total = sum(len(v) for v in found.values())
    if not total:
        raise RuntimeError("%s owns nothing in %s, so there is nothing to hand "
                           "over." % (leaver.get("full_name"),
                                      ", ".join(module_names)))

    excluded = 0
    if "Deals" in module_names and not include_closed:
        every = _handover_scan(["Deals"], str(leaver["id"]), True)["Deals"]
        excluded = max(0, len(every) - len(found.get("Deals", [])))

    fingerprint = _handover_fingerprint(found)
    _plan_save(_plan_key("handover", str(leaver["id"]) + ">" + str(taker["id"]),
                         {"modules": module_names, "closed": closed}),
               {"fingerprint": fingerprint, "count": total,
                "found": {m: len(v) for m, v in found.items()}})

    breakdown = ", ".join("%d %s" % (len(found[m]), m.lower())
                          for m in module_names if found.get(m))
    note = ""
    if excluded:
        note = " %d closed deals were excluded; re-run with closed_deals=include to move them." % excluded

    return {
        "ok": True,
        "from_user": leaver.get("full_name"),
        "to_user": taker.get("full_name"),
        "modules": module_names,
        "counts": {m: len(v) for m, v in found.items()},
        "total": total,
        "closed_deals_excluded": excluded,
        "expires_in_minutes": _PLAN_TTL // 60,
        "summary": "%s owns %s (%d records) to move to %s.%s"
                   % (leaver.get("full_name"), breakdown, total,
                      taker.get("full_name"), note),
    }, None


def zoho_apply_handover(inputs, stamp):
    """Reassign everything in the plan, refusing if the set changed."""
    users = (_call("GET", "users",
                   params={"type": "ActiveConfirmedUsers", "per_page": 200})
             .get("users") or [])
    leaver = _resolve_user(users, inputs.get("from_user"), "from_user")
    taker = _resolve_user(users, inputs.get("to_user"), "to_user")

    closed = str(inputs.get("closed_deals") or "skip").strip().lower()
    if closed not in ("skip", "include"):
        raise RuntimeError("'closed_deals' must be 'skip' or 'include'.")
    module_names = _handover_modules(inputs)

    stored = _plan_load(_plan_key("handover",
                                  str(leaver["id"]) + ">" + str(taker["id"]),
                                  {"modules": module_names, "closed": closed}))
    if not stored:
        raise RuntimeError(
            "No current plan for this handover. Run zoho.plan_handover first, "
            "review what it reports, then apply with the same inputs. Plans "
            "expire after %d minutes." % (_PLAN_TTL // 60))

    found = _handover_scan(module_names, str(leaver["id"]), closed == "include")
    if _handover_fingerprint(found) != stored.get("fingerprint"):
        now = {m: len(v) for m, v in found.items()}
        raise RuntimeError(
            "Refusing to hand over. What %s owns changed since the plan was "
            "made: now %s, planned %s. Re-run zoho.plan_handover."
            % (leaver.get("full_name"), now, stored.get("found")))

    owner = {"Owner": {"id": str(taker["id"])}}
    moved, failed, errors = 0, 0, []
    for module, ids in found.items():
        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            if not batch:
                continue
            payload = [dict(owner, id=rid) for rid in batch]
            result = _call("PUT", module, body={"data": payload})
            for row in result.get("data", []) or []:
                if row.get("code") == "SUCCESS":
                    moved += 1
                else:
                    failed += 1
                    errors.append({"module": module, "code": row.get("code"),
                                   "message": row.get("message")})

    if moved == 0 and failed:
        raise RuntimeError("Zoho rejected every reassignment. First error: %s"
                           % errors[0])

    return {
        "ok": True,
        "action": "handover %s to %s" % (leaver.get("full_name"),
                                         taker.get("full_name")),
        "moved": moved,
        "failed": failed,
        "per_module": {m: len(v) for m, v in found.items()},
        "errors": errors[:10],
        "origin": _origin(stamp),
    }, None


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
