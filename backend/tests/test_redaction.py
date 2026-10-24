"""Tests for gatekeeper.redaction. All token-like values are synthetic and built at runtime."""

from __future__ import annotations

import pytest

from gatekeeper.redaction import Redactor

FAKE_TOKEN_BODY = "Ab3" * 12  # 36 chars, synthetic and low-entropy


@pytest.mark.parametrize("prefix", ["ghp_", "gho_", "ghu_", "ghs_", "ghr_"])
def test_masks_classic_github_tokens(prefix: str) -> None:
    text = f"token={prefix}{FAKE_TOKEN_BODY} trailing"
    assert Redactor().redact_text(text) == "token=[REDACTED:github_token] trailing"


def test_masks_fine_grained_github_pat() -> None:
    token = "github_pat_" + "11ABCDEFG0" + "_" + "x" * 59
    assert Redactor().redact_text(f"use {token}.") == "use [REDACTED:github_token]."


def test_masks_bearer_token_but_keeps_scheme() -> None:
    text = "Authorization: Bearer abcDEF123456ghiJKL789.mno-pqr_stu"
    assert Redactor().redact_text(text) == "Authorization: Bearer [REDACTED:bearer_token]"


def test_masks_private_key_blocks() -> None:
    block = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END " + "RSA PRIVATE KEY-----"
    assert Redactor().redact_text(f"key:\n{block}\nafter") == "key:\n[REDACTED:private_key]\nafter"
    unterminated = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nb3BlbnNz"
    assert Redactor().redact_text(unterminated) == "[REDACTED:private_key]"


def test_masks_email_addresses() -> None:
    text = "ping jane.doe+ci@example.co.uk or J_D@mail.example.org."
    assert Redactor().redact_text(text) == "ping [REDACTED:email] or [REDACTED:email]."


def test_masks_known_secret_values_longest_first() -> None:
    redactor = Redactor(["s3cr3t-value-123", "s3cr3t-value-123-and-more", "short"])
    text = "a s3cr3t-value-123-and-more b s3cr3t-value-123 c short"
    assert redactor.redact_text(text) == "a [REDACTED:secret] b [REDACTED:secret] c short"


def test_does_not_mangle_ordinary_text() -> None:
    sample = (
        "Fixes #42: handle None in parse_config().\n"
        "@pytest.fixture\ndef f(): return {'key': 'value', 'token_count': 3}\n"
        "Bump actions/checkout@v7.0.1 and requests@2.32.3; see git@github.com:org/repo.git\n"
        "The bearer of bad news said ghp_short is not a token. sha 3d3c42e5aac5ba805825da76410c18\n"
        "Email us at support (at) example (dot) com. user@localhost is local. @types/node ok.\n"
        "BEGIN PUBLIC KEY is fine; so is 'Bearer' alone. Price: $5 @ 10%."
    )
    assert Redactor(["unrelated-secret-xyz"]).redact_text(sample) == sample


def test_redact_walks_nested_structures_without_touching_keys() -> None:
    value = {
        "author_email": "dev@example.com",
        "items": [f"ghp_{FAKE_TOKEN_BODY}", 3, None, True, ("a", "b@example.com")],
        "nested": {"ok": 1.5},
    }
    assert Redactor().redact(value) == {
        "author_email": "[REDACTED:email]",
        "items": ["[REDACTED:github_token]", 3, None, True, ["a", "[REDACTED:email]"]],
        "nested": {"ok": 1.5},
    }
