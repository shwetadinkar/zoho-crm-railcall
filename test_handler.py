#!/usr/bin/env python3
"""Mocked tests for shweta/zoho-crm.

Runs the handler against a fake __rc_helpers__ so every code path — including
the retry policy, which is hard to trigger against a live org — is exercised
without touching Zoho.

    python3 test_handler.py
"""

import importlib.util
import json
import os
import sys

HANDLER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "handlers", "handler.py")

PASS, FAIL = [], []


def load(helpers):
    """Load handler.py with an injected __rc_helpers__, as the station does."""
    spec = importlib.util.spec_from_file_location("zh", HANDLER)
    mod = importlib.util.module_from_spec(spec)
    mod.__dict__["__rc_helpers__"] = helpers
    spec.loader.exec_module(mod)
    mod.__dict__["__rc_helpers__"] = helpers
    return mod


class FakeHTTPError(Exception):
    """Mimics how the platform helpers surface an HTTP error status."""
    def __init__(self, code, body):
        super().__init__("HTTP %d: %s" % (code, body))
        self.code = code
        self._body = body.encode()

    def read(self):
        return self._body


def helpers(responses, calls=None, raise_on=None, raise_http=None):
    """Fake helper dict. `responses` is a list of (status, dict) served in order.

    raise_http: (code, body_text) raised on every call, as the real helpers do.
    """
    calls = calls if calls is not None else []
    seq = list(responses)

    def _next(method, url):
        calls.append((method, url))
        if raise_http:
            raise FakeHTTPError(*raise_http)
        if raise_on and method in raise_on:
            raise OSError("simulated network failure")
        status, body = seq.pop(0) if seq else (200, {})
        return status, json.dumps(body).encode()

    return {
        "oauth_refresh": lambda p, **k: {
            "access_token": "tok", "instance_url": "https://www.zohoapis.in"},
        "http_get_json": lambda url, **k: _next("GET", url),
        "http_post_json": lambda url, obj, **k: _next("POST", url),
        "http_delete_json": lambda url, **k: _next("DELETE", url),
        "vault_get": lambda p: {},
    }, calls


def check(name, fn):
    try:
        fn()
        PASS.append(name)
    except AssertionError as e:
        FAIL.append("%s — %s" % (name, e))
    except Exception as e:
        FAIL.append("%s — unexpected %s: %s" % (name, type(e).__name__, e))


# ---------------------------------------------------------------- validation
def t_module_required():
    h, _ = helpers([])
    m = load(h)
    try:
        m.zoho_describe_module({}, {})
        assert False, "should have raised"
    except RuntimeError as e:
        assert "module" in str(e), e


def t_coql_rejects_writes():
    h, _ = helpers([])
    m = load(h)
    for bad in ("DELETE from Leads", "update Leads set x=1", "  insert into X"):
        try:
            m.zoho_search_records({"query": bad}, {})
            assert False, "accepted %r" % bad
        except RuntimeError as e:
            assert "SELECT" in str(e), e


def t_coql_allows_select():
    h, _ = helpers([(200, {"data": [{"id": "1"}], "info": {}})])
    m = load(h)
    out, err = m.zoho_search_records({"query": "select Email from Leads"}, {})
    assert err is None and out["count"] == 1, out


def t_bad_record_id():
    h, _ = helpers([])
    m = load(h)
    try:
        m.zoho_get_record({"module": "Leads", "record_id": "abc"}, {})
        assert False
    except RuntimeError as e:
        assert "numeric" in str(e), e


def t_batch_cap():
    h, _ = helpers([])
    m = load(h)
    try:
        m.zoho_create_record({"module": "Leads",
                              "records": [{"x": 1}] * 101}, {})
        assert False
    except RuntimeError as e:
        assert "100" in str(e), e


def t_update_needs_id():
    h, _ = helpers([])
    m = load(h)
    try:
        m.zoho_update_record({"module": "Leads", "records": [{"Email": "a@b.c"}]}, {})
        assert False
    except RuntimeError as e:
        assert "id" in str(e), e


def t_bad_user_type():
    h, _ = helpers([])
    m = load(h)
    try:
        m.zoho_list_users({"type": "Nope"}, {})
        assert False
    except RuntimeError as e:
        assert "must be one of" in str(e), e


