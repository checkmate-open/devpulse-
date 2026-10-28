
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    CodeQuality,
    ContributorStats,
    FileStats,
    GitStats,
    RepositoryReport,
    TodoFinding,
)

_SKIPPED_DIRECTORIES = {
    ".git", ".hg", ".mypy_cache", ".nox", ".pytest_cache",
    ".ruff_cache", ".svn", ".tox", ".venv", "__pycache__",
    "env", "node_modules", "venv",
}
_GENERATED_DIRECTORIES = {
    ".next", ".nuxt", "build", "coverage", "dist", "htmlcov", "target",
}
_GENERATED_SUFFIXES = {".bak", ".log", ".pyc", ".pyo", ".tmp"}
_SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java",
    ".js", ".jsx", ".php", ".py", ".rb", ".rs", ".scala", ".sh",
    ".sql", ".swift", ".ts", ".tsx", ".vue",
}
_PRIVATE_OR_BINARY_NAMES = {".env", "id_rsa", "id_ed25519"}
_TODO_PATTERN = re.compile(r"\b(TODO|FIXME)\b", re.IGNORECASE)
_MAX_SCANNED_SOURCE_BYTES = 1_000_000
_MAX_LARGE_FILE_RESULTS = 100
_MAX_TODO_RESULTS = 1_000
_COMMIT_SEPARATOR = "\x1e"
_FIELD_SEPARATOR = "\x1f"


def analyze_repository(
    repository_path: Path | str,
    large_file_threshold_bytes: int = 5 * 1024 * 1024,
) -> RepositoryReport:
    """Analyze a local directory without sending its contents to a remote service."""
    if large_file_threshold_bytes <= 0:
        raise ValueError("The large-file threshold must be greater than zero.")

    if not str(repository_path).strip():
        raise ValueError("Repository path cannot be resolved: path is empty.")

    path = Path(repository_path).expanduser()

    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(
            f"Repository path cannot be resolved: {error}"
        ) from error

    if not path.is_dir():
        raise ValueError(f"Repository path is not a directory: {path}")

    diagnostics: list[str] = []
    files = _analyze_files(path, large_file_threshold_bytes, diagnostics)
    quality, todos = _analyze_code(path, files, diagnostics)
    git_stats = _analyze_git(path)
    summary = _health_summary(git_stats, files, quality, todos)

    return RepositoryReport(
        repository_name=path.name or str(path),
        repository_path=str(path),
        git=git_stats,
        files=files,
        quality=quality,
        todos=todos,
        health_summary=summary,
    )


