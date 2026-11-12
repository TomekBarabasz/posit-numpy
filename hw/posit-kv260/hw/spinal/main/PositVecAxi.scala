package posit.kv260

import spinal.core._
import spinal.lib._
import spinal.lib.bus.amba4.axilite._
import posit._
import posit.vector._

// =====================================================================
//  Etap 2: silnik na tablicach. Rejestry z etapu 1 (pojedyncze dzialania)
//  plus AXI4-Stream do AXI DMA:
//
//    s_axis (z MM2S): tdata = {b, a} (2n b, a w mlodszej polowie), tlast
//    m_axis (do S2MM): tdata = wynik (n b), tlast na ostatnim elemencie
//
//  Operacja tablicy: OP (jak w etapie 1); STREAM_CFG.scalar_b: b z
//  OPERAND_B zamiast ze strumienia (tablica (+) skalar).
//  Liczniki NaR / sat / inexact licza wyniki obu sciezek, CNT_ELEMENTS
//  tylko wyniki strumienia; CYCLES: takty z czymkolwiek w ALU.
//
//  Mapa rejestrow: PositVecRegs (= PositAluRegs + rozszerzenia),
//  w Pythonie sw/positnp/regs.py. Porty: s_axi_*, s_axis_*, m_axis_*, aclk, aresetn.
// =====================================================================
object PositVecRegs {
  import PositAluRegs._
  val STREAM_CFG    = 0x24  // RW  bit 0 scalar_b: tablica (+) skalar, b = OPERAND_B
  val CNT_ELEMENTS  = 0x50  // RO  wyniki strumienia od ostatniego zerowania

  val STREAM_CFG_SCALAR_B = 0
  val STATUS_STREAM_ACTIVE = 3   // element(y) w ALU
  val CONFIG_HAS_STREAM    = 12  // bit CONFIG: silnik ma porty strumieniowe

  val VERSION = 0x0200           // 2.0

  def idVersion : Long = (ID.toLong << 16) | VERSION
  def config(cfg : PositAluConfig) : Long = PositAluRegs.config(cfg) | (1L << CONFIG_HAS_STREAM)

  /** Stale dodane w etapie 2 (nazwa -> wartosc) - do porownania z regs.py. */
  val all : Seq[(String, Long)] = Seq[(String, Long)](
    "STREAM_CFG" -> STREAM_CFG, "CNT_ELEMENTS" -> CNT_ELEMENTS,
    "STREAM_CFG_SCALAR_B" -> STREAM_CFG_SCALAR_B, "STATUS_STREAM_ACTIVE" -> STATUS_STREAM_ACTIVE,
    "CONFIG_HAS_STREAM" -> CONFIG_HAS_STREAM, "VERSION_VEC" -> VERSION)
}

class PositVecAxi(val cfg : PositAluConfig) extends Component {
  import PositAluRegs.{VERSION => _, idVersion => _, config => _, all => _, _}
  import PositVecRegs._
  val c = cfg.c
  require(c.n <= 32 && c.n % 8 == 0, "AXI DMA: tdata w pelnych bajtach, a i b w jednym slowie 64 b")

  val io = new Bundle {
    val s_axi  = slave(AxiLite4(PositAluAxiLite.busConfig))
    val s_axis = new Bundle {
      val tdata  = in  Bits(2 * c.n bits)
      val tlast  = in  Bool()
      val tvalid = in  Bool()
      val tready = out Bool()
    }
    val m_axis = new Bundle {
      val tdata  = out Bits(c.n bits)
      val tlast  = out Bool()
      val tvalid = out Bool()
      val tready = in  Bool()
    }
  }
  noIoPrefix()
  AxiLite4SpecRenamer(io.s_axi)
  ClockDomain.current.renamePulledWires(clock = "aclk", reset = "aresetn")

  val bus = AxiLite4SlaveFactory(io.s_axi)

  // ---- CTRL: impulsy na zapis -----------------------------------------------------
  val ctrlData  = Bits(32 bits)
  bus.nonStopWrite(ctrlData, 0)
  val start, softReset, clearCnt = Bool()
  start     := False
  softReset := False
  clearCnt  := False
  bus.onWrite(CTRL) {
    start     := ctrlData(CTRL_START)
    softReset := ctrlData(CTRL_SOFT_RESET)
    clearCnt  := ctrlData(CTRL_CLEAR_COUNTERS)
  }

