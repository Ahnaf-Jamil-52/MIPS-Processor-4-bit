#!/usr/bin/env python3
"""
CSE 210 Jan 2026 - 4-bit MIPS assembler  (store-first stack variant)
Lab Group B1, Group ID 3   (opcode sequence OJNCAEPLGKMFDBHI)

Stack convention: $sp is a fixed base register (stays at 15). push/pop are
each a SINGLE instruction -- the assembler tracks stack depth at compile time
and encodes the slot as an offset from $sp. Valid for straight-line, balanced
push/pop only; a push inside a loop would reuse one slot. Up to 9 items fit
(4-bit signed offset reaches -8).

Usage:
    python3 assembler.py program.asm
    python3 assembler.py program.asm -o instruction_memory.txt

Produces:
    instruction_memory.txt   Logisim "v2.0 raw" image -> load into Instruction_Memory ROM
    program.lst              human-readable listing (address, machine code, source)
"""

import argparse
import re
import sys

# --------------------------------------------------------------------------
# Machine description -- change these two tables if the hardware changes
# --------------------------------------------------------------------------

# Opcodes: position in the sequence OJNCAEPLGKMFDBHI is the opcode value.
OPCODES = {
    "bneq": 0x0,  "srl": 0x1,  "beq": 0x2,  "sub":  0x3,
    "add":  0x4,  "and": 0x5,  "j":   0x6,  "lw":   0x7,
    "or":   0x8,  "nor": 0x9,  "sw":  0xA,  "andi": 0xB,
    "subi": 0xC,  "addi": 0xD, "ori": 0xE,  "sll":  0xF,
}

# Register numbers = the multiplexer input each register feeds in Register_File.
REGISTERS = {
    "$zero": 0, "$0": 0,
    "$t0":   1,
    "$t1":   2,
    "$t2":   3,
    "$t3":   4,
    "$t4":   5,
    "$sp":   6,
}

# Instruction formats
R_TYPE = {"add", "sub", "and", "or", "nor"}          # op | src1 | src2 | dst
S_TYPE = {"sll", "srl"}                              # op | src1 | dst  | shamt
I_ARITH = {"addi", "subi", "andi", "ori"}            # op | src1 | dst  | imm
I_MEM = {"lw", "sw"}                                 # op | base | reg  | offset
I_BRANCH = {"beq", "bneq"}                           # op | reg1 | reg2 | pc-rel offset
J_TYPE = {"j"}                                       # op | 8-bit target | 0000

# Pseudo-instructions, expanded before addresses are assigned.
# Single-instruction push/pop.  $sp is a FIXED base that stays at 15 for the
# whole program; it never moves at runtime.  The assembler keeps a compile-time
# depth counter and encodes each stack slot as an offset from $sp, so a push is
# one `sw` and a pop is one `lw` -- half the instructions of the two-step form.
#
#   item i lives at address 15 - i  ==  offset -i from $sp (which holds 15)
#
# This is valid only for STRAIGHT-LINE, statically balanced push/pop usage:
# because the offset is fixed at assemble time, a push inside a loop would write
# the same slot every iteration.  See the notes printed with the listing.
_STACK = {"depth": 0, "max_depth": 0}


def _reset_stack():
    _STACK["depth"] = 0
    _STACK["max_depth"] = 0


def _push(rx):
    offset = -_STACK["depth"]
    if offset < FIELD_MIN:                       # -8 is the deepest reachable slot
        raise ValueError(
            f"stack too deep for a 4-bit offset: pushing item "
            f"{_STACK['depth'] + 1} needs offset {offset} (min {FIELD_MIN}). "
            f"At most {abs(FIELD_MIN) + 1} items fit with single-instruction push.")
    _STACK["depth"] += 1
    _STACK["max_depth"] = max(_STACK["max_depth"], _STACK["depth"])
    return [f"sw {rx}, {offset}($sp)"]


def _pop(rx):
    if _STACK["depth"] == 0:
        raise ValueError("pop from an empty stack (more pops than pushes)")
    _STACK["depth"] -= 1
    offset = -_STACK["depth"]
    return [f"lw {rx}, {offset}($sp)"]


PSEUDO = {
    "push": _push,
    "pop":  _pop,
    "nop":  lambda: ["add $zero, $zero, $zero"],
}

IMEM_SIZE = 256          # instruction memory is 8-bit addressed
FIELD_MIN, FIELD_MAX = -8, 15   # a 4-bit field, signed or unsigned

# Prologue emitted at address 00 of every program: $sp = 1111 (top of memory).
# The register file has no preset, so the stack pointer is set in software.
SP_INIT = "addi $sp, $zero, 15"


class AsmError(Exception):
    def __init__(self, line_no, text, message):
        super().__init__(message)
        self.line_no = line_no
        self.text = text
        self.message = message

    def report(self):
        return f"line {self.line_no}: {self.message}\n    {self.text.strip()}"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def strip_comment(line):
    for marker in ("#", ";", "//"):
        idx = line.find(marker)
        if idx != -1:
            line = line[:idx]
    return line.strip()


