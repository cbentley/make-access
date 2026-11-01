"""Tests of the pure-Python parts; no ImageMagick or OCRmyPDF needed."""
import argparse
import io
import os
from pathlib import Path

import pytest
from PIL import Image, ImageCms

import make_access as ci


def touch(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).touch()


# --- Image selection & naming ---

def test_natural_sort_key_orders_numbers_by_value_and_ignores_case():
    names = ["scan_10.tif", "Scan_2.tif", "scan_1.tif", "scan_01.tif", "SCAN_3.tif"]
    ordered = sorted((Path(n) for n in names), key=ci.natural_sort_key)
    # scan_01 and scan_1 tie by value; the full name breaks the tie
    assert [p.name for p in ordered] == ["scan_01.tif", "scan_1.tif", "Scan_2.tif", "SCAN_3.tif", "scan_10.tif"]


@pytest.mark.parametrize("name, wanted", [
    ("a.tif", True),
    ("a.TIFF", True),
    ("a.jpg", True),
    ("a.JPEG", True),
    ("a.jp2", True),
    ("a.jpf", True),
    ("a.png", False),
    ("a.pdf", False),
    ("a-ruler.tif", False),
    ("RULER_a.tif", False),
])
def test_is_wanted_image(name, wanted):
    assert ci.is_wanted_image(Path(name)) is wanted


def test_select_images_returns_wanted_images_in_natural_order(tmp_path):
    touch(tmp_path, "p10.tif", "p2.jpg", "p1.TIF", "ruler.tif", "notes.txt")
    assert [p.name for p in ci.select_images(tmp_path)] == ["p1.TIF", "p2.jpg", "p10.tif"]


def test_find_image_folders_skips_work_log_and_output_folders(tmp_path):
    touch(tmp_path, "top.tif")
    touch(tmp_path / "a" / "master", "1.tif")
    touch(tmp_path / "a" / "master" / "deeper", "2.jpg")
    touch(tmp_path / "b", "only-ruler.tif", "readme.txt")
    for skipped in ("_work", "_logs", "access", "Access"):
        touch(tmp_path / "a" / skipped, "x.jpg")
        touch(tmp_path / "a" / skipped / "sub", "x.jpg")
    found = sorted(ci.find_image_folders(tmp_path))
    assert found == [tmp_path, tmp_path / "a" / "master", tmp_path / "a" / "master" / "deeper"]


@pytest.mark.parametrize("folder, name", [
    ("b01-f01/master", "b01-f01"),
    ("b01-f01/Master", "b01-f01"),
    ("b01-f01/scans", "scans"),
])
def test_output_base_name(folder, name):
    assert ci.output_base_name(Path("/data") / folder) == name


@pytest.mark.parametrize("folder, out", [
    ("b01-f01/master", "b01-f01/access"),
    ("b01-f01/Master", "b01-f01/access"),
    ("b01-f01/scans", "b01-f01/scans/access"),
])
def test_output_dir(folder, out):
    assert ci.output_dir(Path("/data") / folder) == Path("/data") / out


def test_output_dir_clashes():
    data = Path("/data")
    folders = [data / "b01", data / "b01" / "master", data / "b01" / "scans", data / "b02" / "master"]
    assert ci.output_dir_clashes(folders) == {
        data / "b01": [data / "b01" / "master"],
        data / "b01" / "master": [data / "b01"],
    }


def test_check_name_clashes_is_case_insensitive():
    with pytest.raises(ci.FolderError, match="same JPEG 2000 name: a.tif and A.jpg"):
        ci.check_name_clashes([Path("a.tif"), Path("A.jpg"), Path("b.tif")], "JPEG 2000")


def test_check_name_clashes_accepts_distinct_names():
    ci.check_name_clashes([Path("a.tif"), Path("b.jpg")], "JPEG")


@pytest.mark.parametrize("items, joined", [
    (["a"], "a"),
    (["a", "b"], "a and b"),
    (["a", "b", "c"], "a, b and c"),
])
def test_join_and(items, joined):
    assert ci.join_and(items) == joined


# --- Image conversion helpers ---

def test_page_size_points_uses_dpi():
    info = ci.ImageInfo(width=600, height=400, x_dpi=400, y_dpi=200, has_dpi=True, frames=1)
    assert ci.page_size_points(info) == (108, 144)


def test_page_size_points_falls_back_without_dpi():
    info = ci.ImageInfo(width=600, height=300, x_dpi=72, y_dpi=72, has_dpi=False, frames=1)
    assert ci.page_size_points(info) == (600 / ci.FALLBACK_DPI * 72, 300 / ci.FALLBACK_DPI * 72)


SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def jpeg_bytes(mode: str, icc: bytes | None) -> bytes:
    out = io.BytesIO()
    Image.new(mode, (16, 16)).save(out, "JPEG", **({"icc_profile": icc} if icc else {}))
    return out.getvalue()


