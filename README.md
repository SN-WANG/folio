# Folio: A Lightweight Filesystem-Based Literature Manager

[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Folio** is a lightweight, filesystem-based literature manager. Drop a PDF and its BibTeX/RIS citation file into the inbox, run one command, and Folio will automatically rename, organize, and index your papers.

## 🏗 Directory Structure

```
~/folio/
├── folio.py          # Main script
├── _inbox/           # Drop PDF + .bib/.ris pairs here
├── library/          # Organized paper folders
│   └── Author2026_ShortTitle/
│       ├── Author2026_ShortTitle.pdf
│       └── Author2026_ShortTitle.bib
├── index.md          # Categorized paper index
├── library.bib       # Combined BibTeX for all papers
└── README.md
```

## 📦 Workflow

1. **Drop** a PDF and its `.bib` or `.ris` file into `~/folio/_inbox/`
2. **Run** `folio` — files are matched by filename stem (or auto-paired if only one pair exists)
3. **Folio** parses the citation, generates a standardized name (`AuthorYYYY_KeywordKeyword`), and moves both files into `library/`
4. **Index** is updated automatically — new entries appear under `## Uncategorized` in `index.md`
5. **BibTeX** library is rebuilt — `library.bib` is regenerated from all papers

### Clipboard BibTeX Paste

For sources like arXiv that don't offer a `.bib` download button, you can copy the BibTeX to your clipboard instead:

1. Copy a BibTeX entry to clipboard (e.g. from arXiv "Export BibTeX Citation")
2. Drop the lone PDF into `_inbox/` (no `.bib`/`.ris` file needed)
3. Run `folio` — the clipboard content is automatically written as a `.bib` file and paired with the PDF

This only triggers when the inbox has **exactly one** unpaired PDF and **zero** ref files. If multiple lone PDFs are present, Folio warns and skips (ambiguous which PDF the clipboard belongs to). macOS only (`pbpaste`).

### ArXiv URL Import

Import a paper directly from ArXiv in one command:

```bash
folio --url https://arxiv.org/abs/1706.03762
```

Folio parses the arXiv ID from the URL, downloads the PDF, fetches metadata from the ArXiv API, generates a `.bib` file, and then auto-processes the paper into your library — no manual download or clipboard step needed.

Supported URL format: `https://arxiv.org/abs/XXXX.XXXXX`

## 🚀 Usage

```bash
# Process all files in the inbox
folio

# Preview what would happen without moving files
folio --dry-run

# Rebuild library.bib from existing library
folio --rebuild

# Initialize directory structure only
folio --init

# Import from ArXiv URL
folio --url https://arxiv.org/abs/1706.03762
```

## ⚙️ Installation

**Requirements:** Python 3.7+ (no external dependencies)

```bash
git clone https://github.com/SN-WANG/folio.git
cd folio
```

Add an alias to your shell configuration (`~/.zshrc` or `~/.bashrc`):

```bash
alias folio="python3 ~/folio/folio.py"
```

Then reload your shell:

```bash
source ~/.zshrc
```

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

## 📞 Contact

For questions and support, please contact:
- Shengning Wang (王晟宁) — snwang2023@163.com
- Project Website: [https://github.com/SN-WANG/folio](https://github.com/SN-WANG/folio)
