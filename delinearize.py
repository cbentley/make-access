#!/usr/bin/env python3
"""
Resave linearized ("fast web view") PDFs without linearization, in place.

Rosetta's viewer (FlowPaper with PDF.js 1.5) loads pages of large linearized PDFs very
slowly until the whole file has arrived. make_access.py no longer linearizes; this fixes
PDFs made before that change. Lossless: page images and text are copied unchanged.

Usage: python delinearize.py <file.pdf or folder> [...]
Folders are searched recursively. PDFs that aren't linearized are left untouched.
Exit code 1 if any file failed.
"""
import os
import sys
from pathlib import Path

import pikepdf


def find_pdfs(paths: list[str]) -> list[Path]:
    pdfs = []
    for arg in paths:
        path = Path(arg)
        if path.is_dir():
            pdfs.extend(sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"))
        elif path.is_file():
            pdfs.append(path)
        else:
            raise FileNotFoundError(f"Not found: {arg}")
    return pdfs


def delinearize(path: Path) -> bool:
    """Resave path without linearization; return False if it wasn't linearized."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with pikepdf.open(path) as pdf:
            if not pdf.is_linearized:
                return False
            pages = len(pdf.pages)
            pdf.save(tmp)
        with pikepdf.open(tmp) as new:
            if len(new.pages) != pages or new.is_linearized:
                raise RuntimeError("check of the new file failed")
        os.replace(tmp, path)  # Atomic: the original stays intact until the new file is complete
        return True
    finally:
        tmp.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 2
    try:
        pdfs = find_pdfs(argv)
    except FileNotFoundError as e:
        print(e)
        return 2

    changed = skipped = 0
    failed = []
    for path in pdfs:
        try:
            if delinearize(path):
                changed += 1
                print(f"Fixed:   {path}")
            else:
                skipped += 1
                print(f"Skipped: {path} (not linearized)")
        except Exception as e:  # Keep going; report at the end
            failed.append(path)
            print(f"FAILED:  {path}: {e}")

    print(f"\n{changed} fixed, {skipped} already not linearized, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
