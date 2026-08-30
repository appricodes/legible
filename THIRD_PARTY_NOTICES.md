# Third-party notices

Legible is licensed under the Apache License, Version 2.0 (see `LICENSE`).
It depends on the third-party packages below, none of which require
Legible's own source to be released under their terms - all are either
permissive or (for the one LGPL package) used unmodified via a normal
Python import, not statically linked or modified.

## Runtime dependencies (from `requirements.txt`)

| Package | License |
|---|---|
| Pillow | HPND |
| reportlab | BSD |
| numpy (synthesizes sound effects - legible/sounds.py) | BSD-3-Clause |
| pypdfium2 | BSD-3-Clause / Apache-2.0 (dual) |
| wxPython | wxWindows Library License (LGPL + a linking exception - explicitly permits use in non-LGPL projects) |
| docling, docling-core, docling-ibm-models | MIT |
| easyocr (EasyOCR backend) | Apache-2.0 |
| torch, torchvision (EasyOCR's dependency) | BSD-3-Clause |
| rapidocr, onnxruntime (RapidOCR backend) | Apache-2.0 / MIT |
| pytesseract (upside-down page detection only - legible/orientation.py; wraps the separately-installed `tesseract` binary, also Apache-2.0 - not bundled by Legible, each user installs it themselves per the README) | Apache-2.0 |
| pywin32 (also used for text-to-speech via SAPI - legible/tts.py) | PSF License |

### Transitive dependencies worth calling out

Pulled in automatically by `docling[easyocr]` (Docling's own layout/table
models plus EasyOCR's "ocr" extra): opencv-python(-contrib), scikit-image,
scipy, numpy, python-bidi, shapely, pyclipper, Pillow, PyYAML,
huggingface-hub, transformers, safetensors, ninja - all Apache-2.0,
BSD-3-Clause, or MIT, except:

- **python-bidi (LGPL-2.1+)**: used unmodified, as a normal Python import,
  for bidirectional-text layout (e.g. Arabic/Hebrew) inside EasyOCR. Since
  the whole project is open source and this package isn't modified, this
  doesn't create any additional obligation beyond crediting it here.

scipy/numpy's Windows wheels also bundle OpenBLAS (BSD-3-Clause) and a GCC
runtime library (GPL-3.0 **with** the GCC Runtime Library Exception, which
explicitly permits combination with non-GPL software) - neither imposes any
obligation on Legible itself.

## Optional scanner backends (not in `requirements.txt` - install separately)

- **pytwain (GPLv2)** - the optional TWAIN scanner backend. Because it's
  GPL-licensed, **do not bundle it into a redistributed build** (e.g. a
  PyInstaller exe published as a GitHub release) - that would place GPL
  obligations on the whole binary. It's fine for an individual user to
  `pip install pytwain` for their own local use (that's not redistribution
  by this project), and the source code's optional TWAIN support can stay
  as-is either way. If a distributed exe needs to be rebuilt, do it from a
  virtualenv that does not have `pytwain` installed (or pass
  `--exclude-module twain` to PyInstaller).
- **python-sane (LGPL)** - the optional Linux/macOS SANE backend. Not
  relevant to a Windows exe build; same "don't bundle, fine to depend on
  optionally" reasoning as above if it's ever built into a Linux binary.

## Build tooling

- **PyInstaller (GPLv2 with a bootloader exception)**: the exception
  explicitly states that using PyInstaller to build and freeze an
  application does not require that application to be GPL-licensed. Using
  it to produce `Legible.exe` carries no licensing obligation for Legible's
  own code or its permissively-licensed dependencies.

## Model weights

Docling/EasyOCR/RapidOCR model weights are downloaded by each user at
runtime from their original sources (not bundled or redistributed by this
project), so their licenses are between the user and those sources, not a
concern for Legible's own distribution.