@pytest.mark.parametrize("mode", ["CMYK", "L"])
def test_strip_mismatched_icc_removes_rgb_profile_from_non_rgb_jpeg(mode):
    jpeg = jpeg_bytes(mode, SRGB_ICC)
    stripped = ci.strip_mismatched_icc(jpeg)
    with Image.open(io.BytesIO(stripped)) as im:
        assert "icc_profile" not in im.info
        assert im.mode == mode
    # Only the profile segment is removed
    with Image.open(io.BytesIO(jpeg)) as before, Image.open(io.BytesIO(stripped)) as after:
        assert before.tobytes() == after.tobytes()


@pytest.mark.parametrize("icc", [SRGB_ICC, None])
def test_strip_mismatched_icc_keeps_matching_or_missing_profile(icc):
    jpeg = jpeg_bytes("RGB", icc)
    assert ci.strip_mismatched_icc(jpeg) is jpeg


def jp2_bytes(mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (16, 16)).save(out, "JPEG2000")
    return out.getvalue()


@pytest.mark.parametrize("dpi", [(300, 300), (118.11 * 2.54, 118.11 * 2.54), (400, 150), (72, 72), (4800, 2400),
                                 (1.5, 0.1)])
def test_set_jp2_resolution_is_read_back(dpi):
    jp2 = jp2_bytes()
    with_dpi = ci.set_jp2_resolution(jp2, *dpi)
    with Image.open(io.BytesIO(with_dpi)) as im:
        assert im.info["dpi"] == pytest.approx(dpi, rel=1e-4)
    # Only the header changes: the image data is the same
    with Image.open(io.BytesIO(jp2)) as before, Image.open(io.BytesIO(with_dpi)) as after:
        assert before.tobytes() == after.tobytes()


def test_set_jp2_resolution_replaces_existing_resolution():
    jp2 = ci.set_jp2_resolution(ci.set_jp2_resolution(jp2_bytes("L"), 300, 300), 600, 200)
    assert jp2.count(b"res ") == 1
    with Image.open(io.BytesIO(jp2)) as im:
        assert im.info["dpi"] == pytest.approx((600, 200), rel=1e-4)
        assert im.mode == "L"


def test_set_jp2_resolution_rejects_codestream_without_jp2_header():
    out = io.BytesIO()
    Image.new("RGB", (16, 16)).save(out, "JPEG2000", no_jp2=True)  # Bare codestream (.j2k), no boxes
    with pytest.raises(ValueError):
        ci.set_jp2_resolution(out.getvalue(), 300, 300)


@pytest.mark.parametrize("dpi", [72, 300, 485, 600, 4800, 100_000])
def test_jp2_resolution_fields_fit_and_are_accurate(dpi):
    num, den, exp = ci.jp2_resolution_fields(dpi)
    assert 0 < num <= 65535 and 0 < den <= 65535 and 0 <= exp <= 255
    assert num / den * 10 ** exp * 0.0254 == pytest.approx(dpi, rel=1e-4)


def test_pillow_writes_jp2():
    assert ci.pillow_writes_jp2()


@pytest.mark.parametrize("mode, quality, expected", [
    ("RGB", 50, {"mct": 1, "irreversible": True, "quality_mode": "dB", "quality_layers": [50]}),
    ("L", 47.5, {"mct": 0, "irreversible": True, "quality_mode": "dB", "quality_layers": [47.5]}),
    ("RGB", None, {"mct": 1, "irreversible": False}),
])
def test_jp2_save_options(mode, quality, expected):
    assert ci.jp2_save_options(mode, quality, {"plt": True}) == {"plt": True, **expected}


# --- Command line ---

def test_parse_args_pdf_defaults(tmp_path):
    args = ci.parse_args([str(tmp_path), "-f", "pdf"])
    assert (args.make_pdf, args.make_jpg, args.make_jp2) == (True, False, False)
    assert args.max_mb == ci.DEFAULT_MAX_MB
    assert args.ocr_lang == ci.DEFAULT_OCR_LANG
    assert args.no_ocr is False


def test_parse_args_pdf_options(tmp_path):
    args = ci.parse_args([str(tmp_path), "-f", "pdf,jpg,jp2", "--max-mb", "5", "--ocr-lang", "eng"])
    assert (args.make_pdf, args.make_jpg, args.make_jp2, args.max_mb, args.ocr_lang) == (True, True, True, 5, "eng")
    assert args.folder == str(tmp_path)


def test_parse_args_no_ocr_leaves_language_unset(tmp_path):
    assert ci.parse_args([str(tmp_path), "-f", "pdf", "--no-ocr"]).ocr_lang is None
    assert ci.parse_args([str(tmp_path), "-f", "pdf", "--no-ocr", "--ocr-lang", "fra"]).ocr_lang == "fra"


@pytest.mark.parametrize("formats, made", [
    (["-f", "jpg"], (False, True, False)),
    (["-f", "jpeg"], (False, True, False)),
    (["-f", "JPG,Jp2"], (False, True, True)),
    (["-f", "jp2"], (False, False, True)),
    (["-f", "jp2,jpeg,jpg"], (False, True, True)),
    (["-f", "pdf, jpg"], (True, True, False)),
    (["-f", "jpg,"], (False, True, False)),
    (["-f", "jpg", "-f", "pdf"], (True, True, False)),
    (["--format", "pdf,jp2"], (True, False, True)),
    (["--format=pdf,jp2"], (True, False, True)),
])
def test_parse_args_formats(tmp_path, formats, made):
    # The folder works both before and after -f
    for argv in ([str(tmp_path), *formats], [*formats, str(tmp_path)]):
        args = ci.parse_args(argv)
        assert (args.make_pdf, args.make_jpg, args.make_jp2) == made, argv
        assert args.folder == str(tmp_path), argv