# ------------------------------------------------------------- retry policy
def t_read_retries_5xx():
    h, calls = helpers([(500, {}), (500, {}), (200, {"fields": []})])
    m = load(h)
    out, _ = m.zoho_describe_module({"module": "Leads"}, {})
    assert len(calls) == 3, "expected 3 attempts, got %d" % len(calls)
    assert out["field_count"] == 0


def t_write_does_not_retry_5xx():
    h, calls = helpers([(500, {}), (200, {"data": []})])
    m = load(h)
    try:
        m.zoho_create_record({"module": "Leads", "records": [{"Last_Name": "X"}]}, {})
        assert False, "write was retried or succeeded"
    except RuntimeError as e:
        assert "not retried" in str(e).lower(), e
    assert len(calls) == 1, "write retried %d times" % len(calls)


def t_write_retries_429():
    h, calls = helpers([(429, {}), (200, {"data": [{"code": "SUCCESS",
                                                   "details": {"id": "9"}}]})])
    m = load(h)
    out, _ = m.zoho_create_record({"module": "Leads",
                                   "records": [{"Last_Name": "X"}]}, {})
    assert len(calls) == 2, "expected retry on 429, got %d calls" % len(calls)
    assert out["succeeded"] == 1


def t_write_no_retry_on_network_error():
    h, calls = helpers([], raise_on={"POST"})
    m = load(h)
    try:
        m.zoho_create_record({"module": "Leads", "records": [{"Last_Name": "X"}]}, {})
        assert False
    except RuntimeError as e:
        assert "not retried" in str(e).lower(), e
    assert len(calls) == 1


def t_read_retries_network_error():
    h, calls = helpers([], raise_on={"GET"})
    m = load(h)
    try:
        m.zoho_describe_module({"module": "Leads"}, {})
        assert False
    except RuntimeError as e:
        assert "after 5 attempts" in str(e), e
    assert len(calls) == 5, "expected 5 attempts, got %d" % len(calls)


# ------------------------------------------------------- partial success etc
def t_partial_success_reported():
    h, _ = helpers([(200, {"data": [
        {"code": "SUCCESS", "details": {"id": "1"}},
        {"code": "INVALID_DATA", "message": "bad email",
         "details": {"api_name": "Email"}}]})])
    m = load(h)
    out, _ = m.zoho_create_record({"module": "Leads",
                                   "records": [{"a": 1}, {"b": 2}]}, {})
    assert out["succeeded"] == 1 and out["failed"] == 1, out
    assert out["errors"][0]["field"] == "Email", out


def t_all_failed_raises():
    h, _ = helpers([(200, {"data": [
        {"code": "INVALID_DATA", "message": "nope", "details": {}}]})])
    m = load(h)
    try:
        m.zoho_create_record({"module": "Leads", "records": [{"a": 1}]}, {})
        assert False
    except RuntimeError as e:
        assert "rejected every record" in str(e), e


def t_origin_stamped_on_writes():
    h, _ = helpers([(200, {"data": [{"code": "SUCCESS", "details": {"id": "1"}}]})])
    m = load(h)
    out, _ = m.zoho_create_record(
        {"module": "Leads", "records": [{"a": 1}]},
        {"actor": "agent:claude", "run_id": "r-42"})
    assert out["origin"]["actor"] == "agent:claude", out["origin"]
    assert out["origin"]["run_id"] == "r-42", out["origin"]


def t_origin_survives_empty_stamp():
    h, _ = helpers([(200, {"data": [{"code": "SUCCESS", "details": {"id": "1"}}]})])
    m = load(h)
    out, _ = m.zoho_create_record({"module": "Leads", "records": [{"a": 1}]}, None)
    assert out["origin"]["initiated_via"] == "railcall-airlock", out


def t_all_returns_tuple():
    """Station contract: every handler returns (dict, None)."""
    h, _ = helpers([(200, {"fields": []})])
    m = load(h)
    out = m.zoho_describe_module({"module": "Leads"}, {})
    assert isinstance(out, tuple) and len(out) == 2, out
    assert isinstance(out[0], dict) and out[1] is None, out


def t_put_used_for_update():
    """update_record must issue PUT, not POST."""
    seen = []
    h, _ = helpers([])
    m = load(h)
    m._put = lambda url, obj, hdrs: (seen.append(url) or
                                     (200, json.dumps({"data": [
                                         {"code": "SUCCESS",
                                          "details": {"id": "1"}}]}).encode()))
    out, _ = m.zoho_update_record({"module": "Leads",
                                   "records": [{"id": "5", "Email": "a@b.c"}]}, {})
    assert seen and "/crm/v8/Leads" in seen[0], seen
    assert out["succeeded"] == 1


