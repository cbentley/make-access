# Convert Images

Converts folders of scanned images into searchable, size-limited PDFs, JPEGs and/or JPEG 2000 files. Built for large image collections.

## Setup

Requires ImageMagick and, for PDFs, OCRmyPDF (with Ghostscript, Tesseract language packs and `pngquant`, which `--optimize 2` and `3` need):

```bash
# Ubuntu
sudo apt update && sudo apt install -y imagemagick ocrmypdf pngquant tesseract-ocr-all

# macOS
brew install imagemagick ghostscript ocrmypdf tesseract-lang
```

Then, in the repo directory:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

To work on the script, install `requirements-dev.txt` instead: it includes `requirements.txt` and adds pytest (see Tests).

## Usage

```bash
python make_access.py <folder> -f <format>[,<format>...] [options]
```

`<folder>` is the folder with the scans, and `-f` (or `--format`) says what to make: one or more formats, in any order, separated by commas (no spaces, or put the list in quotes):

| Format | Makes                                                  |
| ------ | ------------------------------------------------------ |
| `pdf`  | one OCR'd, size-limited PDF per image folder           |
| `jpg`  | one JPEG per image (`jpeg` is accepted too)            |
| `jp2`  | one JPEG 2000 file (`.jp2`) per image                  |

Options for PDFs (an error without `-f pdf`):

| Option              | Description                                                    | Default   |
| ------------------- | -------------------------------------------------------------- | --------- |
| `--max-mb=<size>`   | Maximum size of each final PDF in MB; larger output is split   | `100`     |
| `--ocr-lang=<lang>` | OCR languages, passed to OCRmyPDF `-l` (e.g. `eng`, `deu+eng`) | `DEFAULT_OCR_LANG` |
| `--no-ocr`          | Skip text recognition (`--ocr-lang` is ignored); OCRmyPDF still applies `OCR_OPTS` | off |

The folder and the options can be given in any order. Examples:

```bash
python make_access.py /path/to/images -f pdf --ocr-lang=eng --max-mb=150
python make_access.py /path/to/images -f pdf,jpg,jp2
python make_access.py . -f jpg
python make_access.py -f jp2 /path/to/images
```

## Fixing older PDFs for Rosetta

PDFs made before linearization was turned off load slowly in Rosetta's viewer when they are large. `delinearize.py` resaves them without linearization, in place and losslessly (page images and text are unchanged); folders are searched recursively, and PDFs that aren't linearized are left untouched:

```bash
python delinearize.py /path/to/access-folders [more files or folders ...]
```

Close the PDFs first if they are open in Acrobat or PDFgear.

## What it does

The script searches `<folder>` and all its subfolders. Every folder that directly contains images (`.tif`, `.tiff`, `.jpg`, `.jpeg`, `.jp2`, `.jpf`) is converted on its own; images are never combined across folders. Pages are sorted by filename in natural order, like Windows Explorer: numbers by value (`page2` before `page10`), ignoring case.

