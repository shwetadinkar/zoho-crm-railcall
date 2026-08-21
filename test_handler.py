#!/usr/bin/env python3
"""Mocked tests for shweta/zoho-crm.

Runs the handler against a fake __rc_helpers__ so every code path - including
the retry policy, which is hard to trigger against a live org - is exercised
without touching Zoho.

    python3 test_handler.py
"""

import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HANDLER = os.path.join(HERE, "handlers", "handler.py")
MANIFEST = os.path.join(HERE, "module.json")

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
        FAIL.append("%s - %s" % (name, e))
    except Exception as e:
        FAIL.append("%s - unexpected %s: %s" % (name, type(e).__name__, e))


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
    # a dict stamp, in case the platform ever enriches it
    out, _ = m.zoho_create_record(
        {"module": "Leads", "records": [{"a": 1}]},
        {"actor": "agent:x", "run_id": "r-42"})
    assert out["origin"]["actor"] == "agent:x", out["origin"]
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
    """A 401 is deterministic - it must fail on the first attempt, not the fifth."""
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


def t_convert_lead_parses_details():
    """Zoho nests created records under details.<Module>.id - verified live."""
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


def _handover_env(users, scans, put=None):
    """scans: list of row-lists served to successive COQL calls."""
    seq = list(scans)
    store = {}
    calls = []

    def _post(url, obj, **k):
        calls.append(obj.get("select_query", ""))
        return 200, json.dumps({"data": seq.pop(0) if seq else []}).encode()

    def _get(url, **k):
        if "users" in url:
            return 200, json.dumps({"users": users}).encode()
        return 200, b"{}"

    h = {"oauth_refresh": lambda p, **k: {"access_token": "t", "instance_url": "https://x"},
         "http_get_json": _get, "http_post_json": _post,
         "http_delete_json": lambda u, **k: (200, b"{}"),
         "WS": "/tmp/rc-test",
         "jload": lambda path, default=None: store.get(path, default if default is not None else {}),
         "jsave": lambda path, obj: store.__setitem__(path, obj)}
    m = load(h)
    if put:
        m._put = put
    return m, calls


USERS = [{"id": "100", "full_name": "Ann Leaver", "email": "ann@x.com"},
         {"id": "200", "full_name": "Bob Taker", "email": "bob@x.com"}]


def t_handover_resolves_by_email():
    m, _ = _handover_env(USERS, [[{"id": "1"}], [], [], [], []])
    out, err = m.zoho_plan_handover({"from_user": "ann@x.com", "to_user": "200"}, {})
    assert err is None
    assert out["from_user"] == "Ann Leaver" and out["to_user"] == "Bob Taker", out


def t_handover_same_user_refused():
    m, _ = _handover_env(USERS, [])
    try:
        m.zoho_plan_handover({"from_user": "100", "to_user": "100"}, {})
        assert False
    except RuntimeError as e:
        assert "same person" in str(e), e


def t_handover_unknown_user():
    m, _ = _handover_env(USERS, [])
    try:
        m.zoho_plan_handover({"from_user": "nobody@x.com", "to_user": "200"}, {})
        assert False
    except RuntimeError as e:
        assert "No active user" in str(e), e


def t_handover_nothing_owned():
    m, _ = _handover_env(USERS, [[], [], [], []])
    try:
        m.zoho_plan_handover({"from_user": "100", "to_user": "200"}, {})
        assert False
    except RuntimeError as e:
        assert "nothing to hand" in str(e), e


def t_handover_excludes_closed_deals():
    """Deals query must carry a stage filter unless closed_deals=include."""
    m, calls = _handover_env(USERS, [[{"id": "1"}], [{"id": "2"}], [], [],
                                     [{"id": "2"}, {"id": "3"}]])
    out, _ = m.zoho_plan_handover({"from_user": "100", "to_user": "200"}, {})
    deals_q = [c for c in calls if "from Deals" in c][0]
    # one NOT IN, never chained !=; Zoho's parser rejects three of those
    assert "Stage not in (" in deals_q, deals_q
    assert "Stage !=" not in deals_q, deals_q
    assert out["closed_deals_excluded"] == 1, out


def t_handover_include_closed():
    m, calls = _handover_env(USERS, [[{"id": "1"}], [{"id": "2"}], [], []])
    m.zoho_plan_handover({"from_user": "100", "to_user": "200",
                          "closed_deals": "include"}, {})
    deals_q = [c for c in calls if "from Deals" in c][0]
    assert "Stage not in" not in deals_q, deals_q


def t_handover_bad_closed_flag():
    m, _ = _handover_env(USERS, [])
    try:
        m.zoho_plan_handover({"from_user": "100", "to_user": "200",
                              "closed_deals": "maybe"}, {})
        assert False
    except RuntimeError as e:
        assert "skip" in str(e), e


def t_apply_handover_clean():
    # plan scans 4 modules plus one extra Deals pass for the excluded count
    plan_scans = [[{"id": "1"}], [], [], [], []]
    apply_scans = [[{"id": "1"}], [], [], []]
    m, _ = _handover_env(USERS, plan_scans + apply_scans,
                         put=lambda u, o, h: (200, json.dumps({"data": [
                             {"code": "SUCCESS", "details": {"id": "1"}}]}).encode()))
    args = {"from_user": "100", "to_user": "200"}
    m.zoho_plan_handover(args, {})
    out, err = m.zoho_apply_handover(args, {})
    assert err is None and out["moved"] == 1, out


def t_apply_handover_detects_drift():
    hit = []
    m, _ = _handover_env(USERS,
                         [[{"id": "1"}], [], [], [], []]
                         + [[{"id": "1"}, {"id": "9"}], [], [], []],
                         put=lambda u, o, h: (hit.append(1), (200, b"{}"))[1])
    args = {"from_user": "100", "to_user": "200"}
    m.zoho_plan_handover(args, {})
    try:
        m.zoho_apply_handover(args, {})
        assert False, "drift not detected"
    except RuntimeError as e:
        assert "changed since the plan" in str(e), e
    assert not hit, "write attempted despite drift"


def t_apply_handover_without_plan():
    m, _ = _handover_env(USERS, [[{"id": "1"}], [], [], []])
    try:
        m.zoho_apply_handover({"from_user": "100", "to_user": "200"}, {})
        assert False
    except RuntimeError as e:
        assert "No current plan" in str(e), e


def t_credential_spec_matches_handler():
    """The declared credential fields must be the ones we actually consume.

    oauth_refresh reads token_url, client_id, client_secret and refresh_token
    straight out of the vault entry, and _auth reads instance_url off the
    result. Declaring anything else would render a form that produces an entry
    the platform helper cannot use.
    """
    import json as _j
    spec = _j.load(open(MANIFEST))["credential_spec"]
    declared = set(spec["required"]) | set(spec.get("optional") or [])
    needed = {"client_id", "client_secret", "refresh_token",
              "token_url", "instance_url"}
    assert needed <= declared, "not declared: %s" % (needed - declared)
    assert not (declared - needed), "declared but unused: %s" % (declared - needed)
    assert spec["provider"] == "zoho", spec
    assert spec["shape"] == "dict", spec


def t_manifest_metadata_present():
    """homepage, tests_url and the sandbox declaration must survive edits."""
    import json as _j
    m = _j.load(open(MANIFEST))
    for key in ("homepage", "tests_url", "requires", "credential_spec"):
        assert m.get(key), "missing %s" % key
    assert m["requires"].get("subprocess") is False, m["requires"]


def t_declared_inputs_are_read():
    """Every input name a command declares must appear somewhere in the handler.

    This exists because plan_handover once declared include_closed while the
    handler read closed_deals. Validation rejected the name the code wanted and
    accepted a name the code ignored, so the option silently did nothing.

    File-wide rather than per-function, since several commands read their inputs
    through shared helpers like _records() and _module_name().
    """
    import json as _j
    src = open(HANDLER).read()
    problems = []
    for cmd in _j.load(open(MANIFEST))["commands"]:
        for field in (cmd.get("input_schema") or {}):
            if ('"%s"' % field) not in src and ("'%s'" % field) not in src:
                problems.append("%s declares %r, not read anywhere"
                                % (cmd["id"], field))
    assert not problems, problems


def _paging_helpers(pages):
    """pages: list of (rows, more_records) served to successive COQL calls."""
    seq = list(pages)
    calls = []

    def _post(url, obj, **k):
        q = obj.get("select_query", "")
        calls.append(q)
        rows, more = seq.pop(0) if seq else ([], False)
        return 200, json.dumps({"data": rows, "info": {"more_records": more}}).encode()

    store = {}
    return {"oauth_refresh": lambda p, **k: {"access_token": "t", "instance_url": "https://x"},
            "http_get_json": lambda u, **k: (200, b"{}"),
            "http_post_json": _post,
            "http_delete_json": lambda u, **k: (200, b"{}"),
            "WS": "/tmp/rc-test",
            "jload": lambda path, default=None: store.get(path, default if default is not None else {}),
            "jsave": lambda path, obj: store.__setitem__(path, obj)}, calls


def t_coql_all_pages_until_exhausted():
    """A scan must not stop at the first page. This is the silent-truncation bug."""
    h, calls = _paging_helpers([([{"id": str(i)} for i in range(200)], True),
                               ([{"id": str(i)} for i in range(200, 340)], False)])
    m = load(h)
    rows = m._coql_all("select Owner from Leads where Owner = '1'")
    assert len(rows) == 340, len(rows)
    assert len(calls) == 2, calls
    assert "limit 0, 200" in calls[0], calls[0]
    assert "limit 200, 200" in calls[1], calls[1]


def t_coql_all_refuses_past_cap():
    h, _ = _paging_helpers([([{"id": str(i)} for i in range(200)], True)] * 20)
    m = load(h)
    try:
        m._coql_all("select Owner from Leads where x = 1", cap=300)
        assert False, "should have refused"
    except RuntimeError as e:
        assert "Narrow it" in str(e), e


def t_strip_limit():
    h, _ = _paging_helpers([])
    m = load(h)
    for q, want in [("select a from B where c = 1 limit 200", "select a from B where c = 1"),
                    ("select a from B where c = 1 limit 0, 200", "select a from B where c = 1"),
                    ("select a from B where c = 1", "select a from B where c = 1"),
                    ("select a from B where c = 1 LIMIT 5", "select a from B where c = 1")]:
        assert m._strip_limit(q) == want, (q, m._strip_limit(q))


def t_handover_scan_paginates():
    """Ownership scans page too, or a handover under-reports."""
    users = [{"id": "100", "full_name": "A", "email": "a@x.com"},
             {"id": "200", "full_name": "B", "email": "b@x.com"}]
    seq = [([{"id": str(i)} for i in range(200)], True),
           ([{"id": "900"}], False)] + [([], False)] * 6
    calls = []

    def _post(url, obj, **k):
        calls.append(obj.get("select_query", ""))
        rows, more = seq.pop(0) if seq else ([], False)
        return 200, json.dumps({"data": rows, "info": {"more_records": more}}).encode()

    store = {}
    h = {"oauth_refresh": lambda p, **k: {"access_token": "t", "instance_url": "https://x"},
         "http_get_json": lambda u, **k: (200, json.dumps({"users": users}).encode()),
         "http_post_json": _post, "http_delete_json": lambda u, **k: (200, b"{}"),
         "WS": "/tmp/rc-test",
         "jload": lambda path, default=None: store.get(path, default if default is not None else {}),
         "jsave": lambda path, obj: store.__setitem__(path, obj)}
    m = load(h)
    out, _ = m.zoho_plan_handover({"from_user": "100", "to_user": "200"}, {})
    assert out["counts"]["Leads"] == 201, out["counts"]


def t_apply_update_validates_field_names():
    """Field names reach COQL by interpolation, so validate them here too."""
    h, _ = _paging_helpers([])
    m = load(h)
    try:
        m.zoho_apply_update({"module": "Leads",
                             "query": "select id from Leads where x = 1",
                             "changes": {"Email' or '1'='1": "x"}}, {})
        assert False, "accepted an injectable field name"
    except RuntimeError as e:
        assert "not valid field api_names" in str(e), e


def t_plan_delete_fingerprints():
    h, _ = _paging_helpers([([{"id": "1"}, {"id": "2"}], False),
                           ([{"id": "1", "Modified_Time": "t1"},
                             {"id": "2", "Modified_Time": "t2"}], False)])
    m = load(h)
    out, err = m.zoho_plan_delete({"module": "Leads",
                                   "query": "select id from Leads where x = 1"}, {})
    assert err is None and out["count"] == 2, out
    assert "recycle bin" in out["summary"], out


def t_apply_delete_refuses_without_plan():
    h, _ = _paging_helpers([])
    m = load(h)
    try:
        m.zoho_apply_delete({"module": "Leads",
                             "query": "select id from Leads where x = 1"}, {})
        assert False
    except RuntimeError as e:
        assert "No current plan" in str(e), e


