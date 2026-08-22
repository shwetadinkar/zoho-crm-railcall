"""shweta/zoho-crm 0.9.1

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

import calendar
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


class ZohoUnresolvedWrite(RuntimeError):
    """A write whose outcome cannot be determined.

    Raised when a write gets no HTTP status at all, or a 5xx. Zoho has no
    idempotency key, so the module cannot retry and cannot ask afterwards
    whether the request landed. The three possibilities - applied, not
    applied, partially applied - are all live.

    A subclass of RuntimeError deliberately: every existing caller that
    catches RuntimeError keeps working unchanged. Only the apply paths, which
    have the plan key and the target ids in scope, catch this specifically to
    record what was attempted before re-raising.

    CAUTION for anyone adding a catch site: _NON_IDEMPOTENT includes POST, and
    COQL reads are POSTs, so _coql_capped and _read_by_ids raise this too. It
    means "a non-idempotent request got no verdict", not "a write was made".
    Catch it around the write call alone, never around a block that also reads,
    or a failed pre-flight read gets filed as an unresolved write.
    """


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

    The two unknown-outcome branches raise ZohoUnresolvedWrite rather than
    a bare RuntimeError, so an apply path can record what it attempted. The
    429 and 4xx branches deliberately do not: a 429 is a settled refusal and
    a 4xx is a settled answer, and neither leaves the outcome in doubt.
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
                raise ZohoUnresolvedWrite(
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
                raise ZohoUnresolvedWrite(
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
        # Zoho returns the post-write Modified_Time on every SUCCESS row, and
        # it is byte-identical to what a follow-up read reports (verified
        # live). Recording it lets a later scan match a change to this ledger
        # entry EXACTLY, instead of guessing with a tolerance window around
        # the module's own clock. Without it, a UI edit landing seconds after
        # a module write is indistinguishable from the write itself.
        "written": [{"id": r.get("details", {}).get("id"),
                     "modified_time": r.get("details", {}).get("Modified_Time")}
                    for r in ok],
        "errors": [{"code": r.get("code"),
                    "message": r.get("message"),
                    "field": r.get("details", {}).get("api_name")} for r in bad],
        "origin": _origin(stamp),
    }


def _governed_written(rows):
    """Keep only the written rows a later read can actually be matched against.

    A `written` list whose rows carry no modified_time is the worst of both
    worlds: _ledger_index sees a non-empty list, so it does not count the entry
    as unmatchable, and it adds nothing to the governed set either. The
    approval goes silently missing instead of being reported as one that cannot
    be matched. Filtering here means an entry either carries usable timestamps
    or says plainly that it has none.

    Returns None rather than [] when nothing survives, because _ledger_index
    tests the value for truth and None is the honest shape for "no timestamps".

    The four older apply paths do not need this: _summarise's Modified_Time
    guarantee was verified live for create, update and upsert responses. DELETE
    is the one whose response shape has never been captured, which is exactly
    why apply_delete goes through here.

    Record ids are NOT lost by filtering. Every entry also carries `targets`,
    and _entry_record_ids reads the union, so custody_report still finds the
    record even when the timestamp is missing.
    """
    kept = [r for r in (rows or [])
            if isinstance(r, dict) and r.get("id") and r.get("modified_time")]
    return kept or None


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
_SCAN_MODULES = ("Leads", "Contacts", "Accounts", "Deals")
_SCAN_CHANGES_CAP = 500   # per module per run; truncated plus the seen set
                          # drain a backlog, so a smaller cap costs nothing


def _iso_utc(value):
    """Normalise a Zoho timestamp to ISO-8601 UTC, or None if unreadable.

    Zoho renders explicit offsets: 2026-08-08T16:44:05+05:30. Stripping the
    offset instead of converting it would make every record look 5.5 hours
    newer than it is, drag a watermark into the future, and permanently drop
    everything modified in the next 5.5 hours. The offset is half-hourly on
    this datacenter, so hour arithmetic is wrong too.

    Returns None rather than raising. One unreadable cell must not abort a
    scan, and a caller treats an unreadable timestamp as a change to surface
    rather than one to skip: a row we cannot prove is old must never silently
    become a row we ignore.

    Uses time/calendar rather than datetime to stay with the vocabulary the
    rest of this module already uses for the ledger.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    try:
        secs = calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None
    offset = text[19:]
    if offset in ("", "Z", "z"):
        pass
    elif offset[0] in "+-" and len(offset) >= 6 and offset[3] == ":":
        try:
            hours, minutes = int(offset[1:3]), int(offset[4:6])
        except ValueError:
            return None
        secs -= (1 if offset[0] == "+" else -1) * (hours * 3600 + minutes * 60)
    else:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))


def _detail_rows(detail, key):
    """One detail field as a list, whatever the writer actually put there.

    Ledger entries are hash-chained, so a shape that was written once stays on
    disk forever and every reader has to survive it. apply_merge proved that:
    its refusal recorded `losers` as a COUNT while its applied entry recorded
    the ids, and `for lid in detail["losers"]` on the refusal raised
    "'int' object is not iterable" - killing custody_report for every record it
    had been asked about, not just the merge's. Reading through here means a
    disagreement between two writers costs the ids of one entry, never the
    whole report.
    """
    value = detail.get(key)
    return value if isinstance(value, list) else []


def _entry_record_ids(entry):
    """Every record id one ledger entry names, however it names them.

    Six commands write entries and they do not agree on where the ids live:
    an applied entry has `written`, an unresolved one has `targets` objects, a
    refusal has `targets` as bare ids, and a merge scatters them across
    `master_id`, `losers` and an `archive`. Reading the union in one place is
    the difference between custody_report joining on all of them and joining on
    whichever shape whoever wrote the join happened to remember.
    """
    detail = entry.get("detail") or {}
    ids = []
    for row in _detail_rows(detail, "written"):
        if isinstance(row, dict) and row.get("id"):
            ids.append(str(row["id"]))
    for row in _detail_rows(detail, "before"):
        if isinstance(row, dict) and row.get("id"):
            ids.append(str(row["id"]))
    for row in _detail_rows(detail, "targets"):
        if isinstance(row, dict) and row.get("id"):
            ids.append(str(row["id"]))
        elif isinstance(row, (str, int)):
            ids.append(str(row))
    for row in _detail_rows(detail, "archive"):
        if isinstance(row, dict) and row.get("id"):
            ids.append(str(row["id"]))
    if detail.get("master_id"):
        ids.append(str(detail["master_id"]))
    for lid in _detail_rows(detail, "losers"):
        if isinstance(lid, (str, int)):
            ids.append(str(lid))
    seen, out = set(), []
    for rid in ids:
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def _ledger_index(with_records=False):
    """One walk of the live chain, and the only place the ledger is joined to
    record ids.

    Returns a dict:

      governed     "module:id:modified_at" keys built from the post-write
                   Modified_Time this module recorded. Zoho returns that value
                   on every SUCCESS row and it is byte-identical to a later
                   read, so a change either matches an approval exactly or it
                   does not. No tolerance window: a UI edit landing one second
                   after a module write stays visible.
      covers_from  the earliest entry in the live chain. The ledger rotates
                   past _LEDGER_MAX into sealed archives this does not read, so
                   claiming coverage before that point would report governed
                   changes as ungoverned.
      unmatchable  applied entries with no recorded Modified_Time -- everything
                   written before the ledger began recording it, plus every
                   merge, which Zoho's merge response does not timestamp. Those
                   are real approvals this cannot match, so the count is
                   reported rather than their records quietly called
                   ungoverned.
      by_record    {"module:id": {"applied": [...], "refused": [...],
                   "unresolved": [...], "reconciled": [...]}}, or None.
      unattributed_refusals
                   refusals recorded before refusals carried record ids. They
                   cannot be joined to anything, and the count is reported so
                   their absence from a record's history is not read as "the
                   control never fired here". Only counted when by_record is
                   built, since it is the only consumer.

    `by_record` is built only when asked for. scan_changes wants the key set
    and nothing else, and it runs on the station's schedule against a chain
    holding up to _LEDGER_MAX entries; assembling a per-record timeline it will
    never read would be work on every scheduled run for nothing.

    Both callers walk the same entries under the same matching rule on purpose.
    Two implementations of this join would drift, and the one that drifted
    would be the one nobody was watching.
    """
    governed, unmatchable, covers_from = set(), 0, None
    unattributed_refusals = 0
    by_record = {} if with_records else None

    for entry in (_ledger_load().get("entries") or []):
        at = entry.get("at")
        if at and (covers_from is None or at < covers_from):
            covers_from = at
        outcome = entry.get("outcome")
        module = entry.get("module")
        detail = entry.get("detail") or {}

        if outcome == "applied":
            rows = _detail_rows(detail, "written")
            if not rows:
                unmatchable += 1
            else:
                for row in rows:
                    rid = (row or {}).get("id")
                    stamp = _iso_utc((row or {}).get("modified_time"))
                    if rid and stamp:
                        governed.add("%s:%s:%s" % (module, rid, stamp))

        if by_record is None:
            continue

        # A reconciled entry names no records of its own; it points at the
        # unresolved entry it adjudicated. Filed under that entry's records so
        # a timeline shows the verdict beside the attempt it belongs to.
        if outcome == "reconciled":
            continue

        written_at = {}
        for row in _detail_rows(detail, "written"):
            if isinstance(row, dict) and row.get("id"):
                written_at[str(row["id"])] = row.get("modified_time")

        found_ids = _entry_record_ids(entry)
        if outcome == "refused" and not found_ids:
            unattributed_refusals += 1

        for rid in found_ids:
            slot = by_record.setdefault("%s:%s" % (module, rid),
                                        {"applied": [], "refused": [],
                                         "unresolved": [], "reconciled": []})
            if outcome not in slot:
                continue
            slot[outcome].append({
                "seq": entry.get("seq"),
                "at": at,
                "command": entry.get("command"),
                "plan_key": entry.get("plan_key"),
                "fields": detail.get("fields"),
                "reason": detail.get("reason"),
                "written_modified_time": written_at.get(rid),
            })

    if by_record is not None:
        # Second pass, because a reconciled entry can only be attached once the
        # unresolved entry it resolves is already in place.
        _attach_reconciled(by_record)

    return {"governed": governed, "covers_from": covers_from,
            "unmatchable": unmatchable, "by_record": by_record,
            "unattributed_refusals": unattributed_refusals}


def _attach_reconciled(by_record):
    """File each reconciled entry against the records it adjudicated."""
    by_seq = {}
    for key, slot in by_record.items():
        for row in slot["unresolved"]:
            by_seq.setdefault(row["seq"], []).append(key)

    for entry in (_ledger_load().get("entries") or []):
        if entry.get("outcome") != "reconciled":
            continue
        detail = entry.get("detail") or {}
        verdicts = {str((v or {}).get("id")): (v or {}).get("verdict")
                    for v in (detail.get("verdicts") or [])
                    if isinstance(v, dict)}
        for key in by_seq.get(detail.get("resolves_seq"), []):
            rid = key.split(":", 1)[1]
            by_record[key]["reconciled"].append({
                "seq": entry.get("seq"),
                "at": entry.get("at"),
                "resolves_seq": detail.get("resolves_seq"),
                "resolved": detail.get("resolved"),
                "verdict": verdicts.get(rid),
                "merge_state": detail.get("merge_state"),
            })


def _governed_index():
    """The key-set view of the ledger, for scan_changes.

    Kept as a name of its own because scan_changes reads exactly three things
    and should not have to know the shape of the fuller index.
    """
    index = _ledger_index()
    return index["governed"], index["covers_from"], index["unmatchable"]


def _strip_limit(query):
    """Remove a trailing LIMIT so _coql_all can page the same query.

    Callers write natural COQL with a limit; paging needs to control it.
    """
    return re.sub(r"\s+limit\s+\d+(\s*,\s*\d+)?\s*$", "", str(query).strip(),
                  flags=re.I)


def _coql_capped(base_query, cap=_SCAN_CAP, label="query"):
    """Page a SELECT up to `cap` rows and report whether more were waiting.

    Returns (rows, hit_cap). `hit_cap` is True only when the ceiling was
    reached AND Zoho still flagged more_records -- a result that lands
    exactly on the cap with nothing behind it is complete, not truncated.
    Getting that wrong on a scheduled read means reporting truncation
    forever and never advancing a watermark.

    This is the resumable-stream variant. It stops cleanly and leaves the
    caller to come back for the rest, which is only safe when something
    downstream tracks position. If a human is about to act on the result,
    use _coql_all instead -- see its docstring for why the two differ.

    `base_query` must have no LIMIT of its own; this appends one.
    """
    rows, offset = [], 0
    while True:
        page_q = "%s limit %d, %d" % (base_query, offset, _COQL_PAGE)
        response = _call("POST", "coql", body={"select_query": page_q})
        page = response.get("data") or []
        rows.extend(page)
        more = bool((response.get("info") or {}).get("more_records"))
        if len(rows) >= cap:
            return rows[:cap], (more or len(rows) > cap)
        if not more:
            return rows, False
        offset += _COQL_PAGE


