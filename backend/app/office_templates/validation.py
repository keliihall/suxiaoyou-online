"""Bounded OOXML ZIP, content-type, relationship, and placeholder validation."""

from __future__ import annotations

import math
import posixpath
import re
import stat
import zipfile
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Final
from urllib.parse import unquote, urlsplit

from lxml import etree

from app.office_templates.errors import TemplateContractError, TemplateSecurityError
from app.office_templates.models import OfficeTemplateFormat
from app.office_templates.substitution import (
    is_substitutable_part,
    placeholder_counts,
)


_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_REQUIRED_PART: Final = {
    "docx": "word/document.xml",
    "xlsx": "xl/workbook.xml",
    "pptx": "ppt/presentation.xml",
}
_REQUIRED_MAIN_CONTENT_TYPE: Final = {
    "docx": (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml"
    ),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet.main+xml"
    ),
    "pptx": (
        "application/vnd.openxmlformats-officedocument."
        "presentationml.presentation.main+xml"
    ),
}
_DANGEROUS_PATH_MARKERS = (
    "/embeddings/",
    "/activex/",
    "/externallinks/",
    "vbaproject",
    "oleobject",
    "customui/",
)
_DANGEROUS_TYPE_MARKERS = (
    "vba",
    "macroenabled",
    "activex",
    "oleobject",
    "external-link",
)
_DANGEROUS_REL_SUFFIXES = (
    "/vbaproject",
    "/oleobject",
    "/package",
    "/externallink",
    "/attachedtemplate",
)


