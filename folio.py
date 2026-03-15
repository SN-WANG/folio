#!/usr/bin/env python3
"""Folio — Lightweight filesystem-based literature manager.

Author: Shengning Wang (snwang2023@163.com)

Drop PDF + BibTeX/RIS files into _inbox/, run this script,
and they will be automatically organized, renamed, and indexed.

Usage:
    folio              Process inbox files
    folio --dry-run    Preview without moving files
    folio --rebuild    Rebuild library.bib only
    folio --init       Initialize directory structure only
    folio --url URL    Download paper from ArXiv URL into inbox
"""

import argparse
import logging
import re
import shutil
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from pypinyin import lazy_pinyin, Style
    HAS_PINYIN = True
except ImportError:
    HAS_PINYIN = False

# ─── Paths ─────────────────────────────────────────────────────────────
FOLIO_HOME = Path.home() / "folio"
INBOX_DIR = FOLIO_HOME / "_inbox"
LIBRARY_DIR = FOLIO_HOME / "library"
INDEX_FILE = FOLIO_HOME / "index.md"
LIBRARY_BIB = FOLIO_HOME / "library.bib"


# ─── ANSI Colors ──────────────────────────────────────────────────────
class Hue:
    """ANSI escape codes for colored terminal output."""
    b = "\033[1;34m"    # bold blue     — key names, paper names
    c = "\033[1;36m"    # bold cyan     — file paths, secondary info
    m = "\033[1;35m"    # bold magenta  — values, counts
    y = "\033[1;33m"    # bold yellow   — warnings, dry-run
    g = "\033[1;32m"    # bold green    — success
    r = "\033[1;31m"    # bold red      — errors
    d = "\033[90m"      # dim gray      — decorative lines
    q = "\033[0m"       # reset

hue = Hue()


# ─── Logging ──────────────────────────────────────────────────────────
class _FolioFormatter(logging.Formatter):
    """Custom formatter with colored [folio] prefix per log level."""
    _COLORS = {
        logging.INFO:     hue.b,
        logging.WARNING:  hue.y,
        logging.ERROR:    hue.r,
        logging.CRITICAL: hue.r,
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self._COLORS.get(record.levelno, hue.q)
        return f"{color}[folio]{hue.q} {record.getMessage()}"


def _setup_logger() -> logging.Logger:
    """Create a colored console logger for folio."""
    logger = logging.getLogger("folio")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.hasHandlers():
        logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_FolioFormatter())
    logger.addHandler(handler)
    return logger

log = _setup_logger()

# ─── Naming Constants ─────────────────────────────────────────────────
SHORT_TITLE_WORDS = 2
SHORT_TITLE_CN_CHARS = 4
MAX_NAME_LENGTH = 50

DEFAULT_INDEX = """\
# Folio Library

## Uncategorized

<!-- New papers will be added here. Move them to your categories below. -->

---

"""

STOP_WORDS = frozenset(
    "a an the of for and in on to with is are by from its their this that "
    "using via based".split()
)


# ═══════════════════════════════════════════════════════════════════════
#  BibTeX Parser
# ═══════════════════════════════════════════════════════════════════════

def _extract_bib_field(content: str, field: str) -> str:
    """Extract a field value from BibTeX content.

    Handles {braced}, "quoted", and plain numeric values.
    Correctly tracks nested braces for multiline values.
    """
    match = re.search(rf"\b{field}\s*=\s*", content, re.IGNORECASE)
    if not match:
        return ""

    rest = content[match.end():].lstrip()

    if rest.startswith("{"):
        depth = 0
        for i, ch in enumerate(rest):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return rest[1:i].strip()
        return rest[1:].strip()

    if rest.startswith('"'):
        end = rest.find('"', 1)
        return rest[1:end].strip() if end != -1 else rest[1:].strip()

    m = re.match(r"([^\s,}]+)", rest)
    return m.group(1) if m else ""


