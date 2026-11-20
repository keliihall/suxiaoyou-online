"""Image transport bytes are not text tokens; no real model/network is used."""

import base64
import copy
from io import BytesIO
import json
import random
import struct
import zlib

from PIL import Image
import pytest

from app.cloud.providers import input_reservation
from tests.test_cloud.test_inference_gateway import BASE, gateway, payload, records


def data_url(raw, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def messages(url, **image_fields):
    return [{"role": "user", "content": [
        {"type": "text", "text": "Describe this image."},
        {"type": "image_url", "image_url": {"url": url, **image_fields}},
    ]}]


def text_bytes(value, *, system=None, tools=None, output=1024):
    raw = json.dumps({"messages": value, "system": system, "tools": tools},
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return len(raw.encode()) + 256 + output


@pytest.fixture(scope="module")
def large_png():
    # A deterministic, incompressible screenshot-sized raster: its Base64
    # exceeds 1M bytes while its visual dimensions require only one tile.
    picture = Image.frombytes("RGB", (512, 512), random.Random(2026).randbytes(512 * 512 * 3))
    output = BytesIO()
    picture.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def tiny_png():
    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    return output.getvalue()


def test_base64_image_uses_visual_estimate_without_changing_request(large_png):
    value = messages(data_url(large_png))
    original = copy.deepcopy(value)

    estimated = input_reservation(value, max_output_tokens=8192)

    assert value == original
    assert text_bytes(value, output=8192) > 1_000_000
    assert 8192 + 3072 < estimated < 16384


def test_text_and_image_lookalikes_keep_original_utf8_reservation(large_png):
    url = data_url(large_png)
    value = [{"role": "user", "content": url},
             {"role": "tool", "tool_call_id": "call", "content": [
                 {"type": "text", "text": url},
                 {"type": "text", "image_url": {"url": url}},
             ]}]
    assert input_reservation(value, system=url, tools=[{"description": url}]) == text_bytes(
        value, system=url, tools=[{"description": url}],
    )


def test_extra_image_fields_cannot_hide_text_budget(tiny_png):
    value = messages(data_url(tiny_png))
    plain = input_reservation(value)
    value[0]["content"][1]["explanation"] = "文" * 10000
    value[0]["content"][1]["image_url"]["extra"] = "x" * 10000
    assert input_reservation(value) >= plain + 40000


def test_multiple_images_and_detail_hints_do_not_reduce_visual_budget(tiny_png):
    url = data_url(tiny_png)
    high = messages(url, detail="high")
    low = messages(url, detail="low")
    assert input_reservation(high) == input_reservation(low) + 1
    value = [{"role": "user", "content": [high[0]["content"][1], low[0]["content"][1]]}]
    sanitized = copy.deepcopy(value)
    for block in sanitized[0]["content"]:
        block["image_url"]["url"] = "[validated inline image]"
    assert input_reservation(value) == text_bytes(sanitized) + 2 * 3072


def test_remote_images_keep_original_bytes_and_never_fetch(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Reservation estimation must not fetch URLs")
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    value = messages("https://images.example.test/a.png?size=2048")
    assert input_reservation(value) == text_bytes(value)


@pytest.mark.parametrize("url", [
    "data:image/png;base64,not-valid-PRIVATE-DATA",
    data_url(b"PRIVATE_TEXT_IS_NOT_AN_IMAGE"),
    "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
    "file:///etc/passwd",
    "https://user:PRIVATE@images.example.test/a.png",
])
def test_unknown_or_malformed_images_keep_full_byte_charge(url, caplog):
    value = messages(url)
    assert input_reservation(value) == text_bytes(value)
    assert url not in caplog.text and "PRIVATE" not in caplog.text


def test_mismatched_mime_and_truncated_rasters_keep_original_charge(tiny_png):
    value = messages(data_url(tiny_png, "image/jpeg"))
    assert input_reservation(value) == text_bytes(value)
    output = BytesIO()
    Image.new("RGB", (64, 64), "green").save(output, format="JPEG")
    raw = output.getvalue()
    value = messages(data_url(raw[:-16], "image/jpeg"))
    assert input_reservation(value) == text_bytes(value)


def test_oversized_dimensions_keep_original_charge_without_decompressing(tiny_png):
    raw = bytearray(tiny_png)
    raw[16:24] = struct.pack(">II", 100000, 100000)
    raw[29:33] = struct.pack(">I", zlib.crc32(raw[12:29]))
    value = messages(data_url(raw))
    assert input_reservation(value) == text_bytes(value)


def test_animation_and_excessive_image_count_fall_back_to_original_charge(tiny_png):
    output = BytesIO()
    first, second = Image.new("RGB", (2, 2), "red"), Image.new("RGB", (2, 2), "blue")
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
    animated = messages(data_url(output.getvalue(), "image/gif"))
    assert input_reservation(animated) == text_bytes(animated)
    value = [{"role": "user", "content": [messages(data_url(tiny_png))[0]["content"][1] for _ in range(33)]}]
    sanitized = copy.deepcopy(value)
    for block in sanitized[0]["content"][:32]:
        block["image_url"]["url"] = "[validated inline image]"
    # Inspection limits never reject the 33rd image or give it a discount.
    assert input_reservation(value) == text_bytes(sanitized) + 32 * 3072


def test_nonstandard_image_blocks_keep_all_original_bytes(tiny_png):
    value = [{"role": "user", "content": [
        {"type": "image_url", "image_url": data_url(tiny_png)},
        {"type": "image_url", "image_url": {"url": data_url(tiny_png), "detail": "unknown"}},
        {"type": "image_url"},
    ]}]
    assert input_reservation(value) == text_bytes(value)


async def test_gateway_admits_large_image_and_settles_actual_receipt(gateway, large_png):
    _, database, client, vendor = gateway
    value = messages(data_url(large_png))

    response = await client.post(BASE + "/chat/completions", json=payload(messages=value))

    assert response.status_code == 200 and vendor.calls == 1
    assert vendor.messages == value
    rows, events, summary = await records(database)
    assert len(rows) == len(events) == 1 and rows[0].state == "settled"
    assert rows[0].reserved_tokens < 16384
    assert rows[0].total_tokens == summary["used_tokens"] == 130
    assert summary["reserved_tokens"] == 0
