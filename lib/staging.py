"""
staging.py — stage the prebuilt binary bundle from a UC Volume on cold start.

WHY THIS EXISTS: Databricks Apps caps every *source* file at 10 MB, but the
Grafana binary alone is ~360 MB. So the bundle cannot ship inside the app
source. Instead it is staged once as a single gzipped tarball in a Unity
Catalog Volume, and downloaded + extracted to the local `bin/` directory the
first time the app container boots (cold start). Subsequent boots of the same
container find `bin/` already populated and skip the download.

The tarball layout mirrors what scripts/fetch_binaries.sh produces under bin/:
    grafana/bin/grafana   stunnel   pgbouncer   psql   lib/*.so*

so extracting it into `dest_dir` (== the app's bin/) yields the exact tree the
supervisor and LD_LIBRARY_PATH already expect.
"""
from __future__ import annotations

import logging
import os
import tarfile
from pathlib import Path

log = logging.getLogger("staging")

# Relative path (inside the bundle) whose presence means staging already
# happened. The Grafana binary is the largest, last-written, load-bearing file,
# so its presence is a good "fully staged" sentinel.
DEFAULT_MARKER_REL = "grafana/bin/grafana"

# Local filename for the downloaded tarball before extraction.
DEFAULT_TARBALL_NAME = "bin.tar.gz"


def is_staged(dest_dir: str, marker_rel: str = DEFAULT_MARKER_REL) -> bool:
    """Return True if the bundle already appears staged under dest_dir."""
    return (Path(dest_dir) / marker_rel).is_file()


def _assert_safe_members(tar: tarfile.TarFile, dest_dir: str) -> None:
    """Reject any tar member that would extract outside dest_dir.

    Guards against path-traversal (`../`) and absolute-path members in an
    untrusted-looking archive. We control the tarball, but defence-in-depth is
    cheap and the alternative (a poisoned member writing over /etc) is not.
    """
    dest = Path(dest_dir).resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if target != dest and dest not in target.parents:
            raise ValueError(
                f"unsafe tar member '{member.name}' would extract outside {dest}"
            )
        # Symlinks/hardlinks can also escape; reject link targets that leave dest.
        if member.issym() or member.islnk():
            link_target = (target.parent / member.linkname).resolve()
            if link_target != dest and dest not in link_target.parents:
                raise ValueError(
                    f"unsafe link member '{member.name}' -> '{member.linkname}'"
                )


def stage_binaries(
    client,
    volume_path: str,
    dest_dir: str,
    *,
    marker_rel: str = DEFAULT_MARKER_REL,
    tarball_name: str = DEFAULT_TARBALL_NAME,
) -> bool:
    """Download the bundle tarball from a UC Volume and extract it into dest_dir.

    Idempotent: if the bundle is already staged (marker present), returns False
    without touching the network. On a real cold start it downloads
    `volume_path` (e.g. /Volumes/cat/schema/binaries/bin.tar.gz) to a temp file
    inside dest_dir, extracts it, removes the tarball, and returns True.

    `client` must expose `files.download_to(remote_path, local_path,
    overwrite=...)` (Databricks SDK WorkspaceClient.files). Kept as a duck-typed
    parameter so tests can pass a fake.

    Raises on download/extraction failure or if the marker is still missing
    after extraction (fail-fast: a half-staged bundle must not boot).
    """
    if is_staged(dest_dir, marker_rel):
        log.info("Bundle already staged at %s (%s present); skipping download",
                 dest_dir, marker_rel)
        return False

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    local_tar = dest / tarball_name

    log.info("Staging bundle: downloading %s -> %s", volume_path, local_tar)
    client.files.download_to(volume_path, str(local_tar), overwrite=True)

    size_mb = local_tar.stat().st_size / (1024 * 1024)
    log.info("Downloaded %.1f MB; extracting into %s", size_mb, dest)

    with tarfile.open(local_tar, "r:gz") as tar:
        _assert_safe_members(tar, str(dest))
        tar.extractall(str(dest))

    # Remove the tarball to reclaim space; the extracted tree is what we need.
    local_tar.unlink(missing_ok=True)

    if not is_staged(dest_dir, marker_rel):
        raise RuntimeError(
            f"staging incomplete: '{marker_rel}' missing under {dest} after "
            f"extracting {volume_path}"
        )

    # The Grafana/pgbouncer/stunnel/psql binaries must be executable. tar
    # preserves mode bits, but re-assert on the key binaries defensively in
    # case the archive was created without the exec bit.
    for rel in (marker_rel, "stunnel", "pgbouncer", "psql"):
        p = dest / rel
        if p.is_file():
            os.chmod(p, p.stat().st_mode | 0o755)

    log.info("Bundle staged successfully under %s", dest)
    return True