def t_no_env_reads():
    """Vault-only: the handler must never touch process env for credentials.

    Checked via AST rather than substring, so prose in the docstring that
    merely mentions os.environ doesn't produce a false positive.
    """
    import ast
    tree = ast.parse(open(HANDLER).read())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "os" not in imported, "handler imports os"

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
            raise AssertionError("handler accesses .%s in code" % node.attr)
        if isinstance(node, ast.Name) and node.id in ("environ", "getenv"):
            raise AssertionError("handler references %s in code" % node.id)


def t_4xx_not_retried():
    """A 401 is deterministic — it must fail on the first attempt, not the fifth."""
    h, calls = helpers([], raise_http=(401, '{"code":"OAUTH_SCOPE_MISMATCH"}'))
    m = load(h)
    try:
        m.zoho_describe_module({"module": "Leads"}, {})
        assert False, "should have raised"
    except RuntimeError as e:
        assert "401" in str(e), e
        assert "after 5 attempts" not in str(e), "retried a 4xx: %s" % e
    assert len(calls) == 1, "4xx retried %d times" % len(calls)


def t_scope_mismatch_hint():
    h, _ = helpers([], raise_http=(401, '{"code":"OAUTH_SCOPE_MISMATCH"}'))
    m = load(h)
    try:
        m.zoho_describe_module({"module": "Leads"}, {})
        assert False
    except RuntimeError as e:
        assert "lacks the scope" in str(e), e


def t_404_not_retried():
    h, calls = helpers([], raise_http=(404, '{"code":"INVALID_URL_PATTERN"}'))
    m = load(h)
    try:
        m.zoho_get_record({"module": "Leads", "record_id": "123"}, {})
        assert False
    except RuntimeError as e:
        assert "404" in str(e), e
    assert len(calls) == 1


def t_verify_without_org_scope():
    """verify_connection must succeed when only the required scopes are present."""
    calls = []
    seq = [(200, {"fields": [{"api_name": "Email"}, {"api_name": "Last_Name"}]})]

    def _get(url, **k):
        calls.append(url)
        if "settings/fields" in url:
            return 200, json.dumps(seq[0][1]).encode()
        raise FakeHTTPError(401, '{"code":"OAUTH_SCOPE_MISMATCH"}')

    h = {"oauth_refresh": lambda p, **k: {"access_token": "t",
                                          "instance_url": "https://www.zohoapis.in"},
         "http_get_json": _get,
         "http_post_json": lambda u, o, **k: (200, b"{}"),
         "http_delete_json": lambda u, **k: (200, b"{}")}
    m = load(h)
    out, err = m.zoho_verify_connection({}, {})
    assert err is None and out["authenticated"] is True, out
    assert out["leads_field_count"] == 2, out
    assert "ZohoCRM.org.READ" in out["note"], out


def t_verify_with_org_scope():
    calls = []

    def _get(url, **k):
        calls.append(url)
        if "settings/fields" in url:
            return 200, json.dumps({"fields": [{"api_name": "Email"}]}).encode()
        return 200, json.dumps({"org": [{"company_name": "Acme",
                                         "id": "77", "country": "India"}]}).encode()

    h = {"oauth_refresh": lambda p, **k: {"access_token": "t",
                                          "instance_url": "https://www.zohoapis.in"},
         "http_get_json": _get,
         "http_post_json": lambda u, o, **k: (200, b"{}"),
         "http_delete_json": lambda u, **k: (200, b"{}")}
    m = load(h)
    out, _ = m.zoho_verify_connection({}, {})
    assert out["org_name"] == "Acme" and out["note"] == "", out


def t_convert_lead_parses_details():
    """Zoho nests created records under details.<Module>.id — verified live."""
    h, _ = helpers([(200, {"data": [{
        "code": "SUCCESS",
        "message": "The record has been converted successfully",
        "details": {
            "Contacts": {"name": "Chau Kitzman", "id": "1352736000000535006"},
            "Accounts": {"name": "Creative Business Systems", "id": "1352736000000535005"},
            "Deals": None}}]})])
    m = load(h)
    out, err = m.zoho_convert_lead({"lead_id": "123"}, {})
    assert err is None
    assert out["contact_id"] == "1352736000000535006", out
    assert out["account_id"] == "1352736000000535005", out
    assert out["contact_name"] == "Chau Kitzman", out
    assert out["deal_id"] == "", out


