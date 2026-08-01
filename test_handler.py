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
               "zoho.apply_rollback"}
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
