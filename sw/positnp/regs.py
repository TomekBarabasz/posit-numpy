"""Register maps of the posit engine.

Stage 1 (PositAluAxiLite, version 1.0): mirror of posit.kv260.PositAluRegs.
Stage 2-5 (PositVecAxi, version 4.0): the same map plus the stream, quire and stage 5 additions below,
mirror of posit.kv260.PositVecRegs. PositAluAxiLiteTestplan / PositVecAxiTestplan
fail when Scala and this file differ, so change both together.
"""

ID_VERSION = 0x00   # RO  [31:16] ID = 0x5053 ("PS"), [15:8] major, [7:0] minor
CONFIG = 0x04       # RO  [7:0] n, [11:8] es, [31:16] mask of built operations (1 << code)
CTRL = 0x08         # WO  bit 0 start, bit 1 soft reset, bit 2 clear counters (pulses)
STATUS = 0x0C       # RO  bit 0 busy, bit 1 done, bit 2 error, [10:8] flags of the last result
OP = 0x10           # RW  [3:0] operation code
OPERAND_A = 0x14    # RW  [n-1:0]
OPERAND_B = 0x18    # RW  [n-1:0]
RESULT = 0x1C       # RO  [n-1:0] last result
CNT_NAR = 0x40      # RO  NaR results since the last clear
CNT_SAT = 0x44      # RO  results clamped to maxpos / minpos
CNT_INEXACT = 0x48  # RO  inexact results
CYCLES = 0x4C       # RO  clock cycles spent busy

CTRL_START = 0
CTRL_SOFT_RESET = 1
CTRL_CLEAR_COUNTERS = 2

STATUS_BUSY = 0
STATUS_DONE = 1
STATUS_ERROR = 2
STATUS_FLAGS_SHIFT = 8  # nar = 8, sat = 9, inexact = 10

ID = 0x5053
VERSION = 0x0100

OP_ADD = 0
OP_SUB = 1
OP_MUL = 2
OP_DIV = 3

# ---- stage 2: arrays through AXI4-Stream (PositVecAxi, version 2.0) ----------------------
STREAM_CFG = 0x24     # RW  bit 0 scalar_b: array (+) scalar, b = OPERAND_B
CNT_ELEMENTS = 0x50   # RO  stream results since the last clear

STREAM_CFG_SCALAR_B = 0
STATUS_STREAM_ACTIVE = 3  # element(s) inside the ALU
CONFIG_HAS_STREAM = 12    # CONFIG bit: the engine has the stream ports

# ---- stage 4: quire (PositVecAxi, version 3.0) ----------------------------------------------
QUIRE_CFG = 0x54      # RO  [15:0] quire width in bits, [31:16] quireFrac (LSB = 2**-quireFrac)
QUIRE = 0x80          # RO  QUIRE + 4 k: word k of the raw quire (two's complement), least significant first
OP_SUM = 4            # sum a
OP_DOT = 5            # sum a * b
OP_SUMSQ = 6          # sum a * a
CTRL_QUIRE_CLEAR = 3
CTRL_QUIRE_ROUND = 4
STATUS_QUIRE_BUSY = 4
STATUS_QUIRE_NAR = 5
CONFIG_HAS_QUIRE = 13

# ---- stage 5: sqrt, FMA / AXPY, float32 conversions (PositVecAxi, version 4.0) ----------------
OPERAND_C = 0x58      # RW  [n-1:0] third operand of FMA: a * b + c
OP_SQRT = 7           # sqrt(a)
OP_FMA = 8            # a * b + OPERAND_C
OP_AXPY = 9           # a * OPERAND_B + b (stream): y = alpha * x + y in one pass
OP_F2P = 10           # a = float32 bits -> posit32
OP_P2F = 11           # posit32 -> float32 bits

# ---- 4.1: engine parameters (build options, VecKv260 n:es:k:u) ---------------------------------
ENGINE_CFG = 0x5C     # RO  [7:0] k: divider / sqrt bits per cycle, [15:8] div / sqrt / FMA lanes (slowUnits),
                      #     [23:16] element lanes (elements per clock, stage 6)

# ---- stage 6: own AXI4 master (PositVecAxi 5.0): arrays read and written by the engine ---------
MEM_LEN = 0x60        # RW  elements of the command
MEM_SRC_A = 0x64      # RW  physical address of a (aligned to the 16-byte data word)
MEM_SRC_B = 0x68      # RW  address of b
MEM_SRC_C = 0x6C      # RW  address of c (FMA3)
MEM_DST = 0x70        # RW  address of the result (may equal MEM_SRC_A: in place)
MEM_LO = 0x74         # RW  allowed addresses [MEM_LO, MEM_HI); empty after reset
MEM_HI = 0x78         # RW
MEM_CFG = 0x7C        # RW  bit 0 read_b, 1 read_c, 2 write, 3 irq_en
MEM_STATUS = 0xC0     # RO  bit 0 busy, 1 done, 2 err_range, 3 err_align, 4 err_resp
MEM_CYCLES = 0xC4     # RO  clock cycles of the last command
MEM_CFG_READ_B = 0
MEM_CFG_READ_C = 1
MEM_CFG_WRITE = 2
MEM_CFG_IRQ_EN = 3
MEM_ST_BUSY = 0
MEM_ST_DONE = 1
MEM_ST_ERR_RANGE = 2
MEM_ST_ERR_ALIGN = 3
MEM_ST_ERR_RESP = 4
CTRL_MEM_START = 5
CTRL_MEM_ACK = 6
CONFIG_HAS_MASTER = 14
OP_FMA3 = 12          # a * b + c, all three arrays from memory
MEM_BEAT_BYTES = 16

VERSION_VEC = 0x0500
VERSION_STREAM_MIN = 0x0200   # oldest PositVecAxi with the stream path that PositVecHw drives
REDUCE = {"sum": OP_SUM, "dot": OP_DOT, "sumsq": OP_SUMSQ}
STAGE5 = {"sqrt": OP_SQRT, "fma": OP_FMA, "axpy": OP_AXPY, "f2p": OP_F2P, "p2f": OP_P2F}
STAGE6 = {"fma3": OP_FMA3}

# Flag bits as in STATUS[10:8] >> STATUS_FLAGS_SHIFT and in ref.Flags.bits.
FLAG_NAR = 1
FLAG_SAT = 2
FLAG_INEXACT = 4

OPS = {"add": OP_ADD, "sub": OP_SUB, "mul": OP_MUL, "div": OP_DIV}
