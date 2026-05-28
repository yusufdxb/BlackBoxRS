"""Unit tests for the env_var preflight check."""

from __future__ import annotations


from blackboxrs.prevention.checks.env_var import run


# ---------------------------------------------------------------------------
# present=True (default)
# ---------------------------------------------------------------------------


def test_pass_when_var_is_set(monkeypatch):
    monkeypatch.setenv("TEST_BB_VAR", "hello")
    status, msg = run({"name": "TEST_BB_VAR"})
    assert status == "pass"
    assert "TEST_BB_VAR" in msg
    assert "hello" in msg


def test_fail_when_var_is_missing(monkeypatch):
    monkeypatch.delenv("TEST_BB_VAR", raising=False)
    status, msg = run({"name": "TEST_BB_VAR"})
    assert status == "fail"
    assert "TEST_BB_VAR" in msg
    assert "not set" in msg


def test_fail_when_var_is_empty_string(monkeypatch):
    monkeypatch.setenv("TEST_BB_VAR", "")
    status, msg = run({"name": "TEST_BB_VAR"})
    assert status == "fail"
    assert "TEST_BB_VAR" in msg


# ---------------------------------------------------------------------------
# present=False
# ---------------------------------------------------------------------------


def test_pass_when_var_absent_and_present_false(monkeypatch):
    monkeypatch.delenv("TEST_BB_VAR", raising=False)
    status, msg = run({"name": "TEST_BB_VAR", "present": False})
    assert status == "pass"
    assert "not set" in msg


def test_fail_when_var_set_and_present_false(monkeypatch):
    monkeypatch.setenv("TEST_BB_VAR", "something")
    status, msg = run({"name": "TEST_BB_VAR", "present": False})
    assert status == "fail"
    assert "present=False" in msg


# ---------------------------------------------------------------------------
# value_equals
# ---------------------------------------------------------------------------


def test_pass_value_equals_matches(monkeypatch):
    monkeypatch.setenv("BB_ENV", "production")
    status, msg = run({"name": "BB_ENV", "value_equals": "production"})
    assert status == "pass"


def test_fail_value_equals_mismatch(monkeypatch):
    monkeypatch.setenv("BB_ENV", "staging")
    status, msg = run({"name": "BB_ENV", "value_equals": "production"})
    assert status == "fail"
    assert "production" in msg
    assert "staging" in msg


# ---------------------------------------------------------------------------
# value_regex
# ---------------------------------------------------------------------------


def test_pass_value_regex_matches(monkeypatch):
    monkeypatch.setenv("BB_VERSION", "1.2.3")
    status, msg = run({"name": "BB_VERSION", "value_regex": r"\d+\.\d+\.\d+"})
    assert status == "pass"


def test_fail_value_regex_no_match(monkeypatch):
    monkeypatch.setenv("BB_VERSION", "latest")
    status, msg = run({"name": "BB_VERSION", "value_regex": r"\d+\.\d+\.\d+"})
    assert status == "fail"
    assert r"\d+" in msg or "regex" in msg.lower()


# ---------------------------------------------------------------------------
# error path: missing 'name' param
# ---------------------------------------------------------------------------


def test_error_on_missing_name():
    status, msg = run({})
    assert status == "error"
    assert "name" in msg.lower()


def test_error_on_empty_name():
    status, msg = run({"name": ""})
    assert status == "error"