def parse_bib(filepath: Path) -> Optional[Dict[str, str]]:
    """Parse a .bib file and return metadata dict, or None on failure."""
    content = filepath.read_text(encoding="utf-8-sig", errors="replace")

    key_match = re.search(r"@\w+\s*\{([^,]+)", content)
    cite_key = key_match.group(1).strip() if key_match else ""

    authors_raw = _extract_bib_field(content, "author")
    first_author = authors_raw.split(" and ")[0].split(",")[0].strip()
    first_author = re.sub(r"[{}\\~]", "", first_author)

    year = _extract_bib_field(content, "year")
    title = _extract_bib_field(content, "title")
    title = re.sub(r"[{}]", "", title)

    journal = (
        _extract_bib_field(content, "journal")
        or _extract_bib_field(content, "booktitle")
    )
    journal = re.sub(r"[{}]", "", journal)

    if not first_author and not title:
        return None

    return {
        "first_author": first_author or "Unknown",
        "authors": authors_raw,
        "year": year or "Unknown",
        "title": title or "Untitled",
        "journal": journal,
        "cite_key": cite_key,
        "raw_bib": content.strip(),
    }


# ═══════════════════════════════════════════════════════════════════════
#  RIS Parser
# ═══════════════════════════════════════════════════════════════════════

def parse_ris(filepath: Path) -> Optional[Dict[str, str]]:
    """Parse a .ris file and return metadata dict, or None on failure."""
    content = filepath.read_text(encoding="utf-8-sig", errors="replace")

    authors: List[str] = []
    title = ""
    year = ""
    journal = ""

    for line in content.splitlines():
        tag_match = re.match(r"^([A-Z][A-Z0-9])\s+-\s+(.*)", line.strip())
        if not tag_match:
            continue
        tag, value = tag_match.group(1), tag_match.group(2).strip()

        if tag in ("AU", "A1"):
            authors.append(value)
        elif tag in ("TI", "T1") and not title:
            title = value
        elif tag in ("PY", "Y1", "DA") and not year:
            year = value.split("/")[0].strip()[:4]
        elif tag in ("JO", "JF", "T2") and not journal:
            journal = value

    first_author = authors[0].split(",")[0].strip() if authors else "Unknown"
    if first_author == "Unknown" and not title:
        return None

    safe_key = re.sub(r"[^a-zA-Z0-9]", "", first_author) + year
    authors_str = " and ".join(authors)

    raw_bib = (
        f"@article{{{safe_key},\n"
        f"  author = {{{authors_str}}},\n"
        f"  title = {{{title}}},\n"
        f"  year = {{{year}}},\n"
        + (f"  journal = {{{journal}}},\n" if journal else "")
        + "}\n"
    )

    return {
        "first_author": first_author,
        "authors": authors_str,
        "year": year or "Unknown",
        "title": title or "Untitled",
        "journal": journal,
        "cite_key": safe_key,
        "raw_bib": raw_bib,
    }


# ═══════════════════════════════════════════════════════════════════════
#  Name Generator
# ═══════════════════════════════════════════════════════════════════════

def _to_ascii_name(text: str) -> str:
    """Convert text (possibly Chinese) to ASCII-safe CamelCase.

    Uses pypinyin if available; otherwise keeps Chinese characters as-is.
    """
    if HAS_PINYIN and re.search(r"[\u4e00-\u9fff]", text):
        parts = lazy_pinyin(text, style=Style.NORMAL)
        return "".join(w.capitalize() for w in parts if w.strip())
    return text