def t_apply_delete_detects_drift():
    """A record touched after planning must block the delete."""
    seq = [([{"id": "1"}], False), ([{"id": "1", "Modified_Time": "t1"}], False),
           ([{"id": "1"}], False), ([{"id": "1", "Modified_Time": "CHANGED"}], False)]
    calls, store = [], {}

    def _post(url, obj, **k):
        calls.append(obj.get("select_query", ""))
        rows, more = seq.pop(0) if seq else ([], False)
        return 200, json.dumps({"data": rows, "info": {"more_records": more}}).encode()

    deleted = []
    h = {"oauth_refresh": lambda p, **k: {"access_token": "t", "instance_url": "https://x"},
         "http_get_json": lambda u, **k: (200, b"{}"), "http_post_json": _post,
         "http_delete_json": lambda u, **k: (deleted.append(u), (200, b"{}"))[1],
         "WS": "/tmp/rc-test",
         "jload": lambda path, default=None: store.get(path, default if default is not None else {}),
         "jsave": lambda path, obj: store.__setitem__(path, obj)}
    m = load(h)
    args = {"module": "Leads", "query": "select id from Leads where x = 1"}
    m.zoho_plan_delete(args, {})
    try:
        m.zoho_apply_delete(args, {})
        assert False, "drift not detected"
    except RuntimeError as e:
        assert "Refusing to delete" in str(e), e
    assert not deleted, "delete was issued despite drift"


def t_every_high_risk_write_has_a_plan_step():
    """Anything risk=high that mutates must be reachable only through a plan.

    delete_record and convert_lead are deliberate exceptions: both take explicit
    record ids the operator saw in the preview, and both have plan-gated
    equivalents. This test pins that list so a new high-risk write cannot be
    added without a decision.
    """
    import json as _j
    m = _j.load(open(MANIFEST))
    planned = {"zoho.apply_update", "zoho.apply_delete", "zoho.apply_handover",
               "zoho.apply_rollback", "zoho.apply_merge", "zoho.apply_upsert"}
    by_id = {"zoho.delete_record", "zoho.convert_lead", "zoho.update_record"}
    high = {c["id"] for c in m["commands"]
            if c.get("risk") == "high" and c.get("mode") == "write_requires_approval"}
    assert high == planned | by_id, high


def _preflight_env(fail_scopes=()):
    """Every probe succeeds unless its endpoint is named in fail_scopes."""
    def _fail(url):
        return any(f in url for f in fail_scopes)

    class E(Exception):
        def __init__(s):
            super().__init__('HTTP 401: {"code":"OAUTH_SCOPE_MISMATCH"}')
            s.code = 401
        def read(s):
            return b'{"code":"OAUTH_SCOPE_MISMATCH"}'

    def _get(url, **k):
        if _fail(url):
            raise E()
        if "settings/fields" in url:
            return 200, json.dumps({"fields": [{"api_name": "Email"}]}).encode()
        if "/org" in url:
            return 200, json.dumps({"org": [{"company_name": "Acme", "id": "9",
                                             "country": "India"}]}).encode()
        if "users" in url:
            return 200, json.dumps({"users": [{"id": "1"}]}).encode()
        return 200, json.dumps({"data": []}).encode()

    def _post(url, obj, **k):
        if _fail("coql"):
            raise E()
        return 200, json.dumps({"data": [{"id": "1"}]}).encode()

    return {"oauth_refresh": lambda p, **k: {"access_token": "t",
                                             "instance_url": "https://www.zohoapis.in"},
            "http_get_json": _get, "http_post_json": _post,
            "http_delete_json": lambda u, **k: (200, b"{}")}


def t_preflight_all_scopes_ok():
    m = load(_preflight_env())
    out, err = m.zoho_verify_connection({}, {})
    assert err is None and out["ready"] is True, out
    assert all(v == "ok" for v in out["scopes"].values()), out["scopes"]
    assert out["org_name"] == "Acme", out
    assert not out["blocked_commands"], out


def t_preflight_names_missing_required_scope():
    """A missing coql scope must be named, not surface later as a bad query."""
    m = load(_preflight_env(fail_scopes=("coql",)))
    out, _ = m.zoho_verify_connection({}, {})
    assert out["ready"] is False, out
    assert out["scopes"]["ZohoCRM.coql.READ"] == "MISSING", out["scopes"]
    assert "search_records" in out["blocked_commands"], out
    assert "ZohoCRM.coql.READ" in out["summary"], out["summary"]


def t_preflight_optional_scope_does_not_block():
    m = load(_preflight_env(fail_scopes=("/org",)))
    out, _ = m.zoho_verify_connection({}, {})
    assert out["ready"] is True, out
    assert "optional" in out["scopes"]["ZohoCRM.org.READ"], out["scopes"]
    assert out["org_name"] == "", out


def t_auth_error_shows_vault_shape():
    """A cold buyer with no vault entry must be told exactly what to write."""
    def _boom(provider, **k):
        raise RuntimeError("no zoho OAuth credential saved")
    m = load({"oauth_refresh": _boom, "http_get_json": lambda u, **k: (200, b"{}"),
              "http_post_json": lambda u, o, **k: (200, b"{}"),
              "http_delete_json": lambda u, **k: (200, b"{}")})
    try:
        m.zoho_verify_connection({}, {})
        assert False
    except RuntimeError as e:
        for token in ("refresh_token", "client_id", "token_url", "instance_url",
                      "datacenter"):
            assert token in str(e), (token, str(e)[:200])


def t_version_strings_agree():
    """The handler docstring and the manifest must name the same version.

    They have drifted apart three times now, because the version lives in two
    places and only one of them is checked at publish time.
    """
    import json as _j, re as _re
    manifest = _j.load(open(MANIFEST))["version"]
    first = open(HANDLER).readline()
    found = _re.search(r'shweta/zoho-crm ([0-9.]+)', first)
    assert found, "handler docstring has no version: %r" % first
    assert found.group(1) == manifest, (found.group(1), manifest)


def t_declared_outputs_are_returned():
    """Every key in output_schema must actually be produced by the handler.

    plan_handover once declared 'breakdown' while returning 'counts', so the
    published schema described a field nobody could ever receive. The input
    equivalent of this test caught the same class of bug on closed_deals.
    """
    import ast, json as _j
    tree = ast.parse(open(HANDLER).read())
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def keys_in(node):
        """String keys from dict literals and from result["key"] = ... writes."""
        out = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Dict):
                for k in sub.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        out.add(k.value)
            elif isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant):
                if isinstance(sub.slice.value, str):
                    out.add(sub.slice.value)
        return out

    # commands that hand their return value to _summarise inherit its keys
    summarise_keys = keys_in(funcs["_summarise"])

    problems = []
    for cmd in _j.load(open(MANIFEST))["commands"]:
        fn = funcs.get(cmd["id"].replace(".", "_"))
        if not fn:
            problems.append("no handler for " + cmd["id"])
            continue
        produced = keys_in(fn)
        if "_summarise" in ast.dump(fn):
            produced |= summarise_keys
        for field in (cmd.get("output_schema") or {}):
            if field not in produced:
                problems.append("%s declares output %r, never returned"
                                % (cmd["id"], field))
    assert not problems, problems


def t_origin_records_string_stamp():
    """What the platform actually sends today: a bare ISO timestamp."""
    h, _ = helpers([(200, {"data": [{"code": "SUCCESS", "details": {"id": "1"}}]})])
    m = load(h)
    out, _ = m.zoho_create_record({"module": "Leads", "records": [{"a": 1}]},
                                  "2026-07-29T15:22:15Z")
    assert out["origin"]["stamp"] == "2026-07-29T15:22:15Z", out["origin"]
    assert out["origin"]["initiated_via"] == "railcall-airlock", out["origin"]



# ------------------------------------------------------------------- ledger
def _ledger_helpers(matched, current, store=None):
    """Two-response COQL fake plus a shared workspace store.

    Returns the store too, so a test can reach in and tamper with the ledger
    the way an attacker with disk access would.
    """
    calls = []
    store = store if store is not None else {}

    def _post(url, obj, **k):
        calls.append(("POST", url))
        q = (obj or {}).get("select_query", "")
        rows = current if " in (" in q else matched
        return 200, json.dumps({"data": rows}).encode()

    def _put(url, data, **k):
        calls.append(("PUT", url))
        return 200, json.dumps(
            {"data": [{"code": "SUCCESS", "details": {"id": "1"}}]}).encode()

    return {"oauth_refresh": lambda p, **k: {"access_token": "t",
                                             "instance_url": "https://x"},
            "http_get_json": lambda u, **k: (200, b"{}"),
            "http_post_json": _post,
            "http_patch_json": _put,
            "http_delete_json": lambda u, **k: (200, b"{}"),
            "WS": "/tmp/rc-test",
            "jload": lambda path, default=None: store.get(path, default if default is not None else {}),
            "jsave": lambda path, obj: store.__setitem__(path, obj)}, calls, store


def _load_put(h):
    """load(), with the hand-rolled PUT stubbed.

    _put bypasses __rc_helpers__ entirely because the platform ships no PUT, so
    a fake helper dict cannot intercept it.
    """
    m = load(h)
    m._put = lambda url, obj, hdrs: (200, json.dumps({"data": [
        {"code": "SUCCESS", "details": {"id": r.get("id", "1")}}
        for r in (obj or {}).get("data", [{}])]}).encode())
    return m


def t_ledger_chain_intact():
    h, _, _ = _ledger_helpers([], [])
    m = load(h)
    for i in range(3):
        m._ledger_append(m._ledger_note("applied", "apply_update", "Leads",
                                        "k%d" % i, {"records": i}))
    intact, checked, bad = m._ledger_verify()
    assert intact and checked == 3 and bad is None, (intact, checked, bad)


def t_ledger_detects_edited_entry():
    """Change one field in a sealed entry and the chain must name it."""
    h, _, store = _ledger_helpers([], [])
    m = load(h)
    for i in range(4):
        m._ledger_append(m._ledger_note("applied", "apply_update", "Leads",
                                        "k%d" % i, {"records": i}))
    book = store["/tmp/rc-test/zoho_ledger.json"]
    book["entries"][1]["detail"]["records"] = 999
    intact, checked, bad = m._ledger_verify()
    assert not intact and bad == 2, (intact, checked, bad)
    assert checked == 1, checked


def t_ledger_detects_removed_entry():
    """Deleting an entry breaks the prev link of the one after it."""
    h, _, store = _ledger_helpers([], [])
    m = load(h)
    for i in range(4):
        m._ledger_append(m._ledger_note("refused", "apply_update", "Leads",
                                        "k%d" % i, {"records": i}))
    book = store["/tmp/rc-test/zoho_ledger.json"]
    del book["entries"][1]
    intact, _checked, bad = m._ledger_verify()
    assert not intact and bad == 2, (intact, bad)


def t_ledger_rotates_and_stays_linked():
    """A rotated chain starts from the last sealed hash, not from genesis."""
    h, _, store = _ledger_helpers([], [])
    m = load(h)
    m._LEDGER_MAX = 3
    for i in range(4):
        m._ledger_append(m._ledger_note("applied", "apply_update", "Leads",
                                        "k%d" % i, {"records": i}))
    live = store["/tmp/rc-test/zoho_ledger.json"]
    archives = [k for k in store if k.startswith("/tmp/rc-test/zoho_ledger.json.")]
    assert len(archives) == 1, list(store)
    assert len(live["entries"]) == 1, live["entries"]
    sealed = store[archives[0]]["entries"][-1]["entry_hash"]
    assert live["chain_start"] == sealed, (live["chain_start"], sealed)
    intact, _c, _b = m._ledger_verify()
    assert intact


def t_verify_ledger_command_reports_counts():
    h, _, _ = _ledger_helpers([], [])
    m = load(h)
    m._ledger_append(m._ledger_note("applied", "apply_update", "Leads", "a", {}))
    m._ledger_append(m._ledger_note("refused", "apply_update", "Leads", "b", {}))
    out, err = m.zoho_verify_ledger({}, {})
    assert err is None
    assert out["intact"] and out["applied"] == 1 and out["refused"] == 1, out
    assert "not tamper-proof" in out["summary"], out["summary"]


# ------------------------------------------------- Modified_Time drift guard
def t_apply_update_refuses_on_unrelated_field_edit():
    """The gap this release closes.

    Plan Lead_Status. Someone edits Email on a matched record. Before 0.6.0 the
    fingerprint covered only Lead_Status, so the hash was unchanged and the
    write went through onto a record nobody had reviewed.
    """
    matched = [{"id": "1"}]
    planned = [{"id": "1", "Lead_Status": "New", "Modified_Time": "2026-01-01T00:00:00+05:30"}]
    h, _, store = _ledger_helpers(matched, planned)
    m = load(h)
    args = {"module": "Leads", "query": "select id from Leads where x = 1",
            "changes": {"Lead_Status": "Contacted"}}
    m.zoho_plan_update(args, {})

    # Email changed, Lead_Status untouched, Modified_Time moved as Zoho would.
    edited = [{"id": "1", "Lead_Status": "New",
               "Modified_Time": "2026-06-02T11:00:00+05:30"}]
    h2, _, _ = _ledger_helpers(matched, edited, store=store)
    m2 = load(h2)
    try:
        m2.zoho_apply_update(args, {})
        assert False, "should have refused"
    except RuntimeError as e:
        assert "moved since the plan" in str(e), e


