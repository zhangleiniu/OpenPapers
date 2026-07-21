import unittest

from automation.domain import (
    ArtifactKind,
    OwnershipError,
    SecretBoundaryError,
    Writer,
    assert_secret_free,
    assert_writer_allowed,
)


class DomainBoundaryTests(unittest.TestCase):
    def test_control_state_has_only_control_plane_writers(self):
        assert_writer_allowed(Writer.CLOUD_CONTROL_PLANE, ArtifactKind.CONTROL_STATE)
        assert_writer_allowed(Writer.LOCAL_CONTROL_PLANE, ArtifactKind.CONTROL_STATE)
        with self.assertRaises(OwnershipError):
            assert_writer_allowed(Writer.MAC_WORKER, ArtifactKind.CONTROL_STATE)

    def test_unknown_writer_or_artifact_fails_closed(self):
        with self.assertRaises(OwnershipError):
            assert_writer_allowed("unknown", ArtifactKind.CONTROL_STATE)
        with self.assertRaises(OwnershipError):
            assert_writer_allowed(Writer.LOCAL_CONTROL_PLANE, "job_result")

    def test_credential_shaped_fields_are_rejected_recursively(self):
        assert_secret_free({"safe": [{"value": "not inspected as a secret"}]})
        for payload in (
            {"api_key": "value"},
            {"nested": {"smtp-password": "value"}},
            [{"authorization_header": "value"}],
        ):
            with self.assertRaises(SecretBoundaryError):
                assert_secret_free(payload)


if __name__ == "__main__":
    unittest.main()
