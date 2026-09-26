"""Guarded order-preservation patch for OPENSTEP 4.2 Intel Configure.app."""
import hashlib
from pathlib import Path
import struct


_PROFILES = (
    (0, "77e807152d49daadc0e9a52c176fa8ab09c7f4206fda4291bc2bf124c39a945f"),
    (0x6e000, "76bde18f0cda564183e88d1e78678b5c3715d873a7cca7728df2a92d6804bdde"),
)
_PAYLOAD_HASH = "5ae290ba24b466c7fd3b23dd8ea4c36a252914a383e56c6e204df32b48e6f7a8"
_PAYLOAD_OFFSET = 0xc50
_HOOKS = ((0x4810, 0x2c50), (0x4830, 0x2c50), (0x4885, 0x2d50))
_MSG_SEND = 0x05003477


def _call(site: int, target: int) -> bytes:
    return b"\xe8" + struct.pack("<i", target - site - 5)


def patch_configure_order(binary: bytes) -> bytes:
    """Retain explicit target drivers and preserve order during installation setup.

    Accept only the known stock thin/fat executables or the complete patch.
    Preserve executable length, Mach-O layout, and non-Intel architectures.
    Never repair an unknown binary or an incomplete/modified patch.
    """
    payload = Path(__file__).with_name("patches").joinpath("configure-order.bin").read_bytes()
    if hashlib.sha256(payload).hexdigest() != _PAYLOAD_HASH:
        raise ValueError("Configure order patch payload checksum mismatch")
    changes = [(_PAYLOAD_OFFSET, bytes(len(payload)), payload)]
    changes += [(site - 0x2000, _call(site, _MSG_SEND), _call(site, target))
                for site, target in _HOOKS]
    for base, digest in _PROFILES:
        actual = tuple(binary[base + off:base + off + len(old)] for off, old, new in changes)
        if actual not in (tuple(old for off, old, new in changes),
                          tuple(new for off, old, new in changes)):
            continue
        original = bytearray(binary)
        for offset, old, new in changes:
            original[base + offset:base + offset + len(old)] = old
        if hashlib.sha256(original).hexdigest() != digest:
            continue
        result = bytearray(binary)
        for offset, old, new in changes:
            result[base + offset:base + offset + len(old)] = new
        return bytes(result)
    raise ValueError("unsupported Configure executable (expected stock OPENSTEP 4.2 or complete order fix)")
