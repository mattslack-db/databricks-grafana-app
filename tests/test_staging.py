"""Unit tests for lib/staging.py — bundle staging from a UC Volume."""
from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from lib.staging import is_staged, stage_binaries


def _make_bundle_tarball(path: Path, *, marker_rel: str = "grafana/bin/grafana") -> None:
    """Write a small gzipped tarball mirroring the real bundle layout."""
    with tarfile.open(path, "w:gz") as tar:
        for rel, content in [
            (marker_rel, b"#!/fake grafana binary\n"),
            ("pgbouncer", b"#!/fake pgbouncer\n"),
            ("stunnel", b"#!/fake stunnel\n"),
            ("psql", b"#!/fake psql\n"),
            ("lib/libfake.so.1", b"\x7fELF fake\n"),
        ]:
            data = content
            info = tarfile.TarInfo(name=rel)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))


class _FakeFiles:
    """Stand-in for WorkspaceClient.files: download_to copies a prebuilt tar."""

    def __init__(self, source_tar: Path):
        self._source_tar = source_tar
        self.calls = 0

    def download_to(self, remote_path: str, local_path: str, overwrite: bool = True):
        self.calls += 1
        Path(local_path).write_bytes(self._source_tar.read_bytes())


class _FakeClient:
    def __init__(self, source_tar: Path):
        self.files = _FakeFiles(source_tar)


def test_is_staged_false_when_marker_missing(tmp_path):
    # Arrange
    dest = tmp_path / "bin"

    # Act / Assert
    assert is_staged(str(dest)) is False


def test_stage_binaries_downloads_and_extracts(tmp_path):
    # Arrange
    source_tar = tmp_path / "bin.tar.gz"
    _make_bundle_tarball(source_tar)
    client = _FakeClient(source_tar)
    dest = tmp_path / "bin"

    # Act
    did_stage = stage_binaries(
        client, "/Volumes/c/s/v/bin.tar.gz", str(dest)
    )

    # Assert
    assert did_stage is True
    assert client.files.calls == 1
    assert (dest / "grafana/bin/grafana").is_file()
    assert (dest / "pgbouncer").is_file()
    assert (dest / "lib/libfake.so.1").is_file()
    # Tarball is cleaned up after extraction.
    assert not (dest / "bin.tar.gz").exists()
    # Key binaries are executable.
    assert os.access(dest / "grafana/bin/grafana", os.X_OK)
    assert os.access(dest / "pgbouncer", os.X_OK)


def test_stage_binaries_idempotent_skips_download(tmp_path):
    # Arrange: pre-populate the marker so it looks already-staged.
    dest = tmp_path / "bin"
    (dest / "grafana" / "bin").mkdir(parents=True)
    (dest / "grafana" / "bin" / "grafana").write_bytes(b"already here")
    source_tar = tmp_path / "bin.tar.gz"
    _make_bundle_tarball(source_tar)
    client = _FakeClient(source_tar)

    # Act
    did_stage = stage_binaries(
        client, "/Volumes/c/s/v/bin.tar.gz", str(dest)
    )

    # Assert: no download attempted.
    assert did_stage is False
    assert client.files.calls == 0


def test_stage_binaries_rejects_path_traversal(tmp_path):
    # Arrange: craft a malicious tarball with a ../ escape member.
    bad_tar = tmp_path / "bad.tar.gz"
    with tarfile.open(bad_tar, "w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo(name="../escape.sh")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    client = _FakeClient(bad_tar)
    dest = tmp_path / "bin"

    # Act / Assert
    with pytest.raises(ValueError, match="unsafe tar member"):
        stage_binaries(client, "/Volumes/c/s/v/bad.tar.gz", str(dest))
    # The escape target must not have been written.
    assert not (tmp_path / "escape.sh").exists()


def test_stage_binaries_raises_when_marker_absent_after_extract(tmp_path):
    # Arrange: a tarball that does NOT contain the marker.
    tar_no_marker = tmp_path / "nomarker.tar.gz"
    with tarfile.open(tar_no_marker, "w:gz") as tar:
        data = b"only pgbouncer\n"
        info = tarfile.TarInfo(name="pgbouncer")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    client = _FakeClient(tar_no_marker)
    dest = tmp_path / "bin"

    # Act / Assert
    with pytest.raises(RuntimeError, match="staging incomplete"):
        stage_binaries(client, "/Volumes/c/s/v/nomarker.tar.gz", str(dest))
