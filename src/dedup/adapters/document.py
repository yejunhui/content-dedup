"""PDF / Office / OpenDocument adapter: extract text then SimHash."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from ..fingerprints import SIMHASH_VERSION
from ..models import Fingerprint
from .base import (
    ContentAdapter,
    FingerprintError,
    MatchInfo,
    UnsupportedFingerprint,
    hamming_match,
)
from .textutil import (
    TextBudget,
    build_simhash_fingerprint,
    html_to_text,
    simhash_sig,
)

DOCUMENT_VERSION = 1

_PPTX_SLIDE_RE = re.compile(r"ppt/slides/slide\d+\.xml$")
_ODF_TEXT_RE = re.compile(r"^content\.xml$")
_EPUB_TEXT_RE = re.compile(r".*\.(?:xhtml|html|htm)$", re.IGNORECASE)


class DocumentAdapter(ContentAdapter):
    name = "document"
    extensions = frozenset(
        {
            ".pdf",
            ".docx",
            ".docm",
            ".dotx",
            ".dotm",
            ".doc",
            ".xlsx",
            ".xlsm",
            ".xltx",
            ".xltm",
            ".xls",
            ".pptx",
            ".pptm",
            ".potx",
            ".potm",
            ".ppsx",
            ".ppsm",
            ".ppt",
            ".odt",
            ".ods",
            ".odp",
            ".epub",
        }
    )
    fingerprint_kinds = ("simhash",)

    def fingerprint_sig(self) -> str:
        return simhash_sig(
            {
                "version": DOCUMENT_VERSION,
                "min_tokens": self.config.document.min_tokens,
                "max_chars": self.config.document.max_chars,
                "max_pages": self.config.document.max_pages,
                "tokenizer": 1,
                "simhash_version": SIMHASH_VERSION,
            }
        )

    def compute_fingerprint(self, path: Path) -> dict[str, Fingerprint]:
        text, extractor = self._extract_text(path)
        fingerprint = build_simhash_fingerprint(
            text,
            min_tokens=self.config.document.min_tokens,
            extra_meta={"extractor": extractor},
        )
        return {fingerprint.kind: fingerprint}

    def _extract_text(self, path: Path) -> tuple[str, str]:
        suffix = path.suffix.lower()
        budget = TextBudget(self.config.document.max_chars)
        if suffix == ".pdf":
            self._extract_pdf(path, budget)
            extractor = "pypdf"
        elif suffix in {".docx", ".docm", ".dotx", ".dotm"}:
            self._extract_docx(path, budget)
            extractor = "python-docx"
        elif suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
            self._extract_xlsx(path, budget)
            extractor = "openpyxl"
        elif suffix in {".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm"}:
            self._extract_zip_text(path, budget, _PPTX_SLIDE_RE)
            extractor = "pptx-xml"
        elif suffix in {".odt", ".ods", ".odp"}:
            self._extract_zip_text(path, budget, _ODF_TEXT_RE)
            extractor = "odf-xml"
        elif suffix == ".epub":
            self._extract_zip_text(path, budget, _EPUB_TEXT_RE, strip_html=True)
            extractor = "epub-xml"
        elif suffix in {".doc", ".xls", ".ppt"}:
            raise UnsupportedFingerprint(
                f"legacy binary Office format ({suffix}) is not text-extractable; exact hash only"
            )
        else:
            raise UnsupportedFingerprint(f"no document extractor for {suffix}")

        text = budget.text()
        if not text.strip():
            raise FingerprintError(f"no extractable text in document ({extractor})")
        if budget.truncated:
            raise UnsupportedFingerprint(
                f"document text exceeds max_chars ({self.config.document.max_chars}); "
                "exact hash only"
            )
        return text, extractor

    def _extract_pdf(self, path: Path, budget: TextBudget) -> None:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise FingerprintError("pypdf is required for PDF documents") from exc
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception as exc:
                    raise FingerprintError("encrypted PDF cannot be read") from exc
            if len(reader.pages) > self.config.document.max_pages:
                raise UnsupportedFingerprint(
                    f"PDF has more than max_pages ({self.config.document.max_pages}); "
                    "exact hash only"
                )
            pages = reader.pages[: self.config.document.max_pages]
            for page in pages:
                try:
                    text = page.extract_text() or ""
                except Exception:  # noqa: BLE001 - a damaged page should not abort the document
                    text = ""
                if not budget.add(text + "\n"):
                    break
        except FingerprintError:
            raise
        except Exception as exc:
            raise FingerprintError(f"cannot read PDF: {exc}") from exc

    def _extract_docx(self, path: Path, budget: TextBudget) -> None:
        try:
            from docx import Document
        except ImportError as exc:
            raise FingerprintError("python-docx is required for DOCX documents") from exc
        try:
            document = Document(str(path))
            for paragraph in document.paragraphs:
                if not budget.add(paragraph.text + "\n"):
                    return
            for table in document.tables:
                for row in table.rows:
                    cells = " | ".join(cell.text for cell in row.cells)
                    if not budget.add(cells + "\n"):
                        return
            for section in document.sections:
                for paragraph in section.header.paragraphs:
                    if not budget.add(paragraph.text + "\n"):
                        return
                for paragraph in section.footer.paragraphs:
                    if not budget.add(paragraph.text + "\n"):
                        return
        except Exception as exc:
            raise FingerprintError(f"cannot read DOCX: {exc}") from exc

    def _extract_xlsx(self, path: Path, budget: TextBudget) -> None:
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise FingerprintError("openpyxl is required for XLSX documents") from exc
        workbook = None
        try:
            workbook = load_workbook(str(path), read_only=True, data_only=True)
            for sheet in workbook.worksheets:
                if not budget.add(f"\n# {sheet.title}\n"):
                    return
                for row in sheet.iter_rows(values_only=True):
                    values = [str(cell) for cell in row if cell is not None]
                    if values and not budget.add(" ".join(values) + "\n"):
                        return
        except Exception as exc:
            raise FingerprintError(f"cannot read XLSX: {exc}") from exc
        finally:
            if workbook is not None:
                try:
                    workbook.close()
                except Exception:  # noqa: BLE001, S110 - best-effort close
                    pass

    def _extract_zip_text(
        self,
        path: Path,
        budget: TextBudget,
        pattern: re.Pattern[str],
        *,
        strip_html: bool = False,
    ) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                names = sorted(name for name in archive.namelist() if pattern.match(name))
                for name in names:
                    if budget.truncated:
                        return
                    try:
                        data = archive.read(name)
                    except Exception:  # noqa: BLE001, S112 - skip unreadable member
                        continue
                    if strip_html or name.lower().endswith((".html", ".htm", ".xhtml")):
                        text = html_to_text(data.decode("utf-8", errors="replace"))
                    else:
                        try:
                            root = ElementTree.fromstring(data)
                            text = " ".join(root.itertext())
                        except ElementTree.ParseError:
                            continue
                    if not budget.add(text + "\n"):
                        return
        except zipfile.BadZipFile as exc:
            raise FingerprintError(f"invalid document container: {exc}") from exc
        except Exception as exc:
            raise FingerprintError(f"cannot read document container: {exc}") from exc

    def match(self, kind: str, left: Fingerprint, right: Fingerprint) -> MatchInfo | None:
        if kind != "simhash":
            return None
        return hamming_match(left, right, self.config.document.simhash_hamming)