def t_convert_lead_refusal_raises():
    h, _ = helpers([(200, {"data": [{"code": "INVALID_DATA",
                                     "message": "already converted"}]})])
    m = load(h)
    try:
        m.zoho_convert_lead({"lead_id": "123"}, {})
        assert False, "should have raised"
    except RuntimeError as e:
        assert "refused to convert" in str(e), e


def t_convert_lead_with_deal():
    h, _ = helpers([(200, {"data": [{"code": "SUCCESS", "details": {
        "Contacts": {"name": "A", "id": "1"},
        "Accounts": {"name": "B", "id": "2"},
        "Deals": {"name": "Big deal", "id": "3"}}}]})])
    m = load(h)
    out, _ = m.zoho_convert_lead({"lead_id": "123", "deal": {
        "Deal_Name": "Big deal", "Closing_Date": "2026-12-31", "Stage": "Qualification"}}, {})
    assert out["deal_id"] == "3" and out["deal_name"] == "Big deal", out


def _plan_helpers(matched, current, second=None):
    """COQL twice: caller's query, then the before-values by id."""
    calls = []
    seq = [matched, current] + ([second] if second is not None else [])

    def _post(url, obj, **k):
        calls.append(obj.get("select_query", ""))
        rows = seq.pop(0) if seq else []
        return 200, json.dumps({"data": rows}).encode()

    store = {}
    return {"oauth_refresh": lambda p, **k: {"access_token": "t",
                                             "instance_url": "https://x"},
            "http_get_json": lambda u, **k: (200, b"{}"),
            "http_post_json": _post,
            "http_delete_json": lambda u, **k: (200, b"{}"),
            "WS": "/tmp/rc-test",
            "jload": lambda path, default=None: store.get(path, default if default is not None else {}),
            "jsave": lambda path, obj: store.__setitem__(path, obj)}, calls


def t_plan_update_fingerprints():
    h, calls = _plan_helpers(
        [{"id": "1"}, {"id": "2"}],
        [{"id": "1", "Lead_Status": "New"}, {"id": "2", "Lead_Status": "New"}])
    m = load(h)
    out, err = m.zoho_plan_update(
        {"module": "Leads", "query": "select id from Leads where x = 1",
         "changes": {"Lead_Status": "Contacted"}}, {})
    assert err is None
    assert out["count"] == 2 and out["would_change"] == 2, out
    assert "apply_update" in out["summary"], out
    assert len(calls) == 2, calls


def t_plan_update_stable_hash():
    """Same state, same digest, regardless of row order."""
    h1, _ = _plan_helpers([{"id": "1"}, {"id": "2"}],
                          [{"id": "1", "S": "a"}, {"id": "2", "S": "b"}])
    h2, _ = _plan_helpers([{"id": "2"}, {"id": "1"}],
                          [{"id": "2", "S": "b"}, {"id": "1", "S": "a"}])
    a = load(h1); b = load(h2)
    args = {"module": "Leads", "query": "select id from Leads where x=1", "changes": {"S": "z"}}
    a.zoho_plan_update(args, {}); b.zoho_plan_update(args, {})
    fa = a._plan_load(a._plan_key("Leads", args["query"], args["changes"]))["fingerprint"]
    fb = b._plan_load(b._plan_key("Leads", args["query"], args["changes"]))["fingerprint"]
    assert fa == fb, (fa, fb)


def t_plan_update_no_match_raises():
    h, _ = _plan_helpers([], [])
    m = load(h)
    try:
        m.zoho_plan_update({"module": "Leads", "query": "select id from Leads where x=1",
                            "changes": {"S": "z"}}, {})
        assert False
    except RuntimeError as e:
        assert "matched no records" in str(e), e


def t_plan_update_over_limit():
    h, _ = _plan_helpers([{"id": str(i)} for i in range(30)], [])
    m = load(h)
    try:
        m.zoho_plan_update({"module": "Leads", "query": "select id from Leads where x=1",
                            "changes": {"S": "z"}, "max_records": 10}, {})
        assert False
    except RuntimeError as e:
        assert "max_records" in str(e), e


