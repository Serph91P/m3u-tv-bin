# pyright: reportMissingImports=false
import argparse
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import aur_update
import publish_aur


def workflow_steps() -> list[dict[str, object]]:
    workflow = (REPO / ".github" / "workflows" / "aur-auto-update.yml").read_text(
        encoding="utf-8"
    )
    steps: list[dict[str, object]] = []
    job = None
    current = None
    section = None
    run_indent = None

    for line in workflow.splitlines():
        job_match = re.match(r"^  ([a-zA-Z0-9_-]+):\s*$", line)
        if job_match:
            job = job_match.group(1)
        step_match = re.match(r"^      - name:\s*(.+?)\s*$", line)
        if step_match:
            current = {"job": job, "name": step_match.group(1), "env": {}, "run": []}
            steps.append(current)
            section = None
            run_indent = None
            continue
        if current is None:
            continue
        if line == "        env:":
            section = "env"
            continue
        run_match = re.match(r"^(\s*)run:\s*\|\s*$", line)
        if run_match:
            section = "run"
            run_indent = len(run_match.group(1)) + 2
            continue
        if section == "env":
            env_match = re.match(r"^          ([A-Z0-9_]+):\s*(.+?)\s*$", line)
            if env_match:
                current["env"][env_match.group(1)] = env_match.group(2)  # type: ignore[index]
            elif line.strip() and len(line) - len(line.lstrip()) <= 8:
                section = None
        elif section == "run" and (not line.strip() or len(line) - len(line.lstrip()) >= run_indent):
            current["run"].append(line[run_indent:])  # type: ignore[union-attr]
        elif section == "run":
            section = None

    return steps


def executable_shell_source(step: dict[str, object]) -> str:
    return "\n".join(
        str(line)
        for line in step["run"]  # type: ignore[union-attr]
        if str(line).strip() and not str(line).lstrip().startswith("#")
    )


class PublishSecurityTests(unittest.TestCase):
    def test_push_rejects_invalid_package_name_before_publication_setup(self):
        args = argparse.Namespace(
            package_name="../outside",
            package_dir=str(REPO / "packages" / "m3u-tv-bin"),
            aur_remote_template="ssh://aur@aur.archlinux.org/{package}.git",
            push_ssh_key="",
            ssh_known_hosts="",
            commit_email="actions@github.com",
            commit_name="AUR Update Bot",
            package_ver=None,
        )

        with self.assertRaisesRegex(ValueError, "invalid AUR package name"):
            publish_aur.push_package(args)

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

        for dep in ("gtk3", "libsecret", "mpv", "libepoxy"):
            self.assertIn(f"'{dep}'", pkgbuild)
        self.assertNotIn("java-runtime", pkgbuild)


    def test_pkgbuild_removes_unused_dart_jni_library(self):
        pkgbuild = (REPO / "packages" / "m3u-tv-bin" / "PKGBUILD").read_text(encoding="utf-8")

        self.assertIn("rm -f", pkgbuild)
        self.assertIn("libdartjni.so", pkgbuild)


