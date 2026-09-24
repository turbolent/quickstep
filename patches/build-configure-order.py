"""Regenerate configure-order.bin using clang, ld.lld and llvm-objcopy.

The media builder uses the checked-in payload; these tools are needed only
when changing the native patch. No Apple/NeXT SDK or shared library is used.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    root = Path(__file__).resolve().parent
    tools = {name: shutil.which(name) for name in ("clang", "ld.lld", "llvm-objcopy")}
    if not all(tools.values()):
        raise RuntimeError("clang, ld.lld and llvm-objcopy must be on PATH")
    with tempfile.TemporaryDirectory(prefix="configure-order-") as temp:
        obj, elf = Path(temp) / "patch.o", Path(temp) / "patch.elf"
        subprocess.run([
            tools["clang"], "--target=i386-unknown-none-elf", "-Oz", "-ffreestanding",
            "-fno-pic", "-fno-stack-protector", "-fno-asynchronous-unwind-tables",
            "-fno-unwind-tables", "-mno-sse", "-mstack-alignment=4",
            "-c", str(root / "configure-order.c"), "-o", str(obj),
        ], check=True)
        subprocess.run([tools["ld.lld"], "-m", "elf_i386", "-T", str(root / "configure-order.ld"),
                        "--entry=capture", str(obj), "-o", str(elf)], check=True)
        subprocess.run([tools["llvm-objcopy"], "-O", "binary", str(elf),
                        str(root / "configure-order.bin")], check=True)


if __name__ == "__main__":
    main()