def _plan_then_apply(plan_rows, apply_rows, put=None):
    """One helper dict across both calls so the plan store persists."""
    calls = []
    seq = list(plan_rows) + list(apply_rows)
    store = {}

    def _post(url, obj, **k):
        calls.append(obj.get("select_query", ""))
        rows = seq.pop(0) if seq else []
        return 200, json.dumps({"data": rows}).encode()

    h = {"oauth_refresh": lambda p, **k: {"access_token": "t", "instance_url": "https://x"},
         "http_get_json": lambda u, **k: (200, b"{}"),
         "http_post_json": _post,
         "http_delete_json": lambda u, **k: (200, b"{}"),
         "WS": "/tmp/rc-test",
         "jload": lambda path, default=None: store.get(path, default if default is not None else {}),
         "jsave": lambda path, obj: store.__setitem__(path, obj)}
    m = load(h)
    if put:
        m._put = put
    return m


def t_apply_update_clean():
    """No drift: the write goes through."""
    before = [{"id": "1", "S": "a"}, {"id": "2", "S": "b"}]
    m = _plan_then_apply(
        [[{"id": "1"}, {"id": "2"}], before],
        [[{"id": "1"}, {"id": "2"}], before],
        put=lambda url, obj, hdrs: (200, json.dumps({"data": [
            {"code": "SUCCESS", "details": {"id": "1"}},
            {"code": "SUCCESS", "details": {"id": "2"}}]}).encode()))
    args = {"module": "Leads", "query": "select id from Leads where x=1",
            "changes": {"S": "z"}}
    m.zoho_plan_update(args, {})
    out, err = m.zoho_apply_update(args, {})
    assert err is None and out["succeeded"] == 2, out
    assert out["records_applied"] == 2, out


def t_apply_without_plan_refuses():
    m = _plan_then_apply([[{"id": "1"}], [{"id": "1", "S": "a"}]], [])
    try:
        m.zoho_apply_update({"module": "Leads",
                             "query": "select id from Leads where x=1",
                             "changes": {"S": "z"}}, {})
        assert False, "applied with no plan"
    except RuntimeError as e:
        assert "No current plan" in str(e), e


def t_apply_update_detects_drift():
    """A record edited after planning must block the write."""
    hit = []
    m = _plan_then_apply(
        [[{"id": "1"}, {"id": "2"}], [{"id": "1", "S": "a"}, {"id": "2", "S": "b"}]],
        [[{"id": "1"}, {"id": "2"}], [{"id": "1", "S": "a"}, {"id": "2", "S": "EDITED"}]],
        put=lambda url, obj, hdrs: (hit.append(1), (200, b"{}"))[1])
    args = {"module": "Leads", "query": "select id from Leads where x=1",
            "changes": {"S": "z"}}
    m.zoho_plan_update(args, {})
    try:
        m.zoho_apply_update(args, {})
        assert False, "drift not detected"
    except RuntimeError as e:
        assert "moved since the plan" in str(e), e
    assert not hit, "write was attempted despite drift"


def t_apply_update_detects_new_match():
    """A record that newly matches the filter is drift too."""
    m = _plan_then_apply(
        [[{"id": "1"}], [{"id": "1", "S": "a"}]],
        [[{"id": "1"}, {"id": "9"}], [{"id": "1", "S": "a"}, {"id": "9", "S": "new"}]])
    args = {"module": "Leads", "query": "select id from Leads where x=1",
            "changes": {"S": "z"}}
    m.zoho_plan_update(args, {})
    try:
        m.zoho_apply_update(args, {})
        assert False
    except RuntimeError as e:
        assert "moved since the plan" in str(e), e





def t_apply_update_rejects_junk():
    h, _ = _plan_helpers([], [])
    m = load(h)
    for bad in ({}, {"module": "Leads"},
                {"module": "Leads", "query": "delete from Leads"}):
        try:
            m.zoho_apply_update(bad, {})
            assert False, "accepted %r" % bad
        except RuntimeError:
            pass


for name, fn in sorted((k, v) for k, v in list(globals().items())
                       if k.startswith("t_")):
    check(name[2:], fn)

print("passed: %d" % len(PASS))
for p in PASS:
    print("  ok   %s" % p)
if FAIL:
    print("\nfailed: %d" % len(FAIL))
    for f in FAIL:
        print("  FAIL %s" % f)
sys.exit(1 if FAIL else 0)
