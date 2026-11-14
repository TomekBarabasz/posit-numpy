package posit.kv260

import spinal.core._
import spinal.lib._
import spinal.lib.bus.amba4.axilite._
import spinal.lib.bus.amba4.axi.{Axi4, Axi4SpecRenamer}
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

  // etap 6: wlasny master AXI4 (PositMemMover) zamiast AXI4-Stream + AXI DMA
  val MEM_LEN    = 0x60  // RW  elementy polecenia
  val MEM_SRC_A  = 0x64  // RW  adres fizyczny a (wyrownany do slowa danych, 16 B)
  val MEM_SRC_B  = 0x68  // RW  adres b
  val MEM_SRC_C  = 0x6C  // RW  adres c (FMA3)
  val MEM_DST    = 0x70  // RW  adres wyniku (moze byc rowny SRC_A: dzialanie w miejscu)
  val MEM_LO     = 0x74  // RW  dozwolone adresy: [MEM_LO, MEM_HI); po resecie pusty zakres
  val MEM_HI     = 0x78  // RW
  val MEM_CFG    = 0x7C  // RW  bit 0 read_b, 1 read_c, 2 write, 3 irq_en
  val MEM_STATUS = 0xC0  // RO  bit 0 busy, 1 done, 2 err_range, 3 err_align, 4 err_resp
  val MEM_CYCLES = 0xC4  // RO  takty ostatniego polecenia
  val MEM_CFG_READ_B = 0
  val MEM_CFG_READ_C = 1
  val MEM_CFG_WRITE  = 2
  val MEM_CFG_IRQ_EN = 3
  val MEM_ST_BUSY    = 0
  val MEM_ST_DONE    = 1
  val MEM_ST_ERR_RANGE = 2
  val MEM_ST_ERR_ALIGN = 3
  val MEM_ST_ERR_RESP  = 4
  val CTRL_MEM_START = 5   // CTRL: polecenie z MEM_*
  val CTRL_MEM_ACK   = 6   // CTRL: kasuje MEM_STATUS.done (i przerwanie)
  val CONFIG_HAS_MASTER = 14
  val OP_FMA3 = PositAluOp.FMA3
  val MEM_BEAT_BYTES = 16

  val VERSION = 0x0500           // 5.0

  def idVersion : Long = (ID.toLong << 16) | VERSION
  def config(cfg : PositAluConfig, master : Boolean = false) : Long =
    PositAluRegs.config(cfg) | (1L << CONFIG_HAS_STREAM) | (1L << CONFIG_HAS_QUIRE) | (REDUCE_MASK.toLong << 16) |
    (if (master) 1L << CONFIG_HAS_MASTER else 0L)
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
    "OP_F2P" -> OP_F2P, "OP_P2F" -> OP_P2F, "ENGINE_CFG" -> ENGINE_CFG,
    "MEM_LEN" -> MEM_LEN, "MEM_SRC_A" -> MEM_SRC_A, "MEM_SRC_B" -> MEM_SRC_B, "MEM_SRC_C" -> MEM_SRC_C,
    "MEM_DST" -> MEM_DST, "MEM_LO" -> MEM_LO, "MEM_HI" -> MEM_HI, "MEM_CFG" -> MEM_CFG,
    "MEM_STATUS" -> MEM_STATUS, "MEM_CYCLES" -> MEM_CYCLES,
    "MEM_CFG_READ_B" -> MEM_CFG_READ_B, "MEM_CFG_READ_C" -> MEM_CFG_READ_C, "MEM_CFG_WRITE" -> MEM_CFG_WRITE,
    "MEM_CFG_IRQ_EN" -> MEM_CFG_IRQ_EN, "MEM_ST_BUSY" -> MEM_ST_BUSY, "MEM_ST_DONE" -> MEM_ST_DONE,
    "MEM_ST_ERR_RANGE" -> MEM_ST_ERR_RANGE, "MEM_ST_ERR_ALIGN" -> MEM_ST_ERR_ALIGN, "MEM_ST_ERR_RESP" -> MEM_ST_ERR_RESP,
    "CTRL_MEM_START" -> CTRL_MEM_START, "CTRL_MEM_ACK" -> CTRL_MEM_ACK, "CONFIG_HAS_MASTER" -> CONFIG_HAS_MASTER,
    "OP_FMA3" -> OP_FMA3, "MEM_BEAT_BYTES" -> MEM_BEAT_BYTES)
}