def generate_name(metadata: Dict[str, str]) -> str:
    """Generate standardized name: AuthorYYYY_KeywordKeyword."""
    author_raw = metadata.get("first_author", "Unknown")
    author_ascii = _to_ascii_name(author_raw)
    author = re.sub(r"[^a-zA-Z\u4e00-\u9fff]", "", author_ascii) or "Unknown"
    year = metadata.get("year", "Unknown")

    title_raw = metadata.get("title", "Untitled")
    if re.search(r"[\u4e00-\u9fff]", title_raw):
        cn_chars = re.findall(r"[\u4e00-\u9fff]", title_raw)
        title_for_name = "".join(cn_chars[:SHORT_TITLE_CN_CHARS])
    else:
        title_for_name = title_raw
    title_ascii = _to_ascii_name(title_for_name)
    words = re.findall(r"[a-zA-Z]+", title_ascii)
    keywords = [w.capitalize() for w in words if w.lower() not in STOP_WORDS]

    if not keywords:
        cn_chars = re.findall(r"[\u4e00-\u9fff]+", title_raw)
        short_title = "".join(cn_chars)[:6] if cn_chars else "Untitled"
    else:
        short_title = "".join(keywords[:SHORT_TITLE_WORDS])

    name = f"{author}{year}_{short_title}"
    return name[:MAX_NAME_LENGTH]


# ═══════════════════════════════════════════════════════════════════════
#  Clipboard BibTeX
# ═══════════════════════════════════════════════════════════════════════

def try_clipboard_bib(inbox: Path) -> None:
    """If inbox has unpaired PDFs and no ref files, try reading BibTeX from clipboard."""
    pdfs = [f for f in inbox.iterdir() if f.is_file() and f.suffix.lower() == ".pdf"]
    refs = [f for f in inbox.iterdir() if f.is_file() and f.suffix.lower() in (".bib", ".ris")]

    unpaired = [p for p in pdfs if not any(r.stem.lower() == p.stem.lower() for r in refs)]
    if not unpaired or refs:
        return

    # All PDFs are unpaired and there are zero ref files — try clipboard
    try:
        result = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=5
        )
        clip = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return

    if not re.search(r"@\w+\s*\{", clip):
        return

    if len(unpaired) == 1:
        bib_path = inbox / f"{unpaired[0].stem}.bib"
        bib_path.write_text(clip, encoding="utf-8")
        log.info(f"Clipboard BibTeX -> {hue.c}{bib_path.name}{hue.q}")
    else:
        log.warning(
            f"{hue.m}{len(unpaired)}{hue.q} PDFs in inbox but only one clipboard — "
            "paste applies to most recent PDF only. Skipping."
        )


# ═══════════════════════════════════════════════════════════════════════
#  File Pair Matching
# ═══════════════════════════════════════════════════════════════════════

def find_pairs(inbox: Path) -> List[Tuple[Path, Path]]:
    """Find PDF + bib/ris pairs in inbox by stem matching or auto-pairing."""
    pdfs: Dict[str, Path] = {}
    refs: Dict[str, Path] = {}

    for f in sorted(inbox.iterdir()):
        if not f.is_file():
            continue
        stem = f.stem.lower()
        suffix = f.suffix.lower()
        if suffix == ".pdf":
            pdfs[stem] = f
        elif suffix in (".bib", ".ris"):
            refs[stem] = f

    pairs: List[Tuple[Path, Path]] = []
    matched_p: set = set()
    matched_r: set = set()

    # 1. Exact stem match
    for stem, pdf in pdfs.items():
        if stem in refs:
            pairs.append((pdf, refs[stem]))
            matched_p.add(stem)
            matched_r.add(stem)

    # 2. Single-unmatched auto-pair
    upm = {s: p for s, p in pdfs.items() if s not in matched_p}
    urm = {s: r for s, r in refs.items() if s not in matched_r}

    if len(upm) == 1 and len(urm) == 1:
        pdf = next(iter(upm.values()))
        ref = next(iter(urm.values()))
        pairs.append((pdf, ref))
        log.info(f"Auto-paired: {hue.c}{pdf.name}{hue.q} <-> {hue.c}{ref.name}{hue.q}")
    else:
        for s, p in upm.items():
            log.warning(f"No matching bib/ris for: {hue.y}{p.name}{hue.q}")
        for s, r in urm.items():
            log.warning(f"No matching PDF for: {hue.y}{r.name}{hue.q}")

    return pairs