def t_refusal_is_written_to_the_ledger():
    matched = [{"id": "1"}]
    h, _, store = _ledger_helpers(matched, [{"id": "1", "S": "a", "Modified_Time": "t1"}])
    m = load(h)
    args = {"module": "Leads", "query": "select id from Leads where x = 1",
            "changes": {"S": "z"}}
    m.zoho_plan_update(args, {})
    h2, _, _ = _ledger_helpers(matched, [{"id": "1", "S": "b", "Modified_Time": "t2"}],
                               store=store)
    m2 = load(h2)
    try:
        m2.zoho_apply_update(args, {})
    except RuntimeError:
        pass
    book = store["/tmp/rc-test/zoho_ledger.json"]
    assert len(book["entries"]) == 1, book
    entry = book["entries"][0]
    assert entry["outcome"] == "refused", entry
    assert entry["command"] == "apply_update", entry


def t_apply_records_prior_values_for_rollback():
    matched = [{"id": "1"}]
    rows = [{"id": "1", "S": "old", "Modified_Time": "t1"}]
    h, _, store = _ledger_helpers(matched, rows)
    m = _load_put(h)
    args = {"module": "Leads", "query": "select id from Leads where x = 1",
            "changes": {"S": "new"}}
    m.zoho_plan_update(args, {})
    out, err = m.zoho_apply_update(args, {})
    assert err is None and out["records_applied"] == 1, out
    entry = store["/tmp/rc-test/zoho_ledger.json"]["entries"][0]
    assert entry["outcome"] == "applied", entry
    before = entry["detail"]["before"]
    assert before == [{"id": "1", "before": {"S": "old"}}], before
    assert "Modified_Time" not in before[0]["before"], before


# ----------------------------------------------------------------- rollback
def t_plan_rollback_without_an_apply_raises():
    h, _, _ = _ledger_helpers([], [])
    m = load(h)
    try:
        m.zoho_plan_rollback({"module": "Leads", "query": "select id from Leads where x=1",
                              "changes": {"S": "z"}}, {})
        assert False
    except RuntimeError as e:
        assert "No applied change" in str(e), e


def t_rollback_restores_prior_values():
    matched = [{"id": "1"}]
    h, _, store = _ledger_helpers(matched, [{"id": "1", "S": "old", "Modified_Time": "t1"}])
    m = _load_put(h)
    args = {"module": "Leads", "query": "select id from Leads where x = 1",
            "changes": {"S": "new"}}
    m.zoho_plan_update(args, {})
    m.zoho_apply_update(args, {})

    # The org now reflects the applied change.
    after = [{"id": "1", "S": "new", "Modified_Time": "t2"}]
    h2, _, _ = _ledger_helpers(matched, after, store=store)
    m2 = _load_put(h2)
    plan, err = m2.zoho_plan_rollback(args, {})
    assert err is None and plan["count"] == 1, plan
    assert plan["records"][0]["restore_to"] == {"S": "old"}, plan["records"]
    assert plan["changed_again"] == [], plan["changed_again"]

    h3, _calls, _ = _ledger_helpers(matched, after, store=store)
    m3 = _load_put(h3)
    out, err = m3.zoho_apply_rollback(args, {})
    assert err is None and out["records_restored"] == 1, out
    assert out["succeeded"] == 1 and out["failed"] == 0, out
    # the restore is itself recorded, so an undo can be undone
    entries = store["/tmp/rc-test/zoho_ledger.json"]["entries"]
    assert [e["outcome"] for e in entries] == ["applied", "applied"], entries
    assert entries[-1]["detail"]["before"] == [{"id": "1", "before": {"S": "new"}}], entries[-1]


def t_plan_rollback_flags_records_changed_again():
    matched = [{"id": "1"}]
    h, _, store = _ledger_helpers(matched, [{"id": "1", "S": "old", "Modified_Time": "t1"}])
    m = _load_put(h)
    args = {"module": "Leads", "query": "select id from Leads where x = 1",
            "changes": {"S": "new"}}
    m.zoho_plan_update(args, {})
    m.zoho_apply_update(args, {})

    # Somebody moved it on again after the apply.
    h2, _, _ = _ledger_helpers(matched, [{"id": "1", "S": "third", "Modified_Time": "t3"}],
                               store=store)
    m2 = _load_put(h2)
    plan, _err = m2.zoho_plan_rollback(args, {})
    assert plan["changed_again"] == [{"id": "1", "fields": ["S"]}], plan["changed_again"]


def t_apply_rollback_refuses_on_drift():
    matched = [{"id": "1"}]
    h, _, store = _ledger_helpers(matched, [{"id": "1", "S": "old", "Modified_Time": "t1"}])
    m = _load_put(h)
    args = {"module": "Leads", "query": "select id from Leads where x = 1",
            "changes": {"S": "new"}}
    m.zoho_plan_update(args, {})
    m.zoho_apply_update(args, {})

    h2, _, _ = _ledger_helpers(matched, [{"id": "1", "S": "new", "Modified_Time": "t2"}],
                               store=store)
    m2 = _load_put(h2)
    m2.zoho_plan_rollback(args, {})

    # Record moves between the rollback plan and the rollback apply.
    h3, _, _ = _ledger_helpers(matched, [{"id": "1", "S": "meddled", "Modified_Time": "t9"}],
                               store=store)
    m3 = _load_put(h3)
    try:
        m3.zoho_apply_rollback(args, {})
        assert False, "should have refused"
    except RuntimeError as e:
        assert "Refusing to roll back" in str(e), e
    outcomes = [e["outcome"] for e in store["/tmp/rc-test/zoho_ledger.json"]["entries"]]
    assert outcomes == ["applied", "refused"], outcomes


# --------------------------------------------------------------- audit pack
def t_audit_pack_writes_a_file_not_inline_ids():
    h, _, store = _ledger_helpers([], [])
    m = load(h)
    m._ledger_append(m._ledger_note("applied", "apply_update", "Leads", "a",
                                    {"records": 3}))
    m._ledger_append(m._ledger_note("refused", "apply_update", "Leads", "b",
                                    {"records": 2}))
    out, err = m.zoho_audit_pack({}, {})
    assert err is None
    assert out["entries"] == 2 and out["applied"] == 1 and out["refused"] == 1, out
    assert out["records_changed"] == 3, out
    assert out["pack_path"] in store, list(store)
    # ids must not travel back inline; the pack is a file for exactly this reason
    assert "entries" not in str(out.get("records", "")), out


def t_audit_pack_filters_by_outcome_and_module():
    h, _, _ = _ledger_helpers([], [])
    m = load(h)
    m._ledger_append(m._ledger_note("applied", "apply_update", "Leads", "a", {"records": 1}))
    m._ledger_append(m._ledger_note("refused", "apply_update", "Deals", "b", {"records": 5}))
    out, _ = m.zoho_audit_pack({"outcome": "refused"}, {})
    assert out["entries"] == 1 and out["refused"] == 1, out
    out2, _ = m.zoho_audit_pack({"module": "Leads"}, {})
    assert out2["entries"] == 1 and out2["applied"] == 1, out2
    try:
        m.zoho_audit_pack({"outcome": "maybe"}, {})
        assert False
    except RuntimeError as e:
        assert "applied" in str(e), e


def t_audit_pack_reports_a_broken_chain():
    h, _, store = _ledger_helpers([], [])
    m = load(h)
    for i in range(3):
        m._ledger_append(m._ledger_note("applied", "apply_update", "Leads",
                                        "k%d" % i, {"records": 1}))
    store["/tmp/rc-test/zoho_ledger.json"]["entries"][0]["module"] = "Deals"
    out, _ = m.zoho_audit_pack({}, {})
    assert out["chain_intact"] is False, out
    assert "BROKEN" in out["summary"], out["summary"]


def t_module_version_comes_from_the_docstring():
    """The version must not gain a fourth literal home."""
    import json as _j
    h, _, _ = _ledger_helpers([], [])
    m = load(h)
    assert m._module_version() == _j.load(open(MANIFEST))["version"]



# --------------------------------------------------------------------- merge
def _merge_helpers(records, related=None, store=None):
    """Fake for the merge path.

    GET  {module}/{id}          -> the full record, from `records`
    POST coql                   -> id lookups and related-list queries
    POST {m}/{id}/actions/merge -> SUCCESS

    `records` maps id -> field dict. `related` maps id -> row count for every
    related list, defaulting to none attached.
    """
    calls = []
    store = store if store is not None else {}
    related = related or {}

    def _get(url, **k):
        calls.append(("GET", url))
        rid = url.rstrip("/").rsplit("/", 1)[-1]
        rec = records.get(rid)
        return 200, json.dumps({"data": [rec] if rec else []}).encode()

    def _post(url, obj, **k):
        calls.append(("POST", url))
        if url.endswith("/actions/merge"):
            return 200, json.dumps({"merge": [{"code": "SUCCESS"}]}).encode()
        q = (obj or {}).get("select_query", "")
        if " in (" in q:                       # _read_by_ids
            rows = [dict(v, id=k) for k, v in records.items() if k in q]
            return 200, json.dumps({"data": rows}).encode()
        for rid, n in related.items():         # related-list count
            if rid in q:
                return 200, json.dumps(
                    {"data": [{"id": "r%d" % i} for i in range(n)]}).encode()
        return 200, json.dumps({"data": []}).encode()

    return {"oauth_refresh": lambda p, **k: {"access_token": "t",
                                             "instance_url": "https://x"},
            "http_get_json": _get,
            "http_post_json": _post,
            "http_patch_json": lambda u, d, **k: (200, b"{}"),
            "http_delete_json": lambda u, **k: (200, b"{}"),
            "WS": "/tmp/rc-test",
            "jload": lambda path, default=None: store.get(path, default if default is not None else {}),
            "jsave": lambda path, obj: store.__setitem__(path, obj)}, calls, store


_M_ARGS = {"module": "Leads", "master_id": "100", "loser_ids": ["200"]}


def t_merge_rejects_unverified_modules():
    """Merge is verified on Leads and Contacts. It must not guess elsewhere."""
    h, _, _ = _merge_helpers({})
    m = load(h)
    for mod in ("Accounts", "Deals"):
        try:
            m.zoho_plan_merge({"module": mod, "master_id": "1",
                               "loser_ids": ["2"]}, {})
            assert False, mod + " should be rejected"
        except RuntimeError as e:
            assert "verified on" in str(e), e


def t_merge_rejects_self_and_duplicates():
    h, _, _ = _merge_helpers({})
    m = load(h)
    for args, want in (
        ({"module": "Leads", "master_id": "1", "loser_ids": ["1"]}, "into itself"),
        ({"module": "Leads", "master_id": "1", "loser_ids": ["2", "2"]}, "duplicates"),
        ({"module": "Leads", "master_id": "1", "loser_ids": []}, "required"),
        ({"module": "Leads", "master_id": "", "loser_ids": ["2"]}, "required"),
    ):
        try:
            m.zoho_plan_merge(args, {})
            assert False, args
        except RuntimeError as e:
            assert want in str(e), (args, str(e))


def t_merge_caps_losers_per_call():
    h, _, _ = _merge_helpers({})
    m = load(h)
    try:
        m.zoho_plan_merge({"module": "Leads", "master_id": "1",
                           "loser_ids": ["2", "3", "4", "5"]}, {})
        assert False
    except RuntimeError as e:
        assert "per call" in str(e), e


def t_merge_conflicts_filters_system_stamps():
    """A live preview showed 4 of 7 conflicts were Created_Time, Modified_Time,
    Last_Activity_Time and Change_Log_Time__s. Nobody can act on those, and
    they bury the ones who can."""
    records = {
        "100": {"id": "100", "Phone": "111", "Created_Time": "t1",
                "Modified_Time": "t1", "Last_Activity_Time": "t1",
                "Change_Log_Time__s": "t1", "$editable": True,
                "Full_Name": "A"},
        "200": {"id": "200", "Phone": "222", "Created_Time": "t2",
                "Modified_Time": "t2", "Last_Activity_Time": "t2",
                "Change_Log_Time__s": "t2", "$editable": False,
                "Full_Name": "B"},
    }
    h, _, _ = _merge_helpers(records)
    m = load(h)
    out, _ = m.zoho_plan_merge(_M_ARGS, {})
    loser = out["losers"][0]
    fields = {c["field"] for c in loser["conflicts"]}
    assert fields == {"Phone", "Full_Name"}, fields
    for noise in ("Created_Time", "Modified_Time", "Last_Activity_Time",
                  "Change_Log_Time__s", "$editable"):
        assert noise not in fields, noise
    # filtered, not silently dropped
    assert loser["system_fields_differing"] >= 4, loser


def t_plan_merge_reports_what_is_lost():
    """The conflict list is the product: the master wins silently."""
    records = {
        "100": {"id": "100", "Last_Name": "Ada", "Phone": "111",
                "Modified_Time": "t1"},
        "200": {"id": "200", "Last_Name": "Ada", "Phone": "222",
                "Email": "ada@x.com", "Modified_Time": "t1"},
    }
    h, _, _ = _merge_helpers(records, related={"200": 3})
    m = load(h)
    out, err = m.zoho_plan_merge(_M_ARGS, {})
    assert err is None
    loser = out["losers"][0]
    conflicts = {c["field"]: (c["master"], c["loser"]) for c in loser["conflicts"]}
    assert conflicts["Phone"] == ("111", "222"), conflicts
    assert "Last_Name" not in conflicts, "identical values are not conflicts"
    only = {o["field"] for o in loser["only_on_loser"]}
    assert "Email" in only, only
    assert out["irreversible"] is True
    assert "cannot be recovered" in out["summary"], out["summary"]