def parse_register(token, ctx):
    name = token.strip().lower()
    if name not in REGISTERS:
        known = ", ".join(sorted(set(REGISTERS) - {"$0"}))
        raise AsmError(*ctx, f"unknown register {token.strip()!r} (known: {known})")
    return REGISTERS[name]


def parse_number(token, ctx, what="immediate"):
    text = token.strip()
    try:
        value = int(text, 0)          # accepts 5, -3, 0b0101, 0xA
    except ValueError:
        raise AsmError(*ctx, f"{what} {text!r} is not a number")
    return value


def to_field(value, ctx, what="value"):
    """Pack a signed or unsigned value into 4 bits."""
    if not (FIELD_MIN <= value <= FIELD_MAX):
        raise AsmError(*ctx,
                       f"{what} {value} does not fit in 4 bits "
                       f"(allowed {FIELD_MIN}..{FIELD_MAX})")
    return value & 0xF


def split_operands(rest):
    return [p for p in (x.strip() for x in rest.split(",")) if p]


# --------------------------------------------------------------------------
# Pass 0 -- read the file, expand pseudo-instructions
# --------------------------------------------------------------------------

def expand(source_lines, init_sp=True):
    """Returns a list of (line_no, original_text, mnemonic, rest)."""
    _reset_stack()
    out = []
    if init_sp:
        mnemonic, _, rest = SP_INIT.partition(" ")
        out.append((0, f"{SP_INIT}   [auto: stack pointer init]",
                    mnemonic.lower(), rest.strip()))
    for line_no, raw in enumerate(source_lines, start=1):
        text = strip_comment(raw)
        while text:
            label_match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", text)
            if label_match:
                out.append((line_no, raw, "__label__", label_match.group(1)))
                text = label_match.group(2).strip()
                continue
            break
        if not text:
            continue

        parts = text.split(None, 1)
        mnemonic = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if mnemonic in PSEUDO:
            args = split_operands(rest)
            try:
                expansion = PSEUDO[mnemonic](*args)
            except TypeError:
                raise AsmError(line_no, raw,
                               f"{mnemonic} takes a different number of operands")
            except ValueError as exc:
                raise AsmError(line_no, raw, str(exc))
            for expanded in expansion:
                m, _, r = expanded.partition(" ")
                out.append((line_no, raw + f"   [expanded: {expanded}]",
                            m.lower(), r.strip()))
        else:
            out.append((line_no, raw, mnemonic, rest))
    return out


# --------------------------------------------------------------------------
# Pass 1 -- assign addresses to labels
# --------------------------------------------------------------------------

def collect_labels(items):
    labels = {}
    address = 0
    for line_no, raw, mnemonic, rest in items:
        if mnemonic == "__label__":
            if rest in labels:
                raise AsmError(line_no, raw, f"label {rest!r} defined twice")
            labels[rest] = address
        else:
            address += 1
    if address > IMEM_SIZE:
        raise AsmError(0, "", f"program is {address} instructions, "
                              f"instruction memory holds {IMEM_SIZE}")
    return labels, address


# --------------------------------------------------------------------------
# Pass 2 -- encode
# --------------------------------------------------------------------------

def resolve_target(token, labels, ctx):
    token = token.strip()
    if token in labels:
        return labels[token]
    return parse_number(token, ctx, "target address")