# ═══════════════════════════════════════════════════════════════════════
#  Index Manager
# ═══════════════════════════════════════════════════════════════════════

def get_indexed_papers(index_file: Path) -> set:
    """Get paper names already present anywhere in index.md."""
    if not index_file.exists():
        return set()
    content = index_file.read_text(encoding="utf-8")
    return set(re.findall(r"\*\*\[([^\]]+)\]\*\*", content))


def add_to_uncategorized(
    index_file: Path, entries: List[Dict[str, str]]
) -> None:
    """Insert new entries at the top of the Uncategorized section."""
    if not entries:
        return

    content = index_file.read_text(encoding="utf-8")
    lines = content.split("\n")

    # Find insertion point after header + optional comment
    insert_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "## Uncategorized":
            insert_idx = i + 1
            while insert_idx < len(lines):
                s = lines[insert_idx].strip()
                if s == "" or s.startswith("<!--"):
                    insert_idx += 1
                else:
                    break
            break

    if insert_idx is None:
        log.error(
            f"No {hue.r}## Uncategorized{hue.q} section in index.md — skipping update."
        )
        return

    new_lines: List[str] = []
    for e in entries:
        name = e["name"]
        author = e["first_author"]
        if " and " in e.get("authors", ""):
            author += " et al."
        year = e["year"]
        title = e["title"]
        journal_str = f" *{e['journal']}*" if e.get("journal") else ""
        ref_ext = e.get("ref_ext", ".bib")

        new_lines.append(
            f'- **[{name}]** {author} ({year}) "{title}"{journal_str}'
        )
        new_lines.append(
            f"  - [PDF](./library/{name}/{name}.pdf)"
            f" | [REF](./library/{name}/{name}{ref_ext})"
        )
        new_lines.append("")

    for j, nl in enumerate(new_lines):
        lines.insert(insert_idx + j, nl)

    index_file.write_text("\n".join(lines), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
#  Library BibTeX Builder
# ═══════════════════════════════════════════════════════════════════════

def rebuild_library_bib(library_dir: Path, output: Path) -> int:
    """Rebuild combined library.bib from all papers in library/."""
    bib_entries: List[str] = []

    for paper_dir in sorted(library_dir.iterdir()):
        if not paper_dir.is_dir():
            continue

        bib_files = list(paper_dir.glob("*.bib"))
        if bib_files:
            entry = bib_files[0].read_text(
                encoding="utf-8-sig", errors="replace"
            ).strip()
            if entry:
                bib_entries.append(entry)
            continue

        ris_files = list(paper_dir.glob("*.ris"))
        if ris_files:
            meta = parse_ris(ris_files[0])
            if meta and meta.get("raw_bib"):
                bib_entries.append(meta["raw_bib"].strip())

    combined = "\n\n".join(bib_entries) + "\n" if bib_entries else ""
    output.write_text(combined, encoding="utf-8")
    return len(bib_entries)


# ═══════════════════════════════════════════════════════════════════════
#  ArXiv Downloader
# ═══════════════════════════════════════════════════════════════════════

def _arxiv_xml_to_bib(xml_data: str, arxiv_id: str) -> str:
    """Convert ArXiv API XML response to a BibTeX entry string."""
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_data)
    entry = root.find("atom:entry", ns)
    if entry is None:
        return ""

    title = entry.findtext("atom:title", "", ns).strip().replace("\n", " ")
    title = re.sub(r"\s+", " ", title)

    authors = [
        a.findtext("atom:name", "", ns)
        for a in entry.findall("atom:author", ns)
    ]
    authors_bib = " and ".join(authors)

    published = entry.findtext("atom:published", "", ns)
    year = published[:4] if published else "Unknown"

    first_last = authors[0].split()[-1] if authors else "Unknown"
    cite_key = re.sub(r"[^a-zA-Z]", "", first_last) + year

    return (
        f"@article{{{cite_key},\n"
        f"  author = {{{authors_bib}}},\n"
        f"  title = {{{title}}},\n"
        f"  year = {{{year}}},\n"
        f"  journal = {{arXiv preprint arXiv:{arxiv_id}}},\n"
        f"  eprint = {{{arxiv_id}}},\n"
        f"  archivePrefix = {{arXiv}},\n"
        f"}}\n"
    )