class WorkflowTests(unittest.TestCase):
    def test_executable_shell_source_excludes_comments(self):
        step = {
            "run": [
                "# validate_single_line \"asset_regex\" \"$ASSET_REGEX\"",
                "validate_package_name \"$PACKAGE_NAME\"",
            ]
        }

        self.assertEqual(
            executable_shell_source(step),
            'validate_package_name "$PACKAGE_NAME"',
        )

    def test_asset_regex_defaults_match_updater(self):
        workflow = (REPO / ".github" / "workflows" / "aur-auto-update.yml").read_text(encoding="utf-8")
        shell_regex = aur_update.DEFAULT_ASSET_REGEX.replace("\\", "\\\\")

        self.assertIn(f"default: '{aur_update.DEFAULT_ASSET_REGEX}'", workflow)
        self.assertIn(f'ASSET_REGEX="{shell_regex}"', workflow)

    def test_shell_steps_bind_and_validate_untrusted_workflow_inputs(self):
        workflow = (REPO / ".github" / "workflows" / "aur-auto-update.yml").read_text(
            encoding="utf-8"
        )
        steps = workflow_steps()
        shell_steps = [step for step in steps if step["run"]]
        self.assertTrue(shell_steps)
        for step in shell_steps:
            shell_source = executable_shell_source(step)
            self.assertNotIn("${{ github.event.inputs.", shell_source)
            self.assertNotIn("${{ vars.", shell_source)

        resolve = [
            step
            for step in steps
            if step["job"] == "aur_update" and step["name"] == "Resolve workflow inputs"
        ]
        self.assertEqual(len(resolve), 1)
        env = resolve[0]["env"]
        self.assertEqual(env["INPUT_PACKAGE_NAME"], "${{ github.event.inputs.package_name }}")
        self.assertEqual(env["VARIABLE_PACKAGE_NAME"], "${{ vars.AUR_PACKAGE_NAME }}")
        self.assertEqual(env["INPUT_RELEASE_API_URL"], "${{ github.event.inputs.release_api_url }}")
        self.assertEqual(env["VARIABLE_RELEASE_API_URL"], "${{ vars.UPSTREAM_RELEASE_API_URL }}")
        self.assertEqual(env["INPUT_ASSET_REGEX"], "${{ github.event.inputs.asset_regex }}")
        self.assertEqual(env["VARIABLE_ASSET_REGEX"], "${{ vars.UPSTREAM_ASSET_REGEX }}")
        self.assertEqual(env["INPUT_RUN_BUILD"], "${{ github.event.inputs.run_build }}")
        self.assertEqual(env["INPUT_PUSH"], "${{ github.event.inputs.push }}")
        self.assertEqual(env["INPUT_FORCE_PUBLISH"], "${{ github.event.inputs.force_publish }}")

        untrusted_expressions = re.findall(
            r"\$\{\{\s*(?:github\.event\.inputs|vars)\.[^}]+\}\}", workflow
        )
        self.assertCountEqual(untrusted_expressions, [str(value) for value in env.values()])

        shell_source = executable_shell_source(resolve[0])
        protocol_write = shell_source.index("$GITHUB_ENV")
        validations = [
            'validate_package_name "$PACKAGE_NAME"',
            'validate_boolean "$BUILD"',
            'validate_boolean "$PUSH"',
            'validate_boolean "$FORCE_PUBLISH"',
            'validate_single_line "release_api_url" "$RELEASE_API_URL"',
            'validate_single_line "asset_regex" "$ASSET_REGEX"',
        ]
        for validation in validations:
            self.assertLess(shell_source.index(validation), protocol_write)

        resolve_index = steps.index(resolve[0])
        for name in (
            "Run update script as unprivileged user",
            "Commit package update back to GitHub",
            "Push update to AUR",
        ):
            use = [
                step
                for step in steps
                if step["job"] == "aur_update" and step["name"] == name
            ]
            self.assertEqual(len(use), 1)
            self.assertLess(resolve_index, steps.index(use[0]))

    def test_executable_dependencies_use_canonical_immutable_references(self):
        workflow = (REPO / ".github" / "workflows" / "aur-auto-update.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            re.findall(r"^\s+uses:\s*(\S+)", workflow, re.MULTILINE),
            ["actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"],
        )
        self.assertEqual(
            re.findall(r"^\s+image:\s*(\S+)", workflow, re.MULTILINE),
            [
                "archlinux:base-devel@sha256:"
                "40d14ac9db5af04f695eacd82a53181ad685fecc2534a66e05a51182a077cbd5"
            ],
        )

    def test_shell_steps_do_not_interpolate_github_expressions(self):
        shell_steps = [step for step in workflow_steps() if step["run"]]

        for step in shell_steps:
            self.assertNotIn("${{", executable_shell_source(step))

    def test_stale_reruns_refresh_and_retry_current_metadata_safely(self):
        workflow = (REPO / ".github" / "workflows" / "aur-auto-update.yml").read_text(
            encoding="utf-8"
        )
        steps = workflow_steps()

        self.assertRegex(
            workflow,
            r"(?m)^concurrency:\n"
            r"  group: aur-auto-update-\$\{\{ github\.ref \}\}\n"
            r"  cancel-in-progress: false$",
        )
        self.assertRegex(
            workflow,
            r"(?m)^      - name: Checkout\n"
            r"        uses: actions/checkout@[0-9a-f]{40}.*\n"
            r"        with:\n"
            r"          ref: \$\{\{ github\.ref \}\}$",
        )

        named_steps = {
            str(step["name"]): step
            for step in steps
            if step["job"] == "aur_update"
        }
        verify = named_steps["Verify selected branch tip"]
        verify_source = executable_shell_source(verify)
        verify_lines = verify_source.splitlines()
        self.assertIn('  refs/heads/*) ;;', verify_lines)
        self.assertIn('git fetch --no-tags origin "$GITHUB_REF"', verify_lines)
        self.assertIn('LOCAL_SHA=$(git rev-parse HEAD)', verify_lines)
        self.assertIn('REMOTE_SHA=$(git rev-parse FETCH_HEAD)', verify_lines)
        self.assertIn('if [ "$LOCAL_SHA" != "$REMOTE_SHA" ]; then', verify_lines)
        self.assertLess(
            steps.index(verify),
            steps.index(named_steps["Run update script as unprivileged user"]),
        )

        commit_source = executable_shell_source(
            named_steps["Commit package update back to GitHub"]
        )
        self.assertIn(
            'git push origin "HEAD:$GITHUB_REF"',
            commit_source.splitlines(),
        )
        self.assertNotIn("--force", commit_source)
        commit_block = re.search(
            r"(?ms)^      - name: Commit package update back to GitHub\n"
            r"(?P<body>.*?)(?=^      - name:|\Z)",
            workflow,
        )
        self.assertIsNotNone(commit_block)
        self.assertNotIn("continue-on-error:", commit_block.group("body"))
        self.assertLess(
            steps.index(named_steps["Commit package update back to GitHub"]),
            steps.index(named_steps["Push update to AUR"]),
        )

        build_condition = re.search(
            r"(?m)^      - name: Build and run checks as unprivileged user\n"
            r"        if: (.+)$",
            workflow,
        )
        publish_condition = re.search(
            r"(?m)^      - name: Push update to AUR\n        if: (.+)$",
            workflow,
        )
        self.assertIsNotNone(build_condition)
        self.assertIsNotNone(publish_condition)
        self.assertEqual(
            build_condition.group(1),
            "${{ (steps.result.outputs.changed == 'true' || "
            "env.FORCE_PUBLISH == 'true' || env.PUSH_TO_AUR == 'true') && "
            "env.RUN_BUILD == 'true' }}",
        )
        self.assertEqual(
            publish_condition.group(1),
            "${{ env.PUSH_TO_AUR == 'true' }}",
        )


