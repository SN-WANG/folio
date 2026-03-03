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
"""

import argparse
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─── Paths ─────────────────────────────────────────────────────────────
FOLIO_HOME = Path.home() / "folio"
INBOX_DIR = FOLIO_HOME / "_inbox"
LIBRARY_DIR = FOLIO_HOME / "library"
INDEX_FILE = FOLIO_HOME / "index.md"
LIBRARY_BIB = FOLIO_HOME / "library.bib"

# ─── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="[folio] %(message)s")
log = logging.getLogger("folio")

# ─── Naming Constants ─────────────────────────────────────────────────
SHORT_TITLE_WORDS = 2
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

def generate_name(metadata: Dict[str, str]) -> str:
    """Generate standardized name: AuthorYYYY_KeywordKeyword."""
    author = re.sub(r"[^a-zA-Z]", "", metadata.get("first_author", "Unknown"))
    year = metadata.get("year", "Unknown")

    title = metadata.get("title", "Untitled")
    words = re.findall(r"[a-zA-Z]+", title)
    keywords = [w.capitalize() for w in words if w.lower() not in STOP_WORDS]

    short_title = "".join(keywords[:SHORT_TITLE_WORDS]) if keywords else "Untitled"

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
        log.info(f"Clipboard BibTeX → {bib_path.name}")
    else:
        log.warning(
            f"{len(unpaired)} PDFs in inbox but only one clipboard — "
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
        log.info(f"Auto-paired: {pdf.name} <-> {ref.name}")
    else:
        for s, p in upm.items():
            log.warning(f"No matching bib/ris for: {p.name}")
        for s, r in urm.items():
            log.warning(f"No matching PDF for: {r.name}")

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
        log.error("No '## Uncategorized' section in index.md — skipping update.")
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
        log.info(f"Created: {INDEX_FILE}")

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
            log.warning(f"Failed to parse: {ref_path.name}")
            continue

        name = generate_name(metadata)

        if name in indexed:
            log.info(f"Already indexed, skipping: {name}")
            continue

        target_dir, final_name = ensure_unique_path(LIBRARY_DIR, name)

        if dry_run:
            log.info(f"[DRY RUN] {pdf_path.name} + {ref_path.name} -> {final_name}/")
        else:
            target_dir.mkdir(parents=True)
            shutil.move(str(pdf_path), str(target_dir / f"{final_name}.pdf"))
            shutil.move(str(ref_path), str(target_dir / f"{final_name}{suffix}"))
            log.info(f"Processed: {final_name}")

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
    args = parser.parse_args()

    init_folio()

    if args.init:
        log.info(f"Folio initialized at {FOLIO_HOME}")
        return

    if args.rebuild:
        count = rebuild_library_bib(LIBRARY_DIR, LIBRARY_BIB)
        log.info(f"library.bib rebuilt ({count} entries)")
        return

    log.info("Processing inbox...")
    new_entries = process_inbox(dry_run=args.dry_run)

    if new_entries and not args.dry_run:
        add_to_uncategorized(INDEX_FILE, new_entries)
        log.info(f"Added {len(new_entries)} paper(s) to Uncategorized.")

        count = rebuild_library_bib(LIBRARY_DIR, LIBRARY_BIB)
        log.info(f"library.bib updated ({count} entries total)")

        log.info("─" * 40)
        for e in new_entries:
            log.info(f"  + {e['name']}")
        log.info("─" * 40)
        log.info(
            'Move new entries from "Uncategorized" in index.md '
            'to your preferred categories.'
        )
    elif not new_entries:
        log.info("No new papers to process.")
    else:
        log.info("[DRY RUN] No files were moved.")


if __name__ == "__main__":
    main()
