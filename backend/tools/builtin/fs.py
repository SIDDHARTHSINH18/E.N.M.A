"""
GHOST — safe filesystem read tools (M3-H steps 1 and 6).

fs_read_file: read a local text file. Read-only.

Safety constraints, enforced HERE in code (treat params
as untrusted model output):
- path may be absolute, or relative: it is resolved against
  the canonical execution root by the centralized resolver
- relative paths may never traverse above the canonical root
  (textual ".." escapes are rejected before any filesystem
  access)
- path must exist and be a regular file / directory (no
  device/symlink tricks via isfile + islink rejection)
- file size must be <= MAX_FILE_SIZE_BYTES
- decoded as UTF-8 with errors="replace" (never raises
  on binary junk; result is always deterministic text)
- no writing, no globbing, no shell
"""

import os

from pathlib import Path

MAX_FILE_SIZE_BYTES = 1024 * 1024  # 1 MB

# Optional override of the canonical execution root (e.g. a
# packaged runtime whose working directory differs). Unset by
# default; the repository root is derived from this file's
# location using the same convention as backend/core/memory.py
# and backend/audit/log.py.
WORKSPACE_ROOT_ENV = "ENMA_WORKSPACE_ROOT"


class ToolExecutionError(Exception):
    """Raised for any invalid/unreadable path so the
    Agent maps it to a FAILED task with this message."""


def canonical_execution_root() -> Path:
    """
    The canonical project/execution root every relative tool
    path resolves against.

    ENMA_WORKSPACE_ROOT wins when set; otherwise the repository
    root is derived from this file's location
    (backend/tools/builtin/fs.py -> project root), matching the
    existing memory/audit storage convention. No personal or
    machine-specific path is hardcoded.
    """

    override = os.getenv(WORKSPACE_ROOT_ENV, "").strip()

    if override:
        return Path(os.path.abspath(override))

    return Path(
        os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "..",
            )
        )
    )


# File names that must never be read by model-invoked tools.
# This is the clearly-safe portion of the read boundary: the tools
# otherwise keep their documented sanctioned-absolute-read semantics
# (jailing ALL reads is deferred and must not silently break
# legitimate project reads).
SECRET_FILE_NAMES = (".env", "config.env")


def _refuse_secret_files(path_obj: Path, display: str) -> None:
    """Refuse reads of environment/secret files by name."""

    name = path_obj.name.lower()

    if (
        name in SECRET_FILE_NAMES
        or name.startswith(".env.")
    ):
        raise ToolExecutionError(
            f"Refusing to read a secret/environment file: '{display}'."
        )


def resolve_tool_path(raw_path: str) -> tuple[str, Path]:
    """
    Centralized safe path resolver at the filesystem execution
    boundary.

    - already-absolute paths are preserved unchanged;
    - relative paths ("." , "./x", "folder/x") resolve against
      the canonical execution root and are then jailed to it:
      any textual ".." escape that lands outside the root is
      rejected before touching the filesystem;
    - the result is normalized without resolving symlinks (the
      symlink refusal below stays intact).

    Returns (display_path, normalized Path).
    """

    root = canonical_execution_root()

    if os.path.isabs(raw_path):
        resolved = Path(os.path.normpath(raw_path))
    else:
        resolved = Path(os.path.normpath(root / raw_path))

        if resolved != root and root not in resolved.parents:
            raise ToolExecutionError(
                f"Path escapes the execution root: '{raw_path}'."
            )

    return str(resolved), resolved


def _validated_path(params: dict, tool_name: str) -> tuple[str, Path]:
    """Validate and resolve the shared untrusted ``path`` parameter.

    Relative paths resolve against the canonical execution root.
    The tools deliberately reject a symlink at the requested path
    rather than resolving it, so a caller cannot use these
    read-only tools to follow an indirect filesystem reference.
    """

    raw_path = params.get("path") if isinstance(params, dict) else None

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ToolExecutionError(
            f"{tool_name} requires a 'path' parameter."
        )

    path, path_obj = resolve_tool_path(raw_path.strip())

    # Error privacy: for paths inside the execution root,
    # report them root-relative ("./sub/missing") instead of
    # the absolute host location — task errors can be echoed
    # back through APIs/UIs. Absolute outside paths keep their
    # sanctioned absolute form.
    root = canonical_execution_root()

    if path_obj == root or root in path_obj.parents:
        try:
            relative = path_obj.relative_to(root)

            path = (
                "."
                if not relative.parts
                else "./" + str(relative)
            )
        except ValueError:
            pass

    if path_obj.is_symlink():
        raise ToolExecutionError(
            f"Refusing to follow symbolic link: '{path}'."
        )

    _refuse_secret_files(path_obj, path)

    return path, path_obj


