import pytest

from bill_service.services import operations_context as ctx


@pytest.mark.parametrize(
    ("collection_name", "loader"),
    [
        ("gatepasses_collection", lambda: ctx.load_gate_passes()),
        ("deliveries_collection", lambda: ctx.load_movements()),
        ("returns_collection", lambda: ctx.load_movements()),
    ],
)
async def test_unreadable_operational_record_aborts_balance_load(
    mocked_db, monkeypatch, collection_name, loader
):
    await mocked_db[collection_name].insert_one({"client_name": "encrypted"})

    def unreadable(*_args, **_kwargs):
        raise ValueError("invalid encrypted record")

    monkeypatch.setattr(ctx, "decrypt_dict", unreadable)

    with pytest.raises(RuntimeError, match="complete operational data"):
        await loader()
