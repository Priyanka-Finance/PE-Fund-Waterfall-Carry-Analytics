"""Recalculate the Excel model with LibreOffice and read its results.

``openpyxl`` writes formulas without cached values, so a freshly built workbook
shows numbers only once a spreadsheet application has calculated it.  Excel does
that on opening.  For automated checks this module asks a headless LibreOffice
to do the same, then reads the ``Checks`` sheet to confirm that the Excel
formulas agree with the Python engine.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

ERROR_VALUES = ("#VALUE!", "#DIV/0!", "#REF!", "#NAME?", "#NUM!", "#N/A", "#NULL!")


def libreoffice_binary() -> str | None:
    """Path of the LibreOffice executable, or ``None`` if it is not installed."""
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    mac = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    return str(mac) if mac.exists() else None


def recalculate(path: str | Path, out_path: str | Path | None = None, timeout: int = 180) -> Path:
    """Recalculate ``path`` with LibreOffice and save the result (in place by default)."""
    binary = libreoffice_binary()
    if binary is None:
        raise RuntimeError("LibreOffice is not installed; open the workbook in Excel to calculate it instead")
    source = Path(path).resolve()
    target = Path(out_path).resolve() if out_path else source
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / "profile"
        command = [
            binary, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
            "--convert-to", "xlsx", "--outdir", tmp, str(source),
        ]  # fmt: skip
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        produced = Path(tmp) / source.name
        if not produced.exists():
            raise RuntimeError(f"LibreOffice did not produce a workbook: {completed.stderr.strip() or completed.stdout}")
        shutil.copyfile(produced, target)
    return target


def read_checks(path: str | Path) -> dict[str, Any]:
    """Read a recalculated workbook: formula errors, the Checks table and headline returns."""
    wb = load_workbook(Path(path), data_only=True)
    errors = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value in ERROR_VALUES:
                    errors.append(f"{ws.title}!{cell.coordinate}: {cell.value}")

    checks, overall = [], None
    ws = wb["Checks"]
    for row in ws.iter_rows(values_only=True):
        if row[0] == "Overall":
            overall = row[1]
        if len(row) >= 5 and row[4] in ("OK", "DIFFERENCE", "Lever changed"):
            checks.append({"item": row[0], "excel": row[1], "python": row[2], "difference": row[3], "status": row[4]})

    returns = {row[0]: row[1] for row in wb["Returns"].iter_rows(values_only=True) if row[0] and row[1] is not None}
    return {"errors": errors, "checks": checks, "overall": overall, "returns": returns}
