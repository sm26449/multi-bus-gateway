# Releasing

How a Multi-Bus Gateway release is cut. Every step is a plain git operation;
the images are built by CI from the tag.

## What a release is

- A version `X.Y.Z` in `multibus/__init__.py` (`__version__`), the number the
  UI shows, `/api/status` reports and `config.yaml` is stamped with
  (`config_version`, see the upgrade guide).
- A `## X.Y.Z` section at the top of `CHANGELOG.md` with a dated
  `### YYYY-MM-DD — title` sub-heading per change set (that is the format the
  whole file uses).
- A git tag `vX.Y.Z` on `main`. Pushing the tag triggers
  `.github/workflows/release.yml`, which builds and pushes two multi-arch
  images (linux/amd64 + linux/arm64) to the GitHub Container Registry:
  - `ghcr.io/sm26449/multi-bus-gateway` — tags `X.Y.Z`, `X.Y`, `latest`
  - `ghcr.io/sm26449/multi-bus-gateway-serial-bridge` — tags `X.Y.Z`, `X.Y`, `latest`
- A GitHub Release with the changelog section as its notes.

Versioning is semantic within the 3.x line: a fix → patch, a feature or a
compatible behaviour change → minor. Anything that changes the config
contract or the MQTT/Influx output shape is called out at the top of the
changelog section as **breaking** and documented in `docs/upgrade-guide.md`.

## Steps

1. `main` is green: `ruff check .` and the test suite pass (CI runs both on
   every push; the coverage gate is in `.github/workflows/ci.yml`).
2. Bump `__version__` in `multibus/__init__.py`.
3. Write the `CHANGELOG.md` section. Say what changed for a user of the
   previous version, what they must do (if anything), and why.
4. Commit: `chore(release): X.Y.Z` (or the feature commit itself when it is
   the only change).
5. Tag and push:

   ```bash
   git tag -a vX.Y.Z -m "Multi-Bus Gateway X.Y.Z"
   git push origin main --tags
   ```

6. Wait for the *Release image* workflow (Actions tab). It takes a few
   minutes; arm64 is built under QEMU.
7. Verify the image, on an amd64 host and on an arm64 one if you can:

   ```bash
   docker pull ghcr.io/sm26449/multi-bus-gateway:X.Y.Z
   docker run --rm ghcr.io/sm26449/multi-bus-gateway:X.Y.Z python -c "import multibus; print(multibus.__version__)"
   ```

8. Create the GitHub Release for the tag (Releases → Draft a new release →
   pick `vX.Y.Z`, paste the changelog section).

## After the release

- Users on the published image upgrade with
  `MBG_VERSION=X.Y.Z docker compose pull && docker compose up -d`; the
  upgrade guide covers pinning and rolling back by tag.
- A pre-release (`vX.Y.Z-rc1`) also publishes images; only tag one when you
  want testers on it — `latest` moves only with non-prerelease tags (the workflow gates it).

## If a release is wrong

Do not delete a tag that users may have pulled. Cut the fix as the next
patch version; if the image itself is broken (does not start), also point
people at the previous tag in the release notes.
