"""Offline SIWC security regressions: no external login, browser or inference.

All token strings and identities below are generated test fixtures. Real Windows
DPAPI and a real localhost callback are exercised only in temporary app folders.
"""

import copy
from contextlib import contextmanager
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

from chatgpt_auth import (
    APP_ID, APP_NAME, AUTHORIZE_ENDPOINT, ChatGPTAuth, ChatGPTAuthError,
    CredentialStore, DISCOVERY_ENDPOINT, ISSUER, JWKS_ENDPOINT, JWTVerifier,
    LoginAttempt, OfficialHTTP, PLAN_SCOPES, REQUESTED_SCOPES, RESOURCE,
    TOKEN_ENDPOINT, WindowsDPAPI, _NoRedirect, _official_url, validate_callback,
    TERMINAL_REFRESH_ERRORS,
)


def record(client="oaiapp_fixture", subject="fixture-subject", expired=False):
    return {"issuer": ISSUER, "client_id": client, "subject": subject, "email": "fixture@example.test",
            "access_token": "fixture-access-old", "refresh_token": "fixture-refresh-old", "id_token": "fixture-id-hint",
            "nonce": "fixture-nonce", "scopes": REQUESTED_SCOPES.split(), "expires_at": time.time() - 10 if expired else time.time() + 3600,
            "saved_at": time.time() - 3600, "earliest_refresh_at": None, "welcome_seen": False}


def state_with_record(expired=False):
    fixture = record(expired=expired)
    return {"version": 1, "host_id": "urn:uuid:" + str(uuid.uuid4()), "active_client": fixture["client_id"], "registrations": {fixture["client_id"]: fixture}}


def token_response(**overrides):
    data = {"access_token": "fixture-access-new", "refresh_token": "fixture-refresh-new", "token_type": "Bearer", "expires_in": 3600,
            "scope": REQUESTED_SCOPES, "earliest_refresh_at": time.time() - 1}
    data.update(overrides)
    return data


class MemoryStore:
    """Test-only in-memory stand-in; production never accepts plaintext storage."""

    def __init__(self, data=None):
        self.data = copy.deepcopy(data)
        self.lock = threading.RLock()
        self.saves = 0

    def load(self):
        return copy.deepcopy(self.data)

    def save(self, data):
        self.data = copy.deepcopy(data)
        self.saves += 1

    @contextmanager
    def locked(self, cancel_event=None, deadline=None):
        with self.lock:
            if cancel_event is not None and cancel_event.is_set():
                raise ChatGPTAuthError("已取消。", "cancelled")
            yield


class FixtureTransport:
    def __init__(self, result=None):
        self.result = result if result is not None else token_response()
        self.requests = []
        self.after_request = None

    def request(self, url, form=None, deadline=None, allow_empty=False):
        self.requests.append((url, copy.deepcopy(form)))
        if self.after_request:
            self.after_request()
        if isinstance(self.result, Exception):
            raise self.result
        return copy.deepcopy(self.result)


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.attempt = LoginAttempt.create("http://127.0.0.1:1455/auth/callback")

    def callback(self, **overrides):
        params = {"state": self.attempt.state, "code": "fixture-authorization-code", "client_id": "oaiapp_fixture"}
        params.update(overrides)
        return "/auth/callback?" + urlencode(params)

    def test_fresh_attempts_have_independent_state_nonce_pkce(self):
        second = LoginAttempt.create(self.attempt.redirect_uri)
        self.assertNotEqual(self.attempt.state, second.state)
        self.assertNotEqual(self.attempt.nonce, second.nonce)
        self.assertNotEqual(self.attempt.verifier, second.verifier)
        self.assertTrue(43 <= len(self.attempt.verifier) <= 128)
        self.assertEqual(len(self.attempt.challenge), 43)
        self.assertNotIn(self.attempt.state, repr(self.attempt))

    def test_dynamic_auth_uses_official_parameters(self):
        url = self.attempt.authorize_url("urn:uuid:" + str(uuid.uuid4()))
        self.assertTrue(url.startswith(AUTHORIZE_ENDPOINT + "?"))
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["client_id"], ["dynamic_agent_client"])
        self.assertEqual(query["agent_name_hint"], [APP_NAME])
        self.assertEqual(query["resource"], [RESOURCE])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], [self.attempt.redirect_uri])
        self.assertNotIn("access_token", query)
        self.assertNotIn("refresh_token", query)

    def test_returning_auth_reuses_registration_and_only_identity_hint(self):
        self.attempt.client_id = "oaiapp_fixture"
        query = parse_qs(urlsplit(self.attempt.authorize_url("urn:uuid:" + str(uuid.uuid4()), record())).query)
        self.assertNotIn("agent_name_hint", query)
        self.assertEqual(query["client_id"], ["oaiapp_fixture"])
        self.assertEqual(query["id_token_hint"], ["fixture-id-hint"])
        self.assertNotIn("fixture-access-old", str(query))
        self.assertNotIn("fixture-refresh-old", str(query))
        self.assertNotIn("prompt", query)

    def test_only_explicit_plan_enable_requests_supported_consent(self):
        self.attempt.client_id = "oaiapp_fixture"
        query = parse_qs(urlsplit(self.attempt.authorize_url("urn:uuid:" + str(uuid.uuid4()), record(), enable_plan=True)).query)
        self.assertEqual(query["prompt"], ["consent"])
        self.assertNotIn("force_reconsent", query)

    def test_dynamic_callback_retains_issued_client(self):
        self.assertEqual(validate_callback(self.callback(), self.attempt), ("fixture-authorization-code", "oaiapp_fixture"))

    def test_wrong_missing_duplicate_and_expired_state_rejected(self):
        for callback in (self.callback(state="wrong"), self.callback(state=""), self.callback() + "&state=other"):
            with self.assertRaises(ChatGPTAuthError):
                validate_callback(callback, self.attempt)
        self.attempt.deadline = time.monotonic() - 1
        with self.assertRaises(ChatGPTAuthError) as caught:
            validate_callback(self.callback(), self.attempt)
        self.assertEqual(caught.exception.code, "timeout")

    def test_error_callback_never_yields_code(self):
        with self.assertRaises(ChatGPTAuthError) as caught:
            validate_callback(self.callback(error="access_denied"), self.attempt)
        self.assertEqual(caught.exception.code, "access_denied")

    def test_error_does_not_skip_state_validation(self):
        with self.assertRaises(ChatGPTAuthError) as caught:
            validate_callback(self.callback(error="access_denied", state="wrong"), self.attempt)
        self.assertEqual(caught.exception.code, "state_mismatch")

    def test_dynamic_entry_id_is_not_valid_issued_id(self):
        for client in ("", "dynamic_agent_client", "../../credentials", "client\nmalformed"):
            with self.assertRaises(ChatGPTAuthError):
                validate_callback(self.callback(client_id=client), self.attempt)

    def test_returning_callback_may_omit_but_not_change_client(self):
        self.attempt.client_id = "oaiapp_fixture"
        callback = "/auth/callback?" + urlencode({"state": self.attempt.state, "code": "fixture-code"})
        self.assertEqual(validate_callback(callback, self.attempt), ("fixture-code", "oaiapp_fixture"))
        with self.assertRaises(ChatGPTAuthError) as caught:
            validate_callback(self.callback(client_id="oaiapp_other"), self.attempt)
        self.assertEqual(caught.exception.code, "client_mismatch")

    def test_callback_path_must_match_and_be_relative(self):
        for path in ("/callback", "https://evil.example/auth/callback", "//evil.example/auth/callback", "/auth/callback#secret"):
            with self.assertRaises(ChatGPTAuthError):
                validate_callback(path + "?state=ignored", self.attempt)