def _coql_all(base_query, cap=_SCAN_CAP, label="query"):
    """Run a SELECT to completion instead of taking the first page.
    Zoho returns at most 200 rows per call and flags more_records. Reading one
    page and calling it the answer is how a handover reports 200 records,
    moves 200, and quietly leaves the other 140 behind. Anything that claims a
    set is complete has to page.

    Raises rather than truncating: a caller here is about to act on the set,
    and a half-complete answer is worse than an error. The resumable variant
    is _coql_capped, which is only correct when something tracks position.

    `base_query` must have no LIMIT of its own; this appends one.
    """
    rows, hit_cap = _coql_capped(base_query, cap, label)
    if hit_cap:
        raise RuntimeError(
            "%s matches more than %d records. Narrow it rather than acting "
            "on a partial set; a half-complete result here is worse than an "
            "error." % (label, cap))
    return rows


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
    # about to overwrite, plus Modified_Time as a guard: hashing only the
    # written fields means an edit to any other column on a matched record
    # leaves the fingerprint unchanged and the write lands on state nobody
    # reviewed. Modified_Time is read and hashed, never written.
    guard = sorted(set(fields) | {"Modified_Time"})
    current = _read_by_ids(module, ids, guard)
    fingerprint, snapshot = _fingerprint(module, current, guard)

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
    guard = sorted(set(fields) | {"Modified_Time"})
    current = _read_by_ids(module, ids, guard)
    actual, snapshot = _fingerprint(module, current, guard)

    if actual != expected:
        _ledger_append(_ledger_note(
            "refused", "apply_update", module,
            _plan_key(module, query, changes),
            {"reason": "state moved between plan and apply",
             "expected": expected, "actual": actual,
             "records": len(snapshot), "fields": fields,
             "targets": [r["id"] for r in snapshot]}))
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

    # Only the write is inside the try. _read_by_ids above is a COQL POST and
    # POST is in _NON_IDEMPOTENT, so a pre-flight read that gets no verdict
    # raises ZohoUnresolvedWrite too - widening this block would file a failed
    # read as a write that might have landed.
    try:
        result = _summarise(_call("PUT", module, body={"data": payload}),
                            "apply plan to " + module, stamp)
    except ZohoUnresolvedWrite as exc:
        raise _ledger_unresolved(
            exc, "apply_update", module, _plan_key(module, query, changes),
            {"intent": changes, "fields": fields,
             "targets": [{"id": r["id"],
                          "before": {f: r["before"].get(f) for f in fields},
                          "before_modified_time":
                              r["before"].get("Modified_Time")}
                         for r in snapshot]})
    result["fingerprint_verified"] = expected
    result["records_applied"] = len(payload)
    entry = _ledger_append(_ledger_note(
        "applied", "apply_update", module,
        _plan_key(module, query, changes),
        {"records": len(payload), "fields": fields, "fingerprint": expected,
         "changes": changes,
         "written": result.get("written"),
         "before": [{"id": r["id"],
                     "before": {f: r["before"].get(f) for f in fields}}
                    for r in snapshot]}))
    result["ledger_seq"] = entry["seq"]
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
    """Commit a delete plan, refusing if any record moved since it was made.

    Both outcomes reach the ledger. The prior values are recorded because
    Zoho's recycle bin entry has no display name and no deleted-by, so for 60
    days the ledger entry is the more readable of the two records that a delete
    happened, and after that it is the only one.
    """
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
        # This path used to raise without recording anything, which made a
        # refused delete the one control firing that left no evidence - the
        # exact opposite of what the ledger is for, and a hole custody_report
        # would have reported as an absence of refusals rather than an absence
        # of records.
        _ledger_append(_ledger_note(
            "refused", "apply_delete", module,
            _plan_key("delete:" + module, query, {}),
            {"reason": "state moved between plan and apply",
             "expected": stored.get("fingerprint"), "actual": actual,
             "records": len(snapshot),
             "targets": [r["id"] for r in snapshot]}))
        raise RuntimeError(
            "Refusing to delete. The records moved since the plan was made: %d "
            "now match and the state fingerprint no longer agrees. Re-run "
            "zoho.plan_delete and review the new set." % len(snapshot))

    try:
        response = _call("DELETE", module,
                         params={"ids": ",".join(r["id"] for r in snapshot)})
    except ZohoUnresolvedWrite as exc:
        # A delete sets no value, so there is nothing for a later read to
        # compare against: for these targets "landed" means the record is gone.
        # Said explicitly rather than left to be inferred from the command name.
        # Note this is the only ledger entry apply_delete writes at all - a
        # successful delete is still unrecorded, which is a real gap, not
        # something this change introduced.
        raise _ledger_unresolved(
            exc, "apply_delete", module,
            _plan_key("delete:" + module, query, {}),
            {"intent": None, "verdict_basis": "existence",
             "fields": list(_DELETE_GUARD_FIELDS),
             "targets": [{"id": r["id"], "before": r["before"],
                          "before_modified_time":
                              r["before"].get("Modified_Time")}
                         for r in snapshot]})
    result = _summarise(response, "apply delete plan to " + module, stamp)
    result["records_deleted"] = len(snapshot)

    # A governed delete used to leave no record at all. It does not produce a
    # scan_changes false positive the way a handover does - a deleted record
    # stops coming back from COQL, so nothing scans it - but audit_pack and
    # custody_report both showed the approval as never having happened.
    #
    # Zoho's DELETE response has never been captured, so whether its SUCCESS
    # rows carry Modified_Time is unknown. _governed_written decides: with
    # timestamps the entry is matchable, without them it is reported as
    # unmatchable rather than disappearing. Either way `targets` and `before`
    # carry the ids and the prior state.
    entry = _ledger_append(_ledger_note(
        "applied", "apply_delete", module,
        _plan_key("delete:" + module, query, {}),
        {"records": len(snapshot), "fields": list(_DELETE_GUARD_FIELDS),
         "written": _governed_written(result.get("written")),
         "before": [{"id": r["id"], "before": r["before"]} for r in snapshot],
         "targets": [r["id"] for r in snapshot]}))
    result["ledger_seq"] = entry["seq"]
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
    return "select Owner, Modified_Time from %s where %s" % (module, where)


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
    """Per-module id lists for everything the leaver owns, and when each row
    was last modified.

    Returns (found, before_times): {module: [id]} and {module: {id: mtime}}.

    The timestamps are free - the scan reads every row anyway - and they are
    the only "before" an unresolved reassignment can be given. Read afterwards
    they would prove nothing, because the write that got no verdict may be
    exactly what moved them.
    """
    found, before_times = {}, {}
    for module in module_names:
        rows = _coql_all(_owned_query(module, from_id, include_closed),
                         label="ownership scan on " + module)
        found[module] = [str(r.get("id")) for r in rows if r.get("id")]
        before_times[module] = {str(r.get("id")): r.get("Modified_Time")
                                for r in rows if r.get("id")}
    return found, before_times


