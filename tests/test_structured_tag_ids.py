"""Structured QR/barcode tag IDs: item + hotel record + series/checksum."""

import pytest
from pydantic import ValidationError

from bill_service.models import LinenCreate, LinenTagGenerate
from bill_service.routers import linens as lin
from bill_service.services.tag_identity import (
    is_valid_structured_id,
    item_code,
    series_and_number,
    structured_linen_id,
)

from conftest import tenant_user


def test_structured_id_shape_and_checksum() -> None:
    assert item_code("BEDSHEET", "King Bedsheet") == "BS"
    assert series_and_number(1) == ("A", 1)
    assert series_and_number(9999) == ("A", 9999)
    assert series_and_number(10000) == ("B", 1)
    assert structured_linen_id("BEDSHEET", "King Bedsheet", "G", 1) == "BSG-A0001XL"
    assert is_valid_structured_id("BSG-A0001XL") is True
    # A wrong check letter, an old numeric ID and a legacy LL tag are rejected.
    assert is_valid_structured_id("BSG-A0001XY") is False
    assert is_valid_structured_id("BSG-00001") is False
    assert is_valid_structured_id("LL-7K4P92") is False


async def test_generate_tags_uses_hotel_code_and_start_number(mocked_db) -> None:
    payload = LinenTagGenerate(
        category="BEDSHEET",
        item_type="King Bedsheet",
        client_name="Goldi Sands",
        quantity=3,
        hotel_id="goldi-sands",
        hotel_code="G",
        start_number=7,
    )

    result = await lin.generate_linen_tags(payload, current_user=tenant_user())

    assert result["count"] == 3
    assert [row["linen_id"] for row in result["items"]] == [
        "BSG-A0007ZH",
        "BSG-A0008ZP",
        "BSG-A0009ZX",
    ]
    assert result["items"][0]["hotel_code"] == "G"
    assert result["items"][0]["item_code"] == "BS"


async def test_start_number_only_moves_a_counter_forward(mocked_db) -> None:
    base = dict(
        category="BEDSHEET",
        item_type="King Bedsheet",
        client_name="Goldi Sands",
        hotel_id="goldi-sands",
        hotel_code="G",
    )

    first = await lin.generate_linen_tags(
        LinenTagGenerate(**base, quantity=1, start_number=20),
        current_user=tenant_user(),
    )
    second = await lin.generate_linen_tags(
        LinenTagGenerate(**base, quantity=1, start_number=1),
        current_user=tenant_user(),
    )
    jumped = await lin.generate_linen_tags(
        LinenTagGenerate(**base, quantity=1, start_number=50),
        current_user=tenant_user(),
    )

    assert first["items"][0]["linen_id"] == "BSG-A0020XR"
    assert second["items"][0]["linen_id"] == "BSG-A0021XZ"
    assert jumped["items"][0]["linen_id"] == "BSG-A0050YM"


async def test_sequences_are_scoped_to_hotel_and_laundry(mocked_db) -> None:
    from conftest import LAUNDRY_B

    first = await lin.generate_linen_tags(
        LinenTagGenerate(
            category="BEDSHEET",
            item_type="King Bedsheet",
            client_name="Goldi Sands",
            quantity=1,
            hotel_id="goldi-sands",
            hotel_code="G",
        ),
        current_user=tenant_user(),
    )
    other_hotel = await lin.generate_linen_tags(
        LinenTagGenerate(
            category="BEDSHEET",
            item_type="King Bedsheet",
            client_name="Other Hotel",
            quantity=1,
            hotel_id="other-hotel",
            hotel_code="H",
        ),
        current_user=tenant_user(),
    )
    other_laundry = await lin.generate_linen_tags(
        LinenTagGenerate(
            category="BEDSHEET",
            item_type="King Bedsheet",
            client_name="Goldi Sands",
            quantity=1,
            hotel_id="goldi-sands",
            hotel_code="G",
        ),
        current_user=tenant_user(laundry_id=LAUNDRY_B),
    )

    assert first["items"][0]["linen_id"] == "BSG-A0001XL"
    assert other_hotel["items"][0]["linen_id"] == "BSH-A0001XO"
    # The linen collection still enforces globally unique IDs. A second
    # laundry reusing the same hotel code advances past the existing tag.
    assert other_laundry["items"][0]["linen_id"] == "BSG-A0002XT"


def test_hotel_record_is_mandatory_for_new_tags() -> None:
    """No creation path can fall back to an unscannable random LL tag."""

    with pytest.raises(ValidationError):
        LinenCreate(category="BEDSHEET", item_type="Sheet", client_name="Hotel X")
    with pytest.raises(ValidationError):
        LinenTagGenerate(category="BEDSHEET", item_type="Sheet", client_name="Hotel X", quantity=1)


async def test_invalid_hotel_code_is_rejected(mocked_db) -> None:
    with pytest.raises(Exception) as err:
        await lin.generate_linen_tags(
            LinenTagGenerate(
                category="BEDSHEET",
                item_type="King Bedsheet",
                client_name="Goldi Sands",
                quantity=1,
                hotel_id="goldi-sands",
                hotel_code="THIS-IS-TOO-LONG",
            ),
            current_user=tenant_user(),
        )
    assert getattr(err.value, "status_code", None) == 400