def t_plan_merge_counts_related_records():
    records = {"100": {"id": "100", "Modified_Time": "t1"},
               "200": {"id": "200", "Modified_Time": "t1"}}
    h, _, _ = _merge_helpers(records, related={"200": 4})
    m = load(h)
    out, _ = m.zoho_plan_merge(_M_ARGS, {})
    rel = out["losers"][0]["related"]
    assert rel["Notes"]["count"] == 4, rel
    assert out["related_records_moving"] >= 4, out["related_records_moving"]


def t_plan_merge_raises_on_missing_record():
    records = {"100": {"id": "100", "Modified_Time": "t1"}}
    h, _, _ = _merge_helpers(records)
    m = load(h)
    try:
        m.zoho_plan_merge(_M_ARGS, {})
        assert False
    except RuntimeError as e:
        assert "do not exist" in str(e), e


def t_apply_merge_without_a_plan_raises():
    records = {"100": {"id": "100", "Modified_Time": "t1"},
               "200": {"id": "200", "Modified_Time": "t1"}}
    h, _, _ = _merge_helpers(records)
    m = load(h)
    try:
        m.zoho_apply_merge(_M_ARGS, {})
        assert False
    except RuntimeError as e:
        assert "No current plan" in str(e), e


def t_apply_merge_refuses_on_drift():
    records = {"100": {"id": "100", "Modified_Time": "t1"},
               "200": {"id": "200", "Modified_Time": "t1"}}
    h, _, store = _merge_helpers(records)
    m = load(h)
    m.zoho_plan_merge(_M_ARGS, {})

    moved = {"100": {"id": "100", "Modified_Time": "t1"},
             "200": {"id": "200", "Modified_Time": "t9"}}
    h2, _, _ = _merge_helpers(moved, store=store)
    m2 = load(h2)
    try:
        m2.zoho_apply_merge(_M_ARGS, {})
        assert False, "should have refused"
    except RuntimeError as e:
        assert "Refusing to merge" in str(e), e
    outcomes = [x["outcome"] for x in store["/tmp/rc-test/zoho_ledger.json"]["entries"]]
    assert outcomes == ["refused"], outcomes


def t_apply_merge_refuses_when_the_master_moves():
    """The master's values are the ones that win, so its drift matters too."""
    records = {"100": {"id": "100", "Modified_Time": "t1"},
               "200": {"id": "200", "Modified_Time": "t1"}}
    h, _, store = _merge_helpers(records)
    m = load(h)
    m.zoho_plan_merge(_M_ARGS, {})

    moved = {"100": {"id": "100", "Modified_Time": "t7"},
             "200": {"id": "200", "Modified_Time": "t1"}}
    h2, _, _ = _merge_helpers(moved, store=store)
    m2 = load(h2)
    try:
        m2.zoho_apply_merge(_M_ARGS, {})
        assert False, "should have refused"
    except RuntimeError as e:
        assert "Refusing to merge" in str(e), e


def t_apply_merge_archives_before_it_destroys():
    """The ledger entry is the only readable copy of what was merged away."""
    records = {"100": {"id": "100", "Phone": "111", "Modified_Time": "t1"},
               "200": {"id": "200", "Phone": "222", "Email": "ada@x.com",
                       "Modified_Time": "t1"}}
    h, _, store = _merge_helpers(records, related={"200": 2})
    m = load(h)
    m.zoho_plan_merge(_M_ARGS, {})
    out, err = m.zoho_apply_merge(_M_ARGS, {})
    assert err is None
    assert out["succeeded"] == 1 and out["failed"] == 0, out
    assert out["recoverable"] is False, out

    entries = store["/tmp/rc-test/zoho_ledger.json"]["entries"]
    assert [e["outcome"] for e in entries] == ["applied"], entries
    archive = entries[0]["detail"]["archive"]
    assert archive[0]["id"] == "200", archive
    assert archive[0]["record"]["Phone"] == "222", archive
    assert entries[0]["detail"]["irreversible"] is True
    # the archive must be written BEFORE the merge fires, so it is entry 1
    assert out["ledger_seq"] == entries[0]["seq"], (out["ledger_seq"], entries)


def t_apply_merge_calls_the_verified_endpoint_shape():
    records = {"100": {"id": "100", "Modified_Time": "t1"},
               "200": {"id": "200", "Modified_Time": "t1"}}
    h, calls, _ = _merge_helpers(records)
    m = load(h)
    m.zoho_plan_merge(_M_ARGS, {})
    m.zoho_apply_merge(_M_ARGS, {})
    merge_calls = [u for meth, u in calls if u.endswith("/actions/merge")]
    assert len(merge_calls) == 1, merge_calls
    assert merge_calls[0].endswith("Leads/100/actions/merge"), merge_calls[0]


def t_apply_merge_one_loser_per_call():
    """Batching hides which record failed; a merge failure has to be legible."""
    records = {"100": {"id": "100", "Modified_Time": "t1"},
               "200": {"id": "200", "Modified_Time": "t1"},
               "300": {"id": "300", "Modified_Time": "t1"}}
    args = {"module": "Leads", "master_id": "100", "loser_ids": ["200", "300"]}
    h, calls, _ = _merge_helpers(records)
    m = load(h)
    m.zoho_plan_merge(args, {})
    out, _ = m.zoho_apply_merge(args, {})
    merge_calls = [u for meth, u in calls if u.endswith("/actions/merge")]
    assert len(merge_calls) == 2, merge_calls
    assert out["succeeded"] == 2, out



# -------------------------------------------------------------- bulk upsert
def _upsert_helpers(existing_rows, store=None, upsert_result=None):
    """COQL returns `existing_rows` for lookups; POST /upsert returns SUCCESS
    per record unless `upsert_result` overrides it."""
    calls = []
    store = store if store is not None else {}

    def _post(url, obj, **k):
        calls.append(("POST", url))
        if url.endswith("/upsert"):
            n = len((obj or {}).get("data") or [])
            rows = upsert_result if upsert_result is not None else [
                {"code": "SUCCESS", "details": {"id": str(900 + i)}}
                for i in range(n)]
            return 200, json.dumps({"data": rows}).encode()
        q = (obj or {}).get("select_query", "")
        if "where id in (" in q:               # _read_by_ids, for the guard
            rows = [r for r in existing_rows if str(r.get("id")) in q]
        else:                                  # duplicate-check lookup
            rows = [r for r in existing_rows
                    if any(str(v) in q for k2, v in r.items() if k2 != "id")]
        return 200, json.dumps({"data": rows}).encode()

    return {"oauth_refresh": lambda p, **k: {"access_token": "t",
                                             "instance_url": "https://x"},
            "http_get_json": lambda u, **k: (200, b"{}"),
            "http_post_json": _post,
            "http_patch_json": lambda u, d, **k: (200, b"{}"),
            "http_delete_json": lambda u, **k: (200, b"{}"),
            "WS": "/tmp/rc-test",
            "jload": lambda path, default=None: store.get(path, default if default is not None else {}),
            "jsave": lambda path, obj: store.__setitem__(path, obj)}, calls, store


_U_RECS = [{"Email": "a@x.com", "Last_Name": "A"},
           {"Email": "b@x.com", "Last_Name": "B"}]
_U_ARGS = {"module": "Leads", "records": _U_RECS,
           "duplicate_check_fields": ["Email"]}


def t_plan_upsert_requires_check_fields():
    """Without them the plan cannot say which records are creates."""
    h, _, _ = _upsert_helpers([])
    m = load(h)
    try:
        m.zoho_plan_upsert({"module": "Leads", "records": _U_RECS}, {})
        assert False
    except RuntimeError as e:
        assert "duplicate_check_fields" in str(e), e


def t_plan_upsert_requires_check_field_on_every_record():
    h, _, _ = _upsert_helpers([])
    m = load(h)
    try:
        m.zoho_plan_upsert({"module": "Leads",
                            "records": [{"Email": "a@x.com"}, {"Last_Name": "B"}],
                            "duplicate_check_fields": ["Email"]}, {})
        assert False
    except RuntimeError as e:
        assert "Missing" in str(e), e


def t_plan_upsert_splits_creates_from_updates():
    """The split is the whole point of the preview."""
    existing = [{"id": "1", "Email": "a@x.com", "Modified_Time": "t1"}]
    h, _, _ = _upsert_helpers(existing)
    m = load(h)
    out, err = m.zoho_plan_upsert(_U_ARGS, {})
    assert err is None
    assert out["total"] == 2, out
    assert out["will_update"] == 1 and out["will_create"] == 1, out
    assert out["updates"][0]["matched_on"] == "Email", out["updates"]
    assert out["creates"][0]["Email"] == "b@x.com", out["creates"]


def t_plan_upsert_states_the_narrower_drift_guarantee():
    """A record that does not exist yet has nothing that can move, and the
    output has to say so rather than implying plan_update's guarantee."""
    h, _, _ = _upsert_helpers([{"id": "1", "Email": "a@x.com", "Modified_Time": "t1"}])
    m = load(h)
    out, _ = m.zoho_plan_upsert(_U_ARGS, {})
    assert "no prior state" in out["drift_covers"], out["drift_covers"]
    assert "existing records only" in out["summary"], out["summary"]


def t_plan_upsert_reports_call_count():
    recs = [{"Email": "u%d@x.com" % i} for i in range(250)]
    h, _, _ = _upsert_helpers([])
    m = load(h)
    out, _ = m.zoho_plan_upsert({"module": "Leads", "records": recs,
                                 "duplicate_check_fields": ["Email"]}, {})
    assert out["calls_required"] == 3, out["calls_required"]
    assert out["will_create"] == 250, out["will_create"]


def t_plan_upsert_refuses_an_oversized_set():
    recs = [{"Email": "u%d@x.com" % i} for i in range(2001)]
    h, _, _ = _upsert_helpers([])
    m = load(h)
    try:
        m.zoho_plan_upsert({"module": "Leads", "records": recs,
                            "duplicate_check_fields": ["Email"]}, {})
        assert False
    except RuntimeError as e:
        assert "half-applying" in str(e), e


def t_apply_upsert_without_a_plan_raises():
    h, _, _ = _upsert_helpers([])
    m = load(h)
    try:
        m.zoho_apply_upsert(_U_ARGS, {})
        assert False
    except RuntimeError as e:
        assert "No current plan" in str(e), e


def t_apply_upsert_refuses_when_an_existing_record_moved():
    existing = [{"id": "1", "Email": "a@x.com", "Modified_Time": "t1"}]
    h, _, store = _upsert_helpers(existing)
    m = load(h)
    m.zoho_plan_upsert(_U_ARGS, {})

    moved = [{"id": "1", "Email": "a@x.com", "Modified_Time": "t9"}]
    h2, _, _ = _upsert_helpers(moved, store=store)
    m2 = load(h2)
    try:
        m2.zoho_apply_upsert(_U_ARGS, {})
        assert False, "should have refused"
    except RuntimeError as e:
        assert "Refusing to upsert" in str(e), e
    outcomes = [x["outcome"] for x in store["/tmp/rc-test/zoho_ledger.json"]["entries"]]
    assert outcomes == ["refused"], outcomes


def t_apply_upsert_commits_and_records():
    existing = [{"id": "1", "Email": "a@x.com", "Modified_Time": "t1"}]
    h, calls, store = _upsert_helpers(existing)
    m = load(h)
    m.zoho_plan_upsert(_U_ARGS, {})
    out, err = m.zoho_apply_upsert(_U_ARGS, {})
    assert err is None
    assert out["succeeded"] == 2 and out["failed"] == 0, out
    assert out["planned_updates"] == 1 and out["planned_creates"] == 1, out
    upserts = [u for meth, u in calls if u.endswith("/upsert")]
    assert len(upserts) == 1, upserts
    entry = store["/tmp/rc-test/zoho_ledger.json"]["entries"][0]
    assert entry["outcome"] == "applied" and entry["command"] == "apply_upsert"


def t_apply_upsert_batches_at_100():
    recs = [{"Email": "u%d@x.com" % i} for i in range(250)]
    args = {"module": "Leads", "records": recs,
            "duplicate_check_fields": ["Email"]}
    h, calls, _ = _upsert_helpers([])
    m = load(h)
    m.zoho_plan_upsert(args, {})
    out, _ = m.zoho_apply_upsert(args, {})
    upserts = [u for meth, u in calls if u.endswith("/upsert")]
    assert len(upserts) == 3, upserts
    assert out["succeeded"] == 250, out["succeeded"]