class HTTPTransportTests(unittest.TestCase):
    class Response:
        status = 200

        def __init__(self, payload):
            self.stream = io.BytesIO(payload)

        def read1(self, size):
            return self.stream.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

    def test_only_fixed_official_https_host_allowed(self):
        self.assertTrue(_official_url(TOKEN_ENDPOINT))
        for url in ("http://auth.openai.com/token", "https://evil.example/token", "https://auth.openai.com.evil.example/token", "https://user@auth.openai.com/token", TOKEN_ENDPOINT + "?refresh_token=fixture", TOKEN_ENDPOINT + "#secret", "https://auth.openai.com:8443/token"):
            self.assertFalse(_official_url(url))
            with self.assertRaises(ChatGPTAuthError):
                OfficialHTTP().request(url)

    def test_redirects_are_disabled(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_form_body_and_bounded_timeout(self):
        with patch("chatgpt_auth.build_opener") as opener:
            opener.return_value.open.return_value = self.Response(b'{"scope":"openid"}')
            self.assertEqual(OfficialHTTP().request(TOKEN_ENDPOINT, {"refresh_token": "fixture-refresh"}), {"scope": "openid"})
            call = opener.return_value.open.call_args
            request = call.args[0]
            self.assertEqual(request.full_url, TOKEN_ENDPOINT)
            self.assertIn(b"refresh_token=fixture-refresh", request.data)
            self.assertLessEqual(call.kwargs["timeout"], 15)

    def test_no_secret_in_error_or_exception_chain(self):
        body = io.BytesIO(b'{"error":"invalid_grant","error_description":"fixture-secret-do-not-print"}')
        error = HTTPError(TOKEN_ENDPOINT, 400, "fixture-secret", {}, body)
        with patch("chatgpt_auth.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(ChatGPTAuthError) as caught:
                OfficialHTTP().request(TOKEN_ENDPOINT)
        self.assertEqual(caught.exception.code, "invalid_grant")
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertTrue(body.closed)

    def test_network_error_is_sanitized(self):
        with patch("chatgpt_auth.build_opener") as opener:
            opener.return_value.open.side_effect = URLError("fixture-secret")
            with self.assertRaises(ChatGPTAuthError) as caught:
                OfficialHTTP().request(JWKS_ENDPOINT)
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertEqual(caught.exception.code, "network_error")

    def test_terminal_refresh_error_codes_are_preserved_without_server_descriptions(self):
        for code in TERMINAL_REFRESH_ERRORS:
            body = io.BytesIO(json.dumps({"error": code, "error_description": "fixture-secret"}).encode())
            error = HTTPError(TOKEN_ENDPOINT, 400, "fixture-secret", {}, body)
            with patch("chatgpt_auth.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(ChatGPTAuthError) as caught:
                    OfficialHTTP().request(TOKEN_ENDPOINT)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.http_status, 400)
            self.assertNotIn("fixture-secret", str(caught.exception))

    def test_oversized_and_non_object_json_rejected(self):
        for payload in (b"[]", b"bad json", b"a" * (256 * 1024 + 1), b"[" * 2000 + b"0" + b"]" * 2000):
            with patch("chatgpt_auth.build_opener") as opener:
                opener.return_value.open.return_value = self.Response(payload)
                with self.assertRaises(ChatGPTAuthError):
                    OfficialHTTP().request(JWKS_ENDPOINT)

    def test_empty_revocation_response_accepted_only_when_explicit(self):
        with patch("chatgpt_auth.build_opener") as opener:
            opener.return_value.open.return_value = self.Response(b"")
            self.assertEqual(OfficialHTTP().request(TOKEN_ENDPOINT, {}, allow_empty=True), {})


try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False


@unittest.skipUnless(JWT_AVAILABLE, "PyJWT[crypto] not installed")
class SignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk.update({"kid": "fixture-kid", "alg": "RS256", "use": "sig"})

    def setUp(self):
        self.transport = FixtureTransport({"keys": [self.jwk]})
        self.verifier = JWTVerifier(self.transport)

    def token(self, **overrides):
        claims = {"iss": ISSUER, "aud": "oaiapp_fixture", "sub": "fixture-subject", "iat": int(time.time()) - 1,
                  "exp": int(time.time()) + 3600, "nonce": "fixture-nonce", "email": "fixture@example.test"}
        claims.update(overrides)
        for key in [key for key, value in claims.items() if value is None]:
            claims.pop(key)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "fixture-kid"})

    def test_valid_signature_issuer_audience_expiration_nonce(self):
        claims = self.verifier.verify(self.token(), "oaiapp_fixture", "fixture-nonce")
        self.assertEqual(claims["sub"], "fixture-subject")
        self.assertEqual(self.transport.requests[0][0], JWKS_ENDPOINT)

    def test_invalid_issuer_audience_expiry_nonce_and_missing_subject_rejected(self):
        for overrides in ({"iss": "https://evil.example"}, {"aud": "oaiapp_other"}, {"exp": int(time.time()) - 120}, {"nonce": "wrong"}, {"nonce": None}, {"sub": None}, {"iat": None}, {"sub": ""}, {"iat": int(time.time()) + 120}):
            with self.subTest(overrides=list(overrides)):
                with self.assertRaises(ChatGPTAuthError):
                    self.verifier.verify(self.token(**overrides), "oaiapp_fixture", "fixture-nonce")

    def test_forged_signature_rejected(self):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = jwt.decode(self.token(), options={"verify_signature": False})
        forged = jwt.encode(claims, other_key, algorithm="RS256", headers={"kid": "fixture-kid"})
        with self.assertRaises(ChatGPTAuthError):
            self.verifier.verify(forged, "oaiapp_fixture", "fixture-nonce")

    def test_none_and_hmac_algorithm_cannot_use_public_jwks(self):
        claims = {"iss": ISSUER, "aud": "oaiapp_fixture", "sub": "fixture-subject", "exp": time.time() + 60, "iat": time.time(), "nonce": "fixture-nonce"}
        for token in (jwt.encode(claims, None, algorithm="none", headers={"kid": "fixture-kid"}), jwt.encode(claims, "fixture-hmac-key-long-enough-for-a-test", algorithm="HS256", headers={"kid": "fixture-kid"})):
            with self.assertRaises(ChatGPTAuthError):
                self.verifier.verify(token, "oaiapp_fixture", "fixture-nonce")

    def test_refresh_allows_missing_nonce_but_not_changed_nonce(self):
        self.assertEqual(self.verifier.verify(self.token(nonce=None), "oaiapp_fixture", "fixture-nonce", require_nonce=False)["sub"], "fixture-subject")
        with self.assertRaises(ChatGPTAuthError):
            self.verifier.verify(self.token(nonce="wrong"), "oaiapp_fixture", "fixture-nonce", require_nonce=False)

    def test_multiple_audiences_need_matching_authorized_party(self):
        with self.assertRaises(ChatGPTAuthError):
            self.verifier.verify(self.token(aud=["oaiapp_fixture", "other"]), "oaiapp_fixture", "fixture-nonce")
        self.assertEqual(self.verifier.verify(self.token(aud=["oaiapp_fixture", "other"], azp="oaiapp_fixture"), "oaiapp_fixture", "fixture-nonce")["sub"], "fixture-subject")

    def test_unknown_key_is_refetched_once_and_never_trusted(self):
        claims = jwt.decode(self.token(), options={"verify_signature": False})
        token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "unknown-kid"})
        with self.assertRaises(ChatGPTAuthError):
            self.verifier.verify(token, "oaiapp_fixture", "fixture-nonce")
        self.assertEqual(len(self.transport.requests), 2)


