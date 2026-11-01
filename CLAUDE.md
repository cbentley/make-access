# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A single-file CLI (`make_access.py`) that turns folders of scanned images (`.tif/.tiff/.jpg/.jpeg/.jp2/.jpf`) into OCR'd, size-capped PDFs, standalone JPEGs and/or JPEG 2000 files, chosen with `-f` (comma-separated, any order: `pdf`, `jpg`, `jp2`; `jpeg` is accepted as `jpg`); the input folder is the positional argument. `README.md` documents user-facing behavior and options.

## Setup & running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # pikepdf, img2pdf, Pillow
python make_access.py /path/to/images -f pdf[,jpg][,jp2] [--max-mb=100] [--ocr-lang=deu+eng] [--no-ocr]
python make_access.py /path/to/images -f jpg,jp2
```

External tools must be on PATH: ImageMagick, OCRmyPDF (with Ghostscript and Tesseract language packs), and `pngquant` (needed by OCRmyPDF for `--optimize 2/3`). `jbig2` is unused, since every page is JPEG and OCRmyPDF only applies JBIG2 to 1-bit images. `validate_external_tools()` checks ImageMagick and the sRGB profile (`SRGB_PROFILE`, in the repo) at startup, plus OCRmyPDF for PDFs (not `pngquant`) and, when JPEG 2000 is made, that Pillow has its OpenJPEG codec (`pillow_writes_jp2`; PyPI's wheels include it).

Tests (pytest; `pip install -r requirements-dev.txt`):

```bash
python -m pytest                     # all tests, a few seconds
python -m pytest -m "not integration"  # only the pure-Python tests
```

- `tests/test_unit.py`: selection, naming, sorting, `strip_mismatched_icc`, `set_jp2_resolution` (read back with Pillow), `parse_args`, output-file helpers (incl. old-output selection and the lock check).
- `tests/test_split.py`: `split_pdf_by_size` against a reference that adds and saves one page at a time, with estimates deliberately scaled wrong (so both correction loops are exercised) and a save-count check (so a bad estimate shows up). Built with pikepdf, no external tools.
- `tests/test_pipeline.py` (marked `integration`): runs `main()` on images generated with ImageMagick at test time (no binary fixtures) and checks page order and sizes, DPI, color profiles, text layer, splitting, `--no-ocr`, JPEG 2000 coding (wavelet, color transform, progression and PLT markers, read from the codestream by `jp2_coding`), and failure handling. Skipped if ImageMagick, `SRGB_PROFILE`, OCRmyPDF or Tesseract's `eng` data is missing; the Adobe RGB test also needs `icc-profiles-free`'s `AdobeCompat-v2.icc`.
- Tests check properties (size, DPI, color space, profile), not exact pixels or bytes, so they hold across ImageMagick 6/7 and OCRmyPDF versions. `conftest.py` removes the logger handlers `main()` adds.

For changes the tests don't cover, also run the script on a sample folder; logs go to `<input>/_logs/<timestamp>_log.txt`. There is no linter config or build step.

## Architecture

`find_image_folders()` walks the input folder and `main()` processes **every subfolder that directly contains images independently** (each folder produces its own `<foldername>.pdf` and/or JPEGs; images are not aggregated across folders). Directories whose names start with `_` are pruned from the walk, which is how the script's own `_work` and `_logs` dirs are excluded; so are output folders (`ACCESS_DIR_NAME`, matched case-insensitively), since the JPEGs would otherwise be picked up as input. Each folder goes through `process_folder()` (failure handling and `_work` cleanup), which calls `convert_folder()` (the steps below; `make_final_pdfs()` does steps 2-4). `parse_args` turns `-f` into `args.make_pdf`, `args.make_jpg` and `args.make_jp2` (the rest of the code only looks at these); the folder steps are shared and run the PDF steps only when `make_pdf` is set. `-f` takes a single comma-separated value (`parse_formats` as its argparse `type`: splits, strips, lowercases, resolves `FORMAT_ALIASES`; `action="extend"`, so `-f jpg -f pdf` works too). Not `nargs="+"`: that would swallow a folder given after the formats. It exits with code 2 for a missing folder or `-f`, an unknown format, a PDF option (`--max-mb`, `--ocr-lang`, `--no-ocr`) without `-f pdf` (so `--max-mb` defaults to `None` and is set to `DEFAULT_MAX_MB` afterwards), or a folder that doesn't exist.

Per-folder stages, each writing to a subdir of `<folder>/_work/`:

1. `01_single_pages` (PDF), `jpg` (JPEG) and `jp2` (JPEG 2000) — `convert_all_images` runs `convert_image` per image in parallel (`CONVERT_JOBS` threads, each running ImageMagick limited to one thread via `-limit thread 1`); a failure cancels queued conversions. `convert_image` first runs `read_image_info` (ImageMagick `-ping ... -format ... info:`, header only, with `IMAGEMAGICK_INPUT_OPTS` so an uncommented `-density` applies; for orientation tags that turn the image by 90° it swaps width/height and x/y DPI, so `ImageInfo` describes the upright image), then `encode_jpeg` (PDF page, JPEG) or `encode_jp2` once per requested output with that output's `*_MAX_SIZE` and `*_QUALITY` (`-f pdf,jpg` runs ImageMagick twice per image). Both build their command on `imagemagick_read_opts` (read, orient, sRGB, strip metadata, resize; described here for JPEG). `encode_jpeg` reads only the first image of the file (`<path>[0]`; multi-image files are logged with a warning), applies the orientation tag with `-auto-orient` (rotates/flips the pixels; covers TIFF tags and JPEG EXIF, including mirrored orientations), converts to sRGB via `-profile SRGB_PROFILE` before `-resize` (`profiles/sRGB-v2-micro.icc`, a 456-byte CC0 sRGB profile kept in the repo because Ubuntu 22.04/24.04's `icc-profiles-free` lacks it; converting with it differs from the standard `sRGB.icc` by at most 1 level of 255 on the sample scans, and it's ~6.5 KB smaller in every output), and runs `strip_mismatched_icc`, which drops that sRGB tag from untagged gray/CMYK images (wrong colors in img2pdf and viewers otherwise). Standalone JPEGs (`standalone=True`) are progressive (`-interlace JPEG`, for the web viewer); PDF page JPEGs stay baseline. `-profile` converts images with an embedded profile (e.g. Adobe RGB masters) to sRGB, tags untagged RGB as sRGB without changing pixels, and leaves untagged gray/CMYK alone; the profile survives OCRmyPDF to the final PDF. ImageMagick uses 4:2:0 chroma below quality 90. For PDF pages, img2pdf wraps the JPEG into a one-page PDF without re-encoding (`Rotation.none`: the JPEG is already upright and has no EXIF, so img2pdf must not rotate it; pages never get `/Rotate`). ImageMagick doesn't write the PDF itself: Ubuntu 22.04's default ImageMagick policy blocks the PDF coder, and its PDF writer adds a thumbnail and an opaque soft mask (for images with alpha) to every page. This JPEG encode is the only lossy step unless `OCR_OPTS` includes an option that re-encodes images (see below).
   `encode_jp2` has ImageMagick write the prepared pixels uncompressed (`-alpha off -depth 8`, to `pnm:<name>.pnm` next to the output; PNM picks gray (P5) when every pixel is gray and 1-bit (P4) for bilevel images, which `write_jp2` turns into `L`; CMYK becomes RGB without a profile), then Pillow encodes it (`write_jp2`, with `jp2_save_options`): lossy uses `irreversible=True` (9/7 wavelet) with `quality_mode="dB"`, `quality_layers=[JP2_QUALITY]` (a target PSNR, not the JPEG scale), `JP2_QUALITY = None` is lossless (5/3); `mct=1` for RGB (Pillow's default 0 skips the color transform and about doubles the size); plus `JP2_OPTS` (e.g. progression order and PLT markers). Pillow, not ImageMagick, encodes because ImageMagick always uses the 5/3 wavelet, which gave files 30-40% larger at the same PSNR/SSIM on the sample scans (Pillow with ImageMagick's settings gives byte-identical sizes: same OpenJPEG). Pillow holds the GIL while encoding JPEG 2000, so `write_jp2` runs in a `ProcessPoolExecutor` (one per folder, made by `convert_all_images` only for JP2; its workers ignore SIGINT so Ctrl-C prints one traceback); the conversion threads wait on it. Settings go to the worker as arguments, since a worker process (forkserver on Python 3.14) doesn't see constants patched at runtime (tests monkeypatch them). The `.pnm` is deleted in a `finally`. OpenJPEG writes the color space as an enumerated sRGB/gray `colr` box (no ICC profile) and **no resolution**, so `set_jp2_resolution` rewrites the `jp2h` box with a `res ` box (`resc` and `resd`, both the scaled DPI; replacing any existing one); the codestream is copied unchanged. Exponents stay >= 0 because Pillow reads them unsigned. ImageMagick (OpenJPEG) never reads JP2 resolution back (always 72/Undefined), so check DPI with Pillow.
2. `02_pre_ocr` (PDF) — `merge_pdfs` merges all pages into one PDF (single save). OCRmyPDF parallelizes across pages itself, so one run per folder keeps all cores busy.
3. `03_ocr` (PDF) — `ocr_pdf` runs OCRmyPDF with `OCR_OPTS` plus `-l <ocr-lang>` (default `DEFAULT_OCR_LANG`). The `--optimize` level is a user-tunable size/quality trade-off: don't assume or document a specific value; read `OCR_OPTS` for the current one. `--no-ocr` adds `--tesseract-timeout 0` and leaves out `-l` (OCRmyPDF still checks that the languages are installed, so a given `--ocr-lang` would otherwise fail the folder if missing; it's ignored with a warning): no text layer, but OCRmyPDF still runs (and still rasterizes every page, so it saves only the Tesseract time). If OCRmyPDF fails it raises, failing the whole folder (no fallback).
4. `04_split` (PDF) — if the OCR'd PDF is larger than `--max-mb`, `split_pdf_by_size` splits it into `-partNN` files; otherwise the OCR output is the final PDF.
5. Only after every step succeeded, `move_outputs` moves the final PDFs and the JPEGs into the access folder from `output_dir()`: `<folder>/access/`, or `<parent>/access/` if the folder is in `GENERIC_FOLDER_NAMES`. Just before, every old file of the kinds being made is deleted (`old_outputs` with `output_suffixes`: all `.pdf` when `make_pdf`, all `.jpg`/`.jpeg` when `make_jpg`, all `.jp2`/`.jpf` when `make_jp2`, case-insensitive, files directly in the access folder only; `remove_old_outputs`, which logs how many of each kind in `OUTPUT_KINDS` it removed, not their names), including files added by hand, so no stale parts or JPEGs of removed images are left. That's safe because each access folder belongs to exactly one image folder (see `output_dir_clashes`). `check_not_locked` first opens each of them for writing and raises a `FolderError` naming them if that fails: on `/mnt/c`, Acrobat and PDFgear lock open PDFs (open, rename and delete all fail with `Permission denied`); Photoshop doesn't keep JPEGs open, and files inside WSL are never locked. The check runs in `main()` for every folder before any is converted (so an open file is reported at once, not after hours of OCR) and again in step 5 (a file opened during the run); a delete that fails after a passed check is an unexpected error. Also before any folder is processed, `main()` fails every folder whose access folder is shared with another image folder (`output_dir_clashes`, e.g. `b01` and `b01/master` both with images), without calling `process_folder`, so nothing is written or cleaned up there; their PDFs would get the same name. `_work` is deleted on success; it's preserved after unexpected failures (exceptions, Ctrl-C) but deleted after a `FolderError` (an expected problem with the folder's contents, logged as a plain message without a traceback). A failed folder leaves its access folder untouched (old files are only removed together with moving in the new ones, so the last successful run's set stays complete). Failed folders are listed at the end of the run and `main()` returns exit code 1.

Implementation notes:
- Splitting fills each part greedily (as many pages as fit under the cap; an oversized single page is kept as its own part, with a warning). To avoid a full save per page, `page_object_sizes` estimates each page's bytes (shared objects like the OCR font counted once per part), then real saves correct the estimate: drop pages while over the cap, add pages while still under. The result matches saving after every page; with accurate estimates that's ~2 saves per part. Keep it exact: the cap is user-facing.
- Input images are ordered by `natural_sort_key` (numbers by value, case-insensitive, full name as tie-breaker). Internally, ordering relies on lexical sort of filenames, so names use zero padding (`page000000.pdf` for single pages, `-part01` in the final split). Preserve this when changing naming.
- Output names: PDFs come from `output_base_name()`, the image folder's name or its parent's name if the folder is in `GENERIC_FOLDER_NAMES` (currently `master`; entries lowercase); JPEGs and JPEG 2000 files keep the image's stem (`jpg_name()`, `jp2_name()`). `check_name_clashes` fails the folder (`FolderError`) when two images would get the same JPEG/JPEG 2000 name (same stem), compared case-insensitively since Windows folders under `/mnt/c` are case-insensitive. Folders with TIFF + JPEG pairs of the same pages (e.g. `a.tif` and `a.jpg`) therefore fail in JPEG and JPEG 2000 mode, and in PDF mode get every page twice; that's a known, accepted behavior.
- Image selection goes through `is_wanted_image()` (allowed extension and no `EXCLUDED_WORDS` substring in the filename, case-insensitive). Both `find_image_folders()` and `select_images` use it; keep them in sync.
- Image quality and size are set by step 1 (`PDF_QUALITY`/`PDF_MAX_SIZE` for PDF pages, `JPG_QUALITY`/`JPG_MAX_SIZE` for JPEGs, `JP2_QUALITY`/`JP2_MAX_SIZE` for JPEG 2000) and, for PDFs, possibly by OCRmyPDF re-encoding. Several OCRmyPDF options silently re-encode every page image: `--optimize 2/3` (JPEG q75/q40) and `--deskew` (Pillow default q75). They add a second lossy encode; that can be intentional (e.g. `--optimize 3` for smaller files). `--rotate-pages` is lossless (sets page rotation only).
- Output keeps the scanned object's real size, computed from `read_image_info` before any resizing (pixels ÷ DPI). PDF pages get that size via an img2pdf layout function (`page_size_points`), so resizing doesn't shrink the page; files without a DPI (ImageMagick reports 72 with units `Undefined`) use `FALLBACK_DPI`. Resized JPEGs get their DPI scaled by the resize factor (`-units PixelsPerInch -density` after `-resize`; for JPEG 2000 via `set_jp2_resolution`); JPEGs and JPEG 2000 files of files without a DPI stay without one. Pixels/cm are converted to DPI. `-density` in `IMAGEMAGICK_INPUT_OPTS` is commented out; uncommented, it's paired with `-units PixelsPerInch` so it applies to every image, including those with pixels/cm or no DPI.
- ImageMagick copies XMP and Photoshop (8BIM) metadata and comments from TIFFs into its JPEGs (EXIF and the comment too from JPEG inputs). `imagemagick_read_opts` removes all of it from every JPEG and JPEG 2000 file written, PDF pages included, with `+profile '!icc,*' +set comment` (every profile except the sRGB ICC one, and the comment): ~20-27 KB per file on the sample scans, and img2pdf would otherwise embed it in each page as is (OCRmyPDF only drops it when it re-encodes). The orientation tag is removed with it, which is fine because `-auto-orient` has already applied it. DPI (JFIF header) is kept. The `!name,*` syntax is documented for both ImageMagick 6 and 7, but has only been tested on 7.
- `IMAGEMAGICK_CMD` is `"magick"` when it's on PATH (ImageMagick 7, where `convert` is deprecated) and `"convert"` otherwise (ImageMagick 6, which has no `magick`). Every ImageMagick option the script uses must work with both versions.

## In progress: sharing the script with a non-programmer coworker on Windows/WSL

**Unblocked:** the script (then `convert_images.py`, now `make_access.py`; `pdf`/`jpg` subcommands, `pdf/` and `jpg/` output folders) is merged to `main` (`95bb779`). The steps below are updated for it: a choice of PDF, JPEGs or both, and a self-test covering both outputs. The GitHub repo was renamed from `image-to-pdf` to `convert-images` and is being renamed to `make-access` (old URLs redirect); the paths below use the new name.

Goal: a coworker who isn't a programmer runs the script on their Windows PC through WSL (Ubuntu) without ever using a terminal. It must work reliably, so every step is scripted and tested, and anything that can't be tested from the Mac is listed for the handover test.

### Decisions (and why)

- **Run inside WSL Ubuntu**, not native Windows (ImageMagick/OCRmyPDF/Tesseract are painful to install there) and not Docker (too much to support for a non-programmer). Fallback if WSL proves unreliable: run the script on a machine we control and have the coworker drop scans into a shared watched folder.
- **Ubuntu version: an LTS, installed by explicit name** (e.g. `wsl --install -d Ubuntu-26.04`; check exact names with `wsl --list --online`). Plain `wsl --install` gives whatever "Ubuntu" currently points to, and a non-LTS or future release may bring a Python that `pikepdf>=10.0.2,<10.1` has no wheels for. The pipeline as of `f5be165` was tested on 22.04, 24.04 and 26.04; everything since has only run on 26.04 (see Testing).
- **Install from a git clone** of the public repo (`https://github.com/cbentley/make-access`, HTTPS so no credentials) into `~/make-access` inside WSL, tracking `main`, with the venv at `env/`. Updates are `git pull` plus `pip install -r requirements.txt`. Nothing generated for this machine (launchers, paths, distro name) may be written into tracked files, or `git pull` will conflict.
- **Desktop icons are Windows shortcuts (`.lnk`) that start `wsl.exe` directly; all logic lives in bash scripts in the repo.** Not `.bat` files, because:
  - cmd.exe splits a dragged path containing `&` (Explorer only quotes dragged paths that contain spaces);
  - capturing `wsl.exe` output in a `.bat` (`for /f`) garbles non-ASCII text: `wsl.exe` writes its own messages as UTF-16 unless `WSL_UTF8=1` is set ([microsoft/WSL#4607](https://github.com/microsoft/WSL/issues/4607));
  - `.bat` files need CRLF line endings and are fragile with umlauts.

  With a shortcut, Explorer passes a dropped folder straight to `wsl.exe` as an argument, and `wsl.exe --exec` hands it to the bash script without any shell parsing.
- **Options via prompts with defaults** (Enter keeps the default) inside the bash script, not a config file (typos fail silently).
- **Logs:** the script prints progress to the console and writes `<chosen folder>/_logs/<timestamp>_log.txt`. The bash script turns the exit code into a plain message (0 = all folders OK; 1 = some folder failed; any other nonzero code, e.g. 2 for a bad option, is also a failure); when something fails, the coworker sends that log file.
- **ImageMagick writes JPEGs, img2pdf makes the PDFs** (commit `66521dd`). This avoids Ubuntu 22.04's ImageMagick policy, which blocks the PDF coder. Don't go back to ImageMagick's PDF output.

### Done (merged to `main`)

- `23b31d3` argparse program name showed as `Ellipsis` in usage/errors.
- `27d8604` natural page order (`scan_2` before `scan_10`, case ignored; close to, but not exactly, Windows Explorer's order).
- `66521dd` ImageMagick → JPEG → img2pdf. Verified on Ubuntu 24.04 and 22.04 against the old output: identical decoded pixels, page sizes, rotation, colors (incl. CMYK) and OCR text; final PDFs ~24% smaller (no thumbnail / opaque SMask per page). Also runs on Ubuntu 26.04 with the same page sizes.
- `f5be165` PDF pages keep the scanned object's real size (original pixels ÷ DPI, read before resizing; `-density 300` and the `-resize` cap commented out), and `IMAGEMAGICK_CMD` picks `magick` when available.
- `95bb779` (branch `feat/convert-images`, fast-forwarded): rename to `convert_images.py` (now `make_access.py`) with `pdf`/`jpg` subcommands and `pdf/`/`jpg/` output folders; `.jpf` input; `main()` split into folder-processing functions; pytest suite with generated images; metadata stripped from all outputs; `-auto-orient`; web-sized progressive JPEGs; the compact sRGB profile in `profiles/` (so `icc-profiles-free` is no longer needed); `--no-ocr` ignores `--ocr-lang`.
- **Only tested on ImageMagick 7.1.2 (Ubuntu 26.04):** everything ImageMagick-related since `66521dd`, including JPEG 2000 output (ImageMagick writing `pnm:`, gray/bilevel detection): reading size/DPI/orientation with `-ping -format`, `-auto-orient`, `+profile '!icc,*'`, `+set comment`, `-interlace JPEG`, and page sizes. Run the test suite on 22.04/24.04 (ImageMagick 6) before the handover (step 0).

### Remaining steps

0. **Test on ImageMagick 6:** run `python -m pytest` on Ubuntu 22.04 and 24.04, either in containers (see Testing) or with a GitHub Actions workflow (matrix `ubuntu-22.04`/`ubuntu-24.04`; `apt install imagemagick ocrmypdf pngquant tesseract-ocr-eng icc-profiles-free`, `pip install -r requirements-dev.txt`, `pytest`), which would also run the tests on every push.
1. **`windows/run.sh`** (bash, LF line endings, executable; the shortcut runs it):
   - Folder: use `$1` if given (dropped on the icon); otherwise open a Windows folder picker by calling `powershell.exe` from WSL (`System.Windows.Forms.FolderBrowserDialog`; set `[Console]::OutputEncoding` to UTF-8 so umlauts survive, strip the trailing `\r`, and give the dialog a topmost owner window so it doesn't open behind the console). Cancel → exit cleanly.
   - Convert with `wslpath -a`, then check the folder exists (`[ -d ]`). If not, explain in plain words: UNC paths (`\\server\share`) don't convert, and mapped drive letters (`S:\`) convert to `/mnt/s`, which doesn't exist in WSL; ask them to copy the scans to a local folder like `C:\Scans`.
   - Prompts (`read -r -p`): what to make, any combination of PDF / JPEGs / JPEG 2000 (default to agree on with Chris; the JPEGs or JPEG 2000 files are the access copies for Rosetta) → `-f` with `pdf`, `jpg` and/or `jp2`. Only when a PDF is made: max MB `[100]`, OCR languages `[deu+eng]`, recognize text `(Y/n)` → `--max-mb`, `--ocr-lang`, `--no-ocr`. Validate the MB value is a whole number and re-ask otherwise.
   - Run `"$HOME/make-access/env/bin/python" "$HOME/make-access/make_access.py" "$folder" -f <formats> <options>` (formats comma-separated) with every variable quoted.
   - Exit code 0 → "All done." (naming what was made: PDFs and JPEGs in the `access` folders); anything else → "Some folders failed. Please send Chris the newest file in the _logs folder inside <folder>." Then `read -r -p "Press Enter to close this window"`, so the window never closes before they've read the result.
2. **`windows/update.sh`:** `git -C "$HOME/make-access" pull --ff-only`, then `env/bin/pip install -r requirements.txt`; print a clear success or failure message and wait for Enter.
3. **`setup.sh`** at the repo root, run once inside their WSL; must be safe to re-run:
   - `sudo apt update`, then `sudo apt install -y imagemagick ocrmypdf pngquant tesseract-ocr-deu tesseract-ocr-eng python3-venv git` (only the needed language packs; `tesseract-ocr-all` is huge). `sudo` asks for the coworker's WSL (Linux) password, so run setup together with them.
   - create `env/` and `pip install -r requirements.txt`
   - self-test: generate a few small images at runtime with `magick`, or `convert` on ImageMagick 6 (don't commit binaries), run `-f pdf,jpg` on them, and check both outputs: a PDF with a text layer (e.g. pikepdf: every page has a `/Font` resource) and one JPEG per image with the sRGB profile. End with a clear "Setup OK" or a plain error. (The integration tests do the same more thoroughly, but need `requirements-dev.txt`.)
   - create the two desktop shortcuts by calling `powershell.exe` (`WScript.Shell` → `CreateShortcut`). Find the desktop with `[Environment]::GetFolderPath('Desktop')` (it may be redirected to OneDrive), and pass Windows paths to PowerShell via `wslpath -w`.
     - "Make Access Copies": target `C:\Windows\System32\wsl.exe`, arguments `-d <distro> --cd ~ --exec /home/<user>/make-access/windows/run.sh`
     - "Update - Make Access Copies": the same with `windows/update.sh`
     - Take `<distro>` from `$WSL_DISTRO_NAME` and the path from `$HOME`. `--exec` doesn't expand `~` in the command itself, so it must be an absolute path.
4. **Handover on their machine** (none of the Windows side can be tested from the Mac):
   - First confirm `wsl -l -v` shows VERSION 2 and an LTS Ubuntu.
   - Run `setup.sh`, then check that both desktop icons appear.
   - Test with real data:
     - double-click the icon and use the picker
     - drag a folder onto the icon
     - their largest TIFF
     - a `.jp2` or `.jpf` if they have any
     - a folder name with an umlaut, a space, `&` and parentheses
     - a folder big enough to split
     - `--no-ocr`
     - JPEG output (alone and with PDF), and one JPEG deposited in Rosetta to check its colors in the viewer
     - one deliberate failure (e.g. a corrupt `.tif`), to confirm they see the failure message
     - a mapped drive or OneDrive path, to confirm the friendly message
   - Check that their filenames sort as expected.

Instructions for the coworker: work in a plain local folder like `C:\Scans` (not OneDrive, whose cloud-only files may not be readable from WSL, and not a mapped network drive); keep the PC awake and the window open during long runs (closing it stops the run); close output PDFs before re-running (an open PDF can't be replaced; the folder then fails at once with a message naming the file); don't run two conversions on the same folder at once; `_logs` is normal, and `_work` left after a failure can be deleted once it has been looked at.

### Gotchas found

- `--optimize 2/3` needs `pngquant`. Without it every folder fails at OCR (OCRmyPDF exit code 3). `apt install --no-install-recommends ocrmypdf` doesn't install it.
- ImageMagick limits (the same on 24.04 `/etc/ImageMagick-6/policy.xml` and 26.04 `/etc/ImageMagick-7/policy.xml`): memory 1GiB, map 2GiB, area 256MP, disk 2GiB, width/height 32KP. A very large TIFF can fail with "cache resources exhausted"; raise the limits if that happens. `CONVERT_JOBS` runs one ImageMagick process per core, and WSL 2 gets 50% of host RAM by default.
- Ubuntu 26.04 ships ImageMagick 7, so the script uses `magick` there (its deprecated `convert` also still works); 22.04 and 24.04 ship ImageMagick 6 and use `convert`.
- Disk space: `_work` in each image folder holds the single-page PDFs, the merged PDF and the OCR output until the folder succeeds, and OCRmyPDF's temp files go to `/tmp` inside WSL's virtual disk, which grows on C: and doesn't shrink by itself. Large batches need free space on C:.
- WSL file access under `/mnt/c` is slower than Linux-native files; acceptable since OCR dominates.
- Corporate networks: `apt`, `git clone` and `pip` need internet access; a proxy or TLS inspection can break them. Check this first during setup.
- WSL interop must be enabled (the default) for `powershell.exe` and `wslpath` to work from inside WSL.

### Testing

- Linux parts can be tested on the Mac with Podman (`podman machine start`): build Ubuntu 22.04/24.04/26.04 images with the packages above plus `qpdf`. Generate synthetic scans with ImageMagick (`plasma:` backgrounds with annotated text) covering: untagged RGB over 3500 px, 16-bit Adobe RGB, 8-bit gray, untagged CMYK, a JPEG with EXIF orientation 6, JPEG 2000, 1-bit 600 dpi, a TIFF orientation tag, and a 400 dpi tag.
- Tested versions: 22.04 (ImageMagick 6.9.11, OCRmyPDF 13.4, Python 3.10), 24.04 (ImageMagick 6.9.12, OCRmyPDF 15.2, Python 3.12), 26.04 (ImageMagick 7.1.2, OCRmyPDF 16.13, Tesseract 5.5, Python 3.14; pikepdf 10.0.3 wheel available). All on arm64 containers; the coworker's PC is x86_64. PyPI has pikepdf 10.0.3 x86_64 Linux wheels for Python 3.10, 3.12 and 3.14, and the Ubuntu packages are the same on both architectures, but `setup.sh`'s self-test is the real check.
- Wrap any Ghostscript `txtwrite` text-extraction check in a timeout: Ghostscript 9.55 (Ubuntu 22.04) hangs on OCR'd PDFs. The pipeline itself doesn't use `txtwrite`.
- `run.sh` and `update.sh` can be partly tested in the containers (prompts, path checks, exit-code messages) by passing a Linux path as `$1`; the picker, shortcuts, `wslpath` and drag-and-drop need the real Windows machine.
- The script runs natively on the Mac with Homebrew's ImageMagick 7 (picked up as `magick` automatically); the sRGB profile is in the repo, so no settings need changing.
- Run `python -m pytest` in each container; the integration tests generate the synthetic scans described above (a few cases, like 1-bit images, aren't covered yet).

### Still to verify on the real machine

These are from documentation or reasoning, not tested:
- `wsl.exe --cd ~ --exec <absolute path> <dropped folder>` passes the dropped path through unchanged, including umlauts, spaces and `&`.
- The PowerShell folder picker appears in front and returns non-ASCII paths intact.
- The shortcut's console window shows the prompts and stays open until Enter.

### Open questions

- Their Windows version, WSL version (1 or 2), Ubuntu version and distro name.
- Where their scans live (local, OneDrive, network share).
- Their largest image size and typical batch size, and free space on C:.
- Their filename scheme (zero-padded or not).
- Whether their network allows GitHub and PyPI.