  // ---- rejestry ---------------------------------------------------------------------
  val opReg     = Reg(UInt(PositAluOp.width bits)) init 0
  val aReg      = Reg(Bits(c.n bits)) init 0
  val bReg      = Reg(Bits(c.n bits)) init 0
  val streamCfg = Reg(Bits(1 bits)) init 0
  bus.driveAndRead(opReg, OP)
  bus.driveAndRead(aReg, OPERAND_A)
  bus.driveAndRead(bReg, OPERAND_B)
  bus.driveAndRead(streamCfg, STREAM_CFG)

  // ---- silnik: miekki reset przerywa dzialanie i oproznia potok -----------------------
  val core = new ResetArea(softReset, cumulative = true) {
    val engine = new PositStreamAlu(cfg)
  }
  val engine = core.engine

  engine.io.op      := opReg
  engine.io.scalarB := streamCfg(STREAM_CFG_SCALAR_B)
  engine.io.b       := bReg

  // AXI4-Stream wejscie: {b, a}
  engine.io.input.valid         := io.s_axis.tvalid
  engine.io.input.fragment.a    := io.s_axis.tdata(c.n - 1 downto 0)
  engine.io.input.fragment.b    := io.s_axis.tdata(2 * c.n - 1 downto c.n)
  engine.io.input.last          := io.s_axis.tlast
  io.s_axis.tready              := engine.io.input.ready

  // AXI4-Stream wyjscie: wynik
  io.m_axis.tvalid              := engine.io.output.valid
  io.m_axis.tdata               := engine.io.output.fragment.bits
  io.m_axis.tlast               := engine.io.output.last
  engine.io.output.ready        := io.m_axis.tready

  // ---- sciezka rejestrowa (etap 1) ----------------------------------------------------
  val busy     = RegInit(False)
  val done     = RegInit(False)
  val error    = RegInit(False)
  val cmdValid = RegInit(False)
  val cmd      = Reg(PositAluCmd(c))
  val result   = Reg(Bits(c.n bits)) init 0
  val flags    = Reg(Bits(3 bits)) init 0
  val legal    = B(cfg.opMask, 1 << PositAluOp.width bits)(opReg)

  engine.io.reg.valid   := cmdValid
  engine.io.reg.payload := cmd
  engine.io.regRsp.ready := True
  when(engine.io.reg.fire) { cmdValid := False }

  // ---- liczniki -----------------------------------------------------------------------
  val cntNar, cntSat, cntInexact, cycles, cntElements = Reg(UInt(32 bits)) init 0
  when(busy || engine.io.inFlight) { cycles := cycles + 1 }

  def count(f : PositFlags) : Unit = {
    when(f.nar)     { cntNar     := cntNar + 1 }
    when(f.sat)     { cntSat     := cntSat + 1 }
    when(f.inexact) { cntInexact := cntInexact + 1 }
  }
  // oba wyniki w jednym takcie sa niemozliwe: ALU wydaje jeden wynik na takt
  when(engine.io.regRsp.fire) {
    result := engine.io.regRsp.bits
    flags  := engine.io.regRsp.flags.asBits
    busy   := False
    done   := True
    count(engine.io.regRsp.flags)
  }
  when(engine.io.output.fire) {
    cntElements := cntElements + 1
    count(engine.io.output.fragment.flags)
  }

  when(start && !busy) {
    done  := !legal
    error := !legal
    when(legal) {
      busy     := True
      cmdValid := True
      cmd.op   := opReg
      cmd.a    := aReg
      cmd.b    := bReg
    }
  }
  when(softReset) {
    busy     := False
    done     := False
    error    := False
    cmdValid := False
  }
  when(clearCnt) {
    cntNar      := 0
    cntSat      := 0
    cntInexact  := 0
    cycles      := 0
    cntElements := 0
  }

  // ---- odczyty ----------------------------------------------------------------------
  bus.read(B(idVersion, 32 bits), ID_VERSION)
  bus.read(B(config(cfg), 32 bits), CONFIG)
  bus.read(flags ## B(0, 4 bits) ## engine.io.inFlight ## error ## done ## busy, STATUS)
  bus.read(result, RESULT)
  bus.read(cntNar, CNT_NAR)
  bus.read(cntSat, CNT_SAT)
  bus.read(cntInexact, CNT_INEXACT)
  bus.read(cycles, CYCLES)
  bus.read(cntElements, CNT_ELEMENTS)
}