class AuthSessionTests(unittest.TestCase):
    def setUp(self):
        self.transport = FixtureTransport()
        self.auth = ChatGPTAuth(transport=self.transport)
        self.auth.store = MemoryStore(state_with_record())

    def current(self):
        state = self.auth.store.data
        return state["registrations"][state["active_client"]]

    def test_status_is_local_only_and_never_exposes_tokens(self):
        status = self.auth.status()
        self.assertTrue(status["signed_in"])
        self.assertTrue(status["plan_enabled"])
        self.assertEqual(status["email"], "fixture@example.test")
        self.assertNotIn("fixture-access", repr(status))
        self.assertNotIn("fixture-refresh", repr(status))
        self.assertNotIn("fixture-id-hint", repr(status))
        self.assertEqual(self.transport.requests, [])

    def test_valid_access_is_returned_without_refresh(self):
        self.assertEqual(self.auth.access_token(), "fixture-access-old")
        self.assertEqual(self.transport.requests, [])

    def test_missing_plan_scope_rejects_inference_no_fallback(self):
        self.current()["scopes"] = ["openid", "email", "profile"]
        self.assertTrue(self.auth.status()["signed_in"])
        self.assertFalse(self.auth.status()["plan_enabled"])
        with self.assertRaises(ChatGPTAuthError) as caught:
            self.auth.access_token()
        self.assertEqual(caught.exception.code, "plan_not_enabled")
        self.assertEqual(self.transport.requests, [])

    def test_missing_refresh_token_is_not_silently_preserved(self):
        self.auth.store = MemoryStore(state_with_record(expired=True))
        self.transport.result = token_response(refresh_token=None)
        with self.assertRaises(ChatGPTAuthError):
            self.auth.access_token()
        self.assertFalse(self.auth.status()["signed_in"])
        self.assertEqual(self.current()["client_id"], "oaiapp_fixture")

    def test_rotation_updates_full_token_set_in_one_snapshot(self):
        self.auth.store = MemoryStore(state_with_record(expired=True))
        self.assertEqual(self.auth.access_token(), "fixture-access-new")
        self.assertEqual(self.current()["refresh_token"], "fixture-refresh-new")
        self.assertGreater(self.current()["expires_at"], time.time() + 3500)
        # A metadata-only durable intention precedes the atomic new token set.
        self.assertEqual(self.auth.store.saves, 2)
        request = self.transport.requests[0]
        self.assertEqual(request[1]["client_id"], "oaiapp_fixture")
        self.assertEqual(request[1]["resource"], RESOURCE)
        self.assertNotIn("scope", request[1])

    def test_cancel_during_remote_rotation_persists_new_tokens_then_stops(self):
        self.auth.store = MemoryStore(state_with_record(expired=True))
        cancel = threading.Event()
        self.transport.after_request = cancel.set
        with self.assertRaises(ChatGPTAuthError) as caught:
            self.auth.access_token(cancel)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertEqual(self.current()["refresh_token"], "fixture-refresh-new")
        self.assertEqual(self.current()["access_token"], "fixture-access-new")
        self.assertTrue(self.auth.status()["signed_in"])

    def test_invalid_response_after_rotation_invalidates_spent_old_tokens(self):
        self.auth.store = MemoryStore(state_with_record(expired=True))
        self.transport.result = token_response(expires_in=float("nan"))
        with self.assertRaises(ChatGPTAuthError):
            self.auth.access_token()
        self.assertNotIn("refresh_token", self.current())
        self.assertFalse(self.auth.status()["signed_in"])

    def test_rotation_scope_downgrade_saved_and_inference_disabled(self):
        self.auth.store = MemoryStore(state_with_record(expired=True))
        self.transport.result = token_response(scope="openid email profile offline_access")
        with self.assertRaises(ChatGPTAuthError) as caught:
            self.auth.access_token()
        self.assertEqual(caught.exception.code, "plan_not_enabled")
        self.assertEqual(self.current()["refresh_token"], "fixture-refresh-new")
        self.assertFalse(self.auth.status()["plan_enabled"])

    def test_transient_refresh_failure_keeps_unconsumed_credentials(self):
        self.auth.store = MemoryStore(state_with_record(expired=True))
        self.transport.result = ChatGPTAuthError("暂时无法连接。", "network_error")
        with self.assertRaises(ChatGPTAuthError):
            self.auth.access_token()
        self.assertEqual(self.current()["refresh_token"], "fixture-refresh-old")
        self.assertNotIn("refresh_in_progress", self.current())

    def test_save_failure_after_rotation_blocks_spent_token_in_this_and_next_instance(self):
        self.auth.store = MemoryStore(state_with_record(expired=True))
        real_save = self.auth.store.save
        calls = []
        def failing_save(data):
            calls.append(1)
            if len(calls) > 1:
                raise ChatGPTAuthError("fixture-save-failure", "secure_storage_error")
            real_save(data)
        self.auth.store.save = failing_save
        with self.assertRaises(ChatGPTAuthError) as caught:
            self.auth.access_token()
        self.assertEqual(caught.exception.code, "session_storage_failed")
        self.assertTrue(self.current()["refresh_in_progress"])
        with self.assertRaises(ChatGPTAuthError):
            self.auth.access_token()
        self.assertEqual(len(self.transport.requests), 1)
        second = ChatGPTAuth(transport=self.transport)
        second.store = self.auth.store
        with self.assertRaises(ChatGPTAuthError) as caught:
            second.access_token()
        self.assertEqual(caught.exception.code, "refresh_incomplete")
        self.assertEqual(len(self.transport.requests), 1)

    def test_temporary_jwks_failure_preserves_encrypted_pending_rotation_for_retry(self):
        self.auth.store = MemoryStore(state_with_record(expired=True))
        self.transport.result = token_response(id_token="fixture-new-id")
        verifier_results = [ChatGPTAuthError("验证服务暂时不可用。", "network_error"), {"sub": "fixture-subject", "email": "fixture@example.test"}]
        with patch.object(self.auth.verifier, "verify", side_effect=verifier_results):
            with self.assertRaises(ChatGPTAuthError):
                self.auth.access_token()
            self.assertTrue(self.auth.status()["pending_verification"])
            self.assertFalse(self.auth.status()["plan_enabled"])
            self.assertTrue(self.auth.status()["plan_authorized"])
            self.assertEqual(self.current()["pending_refresh"]["response"]["refresh_token"], "fixture-refresh-new")
            self.assertEqual(self.auth.access_token(), "fixture-access-new")
        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.current()["refresh_token"], "fixture-refresh-new")
        self.assertNotIn("pending_refresh", self.current())
        self.assertNotIn("refresh_in_progress", self.current())

    def test_crash_intention_marker_blocks_refresh_instead_of_reusing_old_credentials(self):
        self.current()["refresh_in_progress"] = True
        with self.assertRaises(ChatGPTAuthError) as caught:
            self.auth.access_token()
        self.assertEqual(caught.exception.code, "refresh_incomplete")
        self.assertEqual(self.transport.requests, [])

    def test_revoked_refresh_clears_tokens_but_not_registration(self):
        self.auth.store = MemoryStore(state_with_record(expired=True))
        self.transport.result = ChatGPTAuthError("重新登录。", "invalid_grant")
        with self.assertRaises(ChatGPTAuthError):
            self.auth.access_token()
        self.assertFalse(self.auth.status()["signed_in"])
        self.assertTrue(self.auth.status()["registered"])

    def test_all_terminal_refresh_errors_clear_tokens_but_keep_account_mapping(self):
        for code in TERMINAL_REFRESH_ERRORS:
            self.auth.store = MemoryStore(state_with_record(expired=True))
            self.transport.result = ChatGPTAuthError("登录失效。", code)
            with self.assertRaises(ChatGPTAuthError):
                self.auth.access_token()
            self.assertFalse(self.auth.status()["signed_in"])
            self.assertEqual(self.current()["client_id"], "oaiapp_fixture")

    def test_confirmed_http200_malformed_refresh_body_cannot_reuse_spent_old_token(self):
        for payload in (b"bad json", b"[]", b"a" * (256 * 1024 + 1)):
            self.auth.store = MemoryStore(state_with_record(expired=True))
            self.auth.transport = OfficialHTTP()
            with patch("chatgpt_auth.build_opener") as opener:
                opener.return_value.open.return_value = HTTPTransportTests.Response(payload)
                with self.assertRaises(ChatGPTAuthError):
                    self.auth.access_token()
            self.assertFalse(self.auth.status()["signed_in"])
            self.assertNotIn("refresh_token", self.current())
            self.assertTrue(self.auth.status()["registered"])

    def test_refresh_identity_mismatch_is_not_saved(self):
        self.auth.store = MemoryStore(state_with_record(expired=True))
        self.transport.result = token_response(id_token="fixture-refreshed-id")
        with patch.object(self.auth.verifier, "verify", return_value={"sub": "different-account"}):
            with self.assertRaises(ChatGPTAuthError) as caught:
                self.auth.access_token()
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.assertEqual(self.current()["subject"], "fixture-subject")
        self.assertNotIn("refresh_token", self.current())

    def test_earliest_refresh_is_respected(self):
        self.current()["expires_at"] = time.time() + 30
        self.current()["earliest_refresh_at"] = time.time() + 20
        self.assertEqual(self.auth.access_token(), "fixture-access-old")
        self.assertEqual(self.transport.requests, [])
        self.current()["expires_at"] = time.time() - 1
        with self.assertRaises(ChatGPTAuthError) as caught:
            self.auth.access_token()
        self.assertEqual(caught.exception.code, "refresh_not_ready")

    def test_two_concurrent_requests_share_one_refresh(self):
        self.auth.store = MemoryStore(state_with_record(expired=True))
        output = []
        failures = []
        def call():
            try:
                output.append(self.auth.access_token())
            except Exception as exc:
                failures.append(type(exc).__name__)
        workers = [threading.Thread(target=call) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        self.assertEqual(failures, [])
        self.assertEqual(output, ["fixture-access-new"] * 2)
        self.assertEqual(len(self.transport.requests), 1)

    def test_logout_revokes_only_selected_registration_and_preserves_mapping_host(self):
        self.auth.store.data["registrations"]["oaiapp_other"] = record("oaiapp_other", "fixture-other")
        host = self.auth.store.data["host_id"]
        endpoint = ISSUER + "/api/accounts/oauth/revoke"
        def request(url, form=None, **kwargs):
            self.transport.requests.append((url, copy.deepcopy(form)))
            return {"issuer": ISSUER, "revocation_endpoint": endpoint} if url == DISCOVERY_ENDPOINT else {}
        self.transport.request = request
        self.auth.sign_out()
        self.assertFalse(self.auth.status()["signed_in"])
        self.assertFalse(self.auth.status()["remote_revocation_unconfirmed"])
        self.assertEqual(self.auth.store.data["host_id"], host)
        self.assertEqual(self.current()["client_id"], "oaiapp_fixture")
        self.assertEqual(self.current()["subject"], "fixture-subject")
        self.assertNotIn("id_token", self.current())
        self.assertEqual(self.auth.store.data["registrations"]["oaiapp_other"]["refresh_token"], "fixture-refresh-old")
        self.assertEqual(self.transport.requests[1], (endpoint, {"token": "fixture-refresh-old", "token_type_hint": "refresh_token", "client_id": "oaiapp_fixture"}))

    def test_logout_unconfirmed_revocation_still_clears_local_tokens(self):
        self.transport.result = ChatGPTAuthError("网络不可用。", "network_error")
        self.auth.sign_out()
        self.assertFalse(self.auth.status()["signed_in"])
        self.assertTrue(self.auth.status()["remote_revocation_unconfirmed"])
        self.assertNotIn("fixture-refresh", self.auth.last_warning)

    def test_logout_uses_latest_pending_rotated_token_not_spent_old_token(self):
        self.current()["pending_refresh"] = {"response": token_response(), "received_at": time.time()}
        self.current()["refresh_in_progress"] = True
        endpoint = ISSUER + "/api/accounts/oauth/revoke"
        def request(url, form=None, **kwargs):
            self.transport.requests.append((url, copy.deepcopy(form)))
            return {"issuer": ISSUER, "revocation_endpoint": endpoint} if url == DISCOVERY_ENDPOINT else {}
        self.transport.request = request
        self.auth.sign_out()
        self.assertEqual(self.transport.requests[1][1]["token"], "fixture-refresh-new")
        self.assertNotIn("pending_refresh", self.current())
        self.assertFalse(self.auth.status()["remote_revocation_unconfirmed"])

    def test_logout_rejects_untrusted_discovery_revocation_endpoint(self):
        self.transport.result = {"issuer": ISSUER, "revocation_endpoint": "https://evil.example/revoke"}
        self.auth.sign_out()
        self.assertEqual(len(self.transport.requests), 1)
        self.assertNotIn("refresh_token", self.current())

    def test_welcome_seen_is_per_registration(self):
        self.auth.mark_welcome_seen()
        self.assertTrue(self.auth.status()["welcome_seen"])
        self.auth.store.data["registrations"]["oaiapp_other"] = record("oaiapp_other", "fixture-other")
        self.auth.select_account("oaiapp_other")
        self.assertFalse(self.auth.status()["welcome_seen"])
        self.assertEqual(len(self.auth.accounts()), 2)

    def test_signin_account_mismatch_preserves_existing_active_session(self):
        attempt = LoginAttempt.create("http://127.0.0.1:1234/auth/callback", "oaiapp_fixture")
        self.transport.result = token_response(id_token="fixture-id")
        with patch.object(self.auth, "_receive_callback", return_value=("fixture-code", "oaiapp_fixture", attempt)), patch.object(self.auth.verifier, "available"), patch.object(self.auth.verifier, "verify", return_value={"sub": "different-subject"}):
            with self.assertRaises(ChatGPTAuthError) as caught:
                self.auth.sign_in()
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.assertEqual(self.current()["access_token"], "fixture-access-old")

    def test_signin_code_exchange_uses_callback_client_and_exact_uri(self):
        self.auth.store = MemoryStore()
        attempt = LoginAttempt.create("http://127.0.0.1:1234/auth/callback")
        self.transport.result = token_response(id_token="fixture-id")
        claims = {"sub": "fixture-subject", "email": "fixture@example.test", "exp": time.time() + 3600}
        with patch.object(self.auth, "_receive_callback", return_value=("fixture-code", "oaiapp_issued", attempt)), patch.object(self.auth.verifier, "available"), patch.object(self.auth.verifier, "verify", return_value=claims):
            self.auth.sign_in()
        form = self.transport.requests[0][1]
        self.assertEqual(form["client_id"], "oaiapp_issued")
        self.assertEqual(form["redirect_uri"], attempt.redirect_uri)
        self.assertEqual(form["code_verifier"], attempt.verifier)
        self.assertEqual(self.auth.status()["registration_id"], "oaiapp_issued")

    def test_invalid_grant_retry_retains_issued_client_without_unverified_identity(self):
        self.auth.store = MemoryStore()
        self.transport.result = ChatGPTAuthError("授权码过期。", "invalid_grant")
        first_attempt = LoginAttempt.create("http://127.0.0.1:1234/auth/callback")
        with patch.object(self.auth.verifier, "available"), patch.object(self.auth, "_receive_callback", return_value=("fixture-code-old", "oaiapp_issued", first_attempt)):
            with self.assertRaises(ChatGPTAuthError):
                self.auth.sign_in()
        self.assertEqual(self.auth.store.data["registrations"], {})
        self.assertEqual(self.auth._pending_client_id, "oaiapp_issued")
        self.transport.result = token_response(id_token="fixture-id")
        second_attempt = LoginAttempt.create("http://127.0.0.1:5432/auth/callback", "oaiapp_issued")
        claims = {"sub": "fixture-subject", "email": "fixture@example.test", "exp": time.time() + 3600}
        with patch.object(self.auth.verifier, "available"), patch.object(self.auth.verifier, "verify", return_value=claims), patch.object(self.auth, "_receive_callback", return_value=("fixture-code-new", "oaiapp_issued", second_attempt)) as callback:
            self.auth.sign_in()
        self.assertEqual(callback.call_args.args[1], {"client_id": "oaiapp_issued"})
        self.assertEqual(self.auth._pending_client_id, "")
        self.assertEqual(self.auth.status()["registration_id"], "oaiapp_issued")

    def test_invalid_client_does_not_retain_failed_dynamic_registration(self):
        self.auth.store = MemoryStore()
        self.transport.result = ChatGPTAuthError("应用无效。", "invalid_client")
        attempt = LoginAttempt.create("http://127.0.0.1:1234/auth/callback")
        with patch.object(self.auth.verifier, "available"), patch.object(self.auth, "_receive_callback", return_value=("fixture-code", "oaiapp_invalid", attempt)):
            with self.assertRaises(ChatGPTAuthError):
                self.auth.sign_in()
        self.assertEqual(self.auth._pending_client_id, "")
        self.assertEqual(self.auth.store.data["registrations"], {})

    def test_explicit_new_account_never_reuses_unverified_pending_old_registration(self):
        self.auth._pending_client_id = "oaiapp_pending_old"
        self.transport.result = token_response(id_token="fixture-new-id")
        attempt = LoginAttempt.create("http://127.0.0.1:1234/auth/callback")
        claims = {"sub": "fixture-new-subject", "email": "fixture@example.test", "exp": time.time() + 3600}
        with patch.object(self.auth.verifier, "available"), patch.object(self.auth.verifier, "verify", return_value=claims), patch.object(self.auth, "_receive_callback", return_value=("fixture-code", "oaiapp_new", attempt)) as callback:
            self.auth.sign_in(new_account=True)
        self.assertIsNone(callback.call_args.args[1])
        self.assertEqual(self.auth.status()["registration_id"], "oaiapp_new")
        self.assertEqual(self.auth.store.data["registrations"]["oaiapp_fixture"]["subject"], "fixture-subject")

    def test_identity_only_login_kept_but_plan_is_disabled(self):
        self.auth.store = MemoryStore()
        attempt = LoginAttempt.create("http://127.0.0.1:1234/auth/callback")
        self.transport.result = {"id_token": "fixture-id-only", "scope": "openid email profile"}
        claims = {"sub": "fixture-subject", "email": "fixture@example.test", "exp": time.time() + 3600}
        with patch.object(self.auth, "_receive_callback", return_value=("fixture-code", "oaiapp_fixture", attempt)), patch.object(self.auth.verifier, "available"), patch.object(self.auth.verifier, "verify", return_value=claims):
            status = self.auth.sign_in()
        self.assertTrue(status["signed_in"])
        self.assertFalse(status["plan_enabled"])
        with self.assertRaises(ChatGPTAuthError) as caught:
            self.auth.access_token()
        self.assertEqual(caught.exception.code, "plan_not_enabled")

    def test_cancelled_signin_never_opens_browser(self):
        event = threading.Event()
        event.set()
        with patch("chatgpt_auth.webbrowser.open") as browser:
            with self.assertRaises(ChatGPTAuthError):
                self.auth.sign_in(event)
            browser.assert_not_called()


class LoopbackTests(unittest.TestCase):
    def test_listener_exists_before_browser_and_rejects_wrong_state_then_accepts_real_callback(self):
        auth = ChatGPTAuth()
        failures = []
        replies = []
        workers = []
        def fake_browser(url, new=1):
            parts = urlsplit(url)
            self.assertEqual(parts.hostname, "auth.openai.com")
            query = parse_qs(parts.query)
            callback = urlsplit(query["redirect_uri"][0])
            self.assertEqual(callback.hostname, "127.0.0.1")
            self.assertEqual(callback.path, "/auth/callback")
            def send():
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", callback.port, timeout=3)
                    for state in ("wrong-state", query["state"][0]):
                        path = callback.path + "?" + urlencode({"state": state, "code": "fixture-callback-code", "client_id": "oaiapp_fixture"})
                        connection.request("GET", path)
                        reply = connection.getresponse()
                        body = reply.read().decode("utf-8")
                        replies.append(reply.status)
                        self.assertNotIn("fixture-callback-code", body)
                        self.assertEqual(reply.getheader("Cache-Control"), "no-store")
                    connection.close()
                except Exception as exc:
                    failures.append(type(exc).__name__)
            worker = threading.Thread(target=send)
            workers.append(worker)
            worker.start()
            return True
        with patch("chatgpt_auth.webbrowser.open", side_effect=fake_browser):
            code, client, attempt = auth._receive_callback("urn:uuid:" + str(uuid.uuid4()), None, time.monotonic() + 5, None)
        for worker in workers:
            worker.join(3)
        self.assertEqual(failures, [])
        self.assertEqual(replies, [400, 200])
        self.assertEqual((code, client), ("fixture-callback-code", "oaiapp_fixture"))
        self.assertTrue(attempt.redirect_uri.startswith("http://127.0.0.1:"))

    def test_callback_wait_can_be_cancelled_and_port_is_closed(self):
        auth = ChatGPTAuth()
        event = threading.Event()
        with patch("chatgpt_auth.webbrowser.open", side_effect=lambda *_a, **_k: event.set() or True):
            start = time.monotonic()
            with self.assertRaises(ChatGPTAuthError) as caught:
                auth._receive_callback("urn:uuid:" + str(uuid.uuid4()), None, time.monotonic() + 5, event)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertLess(time.monotonic() - start, 2)

    def test_slow_incomplete_http_headers_cannot_block_cancellation(self):
        auth = ChatGPTAuth()
        cancel = threading.Event()
        stop = threading.Event()
        workers = []
        def fake_browser(url, new=1):
            callback = urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0])
            def slow_header():
                try:
                    with socket.create_connection(("127.0.0.1", callback.port), timeout=2) as connection:
                        connection.sendall(b"GET /auth/callback HTTP/1.1\r\nX-Fixture: ")
                        while not stop.wait(.05):
                            connection.sendall(b"x")
                except OSError:
                    pass
            worker = threading.Thread(target=slow_header)
            worker.start()
            workers.append(worker)
            return True
        timer = threading.Timer(.2, cancel.set)
        timer.start()
        start = time.monotonic()
        try:
            with patch("chatgpt_auth.webbrowser.open", side_effect=fake_browser):
                with self.assertRaises(ChatGPTAuthError) as caught:
                    auth._receive_callback("urn:uuid:" + str(uuid.uuid4()), None, time.monotonic() + 5, cancel)
            self.assertEqual(caught.exception.code, "cancelled")
            self.assertLess(time.monotonic() - start, 1.5)
        finally:
            timer.cancel()
            stop.set()
            for worker in workers:
                worker.join(2)


