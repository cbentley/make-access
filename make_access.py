#!/usr/bin/env python3
"""
Convert folders of images into OCR-processed, size-limited PDFs, JPEGs and/or JPEG 2000 files.

Usage:
    python make_access.py <folder> -f pdf,jpg,jp2 [options]   (one or more of pdf, jpg, jp2)

See README.md for full documentation.
"""


import argparse
import datetime
import io
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import nullcontext
from fractions import Fraction
from pathlib import Path
from typing import NamedTuple

import img2pdf
import pikepdf
from PIL import Image, features


# ============================================================
# CONFIG
# ============================================================

# --- Input selection ---
ALLOWED_EXT = {".tif", ".tiff", ".jpg", ".jpeg", ".jp2", ".jpf"}
# Other image formats: not converted, but named in a warning, so a page missing from the output doesn't go unnoticed
OTHER_IMAGE_EXT = {".png", ".bmp", ".gif", ".webp", ".heic", ".heif", ".avif", ".jxl", ".psd", ".psb", ".j2k", ".j2c",
                   ".jpx", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf"}
# Images whose filenames contain any of these words (case-insensitive) are skipped
EXCLUDED_WORDS = {"ruler"}
# Folder names that are too generic to name the output; use the parent's name instead, and put the output folder
# next to the image folder rather than inside it. Lowercase.
GENERIC_FOLDER_NAMES = {"master"}
# Output folder for the PDFs, JPEGs and JPEG 2000 files. Folders with this name are never searched for images.
ACCESS_DIR_NAME = "access"

# --- ImageMagick (encodes each image as a JPEG or JPEG 2000; for PDFs, img2pdf wraps the JPEG into a page) ---
# "magick" on ImageMagick 7 (where "convert" is deprecated), "convert" on ImageMagick 6
IMAGEMAGICK_CMD = "magick" if shutil.which("magick") else "convert"
# Read settings, placed before the input file.
# "-limit thread 1" keeps each process single-threaded, since CONVERT_JOBS already runs one per core.
# Outputs keep the scanned object's real size (original pixels / DPI). Uncomment "-density" to treat every image
# as 300 DPI instead; "-units" makes it also apply to images whose DPI is in pixels/cm or missing.
IMAGEMAGICK_INPUT_OPTS = [
    "-limit", "thread", "1",
    # "-density", "300", "-units", "PixelsPerInch",
]
# DPI assumed for PDF page size when an image doesn't store one and -density is not set
FALLBACK_DPI = 300
# sRGB ICC profile, used for color conversion and embedded in every output. Images with an embedded profile
# (e.g. Adobe RGB) are converted to sRGB; untagged RGB images are tagged as sRGB without changing pixels;
# untagged gray/CMYK are left as is. A compact sRGB profile (456 bytes instead of ~7 KB per JPEG and PDF page;
# CC0, from https://github.com/saucecontrol/Compact-ICC-Profiles), kept in the repo so it's the same everywhere.
# Converting with it differs from the standard sRGB.icc by at most 1 level of 255 on the sample scans.
SRGB_PROFILE = str(Path(__file__).resolve().parent / "profiles" / "sRGB-v2-micro.icc")
# Number of images converted in parallel. Lower it if large TIFFs exhaust memory.
CONVERT_JOBS = os.cpu_count() or 1

# --- Image size and JPEG quality, per output ---
# *_MAX_SIZE: scale images down so neither side exceeds this many pixels (None = keep full size).
# *_QUALITY: JPEG quality 1-100. ImageMagick halves color resolution (4:2:0) below 90.
PDF_MAX_SIZE = 2500
PDF_QUALITY = 75
JPG_MAX_SIZE = 2500
JPG_QUALITY = 85
# JP2_QUALITY: target PSNR in dB (higher = closer to the original and larger files), or None for lossless.
# Not the same scale as JPEG quality. Lossy files use the irreversible 9/7 wavelet, lossless ones the 5/3.
JP2_MAX_SIZE = None
JP2_QUALITY = 45
# Further Pillow options for writing JPEG 2000 files (see Pillow's JPEG 2000 docs, e.g. tile_size, num_resolutions).
# RPCL orders the data by resolution first, so viewers can show a smaller version without reading the whole file
# (like a progressive JPEG). PLT markers index the data, so image servers can find a region without decoding it all.
JP2_OPTS = {"progression": "RPCL", "plt": True}

# --- OCRmyPDF ---
OCR_CMD = "ocrmypdf"
# Languages used when --ocr-lang isn't given
DEFAULT_OCR_LANG = "deu+eng"
OCR_OPTS = [
    "--optimize", "3",  # Select 3 for general use, 1 for higher quality (and larger files). 2 and 3 re-encode page images.
    #"--deskew",  # Re-encodes page images
    #"--rotate-pages",  # Use for text; lossless (only sets page rotation)
    "--output-type", "pdf",
]

# ============================================================
# LOGGING SETUP
# ============================================================

logger = logging.getLogger("make_access")

# Inside the input folder; starts with "_" so it's never searched for images
LOGS_DIR_NAME = "_logs"


class WarningCollector(logging.Handler):
    """Keeps the run's warnings (not errors) for the summary at the end, each with the folder being processed when
    it was logged (folder; None before the first folder)."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.folder: Path | None = None
        self.warnings: list[tuple[Path | None, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno == logging.WARNING:
            self.warnings.append((self.folder, record.getMessage().strip()))


def setup_logging(input_folder: Path) -> Path:
    """Log to the console and to a new timestamped file in input_folder/_logs; return the log file's path."""
    logs_dir = input_folder / LOGS_DIR_NAME
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / f"{datetime.datetime.now():%Y%m%d_%H%M%S}_log.txt"
    logger.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return log_file


# ============================================================
# VALIDATION
# ============================================================

def pillow_writes_jp2() -> bool:
    """True if Pillow was built with OpenJPEG, which it needs for JPEG 2000 (PyPI's wheels include it)."""
    return features.check_codec("jpg_2000")


def validate_external_tools(need_ocrmypdf: bool, need_jp2: bool = False) -> None:
    """Verify ImageMagick, the sRGB profile and, for PDFs, OCRmyPDF are available (it runs even with --no-ocr),
    and that Pillow can write JPEG 2000 if it's needed."""
    tools = {f"ImageMagick ({IMAGEMAGICK_CMD})": [IMAGEMAGICK_CMD, "--version"]}
    if need_ocrmypdf:
        tools["OCRmyPDF"] = [OCR_CMD, "--version"]

    for name, cmd in tools.items():
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True
            )
            logger.info(f"{name} found.")
        except Exception:
            logger.error(f"{name} is missing or not working. Check installation.")
            raise

    if need_jp2 and not pillow_writes_jp2():
        logger.error("Pillow can't write JPEG 2000 files (it needs OpenJPEG support). Check installation.")
        raise RuntimeError("Pillow has no JPEG 2000 writer")

    if not Path(SRGB_PROFILE).is_file():
        logger.error(f"sRGB profile not found: {SRGB_PROFILE}. Check SRGB_PROFILE.")
        raise FileNotFoundError(SRGB_PROFILE)
    logger.info(f"sRGB profile found: {SRGB_PROFILE}")