def _handover_fingerprint(found):
    blob = _json.dumps({m: sorted(ids) for m, ids in found.items()},
                       sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()



# =============================================================================
# DEDUPE  -  paste into handler.py after zoho_apply_rollback, before the
# handover section.
#
# Merge is the most destructive operation reachable on this module's scopes and
# the least visible afterwards. Verified live against the org on 2026-08-01 and
# 2026-08-03:
#
#   POST /crm/v8/{module}/{master_id}/actions/merge
#   {"merge": [{"data": [{"id": "<loser_id>"}]}]}
#
#   - synchronous, 201 SUCCESS, no extra OAuth scope
#   - notes are REPARENTED to the master, not destroyed
#   - the master's field values win; the loser's are lost silently
#   - the loser is gone: direct fetch returns 204, invisible to COQL
#   - the recycle-bin entry has display_name=None and deleted_by=None, so an
#     operator opening the bin sees an unidentifiable blank row
#   - PUT /actions/restore answers HTTP 200 with an empty body and does nothing
#
# So there is no undo, no usable bin entry, and no attribution. And merging is
# routine - housekeeping, done casually, by whoever notices the duplicate.
# =============================================================================

# Verified on Leads and Contacts. Accounts and Deals are untested, and assuming
# they behave the same is the assumption this project keeps disproving.
_MERGE_MODULES = ("Leads", "Contacts")

# Zoho accepts a list under data[]; this cap is ours, not theirs. Three losers
# is already a large enough diff for a person to actually read before they
# approve something irreversible.
_MERGE_MAX_LOSERS = 3

# Read for the preview and the fingerprint, never written.
_MERGE_CONTEXT_FIELDS = ["Modified_Time", "Modified_By", "Created_By"]

# Related lists, and how to reach them. The nested REST routes
# (GET Leads/{id}/Tasks) answer 400 REQUIRED_PARAM_MISSING; COQL works. Both
# verified 2026-08-03.
_MERGE_RELATED = (
    ("Notes", "Parent_Id", "Note_Title"),
    ("Tasks", "What_Id", "Subject"),
    ("Calls", "What_Id", "Subject"),
    ("Events", "What_Id", "Event_Title"),
    ("Attachments", "Parent_Id", "File_Name"),
)


def _merge_module(inputs):
    """Resolve and canonicalise the module, or refuse.

    Matched case-insensitively and returned in Zoho's casing. Elsewhere in this
    module a wrong case fails at the API with Zoho's own error, which is fine
    for a read. Here the operation cannot be undone, so a confusing rejection
    is worth avoiding.
    """
    module = _module_name(inputs)
    for known in _MERGE_MODULES:
        if module.lower() == known.lower():
            return known
    raise RuntimeError(
        "Merge is verified on %s only. %r is not covered: Zoho may behave "
        "differently there and this command will not guess about an operation "
        "that cannot be undone."
        % (" and ".join(_MERGE_MODULES), module))


def _merge_ids(inputs):
    master = str(inputs.get("master_id", "")).strip()
    if not master:
        raise RuntimeError("'master_id' is required: the record that survives "
                           "the merge and whose values win.")
    losers = inputs.get("loser_ids")
    if isinstance(losers, str):
        losers = [losers]
    if not isinstance(losers, list) or not losers:
        raise RuntimeError("'loser_ids' is required: an array of record ids to "
                           "merge into the master. They will cease to exist.")
    losers = [str(x).strip() for x in losers if str(x).strip()]
    if not losers:
        raise RuntimeError("'loser_ids' contained no usable ids.")
    if master in losers:
        raise RuntimeError("The master id appears in loser_ids. A record "
                           "cannot be merged into itself.")
    if len(set(losers)) != len(losers):
        raise RuntimeError("'loser_ids' contains duplicates.")
    if len(losers) > _MERGE_MAX_LOSERS:
        raise RuntimeError(
            "%d losers requested; this command allows %d per call. A merge "
            "cannot be undone, and a diff nobody reads is not a review."
            % (len(losers), _MERGE_MAX_LOSERS))
    return master, losers


def _merge_key(module, master, losers):
    return _plan_key("merge:" + module, master, {"losers": sorted(losers)})


def _merge_read_full(module, rec_id):
    """Every field on one record. describe_module gives the field list; the
    merge preview needs all of them, because any field the loser holds and the
    master does not is a value about to disappear."""
    response = _call("GET", "%s/%s" % (module, rec_id))
    rows = response.get("data") or []
    return rows[0] if rows else None


def _merge_related_counts(rec_id):
    """What is attached to a record, per related list.

    COQL rather than the nested REST route: GET Leads/{id}/Notes and friends
    answer 400 REQUIRED_PARAM_MISSING on v8. Verified 2026-08-03.
    """
    out = {}
    for mod, link, label_field in _MERGE_RELATED:
        try:
            rows = _coql_all(
                "select id, %s from %s where %s = '%s'"
                % (label_field, mod, link, rec_id),
                cap=_SCAN_CAP, label="%s on %s" % (mod, rec_id))
        except Exception:
            # A related list this org does not expose is not a reason to
            # refuse the whole preview - but it must not be silently counted
            # as zero either.
            out[mod] = {"count": None, "titles": [],
                        "note": "could not be read; treat as unknown"}
            continue
        out[mod] = {
            "count": len(rows),
            "titles": [str(r.get(label_field) or "") for r in rows[:5]],
        }
    return out


# Fields that always differ between any two records and are never something an
# operator can act on. Zoho stamps these itself; showing them buries the three
# or four conflicts that actually matter under a wall of timestamps. Verified
# against a live merge preview on 2026-08-07, where 4 of 7 "conflicts" were
# system time fields.
_MERGE_IGNORED_FIELDS = frozenset({
    "id", "Created_Time", "Modified_Time", "Created_By", "Modified_By",
    "Last_Activity_Time", "Change_Log_Time__s", "Record_Image",
    "Locked__s", "Tag", "$approved", "$approval", "$editable",
    "$process_flow", "$review", "$review_process", "$orchestration",
    "$in_merge", "$approval_state", "$sharing_permission", "$state",
    "$converted", "$converted_detail", "$zia_visions", "$field_states",
    "$has_more", "$pathfinder", "$wizard_connection_path",
    "$is_duplicate", "$following", "$photo_id", "$layout_id__s",
})


def _merge_ignorable(field):
    """True for system fields nobody can act on.

    The explicit set above plus two shapes: anything starting with `$`, which
    is Zoho's internal namespace, and anything ending `_Time` or `_Time__s`,
    which is a stamp rather than a value.
    """
    if field in _MERGE_IGNORED_FIELDS:
        return True
    if field.startswith("$"):
        return True
    if field.endswith("_Time") or field.endswith("_Time__s"):
        return True
    return False


def _merge_conflicts(master_rec, loser_rec):
    """Fields where the loser holds a value the master will overwrite.

    This is the whole point of the preview. The master wins silently, so an
    operator has to see the phone number that is about to vanish while they can
    still swap which record is the master - which only works if the list is
    short enough to read. System stamps are filtered out and counted
    separately rather than dropped silently.
    """
    conflicts, only_on_loser, system = [], [], []
    for field, lval in sorted((loser_rec or {}).items()):
        if _merge_ignorable(field):
            mval = (master_rec or {}).get(field)
            if lval not in (None, "", [], {}) and mval != lval:
                system.append(field)
            continue
        if lval in (None, "", [], {}):
            continue
        mval = (master_rec or {}).get(field)
        if mval in (None, "", [], {}):
            only_on_loser.append({"field": field, "loser": lval})
        elif mval != lval:
            conflicts.append({"field": field, "master": mval, "loser": lval})
    return conflicts, only_on_loser, system


def zoho_plan_merge(inputs, stamp):
    """Work out exactly what a merge would destroy, without merging anything.

    Reports three things, in the order an operator needs them: what will be
    lost, what will move, and who last touched these records.

    Merge is irreversible. Zoho's recycle-bin entry for a merged record carries
    no name and no deleted-by, and the restore endpoint answers 200 while doing
    nothing. So this preview is the only chance to notice that the wrong record
    was chosen as master.
    """
    module = _merge_module(inputs)
    master_id, loser_ids = _merge_ids(inputs)

    master = _merge_read_full(module, master_id)
    if master is None:
        raise RuntimeError("Master record %s not found in %s."
                           % (master_id, module))

    losers, missing = [], []
    for rid in loser_ids:
        rec = _merge_read_full(module, rid)
        if rec is None:
            missing.append(rid)
            continue
        conflicts, only_on_loser, system = _merge_conflicts(master, rec)
        losers.append({
            "id": rid,
            "conflicts": conflicts,
            "only_on_loser": only_on_loser,
            "system_fields_differing": len(system),
            "related": _merge_related_counts(rid),
            "modified_time": rec.get("Modified_Time"),
            "modified_by": (rec.get("Modified_By") or {}).get("name")
                           if isinstance(rec.get("Modified_By"), dict)
                           else rec.get("Modified_By"),
        })
    if missing:
        raise RuntimeError(
            "These records do not exist in %s: %s. A merge plan has to be "
            "built from records that are actually there."
            % (module, ", ".join(missing)))

    # Fingerprint every record involved, master included: if the master is
    # edited between plan and apply, the values that win are not the values
    # that were reviewed.
    everyone = [master_id] + loser_ids
    current = _read_by_ids(module, everyone, _MERGE_CONTEXT_FIELDS)
    fingerprint, _snapshot = _fingerprint(module, current, _MERGE_CONTEXT_FIELDS)
    _plan_save(_merge_key(module, master_id, loser_ids),
               {"fingerprint": fingerprint,
                "master_id": master_id,
                "loser_ids": loser_ids,
                "count": len(loser_ids)})

    total_conflicts = sum(len(l["conflicts"]) for l in losers)
    total_related = sum(
        (v.get("count") or 0)
        for l in losers for v in l["related"].values())

    return {
        "ok": True,
        "module": module,
        "master_id": master_id,
        "losers": losers,
        "count": len(losers),
        "conflicting_fields": total_conflicts,
        "related_records_moving": total_related,
        "master_modified_time": master.get("Modified_Time"),
        "expires_in_minutes": _PLAN_TTL // 60,
        "irreversible": True,
        "summary": (
            "Merging %d record(s) into %s. %d field value(s) held by the "
            "losers will be overwritten by the master's and lost; %d related "
            "record(s) will move to the master. Notes, tasks, calls, events "
            "and attachments are reparented, not deleted. The losing records "
            "themselves cannot be recovered: Zoho's restore endpoint answers "
            "200 and does nothing after a merge, and the recycle-bin entry "
            "carries no name. Read the conflicts before approving, and swap "
            "the master if the wrong values are winning."
            % (len(losers), master_id, total_conflicts, total_related)),
        "origin": _origin(stamp),
    }, None


def zoho_apply_merge(inputs, stamp):
    """Commit a merge plan, refusing if any record moved since it was made.

    Two things differ from every other apply in this module.

    The ledger entry is written BEFORE the API call, not after. Everywhere else
    the ledger writes afterwards, because recording a change that never
    happened is worse than missing one. Here the reasoning inverts: a merge
    with no record of what the loser held is unrecoverable in a way an
    unrecorded update is not, and Zoho's own bin entry is useless. If the
    ledger write fails, the merge does not proceed.

    And there is no rollback. plan_rollback covers apply_update only. The
    ledger entry is a reference - someone can look up the phone number that
    vanished and re-enter it by hand - not an undo.
    """
    module = _merge_module(inputs)
    master_id, loser_ids = _merge_ids(inputs)
    key = _merge_key(module, master_id, loser_ids)

    stored = _plan_load(key)
    if not stored:
        raise RuntimeError(
            "No current plan for this merge. Run zoho.plan_merge first, read "
            "what it says will be lost, then apply with exactly the same "
            "module, master_id and loser_ids. Plans expire after %d minutes."
            % (_PLAN_TTL // 60))

    everyone = [master_id] + loser_ids
    current = _read_by_ids(module, everyone, _MERGE_CONTEXT_FIELDS)
    actual, _snapshot = _fingerprint(module, current, _MERGE_CONTEXT_FIELDS)
    expected = str(stored.get("fingerprint") or "")

    if actual != expected:
        _ledger_append(_ledger_note(
            "refused", "apply_merge", module, key,
            {"reason": "state moved between plan and apply",
             "expected": expected, "actual": actual,
             # `losers` is the ids here, as the applied entry records
             # them. It was the COUNT, alone among the six refusals in
             # putting a number under a key that means ids everywhere
             # else; `records` is where every other refusal puts its
             # count. Entries already chained onto disk keep the old
             # shape - _detail_rows is what makes those harmless.
             "master_id": master_id, "losers": list(loser_ids),
             "records": len(loser_ids),
             "targets": [master_id] + list(loser_ids)}))
        raise RuntimeError(
            "Refusing to merge. One of these records moved since the plan was "
            "made: the state fingerprint is %s, not %s. A merge cannot be "
            "undone, so re-run zoho.plan_merge and read the new diff."
            % (actual[:23] + "...", expected[:23] + "..."))

    if len(current) != len(everyone):
        found = {str(r.get("id")) for r in current}
        gone = [r for r in everyone if r not in found]
        raise RuntimeError(
            "These records are no longer readable: %s. Re-run zoho.plan_merge."
            % ", ".join(gone))

    # The full record of each loser, written before anything is destroyed.
    # After the merge this is the only readable copy that exists anywhere:
    # Zoho's bin entry has no display_name and no deleted_by.
    archive = []
    for rid in loser_ids:
        rec = _merge_read_full(module, rid)
        archive.append({
            "id": rid,
            "record": rec,
            "related": _merge_related_counts(rid),
        })
    entry = _ledger_append(_ledger_note(
        "applied", "apply_merge", module, key,
        {"master_id": master_id, "losers": loser_ids,
         "fingerprint": expected,
         "irreversible": True,
         "archive": archive}))

    _merge_before = {str(r.get("id")): r.get("Modified_Time") for r in current}
    merged, failed = [], []
    for index, rid in enumerate(loser_ids):
        # One loser at a time so a partial failure is legible: with a single
        # batched call, a failure halfway through leaves no way to say which
        # records were merged and which were not.
        try:
            response = _call("POST", "%s/%s/actions/merge" % (module, master_id),
                             body={"merge": [{"data": [{"id": rid}]}]})
            rows = (response or {}).get("merge") or []
            code = (rows[0] or {}).get("code") if rows else None
            if code == "SUCCESS":
                merged.append(rid)
            else:
                failed.append({"id": rid, "response": rows[0] if rows else response})
        except ZohoUnresolvedWrite as exc:
            # Ordered before the catch-all below, which would otherwise swallow
            # this into `failed` and carry on. It must not carry on: after a
            # merge that returned no verdict the master's state is unknown, and
            # merging further losers into a possibly half-merged master is the
            # kind of confident-looking wrong answer this module exists to
            # avoid. The remaining losers are recorded as not attempted, so
            # nothing about them is left ambiguous either.
            #
            # `archive_seq` points at the entry written before the merge fired,
            # which holds the losers' full values. It is the only readable copy
            # if the merge did in fact land.
            raise _ledger_unresolved(
                exc, "apply_merge", module, key,
                {"intent": None, "verdict_basis": "existence",
                 "master_id": master_id,
                 "archive_seq": entry["seq"],
                 "merged_before_failure": list(merged),
                 "attempted": rid,
                 "not_attempted": loser_ids[index + 1:],
                 # `current` was read before the archive and before the merge,
                 # so these timestamps predate the attempt. Reconciliation
                 # checks a merge for existence, but a master that is still
                 # present and still untouched is a stronger statement than
                 # "still present", and it is free to record here.
                 "targets": ([{"id": rec_id, "role": role,
                               "before": {"Modified_Time": _merge_before.get(rec_id)},
                               "before_modified_time": _merge_before.get(rec_id)}
                              for rec_id, role in ([(master_id, "master")]
                                                   + [(lid, "loser")
                                                      for lid in loser_ids])])})
        except Exception as e:                                  # noqa: BLE001
            failed.append({"id": rid, "error": str(e)[:200]})

    return {
        "ok": not failed,
        "action": "merge into %s/%s" % (module, master_id),
        "master_id": master_id,
        "succeeded": len(merged),
        "failed": len(failed),
        "merged": merged,
        "errors": failed,
        "fingerprint_verified": expected,
        "ledger_seq": entry["seq"],
        "recoverable": False,
        "summary": (
            "Merged %d of %d record(s) into %s. The losing records are gone "
            "and cannot be restored; their full values are in ledger entry %d, "
            "which is the only readable copy. Related records were reparented "
            "to the master."
            % (len(merged), len(loser_ids), master_id, entry["seq"])),
        "origin": _origin(stamp),
    }, None



# =============================================================================
# BULK UPSERT  -  paste into handler.py after zoho_apply_merge, before the
# handover section.
#
# upsert_records already covers one call of up to 100. This is the governed
# form for a set larger than that: page the whole thing, tell the operator how
# many records will be CREATED versus UPDATED before anything happens, and
# refuse if the ones that already exist moved between review and execution.
#
# HONEST LIMIT, and it is stated in the command output rather than only here:
# drift detection covers the records that ALREADY EXIST. A record about to be
# created has no prior state, so there is nothing to fingerprint and nothing
# that can move. The guarantee is therefore weaker than plan_update's - the
# preview is exact about which is which, and the drift check applies to the
# update half only.
# =============================================================================

_UPSERT_CALL = 100        # Zoho's per-call ceiling for upsert
_UPSERT_CAP = 2000        # refuse past this rather than half-apply


def _upsert_records_arg(inputs):
    records = inputs.get("records")
    if not isinstance(records, list) or not records:
        raise RuntimeError("'records' must be a non-empty array of objects to "
                           "insert or update.")
    if not all(isinstance(r, dict) for r in records):
        raise RuntimeError("'records' must contain objects, one per record.")
    if len(records) > _UPSERT_CAP:
        raise RuntimeError(
            "%d records requested; this command refuses past %d rather than "
            "half-applying a set. Split it." % (len(records), _UPSERT_CAP))
    return records


def _upsert_check_fields(inputs, records):
    fields = inputs.get("duplicate_check_fields")
    if isinstance(fields, str):
        fields = [fields]
    if not isinstance(fields, list) or not fields:
        raise RuntimeError(
            "'duplicate_check_fields' is required: the field or fields Zoho "
            "matches on to decide whether a record is an insert or an update. "
            "Without it the plan cannot tell you which is which.")
    fields = [str(f).strip() for f in fields if str(f).strip()]
    missing = sorted({f for f in fields
                      for r in records if f not in r})
    if missing:
        raise RuntimeError(
            "Every record must carry the duplicate-check fields. Missing %s on "
            "at least one record." % ", ".join(missing))
    return fields


def _upsert_key(module, records, fields):
    """A plan is addressed by the same inputs that made it.

    Receipts redact identifiers, so a plan token could not survive the round
    trip - the same constraint plan_update works under. Keyed on the check
    fields plus the values being matched, not the whole payload, so a plan
    stays valid if a non-matched field is corrected before apply.
    """
    keys = sorted(
        "|".join("%s=%s" % (f, r.get(f)) for f in fields) for r in records)
    return _plan_key("upsert:" + module, ",".join(sorted(fields)),
                     {"keys": keys})


def _upsert_existing(module, records, fields):
    """Find which of these records already exist, matched on the check fields.

    One COQL per check field rather than per record: a set of 500 records would
    otherwise be 500 round trips. Values are matched with `in (...)`, the same
    form plan_delete uses.
    """
    found = {}
    for field in fields:
        values = sorted({str(r.get(field)) for r in records
                         if r.get(field) not in (None, "")})
        if not values:
            continue
        for start in range(0, len(values), _UPSERT_CALL):
            chunk = values[start:start + _UPSERT_CALL]
            quoted = ", ".join("'%s'" % v.replace("'", "") for v in chunk)
            rows = _coql_all(
                "select id, %s, Modified_Time from %s where %s in (%s)"
                % (field, module, field, quoted),
                cap=_UPSERT_CAP + _COQL_PAGE,
                label="upsert lookup on " + module)
            for row in rows:
                found[str(row.get(field))] = row
    return found


def _upsert_split(records, fields, existing):
    """Classify each record as an update or a create, and say why."""
    updates, creates = [], []
    for rec in records:
        match = None
        for field in fields:
            hit = existing.get(str(rec.get(field)))
            if hit:
                match = (field, hit)
                break
        if match:
            field, hit = match
            updates.append({"id": str(hit.get("id")),
                            "matched_on": field,
                            "matched_value": rec.get(field),
                            "modified_time": hit.get("Modified_Time")})
        else:
            creates.append({f: rec.get(f) for f in fields})
    return updates, creates


def zoho_plan_upsert(inputs, stamp):
    """Work out which records a bulk upsert would create and which it would
    update, without writing anything.

    upsert_records handles one call of up to 100. This is the governed form for
    a larger set: it pages the lookup to completion, reports the split, and
    fingerprints the records that already exist so apply_upsert can refuse if
    they move.

    The drift guarantee is narrower than plan_update's, and the output says so:
    a record that does not exist yet has no prior state to move.
    """
    module = _module_name(inputs)
    records = _upsert_records_arg(inputs)
    fields = _upsert_check_fields(inputs, records)

    existing = _upsert_existing(module, records, fields)
    updates, creates = _upsert_split(records, fields, existing)

    ids = [u["id"] for u in updates]
    current = _read_by_ids(module, ids, ["Modified_Time"]) if ids else []
    fingerprint, _snapshot = _fingerprint(module, current, ["Modified_Time"])
    _plan_save(_upsert_key(module, records, fields),
               {"fingerprint": fingerprint,
                "update_ids": ids,
                "updates": len(updates),
                "creates": len(creates)})

    _calls = (len(records) + _UPSERT_CALL - 1) // _UPSERT_CALL
    return {
        "ok": True,
        "module": module,
        "duplicate_check_fields": fields,
        "total": len(records),
        "will_update": len(updates),
        "will_create": len(creates),
        "updates": updates,
        "creates": creates,
        "calls_required": (len(records) + _UPSERT_CALL - 1) // _UPSERT_CALL,
        "expires_in_minutes": _PLAN_TTL // 60,
        "drift_covers": "the %d records that already exist; the %d being "
                        "created have no prior state to move"
                        % (len(updates), len(creates)),
        "summary": (
            "%d records matched on %s: %d already exist and would be updated, "
            "%d would be created. Zoho takes %d per call, so this runs as %d "
            "%s. Drift detection covers the %d existing records only - a "
            "record that does not exist yet has nothing that can move. Run "
            "zoho.apply_upsert with the same inputs to commit."
            % (len(records), ", ".join(fields), len(updates), len(creates),
               _UPSERT_CALL, _calls, "call" if _calls == 1 else "calls",
               len(updates))),
        "origin": _origin(stamp),
    }, None


def zoho_apply_upsert(inputs, stamp):
    """Commit an upsert plan, refusing if any existing record moved since it
    was made.

    Batched at Zoho's 100 per call. A batch that fails partway is reported per
    record: Zoho answers HTTP 200 even when some rows fail, so succeeded and
    failed are counted from the response body rather than the status code.
    """
    module = _module_name(inputs)
    records = _upsert_records_arg(inputs)
    fields = _upsert_check_fields(inputs, records)
    key = _upsert_key(module, records, fields)

    stored = _plan_load(key)
    if not stored:
        raise RuntimeError(
            "No current plan for this upsert. Run zoho.plan_upsert first, read "
            "the create/update split, then apply with the same module, records "
            "and duplicate_check_fields. Plans expire after %d minutes."
            % (_PLAN_TTL // 60))

    ids = list(stored.get("update_ids") or [])
    current = _read_by_ids(module, ids, ["Modified_Time"]) if ids else []
    actual, snapshot = _fingerprint(module, current, ["Modified_Time"])
    expected = str(stored.get("fingerprint") or "")

    if actual != expected:
        _ledger_append(_ledger_note(
            "refused", "apply_upsert", module, key,
            {"reason": "an existing record moved between plan and apply",
             "expected": expected, "actual": actual,
             "updates": len(ids), "creates": stored.get("creates"),
             # The records that already exist. A create has no id yet, so a
             # refused upsert can only name the half that does.
             "targets": [str(i) for i in ids]}))
        raise RuntimeError(
            "Refusing to upsert. One of the %d records that already exist "
            "moved since the plan was made: the state fingerprint is %s, not "
            "%s. Re-run zoho.plan_upsert and review the new split."
            % (len(ids), actual[:23] + "...", expected[:23] + "..."))

    succeeded, failed, errors = 0, 0, []
    # Zoho's post-write Modified_Time, per record, exactly as returned. A later
    # scan matches a change to this entry on it, so the match is exact instead
    # of a guess against the module's own clock.
    written = []
    for start in range(0, len(records), _UPSERT_CALL):
        batch = records[start:start + _UPSERT_CALL]
        body = {"data": batch, "duplicate_check_fields": fields}
        try:
            response = _call("POST", "%s/upsert" % module, body=body)
        except ZohoUnresolvedWrite as exc:
            # Two gaps this entry has to state rather than paper over.
            #
            # Only the update half is reconcilable: a record about to be
            # created has no id and no prior state, so there is nothing to
            # re-read and nothing to compare. The create count is recorded so
            # a reader sees the gap instead of reading `targets` as the whole
            # batch.
            #
            # And the plan only ever fingerprinted Modified_Time on the
            # existing records - it never read their field values - so a later
            # check can tell that a record moved but not what it moved to.
            # Hence verdict_basis, and hence recording the batch index: the
            # batches before this one committed, and re-checking them would be
            # noise at best and a wrong verdict at worst.
            raise _ledger_unresolved(
                exc, "apply_upsert", module, key,
                {"intent": None, "verdict_basis": "modified_time",
                 "fields": fields,
                 "batch_index": start // _UPSERT_CALL,
                 "batch_size": len(batch),
                 "succeeded_before_failure": succeeded,
                 "written_before_failure": list(written),
                 "creates_unreconcilable": stored.get("creates"),
                 "targets": [{"id": r["id"], "before": r["before"],
                              "before_modified_time":
                                  r["before"].get("Modified_Time")}
                             for r in snapshot]})
        for row in (response.get("data") or []):
            if (row or {}).get("code") == "SUCCESS":
                succeeded += 1
                _d = (row or {}).get("details") or {}
                written.append({"id": _d.get("id"),
                                "modified_time": _d.get("Modified_Time")})
            else:
                failed += 1
                if len(errors) < 20:
                    errors.append(row)

    entry = _ledger_append(_ledger_note(
        "applied", "apply_upsert", module, key,
        {"records": len(records), "fields": fields, "fingerprint": expected,
         "updated": stored.get("updates"), "created": stored.get("creates"),
         "written": written,
         "succeeded": succeeded, "failed": failed}))

    _done_calls = (len(records) + _UPSERT_CALL - 1) // _UPSERT_CALL
    return {
        "ok": not failed,
        "action": "upsert %d records into %s" % (len(records), module),
        "succeeded": succeeded,
        "failed": failed,
        "errors": errors,
        "planned_updates": stored.get("updates"),
        "planned_creates": stored.get("creates"),
        "fingerprint_verified": expected,
        "ledger_seq": entry["seq"],
        "summary": (
            "Upserted %d of %d records into %s across %d %s. The plan said "
            "%s would be updated and %s created. Zoho answers HTTP 200 even "
            "when rows fail, so these counts come from the response body."
            % (succeeded, len(records), module, _done_calls,
               "call" if _done_calls == 1 else "calls",
               stored.get("updates"), stored.get("creates"))),
        "origin": _origin(stamp),
    }, None



# =============================================================================
# HYGIENE + READINESS  -  paste into handler.py after zoho_apply_upsert.
#
# Both read-only. Neither needs a scope this module does not already have.
#
# Every field name below returned a valid COQL result against the live org on
# 2026-08-03. A check whose field is rejected reports itself as unavailable
# rather than silently counting zero - a hygiene report that under-reports is
# worse than one that admits a gap.
# =============================================================================

def _iso_days_ago(days):
    return time.strftime("%Y-%m-%dT%H:%M:%S+05:30",
                         time.gmtime(time.time() - days * 86400))


def _iso_date_days_ago(days):
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))


def _hygiene_checks(stale_days):
    """(key, module, label, why, COQL) for each check.

    Field names verified live. `owner_gone` is filled in per call because it
    needs the departed users, so it is not in this table.
    """
    since_ts = _iso_days_ago(stale_days)
    today = _iso_date_days_ago(0)
    return [
        ("stale_leads", "Leads",
         "Leads untouched for %d days" % stale_days,
         "Nobody has worked these. They are either dead or being ignored.",
         "select id, Last_Name, Lead_Status, Modified_Time from Leads "
         "where Modified_Time < '%s'" % since_ts),

        ("stale_deals", "Deals",
         "Open deals untouched for %d days" % stale_days,
         "An open deal nobody has touched in months is a forecast that is "
         "quietly wrong.",
         "select id, Deal_Name, Stage, Modified_Time from Deals "
         "where Modified_Time < '%s' and Stage not in "
         "('Closed Won','Closed Lost')" % since_ts),

        ("overdue_deals", "Deals",
         "Deals past their closing date, still open",
         "The close date has passed and the stage has not moved. The pipeline "
         "says one thing and the calendar says another.",
         "select id, Deal_Name, Stage, Closing_Date from Deals "
         "where Closing_Date < '%s' and Stage not in "
         "('Closed Won','Closed Lost')" % today),

        ("leads_no_email", "Leads",
         "Leads with no email address",
         "Nothing automated can reach these, and nobody will notice until a "
         "campaign silently skips them.",
         "select id, Last_Name, Company from Leads where Email is null"),

        ("contacts_no_email", "Contacts",
         "Contacts with no email address",
         "Same problem, on the records that matter more.",
         "select id, Last_Name, Account_Name from Contacts where Email is null"),
    ]


def _hygiene_owner_checks(departed_ids, departed_names):
    if not departed_ids:
        return []
    id_list = ", ".join("'%s'" % str(i) for i in departed_ids)
    who = ", ".join(departed_names) if departed_names else "an inactive user"
    out = []
    for module, label_field in (("Leads", "Last_Name"), ("Deals", "Deal_Name"),
                                ("Contacts", "Last_Name"),
                                ("Accounts", "Account_Name")):
        out.append((
            "orphaned_%s" % module.lower(), module,
            "%s owned by a deactivated user" % module,
            "Owned by %s, who is no longer active. Nobody is working these and "
            "nobody knows it." % who,
            "select id, %s, Owner from %s where Owner in (%s)"
            % (label_field, module, id_list)))
    return out


def zoho_scan_changes(inputs, stamp):
    """Report records changed since the last run, and which nobody governed.

    A scheduled read. The station holds the position and injects `since`; this
    stores no watermark of its own, so a module update cannot silently skip a
    window and leave nothing in a receipt to show for it.

    It exists to close the ledger's largest gap. The ledger sees only what this
    module wrote, so an edit made in the Zoho UI is invisible to it.
    Modified_Time is not, so a scan can name changes that never passed through
    an approval. A change counts as governed when this module recorded the
    exact post-write Modified_Time Zoho returned - an exact match, not a
    window, so a UI edit one second after a module write is still reported.

    Ordering is ascending and mandatory. The cap truncates, and the station
    advances its watermark to the newest row returned; in any other order a
    truncated page leaves older rows behind the mark and they are never seen
    again.

    Honest limits, all of them:
      - This finds ungoverned EDITS. A UI delete leaves nothing to poll, and a
        UI merge makes the loser invisible to COQL.
      - Coverage starts at the earliest entry in the live ledger chain. The
        chain rotates; sealed archives are not read here.
      - Entries written before the ledger recorded Modified_Time, and every
        merge, cannot be matched. The count is reported, but their records may
        appear as ungoverned when they were not.
      - Modified_By is Zoho's record of who last touched the record. For writes
        this module made it is always the OAuth user, so it names a person only
        for changes the module did not make.
      - Counts are inline; the records go to a file. redact_output scrubs ids
        and dates out of a receipt, so anything actionable has to be a path.

    inputs: modules (array, default Leads/Contacts/Accounts/Deals),
            limit (number, cap per module), since and exclude_ids (injected)
    """
    helpers = __rc_helpers__  # noqa: F821
    modules = inputs.get("modules") or list(_SCAN_MODULES)
    if not isinstance(modules, list):
        raise RuntimeError("'modules' must be a list of Zoho API names.")
    cap = int(inputs.get("limit") or _SCAN_CHANGES_CAP)
    since_raw = str(inputs.get("since") or "").strip()
    seen = set(str(x) for x in (inputs.get("exclude_ids") or []))

    since_utc = _iso_utc(since_raw) if since_raw else None
    if since_raw and since_utc is None:
        # The station injects this. Scanning from the beginning of time on an
        # unreadable value would look like success while spending the day's
        # API budget, so refuse and name the value that was rejected.
        raise RuntimeError(
            "'since' is not a timestamp this command can read: %r. Expected "
            "ISO-8601, for example 2026-08-08T11:14:05Z." % since_raw)

    governed, covers_from, unmatchable = _governed_index()

    changes, ungoverned = [], []
    rows_scanned, skipped_seen, truncated = 0, 0, False
    for module in modules:
        columns = "id, Modified_Time, Modified_By, Created_Time"
        if since_utc:
            # COQL compares in UTC and accepts a bare Z literal against a
            # record stamped +05:30 (verified live); an unzoned literal is
            # rejected outright, which is the safe way for it to fail.
            where = "Modified_Time > '%s'" % since_utc
        else:
            where = "id is not null"
        rows, hit_cap = _coql_capped(
            "select %s from %s where %s order by Modified_Time asc"
            % (columns, module, where),
            cap=cap, label="change scan on " + module)
        truncated = truncated or hit_cap
        rows_scanned += len(rows)
        for row in rows:
            rid = row.get("id")
            if not rid:
                continue
            at = _iso_utc(row.get("Modified_Time"))
            # The cursor identifies a CHANGE, not a record. A record edited
            # twice is two items; keying on id alone would have the station
            # suppress the second edit as already delivered.
            ref = "%s:%s:%s" % (module, rid, at or "unreadable")
            if ref in seen:
                skipped_seen += 1
                continue
            by = row.get("Modified_By") or {}
            is_governed = at is not None and ref in governed
            changes.append({"change_ref": ref, "module": module, "id": rid,
                            "modified_at": at, "governed": is_governed})
            if not is_governed:
                ungoverned.append({
                    "change_ref": ref, "module": module, "id": rid,
                    "modified_at": at,
                    "modified_at_raw": row.get("Modified_Time"),
                    "modified_by": by.get("name"),
                    "modified_by_id": by.get("id"),
                    "created_at": row.get("Created_Time"),
                })

    name = "scan_changes" + time.strftime(".%Y%m%dT%H%M%SZ", time.gmtime()) + ".json"
    path = str(helpers.get("WS") or "").rstrip("/") + "/" + name
    helpers["jsave"](path, {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "since": since_utc,
        "modules": modules,
        "ledger_covers_from": covers_from,
        "unmatchable_ledger_entries": unmatchable,
        "ungoverned": ungoverned,
        "note": "Record ids here are unredacted; the same values come back as "
                "[account] through a receipt.",
    })

    window = since_utc or "the start of the scan window"
    if truncated:
        summary = ("Hit the %d-record cap with more waiting. %d changes "
                   "returned, %d with no ledger entry. The watermark will not "
                   "advance past what was not returned, so the next run "
                   "continues from here." % (cap, len(changes), len(ungoverned)))
    elif not changes:
        summary = "No changes since %s." % window
    else:
        summary = ("%d changes since %s. %d have no matching ledger entry and "
                   "are listed in %s. Ledger coverage starts %s; %d applied "
                   "entries cannot be matched."
                   % (len(changes), window, len(ungoverned), path,
                      covers_from or "never", unmatchable))

    return {
        "ok": True,
        "count": len(changes),
        "changes": changes,
        "since": since_utc,
        "rows_scanned": rows_scanned,
        "skipped_already_delivered": skipped_seen,
        "ungoverned_count": len(ungoverned),
        "truncated": truncated,
        "report_path": path,
        "ledger_covers_from": covers_from,
        "unmatchable_ledger_entries": unmatchable,
        "covers": "ungoverned edits only; a UI delete or merge leaves nothing "
                  "to poll",
        "summary": summary,
        "origin": _origin(stamp),
    }, None


def zoho_hygiene_scan(inputs, stamp):
    """Find what is quietly rotting in the CRM. Read-only.

    Zoho's own reports will tell you these numbers. What it will not do is hand
    the result to a governed write: every finding here names the command that
    fixes it, and that command still plans, fingerprints and refuses on drift.

    A check whose field the org rejects is reported as unavailable rather than
    counted as zero. A hygiene report that quietly under-reports is worse than
    one that admits a gap.

    inputs: stale_days (number, default 90), include (array of check keys,
            optional), sample (number, default 5)
    """
    # `or 90` would swallow a deliberate 0 and silently scan 90 days instead.
    raw_days = inputs.get("stale_days")
    stale_days = 90 if raw_days in (None, "") else int(raw_days)
    if stale_days < 1:
        raise RuntimeError("'stale_days' must be at least 1; %r would scan "
                           "everything." % raw_days)
    raw_sample = inputs.get("sample")
    sample = 5 if raw_sample in (None, "") else int(raw_sample)
    sample = max(0, min(sample, 25))
    wanted = inputs.get("include")
    if isinstance(wanted, str):
        wanted = [wanted]
    wanted = set(wanted) if isinstance(wanted, list) and wanted else None

    # Deactivated users, so their records can be found. list_users already
    # exposes this; the scan reuses the same call rather than a new scope.
    departed_ids, departed_names = [], []
    try:
        users = (_call("GET", "users", params={"type": "AllUsers"})
                 .get("users") or [])
        for u in users:
            active = u.get("status")
            if active and str(active).lower() != "active":
                departed_ids.append(str(u.get("id")))
                departed_names.append(str(u.get("full_name")
                                           or u.get("email") or u.get("id")))
    except Exception:
        departed_ids, departed_names = [], []

    checks = _hygiene_checks(stale_days) + _hygiene_owner_checks(
        departed_ids, departed_names)

    findings, unavailable, total = [], [], 0
    for key, module, label, why, query in checks:
        if wanted and key not in wanted:
            continue
        try:
            rows = _coql_all(query, cap=_SCAN_CAP, label=label)
        except Exception as e:                                  # noqa: BLE001
            unavailable.append({"check": key, "module": module,
                                "label": label, "reason": str(e)[:160]})
            continue
        if not rows:
            continue
        total += len(rows)
        findings.append({
            "check": key,
            "module": module,
            "label": label,
            "why_it_matters": why,
            "count": len(rows),
            "sample": rows[:sample],
            "query": query,
            "fix_with": ("zoho.plan_handover" if key.startswith("orphaned_")
                         else "zoho.plan_update"),
        })

    findings.sort(key=lambda f: -f["count"])

    # Write the full findings to a file and return the path.
    #
    # redact_output scrubs identifiers AND date-shaped values before sealing a
    # receipt: a sample row comes back with "id": "[account]" and
    # "Closing_Date": "[date]", and the COQL string loses its date literal, so
    # the query cannot be pasted into plan_update - which is the one thing it
    # is there for. Verified live on station v0.61.
    #
    # audit_pack already proves a file path survives redaction, so the same
    # move works here: counts and labels inline for the operator to scan, the
    # actionable detail in a file they can open.
    helpers = __rc_helpers__  # noqa: F821
    name = "hygiene_scan" + time.strftime(".%Y%m%dT%H%M%SZ", time.gmtime()) + ".json"
    path = str(helpers.get("WS") or "").rstrip("/") + "/" + name
    helpers["jsave"](path, {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stale_days": stale_days,
        "deactivated_users": departed_names,
        "findings": findings,
        "unavailable": unavailable,
        "note": "Queries and sample rows here are unredacted; the same values "
                "come back as [account] and [date] through a receipt.",
    })

    # Inline: everything that survives redaction and is worth scanning. The
    # per-finding query and sample rows live in the file, not here.
    inline = [{"check": f["check"], "module": f["module"], "label": f["label"],
               "why_it_matters": f["why_it_matters"], "count": f["count"],
               "fix_with": f["fix_with"]} for f in findings]

    return {
        "ok": True,
        "stale_days": stale_days,
        "checks_run": len(checks) if not wanted else len(findings) + len(unavailable),
        "findings": inline,
        "report_path": path,
        "issues_found": len(findings),
        "records_affected": total,
        "unavailable": unavailable,
        "deactivated_users": departed_names,
        "summary": (
            "%d issue type(s) across %d records. %sThe full report, with the "
            "COQL behind each finding and the matching record ids, is at %s - "
            "receipts redact identifiers and dates, so the query is in the "
            "file rather than here. Feed it to zoho.plan_update, or reassign "
            "an inactive user's book with zoho.plan_handover; either way that "
            "write still fingerprints and still refuses on drift. Nothing "
            "here changes anything."
            % (len(findings), total,
               ("%d check(s) could not run and are listed under unavailable; "
                "they are not counted as zero. " % len(unavailable))
               if unavailable else "", name)),
        "origin": _origin(stamp),
    }, None


def zoho_check_readiness(inputs, stamp):
    """Check one record against the fields the module says are required, plus
    any you name. Read-only.

    Zoho enforces required fields on its own forms. It does not enforce them
    on an API write, and it does not enforce your rules at all - the ones that
    are not about validity but about a record being ready to act on. A deal
    with no closing date will save happily and then sit in a forecast being
    wrong.

    inputs: module, record_id, require (array of extra field api_names,
            optional)
    """
    module = _module_name(inputs)
    record_id = _record_id(inputs)

    extra = inputs.get("require")
    if isinstance(extra, str):
        extra = [extra]
    extra = [str(f).strip() for f in (extra or []) if str(f).strip()]

    meta = _call("GET", "settings/fields", params={"module": module})
    declared = []
    for f in (meta.get("fields") or []):
        api = f.get("api_name")
        if not api or f.get("read_only"):
            continue
        if f.get("system_mandatory") or f.get("required"):
            declared.append(api)

    response = _call("GET", "%s/%s" % (module, record_id))
    rows = response.get("data") or []
    if not rows:
        raise RuntimeError("Record %s not found in %s." % (record_id, module))
    record = rows[0]

    def _empty(value):
        return value in (None, "", [], {})

    missing_required, missing_requested, present = [], [], []
    for field in sorted(set(declared)):
        (missing_required if _empty(record.get(field)) else present).append(field)
    for field in extra:
        if field not in record:
            missing_requested.append({"field": field,
                                      "note": "not a field on this module"})
        elif _empty(record.get(field)):
            missing_requested.append({"field": field, "note": "empty"})

    ready = not missing_required and not missing_requested
    return {
        "ok": True,
        "module": module,
        "record_id": record_id,
        "ready": ready,
        "missing_required": missing_required,
        "missing_requested": missing_requested,
        "required_fields_checked": len(declared),
        "extra_fields_checked": len(extra),
        "summary": (
            "Record is ready: %d module-required field(s) present%s."
            % (len(present),
               (" and all %d requested field(s) filled" % len(extra))
               if extra else "")
            if ready else
            "Record is NOT ready. Missing %d module-required field(s)%s. "
            "Zoho will accept an API write anyway - it enforces required "
            "fields on its own forms, not on the API - so this is the check "
            "that does not otherwise happen."
            % (len(missing_required),
               (" and %d requested field(s)" % len(missing_requested))
               if missing_requested else "")),
        "origin": _origin(stamp),
    }, None


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
    found, _before = _handover_scan(module_names, str(leaver["id"]),
                                    include_closed)
    total = sum(len(v) for v in found.values())
    if not total:
        raise RuntimeError("%s owns nothing in %s, so there is nothing to hand "
                           "over." % (leaver.get("full_name"),
                                      ", ".join(module_names)))

    excluded = 0
    if "Deals" in module_names and not include_closed:
        every = _handover_scan(["Deals"], str(leaver["id"]), True)[0]["Deals"]
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
    """Reassign everything in the plan, refusing if the set changed.

    Writes one applied ledger entry per module, carrying the post-write
    Modified_Time Zoho returned for each record. Without it scan_changes
    reported every governed reassignment as an ungoverned edit - a false
    positive in the one command whose entire job is to have none.
    """
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

    found, before_times = _handover_scan(module_names, str(leaver["id"]),
                                         closed == "include")
    handover_key = _plan_key("handover",
                             str(leaver["id"]) + ">" + str(taker["id"]),
                             {"modules": module_names, "closed": closed})
    if _handover_fingerprint(found) != stored.get("fingerprint"):
        now = {m: len(v) for m, v in found.items()}
        # One entry per module, because the ledger is joined to a record on
        # module and id - a single entry spanning four modules could not be
        # matched to any of them. Written before the raise, like every other
        # refusal.
        for _module, _ids in found.items():
            if not _ids:
                continue
            _ledger_append(_ledger_note(
                "refused", "apply_handover", _module, handover_key,
                {"reason": "what the leaver owns changed between plan and apply",
                 "expected": stored.get("found"), "actual": now,
                 "fields": ["Owner"],
                 "from_user": str(leaver["id"]), "to_user": str(taker["id"]),
                 "records": len(_ids),
                 "targets": [str(i) for i in _ids]}))
        raise RuntimeError(
            "Refusing to hand over. What %s owns changed since the plan was "
            "made: now %s, planned %s. Re-run zoho.plan_handover."
            % (leaver.get("full_name"), now, stored.get("found")))

    owner = {"Owner": {"id": str(taker["id"])}}
    moved, failed, errors = 0, 0, []
    done = {}
    written_by_module = {}
    for module, ids in found.items():
        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            if not batch:
                continue
            payload = [dict(owner, id=rid) for rid in batch]
            try:
                result = _call("PUT", module, body={"data": payload})
            except ZohoUnresolvedWrite as exc:
                # A handover is many writes, not one, so the entry has to say
                # where in the sequence it stopped. `modules_completed` and
                # `moved_before_failure` are what already committed; the
                # targets are this batch alone, because those are the only
                # records whose fate is actually in doubt.
                #
                # The entry's module is the one being written when it failed,
                # not "handover" - anything joining the ledger to a record
                # matches on module and id.
                raise _ledger_unresolved(
                    exc, "apply_handover", module, handover_key,
                    {"intent": dict(owner), "fields": ["Owner"],
                     "from_user": str(leaver["id"]),
                     "to_user": str(taker["id"]),
                     "batch_index": start // 100,
                     "modules_completed": dict(done),
                     "moved_before_failure": moved,
                     "not_attempted": {m: len(v) for m, v in found.items()
                                       if m not in done and m != module},
                     "targets": [
                         {"id": rid,
                          "before": {"Owner": str(leaver["id"])},
                          "before_modified_time":
                              (before_times.get(module) or {}).get(rid)}
                         for rid in batch]})
            for row in result.get("data", []) or []:
                if row.get("code") == "SUCCESS":
                    moved += 1
                    _d = row.get("details") or {}
                    written_by_module.setdefault(module, []).append(
                        {"id": _d.get("id"),
                         "modified_time": _d.get("Modified_Time")})
                else:
                    failed += 1
                    errors.append({"module": module, "code": row.get("code"),
                                   "message": row.get("message")})
        done[module] = len(ids)

    if moved == 0 and failed:
        raise RuntimeError("Zoho rejected every reassignment. First error: %s"
                           % errors[0])

    # One entry per module, not one per handover. _ledger_index joins the
    # ledger to a record on module and id, so a single entry spanning four
    # modules could not be matched to a record in any of them - which is the
    # same reason the refusal above is written per module.
    #
    # This is what closes the false positive: before it, every record a
    # governed handover moved came back from scan_changes as an ungoverned
    # edit, because Owner and Modified_Time had both moved and nothing in the
    # ledger said why.
    ledger_seqs = {}
    for module, ids in found.items():
        if not ids:
            continue
        entry = _ledger_append(_ledger_note(
            "applied", "apply_handover", module, handover_key,
            {"records": len(ids), "fields": ["Owner"],
             "changes": dict(owner),
             "from_user": str(leaver["id"]), "to_user": str(taker["id"]),
             "written": _governed_written(written_by_module.get(module)),
             # Every id attempted, not only the ones Zoho confirmed. A record
             # that failed still had a governed attempt made against it, and
             # `written` is where the difference is recorded.
             "before": [{"id": rid, "before": {"Owner": str(leaver["id"])}}
                        for rid in ids],
             "targets": [str(i) for i in ids]}))
        ledger_seqs[module] = entry["seq"]

    return {
        "ok": True,
        "action": "handover %s to %s" % (leaver.get("full_name"),
                                         taker.get("full_name")),
        "moved": moved,
        "failed": failed,
        "per_module": {m: len(v) for m, v in found.items()},
        "errors": errors[:10],
        "ledger_seqs": ledger_seqs,
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


# =============================================================================
# LEDGER  -  paste this block into handler.py immediately after the
# plan / apply section (after zoho_apply_delete, before the handover section).
#
# Why this exists, and what it is not.
#
# Every governed change already produces a platform receipt. Two things those
# receipts cannot do. First, redact_output strips identifiers before sealing,
# so a receipt cannot answer "which records changed". Second, a refusal raises,
# and the plan that proved it expires an hour later - so the evidence that the
# control fired is the evidence that gets thrown away.
#
# This keeps a local append-only ledger of applies and refusals. Each entry
# carries the hash of the entry before it, so removing or editing any entry
# breaks every link after it and zoho.verify_ledger will say where.
#
# HONEST LIMITS, and these belong in the docs verbatim:
#   - It records changes made THROUGH this module. Someone editing in the Zoho
#     UI is invisible to it. It is a record of governed changes, not an audit
#     trail of the org.
#   - The chain is tamper-EVIDENT, not tamper-proof. Anyone who can write the
#     file can rewrite the whole chain from any point. It proves nothing was
#     edited in place; it does not prove nothing was replaced wholesale.
#   - Entries hold record ids and prior field values, so the file is PII. It
#     lives in the station workspace beside the plans.
# =============================================================================

def _module_version():
    """Read the version off the module docstring rather than repeating it.

    The version already lives in two places and has drifted apart three times.
    A third literal in this file would be a fourth chance to get it wrong.
    """
    found = re.search(r"shweta/zoho-crm ([0-9.]+)", __doc__ or "")
    return found.group(1) if found else "unknown"


_LEDGER_FILE = "zoho_ledger.json"
_LEDGER_MAX = 5000          # rotate past this so a single file stays readable
_GENESIS = "sha256:" + "0" * 64


def _ledger_path(suffix=""):
    helpers = __rc_helpers__  # noqa: F821
    base = str(helpers.get("WS") or "").rstrip("/") + "/" + _LEDGER_FILE
    return base + suffix


def _entry_hash(entry, prev):
    """Hash an entry together with its predecessor's hash.

    `entry_hash` is excluded from its own input, obviously. Canonical
    serialisation so the same entry always hashes the same way, the same rule
    _fingerprint follows.
    """
    body = {k: v for k, v in entry.items() if k != "entry_hash"}
    blob = _json.dumps({"prev": prev, "entry": body},
                       sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ledger_load():
    helpers = __rc_helpers__  # noqa: F821
    book = helpers["jload"](_ledger_path(), {}) or {}
    entries = book.get("entries")
    return {
        "chain_start": book.get("chain_start") or _GENESIS,
        "entries": entries if isinstance(entries, list) else [],
    }


def _ledger_append(record):
    """Append one entry and return it, chained to whatever came before.

    Called on every apply, successful or refused. A refusal is the more
    valuable of the two: it is the only direct evidence the control works, and
    custody_report reads them.

    Every refusal carries `targets`, the ids it declined to touch. A count
    cannot be joined to a record, so an entry without them is evidence that a
    control fired on somebody, which is not the question anyone asks.
    """
    helpers = __rc_helpers__  # noqa: F821
    book = _ledger_load()
    entries = book["entries"]

    if len(entries) >= _LEDGER_MAX:
        # Seal the current file under a timestamped name and start a fresh
        # chain whose start is the last sealed hash, so the archive and the
        # live file remain verifiable as one sequence.
        stamp_name = time.strftime(".%Y%m%dT%H%M%SZ", time.gmtime())
        helpers["jsave"](_ledger_path(stamp_name), book)
        book = {"chain_start": entries[-1]["entry_hash"], "entries": []}
        entries = book["entries"]

    prev = entries[-1]["entry_hash"] if entries else book["chain_start"]
    entry = dict(record)
    entry["seq"] = len(entries) + 1
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry["prev"] = prev
    entry["entry_hash"] = _entry_hash(entry, prev)

    entries.append(entry)
    helpers["jsave"](_ledger_path(), book)
    return entry


def _ledger_verify():
    """Walk the chain and report the first entry that does not hold.

    Returns (intact, checked, first_bad_seq_or_None).
    """
    book = _ledger_load()
    prev = book["chain_start"]
    for index, entry in enumerate(book["entries"], start=1):
        if not isinstance(entry, dict):
            return False, index - 1, index
        if entry.get("prev") != prev:
            return False, index - 1, index
        if entry.get("entry_hash") != _entry_hash(entry, prev):
            return False, index - 1, index
        prev = entry["entry_hash"]
    return True, len(book["entries"]), None


def _ledger_note(outcome, command, module, key, detail):
    """Build the common shape. Kept in one place so entries stay comparable."""
    return {"outcome": outcome, "command": command, "module": module,
            "plan_key": key, "detail": detail}


def _ledger_unresolved(exc, command, module, key, detail):
    """Record a write whose outcome Zoho never confirmed, and hand back the
    exception the caller should raise.

    The module already reasoned correctly about an unresolved write and then
    discarded the reasoning: the operator was told to go and check, and
    whatever they found left no trace. This keeps it.

    Returns the replacement exception instead of raising it, so a call site
    reads `raise _ledger_unresolved(...)` and the raise stays visible where
    it happens rather than hiding inside a helper.

    `attempted_at` is this module's own UTC clock, which is a lower bound
    and not Zoho's clock: the two differ by the request's flight time.
    Anything comparing it against a Modified_Time has to read it as "no
    earlier than", or a write that landed just before a timeout reads as one
    that never did.

    Two conventions these entries follow, so a later reader can rely on them:

    `intent` is entry-level when every target was being set to the same
    thing, and null at entry level when it differs per record - a target then
    carries its own `intent` (rollback does this).

    `verdict_basis` says how the outcome can be established at all. "value"
    means compare the recorded fields against what the record holds now.
    "existence" means there is no value to compare and the question is
    whether the record is still there - delete and merge. "modified_time"
    means the prior values were never read, so only movement can be judged,
    not what it moved to - upsert. Getting this wrong is how a reconciler
    reports a confident verdict it has no basis for.
    """
    body = dict(detail)
    body["reason"] = str(exc)
    body["attempted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body.setdefault("verdict_basis", "value")
    entry = _ledger_append(_ledger_note("unresolved", command, module, key,
                                        body))
    return ZohoUnresolvedWrite(
        "%s The attempt is recorded as ledger entry %d, holding the prior "
        "values and each record's Modified_Time from just before the call, "
        "so what happened can be established by re-reading those records."
        % (exc, entry["seq"]))


# --- ledger commands --------------------------------------------------------

def zoho_verify_ledger(inputs, stamp):
    """Check the local change ledger has not been edited in place.

    Read-only. Recomputes every link from the chain start and names the first
    entry that fails, if any.
    """
    intact, checked, bad = _ledger_verify()
    book = _ledger_load()
    applied = sum(1 for e in book["entries"] if e.get("outcome") == "applied")
    refused = sum(1 for e in book["entries"] if e.get("outcome") == "refused")
    unresolved = sum(1 for e in book["entries"]
                     if e.get("outcome") == "unresolved")
    reconciled = sum(1 for e in book["entries"]
                     if e.get("outcome") == "reconciled")

    if intact:
        summary = ("Ledger intact. %d entries verified, %d applied, %d refused, "
                   "%d unresolved and %d reconciled. Tamper-evident, not "
                   "tamper-proof: this proves no entry was altered in place."
                   % (checked, applied, refused, unresolved, reconciled))
        if unresolved:
            # Counted separately rather than folded into applied, because that
            # is the whole point: Zoho never said whether these landed, and a
            # summary that guessed either way would be the fake-green this
            # module exists to avoid.
            summary += (" The %d unresolved %s a write Zoho never returned a "
                        "verdict on; the entry holds what was attempted and "
                        "the prior values, so it can be checked by hand."
                        % (unresolved,
                           "entry is" if unresolved == 1 else "entries are each"))
    else:
        summary = ("Ledger BROKEN at entry %d. The %d entries before it verify; "
                   "entry %d and everything after it cannot be trusted."
                   % (bad, checked, bad))

    return {
        "ok": True,
        "intact": intact,
        "entries_verified": checked,
        "first_broken_entry": bad,
        "applied": applied,
        "refused": refused,
        "unresolved": unresolved,
        "reconciled": reconciled,
        "covers": "changes made through this module only; edits made in the "
                  "Zoho UI are not visible here. An unresolved entry records a "
                  "write whose outcome Zoho never confirmed - it is neither an "
                  "applied change nor a refused one. A reconciled entry records "
                  "what a later re-read inferred about one of those, which is "
                  "an inference and not a confirmation",
        "summary": summary,
        "origin": _origin(stamp),
    }, None


def zoho_audit_pack(inputs, stamp):
    """Write the change ledger out as a reviewable file.

    Read-only. The pack is written to the station workspace and this returns
    the path and the counts, not the contents: redact_output would strip the
    record ids out of anything returned inline, which is the whole point of the
    pack. A human reads the file.

    inputs: since (ISO date, optional), until (ISO date, optional),
            module (optional), outcome ('applied' | 'refused', optional)
    """
    helpers = __rc_helpers__  # noqa: F821
    since = str(inputs.get("since", "")).strip()
    until = str(inputs.get("until", "")).strip()
    module = str(inputs.get("module", "")).strip()
    outcome = str(inputs.get("outcome", "")).strip().lower()
    if outcome and outcome not in ("applied", "refused", "unresolved",
                                   "reconciled"):
        raise RuntimeError("'outcome' must be 'applied', 'refused', "
                           "'unresolved' or 'reconciled' if given, got %r."
                           % outcome)

    intact, checked, bad = _ledger_verify()
    book = _ledger_load()

    rows = []
    for entry in book["entries"]:
        at = str(entry.get("at") or "")
        if since and at < since:
            continue
        if until and at > until:
            continue
        if module and entry.get("module") != module:
            continue
        if outcome and entry.get("outcome") != outcome:
            continue
        rows.append(entry)

    pack = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "module_version": _module_version(),
        "filter": {"since": since or None, "until": until or None,
                   "module": module or None, "outcome": outcome or None},
        "chain": {"intact": intact, "entries_verified": checked,
                  "first_broken_entry": bad,
                  "chain_start": book["chain_start"]},
        "scope_note": ("Records changes made through shweta/zoho-crm. Edits "
                       "made directly in the Zoho UI are not represented. An "
                       "entry with outcome 'unresolved' is a write Zoho never "
                       "returned a verdict on: it may have landed, may not, "
                       "and may have landed for some of its targets only. A "
                       "'reconciled' entry holds what a later re-read inferred "
                       "about one of those, from the record's current state - "
                       "an inference, never a confirmation."),
        "counts": {
            "entries": len(rows),
            "applied": sum(1 for r in rows if r.get("outcome") == "applied"),
            "refused": sum(1 for r in rows if r.get("outcome") == "refused"),
            "unresolved": sum(1 for r in rows
                              if r.get("outcome") == "unresolved"),
            "reconciled": sum(1 for r in rows
                              if r.get("outcome") == "reconciled"),
            # Deliberately counts applied entries only. An unresolved entry has
            # targets but no known outcome, so adding its records here would
            # claim changes that may never have happened.
            "records_changed": sum(int(r.get("detail", {}).get("records") or 0)
                                   for r in rows if r.get("outcome") == "applied"),
        },
        "entries": rows,
    }

    name = "audit_pack" + time.strftime(".%Y%m%dT%H%M%SZ", time.gmtime()) + ".json"
    path = str(helpers.get("WS") or "").rstrip("/") + "/" + name
    helpers["jsave"](path, pack)

    return {
        "ok": True,
        "pack_path": path,
        "entries": pack["counts"]["entries"],
        "applied": pack["counts"]["applied"],
        "refused": pack["counts"]["refused"],
        "unresolved": pack["counts"]["unresolved"],
        "reconciled": pack["counts"]["reconciled"],
        "records_changed": pack["counts"]["records_changed"],
        "chain_intact": intact,
        "summary": ("Wrote %d ledger entries to %s - %d applied, %d refused, "
                    "%d unresolved, %d records changed. Chain %s. Refusals are "
                    "the useful half: they are the evidence the control fired. "
                    "The unresolved count is writes Zoho never confirmed either "
                    "way, and is not included in records changed."
                    % (pack["counts"]["entries"], name,
                       pack["counts"]["applied"], pack["counts"]["refused"],
                       pack["counts"]["unresolved"],
                       pack["counts"]["records_changed"],
                       "intact" if intact else "BROKEN at entry %d" % bad)),
        "origin": _origin(stamp),
    }, None




# --- reconciliation ---------------------------------------------------------
#
# Feature 1 keeps an unresolved write instead of discarding it. This is the
# other half: re-read what was attempted and report what can and cannot be
# established. Read-only; nothing here writes to Zoho.
#
# One fact shapes every line below. Zoho cannot be asked whether a request
# landed. Every verdict is inferred from the record's current state, so the
# strongest honest statement available is "consistent with having landed". A
# command that printed "confirmed applied" would be worth less than no command
# at all, because someone would believe it.

_RECON_READ = 100          # ids per COQL read; the same ceiling _read_by_ids
                           # is used with everywhere else in this module
_RECON_BASES = ("value", "existence", "modified_time")


def _recon_flat(value):
    """Flatten a Zoho value so comparing two of them means what it looks like.

    A lookup field is WRITTEN as {"id": "..."} and READ BACK as
    {"name": "...", "id": "..."}. Comparing the raw dicts would report every
    single Owner reassignment as a mismatch, which would make apply_handover
    entries permanently unknown. The id is the part that was being set.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        if "id" in value:
            return str(value["id"])
        return _json.dumps(value, sort_keys=True, separators=(",", ":"),
                           default=str)
    return str(value)


def _recon_intent(entry_intent, target):
    """Intent for one target: its own if it has one, else the entry's.

    apply_rollback restores every record to its own prior values, so a single
    entry-level intent would be wrong for all but one of them. Every other
    apply path sets the same thing on every target and states it once.
    """
    own = target.get("intent")
    if isinstance(own, dict):
        return own
    return entry_intent if isinstance(entry_intent, dict) else {}


def _recon_columns(entry):
    """COQL columns needed to adjudicate one entry.

    Field names here come off a file on disk and are interpolated into COQL by
    _read_by_ids. The ledger is tamper-EVIDENT, not tamper-proof - verify_ledger
    says so in its own output - so a name that does not look like an api_name is
    dropped rather than trusted. apply_update validates the same way before it
    writes, and for the same reason.
    """
    detail = entry.get("detail") or {}
    wanted = set()
    if detail.get("verdict_basis") == "value":
        for field in (detail.get("fields") or []):
            wanted.add(str(field))
        entry_intent = detail.get("intent")
        for target in (detail.get("targets") or []):
            wanted.update(_recon_intent(entry_intent, target).keys())
    wanted.update(("Modified_Time", "Modified_By"))
    safe = sorted(f for f in wanted if str(f).replace("_", "").isalnum())
    return safe or ["Modified_Time"]


def _recon_read(module, ids, columns):
    """Current state of each id, or None for one that no longer reads back.

    Returns (by_id, failed). A read that errors is NOT treated as "deleted":
    a COQL failure and a deleted record are completely different facts, and
    calling the first one the second would manufacture a `landed` verdict for
    an existence check out of a network problem.
    """
    by_id, failed = {}, None
    for start in range(0, len(ids), _RECON_READ):
        batch = ids[start:start + _RECON_READ]
        try:
            for row in _read_by_ids(module, batch, columns):
                if row.get("id"):
                    by_id[str(row["id"])] = row
        except Exception as error:                              # noqa: BLE001
            failed = str(error)[:200]
            break
    return by_id, failed


def _recon_moved(target, current):
    """Has Modified_Time moved since just before the attempt?

    Returns True, False, or None when it cannot be told. Both sides go through
    _iso_utc, because Zoho renders +05:30 and a raw string comparison would
    call two spellings of the same instant a change.
    """
    before = _iso_utc(target.get("before_modified_time"))
    now = _iso_utc((current or {}).get("Modified_Time"))
    if before is None or now is None:
        return None
    return now != before


def _recon_same(current, values, columns):
    """Do the record's current values match `values` on every named column?"""
    if not columns:
        return False
    for field in columns:
        if _recon_flat((current or {}).get(field)) != _recon_flat(values.get(field)):
            return False
    return True


def _recon_one(basis, target, current, entry_intent):
    """One record's verdict. Returns (verdict, note).

    The three bases are genuinely different questions, not one question with
    exceptions - see each branch. Anything unrecognised is `unknown`; guessing
    a default from the command name is how a reconciler ends up confident about
    a record it has no evidence for.
    """
    intent = _recon_intent(entry_intent, target)
    before = target.get("before") or {}
    moved = _recon_moved(target, current)
    present = current is not None

    if basis == "existence":
        # A delete sets no value, so the only question the API can answer is
        # whether the record is still there. The value table's last row
        # inverts here: gone IS the signal, not the absence of one.
        if not present:
            return "landed", "record no longer readable, which is what was asked"
        if moved is False:
            return "not_landed", "record still present and untouched since the attempt"
        return "unknown", ("record still present but modified since the attempt; "
                           "a failed delete followed by an edit is "
                           "indistinguishable from a delete that never fired")

    if basis == "modified_time":
        # apply_upsert fingerprinted Modified_Time alone - it never read the
        # field values it was about to overwrite. Movement is visible;
        # direction is not. This branch can never return `landed`, and the
        # command output says so rather than letting a zero read as "none of
        # them landed".
        if not present:
            return "unknown", "record no longer readable; nothing left to inspect"
        if moved is False:
            return "not_landed", "untouched since the attempt"
        if moved is None:
            return "unknown", "no usable Modified_Time to compare"
        return "unknown", ("record was modified, but its prior values were "
                           "never recorded, so this cannot say by whom or to what")

    if basis != "value":
        return "unknown", "unrecognised verdict_basis %r" % (basis,)

    if not present:
        return "unknown", "record no longer readable; deleted or merged since"
    if moved is None:
        return "unknown", "no usable Modified_Time to compare"

    columns = sorted(intent)
    hit_intent = _recon_same(current, intent, columns)
    hit_before = _recon_same(current, before, columns)

    # A write that set a field to the value it already held cannot be
    # adjudicated at all: landing and not landing produce identical records.
    # Caught before the table, because both of its first two rows match and
    # whichever were checked first would win by accident.
    if columns and hit_intent and hit_before:
        return "unknown", ("the intended value is the value it already held, "
                           "so landing and not landing look the same")
    if hit_intent and moved:
        return "landed", "current value matches the intent and the record moved"
    if hit_before and not moved:
        return "not_landed", "nothing has touched this record since the plan"
    if hit_before and moved:
        return "unknown", ("value is unchanged but the record moved: either the "
                           "write missed and something else edited it, or it "
                           "landed and was reverted")
    return "unknown", "value matches neither the intent nor the prior state"


def _recon_open(book, only_seq=None):
    """Unresolved entries still awaiting a verdict, oldest first.

    An entry is closed only by a `reconciled` entry that actually resolved it.
    A reconciliation that determined nothing leaves it open on purpose: the
    look happened and is recorded, but the question is still unanswered, and
    hiding it would be the fake-green this module exists to avoid.
    """
    closed = set()
    for entry in (book.get("entries") or []):
        if entry.get("outcome") != "reconciled":
            continue
        detail = entry.get("detail") or {}
        if detail.get("resolved") and detail.get("resolves_seq") is not None:
            closed.add(detail["resolves_seq"])
    out = []
    for entry in (book.get("entries") or []):
        if entry.get("outcome") != "unresolved":
            continue
        if only_seq is not None and entry.get("seq") != only_seq:
            continue
        if entry.get("seq") in closed:
            continue
        out.append(entry)
    return out


def zoho_reconcile_writes(inputs, stamp):
    """Work out what happened to writes Zoho never confirmed. Read-only.

    Feature 1 records an attempt when a write returns no verdict. This re-reads
    those records and reports, per record, whether the current state is
    consistent with the write having landed.

    HONEST LIMITS, all of them, and all repeated in the output because that is
    where an operator will read them:

      - A verdict is INFERRED from current state. Zoho has no way to be asked
        whether a request landed, so nothing here is a confirmation.
      - `landed` means the record's current value matches the intent and the
        record moved. Another actor making the same change independently is
        indistinguishable from the write having landed.
      - A record edited again after the attempt cannot be adjudicated at all.
      - What can be established differs by which command made the attempt. An
        apply_upsert entry can never be called landed, because the values it
        was writing over were never read.
      - Entries written before this feature carry no before_modified_time.
        They are reported separately rather than mixed into the counts.
      - A merge is checked for existence only. Master present and losers gone
        is consistent with a completed merge and is not proof of one.

    Verdicts are per record, not per entry. A batch of 100 can legitimately be
    part landed and part not, and that is the case an operator cannot work out
    by hand - it is the reason this command earns its place.

    inputs: ledger_seq (number, optional), record_outcome (boolean, default
            false)
    """
    helpers = __rc_helpers__  # noqa: F821
    raw_seq = inputs.get("ledger_seq")
    only_seq = None
    if raw_seq not in (None, ""):
        try:
            only_seq = int(raw_seq)
        except (TypeError, ValueError):
            raise RuntimeError("'ledger_seq' must be a ledger sequence number, "
                               "got %r. Run zoho.verify_ledger or "
                               "zoho.audit_pack to find it." % raw_seq)
    record_outcome = bool(inputs.get("record_outcome"))

    book = _ledger_load()
    open_entries = _recon_open(book, only_seq)

    if only_seq is not None and not open_entries:
        raise RuntimeError(
            "Ledger entry %d is not an unresolved write awaiting reconciliation. "
            "It may not exist, may be an applied or refused entry, or may "
            "already have been resolved." % only_seq)

    counts = {"landed": 0, "not_landed": 0, "unknown": 0}
    reports, still_open, recorded_seqs = [], [], []
    records_checked, already_committed, legacy = 0, 0, 0

    for entry in open_entries:
        detail = entry.get("detail") or {}
        basis = detail.get("verdict_basis")
        command = entry.get("command")
        module = entry.get("module")
        entry_intent = detail.get("intent")
        targets = [t for t in (detail.get("targets") or []) if (t or {}).get("id")]

        # Records the entry already recorded as confirmed written are settled.
        # Re-adjudicating them would fold certainty back into the counts as if
        # it were inference.
        settled = set()
        for row in (detail.get("written_before_failure") or []):
            if isinstance(row, dict) and row.get("id"):
                settled.add(str(row["id"]))

        pending = [t for t in targets if str(t["id"]) not in settled]
        already_committed += len(targets) - len(pending)

        columns = _recon_columns(entry)
        by_id, read_error = _recon_read(
            module, [str(t["id"]) for t in pending], columns)

        verdicts = []
        for target in pending:
            rid = str(target["id"])
            if read_error is not None:
                verdict, note = "unknown", ("could not re-read this record: %s"
                                            % read_error)
            elif target.get("before_modified_time") in (None, ""):
                # Pre-feature entries. Counted and reported on their own rather
                # than judged by value alone, which is a materially weaker test
                # and would make the headline counts mean two different things.
                verdict, note = "unreconcilable", (
                    "entry predates before_modified_time; there is no baseline "
                    "to compare against")
            else:
                verdict, note = _recon_one(basis, target, by_id.get(rid),
                                           entry_intent)
            verdicts.append({"id": rid, "verdict": verdict, "note": note,
                             "modified_time": (by_id.get(rid) or {}).get("Modified_Time"),
                             "modified_by": _recon_flat(
                                 (by_id.get(rid) or {}).get("Modified_By")),
                             "role": target.get("role")})
            if verdict in counts:
                counts[verdict] += 1
                records_checked += 1
            else:
                legacy += 1

        merge_state = None
        if command == "apply_merge":
            # A merge can only be checked for existence, and even a clean
            # result is not proof: the losers being gone says a merge happened,
            # not that it merged the fields correctly, and Zoho's own bin entry
            # cannot be read back. Every verdict stays unknown.
            master = str(detail.get("master_id") or "")
            present_master = master in by_id
            losers = [v for v in verdicts if v.get("role") == "loser"]
            losers_gone = bool(losers) and all(v["id"] not in by_id for v in losers)
            if read_error is not None or not present_master:
                merge_state = "indeterminate"
            elif losers_gone:
                merge_state = "losers_absent"
            else:
                merge_state = "masters_present"
            for verdict in verdicts:
                if verdict["verdict"] in counts:
                    counts[verdict["verdict"]] -= 1
                    counts["unknown"] += 1
                    verdict["verdict"] = "unknown"
                    verdict["note"] = (
                        "merge is checked for existence only; %s is consistent "
                        "with a completed merge but does not prove one"
                        % merge_state)

        resolved = bool(verdicts) and any(
            v["verdict"] in ("landed", "not_landed") for v in verdicts)
        if not resolved:
            still_open.append(entry.get("seq"))

        report = {
            "ledger_seq": entry.get("seq"),
            "command": command,
            "module": module,
            "verdict_basis": basis,
            "attempted_at": detail.get("attempted_at"),
            "reason": detail.get("reason"),
            "intent": entry_intent,
            "already_committed": len(targets) - len(pending),
            "creates_unreconcilable": detail.get("creates_unreconcilable"),
            "merge_state": merge_state,
            "read_error": read_error,
            "resolved": resolved,
            "records": verdicts,
        }
        reports.append(report)

        if record_outcome:
            # Opt-in on purpose. A read command that silently grows a
            # hash-chained ledger on every run is a surprise, and the ledger
            # rotates at 5000 entries. But a reconciliation nobody recorded is
            # just a look, so when an operator wants it to be evidence one flag
            # makes it evidence.
            note = _ledger_append(_ledger_note(
                "reconciled", "reconcile_writes", module, entry.get("plan_key"),
                {"resolves_seq": entry.get("seq"),
                 "resolved": resolved,
                 "verdict_basis": basis,
                 "merge_state": merge_state,
                 "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "verdicts": [{"id": v["id"], "verdict": v["verdict"]}
                              for v in verdicts]}))
            recorded_seqs.append(note["seq"])

    name = "reconcile_writes" + time.strftime(".%Y%m%dT%H%M%SZ",
                                              time.gmtime()) + ".json"
    path = str(helpers.get("WS") or "").rstrip("/") + "/" + name
    # Counts inline, records in the file. redact_output scrubs ids and dates out
    # of a receipt, so anything an operator has to act on has to be a path -
    # the same reason audit_pack and hygiene_scan write files.
    helpers["jsave"](path, {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "module_version": _module_version(),
        "recorded_to_ledger": recorded_seqs or None,
        "limits": [
            "Verdicts are inferred from current state. Zoho cannot be asked "
            "whether a request landed.",
            "'landed' means the value matches the intent and the record moved. "
            "Another actor making the same change is indistinguishable.",
            "A record edited again after the attempt cannot be adjudicated.",
            "An apply_upsert entry can never be 'landed': the values it was "
            "writing over were never read.",
            "A merge is checked for existence only.",
        ],
        "entries": reports,
    })

    checked = len(open_entries)
    if not checked:
        summary = ("No unresolved writes awaiting reconciliation. Nothing this "
                   "module attempted is currently in doubt.")
    else:
        summary = (
            "Checked %d unresolved %s covering %d record%s: %d consistent with "
            "having landed, %d that nothing has touched since, %d that cannot "
            "be told either way. Records are in %s. These are inferences from "
            "current state, not confirmations - Zoho cannot be asked whether a "
            "request landed."
            % (checked, "entry" if checked == 1 else "entries", records_checked,
               "" if records_checked == 1 else "s", counts["landed"],
               counts["not_landed"], counts["unknown"], name))
        if legacy:
            summary += (" %d record%s came from entries written before this "
                        "feature and have no baseline to compare against; they "
                        "are excluded from the counts above."
                        % (legacy, "" if legacy == 1 else "s"))
        if already_committed:
            summary += (" %d record%s already recorded as written before "
                        "contact was lost and %s not re-examined."
                        % (already_committed,
                           "" if already_committed == 1 else "s",
                           "was" if already_committed == 1 else "were"))
        if still_open:
            summary += (" %d entr%s nothing could be determined for and "
                        "remain%s open."
                        % (len(still_open), "y" if len(still_open) == 1 else "ies",
                           "s" if len(still_open) == 1 else ""))
        if not record_outcome:
            summary += (" Nothing was recorded; re-run with record_outcome "
                        "true to write these verdicts to the ledger.")

    return {
        "ok": True,
        "entries_checked": checked,
        "records_checked": records_checked,
        "landed": counts["landed"],
        "not_landed": counts["not_landed"],
        "unknown": counts["unknown"],
        "unreconcilable": legacy,
        "already_committed": already_committed,
        "report_path": path,
        "still_open": still_open,
        "recorded": bool(recorded_seqs),
        "covers": "verdicts are inferred from current state and Modified_Time; "
                  "Zoho has no way to confirm whether a request landed. An "
                  "apply_upsert entry can never read as landed, and a merge is "
                  "checked for existence only",
        "summary": summary,
        "origin": _origin(stamp),
    }, None




# --- custody ----------------------------------------------------------------
#
# The manifest says the audience is people who get asked "who changed this
# client record, and who approved it". The module has held three separate
# bodies of evidence for that question - the ledger (what it did), scan_changes
# (what happened outside it), and refusals (what it stopped) - and never joined
# them. Answering took reading three outputs and joining them by hand.
#
# This is the join. It adds no new read of Zoho beyond one Modified_Time
# lookup, because everything else it needs is already on disk.

_CUSTODY_CAP = 500        # records per report; a custody answer is read by a
                          # human, and a thousand-row one is not read at all


def _custody_ids(inputs, module):
    """Ids from `record_ids`, or from a COQL query, but not from neither."""
    raw = inputs.get("record_ids")
    query = str(inputs.get("query", "")).strip()
    if raw and query:
        raise RuntimeError("Give 'record_ids' or 'query', not both.")

    if raw:
        if not isinstance(raw, list):
            raise RuntimeError("'record_ids' must be a list of record ids.")
        ids = [str(r).strip() for r in raw if str(r).strip()]
        bad = [r for r in ids if not r.isdigit()]
        if bad:
            raise RuntimeError("Zoho record ids are numeric; these are not: %r"
                               % bad[:5])
    elif query:
        if not query.lower().lstrip("( ").startswith("select"):
            raise RuntimeError("'query' must be a COQL SELECT.")
        rows, hit_cap = _coql_capped(_strip_limit(query), cap=_CUSTODY_CAP,
                                     label="custody query")
        if hit_cap:
            raise RuntimeError(
                "That query matches more than %d records. A custody report is "
                "read by a person; narrow it rather than producing a file "
                "nobody will open." % _CUSTODY_CAP)
        ids = [str(r.get("id")) for r in rows if r.get("id")]
    else:
        raise RuntimeError("Give either 'record_ids' or 'query' to say which "
                           "records to report on.")

    if not ids:
        raise RuntimeError("No records to report on.")
    if len(ids) > _CUSTODY_CAP:
        raise RuntimeError("%d records is over the %d-record cap for one "
                           "report." % (len(ids), _CUSTODY_CAP))
    return ids


def _custody_timeline(slot):
    """Every source for one record, merged and ordered.

    A reconciled verdict is attached to the unresolved attempt it adjudicated
    rather than floating on its own, because on its own it reads like a second
    event and there was only ever one.
    """
    events = []
    for row in slot["applied"]:
        events.append({"at": row["at"], "kind": "governed_change",
                       "command": row["command"], "ledger_seq": row["seq"],
                       "plan_key": row["plan_key"], "fields": row["fields"],
                       "written_modified_time": row["written_modified_time"]})
    for row in slot["refused"]:
        events.append({"at": row["at"], "kind": "refusal",
                       "command": row["command"], "ledger_seq": row["seq"],
                       "plan_key": row["plan_key"], "fields": row["fields"],
                       "reason": row["reason"]})
    verdicts = {}
    for row in slot["reconciled"]:
        verdicts[row["resolves_seq"]] = row
    for row in slot["unresolved"]:
        found = verdicts.get(row["seq"])
        events.append({"at": row["at"], "kind": "unresolved_write",
                       "command": row["command"], "ledger_seq": row["seq"],
                       "plan_key": row["plan_key"], "fields": row["fields"],
                       "reason": row["reason"],
                       "verdict": (found or {}).get("verdict"),
                       "verdict_recorded_as": (found or {}).get("seq"),
                       "merge_state": (found or {}).get("merge_state")})
    events.sort(key=lambda e: (e["at"] or "", e["ledger_seq"] or 0))
    return events


def _custody_verdict(slot, current, governed, covers_from):
    """One record's custody verdict, and why.

    Returns (verdict, diverged_from, note). The three verdicts answer three
    different questions and `unproven` is the important one: it is the module
    declining to claim coverage it does not have, which is the only reason the
    other two are worth anything.
    """
    applied = slot["applied"]
    last_written = None
    for row in applied:
        stamp = row.get("written_modified_time")
        if stamp and (last_written is None or stamp > last_written):
            last_written = stamp

    if current is None:
        # Deleted or merged since. scan_changes has the same blind spot for the
        # same reason: a record that no longer reads back cannot be polled, and
        # its history cannot be reconstructed from an API that exposes none.
        return "unproven", last_written, ("record no longer readable; deleted "
                                          "or merged, and Zoho exposes no "
                                          "history to reconstruct it from")

    now = _iso_utc(current.get("Modified_Time"))
    if now is None:
        return "unproven", last_written, "record has no readable Modified_Time"

    if "%s:%s:%s" % (current["_module"], current["_id"], now) in governed:
        return "governed", last_written, ("current state was produced by a "
                                          "governed write")

    if applied and last_written is None:
        # Every approval this record has is one the ledger cannot match: merges,
        # which Zoho does not timestamp, and anything written before the ledger
        # recorded Modified_Time. Calling that diverged would report a real
        # approval as an ungoverned edit.
        return "unproven", None, ("this record's ledger entries predate the "
                                  "recorded post-write timestamp, so they "
                                  "cannot be matched to its current state")

    if last_written is not None:
        # The ledger holds a matchable governed write for THIS record, so
        # coverage is not in question whatever covers_from says - a record the
        # chain demonstrably knows about cannot also be outside its reach.
        # Checked before covers_from, because the reverse order calls a genuine
        # divergence unproven whenever the divergence is older than the oldest
        # surviving entry, which is exactly when it matters most.
        if now > last_written:
            return "diverged", last_written, ("current state was not produced "
                                              "by a governed write")
        return "unproven", last_written, (
            "current state is older than the last governed write recorded for "
            "it, which should not happen; treat the ledger and the record as "
            "disagreeing rather than trusting either")

    if covers_from is None or now < covers_from:
        return "unproven", None, ("no ledger entry for this record, and the "
                                  "change predates the ledger's coverage; the "
                                  "chain rotates and sealed archives are not "
                                  "read here")

    return "diverged", None, ("no ledger entry for this record within the "
                              "ledger's coverage, so its current state was "
                              "not produced by a governed write")


def zoho_custody_report(inputs, stamp):
    """For these records: what changed, who approved it, what was refused, and
    what the module cannot account for. Read-only.

    The ledger records what this module did. scan_changes finds what happened
    outside it, on a schedule. This answers the question those two exist for,
    for a named set of records, on demand.

    THE LIMIT THAT MATTERS, and it is in the output as well as here: this
    detects THAT a record's current state was not produced by a governed write.
    It cannot enumerate every ungoverned edit that ever happened. Zoho exposes
    no per-field history and Modified_Time holds only the most recent change,
    so the honest phrasing is "at least one change to this record was not
    governed, most recently at T by U" - never "three ungoverned changes".

    The other limits carry over from scan_changes and are reported too:
      - Ledger coverage starts at the earliest entry in the live chain, because
        the chain rotates and sealed archives are not read here.
      - Entries predating the recorded post-write Modified_Time cannot be
        matched to a record's current state.
      - A record deleted or merged in the UI leaves nothing to poll.
      - Modified_By names the OAuth user for this module's own writes, so it
        identifies a person only for changes the module did not make.
      - Refusals recorded before targets were added carry no ids and cannot be
        attributed to a record. The count is reported rather than their absence
        being read as "nothing was ever refused".

    inputs: module (string, required), record_ids (array) or query (COQL),
            include_ungoverned (boolean, default true)
    """
    helpers = __rc_helpers__  # noqa: F821
    module = _module_name(inputs)
    ids = _custody_ids(inputs, module)
    raw_flag = inputs.get("include_ungoverned")
    include_ungoverned = True if raw_flag in (None, "") else bool(raw_flag)

    index = _ledger_index(with_records=True)
    by_record = index["by_record"]
    governed_keys = index["governed"]
    covers_from = index["covers_from"]

    current_by_id = {}
    if include_ungoverned:
        # The one extra read. Everything else this command needs is on disk.
        for start in range(0, len(ids), 100):
            for row in _read_by_ids(module, ids[start:start + 100],
                                    ["Modified_Time", "Modified_By"]):
                if row.get("id"):
                    rid = str(row["id"])
                    row["_module"], row["_id"] = module, rid
                    current_by_id[rid] = row

    empty = {"applied": [], "refused": [], "unresolved": [], "reconciled": []}
    verdicts = {"governed": 0, "diverged": 0, "unproven": 0, "unchecked": 0}
    totals = {"governed_changes": 0, "refusals": 0, "unresolved": 0}
    records = []

    for rid in ids:
        slot = by_record.get("%s:%s" % (module, rid), empty)
        totals["governed_changes"] += len(slot["applied"])
        totals["refusals"] += len(slot["refused"])
        totals["unresolved"] += len(slot["unresolved"])

        if not include_ungoverned:
            # Without the current Modified_Time there is nothing to compare a
            # governed write against, so no custody verdict is possible. Saying
            # "governed" off the ledger alone would be exactly the claim this
            # command exists to stop anyone making.
            verdict, diverged_from, note = "unchecked", None, (
                "include_ungoverned was false, so the record's current state "
                "was not read and custody cannot be determined")
            current = None
        else:
            current = current_by_id.get(rid)
            verdict, diverged_from, note = _custody_verdict(
                slot, current, governed_keys, covers_from)

        verdicts[verdict] += 1
        by = (current or {}).get("Modified_By") or {}
        records.append({
            "id": rid,
            "custody": verdict,
            "why": note,
            "current_modified_time": (current or {}).get("Modified_Time"),
            "current_modified_by": by.get("name") if isinstance(by, dict) else by,
            "diverged_from": diverged_from if verdict == "diverged" else None,
            "governed_changes": len(slot["applied"]),
            "refusals": len(slot["refused"]),
            "unresolved": len(slot["unresolved"]),
            "ungoverned": verdict == "diverged",
            "timeline": _custody_timeline(slot),
        })

    limits = [
        "Detects THAT a record's current state was not produced by a governed "
        "write. It cannot enumerate every ungoverned edit: Zoho exposes no "
        "per-field history and Modified_Time holds only the most recent "
        "change. Read a 'diverged' verdict as 'at least one change here was "
        "not governed, most recently at the time shown'.",
        "Ledger coverage starts at %s, the earliest entry in the live chain. "
        "The chain rotates and sealed archives are not read here."
        % (covers_from or "never - the ledger is empty"),
        "%d applied entries have no recorded post-write Modified_Time and "
        "cannot be matched to a record's current state." % index["unmatchable"],
        "%d refusals were recorded before refusals carried record ids and "
        "cannot be attributed to any record."
        % index.get("unattributed_refusals", 0),
        "A record deleted or merged in the Zoho UI leaves nothing to poll.",
        "Modified_By names the OAuth user for this module's own writes, so it "
        "identifies a person only for changes the module did not make.",
    ]

    name = "custody_report" + time.strftime(".%Y%m%dT%H%M%SZ",
                                            time.gmtime()) + ".json"
    path = str(helpers.get("WS") or "").rstrip("/") + "/" + name
    # Ids and timelines to the file, counts inline. redact_output strips
    # identifiers out of a receipt, so a per-record verdict returned inline
    # would come back as "[account]: diverged" and answer nobody's question.
    helpers["jsave"](path, {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "module_version": _module_version(),
        "module": module,
        "ledger_covers_from": covers_from,
        "limits": limits,
        "records": records,
    })

    summary = (
        "%d %s record%s: %d governed, %d diverged, %d unproven%s. %d governed "
        "changes, %d refusals and %d unresolved writes in their histories. Per "
        "record detail is in %s. 'diverged' means at least one change was not "
        "governed, most recently at the time shown - Zoho exposes no per-field "
        "history, so it cannot be counted. 'unproven' is not a failure: it is "
        "the ledger declining to claim coverage it does not have."
        % (len(ids), module, "" if len(ids) == 1 else "s",
           verdicts["governed"], verdicts["diverged"], verdicts["unproven"],
           (" and %d unchecked" % verdicts["unchecked"])
           if verdicts["unchecked"] else "",
           totals["governed_changes"], totals["refusals"],
           totals["unresolved"], name))

    return {
        "ok": True,
        "module": module,
        "records": len(ids),
        "governed": verdicts["governed"],
        "diverged": verdicts["diverged"],
        "unproven": verdicts["unproven"],
        "unchecked": verdicts["unchecked"],
        "governed_changes": totals["governed_changes"],
        "refusals": totals["refusals"],
        "unresolved": totals["unresolved"],
        "ledger_covers_from": covers_from,
        "unmatchable_ledger_entries": index["unmatchable"],
        "unattributed_refusals": index.get("unattributed_refusals", 0),
        "report_path": path,
        "covers": "detects that a record's current state was not produced by a "
                  "governed write; it cannot enumerate every ungoverned edit, "
                  "because Zoho exposes no per-field history",
        "summary": summary,
        "origin": _origin(stamp),
    }, None


# --- rollback ---------------------------------------------------------------
#
# The listing has always said the plan snapshot doubles as rollback data. It
# did, and nothing consumed it. This does.
#
# No rollback token is issued, deliberately. A token would be an identifier and
# redact_output would strip it out of the receipt, which is the same trap the
# plan store was built to avoid. Instead a rollback is addressed by the same
# module / query / changes the operator already typed to apply it - they know
# those three, and the ledger is keyed on them.

def _last_applied(key):
    book = _ledger_load()
    for entry in reversed(book["entries"]):
        if entry.get("plan_key") == key and entry.get("outcome") == "applied":
            return entry
    return None


def _rollback_key(module, query, changes):
    return _plan_key(module, query, changes)


def zoho_plan_rollback(inputs, stamp):
    """Work out what undoing the last apply of this change would restore.

    Read-only. Takes the same module, query and changes that were applied.
    Reads the records back as they are now and reports, per record, what it
    would put back and whether that record has moved again since the apply.

    Fields written by the original apply are restored. Modified_Time is read
    for drift only and never written.
    """
    module = _module_name(inputs)
    query = str(inputs.get("query", "")).strip()
    changes = inputs.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise RuntimeError("'changes' must be the same changes object that was "
                           "applied.")

    entry = _last_applied(_rollback_key(module, query, changes))
    if not entry:
        raise RuntimeError(
            "No applied change in the ledger for this module, query and "
            "changes. Rollback addresses an apply by the same three inputs "
            "that performed it. Run zoho.audit_pack to see what is on record.")

    before = entry.get("detail", {}).get("before") or []
    if not before:
        raise RuntimeError(
            "The ledger entry for that apply carries no prior values, so there "
            "is nothing to restore. Entries written before 0.6.0 do not hold "
            "them.")

    fields = sorted(str(k) for k in changes)
    ids = [str(r.get("id")) for r in before if r.get("id")]
    guard = sorted(set(fields) | {"Modified_Time"})
    current = _read_by_ids(module, ids, guard)
    by_id = {str(r.get("id")): r for r in current}

    restores, missing, moved_again = [], [], []
    for row in before:
        rid = str(row.get("id"))
        now = by_id.get(rid)
        if now is None:
            missing.append(rid)
            continue
        # What the apply wrote is what should be there now. If it isn't, the
        # record has been changed again by someone else and a blind restore
        # would silently discard their edit.
        touched = [f for f in fields if now.get(f) != changes[f]]
        if touched:
            moved_again.append({"id": rid, "fields": touched})
        restores.append({"id": rid,
                         "restore_to": {f: row.get("before", {}).get(f)
                                        for f in fields}})

    fingerprint, _snapshot = _fingerprint(module, current, guard)
    _plan_save(_plan_key(module, query, {"__rollback__": changes}),
               {"fingerprint": fingerprint, "count": len(restores),
                "records": restores})

    return {
        "ok": True,
        "module": module,
        "applied_at": entry.get("at"),
        "records": restores,
        "count": len(restores),
        "missing": missing,
        "changed_again": moved_again,
        "fields": fields,
        "expires_in_minutes": _PLAN_TTL // 60,
        "summary": ("Would restore %d records on %s to their values before the "
                    "apply of %s. %d have been changed again since and %d no "
                    "longer exist; apply_rollback refuses while any record has "
                    "moved. Run zoho.apply_rollback with the same three inputs."
                    % (len(restores), ", ".join(fields), entry.get("at"),
                       len(moved_again), len(missing))),
        "origin": _origin(stamp),
    }, None


def zoho_apply_rollback(inputs, stamp):
    """Restore the prior values, if nothing has moved since the plan.

    Same contract as apply_update: re-read, re-hash, refuse on drift. A
    rollback is a write like any other and gets no special dispensation -
    undoing onto records somebody has since edited is the exact failure this
    module exists to stop.
    """
    module = _module_name(inputs)
    query = str(inputs.get("query", "")).strip()
    changes = inputs.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise RuntimeError("'changes' must match the rollback plan's changes.")

    key = _plan_key(module, query, {"__rollback__": changes})
    stored = _plan_load(key)
    if not stored:
        raise RuntimeError(
            "No current rollback plan for these inputs. Run "
            "zoho.plan_rollback first, review what it reports, then apply with "
            "exactly the same three inputs. Plans expire after %d minutes."
            % (_PLAN_TTL // 60))

    fields = sorted(str(k) for k in changes)
    guard = sorted(set(fields) | {"Modified_Time"})
    ids = [str(r["id"]) for r in stored.get("records") or [] if r.get("id")]
    if not ids:
        raise RuntimeError("The rollback plan restores no records.")
    if len(ids) > 100:
        raise RuntimeError("The rollback covers %d records, over Zoho's 100 "
                           "per write." % len(ids))

    current = _read_by_ids(module, ids, guard)
    actual, snapshot = _fingerprint(module, current, guard)
    expected = str(stored.get("fingerprint") or "")

    if actual != expected:
        _ledger_append(_ledger_note(
            "refused", "apply_rollback", module, key,
            {"reason": "state moved between plan and apply",
             "expected": expected, "actual": actual,
             "records": len(ids),
             "targets": [str(i) for i in ids]}))
        raise RuntimeError(
            "Refusing to roll back. The records moved since the rollback plan "
            "was made: the state fingerprint is %s, not %s. Re-run "
            "zoho.plan_rollback and review the new plan."
            % (actual[:23] + "...", expected[:23] + "..."))

    payload = []
    for row in stored["records"]:
        record = {"id": row["id"]}
        record.update(row.get("restore_to") or {})
        payload.append(record)

    before_by_id = {r["id"]: r["before"] for r in snapshot}
    try:
        result = _summarise(_call("PUT", module, body={"data": payload}),
                            "roll back " + module, stamp)
    except ZohoUnresolvedWrite as exc:
        # Every record is restored to its own prior values, so there is no one
        # intent for the entry - each target carries its own.
        raise _ledger_unresolved(
            exc, "apply_rollback", module, key,
            {"intent": None, "fields": fields,
             "targets": [{"id": row["id"],
                          "intent": row.get("restore_to") or {},
                          "before": {f: before_by_id.get(row["id"], {}).get(f)
                                     for f in fields},
                          "before_modified_time":
                              before_by_id.get(row["id"], {}).get("Modified_Time")}
                         for row in stored["records"] if row.get("id")]})
    result["fingerprint_verified"] = expected
    result["records_restored"] = len(payload)

    # Record what was actually there before this restore, not placeholders, so
    # a rollback is itself reversible. `current` was read moments ago and the
    # fingerprint proves it is still accurate.
    now_by_id = {str(r.get("id")): r for r in current}
    entry = _ledger_append(_ledger_note(
        "applied", "apply_rollback", module, key,
        {"records": len(payload), "fields": fields,
         "fingerprint": expected,
         "written": result.get("written"),
         "before": [{"id": r["id"],
                     "before": {f: (now_by_id.get(r["id"], {}) or {}).get(f)
                                for f in fields}} for r in payload]}))
    result["ledger_seq"] = entry["seq"]
    return result, None
