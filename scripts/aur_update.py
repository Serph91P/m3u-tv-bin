#!/usr/bin/env python3
"""Maintain m3u-tv-bin AUR package metadata from GitHub releases."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.request import Request, urlopen


DEFAULT_RELEASE_API_URL = "https://api.github.com/repos/m3ue/m3u-tv/releases/latest"
DEFAULT_ASSET_REGEX = r"m3u-tv-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-linux\.tar\.gz$"
UA = "m3u-tv-bin-aur-auto-updater/1.0"


@dataclass
class UpdateResult:
    package_name: str
    package_dir: Path
    old_pkgver: str | None
    new_pkgver: str
    old_source: str | None
    new_source: str
    old_sha256sums: tuple[str, ...]
    new_sha256: str
    changed: bool
    srcinfo_generated: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update m3u-tv-bin AUR package metadata.")
    parser.add_argument(
        "--package-name",
        default=os.getenv("AUR_PACKAGE_NAME", "m3u-tv-bin"),
        help="AUR package name, also used for package subfolder",
    )
    parser.add_argument(
        "--package-dir",
        default=None,
        help="Path to package directory, defaults to packages/<AUR_PACKAGE_NAME>",
    )
    parser.add_argument(
        "--release-api-url",
        default=os.getenv("UPSTREAM_RELEASE_API_URL", DEFAULT_RELEASE_API_URL),
        help="GitHub API URL for the latest upstream release JSON",
    )
    parser.add_argument(
        "--asset-regex",
        default=os.getenv("UPSTREAM_ASSET_REGEX", DEFAULT_ASSET_REGEX),
        help="Regex applied to release asset names to select the Linux archive and extract version",
    )
    parser.add_argument(
        "--srcinfo-command",
        default=os.getenv("SRCINFO_COMMAND", "makepkg"),
        help="Command used to generate .SRCINFO",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only report planned changes without writing files")
    parser.add_argument("--json", action="store_true", help="Write JSON summary to stdout")
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("HTTP_TIMEOUT", "30")),
        help="Timeout in seconds for upstream HTTP requests",
    )
    return parser.parse_args()


def _to_path(value: str | None) -> Path:
    if not value:
        raise ValueError("path is empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    repo_root = Path(__file__).resolve().parents[1]
    package_dir = _to_path(args.package_dir) if args.package_dir else repo_root / "packages" / args.package_name
    pkgbuild_path = package_dir / "PKGBUILD"
    srcinfo_path = package_dir / ".SRCINFO"
    if not pkgbuild_path.exists():
        raise FileNotFoundError(f"PKGBUILD missing: {pkgbuild_path}")
    return pkgbuild_path, srcinfo_path


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _fetch_json(url: str, timeout: int) -> dict[str, object]:
    request = Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"}, method="GET")
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("release API did not return a JSON object")
    return payload


def _hash_streamed(url: str, timeout: int) -> str:
    h = sha256()
    request = Request(url, headers={"User-Agent": UA}, method="GET")
    with urlopen(request, timeout=timeout) as response:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _release_assets(payload: dict[str, object]) -> list[dict[str, object]]:
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("release payload is missing assets")
    return [asset for asset in assets if isinstance(asset, dict)]


def detect_upstream(release_api_url: str, asset_regex: str, timeout: int) -> tuple[str, str, str]:
    payload = _fetch_json(release_api_url, timeout)
    pattern = re.compile(asset_regex)
    matches: list[tuple[dict[str, object], re.Match[str]]] = []
    for asset in _release_assets(payload):
        name = str(asset.get("name") or "")
        match = pattern.search(name)
        if match:
            matches.append((asset, match))

    if not matches:
        names = ", ".join(str(asset.get("name") or "") for asset in _release_assets(payload))
        raise RuntimeError(f"no Linux release asset matched {asset_regex!r}; assets: {names}")
    if len(matches) > 1:
        names = ", ".join(str(asset.get("name") or "") for asset, _ in matches)
        raise RuntimeError(f"multiple Linux release assets matched {asset_regex!r}: {names}")

    asset, match = matches[0]
    name = str(asset.get("name") or "")
    url = str(asset.get("browser_download_url") or "")
    if not url:
        raise RuntimeError(f"release asset {name} has no browser_download_url")

    version = match.groupdict().get("version")
    if not version:
        tag = str(payload.get("tag_name") or "")
        version = tag[1:] if tag.startswith("v") else tag
    if not version:
        raise RuntimeError(f"could not extract version from asset {name}")

    source = f"m3u-tv-{version}-linux.tar.gz::{url}"
    checksum = _hash_streamed(url, timeout)
    return version, source, checksum


def _extract_field(lines: list[str], field: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(field)}\s*=")
    for line in lines:
        if pattern.match(line):
            value = line.split("=", 1)[1].strip()
            if value.startswith(("'", '"')) and value.endswith(value[0]):
                return value[1:-1]
            return value
    return None


def _replace_scalar(lines: list[str], field: str, value: str) -> tuple[bool, str | None]:
    regex = re.compile(rf"^(?P<indent>\s*){re.escape(field)}\s*=\s*(?P<value>.+?)(?P<trailing>\s*(?:#.*)?)$")
    for idx, line in enumerate(lines):
        match = regex.match(line.rstrip("\n"))
        if not match:
            continue
        old = match.group("value").strip()
        if old.startswith(("'", '"')) and old.endswith(old[0]):
            old = old[1:-1]
        new_line = f"{match.group('indent')}{field}={value}{match.group('trailing')}\n"
        if old == value:
            return False, old
        lines[idx] = new_line
        return True, old
    raise ValueError(f"Field {field} not found")


def _extract_array(lines: list[str], field: str) -> tuple[list[str], int, int]:
    open_re = re.compile(rf"^\s*{re.escape(field)}\s*=\(")
    for idx, line in enumerate(lines):
        if not open_re.match(line):
            continue
        block_lines: list[str] = []
        end_idx = idx
        while end_idx < len(lines):
            block_lines.append(lines[end_idx])
            if ")" in lines[end_idx]:
                break
            end_idx += 1
        else:
            raise ValueError(f"Array field {field} has no closing )")
        block_text = "".join(block_lines)
        before_comment = block_text.split("#", 1)[0]
        open_idx = before_comment.find("(")
        close_idx = before_comment.rfind(")")
        if open_idx == -1 or close_idx == -1 or close_idx < open_idx:
            raise ValueError(f"Malformed array field {field}")
        tokens = shlex.split(before_comment[open_idx + 1 : close_idx])
        return tokens, idx, end_idx
    raise ValueError(f"Array field {field} not found")


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _replace_array_first(lines: list[str], field: str, new_value: str) -> tuple[bool, tuple[str, ...]]:
    tokens, start, end = _extract_array(lines, field)
    old_tokens = tuple(tokens)
    if tokens:
        tokens[0] = new_value
    else:
        tokens = [new_value]
    if old_tokens and old_tokens[0] == new_value:
        return False, old_tokens

    if start == end:
        quote = '"' if '"' in lines[start].split("(", 1)[1].split(")", 1)[0] else "'"
        rendered = " ".join(f"{quote}{token}{quote}" for token in tokens)
        prefix = lines[start].split("=", 1)[0]
        lines[start] = f"{prefix}=({rendered})\n"
    else:
        indent = re.match(r"^(\s*)", lines[start]).group(1)  # type: ignore[union-attr]
        rendered = "\n".join(f"{indent}  {_quote(token)}" for token in tokens)
        lines[start : end + 1] = [f"{indent}{field}=(\n", f"{rendered}\n", f"{indent})\n"]
    return True, old_tokens


def update_pkgbuild(pkgbuild_path: Path, new_pkgver: str, new_source: str, new_sha256: str, dry_run: bool) -> UpdateResult:
    lines = _read_text(pkgbuild_path).splitlines(keepends=True)
    old_pkgver = _extract_field(lines, "pkgver")
    source_tokens, _, _ = _extract_array(lines, "source")
    sha_tokens, _, _ = _extract_array(lines, "sha256sums")

    changed_pkgver, _ = _replace_scalar(lines, "pkgver", new_pkgver)
    changed_source, old_source = _replace_array_first(lines, "source", new_source)
    changed_sha, old_sha = _replace_array_first(lines, "sha256sums", new_sha256)
    changed = changed_pkgver or changed_source or changed_sha

    if changed and not dry_run:
        _write_text(pkgbuild_path, "".join(lines))

    return UpdateResult(
        package_name=pkgbuild_path.parent.name,
        package_dir=pkgbuild_path.parent,
        old_pkgver=old_pkgver,
        new_pkgver=new_pkgver,
        old_source=old_source[0] if old_source else (source_tokens[0] if source_tokens else None),
        new_source=new_source,
        old_sha256sums=old_sha if old_sha else tuple(sha_tokens),
        new_sha256=new_sha256,
        changed=changed,
        srcinfo_generated=False,
    )


def generate_srcinfo(package_dir: Path, srcinfo_command: str, dry_run: bool) -> bool:
    if dry_run:
        return False
    result = subprocess.run(
        [srcinfo_command, "--printsrcinfo"],
        cwd=package_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    (package_dir / ".SRCINFO").write_text(result.stdout, encoding="utf-8")
    return True


def run(args: argparse.Namespace) -> UpdateResult:
    pkgbuild_path, _ = resolve_paths(args)
    new_pkgver, new_source, new_sha256 = detect_upstream(args.release_api_url, args.asset_regex, args.timeout)
    result = update_pkgbuild(pkgbuild_path, new_pkgver, new_source, new_sha256, args.dry_run)
    if result.changed:
        result.srcinfo_generated = generate_srcinfo(pkgbuild_path.parent, args.srcinfo_command, args.dry_run)
    return result


def result_to_dict(result: UpdateResult) -> dict[str, object]:
    return {
        "package_name": result.package_name,
        "package_dir": result.package_dir.as_posix(),
        "old_pkgver": result.old_pkgver,
        "new_pkgver": result.new_pkgver,
        "old_source": result.old_source,
        "new_source": result.new_source,
        "old_sha256sums": list(result.old_sha256sums),
        "new_sha256": result.new_sha256,
        "changed": result.changed,
        "srcinfo_generated": result.srcinfo_generated,
    }


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except Exception as e:
        if args.json:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            print(f"error: {e}")
        return 1

    payload = result_to_dict(result)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