def _run_git(
    path: Path, arguments: list[str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=15,
    )


def _analyze_git(path: Path) -> GitStats:
    stats = GitStats()

    if shutil.which("git") is None:
        stats.diagnostics.append(
            "Git was not found on PATH; commit history was not analyzed."
        )
        return stats

    try:
        result = _run_git(path, ["rev-parse", "--is-inside-work-tree"])
    except (OSError, subprocess.TimeoutExpired) as error:
        stats.diagnostics.append(f"Could not check Git history: {error}")
        return stats

    if result.returncode != 0 or result.stdout.strip() != "true":
        stats.diagnostics.append(
            "This folder is not a readable Git working tree; "
            "commit statistics are unavailable."
        )
        return stats

    format_string = (
        f"%H{_FIELD_SEPARATOR}%an{_FIELD_SEPARATOR}"
        f"%aI{_FIELD_SEPARATOR}%s{_COMMIT_SEPARATOR}"
    )

    try:
        result = _run_git(path, ["log", f"--format={format_string}"])
    except (OSError, subprocess.TimeoutExpired) as error:
        stats.diagnostics.append(f"Could not read Git commit history: {error}")
        return stats

    if result.returncode != 0:
        # An initialized repository with no commits is a valid empty state.
        if "does not have any commits yet" in result.stderr.casefold():
            return stats

        stats.diagnostics.append(
            "Git could not read commit history: "
            f"{result.stderr.strip() or 'unknown Git error'}"
        )
        return stats

    if not result.stdout.strip():
        return stats

    contributors: Counter[str] = Counter()
    commits_by_month: Counter[str] = Counter()
    dates: list[datetime] = []

    for record in result.stdout.split(_COMMIT_SEPARATOR):
        fields = record.strip().split(_FIELD_SEPARATOR, maxsplit=3)
        if len(fields) != 4:
            continue

        _, author, authored_at, _ = fields

        try:
            commit_date = datetime.fromisoformat(authored_at)
        except ValueError:
            stats.diagnostics.append("Some commit dates could not be parsed.")
            continue

        contributors[author] += 1
        dates.append(commit_date)
        commits_by_month[commit_date.strftime("%Y-%m")] += 1

    stats.commit_count = len(dates)
    stats.contributor_count = len(contributors)
    stats.first_commit_date = (
        min(dates).date().isoformat() if dates else None
    )
    stats.latest_commit_date = (
        max(dates).date().isoformat() if dates else None
    )
    stats.contributors = [
        ContributorStats(name=name, commits=count)
        for name, count in sorted(
            contributors.items(),
            key=lambda item: (-item[1], item[0].casefold()),
        )
    ]
    stats.commits_by_month = [
        {"month": month, "commits": commits_by_month[month]}
        for month in sorted(commits_by_month)
    ]

    return stats


def _analyze_files(
    root: Path, threshold: int, diagnostics: list[str]
) -> FileStats:
    stats = FileStats()
    extension_counts: Counter[str] = Counter()

    def record_walk_error(error: OSError) -> None:
        failed_path = error.filename or str(root)
        try:
            relative_path = Path(failed_path).relative_to(root).as_posix()
        except ValueError:
            relative_path = failed_path
        diagnostics.append(
            f"Could not scan directory {relative_path}: {error.strerror or error}."
        )

    for current, directories, filenames in os.walk(
        root, topdown=True, onerror=record_walk_error
    ):
        current_path = Path(current)
        kept_directories = []

        for directory in directories:
            child = current_path / directory
            if directory in _SKIPPED_DIRECTORIES:
                stats.excluded_directories.append(
                    str(child.relative_to(root)).replace("\\", "/")
                )
            else:
                kept_directories.append(directory)

        directories[:] = kept_directories
        stats.directory_count += len(directories)

        for filename in filenames:
            file_path = current_path / filename

            if file_path.is_symlink():
                continue

            relative_path = file_path.relative_to(root).as_posix()

            try:
                size = file_path.stat().st_size
            except OSError as error:
                diagnostics.append(f"Could not inspect {relative_path}: {error}")
                continue

            stats.file_count += 1
            suffix = file_path.suffix.lower()
            extension_counts[suffix or "[no extension]"] += 1

            if size >= threshold:
                stats.large_files.append(
                    {"path": relative_path, "size_bytes": size}
                )

            if _is_generated_candidate(file_path):
                stats.generated_candidates.append(
                    {
                        "path": relative_path,
                        "kind": _generated_kind(file_path),
                    }
                )

    stats.files_by_extension = dict(
        sorted(
            extension_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    )
    stats.large_files.sort(
        key=lambda item: (-item["size_bytes"], item["path"])
    )
    stats.large_files = stats.large_files[:_MAX_LARGE_FILE_RESULTS]
    stats.generated_candidates.sort(
        key=lambda item: item["path"].casefold()
    )
    stats.excluded_directories.sort(key=str.casefold)

    return stats


def _is_generated_candidate(path: Path) -> bool:
    lowered_parts = {part.casefold() for part in path.parts}

    return (
        bool(lowered_parts & _GENERATED_DIRECTORIES)
        or path.suffix.casefold() in _GENERATED_SUFFIXES
        or path.name.casefold() in {".coverage", "coverage.xml"}
        or path.name.casefold().endswith(
            (".min.js", ".min.css", ".map")
        )
    )


def _generated_kind(path: Path) -> str:
    matching_directories = sorted(
        {part.casefold() for part in path.parts} & _GENERATED_DIRECTORIES
    )

    if matching_directories:
        return f"generated directory: {', '.join(matching_directories)}"

    return "generated-looking file"


def _analyze_code(
    root: Path, files: FileStats, diagnostics: list[str]
) -> tuple[CodeQuality, list[TodoFinding]]:
    quality = CodeQuality()
    todos: list[TodoFinding] = []

    for current, directories, filenames in os.walk(root, topdown=True):
        directories[:] = [
            name for name in directories
            if name not in _SKIPPED_DIRECTORIES
        ]

        for filename in filenames:
            path = Path(current) / filename

            if path.is_symlink():
                continue

            relative_path = path.relative_to(root).as_posix()

            if path.name.casefold() in _PRIVATE_OR_BINARY_NAMES:
                continue

            if path.suffix.casefold() not in _SOURCE_SUFFIXES:
                continue

            try:
                if path.stat().st_size > _MAX_SCANNED_SOURCE_BYTES:
                    diagnostics.append(
                        f"Skipped source scan for {relative_path}: "
                        "file exceeds 1 MB."
                    )
                    continue

                source = path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError as error:
                diagnostics.append(f"Could not read {relative_path}: {error}")
                continue

            if path.suffix.casefold() == ".py":
                quality.python_file_count += 1
                lines = source.splitlines()

                if len(lines) > 500:
                    quality.long_python_files.append(
                        {"path": relative_path, "lines": len(lines)}
                    )

                try:
                    tree = ast.parse(source, filename=relative_path)
                except SyntaxError as error:
                    quality.syntax_errors.append(
                        {
                            "path": relative_path,
                            "line": error.lineno or 0,
                            "message": error.msg,
                        }
                    )
                else:
                    quality.functions_over_50_lines.extend(
                        _long_functions(tree, relative_path)
                    )

            for line_number, line in enumerate(
                source.splitlines(), start=1
            ):
                for match in _TODO_PATTERN.finditer(line):
                    if len(todos) >= _MAX_TODO_RESULTS:
                        diagnostics.append(
                            "TODO/FIXME results were capped at "
                            "1,000 findings."
                        )
                        return quality, todos

                    todos.append(
                        TodoFinding(
                            path=relative_path,
                            line=line_number,
                            marker=match.group(1).upper(),
                            text=line.strip(),
                        )
                    )

    return quality, todos


def _long_functions(
    tree: ast.AST, relative_path: str
) -> list[dict[str, Any]]:
    findings = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_line = getattr(node, "end_lineno", node.lineno)
            line_count = end_line - node.lineno + 1

            if line_count > 50:
                findings.append(
                    {
                        "path": relative_path,
                        "name": node.name,
                        "line": node.lineno,
                        "lines": line_count,
                    }
                )

    return findings


def _health_summary(
    git: GitStats,
    files: FileStats,
    quality: CodeQuality,
    todos: list[TodoFinding],
) -> str:
    observations = [
        (
            f"{git.commit_count} commits from "
            f"{git.contributor_count} contributors were found."
            if not git.diagnostics
            else "Git history could not be fully assessed."
        )
    ]

    observations.append(
        f"{len(quality.syntax_errors)} Python syntax issue(s), "
        f"{len(files.large_files)} large file(s), "
        f"{len(files.generated_candidates)} generated-file candidate(s), "
        f"and {len(todos)} TODO/FIXME marker(s) were observed."
    )

    if quality.syntax_errors:
        observations.append("Review the reported Python syntax issues.")
    elif not git.commit_count and not git.diagnostics:
        observations.append("This Git repository has no commits yet.")
    else:
        observations.append(
            "Treat these findings as triage indicators; "
            "this is not a comprehensive audit."
        )

    return " ".join(observations)