Skipped:
- folders whose names start with `_`, and folders named `access` (the script's own output folders)
- images whose filenames contain an excluded word (currently `ruler`, case-insensitive)

All outputs go into an `access` folder inside each image folder. When a folder succeeds, the files of the kinds just made are replaced: PDFs replace every PDF, JPEGs every JPEG (`.jpg`/`.jpeg`) and JPEG 2000 files every `.jp2`/`.jpf` directly in its `access` folder before moving in the new ones (the log says how many of each kind), so files from earlier runs, including ones added by hand, don't linger. Other files and subfolders are left alone. If one of those files can't be replaced, usually because it's open in a program (Acrobat locks open PDFs on Windows drives), the folder fails without changing its `access` folder. This is checked before any folder is converted and again just before replacing. A folder named `master` (case-insensitive) is the exception: its `access` folder goes next to it, and its PDF takes the parent's name (`b01-f01/master/` → `b01-f01/access/b01-f01.pdf`). If the parent folder holds images too, both would write to the same `access` folder, so both folders fail without writing anything.
- `<foldername>.pdf`, or `<foldername>-part01.pdf`, `-part02.pdf`, … when it exceeds `--max-mb`.
- `<imagename>.jpg` and/or `<imagename>.jp2`, one per image. If two images would get the same name (e.g. `a.tif` and `a.jpg`), the folder fails rather than one overwriting the other.

Images with an embedded color profile are converted to sRGB, untagged RGB images are tagged as sRGB, and untagged gray and CMYK images are left as is. All images keep the scanned object's real size: PDF pages are sized from each original's pixels and DPI, and JPEGs and JPEG 2000 files that are scaled down get a correspondingly lower DPI. Image size and quality are set separately for PDF pages, JPEGs and JPEG 2000 files (see Configuration). For files holding several images (multi-page TIFFs, Photoshop layers), only the first is used, with a warning in the log. Images with an orientation tag (e.g. a camera's "rotate 90°") are rotated or flipped so they're upright in the output. Metadata from the originals (EXIF, XMP, Photoshop data, comments) is not carried over, only the DPI and, for sRGB images, a compact sRGB profile. JPEGs are progressive, so web viewers can show a rough version while they load.

JPEG 2000 files are prepared by ImageMagick like the JPEGs and encoded by Pillow (with OpenJPEG, which Pillow's wheels include): lossy files use the irreversible 9/7 wavelet, lossless ones the reversible 5/3, and the data is ordered by resolution first and indexed (PLT markers), so viewers and image servers can read a smaller version or a region without decoding the whole file. They are 8-bit, gray if every pixel is gray (as with JPEGs), and store their color space as the JP2 code for sRGB or gray rather than as an ICC profile. JP2 has no CMYK, so untagged CMYK images are converted to RGB without a profile (colors are approximate), and alpha channels are dropped, as in JPEGs. Pillow's JPEG 2000 writer stores no DPI, so the script adds it to each file afterwards (as capture and display resolution). ImageMagick doesn't read that back (it reports 72 DPI for every JPEG 2000 file), but readers that use JP2's resolution box, such as Pillow, do. Most web browsers can't show JPEG 2000 files directly.

A log of each run is written to `<folder>/_logs/<timestamp>_log.txt`.

### Pipeline

Each image folder is processed in its own `_work` directory:

| Step             | Work directory          | What happens                                                                                                                                                                             |
| ---------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Convert       | `_work/01_single_pages`, `_work/jpg`, `_work/jp2` | ImageMagick turns each image into a JPEG and/or JPEG 2000 file. For PDFs, img2pdf wraps the JPEG into a one-page PDF without re-encoding.               |
| 2. Merge         | `_work/02_pre_ocr`      | PDF only: all pages are merged into one PDF.                                                                                                                                             |
| 3. OCR           | `_work/03_ocr`          | PDF only: OCRmyPDF adds a searchable text layer. Depending on `OCR_OPTS`, it may also re-compress the page images (see Configuration). PDFs are never linearized ("fast web view"): that makes Rosetta's viewer much slower.                                                  |
| 4. Split         | `_work/04_split`        | PDF only: if the PDF exceeds `--max-mb`, it is split into parts, each holding as many pages as fit.                                                                                      |
| 5. Finish        |                         | The final PDFs, JPEGs and JPEG 2000 files are moved into the `access` folder and `_work` is deleted.                                                                     |

### Errors

If any step fails for a folder, including OCR (there is no non-OCR fallback), that folder gets no new output and the script moves on to the next folder. Its `access` folder is left unchanged. Its `_work` directory is kept for debugging, except when the failure is a problem with the folder's contents that the log explains (such as a JPEG name clash). Failed folders are listed at the end.

Exit status: `0` if all folders succeeded, `1` if any failed, `2` for invalid options or a folder that doesn't exist.

## Tests

```bash
python -m pytest                       # all tests
python -m pytest -m "not integration"  # skip the tests that run ImageMagick and OCRmyPDF
```

Needs `requirements-dev.txt` (see Setup). The integration tests generate their own small images and are skipped if ImageMagick, OCRmyPDF or Tesseract's English data is missing; the Adobe RGB test also needs `icc-profiles-free` (Ubuntu) for its test profile.

## Configuration

Settings that aren't command-line options are constants at the top of `make_access.py`:

| Constant                  | Controls                                                              |
| ------------------------- | --------------------------------------------------------------------- |
| `ALLOWED_EXT`             | Image file extensions to process (lowercase, with the dot; files match regardless of case) |
| `EXCLUDED_WORDS`          | Images whose filename contains any of these are skipped. Matched anywhere in the name and ignoring case, so `ruler` also skips `Rulers_01.tif`. Lowercase. |
| `GENERIC_FOLDER_NAMES`    | Folder names that take their PDF name from the parent folder and get their `access` folder next to them. Lowercase. |
| `ACCESS_DIR_NAME`         | Name of the output folder. Folders with this name are never searched for images. |
| `IMAGEMAGICK_CMD`, `OCR_CMD` | ImageMagick and OCRmyPDF commands. `IMAGEMAGICK_CMD` is picked automatically: `magick` on ImageMagick 7, `convert` on 6. |
| `IMAGEMAGICK_INPUT_OPTS`  | ImageMagick options placed before each input file. Keep `-limit thread 1`: `CONVERT_JOBS` already runs one ImageMagick per core. Uncommenting `-density` treats every image as that DPI instead of the one it stores, which changes the PDF page size and the DPI of the JPEGs and JPEG 2000 files. |
| `FALLBACK_DPI`            | DPI used for the PDF page size of images that don't store one (their JPEGs and JPEG 2000 files stay without a DPI) |
| `SRGB_PROFILE`            | sRGB ICC profile: images with an embedded profile are converted to it, untagged RGB images are tagged with it, untagged gray and CMYK images are left as is. Default: `profiles/sRGB-v2-micro.icc`, a compact [CC0](https://github.com/saucecontrol/Compact-ICC-Profiles) profile in this repo. |
| `CONVERT_JOBS`            | Images converted in parallel (default: the number of CPU cores; lower it if large TIFFs run out of memory) |
| `PDF_MAX_SIZE`, `JPG_MAX_SIZE`, `JP2_MAX_SIZE` | Longest side in pixels for PDF page images, JPEGs and JPEG 2000 files. Larger images are scaled down, smaller ones are left as is; `None` keeps full size. PDF pages keep the original's real size either way. |
| `PDF_QUALITY`, `JPG_QUALITY` | JPEG quality (1-100) for PDF page images and JPEGs. Below 90, ImageMagick stores color at half the resolution in each direction (4:2:0). |
| `JP2_QUALITY`             | JPEG 2000 quality as a target PSNR in dB: how close each image stays to the original (higher is closer and larger), or `None` for lossless. A different scale from JPEG quality; each image gets the file size it needs to reach the target. |
| `JP2_OPTS`                | Further [Pillow JPEG 2000 options](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#jpeg-2000-saving), e.g. `progression`, `plt`, `tile_size` or `num_resolutions` |
| `DEFAULT_OCR_LANG`        | OCR languages used when `--ocr-lang` isn't given                      |
| `OCR_OPTS`                | OCRmyPDF options, also applied with `--no-ocr` (languages come from `--ocr-lang`) |

These OCRmyPDF options re-encode every page image after `PDF_QUALITY`'s encoding, adding a second lossy compression: `--optimize 2` or `3` and `--deskew`. `--rotate-pages` is lossless.
