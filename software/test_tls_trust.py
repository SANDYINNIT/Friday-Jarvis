"""Secure Windows trust bootstrap and actual client environment integration."""

import ast
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import ssl
from types import SimpleNamespace

import certifi
import pytest

from source.server import tls_trust as trust


@pytest.fixture(autouse=True)
def isolated_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setattr(trust, "_result", None)
    monkeypatch.setattr(trust, "sys", SimpleNamespace(platform="win32"))
    for name in trust._POLICY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    original = trust.tempfile.NamedTemporaryFile

    def temporary_bundle(**kwargs):
        return original(dir=tmp_path, **kwargs)

    monkeypatch.setattr(trust.tempfile, "NamedTemporaryFile", temporary_bundle)
    yield
    # Bootstrap writes directly; remove its test-only policy before fixture undo.
    for name in trust._POLICY_VARIABLES:
        os.environ.pop(name, None)


def expected_roots():
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return set(context.get_ca_certs(binary_form=True))


def test_exact_valid_public_ca_bundle_and_environment_only():
    roots = expected_roots()
    before = dict(os.environ)
    result = trust.bootstrap_windows_tls()
    assert result.status == "ready"
    path = Path(result.bundle_path)
    assert path.read_text(encoding="ascii") == "".join(
        ssl.DER_cert_to_PEM_cert(root) for root in sorted(roots)
    )
    context = ssl.create_default_context(cafile=str(path))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert set(context.get_ca_certs(binary_form=True)) == roots
    changed = {key for key in set(before) | set(os.environ)
               if before.get(key) != os.environ.get(key)}
    assert changed == {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}
    assert os.environ["SSL_CERT_FILE"] == os.environ["REQUESTS_CA_BUNDLE"] == str(path)


@pytest.mark.parametrize("name", trust._POLICY_VARIABLES)
@pytest.mark.parametrize("value", ["", "operator-trust-policy"])
def test_preserves_every_explicit_policy(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    before = dict(os.environ)
    monkeypatch.setattr(trust.ssl, "create_default_context", lambda **kw: pytest.fail("must skip"))
    assert trust.bootstrap_windows_tls().status == "explicit_policy"
    assert dict(os.environ) == before


def test_non_windows_noop(monkeypatch):
    monkeypatch.setattr(trust, "sys", SimpleNamespace(platform="linux"))
    before = dict(os.environ)
    monkeypatch.setattr(trust.ssl, "create_default_context", lambda **kw: pytest.fail("must skip"))
    assert trust.bootstrap_windows_tls().status == "not_windows"
    assert dict(os.environ) == before


def test_thread_safe_idempotent_and_immutable(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: trust.bootstrap_windows_tls(), range(16)))
    assert results[0].status == "ready"
    assert all(result is results[0] for result in results)
    assert len(list(tmp_path.glob("*.pem"))) == 1
    with pytest.raises(AttributeError):
        results[0].status = "changed"


def test_failed_write_does_not_publish_policy(monkeypatch, caplog):
    def unavailable(**kwargs):
        raise OSError("sensitive-path-or-key-never-log-this")

    before = dict(os.environ)
    monkeypatch.setattr(trust.tempfile, "NamedTemporaryFile", unavailable)
    result = trust.bootstrap_windows_tls()
    assert result.status == "failed"
    assert dict(os.environ) == before
    assert "OSError" in caplog.text
    assert "sensitive-path-or-key" not in caplog.text
    assert trust.bootstrap_windows_tls() is result


def test_partial_environment_publication_is_rolled_back(monkeypatch):
    class FailingEnvironment(dict):
        def __setitem__(self, key, value):
            if key == "REQUESTS_CA_BUNDLE":
                raise OSError("publication failed")
            super().__setitem__(key, value)

    environment = FailingEnvironment({"UNCHANGED": "value"})
    monkeypatch.setattr(trust, "os", SimpleNamespace(environ=environment))
    assert trust.bootstrap_windows_tls().status == "failed"
    assert environment == {"UNCHANGED": "value"}


def test_rejects_unsafe_default_verification(monkeypatch):
    class UnsafeContext:
        verify_mode = ssl.CERT_NONE
        check_hostname = False

        def load_verify_locations(self, **kwargs):
            pass

    monkeypatch.setattr(trust.ssl, "create_default_context", lambda **kw: UnsafeContext())
    before = dict(os.environ)
    assert trust.bootstrap_windows_tls().status == "failed"
    assert dict(os.environ) == before


def test_empty_trust_store_does_not_publish(monkeypatch):
    class EmptyContext:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True

        def load_verify_locations(self, **kwargs):
            pass

        def get_ca_certs(self, **kwargs):
            return []

    monkeypatch.setattr(trust.ssl, "create_default_context", lambda **kw: EmptyContext())
    before = dict(os.environ)
    assert trust.bootstrap_windows_tls().status == "failed"
    assert dict(os.environ) == before


def test_real_httpx_and_requests_honor_bundle():
    import httpx
    import requests

    result = trust.bootstrap_windows_tls()
    assert result.status == "ready"
    with requests.Session() as session:
        settings = session.merge_environment_settings("https://example.com", {}, None, None, None)
        assert settings["verify"] == result.bundle_path
    with httpx.Client() as client:
        context = client._transport._pool._ssl_context
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        from_bundle = ssl.create_default_context(cafile=result.bundle_path)
        assert set(context.get_ca_certs(binary_form=True)) == set(
            from_bundle.get_ca_certs(binary_form=True)
        )


def test_bootstrap_precedes_runtime_profile_execution():
    tree = ast.parse((Path(__file__).parent / "main.py").read_text(encoding="utf-8"))
    run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
    calls = [node for node in ast.walk(run) if isinstance(node, ast.Call)]
    bootstrap = next(node for node in calls if isinstance(node.func, ast.Name)
                     and node.func.id == "bootstrap_windows_tls")
    profile = next(node for node in calls if isinstance(node.func, ast.Attribute)
                   and node.func.attr == "exec_module")
    assert bootstrap.lineno < profile.lineno
