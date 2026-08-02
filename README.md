# m3u-tv-bin AUR auto updater

This repository maintains an Arch User Repository package template for the m3u-tv Linux binary release.

A scheduled GitHub Actions workflow checks a bounded `m3ue/m3u-tv` GitHub release list in API order. It skips draft, prerelease, and releases without a Linux build, selects the first published release with a Linux ZIP or legacy tar.gz asset, updates package metadata, validates the package in an Arch Linux container, and publishes the refreshed files to AUR when the AUR SSH secrets are configured. ZIP is preferred when both formats are available, and duplicate assets of the preferred format are rejected.

## What the workflow does

1. Fetches a bounded upstream GitHub release list in API order, or a custom single-release JSON object.
2. Selects the first published, non-draft, non-prerelease release with a `m3u-tv-*-linux.zip` or legacy `m3u-tv-*-linux.tar.gz` asset, preferring ZIP.
3. Extracts the version from the selected asset name or release tag.
4. Computes the release asset SHA256 checksum.
5. Updates `packages/m3u-tv-bin/PKGBUILD` and regenerates `.SRCINFO`.
6. Builds and checks the package in an Arch Linux environment.
7. Commits refreshed package files back to this repository.
8. Pushes the package to AUR if `AUR_SSH_KEY` and `AUR_SSH_KNOWN_HOSTS` are present.

## Repository layout

- `packages/m3u-tv-bin/` - AUR package template.
- `scripts/aur_update.py` - upstream release parser and package metadata updater.
- `scripts/publish_aur.py` - package publication helper used by the workflow.
- `tests/` - unit tests for release parsing and publication safety checks.

## Required GitHub secrets

- `AUR_SSH_KEY` - private SSH key for the AUR account that owns `m3u-tv-bin`.
- `AUR_SSH_KNOWN_HOSTS` - pinned `aur.archlinux.org` host key entries.