def fetch_arxiv(url: str, inbox: Path) -> bool:
    """Download PDF and BibTeX from an ArXiv URL into the inbox.

    Parses the arXiv ID from the URL, downloads the PDF, queries the
    ArXiv API for metadata, and writes a .bib file — both into inbox/.

    Args:
        url: ArXiv abstract URL (e.g. https://arxiv.org/abs/1706.03762).
        inbox: Path to the inbox directory.

    Returns:
        True if download succeeded, False otherwise.
    """
    match = re.search(r"arxiv\.org/abs/([^\s/?#]+)", url)
    if not match:
        log.error(f"Cannot parse arXiv ID from: {hue.r}{url}{hue.q}")
        return False

    arxiv_id = match.group(1)
    safe_stem = arxiv_id.replace("/", "_")
    log.info(f"Fetching arXiv: {hue.m}{arxiv_id}{hue.q}")

    # Download PDF
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    pdf_path = inbox / f"{safe_stem}.pdf"
    try:
        urllib.request.urlretrieve(pdf_url, str(pdf_path))
        log.info(f"  PDF  -> {hue.c}{pdf_path.name}{hue.q}")
    except Exception as e:
        log.error(f"Failed to download PDF: {hue.r}{e}{hue.q}")
        return False

    # Fetch metadata from ArXiv API and build BibTeX
    api_url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
    try:
        with urllib.request.urlopen(api_url, timeout=15) as resp:
            xml_data = resp.read().decode("utf-8")
    except Exception as e:
        log.error(f"Failed to fetch metadata: {hue.r}{e}{hue.q}")
        return False

    bib_content = _arxiv_xml_to_bib(xml_data, arxiv_id)
    if bib_content:
        bib_path = inbox / f"{safe_stem}.bib"
        bib_path.write_text(bib_content, encoding="utf-8")
        log.info(f"  BIB  -> {hue.c}{bib_path.name}{hue.q}")
    else:
        log.warning("Could not extract BibTeX from API response")

    log.info(f"{hue.g}ArXiv download complete.{hue.q}")
    return True


# ═══════════════════════════════════════════════════════════════════════
#  Initialization
# ═══════════════════════════════════════════════════════════════════════

def ensure_unique_path(base: Path, name: str) -> Tuple[Path, str]:
    """Return a unique directory path, appending _2, _3 if needed."""
    target = base / name
    if not target.exists():
        return target, name
    i = 2
    while True:
        new_name = f"{name}_{i}"
        target = base / new_name
        if not target.exists():
            return target, new_name
        i += 1


