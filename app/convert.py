# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Consecrated Tech
"""Convert a PowerPoint file into one image per slide.

Heavy work stays off the displays: a .pptx is rendered to a PDF with LibreOffice
(headless) and then split into one PNG per slide with poppler's pdftoppm. The
displays only ever receive the finished images. Requires the 'libreoffice' and
'poppler-utils' packages, which the installer adds for the controller role.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path


def pptx_to_pngs(pptx_path, out_dir) -> list:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    pdftoppm = shutil.which("pdftoppm")
    if not soffice or not pdftoppm:
        raise RuntimeError(
            "PowerPoint conversion needs libreoffice and poppler-utils installed."
        )

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(pptx_path)],
            check=True, capture_output=True, timeout=180,
        )
        pdfs = list(Path(tmp).glob("*.pdf"))
        if not pdfs:
            raise RuntimeError("Conversion produced no PDF.")
        subprocess.run(
            [pdftoppm, "-png", "-r", "150", str(pdfs[0]), str(out_dir / "slide")],
            check=True, capture_output=True, timeout=180,
        )

    return sorted(out_dir.glob("slide*.png"))