def encode(mnemonic, rest, address, labels, ctx):
    if mnemonic not in OPCODES:
        raise AsmError(*ctx, f"unknown instruction {mnemonic!r}")
    op = OPCODES[mnemonic]
    ops = split_operands(rest)

    def need(n):
        if len(ops) != n:
            raise AsmError(*ctx,
                           f"{mnemonic} needs {n} operands, got {len(ops)}")

    # ---- R-type:  add $dst, $src1, $src2  ->  op | src1 | src2 | dst
    if mnemonic in R_TYPE:
        need(3)
        dst = parse_register(ops[0], ctx)
        s1 = parse_register(ops[1], ctx)
        s2 = parse_register(ops[2], ctx)
        return (op << 12) | (s1 << 8) | (s2 << 4) | dst

    # ---- S-type:  sll $dst, $src, shamt  ->  op | src | dst | shamt
    if mnemonic in S_TYPE:
        need(3)
        dst = parse_register(ops[0], ctx)
        src = parse_register(ops[1], ctx)
        shamt = parse_number(ops[2], ctx, "shift amount")
        if not 0 <= shamt <= 15:
            raise AsmError(*ctx, f"shift amount {shamt} must be 0..15")
        if shamt > 3:
            warn(ctx, f"shifting a 4-bit value by {shamt} always gives 0")
        return (op << 12) | (src << 8) | (dst << 4) | shamt

    # ---- I-type arithmetic:  addi $dst, $src, imm  ->  op | src | dst | imm
    if mnemonic in I_ARITH:
        need(3)
        dst = parse_register(ops[0], ctx)
        src = parse_register(ops[1], ctx)
        imm = to_field(parse_number(ops[2], ctx), ctx, "immediate")
        return (op << 12) | (src << 8) | (dst << 4) | imm

    # ---- Memory:  lw $reg, offset($base)  ->  op | base | reg | offset
    if mnemonic in I_MEM:
        need(2)
        reg = parse_register(ops[0], ctx)
        match = re.match(r"^(-?\w+)?\s*\(\s*(\$\w+)\s*\)$", ops[1])
        if not match:
            raise AsmError(*ctx,
                           f"expected  offset($base)  but got {ops[1]!r}")
        offset_token = match.group(1) or "0"
        base = parse_register(match.group(2), ctx)
        offset = to_field(parse_number(offset_token, ctx, "offset"),
                          ctx, "offset")
        return (op << 12) | (base << 8) | (reg << 4) | offset

    # ---- Branch:  beq $a, $b, label  ->  op | a | b | (target - pc)
    #      PC module adds the offset to PC itself (not PC+1), so the
    #      offset is measured from the branch instruction's own address.
    if mnemonic in I_BRANCH:
        need(3)
        ra = parse_register(ops[0], ctx)
        rb = parse_register(ops[1], ctx)
        target = resolve_target(ops[2], labels, ctx)
        offset = target - address
        if not (FIELD_MIN <= offset <= 7):
            raise AsmError(*ctx,
                           f"branch to {ops[2].strip()!r} is {offset} away; "
                           f"a 4-bit signed offset reaches -8..+7")
        return (op << 12) | (ra << 8) | (rb << 4) | (offset & 0xF)

    # ---- Jump:  j label  ->  op | 8-bit target | 0000
    if mnemonic in J_TYPE:
        need(1)
        target = resolve_target(ops[0], labels, ctx)
        if not 0 <= target <= 0xFF:
            raise AsmError(*ctx, f"jump target {target} must be 0..255")
        return (op << 12) | (target << 4)

    raise AsmError(*ctx, f"{mnemonic!r} has no encoding rule")


WARNINGS = []


def warn(ctx, message):
    WARNINGS.append(f"line {ctx[0]}: warning: {message}")


def assemble(source_lines, init_sp=True):
    items = expand(source_lines, init_sp=init_sp)
    labels, count = collect_labels(items)

    words, listing = [], []
    address = 0
    for line_no, raw, mnemonic, rest in items:
        if mnemonic == "__label__":
            listing.append(("", "", f"{rest}:"))
            continue
        ctx = (line_no, raw)
        word = encode(mnemonic, rest, address, labels, ctx)
        words.append(word)
        note = "   ; auto: $sp init" if line_no == 0 else ""
        listing.append((f"{address:02X}", f"{word:016b}",
                        f"{mnemonic} {rest}".strip() + note))
        address += 1
    return words, listing, labels, count


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_rom_image(path, words):
    with open(path, "w") as f:
        f.write("v2.0 raw\n")
        for i in range(0, len(words), 8):
            f.write(" ".join(f"{w:04x}" for w in words[i:i + 8]) + "\n")


def write_listing(path, listing, labels):
    with open(path, "w") as f:
        f.write("addr  machine code       source\n")
        f.write("----  -----------------  ------\n")
        for addr, binary, src in listing:
            if addr == "":
                f.write(f"                         {src}\n")
            else:
                hexcode = f"{int(binary, 2):04X}"
                f.write(f"{addr}    {binary}  {hexcode}  {src}\n")
        if labels:
            f.write("\nlabels\n")
            for name, addr in sorted(labels.items(), key=lambda kv: kv[1]):
                f.write(f"  {name:<12} {addr:02X}\n")


def main():
    ap = argparse.ArgumentParser(description="4-bit MIPS assembler (B1, group 3)")
    ap.add_argument("source", help="assembly source file")
    ap.add_argument("-o", "--output", default="instruction_memory.txt",
                    help="Logisim ROM image to write")
    ap.add_argument("-l", "--listing", default=None,
                    help="listing file (default: <source>.lst)")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="do not print the listing to the terminal")
    ap.add_argument("--no-init-sp", action="store_true",
                    help="do not emit the automatic '%s' at address 00"
                         % SP_INIT)
    args = ap.parse_args()

    try:
        with open(args.source) as f:
            source_lines = f.readlines()
    except OSError as exc:
        sys.exit(f"cannot read {args.source}: {exc}")

    try:
        words, listing, labels, count = assemble(
            source_lines, init_sp=not args.no_init_sp)
    except AsmError as exc:
        sys.exit("error: " + exc.report())

    listing_path = args.listing or (args.source.rsplit(".", 1)[0] + ".lst")
    write_rom_image(args.output, words)
    write_listing(listing_path, listing, labels)

    if not args.quiet:
        for addr, binary, src in listing:
            if addr == "":
                print(f"                         {src}")
            else:
                print(f"{addr}    {binary}  {int(binary, 2):04X}  {src}")
    for w in WARNINGS:
        print(w, file=sys.stderr)
    print(f"\n{count} instructions -> {args.output}  (listing: {listing_path})")


if __name__ == "__main__":
    main()