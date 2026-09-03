#!/usr/bin/env python3
"""Documentation link + structure checker for the Polymarket Pro repository.

This script scans every Markdown file under the project root (excluding
``node_modules``, ``.next``, ``skills``, ``download`` and ``agent-ctx``
noise directories) and verifies:

  1. **Internal links** — every relative ``[label](path)`` link resolves to a
     file that actually exists on disk. External ``http(s)://`` links, anchor
     ``#section`` links, and mailto links are skipped (a CI link-checker can
     cover those separately if needed).
  2. **Code blocks** — every fenced ```` ``` ```` block declares a language
     (e.g. ```` ```bash ````). Bare fences are reported so docs stay
     syntax-highlighted.
  3. **Heading hierarchy** — headings are properly nested with no level
     skips (e.g. ``# Foo`` immediately followed by ``### Bar`` skips ``##``
     and is flagged).
  4. **Table structure** — every GFM table row has the same column count
     as its header (a trailing pipe gap is the classic silent break).

Exit codes:
    0 — no issues found
    1 — one or more issues found (broken links, bad fences, bad headings,
        broken tables, or usage errors)
    2 — usage error or unrecoverable I/O failure

Usage:
    python3 scripts/check_docs.py
    python3 scripts/check_docs.py --root /path/to/repo
    python3 scripts/check_docs.py --json
    python3 scripts/check_docs.py --verbose
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_ROOT = Path("/home/z/my-project")

# Directories that contain Markdown but should NOT be scanned (either they
# are vendored skills / downstream snapshots / cache, or they are agent
# context briefs that are not user-facing documentation).
EXCLUDED_DIRS: tuple[str, ...] = (
    "node_modules",
    ".next",
    ".git",
    "skills",
    "download",
    "agent-ctx",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "out",
)

# Link regex — captures the URL portion of every `[text](url)` construct.
# We intentionally match aggressively and then filter out non-relative links.
LINK_RE = re.compile(r"\[(?P<label>[^\]]*)\]\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)")

# Fenced code block regex — captures the language tag (the token immediately
# after the opening fence). Empty language tag = flagged as bare.
FENCE_RE = re.compile(r"(?m)^(?P<indent>[ \t]*)```\s*(?P<lang>[A-Za-z0-9_+-]*)\s*$")

# Heading regex — captures the leading ``#`` run for level detection.
HEADING_RE = re.compile(r"(?m)^(?P<hashes>#{1,6})\s+(?P<text>[^\n]*?)\s*$")

# GFM table row regex — splits a row on unescaped pipes.
TABLE_ROW_RE = re.compile(r"^\s*\|(?P<cells>.*?)\|\s*$")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class BrokenLink:
    source: str  # the markdown file containing the link
    line: int    # 1-based line number
    target: str  # the link target as written
    reason: str  # human-readable explanation


@dataclass
class BareFence:
    source: str
    line: int


@dataclass
class HeadingSkip:
    source: str
    line: int
    from_level: int
    to_level: int
    text: str


@dataclass
class BrokenTable:
    source: str
    line: int
    header_columns: int
    row_columns: int


@dataclass
class FileReport:
    path: str
    broken_links: list[BrokenLink] = field(default_factory=list)
    bare_fences: list[BareFence] = field(default_factory=list)
    heading_skips: list[HeadingSkip] = field(default_factory=list)
    broken_tables: list[BrokenTable] = field(default_factory=list)


@dataclass
class RunReport:
    root: str
    files_scanned: int
    total_links: int = 0
    files: list[FileReport] = field(default_factory=list)

    @property
    def broken_link_count(self) -> int:
        return sum(len(f.broken_links) for f in self.files)

    @property
    def bare_fence_count(self) -> int:
        return sum(len(f.bare_fences) for f in self.files)

    @property
    def heading_skip_count(self) -> int:
        return sum(len(f.heading_skips) for f in self.files)

    @property
    def broken_table_count(self) -> int:
        return sum(len(f.broken_tables) for f in self.files)

    @property
    def issue_count(self) -> int:
        return (
            self.broken_link_count
            + self.bare_fence_count
            + self.heading_skip_count
            + self.broken_table_count
        )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_markdown_files(root: Path) -> list[Path]:
    """Return every ``*.md`` file under *root* excluding noise directories."""
    if not root.exists():
        raise FileNotFoundError(f"root does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"root is not a directory: {root}")

    excluded = set(EXCLUDED_DIRS)
    found: list[Path] = []
    for path in root.rglob("*.md"):
        # Skip files whose path contains any excluded directory component.
        if any(part in excluded for part in path.parts):
            continue
        found.append(path)
    found.sort()
    return found


# ---------------------------------------------------------------------------
# Link extraction + verification
# ---------------------------------------------------------------------------


def is_external(url: str) -> bool:
    return url.startswith(("http://", "https://", "mailto:", "ftp://", "tel:"))


def is_anchor(url: str) -> bool:
    return url.startswith("#")


def is_absolute_url(url: str) -> bool:
    # Schema-less absolute URL e.g. //example.com/foo
    return url.startswith("//")


def resolve_link(source: Path, url: str) -> Path | None:
    """Resolve a relative Markdown link target against *source*'s location.

    Supports GitHub-style ``#anchor`` suffixes (stripped before resolution),
    bare anchor links (returns the source itself), and the ``README`` shortcut
    (a folder link resolves to ``<folder>/README.md``).
    """
    if is_external(url) or is_absolute_url(url):
        return None
    if is_anchor(url):
        return source

    # Strip a leading anchor from a path-like URL.
    target_path = url.split("#", 1)[0]
    # Strip query string (rare in docs but defensive).
    target_path = target_path.split("?", 1)[0]

    if not target_path:
        return source  # pure anchor link like `[see below](#section)`

    # Resolve relative to the directory containing the source markdown file.
    base_dir = source.parent
    resolved = (base_dir / target_path).resolve()

    # Folder shortcut — a link to `./docs` resolves to `./docs/README.md`.
    if resolved.is_dir():
        candidate = resolved / "README.md"
        if candidate.exists():
            return candidate
        return None

    return resolved if resolved.exists() else None


def extract_links(text: str) -> Iterable[tuple[int, str, str]]:
    """Yield ``(line_number, label, url)`` for every ``[label](url)`` link.

    Line numbers are 1-based. Uses ``finditer`` over the full text and
    counts newlines up to each match start to compute the line number.

    Links inside backtick code spans (`` `…` ``) are intentionally
    skipped — they're literal code samples in narrative prose, not
    live links. We mask the contents of every code span to ``x`` chars
    of the same length so the line-offset / column math is preserved.
    """
    # Mask the contents of every `...` inline code span so a literal
    # `[label](url)` written inside a code span (very common in
    # narrative docs that describe the link syntax itself) is not
    # mistaken for a live link. Newlines inside the matched span are
    # PRESERVED so the line-offset math below stays accurate (a multi-
    # line code span keeps its newlines; only the non-newline bytes
    # are masked to `x`).
    def _mask(m: re.Match[str]) -> str:
        body = m.group(0)[1:-1]  # strip the surrounding backticks
        # Replace every non-newline char with `x`; keep `\n` intact.
        masked_body = re.sub(r"[^\n]", "x", body)
        return "`" + masked_body + "`"

    masked = re.sub(r"`[^`]*`", _mask, text)

    # Pre-compute a running offset -> line-number index for fast lookups.
    line_starts = [0]
    for i, ch in enumerate(masked):
        if ch == "\n":
            line_starts.append(i + 1)

    for m in LINK_RE.finditer(masked):
        start = m.start()
        # Binary search for the last line_start <= start.
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= start:
                lo = mid
            else:
                hi = mid - 1
        line_no = lo + 1
        url = m.group("url")
        # Skip obvious placeholder URLs that are clearly narrative
        # examples of the link syntax itself (e.g. ``[label](url)`` or
        # ``[text](path)`` written in prose without backticks). A real
        # relative file path, anchor link, or external URL always
        # contains at least one of `/ . # ? :` — a bare alphabetic
        # word is almost certainly a placeholder.
        if not any(ch in url for ch in "/.#?:"):
            continue
        yield line_no, m.group("label"), url


# ---------------------------------------------------------------------------
# Fenced code-block check
# ---------------------------------------------------------------------------


def find_bare_fences(text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(line_number, fence_indent)`` for fences missing a language tag."""
    line_no = 1
    in_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        # Match a fence line — ``` optionally followed by language.
        if re.match(r"^[ \t]*```", stripped):
            if not in_fence:
                # Opening fence — language tag must be present + non-empty.
                m = FENCE_RE.match(line)
                if m and not m.group("lang"):
                    yield line_no, m.group("indent")
                in_fence = True
            else:
                # Closing fence — language tag should be empty here; we don't
                # flag closing fences.
                in_fence = False
        line_no += 1


