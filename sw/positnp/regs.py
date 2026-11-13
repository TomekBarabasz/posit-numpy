"""Register maps of the posit engine.

Stage 1 (PositAluAxiLite, version 1.0): mirror of posit.kv260.PositAluRegs.
Stage 2-4 (PositVecAxi, version 3.0): the same map plus the stream and quire additions below,
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

VERSION_VEC = 0x0300
REDUCE = {"sum": OP_SUM, "dot": OP_DOT, "sumsq": OP_SUMSQ}

# Flag bits as in STATUS[10:8] >> STATUS_FLAGS_SHIFT and in ref.Flags.bits.
FLAG_NAR = 1
FLAG_SAT = 2
FLAG_INEXACT = 4

OPS = {"add": OP_ADD, "sub": OP_SUB, "mul": OP_MUL, "div": OP_DIV}