def t_apply_upsert_counts_partial_failure_from_the_body():
    """Zoho answers HTTP 200 when some rows fail. The status code is not the
    answer; the body is."""
    rows = [{"code": "SUCCESS", "details": {"id": "900"}},
            {"code": "DUPLICATE_DATA", "message": "duplicate"}]
    h, _, _ = _upsert_helpers([], upsert_result=rows)
    m = load(h)
    m.zoho_plan_upsert(_U_ARGS, {})
    out, _ = m.zoho_apply_upsert(_U_ARGS, {})
    assert out["succeeded"] == 1 and out["failed"] == 1, out
    assert out["ok"] is False, out
    assert out["errors"][0]["code"] == "DUPLICATE_DATA", out["errors"]



# ------------------------------------------------------- hygiene + readiness
def _scan_helpers(coql_rows=None, users=None, fields=None, record=None,
                  raise_on_query=None, saved=None):
    """COQL returns rows per query substring; GET serves users/fields/record."""
    calls = []
    coql_rows = coql_rows or {}

    def _get(url, **k):
        calls.append(("GET", url))
        if "settings/fields" in url:
            return 200, json.dumps({"fields": fields or []}).encode()
        if url.rstrip("/").endswith("users") or "users?" in url:
            return 200, json.dumps({"users": users or []}).encode()
        return 200, json.dumps({"data": [record] if record else []}).encode()

    def _post(url, obj, **k):
        calls.append(("POST", url))
        q = (obj or {}).get("select_query", "")
        if raise_on_query and raise_on_query in q:
            raise FakeHTTPError(400, '{"code":"INVALID_QUERY"}')
        for frag, rows in coql_rows.items():
            if frag in q:
                return 200, json.dumps({"data": rows}).encode()
        return 200, json.dumps({"data": []}).encode()

    return {"oauth_refresh": lambda p, **k: {"access_token": "t",
                                             "instance_url": "https://x"},
            "http_get_json": _get,
            "http_post_json": _post,
            "http_patch_json": lambda u, d, **k: (200, b"{}"),
            "http_delete_json": lambda u, **k: (200, b"{}"),
            "WS": "/tmp/rc-test",
            "jload": lambda p2, default=None: default if default is not None else {},
            "jsave": (lambda p2, o: saved.__setitem__(p2, o))
                     if saved is not None else (lambda p2, o: None)}, calls


def t_hygiene_scan_reports_only_what_it_finds():
    """A check with no rows is not a finding; silence is the healthy case."""
    h, _ = _scan_helpers(coql_rows={
        "Email is null": [{"id": "1", "Last_Name": "A"}]})
    m = load(h)
    out, err = m.zoho_hygiene_scan({}, {})
    assert err is None
    keys = {f["check"] for f in out["findings"]}
    assert "leads_no_email" in keys, keys
    assert "stale_leads" not in keys, keys
    assert out["issues_found"] == len(out["findings"])


def t_hygiene_scan_names_the_command_that_fixes_it():
    """Counts and fix_with stay inline; the query lives in the report file,
    because redact_output eats the date literal out of a COQL string."""
    saved = {}
    h, _ = _scan_helpers(coql_rows={
        "Email is null": [{"id": "1"}, {"id": "2"}]}, saved=saved)
    m = load(h)
    out, _ = m.zoho_hygiene_scan({}, {})
    f = [x for x in out["findings"] if x["check"] == "leads_no_email"][0]
    assert f["fix_with"] == "zoho.plan_update", f
    assert f["count"] == 2, f
    assert "query" not in f, "the query must not travel through the receipt"
    assert out["report_path"].endswith(".json"), out["report_path"]

    report = saved[out["report_path"]]
    rf = [x for x in report["findings"] if x["check"] == "leads_no_email"][0]
    assert "select" in rf["query"].lower(), rf["query"]
    assert rf["sample"], rf


def t_hygiene_scan_finds_records_owned_by_deactivated_users():
    users = [{"id": "9", "full_name": "Departed D", "status": "inactive"},
             {"id": "1", "full_name": "Active A", "status": "active"}]
    h, _ = _scan_helpers(users=users,
                         coql_rows={"Owner in ('9')": [{"id": "5"}]})
    m = load(h)
    out, _ = m.zoho_hygiene_scan({}, {})
    orphaned = [f for f in out["findings"] if f["check"].startswith("orphaned_")]
    assert orphaned, out["findings"]
    assert orphaned[0]["fix_with"] == "zoho.plan_handover", orphaned[0]
    assert "Departed D" in out["deactivated_users"], out["deactivated_users"]
    assert "Active A" not in out["deactivated_users"]


def t_hygiene_scan_reports_a_broken_check_rather_than_zero():
    """Under-reporting silently is worse than admitting a gap."""
    h, _ = _scan_helpers(raise_on_query="Closing_Date")
    m = load(h)
    out, _ = m.zoho_hygiene_scan({}, {})
    keys = {u["check"] for u in out["unavailable"]}
    assert "overdue_deals" in keys, out["unavailable"]
    assert "not counted as zero" in out["summary"], out["summary"]


def t_hygiene_scan_honours_include_and_sample():
    rows = [{"id": str(i)} for i in range(20)]
    saved = {}
    h, _ = _scan_helpers(coql_rows={"Email is null": rows}, saved=saved)
    m = load(h)
    out, _ = m.zoho_hygiene_scan({"include": ["leads_no_email"], "sample": 3}, {})
    assert {f["check"] for f in out["findings"]} == {"leads_no_email"}
    assert out["findings"][0]["count"] == 20
    report = saved[out["report_path"]]
    assert len(report["findings"][0]["sample"]) == 3, report["findings"][0]


def t_hygiene_scan_rejects_a_nonsense_window():
    h, _ = _scan_helpers()
    m = load(h)
    try:
        m.zoho_hygiene_scan({"stale_days": 0}, {})
        assert False
    except RuntimeError as e:
        assert "at least 1" in str(e), e


def t_check_readiness_flags_missing_required_fields():
    fields = [{"api_name": "Last_Name", "system_mandatory": True},
              {"api_name": "Company", "system_mandatory": True},
              {"api_name": "Notes_Field", "system_mandatory": False}]
    record = {"id": "7", "Last_Name": "Ada", "Company": None}
    h, _ = _scan_helpers(fields=fields, record=record)
    m = load(h)
    out, err = m.zoho_check_readiness({"module": "Leads", "record_id": "7"}, {})
    assert err is None
    assert out["ready"] is False, out
    assert out["missing_required"] == ["Company"], out["missing_required"]
    assert "not ready" in out["summary"].lower(), out["summary"]


def t_check_readiness_passes_a_complete_record():
    fields = [{"api_name": "Last_Name", "system_mandatory": True}]
    record = {"id": "7", "Last_Name": "Ada"}
    h, _ = _scan_helpers(fields=fields, record=record)
    m = load(h)
    out, _ = m.zoho_check_readiness({"module": "Leads", "record_id": "7"}, {})
    assert out["ready"] is True, out
    assert out["missing_required"] == [], out


def t_check_readiness_checks_extra_named_fields():
    fields = [{"api_name": "Last_Name", "system_mandatory": True}]
    record = {"id": "7", "Last_Name": "Ada", "Phone": ""}
    h, _ = _scan_helpers(fields=fields, record=record)
    m = load(h)
    out, _ = m.zoho_check_readiness(
        {"module": "Leads", "record_id": "7", "require": ["Phone", "Nope"]}, {})
    assert out["ready"] is False, out
    notes = {x["field"]: x["note"] for x in out["missing_requested"]}
    assert notes["Phone"] == "empty", notes
    assert "not a field" in notes["Nope"], notes


def t_check_readiness_ignores_read_only_required_fields():
    """A read-only field cannot be filled in, so demanding it is noise."""
    fields = [{"api_name": "Created_Time", "system_mandatory": True,
               "read_only": True},
              {"api_name": "Last_Name", "system_mandatory": True}]
    record = {"id": "7", "Last_Name": "Ada"}
    h, _ = _scan_helpers(fields=fields, record=record)
    m = load(h)
    out, _ = m.zoho_check_readiness({"module": "Leads", "record_id": "7"}, {})
    assert out["ready"] is True, out


def t_check_readiness_raises_on_a_missing_record():
    h, _ = _scan_helpers(fields=[], record=None)
    m = load(h)
    try:
        m.zoho_check_readiness({"module": "Leads", "record_id": "404"}, {})
        assert False
    except RuntimeError as e:
        assert "not found" in str(e), e



# ----------------------------------------------------------- scan_changes
def _change_helpers(rows_by_module=None, ledger=None, saved=None, pages=None):
    """COQL serves rows per module name; jload serves a seeded ledger.

    _scan_helpers' jload always returns the default, so a seeded ledger needs
    its own store here.
    """
    rows_by_module = rows_by_module or {}
    store = {"/tmp/rc-test/zoho_ledger.json": ledger} if ledger else {}
    seq = list(pages or [])

    def _post(url, obj, **k):
        q = (obj or {}).get("select_query", "")
        if seq:
            return 200, json.dumps(seq.pop(0)).encode()
        for mod, rows in rows_by_module.items():
            if " from %s " % mod in q:
                return 200, json.dumps({"data": rows}).encode()
        return 200, json.dumps({"data": []}).encode()

    return {"oauth_refresh": lambda p, **k: {"access_token": "t",
                                             "instance_url": "https://x"},
            "http_get_json": lambda u, **k: (200, b'{"data":[]}'),
            "http_post_json": _post,
            "WS": "/tmp/rc-test",
            "jload": lambda p2, default=None: store.get(
                p2, default if default is not None else {}),
            "jsave": (lambda p2, o: saved.__setitem__(p2, o))
                     if saved is not None else (lambda p2, o: None)}


def t_iso_utc_converts_the_half_hour_offset():
    """+05:30 is not hour-aligned; hour arithmetic skews by 30 minutes."""
    m = load(_change_helpers())
    assert m._iso_utc("2026-08-08T16:44:05+05:30") == "2026-08-08T11:14:05Z"
    assert m._iso_utc("2026-08-08T11:14:05Z") == "2026-08-08T11:14:05Z"
    assert m._iso_utc("2026-08-08T06:14:05-05:00") == "2026-08-08T11:14:05Z"


def t_iso_utc_returns_none_rather_than_raising():
    """One unreadable cell must not abort a scan."""
    m = load(_change_helpers())
    for bad in ("", None, "not a date", "2026-08-08 16:44:05", 12345):
        assert m._iso_utc(bad) is None, bad


def t_scan_changes_marks_an_exact_ledger_match_governed():
    ledger = {"chain_start": "0", "entries": [
        {"outcome": "applied", "module": "Leads", "at": "2026-08-08T11:14:05Z",
         "detail": {"written": [
             {"id": "1", "modified_time": "2026-08-08T16:44:05+05:30"}]}}]}
    h = _change_helpers(
        rows_by_module={"Leads": [{"id": "1",
                                   "Modified_Time": "2026-08-08T16:44:05+05:30"}]},
        ledger=ledger)
    m = load(h)
    out, err = m.zoho_scan_changes({"modules": ["Leads"]}, {})
    assert err is None
    assert out["count"] == 1, out
    assert out["changes"][0]["governed"] is True, out
    assert out["ungoverned_count"] == 0, out


def t_scan_changes_reports_a_second_later_edit_as_ungoverned():
    """The point of an exact match: a UI edit right after a module write must
    stay visible instead of hiding behind the approval."""
    ledger = {"chain_start": "0", "entries": [
        {"outcome": "applied", "module": "Leads", "at": "2026-08-08T11:14:05Z",
         "detail": {"written": [
             {"id": "1", "modified_time": "2026-08-08T16:44:05+05:30"}]}}]}
    h = _change_helpers(
        rows_by_module={"Leads": [{"id": "1",
                                   "Modified_Time": "2026-08-08T16:44:06+05:30"}]},
        ledger=ledger)
    m = load(h)
    out, _ = m.zoho_scan_changes({"modules": ["Leads"]}, {})
    assert out["ungoverned_count"] == 1, out
    assert out["changes"][0]["governed"] is False, out


def t_scan_changes_cursor_identifies_a_change_not_a_record():
    """Same id, two edits. Keying on id alone would have the station suppress
    the second as already delivered."""
    h = _change_helpers(rows_by_module={"Leads": [
        {"id": "1", "Modified_Time": "2026-08-08T16:44:05+05:30"},
        {"id": "1", "Modified_Time": "2026-08-08T17:10:00+05:30"}]})
    m = load(h)
    out, _ = m.zoho_scan_changes({"modules": ["Leads"]}, {})
    refs = [c["change_ref"] for c in out["changes"]]
    assert len(set(refs)) == 2, refs


def t_scan_changes_honours_the_seen_set():
    h = _change_helpers(rows_by_module={"Leads": [
        {"id": "1", "Modified_Time": "2026-08-08T16:44:05+05:30"},
        {"id": "2", "Modified_Time": "2026-08-08T17:10:00+05:30"}]})
    m = load(h)
    out, _ = m.zoho_scan_changes(
        {"modules": ["Leads"],
         "exclude_ids": ["Leads:1:2026-08-08T11:14:05Z"]}, {})
    assert out["skipped_already_delivered"] == 1, out
    assert out["count"] == 1, out


