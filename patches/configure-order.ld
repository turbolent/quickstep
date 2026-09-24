/* Unused, executable header padding: file 0xc50, virtual address 0x2c50. */
SECTIONS {
  . = 0x2c50;
  .capture : { *(.text.capture) }
  ASSERT(. <= 0x2d50, "capture exceeds reserved slot")
  . = 0x2d50;
  .finish : { *(.text.finish) }
  ASSERT(. <= 0x2e50, "finish exceeds reserved slot")
  . = 0x2e50;
  .text : { *(.text*) *(.rodata*) }
  ASSERT(. <= 0x3d5c, "payload exceeds Configure header padding")
  /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.llvm_addrsig) }
}
