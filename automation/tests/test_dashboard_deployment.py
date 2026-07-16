import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from automation.dashboard_deployment import (
    DASHBOARD_LABEL,
    PROXY_LABEL,
    DashboardDeploymentError,
    build_dashboard_plist,
    build_proxy_plist,
    render_caddyfile,
    render_dashboard_deployment,
)


HASH = "$2a$14$" + "A" * 53


class DashboardDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.caddy = self.root / "caddy"
        self.caddy.write_bytes(b"fixture-caddy")
        self.paths = {
            "python": Path("/opt/openpapers-shadow/venv/bin/python"),
            "runtime": Path("/opt/openpapers-shadow/runtime"),
            "state": Path("/var/db/openpapers-production/control/state.sqlite3"),
            "caddy": self.caddy,
            "installed_caddy": Path(
                "/opt/openpapers-shadow/bin/openpapers-dashboard-caddy"
            ),
            "deployed_root": Path("/var/db/openpapers-dashboard"),
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_documents_keep_backend_loopback_and_proxy_unprivileged(self):
        caddyfile = render_caddyfile(
            hostname="archer.cs.niu.edu",
            bind_address="10.158.56.37",
            public_port=8443,
            backend_port=8765,
            username="openpapers",
            password_hash=HASH,
        ).decode()
        dashboard = build_dashboard_plist(
            python=self.paths["python"],
            runtime=self.paths["runtime"],
            state=self.paths["state"],
            role_user="_openpapers",
            role_group="_openpapers",
            backend_port=8765,
        )
        proxy = build_proxy_plist(
            caddy=self.paths["installed_caddy"],
            caddyfile=self.paths["deployed_root"] / "Caddyfile",
            working_root=self.paths["deployed_root"],
            role_user="_openpapers",
            role_group="_openpapers",
        )

        self.assertIn("https://archer.cs.niu.edu:8443", caddyfile)
        self.assertIn("bind 10.158.56.37", caddyfile)
        self.assertIn("tls internal", caddyfile)
        self.assertIn("basic_auth", caddyfile)
        self.assertIn("reverse_proxy 127.0.0.1:8765", caddyfile)
        self.assertNotIn("0.0.0.0", caddyfile)
        self.assertEqual(dashboard["Label"], DASHBOARD_LABEL)
        self.assertEqual(proxy["Label"], PROXY_LABEL)
        self.assertEqual(dashboard["UserName"], "_openpapers")
        self.assertEqual(proxy["UserName"], "_openpapers")
        self.assertIn("127.0.0.1", dashboard["ProgramArguments"])
        self.assertNotIn("--write", dashboard["ProgramArguments"])
        self.assertEqual(proxy["ProgramArguments"][0],
                         str(self.paths["installed_caddy"]))

    def test_staging_is_private_canonical_and_password_free_in_manifest(self):
        staging = self.root / "staging"

        manifest = render_dashboard_deployment(
            staging,
            **self.paths,
            role_user="_openpapers",
            role_group="_openpapers",
            hostname="archer.cs.niu.edu",
            bind_address="10.158.56.37",
            public_port=8443,
            backend_port=8765,
            username="openpapers",
            password_hash=HASH,
        )

        retained = json.loads((staging / "manifest.json").read_text())
        self.assertEqual(retained, manifest)
        self.assertNotIn(HASH, json.dumps(retained))
        self.assertEqual((staging / "Caddyfile").stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            (staging / f"{DASHBOARD_LABEL}.plist").stat().st_mode & 0o777,
            0o644,
        )
        dashboard = plistlib.loads(
            (staging / f"{DASHBOARD_LABEL}.plist").read_bytes()
        )
        self.assertEqual(dashboard["Umask"], 0o077)
        with self.assertRaisesRegex(DashboardDeploymentError, "exists"):
            render_dashboard_deployment(
                staging,
                **self.paths,
                role_user="_openpapers",
                role_group="_openpapers",
                hostname="archer.cs.niu.edu",
                bind_address="10.158.56.37",
                public_port=8443,
                backend_port=8765,
                username="openpapers",
                password_hash=HASH,
            )

    def test_unsafe_network_identity_and_paths_are_rejected(self):
        kwargs = {
            "hostname": "archer.cs.niu.edu",
            "bind_address": "10.158.56.37",
            "public_port": 8443,
            "backend_port": 8765,
            "username": "openpapers",
            "password_hash": HASH,
        }
        for field, value in (
            ("bind_address", "0.0.0.0"),
            ("bind_address", "8.8.8.8"),
            ("hostname", "bad host"),
            ("public_port", 443),
            ("username", "bad user"),
            ("password_hash", "plaintext"),
        ):
            with self.subTest(field=field):
                invalid = dict(kwargs, **{field: value})
                with self.assertRaises(DashboardDeploymentError):
                    render_caddyfile(**invalid)
        with self.assertRaisesRegex(DashboardDeploymentError, "absolute"):
            build_dashboard_plist(
                python=Path("python"),
                runtime=self.paths["runtime"],
                state=self.paths["state"],
                role_user="_openpapers",
                role_group="_openpapers",
                backend_port=8765,
            )


if __name__ == "__main__":
    unittest.main()
