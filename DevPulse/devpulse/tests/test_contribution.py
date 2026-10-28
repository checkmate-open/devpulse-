
from pathlib import Path
import os

from devpulse.analyzer import _analyze_files


def test_directory_walk_errors_are_reported(
    tmp_path: Path, monkeypatch
) -> None:
    real_walk = os.walk

    def walk_with_error(root, topdown=True, onerror=None):
        if onerror is not None:
            onerror(
                PermissionError(
                    13,
                    "Permission denied",
                    str(Path(root) / "locked"),
                )
            )
        yield from real_walk(root, topdown=topdown, onerror=onerror)

    monkeypatch.setattr("devpulse.analyzer.os.walk", walk_with_error)
    diagnostics = []

    _analyze_files(tmp_path, 1024, diagnostics)

    assert any(
        "Could not scan directory locked" in item
        for item in diagnostics
    )
