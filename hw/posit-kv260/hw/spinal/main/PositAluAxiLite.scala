package posit.kv260

import spinal.core._
import spinal.lib._
import spinal.lib.bus.amba4.axilite._
import posit._
import posit.vector._

// =====================================================================
//  Etap 1: PositAlu za AXI4-Lite. Dane ida przez rejestry, po jednej
//  parze argumentow: zapis OP, OPERAND_A, OPERAND_B, CTRL.start,
//  czekanie na STATUS.done, odczyt RESULT.
//
//  Mapa rejestrow (32 b, offsety w bajtach) - PositAluRegs ponizej;
//  ta sama w sw/positnp/regs.py (zgodnosc sprawdza PositAluRegsTestplan).
//
//  Porty pod IP packagera Vivado: s_axi_* (AXI4-Lite), aclk, aresetn
//  (reset synchroniczny aktywny niskim, Conventions.spinal).
// =====================================================================
object PositAluRegs {
  val ID_VERSION  = 0x00  // RO  [31:16] ID = 0x5053 ("PS"), [15:8] major, [7:0] minor
  val CONFIG      = 0x04  // RO  [7:0] n, [11:8] es, [31:16] maska zbudowanych operacji (1 << kod)
  val CTRL        = 0x08  // WO  bit 0 start, bit 1 miekki reset, bit 2 zerowanie licznikow (impulsy)
  val STATUS      = 0x0C  // RO  bit 0 busy, bit 1 done, bit 2 error, [10:8] flagi ostatniego wyniku
  val OP          = 0x10  // RW  [3:0] kod operacji (PositAluOp)
  val OPERAND_A   = 0x14  // RW  [n-1:0]
  val OPERAND_B   = 0x18  // RW  [n-1:0]
  val RESULT      = 0x1C  // RO  [n-1:0] ostatni wynik
  val CNT_NAR     = 0x40  // RO  wyniki NaR od ostatniego zerowania
  val CNT_SAT     = 0x44  // RO  wyniki nasycone do maxpos / minpos
  val CNT_INEXACT = 0x48  // RO  wyniki niedokladne
  val CYCLES      = 0x4C  // RO  takty w stanie busy

  val CTRL_START          = 0
  val CTRL_SOFT_RESET     = 1
  val CTRL_CLEAR_COUNTERS = 2

  val STATUS_BUSY        = 0
  val STATUS_DONE        = 1
  val STATUS_ERROR       = 2
  val STATUS_FLAGS_SHIFT = 8   // nar = 8, sat = 9, inexact = 10 (kolejnosc PositFlags)

  val ID      = 0x5053
  val VERSION = 0x0100         // 1.0

  def idVersion : Long = (ID.toLong << 16) | VERSION
  def config(cfg : PositAluConfig) : Long = (cfg.opMask.toLong << 16) | (cfg.c.es << 8) | cfg.c.n

  /** Wszystkie stale (nazwa -> wartosc) - do porownania z regs.py. */
  val all : Seq[(String, Long)] = Seq[(String, Long)](
    "ID_VERSION" -> ID_VERSION, "CONFIG" -> CONFIG, "CTRL" -> CTRL, "STATUS" -> STATUS, "OP" -> OP,
    "OPERAND_A" -> OPERAND_A, "OPERAND_B" -> OPERAND_B, "RESULT" -> RESULT,
    "CNT_NAR" -> CNT_NAR, "CNT_SAT" -> CNT_SAT, "CNT_INEXACT" -> CNT_INEXACT, "CYCLES" -> CYCLES,
    "CTRL_START" -> CTRL_START, "CTRL_SOFT_RESET" -> CTRL_SOFT_RESET, "CTRL_CLEAR_COUNTERS" -> CTRL_CLEAR_COUNTERS,
    "STATUS_BUSY" -> STATUS_BUSY, "STATUS_DONE" -> STATUS_DONE, "STATUS_ERROR" -> STATUS_ERROR,
    "STATUS_FLAGS_SHIFT" -> STATUS_FLAGS_SHIFT, "ID" -> ID, "VERSION" -> VERSION) ++
    PositAluOp.codes.toSeq.sortBy(_._2).map { case (op, code) => s"OP_${op.name.toUpperCase}" -> code.toLong }
}

