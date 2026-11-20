"""Envelope-crypto regression tests for bill_service.

Covers the hardened behaviour of the envelope helpers:

  * a sensitive field whose name ends in ``_search`` is decrypted instead of
    being silently dropped (the old code stripped every ``*_search`` key before
    checking whether it held real, encrypted data),
  * derived search tokens still never leave the service,
  * a failed decryption does not echo the raw crypto exception,
  * an envelope this build cannot read is rejected instead of guessed at.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-0123456789abcdef")
os.environ.setdefault("MASTER_KEY", "test-only-master-key-0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from bill_service import crypto_helper

SENSITIVE = ["client_name", "client_name_search"]


def test_sensitive_field_ending_in_search_is_decrypted():
    """The ``*_search`` rule must not swallow real (encrypted) sensitive data."""
    plain = {"client_name": "Alice", "client_name_search": "Alice"}
    encrypted = crypto_helper.encrypt_dict(plain, SENSITIVE)

    assert isinstance(encrypted["client_name_search"], dict)
    assert "ciphertext" in encrypted["client_name_search"]

    decrypted = crypto_helper.decrypt_dict(encrypted, SENSITIVE)
    assert decrypted["client_name_search"] == "Alice"
    assert decrypted["client_name"] == "Alice"


def test_plain_search_token_is_stripped():
    """A derived token is not sensitive data and must not leave the service."""
    encrypted = crypto_helper.encrypt_dict({"client_name": "Alice"}, ["client_name"])
    encrypted["client_name_search"] = crypto_helper.get_search_token("Alice")

    decrypted = crypto_helper.decrypt_dict(encrypted, ["client_name"])
    assert "client_name_search" not in decrypted
    assert decrypted["client_name"] == "Alice"


def test_decryption_failure_does_not_leak_raw_exception_text():
    encrypted = crypto_helper.encrypt_dict({"client_name": "Alice"}, ["client_name"])
    encrypted["encryption_metadata"]["wrappedDek"]["ciphertext"] = "not-hex-at-all"

    with pytest.raises(ValueError) as exc:
        crypto_helper.decrypt_dict(encrypted, ["client_name"])

    message = str(exc.value)
    # The raw error was "non-hexadecimal number found in fromhex() arg at
    # position 0" - none of that may reach the caller.
    assert "non-hexadecimal" not in message
    assert "at position 0" not in message
    assert "not-hex-at-all" not in message


def test_unsupported_envelope_version_is_rejected():
    encrypted = crypto_helper.encrypt_dict({"client_name": "Alice"}, ["client_name"])
    encrypted["encryption_metadata"]["version"] = 99

    with pytest.raises(ValueError) as exc:
        crypto_helper.decrypt_dict(encrypted, ["client_name"])
    assert "version" in str(exc.value).lower()


def test_unsupported_envelope_algorithm_is_rejected():
    encrypted = crypto_helper.encrypt_dict({"client_name": "Alice"}, ["client_name"])
    encrypted["encryption_metadata"]["algorithm"] = "AES-128-CBC"

    with pytest.raises(ValueError) as exc:
        crypto_helper.decrypt_dict(encrypted, ["client_name"])
    assert "algorithm" in str(exc.value).lower()


def test_envelope_metadata_is_required_to_be_well_formed():
    encrypted = crypto_helper.encrypt_dict({"client_name": "Alice"}, ["client_name"])
    encrypted["encryption_metadata"] = {"version": 1, "algorithm": "AES-256-GCM"}

    with pytest.raises(ValueError) as exc:
        crypto_helper.decrypt_dict(encrypted, ["client_name"])
    assert "wrapped" in str(exc.value).lower()