# ---------------------------------------------------------------------------
# Heading hierarchy check
# ---------------------------------------------------------------------------


def find_heading_skips(text: str) -> Iterable[tuple[int, int, int, str]]:
    """Yield ``(line, from_level, to_level, text)`` where the level jumps
    by more than 1.
    """
    last_level = 0
    last_text = ""
    last_line = 0
    for m in HEADING_RE.finditer(text):
        level = len(m.group("hashes"))
        # Compute line number of the heading.
        line_no = text.count("\n", 0, m.start()) + 1
        heading_text = m.group("text").strip()
        if last_level > 0 and level > last_level + 1:
            yield line_no, last_level, level, heading_text
        last_level = level
        last_text = heading_text
        last_line = line_no


# ---------------------------------------------------------------------------
# Table check
# ---------------------------------------------------------------------------


def _row_cell_count(line: str) -> int | None:
    """Return the number of cells in a table row, or ``None`` if not a row."""
    m = TABLE_ROW_RE.match(line)
    if not m:
        return None
    # An empty row like `| |` is still 1 cell.
    cells = m.group("cells")
    # Mask out pipes inside backtick code spans so a cell like
    # `` `{"a"|"b"}` `` counts as a single cell instead of being split on
    # the inner pipe. (GFM renderers treat pipes inside code spans as
    # literal text; the naive ``split('|')`` would over-count.)
    masked = re.sub(r"`[^`]*`", lambda mm: "x" * len(mm.group(0)), cells)
    parts = re.split(r"(?<!\\)\|", masked)
    # Trailing empty cell from a row ending with `|` is the regex artifact.
    return max(1, len(parts))