# ============================================================
# IMAGE SELECTION & NAMING
# ============================================================

def is_allowed_ext(path: Path) -> bool:
    """True if path has an allowed image extension."""
    return path.suffix.lower() in ALLOWED_EXT


def is_excluded(path: Path) -> bool:
    """True if the filename contains any word in EXCLUDED_WORDS."""
    name = path.stem.lower()
    return any(word in name for word in EXCLUDED_WORDS)


def is_wanted_image(path: Path) -> bool:
    """True if path has an allowed extension and no excluded word in its name."""
    return is_allowed_ext(path) and not is_excluded(path)


def natural_sort_key(path: Path) -> tuple:
    """Sort key ordering filenames like Windows Explorer: numbers by value (2 before 10), case ignored.
    The full name breaks ties between names that differ only in padding or case (p_1 vs p_01)."""
    # re.split with a capture group alternates text and digit runs, so odd indexes are always digits
    parts = re.split(r"(\d+)", path.name)
    return ([int(t) if i % 2 else t.lower() for i, t in enumerate(parts)], path.name)


def other_images(names: list[str]) -> list[str]:
    """The names of images in a format that isn't converted (OTHER_IMAGE_EXT), in natural order."""
    return sorted((n for n in names if Path(n).suffix.lower() in OTHER_IMAGE_EXT),
                  key=lambda n: natural_sort_key(Path(n)))


def name_list(names: list[str], limit: int = 5) -> str:
    """Names for a message, at most limit of them: "a, b, c and 7 more"."""
    if len(names) <= limit:
        return join_and(names)
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


def select_images(folder: Path) -> list[Path]:
    """Return the folder's wanted images in natural order, logging excluded ones and warning about images in
    a format that isn't converted."""
    files = list(folder.iterdir())
    candidates = [p for p in files if is_allowed_ext(p)]
    images = sorted((p for p in candidates if not is_excluded(p)), key=natural_sort_key)
    skipped = sorted(p.name for p in candidates if is_excluded(p))
    if skipped:
        logger.info(f"  Skipped {len(skipped)} excluded image(s): {', '.join(skipped)}")
    unsupported = other_images([p.name for p in files if p.is_file()])
    if unsupported:
        logger.warning(f"  Skipped {len(unsupported)} image(s) in a format that isn't converted: "
                       f"{name_list(unsupported)}")
    return images


class FolderError(Exception):
    """A problem with a folder's contents that fails the folder; logged as a plain message, without a traceback."""


def jpg_name(image: Path) -> str:
    """Output filename of an image's standalone JPEG."""
    return f"{image.stem}.jpg"


def jp2_name(image: Path) -> str:
    """Output filename of an image's JPEG 2000 file."""
    return f"{image.stem}.jp2"


def command_error_output(e: subprocess.CalledProcessError) -> str:
    """A failed command's error output as text ("" if it wasn't captured)."""
    stderr = e.stderr or ""
    return stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr


def failure_reason(e: BaseException) -> str:
    """One line saying why a folder failed, for the summary at the end of the run. For a failed command, its error
    output's first error line (ImageMagick's says what's wrong with the file) instead of the whole command line."""
    if isinstance(e, subprocess.CalledProcessError):
        lines = [line.strip() for line in command_error_output(e).splitlines() if line.strip()]
        # ImageMagick writes "<program>: <message> `<file>' @ error/<source>" (or "@ warning/", which can come
        # first); keep only the message, the full output is in the log
        for line in lines:
            if match := re.match(r"\S+: (.*?)(?: `[^']*')? @ error/\S+$", line):
                return match[1]
        return f"{Path(str(e.cmd[0])).name} failed (exit code {e.returncode})" + (f": {lines[0]}" if lines else "")
    message = str(e).strip().splitlines()[0] if str(e).strip() else ""
    if isinstance(e, (FolderError, RuntimeError)) and message:
        return message
    return f"{type(e).__name__}: {message}" if message else type(e).__name__


def check_name_clashes(images: list[Path], kinds: str) -> None:
    """Raise if two images would get the same output name (e.g. a.tif and a.jpg both make a.jpg and a.jp2), instead
    of one overwriting the other. kinds names the outputs in the message, e.g. "JPEG". JPEG and JPEG 2000 names both
    come from the image's stem, so one check covers both. Compared case-insensitively, since Windows folders
    (/mnt/c in WSL) don't distinguish case."""
    by_name: dict[str, list[str]] = {}
    for image in images:
        by_name.setdefault(image.stem.lower(), []).append(image.name)
    clashes = [names for names in by_name.values() if len(names) > 1]
    if clashes:
        raise FolderError(
            f"These images would get the same {kinds} name: " + "; ".join(" and ".join(names) for names in clashes)
        )


def join_and(items: list[str]) -> str:
    """Join items for a message: "a", "a and b", "a, b and c"."""
    return " and ".join(filter(None, [", ".join(items[:-1]), items[-1]]))


# ============================================================
# IMAGE CONVERSION (JPEG, JPEG 2000, and single-page PDF)
# ============================================================

# ICC profile color space (header bytes 16-20) expected for each JPEG mode
ICC_COLOR_SPACES = {"L": b"GRAY", "RGB": b"RGB ", "CMYK": b"CMYK"}
# Orientation tags (as ImageMagick names them) that display the image turned by 90°, so width and height swap
ROTATED_ORIENTATIONS = {"LeftTop", "RightTop", "RightBottom", "LeftBottom"}


class ImageInfo(NamedTuple):
    """Size and DPI as displayed, i.e. after applying the file's orientation tag (as -auto-orient does)."""
    width: int
    height: int
    x_dpi: float
    y_dpi: float
    has_dpi: bool  # False if the file stores no DPI (x_dpi/y_dpi are then ImageMagick's placeholder 72)
    frames: int    # Images in the file (multi-page TIFFs, Photoshop layers); only the first is used


