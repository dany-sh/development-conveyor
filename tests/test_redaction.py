import unittest

from development_conveyor.redaction import REDACTED, redact_text, redact_value


class RedactionTests(unittest.TestCase):
    def test_tokens_headers_cookies_keys_and_sensitive_queries_are_redacted(self):
        value = (
            "Authorization: Bearer abcdef123456 cookie=session123 api_key=secret123 "
            "https://example.test/path?token=secret&safe=value"
        )
        redacted = redact_text(value)
        self.assertNotIn("abcdef123456", redacted)
        self.assertNotIn("session123", redacted)
        self.assertNotIn("secret123", redacted)
        self.assertNotIn("token=secret", redacted)
        self.assertIn("safe=value", redacted)

    def test_structured_secret_fields_and_user_workspace_paths_are_redacted(self):
        value = {
            "password": "do-not-log",
            "message": "/Users/person/Library/CloudStorage/Provider/private/file.txt failed",
            "context": "kept",
        }
        redacted = redact_value(value)
        self.assertEqual(redacted["password"], REDACTED)
        self.assertNotIn("private/file.txt", redacted["message"])
        self.assertEqual(redacted["context"], "kept")

    def test_authentication_challenge_metadata_is_redacted(self):
        value = (
            'ERROR AuthRequired(AuthRequiredError { www_authenticate_header: "Bearer '
            'resource_metadata=\\"https://mcp.example/.well-known/oauth-protected-resource\\"" })'
        )
        redacted = redact_text(value)
        self.assertEqual(redacted, "[REDACTED_AUTH_CHALLENGE]")
        self.assertNotIn("resource_metadata", redacted)
