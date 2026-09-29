"""Guarded multifunction scan fix for OPENSTEP 4.2 Intel PCIBus_reloc."""
import hashlib

from macho import strip_driver_debug


_STOCK_SHA256 = "de7a75831da01a27320bd40b0f4c58a3c6bd19543bd772607aecc763d3ddc71f"
_OFFSET = 0x10e5  # __text file offset 0x918 + instruction address 0x7cd
_OLD = bytes.fromhex("0f b6 45 f8")  # movzx eax, byte ptr [ebp-8] (current function)
_NEW = bytes.fromhex("31 c0 90 90")  # xor eax,eax; nop; nop (function zero)


def patch_pcibus_multifunction(binary: bytes) -> bytes:
    """Use function zero's header for the complete device scan.

    Preserve length, symbols, relocations and all other bytes. Accept only the
    known runtime binary (with or without STABS) or its complete fixed form.
    Reapplying is a no-op; unknown or partially patched inputs are rejected.
    """
    if binary[_OFFSET:_OFFSET + 4] not in (_OLD, _NEW):
        raise ValueError("unsupported PCIBus driver: multifunction instruction mismatch")
    original = bytearray(binary)
    original[_OFFSET:_OFFSET + 4] = _OLD
    canonical = strip_driver_debug(bytes(original))
    if hashlib.sha256(canonical).hexdigest() != _STOCK_SHA256:
        raise ValueError("unsupported PCIBus driver: runtime checksum mismatch")
    result = bytearray(binary)
    result[_OFFSET:_OFFSET + 4] = _NEW
    return bytes(result)
