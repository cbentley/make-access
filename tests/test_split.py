"""Tests of merging and size-capped splitting, on PDFs built with pikepdf; no external tools needed."""
import random
from pathlib import Path

import pikepdf
import pytest

import make_access as ci

MB = 1_000_000


def make_pdf(path: Path, page_kb: list[int], shared_kb: int = 0, seed: int = 0, first_index: int = 0) -> Path:
    """Write a PDF with one page per page_kb entry, each holding an incompressible image of that many KB.
    With shared_kb, every page also references one shared object (like the OCR font).
    Each page records its index (from first_index) in /TestIndex, to check page order."""
    rng = random.Random(seed)
    with pikepdf.Pdf.new() as pdf:
        shared = pdf.make_stream(rng.randbytes(shared_kb * 1000)) if shared_kb else None
        for idx, kb in enumerate(page_kb):
            image = pdf.make_stream(rng.randbytes(kb * 1000))
            image.Type, image.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
            image.Width, image.Height, image.BitsPerComponent = 1, 1, 8
            image.ColorSpace = pikepdf.Name.DeviceGray
            resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=image))
            if shared is not None:
                resources.Shared = shared
            page = pikepdf.Page(pikepdf.Dictionary(
                Type=pikepdf.Name.Page,
                MediaBox=[0, 0, 100, 100],
                Resources=resources,
                Contents=pdf.make_stream(b"q 100 0 0 100 0 0 cm /Im0 Do Q"),
            ))
            pdf.pages.append(page)
            pdf.pages[-1].obj.TestIndex = first_index + idx
        pdf.save(path)
    return path


def page_indexes(path: Path) -> list[int]:
    with pikepdf.open(path) as pdf:
        return [int(page.obj.TestIndex) for page in pdf.pages]


def reference_split(src_path: Path, max_mb: int, tmp: Path) -> list[int]:
    """Pages per part when adding one page at a time and saving after each: the behavior split_pdf_by_size promises."""
    counts = []
    with pikepdf.open(src_path) as src:
        start, total = 0, len(src.pages)
        while start < total:
            end = start + 1
            while end < total and ci.save_page_range(src, start, end + 1, tmp / "ref.pdf") <= max_mb:
                end += 1
            counts.append(end - start)
            start = end
    return counts


@pytest.mark.parametrize("seed", range(4))
def test_split_matches_one_page_at_a_time_reference(tmp_path, seed):
    rng = random.Random(seed)
    page_kb = [rng.randint(20, 450) for _ in range(rng.randint(10, 25))]
    src = make_pdf(tmp_path / "in.pdf", page_kb, shared_kb=rng.choice([0, 150]), seed=seed)
    out = tmp_path / "out"
    out.mkdir()

    parts = ci.split_pdf_by_size(src, out, max_mb=1)

    assert [p.name for p in parts] == [f"in-part{i:02d}.pdf" for i in range(1, len(parts) + 1)]
    assert [len(page_indexes(p)) for p in parts] == reference_split(src, 1, tmp_path)
    assert [i for p in parts for i in page_indexes(p)] == list(range(len(page_kb)))
    assert all(p.stat().st_size <= MB for p in parts)
    assert not list(out.glob("*.trial.pdf"))


def test_split_keeps_oversized_page_as_its_own_part(tmp_path, caplog):
    src = make_pdf(tmp_path / "in.pdf", [300, 1500, 300, 300])
    parts = ci.split_pdf_by_size(src, tmp_path, max_mb=1)
    assert [page_indexes(p) for p in parts] == [[0], [1], [2, 3]]
    assert parts[1].stat().st_size > MB
    assert "Page 2 alone is" in caplog.text


def test_merge_pdfs_keeps_order(tmp_path):
    pages = [make_pdf(tmp_path / f"p{i}.pdf", [1, 1], first_index=2 * i) for i in range(3)]
    merged = ci.merge_pdfs(pages, tmp_path / "merged.pdf")
    assert page_indexes(merged) == list(range(6))


def random_pdf(tmp_path: Path, seed: int) -> Path:
    rng = random.Random(seed)
    page_kb = [rng.randint(20, 450) for _ in range(rng.randint(10, 25))]
    return make_pdf(tmp_path / "in.pdf", page_kb, shared_kb=150, seed=seed)


@pytest.mark.parametrize("factor", [0.5, 2.0])
def test_split_corrects_wrong_estimates(tmp_path, monkeypatch, factor):
    """Estimates that are too low (0.5) or too high (2.0) must be corrected by real saves, giving the same parts."""
    src = random_pdf(tmp_path, seed=1)
    expected = reference_split(src, 1, tmp_path)
    estimate = ci.page_object_sizes
    monkeypatch.setattr(ci, "page_object_sizes",
                        lambda page: {k: int(v * factor) for k, v in estimate(page).items()})
    parts = ci.split_pdf_by_size(src, tmp_path, max_mb=1)
    assert [len(page_indexes(p)) for p in parts] == expected


def test_split_needs_at_most_two_saves_per_part(tmp_path, monkeypatch):
    """With accurate estimates (shared objects counted once), each part takes one save plus one failed trial."""
    src = random_pdf(tmp_path, seed=2)
    saves = 0
    save = ci.save_page_range

    def counting_save(*args):
        nonlocal saves
        saves += 1
        return save(*args)

    monkeypatch.setattr(ci, "save_page_range", counting_save)
    parts = ci.split_pdf_by_size(src, tmp_path, max_mb=1)
    assert len(parts) > 2
    assert saves <= 2 * len(parts)