@pytest.mark.parametrize("argv, message", [
    (["/does/not/exist", "-f", "pdf"], "folder not found"),
    ([".", "-f", "pdf", "--max-mb", "big"], "invalid int"),
    ([".", "-f", "jpg", "--max-mb", "5"], "--max-mb only applies with -f pdf"),
    ([".", "-f", "jp2", "--no-ocr", "--ocr-lang", "eng"], "--ocr-lang and --no-ocr only apply with -f pdf"),
    ([".", "-f", "pdf,png"], "unknown format: png (choose from pdf, jpg, jp2)"),
    ([".", "-f", ","], "no format given"),
    ([".", "-f", "pdf", "jpg"], "unrecognized arguments: jpg"),  # Formats need commas
    (["-f", "pdf"], "required: folder"),
    (["."], "required: -f"),
    (["pdf", "."], "required: -f"),  # The old subcommand syntax
    ([], "required: folder, -f"),
])
def test_parse_args_errors_exit_with_code_2(capsys, argv, message):
    with pytest.raises(SystemExit) as exc:
        ci.parse_args(argv)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


# --- Output files ---

def test_move_outputs_replaces_same_name_and_keeps_others(tmp_path):
    work, dest = tmp_path / "work", tmp_path / "dest"
    touch(dest, "keep.pdf")
    (dest / "a.pdf").write_text("old")
    work.mkdir()
    (work / "a.pdf").write_text("new")
    ci.move_outputs([work / "a.pdf"], dest)
    assert (dest / "a.pdf").read_text() == "new"
    assert (dest / "keep.pdf").exists()
    assert not (work / "a.pdf").exists()


@pytest.mark.parametrize("make_pdf, make_jpg, make_jp2, expected", [
    (True, False, False, ["a.pdf", "b.PDF"]),
    (False, True, False, ["c.jpg", "d.JPEG"]),
    (False, False, True, ["i.jp2", "j.JPF"]),
    (True, True, False, ["a.pdf", "b.PDF", "c.jpg", "d.JPEG"]),
    (True, True, True, ["a.pdf", "b.PDF", "c.jpg", "d.JPEG", "i.jp2", "j.JPF"]),
])
def test_old_outputs_selects_by_command(tmp_path, make_pdf, make_jpg, make_jp2, expected):
    touch(tmp_path, "a.pdf", "b.PDF", "c.jpg", "d.JPEG", "e.tif", "i.jp2", "j.JPF", "notes.txt", "g.pdf.bak")
    touch(tmp_path / "sub", "f.pdf", "h.jpg", "k.jp2")  # Subfolders are left alone
    args = argparse.Namespace(make_pdf=make_pdf, make_jpg=make_jpg, make_jp2=make_jp2)
    assert [p.name for p in ci.old_outputs(tmp_path, ci.output_suffixes(args))] == expected


def test_old_outputs_missing_folder(tmp_path):
    assert ci.old_outputs(tmp_path / "missing", {".pdf"}) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to read-only files")
def test_check_not_locked_names_files_that_cant_be_written(tmp_path):
    touch(tmp_path, "a.pdf", "b.pdf", "c.pdf")
    files = sorted(tmp_path.iterdir())
    ci.check_not_locked(files)
    for name in ("a.pdf", "c.pdf"):
        (tmp_path / name).chmod(0o444)  # Like a file open in a Windows program: opening for writing fails
    with pytest.raises(ci.FolderError, match=r"Can't replace a\.pdf, c\.pdf in .*Are they open"):
        ci.check_not_locked(files)


@pytest.mark.parametrize("names, message", [
    (["a.pdf", "b.PDF", "c.jpg"], "Removed 2 PDFs and 1 JPEG from an earlier run"),
    (["a.jpg", "b.jpeg"], "Removed 2 JPEGs from an earlier run"),
    (["a.pdf"], "Removed 1 PDF from an earlier run"),
    (["a.jp2", "b.pdf", "c.jpg"], "Removed 1 PDF, 1 JPEG and 1 JPEG 2000 file from an earlier run"),
    (["a.jp2", "b.JPF"], "Removed 2 JPEG 2000 files from an earlier run"),
])
def test_remove_old_outputs_logs_counts(tmp_path, caplog, names, message):
    touch(tmp_path, *names, "keep.pdf")
    with caplog.at_level("INFO", logger=ci.logger.name):
        ci.remove_old_outputs([tmp_path / name for name in names])
    assert [p.name for p in tmp_path.iterdir()] == ["keep.pdf"]
    assert caplog.messages == [f"  {message}"]


def test_remove_old_outputs_logs_nothing_without_files(caplog):
    with caplog.at_level("INFO", logger=ci.logger.name):
        ci.remove_old_outputs([])
    assert caplog.messages == []
