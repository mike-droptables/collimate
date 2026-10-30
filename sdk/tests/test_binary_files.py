# SPDX-License-Identifier: MIT
"""Binary input and output file maps round-trip byte-exactly."""
from __future__ import annotations

from collimate.testing import run_generator_callable

# A payload that is NOT valid UTF-8 (lone continuation + high bytes) plus the
# full byte range — anything that mishandles encoding corrupts or crashes on it.
_BLOB = b"\x89PNG\r\n\x1a\n\x00\x80\x81\xfe\xff" + bytes(range(256))


def test_update_file_roundtrips_bytes():
    async def run():
        import gen
        gen.update_file("data.bin", _BLOB)                    # output-cell write
        await gen.commit(name="Main")

    res = run_generator_callable(run)
    assert res.ok, res.error
    assert res.files.get("data.bin") == _BLOB                 # bytes survive to output


def test_inputs_roundtrip_bytes():
    async def run():
        import gen
        data = gen.read_file("seed.bin")
        assert data == _BLOB, "binary input corrupted in transit"
        gen.update_file("echo.bin", data)
        await gen.commit(name="Echo")

    res = run_generator_callable(run, inputs={"seed.bin": _BLOB})
    assert res.ok, res.error
    assert res.files.get("echo.bin") == _BLOB