def t_scan_changes_surfaces_an_unreadable_timestamp():
    """A row we cannot prove is old must not silently become a row we skip."""
    h = _change_helpers(rows_by_module={"Leads": [
        {"id": "1", "Modified_Time": "sometime last tuesday"}]})
    m = load(h)
    out, _ = m.zoho_scan_changes({"modules": ["Leads"]}, {})
    assert out["count"] == 1, out
    assert out["changes"][0]["governed"] is False, out


def t_scan_changes_counts_unmatchable_ledger_entries():
    """Pre-upgrade entries and merges carry no Modified_Time. They are real
    approvals, so they are counted rather than treated as evidence."""
    ledger = {"chain_start": "0", "entries": [
        {"outcome": "applied", "module": "Leads", "at": "2026-08-01T00:00:00Z",
         "detail": {"records": 3}},
        {"outcome": "applied", "module": "Leads", "at": "2026-08-02T00:00:00Z",
         "detail": {"irreversible": True}},
        {"outcome": "refused", "module": "Leads", "at": "2026-08-03T00:00:00Z",
         "detail": {}}]}
    m = load(_change_helpers(ledger=ledger))
    out, _ = m.zoho_scan_changes({"modules": ["Leads"]}, {})
    assert out["unmatchable_ledger_entries"] == 2, out
    assert out["ledger_covers_from"] == "2026-08-01T00:00:00Z", out


def t_scan_changes_refuses_an_unreadable_since():
    """The station injects this; scanning from the beginning of time would
    look like success while spending the day's API budget."""
    m = load(_change_helpers())
    try:
        m.zoho_scan_changes({"modules": ["Leads"], "since": "yesterday"}, {})
        assert False, "expected a refusal"
    except RuntimeError as e:
        assert "yesterday" in str(e), e


def t_scan_changes_records_go_to_the_file_not_the_receipt():
    """redact_output scrubs ids out of a receipt, so detail has to be a path."""
    saved = {}
    h = _change_helpers(rows_by_module={"Leads": [
        {"id": "1", "Modified_Time": "2026-08-08T16:44:05+05:30",
         "Modified_By": {"name": "Indra S", "id": "u1"}}]}, saved=saved)
    m = load(h)
    out, _ = m.zoho_scan_changes({"modules": ["Leads"]}, {})
    assert "ungoverned" not in out, "records must not travel through a receipt"
    report = saved[out["report_path"]]
    assert report["ungoverned"][0]["id"] == "1", report
    assert report["ungoverned"][0]["modified_by"] == "Indra S", report


def t_scan_changes_truncates_rather_than_stepping_over_rows():
    """Hitting the cap with more waiting must set truncated, or the station
    advances its watermark past rows that were never returned."""
    pages = [{"data": [{"id": str(i),
                        "Modified_Time": "2026-08-08T16:44:05+05:30"}
                       for i in range(200)], "info": {"more_records": True}}]
    m = load(_change_helpers(pages=pages))
    out, _ = m.zoho_scan_changes({"modules": ["Leads"], "limit": 200}, {})
    assert out["truncated"] is True, out
    assert out["count"] == 200, out["count"]


def t_scan_changes_orders_ascending():
    """ASC is mandatory: the cap truncates, and the station advances to the
    newest row returned. Any other order strands older rows behind the mark."""
    seen = {}

    def _post(url, obj, **k):
        seen["q"] = (obj or {}).get("select_query", "")
        return 200, json.dumps({"data": []}).encode()

    h = _change_helpers()
    h["http_post_json"] = _post
    m = load(h)
    m.zoho_scan_changes({"modules": ["Leads"]}, {})
    assert "order by Modified_Time asc" in seen["q"], seen["q"]



# ------------------------------------------------- unresolved writes (0.9.0)
#
# A write that got no verdict is a third outcome, not a failed one. Everything
# below checks the module keeps what it already knew instead of discarding it.

_UNRESOLVED_BODY = b'{"message":"boom"}'


def _failing_put(status=500):
    """A PUT that answers 5xx, which _call must treat as unresolved."""
    return lambda url, obj, hdrs: (status, _UNRESOLVED_BODY)


def _unresolved_update_env():
    """plan_update stored, then an apply whose PUT comes back 500."""
    matched = [{"id": "1"}]
    rows = [{"id": "1", "S": "old", "Modified_Time": "t1"}]
    h, _calls, store = _ledger_helpers(matched, rows)
    m = load(h)
    m._put = _failing_put()
    args = {"module": "Leads", "query": "select id from Leads where x = 1",
            "changes": {"S": "new"}}
    m.zoho_plan_update(args, {})
    return m, args, store


def _entries(store):
    """Ledger entries, or none if the file was never written - "no entry at
    all" is the assertion in several of these tests."""
    return (store.get("/tmp/rc-test/zoho_ledger.json") or {}).get("entries", [])


def t_unresolved_write_is_recorded_in_the_ledger():
    m, args, store = _unresolved_update_env()
    try:
        m.zoho_apply_update(args, {})
        assert False, "should have raised"
    except m.ZohoUnresolvedWrite:
        pass

    entries = _entries(store)
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["outcome"] == "unresolved", entry
    assert entry["command"] == "apply_update", entry
    detail = entry["detail"]
    assert detail["intent"] == {"S": "new"}, detail
    assert detail["fields"] == ["S"], detail
    assert detail["verdict_basis"] == "value", detail
    assert detail["attempted_at"].endswith("Z"), detail
    assert "not retried" in detail["reason"].lower(), detail
    assert detail["targets"] == [
        {"id": "1", "before": {"S": "old"}, "before_modified_time": "t1"}
    ], detail["targets"]


def t_unresolved_write_still_raises():
    """The command must not return a normal result. A caller that got one
    would report a change it has no idea happened."""
    m, args, _store = _unresolved_update_env()
    returned = []
    try:
        returned.append(m.zoho_apply_update(args, {}))
    except m.ZohoUnresolvedWrite:
        pass
    assert not returned, returned


def t_unresolved_error_names_the_ledger_entry():
    m, args, store = _unresolved_update_env()
    try:
        m.zoho_apply_update(args, {})
        assert False
    except m.ZohoUnresolvedWrite as e:
        seq = _entries(store)[0]["seq"]
        assert "ledger entry %d" % seq in str(e), e
        # the original diagnosis has to survive the wrapping
        assert "may have been applied" in str(e), e


def t_unresolved_is_a_runtime_error():
    """Every existing caller catches RuntimeError. None of them may change
    behaviour because a subclass was introduced."""
    m, args, _store = _unresolved_update_env()
    caught = None
    try:
        m.zoho_apply_update(args, {})
    except RuntimeError as e:
        caught = e
    assert caught is not None, "plain except RuntimeError missed it"
    assert isinstance(caught, m.ZohoUnresolvedWrite), type(caught)


def t_a_settled_4xx_is_not_unresolved():
    """A 400 is Zoho's answer, not the absence of one: plain RuntimeError and
    nothing in the ledger."""
    matched = [{"id": "1"}]
    rows = [{"id": "1", "S": "old", "Modified_Time": "t1"}]
    h, _calls, store = _ledger_helpers(matched, rows)
    m = load(h)
    m._put = _failing_put(400)
    args = {"module": "Leads", "query": "select id from Leads where x = 1",
            "changes": {"S": "new"}}
    m.zoho_plan_update(args, {})
    try:
        m.zoho_apply_update(args, {})
        assert False, "should have raised"
    except RuntimeError as e:
        assert not isinstance(e, m.ZohoUnresolvedWrite), e
    assert _entries(store) == [], _entries(store)


def t_a_failed_read_is_not_recorded_as_a_write():
    """COQL reads are POSTs, and POST is non-idempotent, so a read that gets no
    status raises ZohoUnresolvedWrite too. The apply must not file that as an
    attempted write - only the write call itself is inside the try."""
    matched = [{"id": "1"}]
    rows = [{"id": "1", "S": "old", "Modified_Time": "t1"}]
    h, _calls, store = _ledger_helpers(matched, rows)
    m = load(h)
    args = {"module": "Leads", "query": "select id from Leads where x = 1",
            "changes": {"S": "new"}}
    m.zoho_plan_update(args, {})

    wrote = []
    m._put = lambda url, obj, hdrs: (wrote.append(url), (200, b"{}"))[1]

    # Fail the id lookup specifically - the read immediately before the write,
    # and the one a too-wide try would swallow. The query-matching read earlier
    # in the command is served normally, so this is not just "any read fails".
    inner = h["http_post_json"]

    def _post(url, obj, **k):
        if " in (" in (obj or {}).get("select_query", ""):
            raise OSError("simulated network failure")
        return inner(url, obj, **k)

    h["http_post_json"] = _post
    try:
        m.zoho_apply_update(args, {})
        assert False, "should have raised"
    except RuntimeError:
        pass
    assert not wrote, "a write was issued after the pre-flight read failed"
    assert _entries(store) == [], _entries(store)


def t_unresolved_entry_keeps_the_chain_verifiable():
    m, args, store = _unresolved_update_env()
    try:
        m.zoho_apply_update(args, {})
    except m.ZohoUnresolvedWrite:
        pass
    intact, checked, bad = m._ledger_verify()
    assert intact and checked == 1 and bad is None, (intact, checked, bad)


def t_verify_ledger_counts_unresolved():
    h, _, _ = _ledger_helpers([], [])
    m = load(h)
    m._ledger_append(m._ledger_note("applied", "apply_update", "Leads", "a", {}))
    m._ledger_append(m._ledger_note("refused", "apply_update", "Leads", "b", {}))
    m._ledger_append(m._ledger_note("unresolved", "apply_update", "Leads", "c", {}))
    out, err = m.zoho_verify_ledger({}, {})
    assert err is None
    assert out["applied"] == 1 and out["refused"] == 1, out
    assert out["unresolved"] == 1, out
    assert "1 unresolved" in out["summary"], out["summary"]
    assert "never returned a verdict" in out["summary"], out["summary"]
    assert "unresolved" in out["covers"], out["covers"]


def t_audit_pack_accepts_the_unresolved_filter():
    h, _, _ = _ledger_helpers([], [])
    m = load(h)
    m._ledger_append(m._ledger_note("applied", "apply_update", "Leads", "a",
                                    {"records": 3}))
    m._ledger_append(m._ledger_note("unresolved", "apply_update", "Leads", "b",
                                    {"records": 2}))
    out, _ = m.zoho_audit_pack({"outcome": "unresolved"}, {})
    assert out["entries"] == 1 and out["unresolved"] == 1, out
    # an unresolved entry's records are not known to have changed
    assert out["records_changed"] == 0, out
    both, _ = m.zoho_audit_pack({}, {})
    assert both["records_changed"] == 3, both
    assert both["unresolved"] == 1, both
    try:
        m.zoho_audit_pack({"outcome": "maybe"}, {})
        assert False
    except RuntimeError as e:
        assert "unresolved" in str(e), e


def t_unresolved_delete_records_an_existence_basis():
    """A delete sets no value, so the only later question is whether the
    record is gone. The entry has to say that rather than imply a comparison."""
    seq = [([{"id": "1"}], False), ([{"id": "1", "Modified_Time": "t1"}], False),
           ([{"id": "1"}], False), ([{"id": "1", "Modified_Time": "t1"}], False)]
    store = {}

    def _post(url, obj, **k):
        rows, more = seq.pop(0) if seq else ([], False)
        return 200, json.dumps({"data": rows, "info": {"more_records": more}}).encode()

    h = {"oauth_refresh": lambda p, **k: {"access_token": "t", "instance_url": "https://x"},
         "http_get_json": lambda u, **k: (200, b"{}"), "http_post_json": _post,
         "http_delete_json": lambda u, **k: (500, _UNRESOLVED_BODY),
         "WS": "/tmp/rc-test",
         "jload": lambda path, default=None: store.get(path, default if default is not None else {}),
         "jsave": lambda path, obj: store.__setitem__(path, obj)}
    m = load(h)
    args = {"module": "Leads", "query": "select id from Leads where x = 1"}
    m.zoho_plan_delete(args, {})
    try:
        m.zoho_apply_delete(args, {})
        assert False, "should have raised"
    except m.ZohoUnresolvedWrite:
        pass

    entry = _entries(store)[0]
    assert entry["outcome"] == "unresolved", entry
    assert entry["command"] == "apply_delete", entry
    assert entry["detail"]["verdict_basis"] == "existence", entry["detail"]
    assert entry["detail"]["intent"] is None, entry["detail"]
    assert entry["detail"]["targets"][0]["before_modified_time"] == "t1", entry


def t_unresolved_rollback_records_intent_per_target():
    """Each record is restored to its own prior values, so a single entry-level
    intent would be wrong for all but one of them."""
    matched = [{"id": "1"}]
    h, _, store = _ledger_helpers(matched, [{"id": "1", "S": "old",
                                             "Modified_Time": "t1"}])
    m = _load_put(h)
    args = {"module": "Leads", "query": "select id from Leads where x = 1",
            "changes": {"S": "new"}}
    m.zoho_plan_update(args, {})
    m.zoho_apply_update(args, {})

    after = [{"id": "1", "S": "new", "Modified_Time": "t2"}]
    h2, _, _ = _ledger_helpers(matched, after, store=store)
    m2 = _load_put(h2)
    m2.zoho_plan_rollback(args, {})

    h3, _, _ = _ledger_helpers(matched, after, store=store)
    m3 = load(h3)
    m3._put = _failing_put()
    try:
        m3.zoho_apply_rollback(args, {})
        assert False, "should have raised"
    except m3.ZohoUnresolvedWrite:
        pass

    entry = _entries(store)[-1]
    assert entry["outcome"] == "unresolved", entry
    assert entry["command"] == "apply_rollback", entry
    detail = entry["detail"]
    assert detail["intent"] is None, detail
    target = detail["targets"][0]
    assert target["intent"] == {"S": "old"}, target
    assert target["before"] == {"S": "new"}, target
    assert target["before_modified_time"] == "t2", target


