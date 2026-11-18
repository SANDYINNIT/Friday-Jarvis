"""Process-local Windows trust bridge for HTTPX and Requests.

Use the OS trust store plus certifi without changing system certificates or
disabling chain/hostname verification. Call before constructing cloud clients.
"""

from dataclasses import dataclass
import logging
import os
import ssl
import sys
import tempfile
import threading


_POLICY_VARIABLES = (
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"
)
_lock = threading.Lock()
_result = None
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrustBootstrapResult:
    status: str
    bundle_path: str | None = None


def bootstrap_windows_tls() -> TrustBootstrapResult:
    """Publish a validated public CA bundle once, preserving explicit policy.

    Empty-but-present override variables also count as operator policy.
    Failure leaves environment trust settings untouched and verification on.
    The uniquely named temporary public-CA file remains available to child
    processes for this process lifetime; no existing file is deleted.
    """
    global _result
    with _lock:
        if _result is not None:
            return _result
        if sys.platform != "win32":
            _result = TrustBootstrapResult("not_windows")
            return _result
        if any(name in os.environ for name in _POLICY_VARIABLES):
            _result = TrustBootstrapResult("explicit_policy")
            return _result
        try:
            import certifi

            context = ssl.create_default_context()
            context.load_verify_locations(cafile=certifi.where())
            if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                raise ValueError("Default TLS verification policy is unsafe")
            roots = sorted(set(context.get_ca_certs(binary_form=True)))
            if not roots:
                raise ValueError("No trusted CA certificates available")
            pem = "".join(ssl.DER_cert_to_PEM_cert(root) for root in roots)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="ascii", newline="\n",
                prefix="friday-public-ca-", suffix=".pem", delete=False,
            ) as bundle:
                bundle.write(pem)
                path = bundle.name
            # Validate the complete closed file before either env publication.
            verified = ssl.create_default_context(cafile=path)
            if verified.verify_mode != ssl.CERT_REQUIRED or not verified.check_hostname:
                raise ValueError("Bundle TLS verification policy is unsafe")
            if any(name in os.environ for name in _POLICY_VARIABLES):
                _result = TrustBootstrapResult("explicit_policy")
                return _result
            try:
                os.environ["SSL_CERT_FILE"] = path
                os.environ["REQUESTS_CA_BUNDLE"] = path
            except Exception:
                # Both were absent before publication; undo a partial update.
                os.environ.pop("SSL_CERT_FILE", None)
                os.environ.pop("REQUESTS_CA_BUNDLE", None)
                raise
            _result = TrustBootstrapResult("ready", path)
            _log.info("Windows TLS trust initialized with verified public CA roots")
        except Exception as error:
            _result = TrustBootstrapResult("failed")
            _log.warning(
                "Windows TLS trust initialization failed (%s); certificate verification remains enabled",
                type(error).__name__,
            )
        return _result