def fs_read_file(params: dict) -> str:
    """
    Read one local text file.

    params: {"path": <filesystem path, absolute or relative to
             the canonical execution root>}
    Returns the decoded file contents.
    """

    path, path_obj = _validated_path(params, "fs_read_file")

    if not path_obj.exists():
        raise ToolExecutionError(
            f"File not found: '{path}'."
        )

    if not path_obj.is_file():
        raise ToolExecutionError(
            f"Path is not a regular file: '{path}'."
        )

    try:
        size = path_obj.stat().st_size
    except OSError as error:
        raise ToolExecutionError(
            f"Cannot stat '{path}': {error.strerror}."
        )

    if size > MAX_FILE_SIZE_BYTES:
        raise ToolExecutionError(
            f"File too large ({size} bytes; limit "
            f"{MAX_FILE_SIZE_BYTES} bytes): '{path}'."
        )

    try:
        return path_obj.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        raise ToolExecutionError(
            f"Cannot read '{path}': {error.strerror}."
        )


def fs_list_directory(params: dict) -> list[str]:
    """Return the immediate entries of one directory in name order.

    No entry is opened or traversed; only its name is returned. The
    requested directory itself must be an existing, non-symlink directory.
    """

    path, path_obj = _validated_path(
        params,
        "fs_list_directory",
    )

    if not path_obj.exists():
        raise ToolExecutionError(
            f"Directory not found: '{path}'."
        )

    if not path_obj.is_dir():
        raise ToolExecutionError(
            f"Path is not a directory: '{path}'."
        )

    try:
        return sorted(entry.name for entry in path_obj.iterdir())
    except OSError as error:
        raise ToolExecutionError(
            f"Cannot list directory '{path}': {error.strerror}."
        )


def fs_file_exists(params: dict) -> bool:
    """Report whether an absolute, non-symlink path is a regular file.

    Missing paths and non-file paths are normal ``False`` results. No file
    contents are opened or read.
    """

    _, path_obj = _validated_path(
        params,
        "fs_file_exists",
    )

    try:
        return path_obj.is_file()
    except OSError as error:
        raise ToolExecutionError(
            f"Cannot inspect path: {error.strerror}."
        )


MAX_WRITE_BYTES = 1024 * 1024  # 1 MB


def _write_validated_path(params: dict) -> tuple[str, Path]:
    """Write-specific path validation on top of the shared resolver.

    Unlike the read tools, writing refuses absolute paths outside the
    canonical execution root outright: a write may never land in an
    arbitrary host location, only inside the sanctioned workspace.
    """

    raw_path = params.get("path") if isinstance(params, dict) else None

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ToolExecutionError(
            "fs_write_file requires a 'path' parameter."
        )

    root = canonical_execution_root()

    path, path_obj = resolve_tool_path(raw_path.strip())

    if path_obj != root and root not in path_obj.parents:
        raise ToolExecutionError(
            "fs_write_file refuses paths outside the "
            f"execution root: '{path}'."
        )

    if path_obj.is_symlink():
        raise ToolExecutionError(
            f"Refusing to write through symbolic link: '{path}'."
        )

    # Intermediate-parent symlink check: a symlinked directory
    # INSIDE the root must not redirect a write outside the
    # workspace (the final-component check above is not enough).
    root = canonical_execution_root()

    for parent in path_obj.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ToolExecutionError(
                "Refusing to write through a symbolic link "
                f"directory: '{path}'."
            )

    return path, path_obj


def fs_write_file(params: dict) -> dict:
    """
    Write text into one file inside the workspace.

    Safety constraints, enforced HERE in code (params are
    untrusted model output):
    - the path must resolve INSIDE the canonical execution
      root; absolute paths outside it are refused outright
      (the read tools tolerate sanctioned absolute paths —
      a write tool must not)
    - content is UTF-8 text, size-bounded
    - parent directories are created as needed
    - no append, no shell, no globbing

    Returns {"path": <display path>, "bytes": <written size>} so
    callers can verify the write actually happened.
    """

    content = params.get("content") if isinstance(params, dict) else None

    if not isinstance(content, str):
        raise ToolExecutionError(
            "fs_write_file requires a 'content' string parameter."
        )

    encoded = content.encode("utf-8")

    if len(encoded) > MAX_WRITE_BYTES:
        raise ToolExecutionError(
            f"Content too large ({len(encoded)} bytes; limit "
            f"{MAX_WRITE_BYTES} bytes)."
        )

    path, path_obj = _write_validated_path(params)

    try:
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.write_bytes(encoded)
    except OSError as error:
        raise ToolExecutionError(
            f"Cannot write '{path}': {error.strerror}."
        )

    # Verification boundary: re-read from disk and compare. The
    # result is VERIFIED only with this independent evidence —
    # the write reporting success is COMPLETED, the re-read
    # confirms it. A mismatch is a hard failure, never smoothed
    # over.
    try:
        reread = path_obj.read_bytes()
    except OSError as error:
        raise ToolExecutionError(
            f"Write to '{path}' could not be verified: "
            f"{error.strerror}."
        )

    if reread != encoded:
        raise ToolExecutionError(
            f"Write to '{path}' could not be verified: the file "
            "content on disk does not match what was written."
        )

    return {"path": path, "bytes": len(encoded), "verified": True}