def read_image_info(image_path: Path) -> ImageInfo:
    """Read size and DPI of the first image in a file from its header (-ping doesn't decode pixels).
    IMAGEMAGICK_INPUT_OPTS apply, so an uncommented -density overrides the stored DPI.
    ImageMagick reports 72 DPI with units "Undefined" when the file stores no DPI.
    For images whose orientation tag turns them by 90°, width/height and x/y DPI are swapped."""
    cmd = [
        IMAGEMAGICK_CMD,
        *IMAGEMAGICK_INPUT_OPTS,
        "-ping",
        str(image_path),
        "-format", "%w %h %x %y %U %[orientation]\n",  # One line per image in the file
        "info:",
    ]
    result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = result.stdout.splitlines()
    width, height, x_dpi, y_dpi, units, orientation = lines[0].split()
    width, height, x_dpi, y_dpi = int(width), int(height), float(x_dpi), float(y_dpi)
    if units == "PixelsPerCentimeter":
        x_dpi, y_dpi = x_dpi * 2.54, y_dpi * 2.54
    if orientation in ROTATED_ORIENTATIONS:
        width, height, x_dpi, y_dpi = height, width, y_dpi, x_dpi
    has_dpi = units in ("PixelsPerInch", "PixelsPerCentimeter") and x_dpi > 0 and y_dpi > 0
    return ImageInfo(width, height, x_dpi, y_dpi, has_dpi, len(lines))


def strip_mismatched_icc(jpeg: bytes) -> bytes:
    """Remove the ICC profile from a JPEG if it doesn't match the image's color space.
    ImageMagick's -profile tags untagged gray/CMYK images with the sRGB profile without converting them;
    viewers and img2pdf would apply that RGB profile to a CMYK image, giving wrong colors. Pixel data is unchanged."""
    with Image.open(io.BytesIO(jpeg)) as im:
        icc = im.info.get("icc_profile")
        if not icc or icc[16:20] == ICC_COLOR_SPACES.get(im.mode):
            return jpeg
    out = bytearray(jpeg[:2])  # SOI marker
    i = 2
    while i < len(jpeg):
        marker = jpeg[i + 1]
        if marker == 0xDA:  # Start of scan: the rest is image data
            out += jpeg[i:]
            break
        segment = jpeg[i:i + 2 + int.from_bytes(jpeg[i + 2:i + 4], "big")]
        if not (marker == 0xE2 and segment[4:16] == b"ICC_PROFILE\0"):
            out += segment
        i += len(segment)
    return bytes(out)


def resize_scale(info: ImageInfo, max_size: int | None) -> float:
    """Factor an image is scaled by so neither side exceeds max_size (1 if it already fits or max_size is None)."""
    if max_size and max(info.width, info.height) > max_size:
        return max_size / max(info.width, info.height)
    return 1.0


def imagemagick_read_opts(image_path: Path, max_size: int | None, scale: float) -> list[str]:
    """ImageMagick command up to the output settings: read the first image of a file, make it upright, convert it
    to sRGB, remove metadata, and scale it down by scale (from resize_scale) to fit max_size.
    Comments and all profiles except the sRGB one are removed (e.g. EXIF, XMP and Photoshop data, which ImageMagick
    copies from the original): they make every file larger and nothing here uses them. The orientation tag is
    applied first (-auto-orient), so the image stays upright without it."""
    return [
        IMAGEMAGICK_CMD,
        *IMAGEMAGICK_INPUT_OPTS,
        f"{image_path}[0]",  # First image only (multi-page TIFFs, Photoshop layers)
        "-auto-orient",  # Rotate/flip the pixels as the orientation tag says, so the output needs no tag
        "-profile", SRGB_PROFILE,  # Before -resize, so pixels are converted before resizing and encoding
        "+profile", "!icc,*", "+set", "comment",  # Remove all profiles except ICC (now sRGB), and comments
        *(["-resize", f"{max_size}x{max_size}>"] if scale < 1 else []),
    ]


