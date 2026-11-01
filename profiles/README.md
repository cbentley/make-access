# ICC profiles

`sRGB-v2-micro.icc` is the sRGB profile `make_access.py` converts every image to and embeds in its output (`SRGB_PROFILE`). At 456 bytes it's much smaller than a standard sRGB profile (~7 KB), which saves that much in every JPEG and PDF page. Converting with it differs from the standard `sRGB.icc` by at most 1 level of 255.

- Source: [Compact-ICC-Profiles](https://github.com/saucecontrol/Compact-ICC-Profiles) by Clinton Ingram
- License: CC0-1.0 (public domain)
- Copied from Ubuntu 26.04's `icc-profiles-free` package (version 2.4), `/usr/share/color/icc/sRGB-v2-micro.icc`. It's kept here because Ubuntu 22.04 and 24.04 don't ship it.
- SHA-256: `0a8a33aea66a6f154a5642ebe168ef287e73265d9f7b51c42a45e6eedbacda7a`