def t_unresolved_upsert_names_the_batch_and_its_gaps():
    """250 records is three calls. If the second one gets no verdict, the
    entry has to say which batch, what already committed, and that the creates
    cannot be reconciled at all."""
    recs = [{"Email": "u%d@x.com" % i} for i in range(250)]
    args = {"module": "Leads", "records": recs,
            "duplicate_check_fields": ["Email"]}
    existing = [{"id": "1", "Email": "u0@x.com", "Modified_Time": "t1"}]
    h, _calls, store = _upsert_helpers(existing)
    m = load(h)
    m.zoho_plan_upsert(args, {})

    inner = h["http_post_json"]
    seen = {"n": 0}

    def _post(url, obj, **k):
        if url.endswith("/upsert"):
            seen["n"] += 1
            if seen["n"] == 2:
                return 500, _UNRESOLVED_BODY
        return inner(url, obj, **k)

    h["http_post_json"] = _post
    try:
        m.zoho_apply_upsert(args, {})
        assert False, "should have raised"
    except m.ZohoUnresolvedWrite:
        pass

    entry = _entries(store)[-1]
    assert entry["outcome"] == "unresolved", entry
    assert entry["command"] == "apply_upsert", entry
    detail = entry["detail"]
    assert detail["batch_index"] == 1, detail
    assert detail["batch_size"] == 100, detail
    assert detail["succeeded_before_failure"] == 100, detail
    assert len(detail["written_before_failure"]) == 100, detail
    # the plan only ever fingerprinted Modified_Time on existing records
    assert detail["verdict_basis"] == "modified_time", detail
    assert detail["creates_unreconcilable"] == 249, detail
    assert detail["targets"] == [
        {"id": "1", "before": {"Modified_Time": "t1"},
         "before_modified_time": "t1"}], detail["targets"]


def t_unresolved_handover_records_the_owner_before_the_write():
    """The ownership scan is the only chance to capture Modified_Time. Read
    after a write that got no verdict it would prove nothing."""
    plan_scans = [[{"id": "1", "Modified_Time": "t1"}], [], [], [], []]
    apply_scans = [[{"id": "1", "Modified_Time": "t1"}], [], [], []]
    m, _calls = _handover_env(
        USERS, plan_scans + apply_scans,
        put=lambda u, o, h: (500, _UNRESOLVED_BODY))
    args = {"from_user": "100", "to_user": "200"}
    m.zoho_plan_handover(args, {})
    try:
        m.zoho_apply_handover(args, {})
        assert False, "should have raised"
    except m.ZohoUnresolvedWrite:
        pass

    entry = m._ledger_load()["entries"][-1]
    assert entry["outcome"] == "unresolved", entry
    assert entry["command"] == "apply_handover", entry
    # the module written when it failed, not "handover" - the ledger is joined
    # to records on module and id
    assert entry["module"] == "Leads", entry
    detail = entry["detail"]
    assert detail["intent"] == {"Owner": {"id": "200"}}, detail
    assert detail["batch_index"] == 0, detail
    assert detail["moved_before_failure"] == 0, detail
    assert detail["targets"] == [
        {"id": "1", "before": {"Owner": "100"},
         "before_modified_time": "t1"}], detail["targets"]


def t_handover_scan_still_reports_counts_with_timestamps():
    """Adding Modified_Time to the ownership query must not change the split."""
    m, calls = _handover_env(USERS, [[{"id": "1", "Modified_Time": "t1"}],
                                     [{"id": "2", "Modified_Time": "t2"}],
                                     [], [], [{"id": "2"}, {"id": "3"}]])
    out, _ = m.zoho_plan_handover({"from_user": "100", "to_user": "200"}, {})
    assert out["total"] == 2, out
    leads_q = [c for c in calls if "from Leads" in c][0]
    assert "Modified_Time" in leads_q, leads_q


def t_unresolved_merge_stops_before_the_next_loser():
    """After a merge with no verdict the master's state is unknown. Merging
    the next loser into it would be a confident wrong answer."""
    records = {"100": {"id": "100", "Modified_Time": "t1"},
               "200": {"id": "200", "Modified_Time": "t1"},
               "300": {"id": "300", "Modified_Time": "t1"}}
    args = {"module": "Leads", "master_id": "100", "loser_ids": ["200", "300"]}
    h, calls, store = _merge_helpers(records)
    m = load(h)
    m.zoho_plan_merge(args, {})

    inner = h["http_post_json"]

    def _post(url, obj, **k):
        if url.endswith("/actions/merge"):
            calls.append(("POST", url))
            return 500, _UNRESOLVED_BODY
        return inner(url, obj, **k)

    h["http_post_json"] = _post
    try:
        m.zoho_apply_merge(args, {})
        assert False, "should have raised"
    except m.ZohoUnresolvedWrite:
        pass

    merge_calls = [u for meth, u in calls if u.endswith("/actions/merge")]
    assert len(merge_calls) == 1, merge_calls

    entries = _entries(store)
    assert [e["outcome"] for e in entries] == ["applied", "unresolved"], entries
    detail = entries[-1]["detail"]
    assert detail["verdict_basis"] == "existence", detail
    assert detail["attempted"] == "200", detail
    assert detail["not_attempted"] == ["300"], detail
    assert detail["merged_before_failure"] == [], detail
    # points back at the archive, which is the only readable copy of the loser
    assert detail["archive_seq"] == entries[0]["seq"], detail
    assert entries[0]["detail"]["archive"][0]["id"] == "200", entries[0]




# ------------------------------------------------- reconcile_writes (0.9.0)
#
# Three verdict bases, three different questions. Most of what follows is
# proving the branch is real rather than one table with special cases.

_T0 = "2026-01-01T00:00:00+05:30"      # before_modified_time on every entry
_T1 = "2026-01-02T00:00:00+05:30"      # the record moved
_T0_UTC = "2025-12-31T18:30:00Z"       # the same instant, spelled differently


def _recon_helpers(current_rows, store=None):
    """COQL id-lookups return whichever of `current_rows` were asked for.

    A row absent from `current_rows` is a record that no longer reads back,
    which is the input for every "deleted since" branch.
    """
    import re
    calls = []
    store = store if store is not None else {}

    def _post(url, obj, **k):
        q = (obj or {}).get("select_query", "")
        calls.append(q)
        found = re.search(r"in \(([^)]*)\)", q)
        wanted = {x.strip() for x in found.group(1).split(",")} if found else set()
        rows = [r for r in current_rows if str(r.get("id")) in wanted]
        return 200, json.dumps({"data": rows}).encode()

    return {"oauth_refresh": lambda p, **k: {"access_token": "t",
                                             "instance_url": "https://x"},
            "http_get_json": lambda u, **k: (200, b"{}"),
            "http_post_json": _post,
            "http_delete_json": lambda u, **k: (200, b"{}"),
            "WS": "/tmp/rc-test",
            "jload": lambda path, default=None: store.get(path, default if default is not None else {}),
            "jsave": lambda path, obj: store.__setitem__(path, obj)}, calls, store


def _seed(m, detail, command="apply_update", module="Leads"):
    """Put one unresolved entry in the ledger and return it."""
    base = {"reason": "Zoho returned HTTP 500 on PUT Leads.",
            "attempted_at": "2026-01-01T12:00:00Z"}
    base.update(detail)
    return m._ledger_append(m._ledger_note("unresolved", command, module,
                                           "key-1", base))


def _value_detail(**over):
    detail = {"verdict_basis": "value", "intent": {"S": "new"}, "fields": ["S"],
              "targets": [{"id": "1", "before": {"S": "old"},
                           "before_modified_time": _T0}]}
    detail.update(over)
    return detail


def _reconcile(current_rows, detail, command="apply_update", inputs=None):
    h, _calls, store = _recon_helpers(current_rows)
    m = load(h)
    _seed(m, detail, command=command)
    out, err = m.zoho_reconcile_writes(inputs or {}, {})
    assert err is None, err
    return m, out, store


def _file_records(out, store):
    return store[out["report_path"]]["entries"][0]["records"]


# --------------------------------------------------- verdict_basis: "value"
def t_recon_value_landed():
    _m, out, store = _reconcile(
        [{"id": "1", "S": "new", "Modified_Time": _T1}], _value_detail())
    assert (out["landed"], out["not_landed"], out["unknown"]) == (1, 0, 0), out
    assert _file_records(out, store)[0]["verdict"] == "landed", out
    # a judgement, never a confirmation
    assert "consistent with having landed" in out["summary"], out["summary"]
    assert "not confirmations" in out["summary"], out["summary"]


def t_recon_value_not_landed():
    _m, out, _s = _reconcile(
        [{"id": "1", "S": "old", "Modified_Time": _T0}], _value_detail())
    assert (out["landed"], out["not_landed"], out["unknown"]) == (0, 1, 0), out


def t_recon_value_reverted_is_unknown():
    """Value back where it started but the record moved: the write may have
    landed and been reverted, or missed while someone else edited."""
    _m, out, store = _reconcile(
        [{"id": "1", "S": "old", "Modified_Time": _T1}], _value_detail())
    assert (out["landed"], out["not_landed"], out["unknown"]) == (0, 0, 1), out
    assert "reverted" in _file_records(out, store)[0]["note"], out


def t_recon_value_third_party_edit_is_unknown():
    _m, out, _s = _reconcile(
        [{"id": "1", "S": "somebody else", "Modified_Time": _T1}],
        _value_detail())
    assert out["unknown"] == 1 and out["landed"] == 0, out


def t_recon_value_missing_record_is_unknown():
    """The half of the branch proof: on a value entry a vanished record tells
    you nothing. Compare with the existence test below."""
    _m, out, store = _reconcile([], _value_detail())
    assert (out["landed"], out["not_landed"], out["unknown"]) == (0, 0, 1), out
    assert "no longer readable" in _file_records(out, store)[0]["note"], out


def t_recon_value_ignores_timezone_spelling():
    """+05:30 and the same instant in Z must not read as a change."""
    _m, out, _s = _reconcile(
        [{"id": "1", "S": "old", "Modified_Time": _T0_UTC}], _value_detail())
    assert out["not_landed"] == 1, out


def t_recon_value_no_op_write_is_unknown():
    """Setting a field to the value it already held: landing and not landing
    produce identical records, so neither table row may win by accident."""
    _m, out, store = _reconcile(
        [{"id": "1", "S": "same", "Modified_Time": _T1}],
        _value_detail(intent={"S": "same"},
                      targets=[{"id": "1", "before": {"S": "same"},
                                "before_modified_time": _T0}]))
    assert out["unknown"] == 1 and out["landed"] == 0, out
    assert "already held" in _file_records(out, store)[0]["note"], out


def t_recon_resolves_intent_per_target():
    """apply_rollback restores each record to its own prior values, so the
    entry-level intent is null and each target carries its own."""
    detail = {"verdict_basis": "value", "intent": None, "fields": ["S"],
              "targets": [{"id": "1", "intent": {"S": "a"},
                           "before": {"S": "x"}, "before_modified_time": _T0},
                          {"id": "2", "intent": {"S": "b"},
                           "before": {"S": "y"}, "before_modified_time": _T0}]}
    _m, out, store = _reconcile(
        [{"id": "1", "S": "a", "Modified_Time": _T1},
         {"id": "2", "S": "y", "Modified_Time": _T0}],
        detail, command="apply_rollback")
    verdicts = {r["id"]: r["verdict"] for r in _file_records(out, store)}
    assert verdicts == {"1": "landed", "2": "not_landed"}, verdicts


def t_recon_owner_lookup_compares_on_id():
    """Owner is written as {"id": x} and read back as {"name":..., "id": x}.
    Comparing the raw dicts would make every handover permanently unknown."""
    detail = {"verdict_basis": "value", "intent": {"Owner": {"id": "200"}},
              "fields": ["Owner"],
              "targets": [{"id": "1", "before": {"Owner": "100"},
                           "before_modified_time": _T0}]}
    _m, out, _s = _reconcile(
        [{"id": "1", "Owner": {"name": "Bob Taker", "id": "200"},
          "Modified_Time": _T1}], detail, command="apply_handover")
    assert out["landed"] == 1, out


# ----------------------------------------------- verdict_basis: "existence"
def _existence_detail(**over):
    detail = {"verdict_basis": "existence", "intent": None,
              "fields": ["Modified_Time"],
              "targets": [{"id": "1", "before": {"Modified_Time": _T0},
                           "before_modified_time": _T0}]}
    detail.update(over)
    return detail