@dataclass(frozen=True, slots=True)
class TemplateSafetyLimits:
    max_package_bytes: int = 100 * 1024 * 1024
    max_entries: int = 5_000
    max_entry_bytes: int = 100 * 1024 * 1024
    max_total_uncompressed_bytes: int = 500 * 1024 * 1024
    max_compression_ratio: float = 200.0

    def __post_init__(self) -> None:
        for name in (
            "max_package_bytes",
            "max_entries",
            "max_entry_bytes",
            "max_total_uncompressed_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise TemplateContractError(f"template safety {name} must be positive")
        if (
            isinstance(self.max_compression_ratio, bool)
            or not isinstance(self.max_compression_ratio, (int, float))
            or not math.isfinite(float(self.max_compression_ratio))
            or self.max_compression_ratio < 1
        ):
            raise TemplateContractError(
                "template safety compression ratio must be positive and finite"
            )


@dataclass(frozen=True, slots=True)
class OOXMLInspection:
    format: OfficeTemplateFormat
    entries: MappingProxyType[str, bytes]
    placeholder_occurrences: MappingProxyType[str, int]


def inspect_ooxml_package(
    content: bytes,
    format_name: OfficeTemplateFormat,
    *,
    expected_placeholders: tuple[str, ...] | None,
    limits: TemplateSafetyLimits | None = None,
    allow_chart_workbooks: bool = False,
) -> OOXMLInspection:
    """Fully inspect an in-memory OOXML package without extracting it."""

    selected_limits = limits or TemplateSafetyLimits()
    if not isinstance(allow_chart_workbooks, bool) or (allow_chart_workbooks and format_name != "pptx"):
        raise TemplateContractError("chart workbook policy is invalid")
    if not isinstance(content, bytes):
        raise TemplateContractError("OOXML template content must be bytes")
    if not 1 <= len(content) <= selected_limits.max_package_bytes:
        raise TemplateSecurityError("OOXML template package exceeds its byte budget")
    if not content.startswith(b"PK\x03\x04"):
        raise TemplateSecurityError("Office template is not an OOXML ZIP package")
    if format_name not in _REQUIRED_PART:
        raise TemplateContractError("Office template format is invalid")

    entries: dict[str, bytes] = {}
    seen_casefold: set[str] = set()
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(BytesIO(content), "r") as archive:
            if archive.comment:
                raise TemplateSecurityError("OOXML archive comments are not allowed")
            infos = archive.infolist()
            if not 1 <= len(infos) <= selected_limits.max_entries:
                raise TemplateSecurityError("OOXML entry count exceeds its budget")
            for info in infos:
                name = _validate_entry_name(info.filename)
                folded = name.casefold()
                if name in entries or folded in seen_casefold:
                    raise TemplateSecurityError("OOXML contains duplicate part names")
                seen_casefold.add(folded)
                _validate_zip_info(info, selected_limits)
                total_uncompressed += info.file_size
                if total_uncompressed > selected_limits.max_total_uncompressed_bytes:
                    raise TemplateSecurityError(
                        "OOXML total uncompressed size exceeds its budget"
                    )
                try:
                    payload = archive.read(info)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise TemplateSecurityError("OOXML part cannot be read safely") from exc
                if len(payload) != info.file_size:
                    raise TemplateSecurityError("OOXML part size changed while reading")
                entries[name] = payload
    except TemplateSecurityError:
        raise
    except (OSError, EOFError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise TemplateSecurityError("Office template ZIP is corrupt") from exc

    _validate_required_parts(entries, format_name)
    chart_workbooks = (
        _validate_chart_workbooks(entries, selected_limits)
        if allow_chart_workbooks else frozenset()
    )
    _validate_dangerous_parts(entries, chart_workbooks=chart_workbooks)
    _validate_content_types(entries, format_name)
    _validate_relationships(entries, format_name, chart_workbooks=chart_workbooks)
    if chart_workbooks:
        _validate_native_chart_graph(entries, chart_workbooks)
    occurrences = (
        Counter()
        if expected_placeholders is None
        else _validate_placeholders(
            entries,
            format_name,
            expected_placeholders,
        )
    )
    return OOXMLInspection(
        format=format_name,
        entries=MappingProxyType(entries),
        placeholder_occurrences=MappingProxyType(dict(occurrences)),
    )


def _validate_entry_name(raw_name: str) -> str:
    if (
        not raw_name
        or "\x00" in raw_name
        or "\\" in raw_name
        or "%" in raw_name
        or raw_name.startswith("/")
        or ":" in raw_name
        or any(part in {"", ".", ".."} for part in raw_name.split("/"))
    ):
        raise TemplateSecurityError("OOXML part path is invalid")
    path = PurePosixPath(raw_name)
    if (
        path.is_absolute()
        or raw_name.endswith("/")
    ):
        raise TemplateSecurityError("OOXML part path escapes the package")
    return raw_name


def _validate_zip_info(info: zipfile.ZipInfo, limits: TemplateSafetyLimits) -> None:
    if info.is_dir():
        raise TemplateSecurityError("OOXML directory entries are not allowed")
    if info.flag_bits & 0x1:
        raise TemplateSecurityError("Encrypted OOXML entries are not allowed")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise TemplateSecurityError("Unsupported OOXML compression method")
    if info.file_size > limits.max_entry_bytes:
        raise TemplateSecurityError("OOXML part exceeds its byte budget")
    if info.file_size:
        if info.compress_size <= 0:
            raise TemplateSecurityError("OOXML part has an invalid compressed size")
        if info.file_size / info.compress_size > limits.max_compression_ratio:
            raise TemplateSecurityError("OOXML part exceeds the compression ratio budget")
    if info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16):
        raise TemplateSecurityError("OOXML symbolic link entries are not allowed")


def _validate_required_parts(
    entries: dict[str, bytes],
    format_name: OfficeTemplateFormat,
) -> None:
    required = {"[Content_Types].xml", "_rels/.rels", _REQUIRED_PART[format_name]}
    missing = sorted(required - set(entries))
    if missing:
        raise TemplateSecurityError(
            "OOXML package is missing required parts: " + ", ".join(missing)
        )


def _validate_chart_workbooks(entries: dict[str, bytes], limits: TemplateSafetyLimits) -> frozenset[str]:
    """Inspect only the native chart data packages emitted by python-pptx.

    This is an explicit authoring policy, never the template import default.
    Nested packages retain the ordinary no-embedding/no-macro/no-external-link
    policy, and share entry and expansion budgets with their outer package.
    Relationships are bound to chart externalData by the outer validator.
    """
    approved = frozenset(name for name in entries if re.fullmatch(
        r"ppt/embeddings/Microsoft_Excel_(?:Sheet|WorkSheet)[1-9][0-9]*\.xlsx", name))
    remaining_entries = limits.max_entries - len(entries)
    remaining_bytes = limits.max_total_uncompressed_bytes - sum(map(len, entries.values()))
    for name in sorted(approved):
        if remaining_entries < 1 or remaining_bytes < 1:
            raise TemplateSecurityError("Chart workbook exceeds the shared package budget")
        nested = inspect_ooxml_package(entries[name], "xlsx", expected_placeholders=None,
            limits=TemplateSafetyLimits(max_package_bytes=limits.max_package_bytes,
                max_entries=remaining_entries, max_entry_bytes=limits.max_entry_bytes,
                max_total_uncompressed_bytes=remaining_bytes,
                max_compression_ratio=limits.max_compression_ratio))
        remaining_entries -= len(nested.entries)
        remaining_bytes -= sum(map(len, nested.entries.values()))
    return approved


def _validate_dangerous_parts(entries: dict[str, bytes], *, chart_workbooks: frozenset[str] = frozenset()) -> None:
    for name, payload in entries.items():
        lowered_name = "/" + name.casefold()
        safe_printer_settings = (
            re.fullmatch(
                r"ppt/printersettings/printersettings[0-9]+\.bin",
                name.casefold(),
            )
            is not None
        )
        if (lowered_name.endswith(".bin") and not safe_printer_settings) or any(
            marker in lowered_name and not (marker == "/embeddings/" and name in chart_workbooks)
            for marker in _DANGEROUS_PATH_MARKERS
        ):
            raise TemplateSecurityError(f"Unsafe OOXML part is not allowed: {name}")
        if name.casefold().endswith((".xml", ".rels")):
            # Parse every XML part with the same security policy, including
            # UTF-16/32 parts where byte-string DTD scans are insufficient.
            _parse_xml(payload, name)


def _validate_native_chart_graph(entries: dict[str, bytes], workbooks: frozenset[str]) -> None:
    chart_ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    drawing_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    presentation_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    relation_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    type_root = _parse_xml(entries["[Content_Types].xml"], "[Content_Types].xml")
    defaults = {e.get("Extension").casefold(): e.get("ContentType") for e in type_root
        if e.tag == f"{{{_CONTENT_TYPES_NS}}}Default"}
    overrides = {e.get("PartName")[1:]: e.get("ContentType") for e in type_root
        if e.tag == f"{{{_CONTENT_TYPES_NS}}}Override"}
    def content_type(name):
        return overrides.get(name, defaults.get(name.rsplit(".", 1)[-1].casefold()))
    def relationships(name):
        return _parse_xml(entries[name], name)
    for name in workbooks:
        if content_type(name) != "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            raise TemplateSecurityError("Chart workbook content type is invalid")
    charts = set()
    for name in entries:
        if re.fullmatch(r"ppt/charts/_rels/chart[1-9][0-9]*\.xml\.rels", name):
            if any(_resolve_relationship_target(name, rel.get("Target")) in workbooks
                   for rel in relationships(name)):
                charts.add(_relationship_source_name(name))
    for name in charts:
        root = _parse_xml(entries[name], name)
        area = root.find(f"{{{chart_ns}}}chart/{{{chart_ns}}}plotArea")
        if (content_type(name) != "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
                or area is None or not any(child.tag in {f"{{{chart_ns}}}{kind}" for kind in
                    ("barChart", "lineChart", "pieChart", "scatterChart")} for child in area)):
            raise TemplateSecurityError("Embedded workbook owner is not a supported native chart")
    # Require a live presentation -> slide -> graphicFrame/chart chain, not
    # an orphan chart-shaped XML part constructed merely to own a package.
    presentation = _parse_xml(entries["ppt/presentation.xml"], "ppt/presentation.xml")
    slide_ids = {element.get(f"{{{relation_ns}}}id") for element in
        presentation.findall(f"{{{presentation_ns}}}sldIdLst/{{{presentation_ns}}}sldId")}
    active_slides = set()
    presentation_rels = "ppt/_rels/presentation.xml.rels"
    if presentation_rels in entries:
        for rel in relationships(presentation_rels):
            if rel.get("Id") in slide_ids and rel.get("Type") == relation_ns + "/slide":
                active_slides.add(_resolve_relationship_target(presentation_rels, rel.get("Target")))
    owned_charts = set()
    for name in entries:
        if not re.fullmatch(r"ppt/slides/_rels/slide[1-9][0-9]*\.xml\.rels", name):
            continue
        source = _relationship_source_name(name)
        slide = _parse_xml(entries[source], source)
        frames = slide.findall(f"{{{presentation_ns}}}cSld/{{{presentation_ns}}}spTree/"
            f"{{{presentation_ns}}}graphicFrame/{{{drawing_ns}}}graphic/{{{drawing_ns}}}graphicData")
        chart_ids = {chart.get(f"{{{relation_ns}}}id") for frame in frames if frame.get("uri") == chart_ns
            for chart in frame.findall(f"{{{chart_ns}}}chart")}
        for rel in relationships(name):
            target = _resolve_relationship_target(name, rel.get("Target"))
            if target in charts:
                if (source not in active_slides or slide.tag != f"{{{presentation_ns}}}sld"
                        or rel.get("Type") != relation_ns + "/chart" or rel.get("Id") not in chart_ids):
                    raise TemplateSecurityError("Native chart is not bound to an active slide graphic frame")
                owned_charts.add(target)
    if charts != owned_charts:
        raise TemplateSecurityError("Native chart has no active slide owner")


def _validate_content_types(
    entries: dict[str, bytes],
    format_name: OfficeTemplateFormat,
) -> None:
    root = _parse_xml(entries["[Content_Types].xml"], "[Content_Types].xml")
    if root.tag != f"{{{_CONTENT_TYPES_NS}}}Types":
        raise TemplateSecurityError("OOXML content types root is invalid")
    main_part = _REQUIRED_PART[format_name]
    main_content_type: str | None = None
    default_extensions: set[str] = set()
    override_parts: set[str] = set()
    for element in root:
        content_type = element.get("ContentType")
        if not content_type:
            raise TemplateSecurityError("OOXML content type entry is incomplete")
        lowered = content_type.casefold()
        if any(marker in lowered for marker in _DANGEROUS_TYPE_MARKERS):
            raise TemplateSecurityError("Unsafe OOXML content type is not allowed")
        if element.tag == f"{{{_CONTENT_TYPES_NS}}}Override":
            if set(element.attrib) != {"PartName", "ContentType"}:
                raise TemplateSecurityError(
                    "OOXML content type override fields are invalid"
                )
            part_name = element.get("PartName")
            if not part_name or not part_name.startswith("/"):
                raise TemplateSecurityError("OOXML content type override path is invalid")
            normalized = _validate_entry_name(part_name[1:])
            if normalized in override_parts:
                raise TemplateSecurityError(
                    "OOXML contains duplicate content type overrides"
                )
            override_parts.add(normalized)
            if normalized not in entries:
                raise TemplateSecurityError(
                    "OOXML content type references a missing part"
                )
            if normalized == main_part:
                main_content_type = lowered
        elif element.tag == f"{{{_CONTENT_TYPES_NS}}}Default":
            if set(element.attrib) != {"Extension", "ContentType"}:
                raise TemplateSecurityError(
                    "OOXML default content type fields are invalid"
                )
            extension = element.get("Extension")
            if (
                not extension
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", extension)
                is None
                or extension.casefold() in default_extensions
            ):
                raise TemplateSecurityError(
                    "OOXML default content type extension is invalid or duplicate"
                )
            default_extensions.add(extension.casefold())
        else:
            raise TemplateSecurityError("Unknown OOXML content type element")
    required_content_type = _REQUIRED_MAIN_CONTENT_TYPE[format_name]
    if main_content_type != required_content_type:
        raise TemplateSecurityError("OOXML main content type does not match its format")


def _validate_relationships(
    entries: dict[str, bytes],
    format_name: OfficeTemplateFormat,
    *,
    chart_workbooks: frozenset[str] = frozenset(),
) -> None:
    names = set(entries)
    root_office_targets: list[str] = []
    referenced_chart_workbooks: set[str] = set()
    for rels_name, payload in entries.items():
        if not rels_name.casefold().endswith(".rels"):
            continue
        if not rels_name.endswith(".rels"):
            raise TemplateSecurityError("OOXML relationship part casing is invalid")
        if rels_name != "_rels/.rels":
            source_name = _relationship_source_name(rels_name)
            if source_name not in names:
                raise TemplateSecurityError(
                    "OOXML relationship part has no source part"
                )
        root = _parse_xml(payload, rels_name)
        if root.tag != f"{{{_RELATIONSHIPS_NS}}}Relationships":
            raise TemplateSecurityError("OOXML relationships root is invalid")
        relationship_ids: set[str] = set()
        for relationship in root:
            if relationship.tag != f"{{{_RELATIONSHIPS_NS}}}Relationship":
                raise TemplateSecurityError("Unknown OOXML relationship element")
            relationship_id = relationship.get("Id")
            relationship_type = relationship.get("Type")
            target = relationship.get("Target")
            if not relationship_id or relationship_id in relationship_ids:
                raise TemplateSecurityError("OOXML relationship id is missing or duplicate")
            relationship_ids.add(relationship_id)
            if not relationship_type or not target:
                raise TemplateSecurityError("OOXML relationship is incomplete")
            target_mode = relationship.get("TargetMode", "Internal")
            if target_mode.casefold() == "external":
                raise TemplateSecurityError("External OOXML relationships are not allowed")
            if target_mode.casefold() != "internal":
                raise TemplateSecurityError("Unknown OOXML relationship target mode")
            lowered_type = relationship_type.casefold()
            normalized_target = _resolve_relationship_target(rels_name, target)
            chart_package = (
                relationship_type == "http://schemas.openxmlformats.org/officeDocument/2006/relationships/package"
                and re.fullmatch(r"ppt/charts/_rels/chart[1-9][0-9]*\.xml\.rels", rels_name) is not None
                and normalized_target in chart_workbooks
            )
            if any(
                lowered_type.endswith(suffix) and not (suffix == "/package" and chart_package)
                for suffix in _DANGEROUS_REL_SUFFIXES
            ):
                raise TemplateSecurityError("Unsafe OOXML relationship type is not allowed")
            if normalized_target in chart_workbooks:
                if not chart_package:
                    raise TemplateSecurityError("Chart workbook has an unexpected owner")
                chart = _parse_xml(entries[_relationship_source_name(rels_name)], rels_name)
                chart_ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
                relation_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                data = chart.findall(f"{{{chart_ns}}}externalData")
                if (chart.tag != f"{{{chart_ns}}}chartSpace" or len(data) != 1
                        or data[0].get(f"{{{relation_ns}}}id") != relationship_id):
                    raise TemplateSecurityError("Chart workbook is not bound to native chart data")
                referenced_chart_workbooks.add(normalized_target)
            if normalized_target not in names:
                raise TemplateSecurityError("OOXML relationship target is missing")
            if rels_name == "_rels/.rels" and lowered_type.endswith(
                "/officedocument"
            ):
                root_office_targets.append(normalized_target)
    if len(root_office_targets) != 1:
        raise TemplateSecurityError(
            "OOXML root must contain exactly one officeDocument relationship"
        )
    if root_office_targets[0] != _REQUIRED_PART[format_name]:
        raise TemplateSecurityError(
            "OOXML root officeDocument relationship does not match its format"
        )
    if referenced_chart_workbooks != set(chart_workbooks):
        raise TemplateSecurityError("Chart workbook is not owned by a chart relationship")


def _resolve_relationship_target(rels_name: str, target: str) -> str:
    if "\\" in target or "\x00" in target:
        raise TemplateSecurityError("OOXML relationship target path is invalid")
    decoded = target
    for _ in range(8):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    if "%" in decoded:
        raise TemplateSecurityError("OOXML relationship target encoding is invalid")
    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise TemplateSecurityError("OOXML relationship target URI is invalid")
    if rels_name == "_rels/.rels":
        base = ""
    else:
        rels_path = PurePosixPath(rels_name)
        if rels_path.parent.name != "_rels" or not rels_path.name.endswith(".rels"):
            raise TemplateSecurityError("OOXML relationship part path is invalid")
        source_name = rels_path.name[: -len(".rels")]
        base = str(rels_path.parent.parent / source_name)
        base = posixpath.dirname(base)
    if parsed.path.startswith("/"):
        normalized = posixpath.normpath(parsed.path.lstrip("/"))
    else:
        normalized = posixpath.normpath(posixpath.join(base, parsed.path))
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise TemplateSecurityError("OOXML relationship target escapes the package")
    return _validate_entry_name(normalized)


def _relationship_source_name(rels_name: str) -> str:
    rels_path = PurePosixPath(rels_name)
    if rels_path.parent.name != "_rels" or not rels_path.name.endswith(".rels"):
        raise TemplateSecurityError("OOXML relationship part path is invalid")
    source_name = rels_path.name[: -len(".rels")]
    if not source_name:
        raise TemplateSecurityError("OOXML relationship source is invalid")
    return _validate_entry_name(str(rels_path.parent.parent / source_name))


def _validate_placeholders(
    entries: dict[str, bytes],
    format_name: OfficeTemplateFormat,
    expected_placeholders: tuple[str, ...],
) -> Counter[str]:
    occurrences: Counter[str] = Counter()
    for name, payload in entries.items():
        if is_substitutable_part(name, format_name):
            occurrences.update(placeholder_counts(name, payload, format_name))
        elif name.casefold().endswith((".xml", ".rels")) and (
            b"{{" in payload or b"}}" in payload
        ):
            raise TemplateContractError(
                f"Placeholders are not allowed in unsupported OOXML part: {name}"
            )
    found = set(occurrences)
    expected = set(expected_placeholders)
    unknown = sorted(found - expected)
    missing = sorted(expected - found)
    if unknown:
        raise TemplateContractError(
            "template contains unknown placeholders: " + ", ".join(unknown)
        )
    if missing:
        raise TemplateContractError(
            "template is missing required placeholders: " + ", ".join(missing)
        )
    return occurrences


def _parse_xml(payload: bytes, part_name: str) -> etree._Element:
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise TemplateSecurityError(
            f"DTD or entity declarations are forbidden: {part_name}"
        )
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        remove_blank_text=False,
        huge_tree=False,
    )
    try:
        tree = etree.parse(BytesIO(payload), parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise TemplateSecurityError(f"Invalid OOXML XML part: {part_name}") from exc
    root = tree.getroot()
    if tree.docinfo.doctype or root is None or any(isinstance(node, etree._Entity) for node in root.iter()):
        raise TemplateSecurityError(f"DTD or entity declarations are forbidden: {part_name}")
    return root