def init_folio() -> None:
    """Create directory structure and default files if missing."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)

    if not INDEX_FILE.exists():
        INDEX_FILE.write_text(DEFAULT_INDEX, encoding="utf-8")
        log.info(f"Created: {hue.c}{INDEX_FILE}{hue.q}")

    if not LIBRARY_BIB.exists():
        LIBRARY_BIB.write_text("", encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
#  Core Processing
# ═══════════════════════════════════════════════════════════════════════

def process_inbox(dry_run: bool = False) -> List[Dict[str, str]]:
    """Process all file pairs in inbox. Returns list of new entries."""
    if not dry_run:
        try_clipboard_bib(INBOX_DIR)
    pairs = find_pairs(INBOX_DIR)
    if not pairs:
        log.info("Inbox is empty — nothing to process.")
        return []

    indexed = get_indexed_papers(INDEX_FILE)
    new_entries: List[Dict[str, str]] = []

    for pdf_path, ref_path in pairs:
        suffix = ref_path.suffix.lower()
        metadata = parse_bib(ref_path) if suffix == ".bib" else parse_ris(ref_path)

        if metadata is None:
            log.warning(f"Failed to parse: {hue.y}{ref_path.name}{hue.q}")
            continue

        name = generate_name(metadata)

        if name in indexed:
            log.info(f"Already indexed, skipping: {hue.c}{name}{hue.q}")
            continue

        target_dir, final_name = ensure_unique_path(LIBRARY_DIR, name)

        if dry_run:
            log.info(
                f"{hue.y}[DRY RUN]{hue.q} {hue.c}{pdf_path.name}{hue.q} + "
                f"{hue.c}{ref_path.name}{hue.q} -> {hue.b}{final_name}/{hue.q}"
            )
        else:
            target_dir.mkdir(parents=True)
            shutil.move(str(pdf_path), str(target_dir / f"{final_name}.pdf"))
            shutil.move(str(ref_path), str(target_dir / f"{final_name}{suffix}"))
            log.info(f"{hue.g}Processed:{hue.q} {hue.b}{final_name}{hue.q}")

        new_entries.append({
            "name": final_name,
            "first_author": metadata["first_author"],
            "authors": metadata.get("authors", ""),
            "year": metadata["year"],
            "title": metadata["title"],
            "journal": metadata.get("journal", ""),
            "ref_ext": suffix,
        })

    return new_entries


# ═══════════════════════════════════════════════════════════════════════
#  Entry Point
# ═══════════════════════════════════════════════════════════════════════

def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Folio — lightweight literature manager"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes without moving files",
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Only rebuild library.bib from existing library",
    )
    parser.add_argument(
        "--init", action="store_true",
        help="Initialize directory structure and exit",
    )
    parser.add_argument(
        "--url", type=str, metavar="URL",
        help="ArXiv URL to download (PDF + BibTeX -> inbox, then process)",
    )
    args = parser.parse_args()

    init_folio()

    if args.init:
        log.info(f"Folio initialized at {hue.c}{FOLIO_HOME}{hue.q}")
        return

    if args.rebuild:
        count = rebuild_library_bib(LIBRARY_DIR, LIBRARY_BIB)
        log.info(f"library.bib rebuilt ({hue.m}{count}{hue.q} entries)")
        return

    # ArXiv download: fetch into inbox first, then fall through to process
    if args.url:
        if not fetch_arxiv(args.url, INBOX_DIR):
            return

    log.info(f"{hue.c}Processing inbox...{hue.q}")
    new_entries = process_inbox(dry_run=args.dry_run)

    if new_entries and not args.dry_run:
        add_to_uncategorized(INDEX_FILE, new_entries)
        log.info(
            f"{hue.g}Added {hue.m}{len(new_entries)}{hue.g} paper(s) "
            f"to Uncategorized.{hue.q}"
        )

        count = rebuild_library_bib(LIBRARY_DIR, LIBRARY_BIB)
        log.info(f"library.bib updated ({hue.m}{count}{hue.q} entries total)")

        log.info(f"{hue.d}{'─' * 40}{hue.q}")
        for e in new_entries:
            log.info(f"  {hue.g}+{hue.q} {hue.b}{e['name']}{hue.q}")
        log.info(f"{hue.d}{'─' * 40}{hue.q}")
        log.info(
            f"Move new entries from {hue.m}Uncategorized{hue.q} in index.md "
            f"to your preferred categories."
        )
    elif not new_entries:
        log.info("No new papers to process.")
    else:
        log.info(f"{hue.y}[DRY RUN]{hue.q} No files were moved.")


if __name__ == "__main__":
    main()