def find_broken_tables(text: str) -> Iterable[tuple[int, int, int]]:
    """Yield ``(line, header_cols, row_cols)`` for every table row whose cell
    count disagrees with its header.
    """
    lines = text.splitlines()
    in_table = False
    header_cols = 0
    header_line = 0
    for idx, line in enumerate(lines, start=1):
        cells = _row_cell_count(line)
        if cells is None:
            # End of any in-progress table.
            in_table = False
            header_cols = 0
            continue
        if not in_table:
            # Header row.
            header_cols = cells
            header_line = idx
            in_table = True
            continue
        # Subsequent row. If it's the row immediately after the header AND
        # it's a delimiter row (all dashes / colons), skip it.
        if idx == header_line + 1 and re.fullmatch(
            r"\s*\|?[\s:|\-]+\|?\s*", line
        ):
            continue
        if cells != header_cols:
            yield idx, header_cols, cells


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------


def scan_file(path: Path, root: Path) -> tuple[FileReport, int]:
    """Scan a single Markdown file. Returns ``(report, total_link_count)``."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # Cannot read — synthesise a single broken-link entry so the failure
        # surfaces in the report.
        report = FileReport(path=str(path.relative_to(root)))
        report.broken_links.append(
            BrokenLink(
                source=str(path.relative_to(root)),
                line=0,
                target="(file)",
                reason=f"unreadable: {exc}",
            )
        )
        return report, 0

    rel = str(path.relative_to(root))
    report = FileReport(path=rel)
    total_links = 0

    # Links ----------------------------------------------------------------
    for line_no, label, url in extract_links(text):
        total_links += 1
        if is_external(url) or is_absolute_url(url):
            continue
        if is_anchor(url):
            # Local anchor link — we don't verify these (would need an
            # index of every heading id across the doc set). Skipped.
            continue
        target = resolve_link(path, url)
        if target is None:
            report.broken_links.append(
                BrokenLink(
                    source=rel,
                    line=line_no,
                    target=url,
                    reason="target file does not exist",
                )
            )

    # Bare fences ----------------------------------------------------------
    for line_no, _indent in find_bare_fences(text):
        report.bare_fences.append(BareFence(source=rel, line=line_no))

    # Heading skips --------------------------------------------------------
    for line_no, from_level, to_level, heading_text in find_heading_skips(text):
        report.heading_skips.append(
            HeadingSkip(
                source=rel,
                line=line_no,
                from_level=from_level,
                to_level=to_level,
                text=heading_text,
            )
        )

    # Broken tables --------------------------------------------------------
    for line_no, header_cols, row_cols in find_broken_tables(text):
        report.broken_tables.append(
            BrokenTable(
                source=rel,
                line=line_no,
                header_columns=header_cols,
                row_columns=row_cols,
            )
        )

    return report, total_links


def scan_root(root: Path) -> RunReport:
    files = discover_markdown_files(root)
    report = RunReport(root=str(root), files_scanned=len(files))
    for path in files:
        file_report, total_links = scan_file(path, root)
        report.total_links += total_links
        # Always include the file so the JSON dump is complete; the
        # human-readable report filters to files with issues.
        report.files.append(file_report)
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _format_line(prefix: str, msg: str) -> str:
    return f"  {prefix:18} {msg}"


def render_human(report: RunReport, verbose: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"Scanned {report.files_scanned} Markdown files under {report.root}")
    lines.append(
        f"Checked {report.total_links} relative links — "
        f"{report.broken_link_count} broken, "
        f"{report.bare_fence_count} bare fences, "
        f"{report.heading_skip_count} heading skips, "
        f"{report.broken_table_count} broken tables."
    )
    lines.append("")

    if report.issue_count == 0:
        lines.append("OK — no documentation issues found.")
        return "\n".join(lines)

    # Group by file. Only emit files that have at least one issue unless
    # --verbose was passed (in which case emit every scanned file).
    for file_report in sorted(report.files, key=lambda f: f.path):
        if not verbose and not (
            file_report.broken_links
            or file_report.bare_fences
            or file_report.heading_skips
            or file_report.broken_tables
        ):
            continue
        lines.append(f"## {file_report.path}")
        for bl in file_report.broken_links:
            lines.append(
                _format_line(
                    "broken link",
                    f"line {bl.line}: `{bl.target}` — {bl.reason}",
                )
            )
        for bf in file_report.bare_fences:
            lines.append(
                _format_line("bare fence", f"line {bf.line}: ``` block has no language tag")
            )
        for hs in file_report.heading_skips:
            lines.append(
                _format_line(
                    "heading skip",
                    f"line {hs.line}: H{hs.from_level} → H{hs.to_level} "
                    f"({hs.text!r})",
                )
            )
        for bt in file_report.broken_tables:
            lines.append(
                _format_line(
                    "broken table",
                    f"line {bt.line}: header has {bt.header_columns} cols, "
                    f"row has {bt.row_columns}",
                )
            )
        lines.append("")

    lines.append(f"Total issues: {report.issue_count}")
    return "\n".join(lines)


def render_json(report: RunReport) -> str:
    payload = {
        "root": report.root,
        "files_scanned": report.files_scanned,
        "total_links": report.total_links,
        "broken_links": report.broken_link_count,
        "bare_fences": report.bare_fence_count,
        "heading_skips": report.heading_skip_count,
        "broken_tables": report.broken_table_count,
        "issue_count": report.issue_count,
        "files": [
            {
                "path": f.path,
                "broken_links": [asdict(b) for b in f.broken_links],
                "bare_fences": [asdict(b) for b in f.bare_fences],
                "heading_skips": [asdict(h) for h in f.heading_skips],
                "broken_tables": [asdict(t) for t in f.broken_tables],
            }
            for f in report.files
            if f.broken_links or f.bare_fences or f.heading_skips or f.broken_tables
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify documentation links, fences, headings, and tables.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Repository root (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Also list files with zero issues (default: only files with issues).",
    )
    args = parser.parse_args(argv)

    try:
        report = scan_root(args.root)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(render_json(report))
    else:
        print(render_human(report, verbose=args.verbose))

    return 0 if report.issue_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