class UpdateParsingTests(unittest.TestCase):
    def test_resolve_paths_rejects_invalid_package_name_before_path_use(self):
        args = argparse.Namespace(package_name="../outside", package_dir=None)

        with self.assertRaisesRegex(ValueError, "invalid AUR package name"):
            aur_update.resolve_paths(args)

    def test_detect_upstream_selects_zip_with_default_regex(self):
        payload = {
            "tag_name": "v1.0.7",
            "assets": [
                {
                    "name": "m3u-tv-v1.0.7-linux.zip",
                    "browser_download_url": "https://example.invalid/m3u-tv-v1.0.7-linux.zip",
                }
            ],
        }
        seen = []

        old_fetch = aur_update._fetch_json
        old_hash = aur_update._hash_streamed
        try:
            aur_update._fetch_json = lambda url, timeout: payload

            def fake_hash(url, timeout):
                seen.append((url, timeout))
                return "zipsha"

            aur_update._hash_streamed = fake_hash
            pkgver, source, checksum = aur_update.detect_upstream(
                "https://api.example.invalid/latest",
                aur_update.DEFAULT_ASSET_REGEX,
                9,
            )
        finally:
            aur_update._fetch_json = old_fetch
            aur_update._hash_streamed = old_hash

        self.assertEqual(pkgver, "1.0.7")
        self.assertEqual(
            source,
            "m3u-tv-1.0.7-linux.zip::https://example.invalid/m3u-tv-v1.0.7-linux.zip",
        )
        self.assertEqual(checksum, "zipsha")
        self.assertEqual(seen, [("https://example.invalid/m3u-tv-v1.0.7-linux.zip", 9)])

    def test_detect_upstream_prefers_zip_when_both_formats_are_present(self):
        payload = {
            "tag_name": "v1.0.7",
            "assets": [
                {
                    "name": "m3u-tv-v1.0.7-linux.tar.gz",
                    "browser_download_url": "https://example.invalid/m3u-tv-v1.0.7-linux.tar.gz",
                },
                {
                    "name": "m3u-tv-v1.0.7-linux.zip",
                    "browser_download_url": "https://example.invalid/m3u-tv-v1.0.7-linux.zip",
                },
            ],
        }
        seen = []

        old_fetch = aur_update._fetch_json
        old_hash = aur_update._hash_streamed
        try:
            aur_update._fetch_json = lambda url, timeout: payload

            def fake_hash(url, timeout):
                seen.append((url, timeout))
                return "zipsha"

            aur_update._hash_streamed = fake_hash
            pkgver, source, checksum = aur_update.detect_upstream(
                "https://api.example.invalid/latest",
                aur_update.DEFAULT_ASSET_REGEX,
                9,
            )
        finally:
            aur_update._fetch_json = old_fetch
            aur_update._hash_streamed = old_hash

        self.assertEqual(pkgver, "1.0.7")
        self.assertEqual(
            source,
            "m3u-tv-1.0.7-linux.zip::https://example.invalid/m3u-tv-v1.0.7-linux.zip",
        )
        self.assertEqual(checksum, "zipsha")
        self.assertEqual(seen, [("https://example.invalid/m3u-tv-v1.0.7-linux.zip", 9)])

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
                aur_update.DEFAULT_ASSET_REGEX,
                9,
            )
        finally:
            aur_update._fetch_json = old_fetch
            aur_update._hash_streamed = old_hash

        self.assertEqual(pkgver, "1.2.3")
        self.assertEqual(source, "m3u-tv-1.2.3-linux.tar.gz::https://example.invalid/linux.tar.gz")
        self.assertEqual(checksum, "abc123")
        self.assertEqual(seen, [("https://example.invalid/linux.tar.gz", 9)])

    def test_detect_upstream_skips_newer_release_without_linux_asset(self):
        payload = [
            {
                "tag_name": "v1.2.4",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "m3u-tv-v1.2.4-android.apk",
                        "browser_download_url": "https://example.invalid/android.apk",
                    }
                ],
            },
            {
                "tag_name": "v1.2.3",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "m3u-tv-v1.2.3-linux.tar.gz",
                        "browser_download_url": "https://example.invalid/linux.tar.gz",
                    }
                ],
            },
        ]
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
                "https://api.example.invalid/releases?per_page=20",
                aur_update.DEFAULT_ASSET_REGEX,
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
                {"name": "m3u-tv-v1.2.3-linux.zip", "browser_download_url": "https://example.invalid/one.zip"},
                {"name": "m3u-tv-v1.2.4-linux.zip", "browser_download_url": "https://example.invalid/two.zip"},
            ],
        }
        old_fetch = aur_update._fetch_json
        try:
            aur_update._fetch_json = lambda url, timeout: payload
            with self.assertRaisesRegex(RuntimeError, "multiple Linux release assets"):
                aur_update.detect_upstream(
                    "https://api.example.invalid/latest",
                    aur_update.DEFAULT_ASSET_REGEX,
                    9,
                )
        finally:
            aur_update._fetch_json = old_fetch

    def test_generated_pkgver_rejects_shell_syntax_outside_arch_alnum_dot_underscore_plus_grammar(self):
        unsafe_pkgver = "1.0.7$(touch${IFS}pkgver-executed)"
        payload = {
            "tag_name": "v1.0.7",
            "assets": [
                {
                    "name": f"m3u-tv-v{unsafe_pkgver}-linux.zip",
                    "browser_download_url": "https://example.invalid/m3u-tv-linux.zip",
                }
            ],
        }
        asset_regex = r"m3u-tv-v(?P<version>.+)-linux\.(?P<archive>zip)$"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkgbuild = root / "PKGBUILD"
            marker = root / "pkgver-executed"
            pkgbuild.write_text(
                "pkgver=1.0.6\n"
                "source=('https://example.invalid/old.zip')\n"
                "sha256sums=('oldsha')\n",
                encoding="utf-8",
            )

            detected_error = None
            old_fetch = aur_update._fetch_json
            old_hash = aur_update._hash_streamed
            try:
                aur_update._fetch_json = lambda url, timeout: payload
                aur_update._hash_streamed = lambda url, timeout: "newsha"
                try:
                    detected = aur_update.detect_upstream(
                        "https://api.example.invalid/latest", asset_regex, 9
                    )
                except RuntimeError as error:
                    detected_error = error
                    detected = (
                        unsafe_pkgver,
                        "https://example.invalid/m3u-tv-linux.zip",
                        "newsha",
                    )
            finally:
                aur_update._fetch_json = old_fetch
                aur_update._hash_streamed = old_hash

            update_error = None
            try:
                aur_update.update_pkgbuild(pkgbuild, *detected, dry_run=False)
            except RuntimeError as error:
                update_error = error

            subprocess.run(
                ["bash", "-c", 'source "$1"', "bash", pkgbuild.name],
                cwd=root,
                check=True,
            )

            self.assertIsNotNone(detected_error)
            self.assertRegex(str(detected_error), "invalid pkgver")
            self.assertIsNotNone(update_error)
            self.assertRegex(str(update_error), "invalid pkgver")
            self.assertFalse(marker.exists())

    def test_replace_array_preserves_single_line_style(self):
        lines = ["source=('old')\n", "sha256sums=('0')\n"]

        changed, old = aur_update._replace_array_first(lines, "source", "new-value")

        self.assertTrue(changed)
        self.assertEqual(old, ("old",))
        self.assertEqual(lines[0], "source=('new-value')\n")

    def test_extract_array_ignores_parentheses_inside_quotes_and_comments(self):
        lines = [
            "source=(\n",
            "  'archive::https://example.invalid/release(1).zip'\n",
            "  # old mirror )\n",
            "  'fallback::https://example.invalid/release.zip'\n",
            ")\n",
        ]

        tokens, start, end = aur_update._extract_array(lines, "source")

        self.assertEqual(
            tokens,
            [
                "archive::https://example.invalid/release(1).zip",
                "fallback::https://example.invalid/release.zip",
            ],
        )
        self.assertEqual((start, end), (0, 4))

    def test_extract_array_preserves_unquoted_url_fragment_and_closes_before_next_array(self):
        lines = [
            "source=(https://host/file#fragment)\n",
            "sha256sums=('newsha')\n",
        ]

        tokens, start, end = aur_update._extract_array(lines, "source")

        self.assertEqual(tokens, ["https://host/file#fragment"])
        self.assertEqual((start, end), (0, 0))

    def test_generated_single_line_source_keeps_shell_payload_inert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkgbuild = root / "PKGBUILD"
            sentinel = root / "sentinel"
            source = "archive::https://example.invalid/release'$(touch \"$PWD/sentinel\")'.zip"
            pkgbuild.write_text(
                "pkgver=1.0.0\n"
                "source=('archive::https://example.invalid/old.zip')\n"
                "sha256sums=('oldsha')\n",
                encoding="utf-8",
            )

            aur_update.update_pkgbuild(pkgbuild, "1.0.1", source, "newsha", False)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; printf "%s" "${source[0]}"',
                    "bash",
                    pkgbuild.name,
                ],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertFalse(sentinel.exists())
            self.assertEqual(result.stdout, source)

    def test_update_pkgbuild_preserves_quoted_url_fragment_on_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkgbuild = Path(tmp) / "PKGBUILD"
            source = "archive::https://example.invalid/release.zip#fragment"
            pkgbuild.write_text(
                "pkgver=1.0.0\n"
                "source=('archive::https://example.invalid/old.zip')\n"
                "sha256sums=('oldsha')\n",
                encoding="utf-8",
            )

            aur_update.update_pkgbuild(pkgbuild, "1.0.1", source, "newsha", False)
            second = aur_update.update_pkgbuild(pkgbuild, "1.0.1", source, "newsha", False)

            self.assertFalse(second.changed)
            self.assertEqual(second.old_source, source)

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
