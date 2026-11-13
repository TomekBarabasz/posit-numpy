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
//  Etap 4 (wersja 3.0): quire. OP = SUM / DOT / SUMSQ kieruje s_axis do
//  PositQuire zamiast do ALU (bez wynikow na m_axis: tylko MM2S w DMA).
//  CTRL.quire_clear zeruje quire, CTRL.quire_round zaokragla biezacy stan
//  do RESULT (+ flagi, STATUS.done), surowy quire w QUIRE[k] od 0x80.
//  Quire nie jest niszczony odczytem: tablice dluzsze niz jeden transfer
//  DMA akumuluja sie dalej. CNT_ELEMENTS liczy tez elementy quire.
//
//  Etap 5 (wersja 4.0): OP = SQRT / FMA / AXPY / F2P / P2F w ALU (gdy
//  zbudowane, PositAluConfig.sqrt/fma/convert): FMA = a * b + OPERAND_C,
//  AXPY = a * OPERAND_B + b (y = alfa x + y w jednym przebiegu strumienia),
//  F2P / P2F: float32 <-> posit32. Ktore sa zbudowane: CONFIG.opMask.
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

  // etap 4: quire
  val QUIRE_CFG     = 0x54  // RO  [15:0] szerokosc quire w bitach, [31:16] quireFrac (LSB = 2^-quireFrac)
  val QUIRE         = 0x80  // RO  QUIRE + 4 k: slowo k surowego quire (U2), od najmlodszego
  val OP_SUM        = 4     // sum a
  val OP_DOT        = 5     // sum a * b
  val OP_SUMSQ      = 6     // sum a * a
  val CTRL_QUIRE_CLEAR  = 3
  val CTRL_QUIRE_ROUND  = 4
  val STATUS_QUIRE_BUSY = 4
  val STATUS_QUIRE_NAR  = 5
  val CONFIG_HAS_QUIRE  = 13
  val REDUCE_MASK = (1 << OP_SUM) | (1 << OP_DOT) | (1 << OP_SUMSQ)

  // etap 5: sqrt, FMA / AXPY, konwersje float32 (kody w PositAluOp)
  val OPERAND_C = 0x58  // RW  [n-1:0] trzeci argument FMA (a * b + c)
  val OP_SQRT   = PositAluOp.SQRT
  val OP_FMA    = PositAluOp.FMA
  val OP_AXPY   = PositAluOp.AXPY
  val OP_F2P    = PositAluOp.F2P
  val OP_P2F    = PositAluOp.P2F

  // 4.1: parametry silnika do odczytu (tory wielotaktowe, slowUnits)
  val ENGINE_CFG = 0x5C  // RO  [7:0] k: bity dzielnika i sqrt na takt, [15:8] tory div / sqrt / FMA (slowUnits)

  val VERSION = 0x0401           // 4.1

  def idVersion : Long = (ID.toLong << 16) | VERSION
  def config(cfg : PositAluConfig) : Long =
    PositAluRegs.config(cfg) | (1L << CONFIG_HAS_STREAM) | (1L << CONFIG_HAS_QUIRE) | (REDUCE_MASK.toLong << 16)
  def engineCfg(cfg : PositAluConfig) : Long = (cfg.slowUnits.toLong << 8) | cfg.divBitsPerCycle
  def quireCfg(cfg : PositAluConfig) : Long = (cfg.c.quireFrac.toLong << 16) | PositQuireConfig(cfg.c).width

  /** Stale dodane w etapie 2 (nazwa -> wartosc) - do porownania z regs.py. */
  val all : Seq[(String, Long)] = Seq[(String, Long)](
    "STREAM_CFG" -> STREAM_CFG, "CNT_ELEMENTS" -> CNT_ELEMENTS,
    "STREAM_CFG_SCALAR_B" -> STREAM_CFG_SCALAR_B, "STATUS_STREAM_ACTIVE" -> STATUS_STREAM_ACTIVE,
    "CONFIG_HAS_STREAM" -> CONFIG_HAS_STREAM, "VERSION_VEC" -> VERSION,
    "QUIRE_CFG" -> QUIRE_CFG, "QUIRE" -> QUIRE, "OP_SUM" -> OP_SUM, "OP_DOT" -> OP_DOT, "OP_SUMSQ" -> OP_SUMSQ,
    "CTRL_QUIRE_CLEAR" -> CTRL_QUIRE_CLEAR, "CTRL_QUIRE_ROUND" -> CTRL_QUIRE_ROUND,
    "STATUS_QUIRE_BUSY" -> STATUS_QUIRE_BUSY, "STATUS_QUIRE_NAR" -> STATUS_QUIRE_NAR,
    "CONFIG_HAS_QUIRE" -> CONFIG_HAS_QUIRE,
    "OPERAND_C" -> OPERAND_C, "OP_SQRT" -> OP_SQRT, "OP_FMA" -> OP_FMA, "OP_AXPY" -> OP_AXPY,
    "OP_F2P" -> OP_F2P, "OP_P2F" -> OP_P2F, "ENGINE_CFG" -> ENGINE_CFG)
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
  val start, softReset, clearCnt, quireClear, quireRound = Bool()
  start      := False
  softReset  := False
  clearCnt   := False
  quireClear := False
  quireRound := False
  bus.onWrite(CTRL) {
    start      := ctrlData(CTRL_START)
    softReset  := ctrlData(CTRL_SOFT_RESET)
    clearCnt   := ctrlData(CTRL_CLEAR_COUNTERS)
    quireClear := ctrlData(CTRL_QUIRE_CLEAR)
    quireRound := ctrlData(CTRL_QUIRE_ROUND)
  }

  // ---- rejestry ---------------------------------------------------------------------
  val opReg     = Reg(UInt(PositAluOp.width bits)) init 0
  val aReg      = Reg(Bits(c.n bits)) init 0
  val bReg      = Reg(Bits(c.n bits)) init 0
  val streamCfg = Reg(Bits(1 bits)) init 0
  val cReg      = Reg(Bits(c.n bits)) init 0
  bus.driveAndRead(opReg, OP)
  bus.driveAndRead(aReg, OPERAND_A)
  bus.driveAndRead(bReg, OPERAND_B)
  bus.driveAndRead(streamCfg, STREAM_CFG)
  bus.driveAndRead(cReg, OPERAND_C)

  // ---- silnik: miekki reset przerywa dzialanie i oproznia potok -----------------------
  val core = new ResetArea(softReset, cumulative = true) {
    val engine = new PositStreamAlu(cfg)
    val quire  = new PositQuire(PositQuireConfig(c))
  }
  val engine = core.engine
  val quire  = core.quire

  engine.io.op      := opReg
  engine.io.scalarB := streamCfg(STREAM_CFG_SCALAR_B)
  engine.io.b       := bReg
  engine.io.addend  := cReg

  // AXI4-Stream wejscie: {b, a}; OP sum/dot/sumsq -> quire, reszta -> ALU
  val reduce = opReg === OP_SUM || opReg === OP_DOT || opReg === OP_SUMSQ
  val inA    = io.s_axis.tdata(c.n - 1 downto 0)
  val inB    = io.s_axis.tdata(2 * c.n - 1 downto c.n)
  engine.io.input.valid         := io.s_axis.tvalid && !reduce
  engine.io.input.fragment.a    := inA
  engine.io.input.fragment.b    := inB
  engine.io.input.last          := io.s_axis.tlast
  quire.io.input.valid          := io.s_axis.tvalid && reduce
  quire.io.input.a              := inA
  quire.io.input.b              := Mux(opReg === OP_SUMSQ, inA, inB)
  quire.io.input.dot            := opReg =/= OP_SUM
  quire.io.clear                := quireClear
  quire.io.round                := quireRound
  io.s_axis.tready              := Mux(reduce, quire.io.input.ready, engine.io.input.ready)

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
  when(busy || engine.io.inFlight || quire.io.busy) { cycles := cycles + 1 }

  // Trzy zrodla wynikow (rejestry, strumien, quire) moga trafic na ten sam takt:
  // liczniki dodaja sume przyrostow, nie nadpisuja sie nawzajem.
  val sources = Seq(
    (engine.io.regRsp.fire,   engine.io.regRsp.flags),
    (engine.io.output.fire,   engine.io.output.fragment.flags),
    (quire.io.result.valid,   quire.io.result.flags))
  def inc(pick : PositFlags => Bool) : UInt =
    sources.map { case (v, f) => (v && pick(f)).asUInt.resize(2) }.reduce(_ + _)
  cntNar      := cntNar + inc(_.nar)
  cntSat      := cntSat + inc(_.sat)
  cntInexact  := cntInexact + inc(_.inexact)
  cntElements := cntElements + engine.io.output.fire.asUInt.resize(2) + quire.io.input.fire.asUInt.resize(2)

  when(engine.io.regRsp.fire) {
    result := engine.io.regRsp.bits
    flags  := engine.io.regRsp.flags.asBits
    busy   := False
    done   := True
  }
  when(quireRound) { done := False }
  when(quire.io.result.valid) {
    result := quire.io.result.bits
    flags  := quire.io.result.flags.asBits
    done   := True
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
      cmd.addend := cReg
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
  bus.read(flags ## B(0, 2 bits) ## quire.io.nar ## quire.io.busy ## engine.io.inFlight ## error ## done ## busy, STATUS)
  bus.read(result, RESULT)
  bus.read(cntNar, CNT_NAR)
  bus.read(cntSat, CNT_SAT)
  bus.read(cntInexact, CNT_INEXACT)
  bus.read(cycles, CYCLES)
  bus.read(cntElements, CNT_ELEMENTS)
  bus.read(B(quireCfg(cfg), 32 bits), QUIRE_CFG)
  bus.read(B(engineCfg(cfg), 32 bits), ENGINE_CFG)
  for (k <- 0 until quire.qc.width / 32) bus.read(quire.io.raw(32 * k + 31 downto 32 * k), QUIRE + 4 * k)
}