def t_recon_existence_missing_record_is_landed():
    """The other half of the branch proof: on a delete, gone IS the signal.
    The same input reads `unknown` on a value entry."""
    _m, out, store = _reconcile([], _existence_detail(),
                                command="apply_delete")
    assert (out["landed"], out["not_landed"], out["unknown"]) == (1, 0, 0), out
    assert "what was asked" in _file_records(out, store)[0]["note"], out


def t_recon_existence_present_and_untouched_is_not_landed():
    _m, out, _s = _reconcile([{"id": "1", "Modified_Time": _T0}],
                             _existence_detail(), command="apply_delete")
    assert out["not_landed"] == 1, out


def t_recon_existence_present_and_moved_is_unknown():
    _m, out, _s = _reconcile([{"id": "1", "Modified_Time": _T1}],
                             _existence_detail(), command="apply_delete")
    assert out["unknown"] == 1, out


# ------------------------------------------- verdict_basis: "modified_time"
def _mtime_detail(**over):
    detail = {"verdict_basis": "modified_time", "intent": None,
              "fields": ["Email"], "creates_unreconcilable": 4,
              "targets": [{"id": "1", "before": {"Modified_Time": _T0},
                           "before_modified_time": _T0}]}
    detail.update(over)
    return detail


def t_recon_modified_time_never_lands():
    """apply_upsert never read the values it was overwriting, so movement is
    visible and direction is not. No input may produce `landed`."""
    for rows in ([{"id": "1", "Email": "a@x.com", "Modified_Time": _T1}],
                 [{"id": "1", "Email": "a@x.com", "Modified_Time": _T0}],
                 []):
        _m, out, _s = _reconcile(rows, _mtime_detail(), command="apply_upsert")
        assert out["landed"] == 0, (rows, out)
    assert "can never read as landed" in out["covers"], out["covers"]


def t_recon_modified_time_untouched_is_not_landed():
    _m, out, _s = _reconcile([{"id": "1", "Modified_Time": _T0}],
                             _mtime_detail(), command="apply_upsert")
    assert out["not_landed"] == 1, out


def t_recon_modified_time_moved_is_unknown():
    _m, out, store = _reconcile([{"id": "1", "Modified_Time": _T1}],
                                _mtime_detail(), command="apply_upsert")
    assert out["unknown"] == 1, out
    assert "never recorded" in _file_records(out, store)[0]["note"], out
    # the created half is surfaced, not silently dropped
    assert store[out["report_path"]]["entries"][0]["creates_unreconcilable"] == 4


# ------------------------------------------------------------ other branches
def t_recon_unrecognised_basis_is_unknown():
    """Guessing a default from the command name is how a reconciler ends up
    confident about a record it has no evidence for."""
    _m, out, store = _reconcile([{"id": "1", "S": "new", "Modified_Time": _T1}],
                                _value_detail(verdict_basis="sideways"))
    assert out["unknown"] == 1 and out["landed"] == 0, out
    assert "unrecognised verdict_basis" in _file_records(out, store)[0]["note"]


def t_recon_merge_is_existence_only_and_never_lands():
    detail = {"verdict_basis": "existence", "intent": None,
              "master_id": "100", "archive_seq": 1,
              "targets": [{"id": "100", "role": "master",
                           "before": {"Modified_Time": _T0},
                           "before_modified_time": _T0},
                          {"id": "200", "role": "loser",
                           "before": {"Modified_Time": _T0},
                           "before_modified_time": _T0}]}
    # master still there, loser gone: exactly what a completed merge looks like
    _m, out, store = _reconcile([{"id": "100", "Modified_Time": _T0}],
                                detail, command="apply_merge")
    assert out["landed"] == 0, out
    assert out["unknown"] == 2, out
    report = store[out["report_path"]]["entries"][0]
    assert report["merge_state"] == "losers_absent", report
    assert "does not prove one" in report["records"][0]["note"], report


def t_recon_merge_master_gone_is_indeterminate():
    detail = {"verdict_basis": "existence", "master_id": "100",
              "targets": [{"id": "100", "role": "master",
                           "before": {"Modified_Time": _T0},
                           "before_modified_time": _T0},
                          {"id": "200", "role": "loser",
                           "before": {"Modified_Time": _T0},
                           "before_modified_time": _T0}]}
    _m, out, store = _reconcile([], detail, command="apply_merge")
    assert store[out["report_path"]]["entries"][0]["merge_state"] == "indeterminate"
    assert out["landed"] == 0, out


def t_recon_pre_version_entry_is_reported_separately():
    """No before_modified_time means no baseline. Judging it by value alone is
    a materially weaker test and would make the headline counts mean two
    different things."""
    _m, out, store = _reconcile(
        [{"id": "1", "S": "new", "Modified_Time": _T1}],
        _value_detail(targets=[{"id": "1", "before": {"S": "old"}}]))
    assert out["unreconcilable"] == 1, out
    assert (out["landed"], out["not_landed"], out["unknown"]) == (0, 0, 0), out
    assert out["records_checked"] == 0, out
    assert "no baseline" in _file_records(out, store)[0]["note"], out
    assert "excluded from the counts" in out["summary"], out["summary"]


def t_recon_does_not_readjudicate_committed_records():
    """A batched write records what it confirmed before contact was lost.
    Folding that certainty back into the counts would launder it as
    inference."""
    detail = _mtime_detail(
        written_before_failure=[{"id": "1", "modified_time": _T0}],
        targets=[{"id": "1", "before": {"Modified_Time": _T0},
                  "before_modified_time": _T0},
                 {"id": "2", "before": {"Modified_Time": _T0},
                  "before_modified_time": _T0}])
    _m, out, store = _reconcile([{"id": "1", "Modified_Time": _T1},
                                 {"id": "2", "Modified_Time": _T0}],
                                detail, command="apply_upsert")
    assert out["already_committed"] == 1, out
    assert out["records_checked"] == 1, out
    assert out["not_landed"] == 1, out
    assert [r["id"] for r in _file_records(out, store)] == ["2"], out


def t_recon_batch_is_judged_per_record():
    """The case an operator cannot work out by hand: one entry, mixed fates."""
    detail = _value_detail(targets=[
        {"id": "1", "before": {"S": "old"}, "before_modified_time": _T0},
        {"id": "2", "before": {"S": "old"}, "before_modified_time": _T0},
        {"id": "3", "before": {"S": "old"}, "before_modified_time": _T0}])
    _m, out, store = _reconcile(
        [{"id": "1", "S": "new", "Modified_Time": _T1},
         {"id": "2", "S": "old", "Modified_Time": _T0},
         {"id": "3", "S": "wandered off", "Modified_Time": _T1}], detail)
    assert (out["landed"], out["not_landed"], out["unknown"]) == (1, 1, 1), out
    assert out["records_checked"] == 3, out


def t_recon_read_failure_is_not_a_deleted_record():
    """A COQL failure and a deleted record are different facts. Confusing them
    would manufacture a `landed` verdict out of a network problem."""
    h, _calls, store = _recon_helpers([])
    m = load(h)
    _seed(m, _existence_detail(), command="apply_delete")
    h["http_post_json"] = lambda u, o, **k: (_ for _ in ()).throw(
        RuntimeError("coql exploded"))
    out, _err = m.zoho_reconcile_writes({}, {})
    assert out["landed"] == 0 and out["unknown"] == 1, out
    assert store[out["report_path"]]["entries"][0]["read_error"], out


# ---------------------------------------------------------- recording, scope
def t_recon_records_nothing_by_default():
    h, _calls, store = _recon_helpers([{"id": "1", "S": "new",
                                        "Modified_Time": _T1}])
    m = load(h)
    _seed(m, _value_detail())
    before = len(store["/tmp/rc-test/zoho_ledger.json"]["entries"])
    out, _err = m.zoho_reconcile_writes({}, {})
    after = store["/tmp/rc-test/zoho_ledger.json"]["entries"]
    assert len(after) == before == 1, after
    assert out["recorded"] is False, out
    assert "re-run with record_outcome" in out["summary"], out["summary"]


def t_recon_record_outcome_appends_a_verifiable_entry():
    h, _calls, store = _recon_helpers([{"id": "1", "S": "new",
                                        "Modified_Time": _T1}])
    m = load(h)
    _seed(m, _value_detail())
    out, _err = m.zoho_reconcile_writes({"record_outcome": True}, {})
    assert out["recorded"] is True, out
    entries = store["/tmp/rc-test/zoho_ledger.json"]["entries"]
    assert [e["outcome"] for e in entries] == ["unresolved", "reconciled"], entries
    detail = entries[1]["detail"]
    assert detail["resolves_seq"] == 1 and detail["resolved"] is True, detail
    assert detail["verdicts"] == [{"id": "1", "verdict": "landed"}], detail
    intact, checked, bad = m._ledger_verify()
    assert intact and checked == 2 and bad is None, (intact, checked, bad)


def t_recon_all_unknown_entry_stays_open():
    """Nothing was determined. Marking it resolved would be the fake-green
    this module exists to avoid."""
    h, _calls, store = _recon_helpers([{"id": "1", "S": "elsewhere",
                                        "Modified_Time": _T1}])
    m = load(h)
    _seed(m, _value_detail())
    out, _err = m.zoho_reconcile_writes({"record_outcome": True}, {})
    assert out["still_open"] == [1], out
    assert "remains open" in out["summary"], out["summary"]
    entries = store["/tmp/rc-test/zoho_ledger.json"]["entries"]
    assert entries[1]["detail"]["resolved"] is False, entries[1]

    # and a second run still finds it, because the look settled nothing
    h2, _c2, _s2 = _recon_helpers([{"id": "1", "S": "elsewhere",
                                    "Modified_Time": _T1}], store=store)
    m2 = load(h2)
    again, _e = m2.zoho_reconcile_writes({}, {})
    assert again["entries_checked"] == 1, again


def t_recon_a_resolved_entry_is_not_checked_again():
    h, _calls, store = _recon_helpers([{"id": "1", "S": "new",
                                        "Modified_Time": _T1}])
    m = load(h)
    _seed(m, _value_detail())
    m.zoho_reconcile_writes({"record_outcome": True}, {})
    h2, _c2, _s2 = _recon_helpers([{"id": "1", "S": "new",
                                    "Modified_Time": _T1}], store=store)
    m2 = load(h2)
    out, _err = m2.zoho_reconcile_writes({}, {})
    assert out["entries_checked"] == 0, out
    assert "Nothing this module attempted" in out["summary"], out["summary"]


def t_recon_ledger_seq_selects_one_entry():
    h, _calls, store = _recon_helpers([{"id": "1", "S": "new",
                                        "Modified_Time": _T1}])
    m = load(h)
    _seed(m, _value_detail())
    _seed(m, _value_detail())
    out, _err = m.zoho_reconcile_writes({"ledger_seq": 2}, {})
    assert out["entries_checked"] == 1, out
    assert store[out["report_path"]]["entries"][0]["ledger_seq"] == 2, out


def t_recon_ledger_seq_must_name_an_open_unresolved_entry():
    h, _calls, _store = _recon_helpers([])
    m = load(h)
    m._ledger_append(m._ledger_note("applied", "apply_update", "Leads", "k", {}))
    try:
        m.zoho_reconcile_writes({"ledger_seq": 1}, {})
        assert False, "should have raised"
    except RuntimeError as e:
        assert "not an unresolved write" in str(e), e


def t_recon_ids_go_to_the_file_not_the_receipt():
    """redact_output strips ids out of a receipt, so anything actionable has
    to be a path - the same rule audit_pack and hygiene_scan follow."""
    _m, out, store = _reconcile([{"id": "1", "S": "new", "Modified_Time": _T1}],
                               _value_detail())
    # The field NAME appears in the limits sentence, which is the point of
    # that sentence. What must not appear is a record id or a per-record list.
    blob = json.dumps(out)
    assert '"1"' not in blob, out
    assert "records" not in out and "verdicts" not in out, out
    assert out["report_path"] in store, list(store)
    assert _file_records(out, store)[0]["id"] == "1", out


def t_recon_writes_nothing_to_zoho():
    """mode: read. Every call it makes must be a COQL select."""
    h, calls, _store = _recon_helpers([{"id": "1", "S": "new",
                                        "Modified_Time": _T1}])
    m = load(h)
    m._put = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("reconcile_writes issued a PUT"))
    _seed(m, _value_detail())
    m.zoho_reconcile_writes({"record_outcome": True}, {})
    assert calls, "no read was made"
    for q in calls:
        assert q.lower().lstrip().startswith("select"), q


def t_verify_ledger_counts_reconciled():
    h, _, _ = _ledger_helpers([], [])
    m = load(h)
    m._ledger_append(m._ledger_note("unresolved", "apply_update", "Leads", "a", {}))
    m._ledger_append(m._ledger_note("reconciled", "reconcile_writes", "Leads",
                                    "a", {"resolves_seq": 1, "resolved": True}))
    out, _err = m.zoho_verify_ledger({}, {})
    assert out["unresolved"] == 1 and out["reconciled"] == 1, out
    out2, _e = m.zoho_audit_pack({"outcome": "reconciled"}, {})
    assert out2["entries"] == 1 and out2["reconciled"] == 1, out2


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