/** withMaster = false: dane przez AXI4-Stream (s_axis {b, a}, m_axis wynik) z AXI DMA (etapy 2-5);
  * withMaster = true (etap 6): wlasny master AXI4 m_axi (PositMemMover) czyta a, b, c i zapisuje wynik
  * pod adresami z rejestrow MEM_*, wyjscie przerwania irq. */
class PositVecAxi(val cfg : PositAluConfig, val withMaster : Boolean = false,
                  val memCfg : Option[MemMoverConfig] = None) extends Component {
  import PositAluRegs.{VERSION => _, idVersion => _, config => _, all => _, _}
  import PositVecRegs._
  val c = cfg.c
  require(c.n <= 32 && c.n % 8 == 0, "tdata / slowa pamieci w pelnych bajtach")
  require(!cfg.fma3 || withMaster, "FMA3 (trzecia tablica) tylko z masterem AXI4")
  val mc = memCfg.getOrElse(MemMoverConfig(c.n))

  val io = new Bundle {
    val s_axi  = slave(AxiLite4(PositAluAxiLite.busConfig))
    val s_axis = !withMaster generate new Bundle {
      val tdata  = in  Bits(2 * c.n bits)
      val tlast  = in  Bool()
      val tvalid = in  Bool()
      val tready = out Bool()
    }
    val m_axis = !withMaster generate new Bundle {
      val tdata  = out Bits(c.n bits)
      val tlast  = out Bool()
      val tvalid = out Bool()
      val tready = in  Bool()
    }
    val m_axi = withMaster generate master(Axi4(mc.axi))
    val irq   = withMaster generate out(Bool())
  }
  noIoPrefix()
  AxiLite4SpecRenamer(io.s_axi)
  if (withMaster) Axi4SpecRenamer(io.m_axi)
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
  bus.readAndWrite(opReg, OP)
  bus.readAndWrite(aReg, OPERAND_A)
  bus.readAndWrite(bReg, OPERAND_B)
  bus.readAndWrite(streamCfg, STREAM_CFG)
  bus.readAndWrite(cReg, OPERAND_C)

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

  // ---- dane: elementy {a, b(, c), last}; OP sum/dot/sumsq -> quire, reszta -> ALU ----------
  val reduce = opReg === OP_SUM || opReg === OP_DOT || opReg === OP_SUMSQ
  val inValid = Bool()
  val inA, inB = Bits(c.n bits)
  val inC     = Bits(c.n bits)
  val inLast  = Bool()
  val inReady = Bool()
  engine.io.input.valid         := inValid && !reduce
  engine.io.input.fragment.a    := inA
  engine.io.input.fragment.b    := inB
  if (cfg.fma3) engine.io.input.fragment.c3 := inC
  engine.io.input.last          := inLast
  quire.io.input.valid          := inValid && reduce
  quire.io.input.a              := inA
  quire.io.input.b              := Mux(opReg === OP_SUMSQ, inA, inB)
  quire.io.input.dot            := opReg =/= OP_SUM
  quire.io.clear                := quireClear
  quire.io.round                := quireRound
  inReady                       := Mux(reduce, quire.io.input.ready, engine.io.input.ready)

  val stream = !withMaster generate new Area {
    // AXI4-Stream wejscie {b, a}, wyjscie: wynik (AXI DMA)
    inValid := io.s_axis.tvalid
    inA     := io.s_axis.tdata(c.n - 1 downto 0)
    inB     := io.s_axis.tdata(2 * c.n - 1 downto c.n)
    inC     := 0
    inLast  := io.s_axis.tlast
    io.s_axis.tready       := inReady
    io.m_axis.tvalid       := engine.io.output.valid
    io.m_axis.tdata        := engine.io.output.fragment.bits
    io.m_axis.tlast        := engine.io.output.last
    engine.io.output.ready := io.m_axis.tready
  }

  // ---- etap 6: master AXI4 -------------------------------------------------------------------
  val memStart, memAck = Bool()
  memStart := False
  memAck   := False
  bus.onWrite(CTRL) {
    memStart := ctrlData(CTRL_MEM_START)
    memAck   := ctrlData(CTRL_MEM_ACK)
  }
  val mem = withMaster generate new Area {
    val len   = Reg(UInt(32 bits)) init 0
    val src   = Vec.fill(3)(Reg(UInt(32 bits)) init 0)
    val dst   = Reg(UInt(32 bits)) init 0
    val lo    = Reg(UInt(32 bits)) init 0
    val hi    = Reg(UInt(32 bits)) init 0
    val mcfg  = Reg(Bits(4 bits)) init 0
    bus.readAndWrite(len, MEM_LEN)
    for (i <- 0 until 3) bus.readAndWrite(src(i), MEM_SRC_A + 4 * i)
    bus.readAndWrite(dst, MEM_DST)
    bus.readAndWrite(lo, MEM_LO)
    bus.readAndWrite(hi, MEM_HI)
    bus.readAndWrite(mcfg, MEM_CFG)

    val mover = new PositMemMover(mc)
    io.m_axi <> mover.io.axi

    val busy, done, errRange, errAlign, errResp = RegInit(False)
    val cycles = Reg(UInt(32 bits)) init 0
    when(busy) { cycles := cycles + 1 }

    // zakres: [addr, addr + slowa * 16) w [lo, hi), bez przekroczenia 2^32
    val bytes = (mc.beats(len).resize(33) << log2Up(mc.beatBytes)).resize(33)
    def inRange(a : UInt) : Bool = {
      val end = a.resize(33) + bytes
      a >= lo && end <= hi.resize(33)
    }
    def aligned(a : UInt) : Bool = a(log2Up(mc.beatBytes) - 1 downto 0) === 0
    val use = Vec(True, mcfg(MEM_CFG_READ_B), mcfg(MEM_CFG_READ_C))
    val wr  = mcfg(MEM_CFG_WRITE)
    val badRange = (0 until 3).map(i => use(i) && !inRange(src(i))).reduce(_ || _) || (wr && !inRange(dst))
    val badAlign = (0 until 3).map(i => use(i) && !aligned(src(i))).reduce(_ || _) || (wr && !aligned(dst))

    mover.io.cmd.valid         := False
    mover.io.cmd.payload.len   := len
    mover.io.cmd.payload.src   := src
    mover.io.cmd.payload.read  := use.asBits
    mover.io.cmd.payload.dst   := dst
    mover.io.cmd.payload.write := wr
    when(memStart && !busy) {
      done     := False
      errRange := badRange
      errAlign := badAlign
      errResp  := False
      cycles   := 0
      when(badRange || badAlign || len === 0) {
        done := True                                   // bez zadnej transakcji
      } otherwise {
        mover.io.cmd.valid := True
        busy := True
      }
    }
    when(mover.io.respError) { errResp := True }
    when(mover.io.done) { busy := False; done := True }
    when(memAck) { done := False }
    io.irq := done && mcfg(MEM_CFG_IRQ_EN)

    // elementy z mastera -> silnik / quire; wyniki silnika -> zapis
    val sa = mover.io.src(0)
    val sb = mover.io.src(1)
    val sc = mover.io.src(2)
    val needB = use(1)
    val needC = use(2)
    inValid := sa.valid && (!needB || sb.valid) && (!needC || sc.valid)
    inA     := sa.fragment
    inB     := Mux(needB, sb.fragment, B(0, c.n bits))
    inC     := Mux(needC, sc.fragment, B(0, c.n bits))
    inLast  := sa.last
    val take = inValid && inReady
    sa.ready := take
    sb.ready := take && needB
    sc.ready := take && needC
    mover.io.res.valid   := engine.io.output.valid
    mover.io.res.payload := engine.io.output.fragment.bits
    engine.io.output.ready := mover.io.res.ready || !wr      // bez zapisu (np. pomylka w MEM_CFG): wyniki odrzucane

    bus.read(errResp ## errAlign ## errRange ## done ## busy, MEM_STATUS)
    bus.read(cycles, MEM_CYCLES)
  }

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
  when(busy || engine.io.inFlight || quire.io.busy || (if (withMaster) mem.busy else False)) { cycles := cycles + 1 }

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
  bus.read(B(config(cfg, withMaster), 32 bits), CONFIG)
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