object PositAluAxiLite {
  val busConfig = AxiLite4Config(addressWidth = 12, dataWidth = 32)   // 4 KB: najmniejszy segment w Vivado
}

class PositAluAxiLite(val cfg : PositAluConfig) extends Component {
  import PositAluRegs._
  val c = cfg.c
  require(c.n <= 32, "argumenty i wynik w jednym slowie 32 b")

  val io = new Bundle {
    val s_axi = slave(AxiLite4(PositAluAxiLite.busConfig))
  }
  noIoPrefix()
  AxiLite4SpecRenamer(io.s_axi)
  ClockDomain.current.renamePulledWires(clock = "aclk", reset = "aresetn")

  val bus = AxiLite4SlaveFactory(io.s_axi)

  // ---- CTRL: impulsy na zapis -----------------------------------------------------
  val ctrlData  = Bits(32 bits)
  bus.nonStopWrite(ctrlData, 0)
  val start     = Bool()
  val softReset = Bool()
  val clearCnt  = Bool()
  start     := False
  softReset := False
  clearCnt  := False
  bus.onWrite(CTRL) {
    start     := ctrlData(CTRL_START)
    softReset := ctrlData(CTRL_SOFT_RESET)
    clearCnt  := ctrlData(CTRL_CLEAR_COUNTERS)
  }

  // ---- rejestry argumentow ----------------------------------------------------------
  val opReg = Reg(UInt(PositAluOp.width bits)) init 0
  val aReg  = Reg(Bits(c.n bits)) init 0
  val bReg  = Reg(Bits(c.n bits)) init 0
  bus.driveAndRead(opReg, OP)
  bus.driveAndRead(aReg, OPERAND_A)
  bus.driveAndRead(bReg, OPERAND_B)

  // ---- ALU: miekki reset przerywa dzielenie w toku ----------------------------------
  val core = new ResetArea(softReset, cumulative = true) {
    val alu = new PositAlu(cfg)
  }
  val alu = core.alu

  val busy     = RegInit(False)
  val done     = RegInit(False)
  val error    = RegInit(False)
  val cmdValid = RegInit(False)
  val cmd      = Reg(PositAluCmd(c))     // polecenie zatrzaskiwane przy starcie: zapis OP/A/B w trakcie nie psuje strumienia
  val result   = Reg(Bits(c.n bits)) init 0
  val flags    = Reg(Bits(3 bits)) init 0

  val legal = B(cfg.opMask, 1 << PositAluOp.width bits)(opReg)

  alu.io.cmd.valid   := cmdValid
  alu.io.cmd.payload := cmd
  alu.io.rsp.ready   := True

  when(alu.io.cmd.fire) { cmdValid := False }

  val cntNar, cntSat, cntInexact, cycles = Reg(UInt(32 bits)) init 0
  when(busy) { cycles := cycles + 1 }

  when(alu.io.rsp.fire) {
    result := alu.io.rsp.bits
    flags  := alu.io.rsp.flags.asBits
    busy   := False
    done   := True
    when(alu.io.rsp.flags.nar)     { cntNar     := cntNar + 1 }
    when(alu.io.rsp.flags.sat)     { cntSat     := cntSat + 1 }
    when(alu.io.rsp.flags.inexact) { cntInexact := cntInexact + 1 }
  }

  // start w trakcie pracy jest ignorowany; kod spoza maski: error, bez liczenia
  when(start && !busy) {
    done  := !legal
    error := !legal
    when(legal) {
      busy     := True
      cmdValid := True
      cmd.op   := opReg
      cmd.a    := aReg
      cmd.b    := bReg
      cmd.addend := 0
    }
  }

  when(softReset) {
    busy     := False
    done     := False
    error    := False
    cmdValid := False
  }
  when(clearCnt) {
    cntNar     := 0
    cntSat     := 0
    cntInexact := 0
    cycles     := 0
  }

  // ---- odczyty ----------------------------------------------------------------------
  bus.read(B(idVersion, 32 bits), ID_VERSION)
  bus.read(B(config(cfg), 32 bits), CONFIG)
  bus.read(flags ## B(0, 5 bits) ## error ## done ## busy, STATUS)
  bus.read(result, RESULT)
  bus.read(cntNar, CNT_NAR)
  bus.read(cntSat, CNT_SAT)
  bus.read(cntInexact, CNT_INEXACT)
  bus.read(cycles, CYCLES)
}
