"""Native PPTX charts retain package safety through authoring precommit."""
from io import BytesIO
import zipfile

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches
import pytest

from app.office_templates.errors import TemplateSecurityError
from app.office_templates.validation import TemplateSafetyLimits, inspect_ooxml_package
from app.office_validation.structure import inspect_ooxml_path


def chart_package():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "Real native chart"
    data = CategoryChartData()
    data.categories = ["Alpha", "Beta"]
    data.add_series("Amount", [10, 20])
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED,
        Inches(4), Inches(1), Inches(4), Inches(3), data)
    slide.notes_slide.notes_text_frame.text = "Chart evidence and speaker notes"
    stream = BytesIO()
    presentation.save(stream)
    with zipfile.ZipFile(stream) as package:
        return {name: package.read(name) for name in package.namelist()}


def packed(entries):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in entries.items():
            package.writestr(name, content)
    return stream.getvalue()


def inspect(entries, **kwargs):
    return inspect_ooxml_package(packed(entries), "pptx", expected_placeholders=None,
        allow_chart_workbooks=True, **kwargs)


def test_actual_chart_and_notes_pass_authoring_but_template_default_stays_closed(tmp_path):
    entries = chart_package()
    path = tmp_path / "chart.pptx"
    path.write_bytes(packed(entries))
    assert "ppt/embeddings/Microsoft_Excel_Sheet1.xlsx" in inspect_ooxml_path(path, "pptx").parts
    with pytest.raises(TemplateSecurityError):
        inspect_ooxml_package(path.read_bytes(), "pptx", expected_placeholders=None)


@pytest.mark.parametrize("attack", ["macro", "external", "nested_embedding"])
def test_nested_workbook_is_fully_revalidated(attack):
    entries = chart_package()
    name = "ppt/embeddings/Microsoft_Excel_Sheet1.xlsx"
    with zipfile.ZipFile(BytesIO(entries[name])) as package:
        inner = {key: package.read(key) for key in package.namelist()}
    if attack == "macro":
        inner["xl/vbaProject.bin"] = b"not allowed"
    elif attack == "nested_embedding":
        inner["xl/embeddings/package.xlsx"] = entries[name]
    else:
        root = etree.fromstring(inner["_rels/.rels"])
        etree.SubElement(root, "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship",
            Id="external", Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            Target="https://example.invalid/private", TargetMode="External")
        inner["_rels/.rels"] = etree.tostring(root)
    entries[name] = packed(inner)
    with pytest.raises(TemplateSecurityError):
        inspect(entries)


@pytest.mark.parametrize("attack", ["orphan", "ole", "wrong_binding", "unknown_name"])
def test_embedded_package_requires_exact_native_chart_ownership(attack):
    entries = chart_package()
    name = "ppt/embeddings/Microsoft_Excel_Sheet1.xlsx"
    rel = "ppt/charts/_rels/chart1.xml.rels"
    if attack == "orphan":
        entries["ppt/embeddings/Microsoft_Excel_Sheet2.xlsx"] = entries[name]
    elif attack == "ole":
        entries[rel] = entries[rel].replace(b"relationships/package", b"relationships/oleObject")
    elif attack == "wrong_binding":
        entries["ppt/charts/chart1.xml"] = entries["ppt/charts/chart1.xml"].replace(b'r:id="rId1"', b'r:id="not-the-owner"')
    else:
        entries["ppt/embeddings/arbitrary.xlsx"] = entries.pop(name)
        entries[rel] = entries[rel].replace(b"Microsoft_Excel_Sheet1.xlsx", b"arbitrary.xlsx")
    with pytest.raises(TemplateSecurityError):
        inspect(entries)


@pytest.mark.parametrize("budget", ["entries", "bytes"])
def test_nested_and_outer_packages_share_expansion_budget(budget):
    entries = chart_package()
    limits = (TemplateSafetyLimits(max_entries=len(entries)) if budget == "entries" else
        TemplateSafetyLimits(max_total_uncompressed_bytes=sum(map(len, entries.values()))))
    with pytest.raises(TemplateSecurityError):
        inspect(entries, limits=limits)


@pytest.mark.parametrize("encoding", ["UTF-16", "UTF-32"])
def test_nested_non_utf8_xml_cannot_hide_dtd_or_entities(encoding):
    entries = chart_package()
    name = "ppt/embeddings/Microsoft_Excel_Sheet1.xlsx"
    with zipfile.ZipFile(BytesIO(entries[name])) as package:
        inner = {key: package.read(key) for key in package.namelist()}
    xml = ('<?xml version="1.0" encoding="' + encoding + '"?>'
        '<!DOCTYPE worksheet [<!ENTITY hidden "forbidden">]>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData/><!-- &hidden; --></worksheet>')
    inner["xl/worksheets/sheet1.xml"] = xml.encode(encoding)
    entries[name] = packed(inner)
    with pytest.raises(TemplateSecurityError):
        inspect(entries)


def test_benign_utf16_xml_remains_valid():
    entries = chart_package()
    name = "ppt/embeddings/Microsoft_Excel_Sheet1.xlsx"
    with zipfile.ZipFile(BytesIO(entries[name])) as package:
        inner = {key: package.read(key) for key in package.namelist()}
    element = etree.fromstring(inner["xl/worksheets/sheet1.xml"])
    inner["xl/worksheets/sheet1.xml"] = etree.tostring(element, encoding="UTF-16", xml_declaration=True)
    entries[name] = packed(inner)
    assert inspect(entries).format == "pptx"


@pytest.mark.parametrize("attack", ["empty_chart", "chart_type", "workbook_type", "slide_type", "graphic_uri", "orphan_slide"])
def test_chart_workbook_owner_requires_live_typed_native_graph(attack):
    entries = chart_package()
    if attack == "empty_chart":
        entries["ppt/charts/chart1.xml"] = (
            b'<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
            b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            b'<c:externalData r:id="rId1"/></c:chartSpace>')
    elif attack == "chart_type":
        entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
            b"application/vnd.openxmlformats-officedocument.drawingml.chart+xml", b"image/png")
    elif attack == "workbook_type":
        entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
            b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"image/png")
    elif attack == "slide_type":
        name = "ppt/slides/_rels/slide1.xml.rels"
        entries[name] = entries[name].replace(b"relationships/chart", b"relationships/image")
    elif attack == "graphic_uri":
        name = "ppt/slides/slide1.xml"
        entries[name] = entries[name].replace(
            b'uri="http://schemas.openxmlformats.org/drawingml/2006/chart"', b'uri="unrelated"')
    else:
        name = "ppt/_rels/presentation.xml.rels"
        entries[name] = entries[name].replace(b"relationships/slide\"", b"relationships/image\"")
    with pytest.raises(TemplateSecurityError):
        inspect(entries)
