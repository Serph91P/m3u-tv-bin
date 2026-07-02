# m3u-tv-bin AUR auto updater

This repository maintains an Arch User Repository package template for the m3u-tv Linux binary release.

A scheduled GitHub Actions workflow checks the latest `m3ue/m3u-tv` GitHub release, updates package metadata when a new Linux build is available, validates the package in an Arch Linux container, and publishes the refreshed files to AUR when the AUR SSH secrets are configured.

## What the workflow does

1. Fetches the latest upstream GitHub release metadata.
2. Selects the `m3u-tv-*-linux.tar.gz` release asset.
3. Extracts the version from the asset name or release tag.
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
