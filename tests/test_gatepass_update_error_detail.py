"""Regression: an unexpected error while editing a gate pass must be logged
server-side while the client receives only a generic 500 detail.

The previous handler echoed ``str(e)`` into the HTTP 500 body (leaking internal
details) and printed a raw traceback to stderr.
"""
import logging

import pytest
from fastapi import HTTPException

from bill_service.crypto_helper import encrypt_dict
from bill_service.models import GatePassUpdate
from bill_service.routers import gatepasses

from conftest import auth_user, now

SENSITIVE = ["client_name", "items", "notes"]
RAW_ERROR_TEXT = "internal-credential-must-not-leak"


async def _seed_gate_pass(mocked_db) -> str:
    doc = {
        "gate_pass_number": "GP-UPDATE-500",
        "client_name": "Test Client",
        "status": "RECEIVED",
        "receiving_date": now(),
        "received_by": "System",
        "items": [
            {
                "item_name": "Pillow",
                "category": "Bed",
                "specification": None,
                "client_qty": 10,
                "received_qty": 10,
                "difference": 0,
            }
        ],
        "notes": "",
        "created_at": now(),
        "updated_at": now(),
    }
    result = await mocked_db["gatepasses_collection"].insert_one(
        encrypt_dict(doc, SENSITIVE)
    )
    return str(result.inserted_id)


async def test_update_gate_pass_unexpected_error_returns_generic_500(
    mocked_db, monkeypatch, caplog
):
    gate_pass_id = await _seed_gate_pass(mocked_db)

    async def _repository_failure(*_args, **_kwargs):
        raise RuntimeError(RAW_ERROR_TEXT)

    # bump_version is a repository call made by the happy path of the edit.
    monkeypatch.setattr(gatepasses, "bump_version", _repository_failure)

    with caplog.at_level(logging.ERROR, logger=gatepasses.logger.name):
        with pytest.raises(HTTPException) as caught:
            await gatepasses.update_gate_pass(
                gate_pass_id,
                GatePassUpdate(notes="please update"),
                current_user=auth_user(),
            )

    error = caught.value
    assert error.status_code == 500
    # main.py serialises HTTPException.detail verbatim as the JSON body.
    body = {"detail": error.detail}
    assert body["detail"] == gatepasses.UPDATE_GATE_PASS_ERROR_DETAIL
    assert RAW_ERROR_TEXT not in str(body)
    assert "RuntimeError" not in str(body)

    # The failure is still visible in server logs, via the module logger.
    assert any(
        record.name == gatepasses.logger.name and record.levelno == logging.ERROR
        for record in caplog.records
    )