def encode_jpeg(image_path: Path, info: ImageInfo, jpeg_path: Path, max_size: int | None, quality: int,
                standalone: bool) -> None:
    """Encode the first image of a file as an upright sRGB JPEG, scaled down so neither side exceeds max_size
    (if set), without metadata (see imagemagick_read_opts). info must come from read_image_info, so it describes
    the image after -auto-orient.
    Standalone JPEGs (not PDF pages) are progressive, for faster display on the web, and their DPI is scaled
    along with the pixels, so they keep the original's real size; images without a stored DPI are left without one."""
    scale = resize_scale(info, max_size)
    cmd = [
        *imagemagick_read_opts(image_path, max_size, scale),
        *(["-units", "PixelsPerInch", "-density", f"{info.x_dpi * scale:.4f}x{info.y_dpi * scale:.4f}"]
          if standalone and info.has_dpi else []),
        *(["-interlace", "JPEG"] if standalone else []),  # Progressive
        "-quality", str(quality),
        str(jpeg_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    jpeg = jpeg_path.read_bytes()
    stripped = strip_mismatched_icc(jpeg)
    if stripped is not jpeg:
        jpeg_path.write_bytes(stripped)


def jp2_resolution_fields(dpi: float) -> tuple[int, int, int]:
    """Numerator, denominator and exponent of a JP2 resolution in pixels per metre (num / denom * 10**exp), each
    within the box's field sizes (16-bit unsigned; the exponent is kept >= 0, since Pillow reads it unsigned)."""
    exp = 0
    value = Fraction(dpi) / Fraction("0.0254")
    while value > 65535:
        value /= 10
        exp += 1
    fraction = value.limit_denominator(65535)
    while fraction.numerator > 65535:  # A close fraction can have a numerator just over the limit
        fraction = value.limit_denominator(fraction.denominator - 1)
    return fraction.numerator, fraction.denominator, exp


def jp2_box(box_type: bytes, content: bytes) -> bytes:
    return (8 + len(content)).to_bytes(4, "big") + box_type + content


def set_jp2_resolution(jp2: bytes, x_dpi: float, y_dpi: float) -> bytes:
    """Return a JP2 file with its DPI set: a resolution box ("res ") with capture and default display resolution
    ("resc", "resd"), replacing any there is, in the JP2 header box. Pillow's JPEG 2000 writer (OpenJPEG) stores
    no DPI. Only the header changes; the image data is copied as is."""
    hx_num, hx_den, hx_exp = jp2_resolution_fields(x_dpi)
    vy_num, vy_den, vy_exp = jp2_resolution_fields(y_dpi)
    # Fields: vertical num, denom, horizontal num, denom, vertical exponent, horizontal exponent
    fields = (vy_num.to_bytes(2, "big") + vy_den.to_bytes(2, "big") + hx_num.to_bytes(2, "big")
              + hx_den.to_bytes(2, "big") + bytes([vy_exp, hx_exp]))
    res_box = jp2_box(b"res ", jp2_box(b"resc", fields) + jp2_box(b"resd", fields))

    out = bytearray()
    i = 0
    while i < len(jp2):
        length, box_type = int.from_bytes(jp2[i:i + 4], "big"), jp2[i + 4:i + 8]
        if box_type != b"jp2h":
            if length == 0:  # Box runs to the end of the file (the codestream)
                out += jp2[i:]
                break
            if length == 1:  # 64-bit length follows
                length = int.from_bytes(jp2[i + 8:i + 16], "big")
            out += jp2[i:i + length]
            i += length
            continue
        if length < 8:
            raise ValueError("unexpected JP2 header box length")
        # Copy the header's boxes except an existing resolution box, then add the new one
        content = bytearray()
        j, end = i + 8, i + length
        while j < end:
            sub_length, sub_type = int.from_bytes(jp2[j:j + 4], "big"), jp2[j + 4:j + 8]
            if sub_length < 8:
                raise ValueError("unexpected JP2 header box length")
            if sub_type != b"res ":
                content += jp2[j:j + sub_length]
            j += sub_length
        out += jp2_box(b"jp2h", bytes(content) + res_box)
        out += jp2[end:]
        return bytes(out)
    raise ValueError("no JP2 header box found")


def jp2_save_options(mode: str, quality: float | None, extra_options: dict) -> dict:
    """Pillow save options for a JPEG 2000 file: extra_options (JP2_OPTS), plus lossy (9/7 wavelet, quality as
    target PSNR in dB) or, with quality None, lossless (5/3 wavelet). RGB images are converted to a luma/chroma color
    space for compression (mct; Pillow leaves it off by default, which about doubles the file size)."""
    options = {**extra_options, "mct": 1 if mode == "RGB" else 0}
    if quality is None:
        options["irreversible"] = False
    else:
        options.update(irreversible=True, quality_mode="dB", quality_layers=[quality])
    return options


def write_jp2(pnm_path: Path, jp2_path: Path, quality: float | None, extra_options: dict) -> None:
    """Encode an uncompressed image from ImageMagick as a JPEG 2000 file with Pillow (see jp2_save_options).
    Runs in a worker process (encode_jp2): Pillow holds Python's global lock while encoding JPEG 2000, so in
    threads only one image would be encoded at a time. The settings come as arguments, not from the module's
    constants, which a worker process wouldn't see changed."""
    with Image.open(pnm_path) as im:
        # Black-and-white images come as 1-bit, which Pillow can't write as JPEG 2000
        pixels = im.convert("L") if im.mode == "1" else im
        pixels.save(jp2_path, "JPEG2000", **jp2_save_options(pixels.mode, quality, extra_options))


def ignore_ctrl_c() -> None:
    """Worker process initializer: leave Ctrl-C to the main process, which stops the run (otherwise every worker
    prints its own traceback). Running encodes finish first; they take seconds."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def encode_jp2(image_path: Path, info: ImageInfo, jp2_path: Path, max_size: int | None,
               quality: float | None, pool: Executor) -> None:
    """Encode the first image of a file as an upright 8-bit sRGB (or gray) JPEG 2000 file, scaled down so neither
    side exceeds max_size (if set), without metadata (see imagemagick_read_opts). info must come from
    read_image_info. The DPI is scaled along with the pixels, as for standalone JPEGs; images without a stored DPI
    are left without one.
    ImageMagick prepares the pixels and writes them uncompressed (PNM, next to jp2_path; gray if every pixel is
    gray, as its JPEG writer does), then Pillow encodes them with OpenJPEG, in a process from pool (see write_jp2).
    Pillow, unlike ImageMagick, can use the 9/7 wavelet for lossy files, which gives files about a third smaller at
    the same quality.
    JP2 has no CMYK, so ImageMagick converts untagged CMYK images to RGB (without a profile, so colors are
    approximate). Alpha channels are dropped, as they are in JPEGs. The color space is stored as the JP2 code for
    sRGB or gray, not as an ICC profile."""
    scale = resize_scale(info, max_size)
    pnm_path = jp2_path.with_suffix(".pnm")
    cmd = [
        *imagemagick_read_opts(image_path, max_size, scale),
        "-alpha", "off",
        "-depth", "8",  # 16-bit scans would otherwise stay 16-bit
        f"pnm:{pnm_path}",
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pool.submit(write_jp2, pnm_path, jp2_path, quality, JP2_OPTS).result()
    finally:
        pnm_path.unlink(missing_ok=True)
    if info.has_dpi:
        jp2_path.write_bytes(set_jp2_resolution(jp2_path.read_bytes(), info.x_dpi * scale, info.y_dpi * scale))


def page_size_points(info: ImageInfo) -> tuple[float, float]:
    """PDF page size in points: the original image's real size (pixels / DPI, FALLBACK_DPI if it has none)."""
    x_dpi, y_dpi = (info.x_dpi, info.y_dpi) if info.has_dpi else (FALLBACK_DPI, FALLBACK_DPI)
    return info.width / x_dpi * 72, info.height / y_dpi * 72


def convert_image(image_path: Path, page_pdf: Path | None, jpg_path: Path | None, jp2_path: Path | None,
                  jp2_pool: Executor | None = None) -> None:
    """Convert one image into a one-page PDF (page_pdf), a standalone JPEG (jpg_path) and/or a JPEG 2000 file
    (jp2_path, encoded in a process from jp2_pool).
    PDF pages: ImageMagick encodes a JPEG and img2pdf wraps it without re-encoding. (ImageMagick's own PDF writer
    is blocked by the default security policy on Ubuntu 22.04, and adds a thumbnail and, for images with an
    alpha channel, an opaque soft mask to every page.)"""
    info = read_image_info(image_path)
    if info.frames > 1:
        logger.warning(f"  {image_path.name} contains {info.frames} images; using only the first")

    if page_pdf:
        page_jpeg = page_pdf.with_suffix(".jpg")
        encode_jpeg(image_path, info, page_jpeg, PDF_MAX_SIZE, PDF_QUALITY, standalone=False)
        page_size = page_size_points(info)
        # The JPEG is already upright (-auto-orient) and has no EXIF; Rotation.none keeps img2pdf from rotating it
        page_pdf.write_bytes(img2pdf.convert(
            page_jpeg.read_bytes(),
            rotation=img2pdf.Rotation.none,
            layout_fun=lambda *_: (*page_size, *page_size),  # Image fills a page of the original's size
        ))
        page_jpeg.unlink()

    if jpg_path:
        encode_jpeg(image_path, info, jpg_path, JPG_MAX_SIZE, JPG_QUALITY, standalone=True)

    if jp2_path:
        encode_jp2(image_path, info, jp2_path, JP2_MAX_SIZE, JP2_QUALITY, jp2_pool)


def convert_all_images(images: list[Path], single_pages_dir: Path | None, jpg_dir: Path | None,
                       jp2_dir: Path | None) -> list[Path]:
    """Convert images into single-page PDFs in single_pages_dir, JPEGs in jpg_dir and/or JPEG 2000 files in jp2_dir
    (those that aren't None are created). Returns the page PDFs in page order (empty if single_pages_dir is None)."""
    for d in (single_pages_dir, jpg_dir, jp2_dir):
        if d:
            d.mkdir(parents=True, exist_ok=True)
    # Zero-padded so the pages also sort correctly by name
    page_pdfs = [single_pages_dir / f"page{idx:06d}.pdf" for idx in range(len(images))] \
        if single_pages_dir else [None] * len(images)
    jpgs = [jpg_dir / jpg_name(img) for img in images] if jpg_dir else [None] * len(images)
    jp2s = [jp2_dir / jp2_name(img) for img in images] if jp2_dir else [None] * len(images)

    # Processes for Pillow's JPEG 2000 encoding (see write_jp2); the threads wait for them
    jp2_pool_context = (ProcessPoolExecutor(max_workers=CONVERT_JOBS, initializer=ignore_ctrl_c) if jp2_dir
                        else nullcontext())
    with jp2_pool_context as jp2_pool, ThreadPoolExecutor(max_workers=CONVERT_JOBS) as executor:
        futures = [
            executor.submit(convert_image, img, page_pdf, jpg_path, jp2_path, jp2_pool)
            for img, page_pdf, jpg_path, jp2_path in zip(images, page_pdfs, jpgs, jp2s)
        ]
        try:
            for image, future in zip(images, futures):
                try:
                    future.result()
                except Exception as e:
                    if isinstance(e, subprocess.CalledProcessError) and command_error_output(e).strip():
                        logger.error(f"  ImageMagick error output for {image.name}:\n{command_error_output(e)}")
                    raise RuntimeError(f"Can't convert {image.name}: {failure_reason(e)}") from e
        except BaseException:
            # Don't start queued conversions after a failure or Ctrl-C
            executor.shutdown(cancel_futures=True)
            raise

    outputs = [name for name, wanted in (("PDF pages", single_pages_dir), ("JPEGs", jpg_dir),
                                         ("JPEG 2000 files", jp2_dir)) if wanted]
    logger.info(f"  Converted {len(images)} images to {join_and(outputs)}")
    return page_pdfs if single_pages_dir else []


# ============================================================
# PDF: MERGE (PRE-OCR), OCR, SPLIT (POST-OCR)
# ============================================================

def size_mb(path: Path) -> float:
    """Return the size of a file in megabytes."""
    return path.stat().st_size / 1_000_000


def merge_pdfs(pdf_files: list[Path], output_pdf: Path) -> Path:
    """Merge pdf_files, in order, into a single PDF at output_pdf."""
    with pikepdf.Pdf.new() as merged:
        for pdf in pdf_files:
            with pikepdf.open(pdf) as src:
                merged.pages.extend(src.pages)
        merged.save(output_pdf)

    logger.info(f"  Merged {len(pdf_files)} PDFs into {output_pdf.name}")
    return output_pdf


def ocr_pdf(input_pdf: Path, output_pdf: Path, ocr_lang: str | None) -> None:
    """
    Run OCRmyPDF on a PDF, writing the result to output_pdf.

    With ocr_lang None, text recognition is skipped ("--tesseract-timeout 0") but the
    rest of OCR_OPTS (e.g. --optimize) still applies. "-l" is then left out, so OCRmyPDF
    only checks for English, which comes with Tesseract.
    Raises RuntimeError if OCRmyPDF fails (there is no fallback).
    """
    no_ocr = ocr_lang is None
    cmd = [
        OCR_CMD,
        *OCR_OPTS,
        # Never linearize ("fast web view", by default for files over 1 MB): in Rosetta's viewer
        # (FlowPaper with PDF.js 1.5), pages of a linearized PDF load very slowly until the whole
        # file has arrived; the same PDF not linearized is fast. Split parts aren't linearized either.
        "--fast-web-view", "999999",
        *(["--tesseract-timeout", "0"] if no_ocr else ["-l", ocr_lang]),
        str(input_pdf),
        str(output_pdf),
    ]

    action = "Optimizing (no OCR)" if no_ocr else "OCRing"
    logger.info(f"  {action} {input_pdf.name} -> {output_pdf.name}...")

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        logger.error(f"OCRmyPDF error output for {input_pdf.name}:\n{result.stderr}")
        raise RuntimeError(
            f"OCR failed for {input_pdf.name} (OCRmyPDF exit code {result.returncode})"
        )

    logger.info(f"  Finished {'optimizing' if no_ocr else 'OCR'}: {output_pdf.name}")


def page_object_sizes(page: pikepdf.Page) -> dict[tuple[int, int], int]:
    """
    Approximate the saved size in bytes of each indirect object a page uses
    (content streams, images, fonts), keyed by object id so objects shared
    between pages can be counted once.
    """
    sizes: dict[tuple[int, int], int] = {}
    stack = [page.obj]
    while stack:
        obj = stack.pop()
        if not isinstance(obj, pikepdf.Object):
            continue  # numbers and booleans come back as plain Python values
        if obj.is_indirect:
            if obj.objgen in sizes:
                continue
            if isinstance(obj, pikepdf.Stream):
                size = len(obj.read_raw_bytes()) + len(obj.stream_dict.unparse())
            else:
                size = len(obj.unparse(resolved=True))
            sizes[obj.objgen] = size
        if isinstance(obj, pikepdf.Stream):
            obj = obj.stream_dict
        if isinstance(obj, pikepdf.Dictionary):
            # /Parent leads back up to the page tree and every other page
            stack.extend(v for k, v in obj.items() if k != "/Parent")
        elif isinstance(obj, pikepdf.Array):
            stack.extend(obj)
    return sizes


def save_page_range(src: pikepdf.Pdf, start: int, end: int, path: Path) -> float:
    """Save pages [start, end) of src as a new PDF at path; return its size in MB."""
    with pikepdf.Pdf.new() as pdf:
        pdf.pages.extend(src.pages[start:end])
        pdf.save(path)
    return size_mb(path)


def split_pdf_by_size(input_pdf: Path, output_dir: Path, max_mb: int) -> list[Path]:
    """
    Split a PDF into parts of at most max_mb, each holding as many pages as fit.

    Pages per part are first estimated from page_object_sizes, then corrected
    with real saves: pages are removed while the part is over max_mb and added
    while it still fits. This gives the same parts as adding and saving one
    page at a time, with only a few saves per part. A page alone in its part
    stays there even if oversized (it can't be split).

    Output files are named "<stem>-part01.pdf", "<stem>-part02.pdf", etc.
    """
    part_pdfs: list[Path] = []
    max_bytes = max_mb * 1_000_000

    with pikepdf.open(input_pdf) as src:
        page_sizes = [page_object_sizes(page) for page in src.pages]
        page_count = len(page_sizes)

        start = 0
        while start < page_count:
            # Estimate: add pages while the part's distinct objects fit (at least one page)
            end = start
            seen: set[tuple[int, int]] = set()
            estimate = 0
            while end < page_count:
                new = {k: v for k, v in page_sizes[end].items() if k not in seen}
                if end > start and estimate + sum(new.values()) > max_bytes:
                    break
                seen.update(new)
                estimate += sum(new.values())
                end += 1

            part_pdf = output_dir / f"{input_pdf.stem}-part{len(part_pdfs) + 1:02d}.pdf"
            trial_pdf = part_pdf.with_suffix(".trial.pdf")
            size_mb = save_page_range(src, start, end, part_pdf)

            # Estimate too high: remove pages until the part fits
            while size_mb > max_mb and end - start > 1:
                end -= 1
                size_mb = save_page_range(src, start, end, part_pdf)

            # Estimate too low: add pages while the part still fits
            while size_mb <= max_mb and end < page_count:
                trial_mb = save_page_range(src, start, end + 1, trial_pdf)
                if trial_mb > max_mb:
                    trial_pdf.unlink()
                    break
                trial_pdf.replace(part_pdf)
                end += 1
                size_mb = trial_mb

            if size_mb > max_mb:
                logger.warning(
                    f"  Page {start + 1} alone is {size_mb:.1f} MB (> {max_mb} MB); "
                    f"keeping it as an oversized part"
                )
            logger.info(f"  Finalized {part_pdf.name} ({size_mb:.1f} MB, {end - start} pages)")
            part_pdfs.append(part_pdf)
            start = end

    return part_pdfs


# ============================================================
# COMMAND LINE
# ============================================================

# Values of -f, and other spellings accepted for them
FORMATS = ["pdf", "jpg", "jp2"]
FORMAT_ALIASES = {"jpeg": "jpg"}
DEFAULT_MAX_MB = 100


def parse_formats(value: str) -> list[str]:
    """argparse type for -f: a comma-separated list of formats (spaces and case ignored, aliases resolved)."""
    names = [name.strip().lower() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if FORMAT_ALIASES.get(name, name) not in FORMATS]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown format: {', '.join(unknown)} (choose from {', '.join(FORMATS)})")
    if not names:
        raise argparse.ArgumentTypeError(f"no format given (choose from {', '.join(FORMATS)})")
    return [FORMAT_ALIASES.get(name, name) for name in names]


def parse_args(argv=None) -> argparse.Namespace:
    """Parse the command line. Sets make_pdf, make_jpg and make_jp2 from -f; exits (code 2) on an unknown format,
    a PDF option without -f pdf, or a folder that doesn't exist."""
    generic_names = " or ".join(sorted(GENERIC_FOLDER_NAMES))
    parser = argparse.ArgumentParser(
        description="Convert folders of scanned images to OCR'd PDFs, JPEGs and/or JPEG 2000 files.\n"
                    "Every image folder gets its outputs in an access folder inside it (next to it for a\n"
                    f"folder named {generic_names}): one PDF per folder, one JPEG or JPEG 2000 file per image.",
        epilog="""examples:
  %(prog)s . -f pdf,jpg
  %(prog)s /scans -f pdf
  %(prog)s /scans -f pdf,jpg,jp2 --max-mb 50 --ocr-lang eng
  %(prog)s -f jp2 /scans""",
        formatter_class=argparse.RawDescriptionHelpFormatter,  # Keep the epilog's line breaks
    )
    parser.add_argument(
        "folder",
        help="Folder with the scans. Subfolders are searched too, and each folder with images is converted on its own."
    )
    parser.add_argument(
        "-f", "--format",
        dest="formats",
        type=parse_formats,
        action="extend",  # -f pdf -f jpg is the same as -f pdf,jpg
        required=True,
        metavar="FORMATS",
        help="What to make, separated by commas: pdf (an OCR'd, size-limited PDF per image folder), jpg (a JPEG "
             "per image; jpeg also works), jp2 (a JPEG 2000 file per image). Example: -f pdf,jpg"
    )
    pdf_options = parser.add_argument_group("PDF options (only with -f pdf)")
    pdf_options.add_argument(
        "--max-mb",
        type=int,
        help=f"Maximum size in MB for each final PDF before splitting. Default: {DEFAULT_MAX_MB}"
    )
    pdf_options.add_argument(
        "--ocr-lang",
        help=f'Languages to use for OCR. Examples: "eng", "deu", "deu+eng". Default: {DEFAULT_OCR_LANG}'
    )
    pdf_options.add_argument(
        "--no-ocr",
        action="store_true",
        help="Skip text recognition. OCRmyPDF still runs, so OCR_OPTS such as --optimize still apply."
    )

    args = parser.parse_args(argv)
    args.make_pdf, args.make_jpg, args.make_jp2 = (name in args.formats for name in FORMATS)

    given_pdf_options = [option for option, given in (("--max-mb", args.max_mb is not None),
                                                      ("--ocr-lang", args.ocr_lang is not None),
                                                      ("--no-ocr", args.no_ocr)) if given]
    if given_pdf_options and not args.make_pdf:
        verb = "applies" if len(given_pdf_options) == 1 else "apply"
        parser.error(f"{' and '.join(given_pdf_options)} only {verb} with -f pdf")
    if not Path(args.folder).is_dir():
        parser.error(f"folder not found: {args.folder}")
    if args.max_mb is None:
        args.max_mb = DEFAULT_MAX_MB
    # Left as None with --no-ocr, so main() can tell if a given --ocr-lang is ignored
    if args.make_pdf and not args.no_ocr and args.ocr_lang is None:
        args.ocr_lang = DEFAULT_OCR_LANG
    return args


# ============================================================
# OUTPUT FILES
# ============================================================

def is_generic_folder(folder: Path) -> bool:
    return folder.name.lower() in GENERIC_FOLDER_NAMES


def output_base_name(folder: Path) -> str:
    """Return the base name for a folder's final PDF(s)."""
    return folder.parent.name if is_generic_folder(folder) else folder.name


def output_dir(folder: Path) -> Path:
    """Return the folder an image folder's outputs go to: next to a generic folder, else inside it."""
    return (folder.parent if is_generic_folder(folder) else folder) / ACCESS_DIR_NAME


def output_dir_clashes(folders: list[Path]) -> dict[Path, list[Path]]:
    """Map each folder that shares its output folder with other image folders (e.g. b01 and b01/master both
    write to b01/access) to those other folders. Their PDFs would get the same name and overwrite each other."""
    by_output: dict[Path, list[Path]] = {}
    for folder in folders:
        by_output.setdefault(output_dir(folder), []).append(folder)
    return {folder: [other for other in group if other != folder]
            for group in by_output.values() if len(group) > 1 for folder in group}


def is_output_dir(name: str) -> bool:
    """True for the script's own output folder, which is never searched for images."""
    return name.lower() == ACCESS_DIR_NAME.lower()


def move_outputs(files: list[Path], dest_dir: Path) -> None:
    """Move finished files from _work into dest_dir, replacing files with the same name."""
    dest_dir.mkdir(exist_ok=True)
    for file in files:
        file.replace(dest_dir / file.name)


PDF_SUFFIXES = {".pdf"}
JPG_SUFFIXES = {".jpg", ".jpeg"}
JP2_SUFFIXES = {".jp2", ".jpf"}
# Each kind of old output, as (singular, plural, extensions), for remove_old_outputs's message
OUTPUT_KINDS = [
    ("PDF", "PDFs", PDF_SUFFIXES),
    ("JPEG", "JPEGs", JPG_SUFFIXES),
    ("JPEG 2000 file", "JPEG 2000 files", JP2_SUFFIXES),
]


def output_suffixes(args: argparse.Namespace) -> set[str]:
    """Extensions of the files a run replaces in the access folder: those of each kind of output it makes."""
    return ((PDF_SUFFIXES if args.make_pdf else set()) | (JPG_SUFFIXES if args.make_jpg else set())
            | (JP2_SUFFIXES if args.make_jp2 else set()))


def old_outputs(dest_dir: Path, suffixes: set[str]) -> list[Path]:
    """Return the files directly in dest_dir with one of the suffixes (case-insensitive). They're all replaced by
    the new outputs; each access folder belongs to one image folder (see output_dir_clashes)."""
    if not dest_dir.is_dir():
        return []
    return sorted(p for p in dest_dir.iterdir() if p.is_file() and p.suffix.lower() in suffixes)


def check_not_locked(files: list[Path]) -> None:
    """Raise if a file can't be opened for writing, usually because a Windows program has it open (e.g. a PDF in
    Acrobat or PDFgear; Photoshop doesn't keep JPEGs open). Windows then also blocks deleting or replacing it.
    Files inside WSL (not under /mnt) are never locked this way."""
    locked = []
    for file in files:
        try:
            with open(file, "r+b"):
                pass
        except OSError:
            locked.append(file.name)
    if locked:
        question = "Is it open" if len(locked) == 1 else "Are they open"
        it = "it" if len(locked) == 1 else "them"
        raise FolderError(
            f"Can't replace {', '.join(locked)} in {files[0].parent}. {question} in another program? "
            f"Close {it} and run again."
        )


def remove_old_outputs(files: list[Path]) -> None:
    """Delete the outputs of earlier runs, just before the new ones are moved in, and log how many of each kind."""
    for file in files:
        file.unlink()
    removed = []
    for singular, plural, suffixes in OUTPUT_KINDS:
        n = sum(file.suffix.lower() in suffixes for file in files)
        if n:
            removed.append(f"{n} {singular if n == 1 else plural}")
    if removed:
        logger.info(f"  Removed {join_and(removed)} from an earlier run")


# ============================================================
# FOLDER PROCESSING
# ============================================================

# Inside each image folder; starts with "_" so it's never searched for images
WORK_DIR_NAME = "_work"


def find_image_folders(input_folder: Path) -> Iterator[Path]:
    """Yield input_folder and every folder below it that directly contains wanted images.
    Folders starting with "_" (work and log folders) and the output folders are skipped, with their subfolders.
    Warns about folders that have only images in a format that isn't converted (select_images warns about the
    others)."""
    for root, dirs, files in os.walk(input_folder):
        dirs[:] = [d for d in dirs if not d.startswith("_") and not is_output_dir(d)]
        if any(is_wanted_image(Path(f)) for f in files):
            yield Path(root)
        elif unsupported := other_images(files):
            logger.warning(f"Skipped {root}: its {len(unsupported)} image(s) are in a format that isn't converted: "
                           f"{name_list(unsupported)}")


def make_final_pdfs(page_pdfs: list[Path], work_dir: Path, base_name: str, args: argparse.Namespace) -> list[Path]:
    """Merge the page PDFs, OCR the result and split it if it's over --max-mb (steps 2-4).
    Returns the final PDFs, still in work_dir."""
    pre_ocr_dir = work_dir / "02_pre_ocr"
    ocr_dir = work_dir / "03_ocr"
    split_dir = work_dir / "04_split"
    for d in (pre_ocr_dir, ocr_dir, split_dir):
        d.mkdir(exist_ok=True)

    # 2. Merge all pages into one PDF, so a single OCRmyPDF run can use every core
    pre_ocr_pdf = merge_pdfs(page_pdfs, pre_ocr_dir / f"{base_name}.pdf")

    # 3. OCR (with --no-ocr, only the rest of OCR_OPTS applies)
    ocr_output = ocr_dir / f"{base_name}.pdf"
    ocr_pdf(pre_ocr_pdf, ocr_output, None if args.no_ocr else args.ocr_lang)

    # 4. Split if over --max-mb
    ocr_size_mb = size_mb(ocr_output)
    if ocr_size_mb <= args.max_mb:
        logger.info(f"  PDF {ocr_output.name} is {ocr_size_mb:.1f} MB (<= {args.max_mb} MB); no splitting needed.")
        return [ocr_output]
    logger.info(f"  PDF {ocr_output.name} is {ocr_size_mb:.1f} MB (>{args.max_mb} MB); splitting...")
    return split_pdf_by_size(ocr_output, split_dir, args.max_mb)


def convert_folder(folder: Path, args: argparse.Namespace) -> None:
    """Convert one image folder's images into PDFs, JPEGs and/or JPEG 2000 files in its access folder. Intermediate files go to folder/_work;
    outputs are moved out only after every step succeeded. Raises on failure."""
    images = select_images(folder)
    if not images:
        # Only if the images vanished since find_image_folders listed them
        logger.warning("  No valid images found.")
        return
    logger.info(f"  Found {len(images)} images")
    image_kinds = [kind for kind, wanted in (("JPEG", args.make_jpg), ("JPEG 2000", args.make_jp2)) if wanted]
    if image_kinds:
        check_name_clashes(images, join_and(image_kinds))

    # 1. Convert each image into a single-page PDF, a JPEG and/or a JPEG 2000 file
    work_dir = folder / WORK_DIR_NAME
    jpg_work_dir = work_dir / "jpg"
    jp2_work_dir = work_dir / "jp2"
    page_pdfs = convert_all_images(
        images,
        work_dir / "01_single_pages" if args.make_pdf else None,
        jpg_work_dir if args.make_jpg else None,
        jp2_work_dir if args.make_jp2 else None,
    )

    # 2-4. Merge, OCR and split
    base_name = output_base_name(folder)
    final_pdfs = make_final_pdfs(page_pdfs, work_dir, base_name, args) if args.make_pdf else []

    # 5. Move the outputs into the access folder
    dest_dir = output_dir(folder)
    old_files = old_outputs(dest_dir, output_suffixes(args))
    check_not_locked(old_files)  # Again, in case a file was opened during the run
    remove_old_outputs(old_files)
    if args.make_pdf:
        move_outputs(final_pdfs, dest_dir)
        for pdf in final_pdfs:
            logger.info(f"  Final output written: {dest_dir / pdf.name}")
    if args.make_jpg:
        move_outputs(sorted(jpg_work_dir.iterdir()), dest_dir)
        logger.info(f"  {len(images)} JPEGs written to: {dest_dir}")
    if args.make_jp2:
        move_outputs(sorted(jp2_work_dir.iterdir()), dest_dir)
        logger.info(f"  {len(images)} JPEG 2000 files written to: {dest_dir}")


def process_folder(folder: Path, args: argparse.Namespace) -> str | None:
    """Run convert_folder, log any failure and clean up. Returns None if the folder succeeded, else the reason it
    failed (one line, for the summary).

    _work is deleted, except after unexpected failures and Ctrl-C, where it's kept for debugging. A FolderError
    is an expected problem with the folder's contents, logged without a traceback. Ctrl-C stops the whole run."""
    logger.info(f"\nProcessing folder: {folder}")
    work_dir = folder / WORK_DIR_NAME
    success = False
    keep_work = False
    reason = None
    try:
        convert_folder(folder, args)
        success = True
    except KeyboardInterrupt:
        keep_work = True
        logger.error("Interrupted by user (Ctrl-C); leaving _work for debugging.")
        raise
    except FolderError as e:
        reason = failure_reason(e)
        logger.error(f"Folder failed: {folder}\n  {e}")
    except Exception as e:
        keep_work = True
        reason = failure_reason(e)
        logger.exception(f"Folder failed: {folder}; leaving _work for debugging.")
    finally:
        if keep_work:
            logger.info(f"  Preserving work directory for debugging: {work_dir}")
        elif work_dir.exists():
            try:
                shutil.rmtree(work_dir)
                if success:
                    logger.info(f"  Cleaned up work directory: {work_dir}")
            except Exception:
                logger.exception(f"  Failed to remove work directory: {work_dir}")

    return reason


# ============================================================
# MAIN
# ============================================================

def log_summary(input_folder: Path, folders: list[Path], failed: dict[Path, str],
                warnings: list[tuple[Path | None, str]]) -> int:
    """Log the end of the run: the warnings, grouped by folder, then the failed folders with their reasons, or
    that all folders were converted. Returns the exit code: 1 if a folder failed or no image folder was found."""
    if warnings:
        logger.warning(f"\n{len(warnings)} warning(s):")
        for message in (message for folder, message in warnings if folder is None):
            logger.warning(f"  {message}")
        for folder in dict.fromkeys(folder for folder, _ in warnings if folder is not None):
            logger.warning(f"  {folder}")
            for message in (message for f, message in warnings if f == folder):
                logger.warning(f"    {message}")

    if not folders:
        logger.error(f"\nNo images found in {input_folder} or its subfolders. "
                     f"Only {join_and(sorted(ALLOWED_EXT))} files are converted.")
        return 1
    if failed:
        logger.error(f"\nFinished with {len(failed)} failed folder(s):")
        for folder, reason in failed.items():
            logger.error(f"  {folder}\n    {reason}")
        return 1
    if warnings:
        logger.warning("\nAll folders converted, but check the warnings above.")
    else:
        logger.info("\nAll folders converted.")
    return 0


def main(argv=None) -> int:
    """Convert every image folder under the input folder. Returns the exit code: 0 if all folders succeeded, 1 if
    a folder failed or no image folder was found."""
    args = parse_args(argv)
    input_folder = Path(args.folder).resolve()

    log_file = setup_logging(input_folder)
    warnings = WarningCollector()
    logger.addHandler(warnings)
    outputs = join_and([name for name, wanted in (("PDFs", args.make_pdf), ("JPEGs", args.make_jpg),
                                                  ("JPEG 2000 files", args.make_jp2)) if wanted])
    logger.info(f"Converting images to {outputs} in {input_folder}")
    if args.make_pdf and args.no_ocr and args.ocr_lang:
        logger.warning(f"Ignoring --ocr-lang {args.ocr_lang}: no text is recognized with --no-ocr")
    validate_external_tools(need_ocrmypdf=args.make_pdf, need_jp2=args.make_jp2)

    folders = list(find_image_folders(input_folder))
    clashes = output_dir_clashes(folders)
    # Checked for every folder before any is converted, so an open file shows up at once, not after hours of OCR
    locked = {}
    for folder in folders:
        try:
            check_not_locked(old_outputs(output_dir(folder), output_suffixes(args)))
        except FolderError as e:
            locked[folder] = e
    failed: dict[Path, str] = {}
    for folder in folders:
        warnings.folder = folder
        # The first two fail before anything is written or cleaned up, so the access folder is left alone
        if folder in clashes:
            others = ", ".join(str(other) for other in clashes[folder])
            failed[folder] = (f"Its output folder {output_dir(folder)} is also used by the images in {others}. "
                              f"Move one set of images into its own folder.")
            logger.error(f"\nFolder failed: {folder}\n  {failed[folder]}")
        elif folder in locked:
            failed[folder] = failure_reason(locked[folder])
            logger.error(f"\nFolder failed: {folder}\n  {locked[folder]}")
        elif (reason := process_folder(folder, args)) is not None:
            failed[folder] = reason

    logger.removeHandler(warnings)  # The summary's own warnings aren't collected
    exit_code = log_summary(input_folder, folders, failed, warnings.warnings)
    logger.info(f"Log written to: {log_file}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