@unittest.skipUnless(os.name == "nt", "Windows DPAPI required")
class WindowsStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="eastmoney-auth-test-")
        self.store = CredentialStore(Path(self.temporary.name) / "app-auth")

    def tearDown(self):
        self.temporary.cleanup()

    def test_actual_dpapi_roundtrip_and_no_plaintext_credentials_on_disk(self):
        data = state_with_record()
        with self.store.locked():
            self.store.save(data)
        binary = self.store.path.read_bytes()
        self.assertNotIn(b"fixture-access-old", binary)
        self.assertNotIn(b"fixture-refresh-old", binary)
        self.assertNotIn(b"fixture@example.test", binary)
        self.assertEqual(self.store.load(), data)
        self.assertEqual(list(self.store.directory.glob("*.tmp")), [])

    def test_dpapi_rejects_tampering_and_plaintext(self):
        dpapi = WindowsDPAPI()
        encrypted = dpapi.protect(b"fixture-private-record")
        self.assertEqual(dpapi.unprotect(encrypted), b"fixture-private-record")
        with self.assertRaises(ChatGPTAuthError):
            dpapi.unprotect(b"plaintext-is-not-a-credential-store")
        damaged = encrypted[:-8] + b"damaged!"
        with self.assertRaises(ChatGPTAuthError):
            dpapi.unprotect(damaged)

    def test_signout_clears_own_encrypted_tokens_but_preserves_host_and_mapping(self):
        data = state_with_record()
        with self.store.locked():
            self.store.save(data)
        auth = ChatGPTAuth(self.store.directory, transport=FixtureTransport(ChatGPTAuthError("offline", "network_error")))
        auth.sign_out()
        saved = self.store.load()
        self.assertEqual(saved["host_id"], data["host_id"])
        self.assertEqual(saved["registrations"]["oaiapp_fixture"]["subject"], "fixture-subject")
        self.assertNotIn("refresh_token", saved["registrations"]["oaiapp_fixture"])

    def test_cross_process_lock_blocks_another_refresh_and_is_cancellable(self):
        script = "from pathlib import Path; import sys; from chatgpt_auth import CredentialStore; store=CredentialStore(Path(sys.argv[1])); lock=store.locked(); lock.__enter__(); print('fixture-lock-ready', flush=True); sys.stdin.readline(); lock.__exit__(None,None,None)"
        process = subprocess.Popen([sys.executable, "-c", script, str(self.store.directory)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "fixture-lock-ready")
            event = threading.Event()
            timer = threading.Timer(.15, event.set)
            timer.start()
            try:
                with self.assertRaises(ChatGPTAuthError) as caught:
                    with self.store.locked(event):
                        self.fail("cross-process lock was not exclusive")
                self.assertEqual(caught.exception.code, "cancelled")
            finally:
                timer.cancel()
            process.stdin.write("finish\n")
            process.stdin.flush()
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
