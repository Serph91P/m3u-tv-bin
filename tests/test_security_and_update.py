# pyright: reportMissingImports=false
import argparse
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import aur_update
import publish_aur


class PublishSecurityTests(unittest.TestCase):
    def test_push_requires_known_hosts_when_key_is_present(self):
        args = argparse.Namespace(
            package_name="m3u-tv-bin",
            package_dir=str(REPO / "packages" / "m3u-tv-bin"),
            aur_remote_template="ssh://aur@aur.archlinux.org/{package}.git",
            push_ssh_key="dummy-key",
            ssh_known_hosts="",
            commit_email="actions@github.com",
            commit_name="AUR Update Bot",
            package_ver=None,
        )

        with self.assertRaisesRegex(RuntimeError, "AUR_SSH_KNOWN_HOSTS"):
            publish_aur.push_package(args)

    def test_git_ssh_command_uses_strict_host_key_checking_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = publish_aur.build_ssh_env(
                base_env={},
                key_path=Path(tmp) / "id_ed25519",
                known_hosts_path=Path(tmp) / "known_hosts",
            )

        command = env["GIT_SSH_COMMAND"]
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("UserKnownHostsFile=", command)
        self.assertNotIn("StrictHostKeyChecking=no", command)


class PkgbuildTests(unittest.TestCase):
    def test_pkgbuild_installs_wrapper_with_ld_library_path(self):
        pkgbuild = (REPO / "packages" / "m3u-tv-bin" / "PKGBUILD").read_text(encoding="utf-8")

        self.assertIn("/opt/m3u-tv", pkgbuild)
        self.assertIn("LD_LIBRARY_PATH", pkgbuild)
        self.assertIn("exec ./m3u_tv", pkgbuild)

    def test_pkgbuild_contains_runtime_dependencies_seen_in_linux_archive(self):
        pkgbuild = (REPO / "packages" / "m3u-tv-bin" / "PKGBUILD").read_text(encoding="utf-8")

        for dep in ("gtk3", "libsecret", "mpv", "libepoxy", "java-runtime"):
            self.assertIn(f"'{dep}'", pkgbuild)


class UpdateParsingTests(unittest.TestCase):
    def test_detect_upstream_selects_linux_asset_and_hashes_it(self):
        payload = {
            "tag_name": "v1.2.3",
            "assets": [
                {"name": "m3u-tv-v1.2.3-windows.zip", "browser_download_url": "https://example.invalid/windows.zip"},
                {"name": "m3u-tv-v1.2.3-linux.tar.gz", "browser_download_url": "https://example.invalid/linux.tar.gz"},
            ],
        }
        seen = []

        old_fetch = aur_update._fetch_json
        old_hash = aur_update._hash_streamed
        try:
            aur_update._fetch_json = lambda url, timeout: payload

            def fake_hash(url, timeout):
                seen.append((url, timeout))
                return "abc123"

            aur_update._hash_streamed = fake_hash
            pkgver, source, checksum = aur_update.detect_upstream(
                "https://api.example.invalid/latest",
                r"m3u-tv-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-linux\.tar\.gz$",
                9,
            )
        finally:
            aur_update._fetch_json = old_fetch
            aur_update._hash_streamed = old_hash

        self.assertEqual(pkgver, "1.2.3")
        self.assertEqual(source, "m3u-tv-1.2.3-linux.tar.gz::https://example.invalid/linux.tar.gz")
        self.assertEqual(checksum, "abc123")
        self.assertEqual(seen, [("https://example.invalid/linux.tar.gz", 9)])

    def test_detect_upstream_fails_when_multiple_linux_assets_match(self):
        payload = {
            "tag_name": "v1.2.3",
            "assets": [
                {"name": "m3u-tv-v1.2.3-linux.tar.gz", "browser_download_url": "https://example.invalid/one.tar.gz"},
                {"name": "m3u-tv-v1.2.4-linux.tar.gz", "browser_download_url": "https://example.invalid/two.tar.gz"},
            ],
        }
        old_fetch = aur_update._fetch_json
        try:
            aur_update._fetch_json = lambda url, timeout: payload
            with self.assertRaisesRegex(RuntimeError, "multiple Linux release assets"):
                aur_update.detect_upstream(
                    "https://api.example.invalid/latest",
                    r"m3u-tv-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-linux\.tar\.gz$",
                    9,
                )
        finally:
            aur_update._fetch_json = old_fetch

    def test_replace_array_preserves_single_line_style(self):
        lines = ["source=('old')\n", "sha256sums=('0')\n"]

        changed, old = aur_update._replace_array_first(lines, "source", "new-value")

        self.assertTrue(changed)
        self.assertEqual(old, ("old",))
        self.assertEqual(lines[0], "source=('new-value')\n")

    def test_run_updates_pkgbuild_before_generating_srcinfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkgbuild = root / "PKGBUILD"
            pkgbuild.write_text(
                "pkgname='m3u-tv-bin'\n"
                "pkgver=1.0.0\n"
                "source=('m3u-tv-1.0.0-linux.tar.gz::https://example.invalid/old.tar.gz')\n"
                "sha256sums=('oldsha')\n",
                encoding="utf-8",
            )
            generated = []

            old_detect = aur_update.detect_upstream
            old_generate = aur_update.generate_srcinfo
            try:
                aur_update.detect_upstream = lambda release_api_url, asset_regex, timeout: (
                    "1.2.3",
                    "m3u-tv-1.2.3-linux.tar.gz::https://example.invalid/new.tar.gz",
                    "newsha",
                )

                def fake_generate(package_dir: Path, srcinfo_command: str, dry_run: bool):
                    generated.append(pkgbuild.read_text(encoding="utf-8"))
                    return True

                aur_update.generate_srcinfo = fake_generate
                args = argparse.Namespace(
                    package_name="m3u-tv-bin",
                    package_dir=str(root),
                    release_api_url="https://api.example.invalid/latest",
                    asset_regex="linux",
                    srcinfo_command="makepkg",
                    dry_run=False,
                    json=False,
                    timeout=5,
                )

                result = aur_update.run(args)
            finally:
                aur_update.detect_upstream = old_detect
                aur_update.generate_srcinfo = old_generate

        self.assertTrue(result.changed)
        self.assertEqual(result.new_pkgver, "1.2.3")
        self.assertEqual(len(generated), 1)
        self.assertIn("pkgver=1.2.3", generated[0])
        self.assertIn("newsha", generated[0])


if __name__ == "__main__":
    unittest.main()
