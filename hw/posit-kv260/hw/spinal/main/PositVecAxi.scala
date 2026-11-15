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
  val ENGINE_CFG = 0x5C  // RO  [7:0] k: bity dzielnika i sqrt na takt, [15:8] tory div / sqrt / FMA (slowUnits),
                         //     [23:16] tory elementowe (lanes: elementy na takt, etap 6)

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

  // etap 7a (wersja 5.1): dokladny gemv na quire (withGemv)
  val MEM_WLEN   = 0xC8  // RW  elementy do zapisu przy OP_GEMV (M wierszy); inne operacje: MEM_LEN
  val GEMV_K     = 0xCC  // RW  dlugosc wiersza K (elementy x), 1..GEMV_CFG.maxK
  val GEMV_CFG   = 0xD0  // RO  [15:0] maxK: pojemnosc bufora x (0: brak gemv), [23:16] P: kolumny naraz (5.2)
  val GEMV_COLS  = 0xD4  // RW  kolumny x naraz, 1..P (5.2; po resecie 1)
  val OP_XLOAD   = 13    // MEM_SRC_A, MEM_LEN = K * cols: kolumny x po kolei (K elementow kazda) -> bufory x (bez zapisu)
  val OP_GEMV    = 14    // y[i, c] = round(sum_j A[i, j] x_c[j]): A z MEM_SRC_A (M * K), y (M x cols, wierszami)
                         // do MEM_DST (MEM_WLEN = M, zapis M * cols elementow)
  val MEM_ST_ERR_SHAPE = 5   // MEM_STATUS: GEMV MEM_LEN != M * K, XLOAD MEM_LEN != K * cols, K poza 1..maxK, cols poza 1..P
  val CONFIG_HAS_GEMV  = 15
  val GEMV_MASK = (1 << OP_XLOAD) | (1 << OP_GEMV)

  val VERSION = 0x0502           // 5.2: P kolumn naraz w gemv

  def idVersion : Long = (ID.toLong << 16) | VERSION
  def config(cfg : PositAluConfig, master : Boolean = false, gemv : Boolean = false) : Long =
    PositAluRegs.config(cfg) | (1L << CONFIG_HAS_STREAM) | (1L << CONFIG_HAS_QUIRE) | (REDUCE_MASK.toLong << 16) |
    (if (master) 1L << CONFIG_HAS_MASTER else 0L) |
    (if (gemv) (1L << CONFIG_HAS_GEMV) | (GEMV_MASK.toLong << 16) else 0L)
  def engineCfg(cfg : PositAluConfig, lanes : Int = 1) : Long = (lanes.toLong << 16) | (cfg.slowUnits.toLong << 8) | cfg.divBitsPerCycle
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
    "OP_FMA3" -> OP_FMA3, "MEM_BEAT_BYTES" -> MEM_BEAT_BYTES,
    "MEM_WLEN" -> MEM_WLEN, "GEMV_K" -> GEMV_K, "GEMV_CFG" -> GEMV_CFG, "OP_XLOAD" -> OP_XLOAD, "OP_GEMV" -> OP_GEMV,
    "MEM_ST_ERR_SHAPE" -> MEM_ST_ERR_SHAPE, "CONFIG_HAS_GEMV" -> CONFIG_HAS_GEMV, "GEMV_COLS" -> GEMV_COLS)
}

/** withMaster = false: dane przez AXI4-Stream (s_axis {b, a}, m_axis wynik) z AXI DMA (etapy 2-5);
  * withMaster = true (etap 6): wlasny master AXI4 m_axi (PositMemMover) czyta a, b, c i zapisuje wynik
  * pod adresami z rejestrow MEM_*, wyjscie przerwania irq.
  * withGemv (etap 7a, tylko z masterem): OP_XLOAD laduje x (do gemvMaxK elementow) do BRAM, OP_GEMV
  * liczy y = A x dokladnie: kazdy wiersz w quire, jedno zaokraglenie na wiersz. Dwa quire na zmiane
  * (wiersz i w quire i mod 2), wiec zaokraglenie wiersza i nie zatrzymuje wiersza i + 1.
  * gemvCols = P (5.2): P kolumn x naraz, kazda z wlasna para quire - jedno przejscie A daje P kolumn
  * wyniku, P mnozen z dodawaniem na takt. */
class PositVecAxi(val cfg : PositAluConfig, val withMaster : Boolean = false,
                  val memCfg : Option[MemMoverConfig] = None,
                  val withGemv : Boolean = false, val gemvMaxK : Int = 4096,
                  val gemvCols : Int = 1) extends Component {
  import PositAluRegs.{VERSION => _, idVersion => _, config => _, all => _, _}
  import PositVecRegs._
  val c = cfg.c
  require(c.n <= 32 && c.n % 8 == 0, "tdata / slowa pamieci w pelnych bajtach")
  require(!cfg.fma3 || withMaster, "FMA3 (trzecia tablica) tylko z masterem AXI4")
  val mc = memCfg.getOrElse(MemMoverConfig(c.n))
  val L  = if (withMaster) mc.lanes else 1             // elementy na takt: L kopii PositStreamAlu
  require(mc.n == c.n, s"memCfg.n ${mc.n} != ${c.n}")
  require(!withGemv || withMaster, "gemv tylko z masterem AXI4")
  require(!withGemv || (isPow2(gemvMaxK) && gemvMaxK >= 2 && gemvMaxK <= 32768), s"gemvMaxK $gemvMaxK")
  require(gemvCols >= 1 && gemvCols <= 16, s"gemvCols $gemvCols: 1..16")
  val P = if (withGemv) gemvCols else 0

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
    val engines = Seq.fill(L)(new PositStreamAlu(cfg))
    val quire   = new PositQuire(PositQuireConfig(c))
    // gemv: 2 P quire (bank 0: wiersze parzyste, bank 1: nieparzyste; kolumna 0 banku 0 = quire redukcji)
    val gemvQ   = Seq.fill(2 * P - (if (withGemv) 1 else 0))(new PositQuire(PositQuireConfig(c)))
  }
  val engines = core.engines
  val engine  = engines.head                           // tor 0: tez sciezka rejestrowa
  val quire   = core.quire
  /** Quire gemv: banku b (wiersze i mod 2 = b), kolumny j. */
  def gq(b : Int, j : Int) : PositQuire = if (b == 0 && j == 0) quire else core.gemvQ(b * P + j - 1)
  val gemvExtra = core.gemvQ
  val anyInFlight = engines.map(_.io.inFlight).reduce(_ || _)

  for (e <- engines) {
    e.io.op      := opReg
    e.io.scalarB := streamCfg(STREAM_CFG_SCALAR_B)
    e.io.b       := bReg
    e.io.addend  := cReg
  }
  for (e <- engines.tail) {
    e.io.reg.valid := False
    e.io.reg.payload.assignDontCare()
    e.io.regRsp.ready := True
  }

  // ---- dane: grupy po L elementow {a, b(, c), last}; OP sum/dot/sumsq -> quire, reszta -> ALU ------
  val reduce = opReg === OP_SUM || opReg === OP_DOT || opReg === OP_SUMSQ
  val isXload = if (withGemv) opReg === OP_XLOAD else False
  val isGemv  = if (withGemv) opReg === OP_GEMV else False
  val serial  = reduce || isXload || isGemv           // po jednym elemencie na takt (quire, bufor x)
  val inValid = Bool()
  val inA, inB = Bits(L * c.n bits)
  val inC     = Bits(L * c.n bits)
  val inLast  = Bool()
  val inReady = Bool()
  val lastCnt = UInt(log2Up(L + 1) bits)               // elementy w ostatniej grupie (L, gdy pelna)
  def lane(x : Bits, j : Int) : Bits = x(j * c.n, c.n bits)

  // ALU: przy L > 1 kazdy tor ma wlasna kolejke; grupa idzie dalej, gdy wszystkie maja miejsce
  // (push.ready z rejestrow kolejki - bez petli kombinacyjnej miedzy torami)
  val laneIn = engines.zipWithIndex.map { case (e, j) =>
    val s = Stream(Fragment(PositPair(c, cfg.fma3)))
    s.fragment.a := lane(inA, j)
    s.fragment.b := lane(inB, j)
    if (cfg.fma3) s.fragment.c3 := lane(inC, j)
    s.last := inLast
    if (L == 1) { e.io.input << s } else { e.io.input << s.queue(4) }
    s
  }
  val aluReady = laneIn.map(_.ready).reduce(_ && _)
  for (s <- laneIn) s.valid := inValid && !serial && (if (L == 1) True else aluReady)   // L = 1: bez kolejki, ready nie wraca do valid

  // quire (i gemv): 1 element / takt - grupa rozkladana na elementy
  val sReady = Bool()                                  // odbiorca elementu qA gotowy
  val sFire  = inValid && serial && sReady
  val qIdx = Reg(UInt(log2Up(L) max 1 bits)) init 0
  val qSel = if (L == 1) U(0, 1 bits) else qIdx
  val qA = if (L == 1) inA else inA.subdivideIn(c.n bits)(qSel)
  val qB = if (L == 1) inB else inB.subdivideIn(c.n bits)(qSel)
  val qCnt = Mux(inLast, lastCnt, U(L, lastCnt.getWidth bits))
  val qLastOfGroup = if (L == 1) True else (qIdx.resize(lastCnt.getWidth) + 1 === qCnt)
  quire.io.input.valid          := inValid && reduce
  quire.io.input.a              := qA
  quire.io.input.b              := Mux(opReg === OP_SUMSQ, qA, qB)
  quire.io.input.dot            := opReg =/= OP_SUM
  quire.io.clear                := quireClear
  quire.io.round                := quireRound
  sReady                        := quire.io.input.ready          // gemv: nadpisuje mem.gemv
  if (L > 1) when(sFire) { qIdx := Mux(qLastOfGroup, U(0, qIdx.getWidth bits), qIdx + 1) }
  inReady                       := Mux(serial, sReady && qLastOfGroup, aluReady)
  for (q <- gemvExtra) {
    q.io.input.valid := False
    q.io.input.payload.assignDontCare()
    q.io.clear       := quireClear
    q.io.round       := False
  }

  // wyniki torow -> grupa (StreamJoin: wszystkie tory gotowe, kolejnosc w torach zachowana)
  val outValid = engines.map(_.io.output.valid).reduce(_ && _)
  val outReady = Bool()
  val outBits  = Cat(engines.map(_.io.output.fragment.bits))        // Cat: pierwszy = najmlodsze bity (tor 0)
  val outLast  = engine.io.output.last
  for (e <- engines) e.io.output.ready := outValid && outReady
  val outFire  = outValid && outReady
  val outCnt   = Mux(outLast, lastCnt, U(L, lastCnt.getWidth bits))

  val stream = !withMaster generate new Area {
    // AXI4-Stream wejscie {b, a}, wyjscie: wynik (AXI DMA)
    inValid := io.s_axis.tvalid
    inA     := io.s_axis.tdata(c.n - 1 downto 0)
    inB     := io.s_axis.tdata(2 * c.n - 1 downto c.n)
    inC     := 0
    inLast  := io.s_axis.tlast
    lastCnt := 1
    io.s_axis.tready       := inReady
    io.m_axis.tvalid       := outValid
    io.m_axis.tdata        := outBits
    io.m_axis.tlast        := outLast
    outReady               := io.m_axis.tready
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
    lastCnt := (if (L == 1) U(1, lastCnt.getWidth bits) else {
      val rem = len(log2Up(L) - 1 downto 0)
      Mux(rem === 0, U(L, lastCnt.getWidth bits), rem.resize(lastCnt.getWidth))
    })
    io.m_axi <> mover.io.axi

    val busy, done, errRange, errAlign, errResp, errShape = RegInit(False)
    val cycles = Reg(UInt(32 bits)) init 0
    when(busy) { cycles := cycles + 1 }

    // gemv: M (MEM_WLEN) i K (GEMV_K); iloczyn M * K z rejestru (rejestry stoja przed startem)
    val wlenReg = withGemv generate (Reg(UInt(32 bits)) init 0)
    val kReg    = withGemv generate (Reg(UInt(log2Up(gemvMaxK + 1) bits)) init 0)
    val colsReg = withGemv generate (Reg(UInt(log2Up(gemvCols + 1) + 1 bits)) init 1)
    if (withGemv) {
      bus.readAndWrite(wlenReg, MEM_WLEN)
      bus.readAndWrite(kReg, GEMV_K)
      bus.readAndWrite(colsReg, GEMV_COLS)
    }
    // iloczyny z rejestrow (rejestry stoja kilka taktow przed startem): M * K, K * cols, M * cols
    val prodMK = withGemv generate RegNext(wlenReg * kReg)
    val prodKC = withGemv generate RegNext(kReg * colsReg)
    val prodMC = withGemv generate RegNext(wlenReg * colsReg)
    val wtotal = withGemv generate prodMC(31 downto 0)              // elementy y do zapisu
    val wlen = if (withGemv) Mux(isGemv, wtotal, len) else len
    val badShape = if (!withGemv) False else {
      val badK    = kReg === 0 || kReg > gemvMaxK
      val badCols = colsReg === 0 || colsReg > gemvCols
      (isGemv && (badK || badCols || prodMK =/= len.resize(prodMK.getWidth) || prodMC(prodMC.getWidth - 1 downto 32) =/= 0)) ||
      (isXload && (badK || badCols || prodKC =/= len.resize(prodKC.getWidth)))
    }

    // zakres: [addr, addr + slowa * 16) w [lo, hi), bez przekroczenia 2^32
    def bytesOf(n : UInt) : UInt = (mc.beats(n).resize(33) << log2Up(mc.beatBytes)).resize(33)
    def inRange(a : UInt, b : UInt) : Bool = {
      val end = a.resize(33) + b
      a >= lo && end <= hi.resize(33)
    }
    val bytes  = bytesOf(len)
    val wbytes = if (withGemv) bytesOf(wlen) else bytes
    def aligned(a : UInt) : Bool = a(log2Up(mc.beatBytes) - 1 downto 0) === 0
    val use = Vec(True, mcfg(MEM_CFG_READ_B), mcfg(MEM_CFG_READ_C))
    val wr  = mcfg(MEM_CFG_WRITE) && !isXload          // XLOAD nic nie zapisuje
    val badRange = (0 until 3).map(i => use(i) && !inRange(src(i), bytes)).reduce(_ || _) || (wr && !inRange(dst, wbytes))
    val badAlign = (0 until 3).map(i => use(i) && !aligned(src(i))).reduce(_ || _) || (wr && !aligned(dst))

    mover.io.cmd.valid         := False
    mover.io.cmd.payload.len   := len
    mover.io.cmd.payload.src   := src
    mover.io.cmd.payload.read  := use.asBits
    mover.io.cmd.payload.dst   := dst
    mover.io.cmd.payload.write := wr
    mover.io.cmd.payload.wlen  := wlen
    val accepted = memStart && !busy
    val launch   = Bool()
    launch := False
    when(accepted) {
      done     := False
      errRange := badRange
      errAlign := badAlign
      errResp  := False
      errShape := badShape
      cycles   := 0
      when(badRange || badAlign || badShape || len === 0) {
        done := True                                   // bez zadnej transakcji
      } otherwise {
        mover.io.cmd.valid := True
        launch := True
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
    inB     := Mux(needB, sb.fragment, B(0, L * c.n bits))
    inC     := Mux(needC, sc.fragment, B(0, L * c.n bits))
    inLast  := sa.last
    val take = inValid && inReady
    sa.ready := take
    sb.ready := take && needB
    sc.ready := take && needC
    mover.io.res.valid   := outValid && wr
    mover.io.res.payload := outBits
    outReady             := mover.io.res.ready || !wr      // bez zapisu (np. pomylka w MEM_CFG): wyniki odrzucane

    // ---- etap 7a: gemv ------------------------------------------------------------------------
    //  XLOAD: elementy a po kolei: kolumna xc = e / K do bufora xbuf(xc)[e mod K].
    //  GEMV:  element A[i, j] (po kolei, j = col) -> odczyt xbuf(0..P-1)[col] (1 takt) -> quire wiersza
    //         w kazdej aktywnej kolumnie (bank 0 dla parzystych i, bank 1 dla nieparzystych): a * x_c;
    //         ostatni element wiersza: round. Wynik quire -> jego kolejka -> scalanie wierszami
    //         (y[i, 0], y[i, 1], .. y[i, cols-1], y[i+1, 0] ..) -> grupy po L -> zapis M * cols elementow;
    //         quire zerowany w takcie wyniku (nowe elementy wchodza dopiero za 3 takty, do S4).
    //  Element wchodzi do banku tylko razem do wszystkich aktywnych kolumn, gdy kazda ma quire gotowy
    //  i miejsce w kolejce (zawsze miejsce na wynik).
    val gemv = withGemv generate new Area {
      val xbuf = Seq.fill(P)(Mem(Bits(c.n bits), gemvMaxK))
      val col  = Reg(UInt(log2Up(gemvMaxK) bits)) init 0
      val xc   = Reg(UInt(log2Up(P) max 1 bits)) init 0          // XLOAD: biezaca kolumna
      val sel  = Reg(Bool()) init False                          // bank biezacego wiersza (na wejsciu)
      val rowEnd = col.resize(kReg.getWidth) + 1 === kReg
      val active = (0 until P).map(j => colsReg > j)
      when(launch) { col := 0; xc := 0; sel := False }

      for (j <- 0 until P)
        xbuf(j).write(col, qA, enable = sFire && isXload && (if (P == 1) True else xc === j))

      // odczyt x: jeden stopien potoku z rejestrem
      val s1Valid  = RegInit(False)
      val s1A      = Reg(Bits(c.n bits))
      val s1End    = Reg(Bool())
      val s1Sel    = Reg(Bool())
      val s1Ready  = Bool()
      val advance  = !s1Valid || s1Ready
      val x        = xbuf.map(_.readSync(col, enable = advance))
      when(isXload) { sReady := True }
      when(isGemv)  { sReady := advance }
      when(advance) {
        s1Valid := inValid && isGemv
        s1A     := qA
        s1End   := rowEnd
        s1Sel   := sel
      }
      when(sFire) {
        col := Mux(rowEnd, U(0, col.getWidth bits), col + 1)
        when(rowEnd) {
          when(isGemv) { sel := !sel }
          when(isXload) { xc := xc + 1 }
        }
      }
      when(launch || softReset) { s1Valid := False }

      val fifo = Seq.tabulate(2, P)((_, _) => StreamFifo(Bits(c.n bits), 4))
      val room = fifo.map(_.map(_.io.availability > 1))         // > 1: wynik z tego taktu moze jeszcze wchodzic
      val bankOk = (0 until 2).map(b => (0 until P).map(j => !active(j) || (gq(b, j).io.input.ready && room(b)(j))).reduce(_ && _))
      s1Ready := Mux(s1Sel, bankOk(1), bankOk(0))
      for (b <- 0 until 2; j <- 0 until P) {
        val q    = gq(b, j)
        val mine = if (b == 0) !s1Sel else s1Sel
        val take = s1Valid && mine && bankOk(b) && active(j)
        when(isGemv) {
          q.io.input.valid := take
          q.io.input.a     := s1A
          q.io.input.b     := x(j)
          q.io.input.dot   := True
          when(take && s1End) { q.io.round := True }
          when(q.io.result.valid) { q.io.clear := True }
        }
        when(launch && isGemv) { q.io.clear := True }
        fifo(b)(j).io.push.valid   := q.io.result.valid && isGemv
        fifo(b)(j).io.push.payload := q.io.result.bits
        fifo(b)(j).io.flush        := launch || softReset
      }

      // scalanie: wiersz po wierszu (bank na zmiane), w wierszu kolumny po kolei
      val outSel = Reg(Bool()) init False
      val outCol = Reg(UInt(log2Up(P) max 1 bits)) init 0
      when(launch) { outSel := False; outCol := 0 }
      val pops   = (0 until 2).flatMap(b => (0 until P).map(j => (b, j)))
      val merged = Stream(Bits(c.n bits))
      def at(b : Int, j : Int) : Bool = (if (b == 0) !outSel else outSel) && (if (P == 1) True else outCol === j)
      merged.valid   := pops.map { case (b, j) => at(b, j) && fifo(b)(j).io.pop.valid }.reduce(_ || _)
      merged.payload := MuxOH(pops.map { case (b, j) => at(b, j) }, pops.map { case (b, j) => fifo(b)(j).io.pop.payload })
      for ((b, j) <- pops) fifo(b)(j).io.pop.ready := merged.ready && at(b, j)
      val lastCol = outCol.resize(colsReg.getWidth) + 1 === colsReg
      when(merged.fire) {
        outCol := Mux(lastCol, U(0, outCol.getWidth bits), outCol + 1)
        when(lastCol) { outSel := !outSel }
      }

      // grupy po L wynikow (ostatnia niepelna: zapisuje ja writer z lastCnt z M * cols)
      val packed = Stream(Bits(L * c.n bits))
      if (L == 1) packed << merged else {
        val buf     = Vec.fill(L)(Reg(Bits(c.n bits)) init 0)   // init: tory za koncem ostatniej grupy bez X na W
        val idx     = Reg(UInt(log2Up(L) bits)) init 0
        val left    = Reg(UInt(32 bits)) init 0        // wyniki jeszcze do zebrania
        val full    = RegInit(False)
        when(launch) { idx := 0; left := wtotal; full := False }
        merged.ready := !full
        when(merged.fire) {
          buf(idx) := merged.payload
          idx  := idx + 1
          left := left - 1
          when(idx === L - 1 || left === 1) { full := True; idx := 0 }
        }
        packed.valid   := full
        packed.payload := Cat(buf)
        when(packed.fire) { full := False }
      }
      packed.ready := mover.io.res.ready || !wr
      when(isGemv) {
        mover.io.res.valid   := packed.valid && wr
        mover.io.res.payload := packed.payload
      }
    }

    bus.read(errShape ## errResp ## errAlign ## errRange ## done ## busy, MEM_STATUS)
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
  when(busy || anyInFlight || quire.io.busy || gemvExtra.map(_.io.busy).foldLeft(False)(_ || _) ||
       (if (withMaster) mem.busy else False)) { cycles := cycles + 1 }

  // Trzy zrodla wynikow (rejestry, strumien, quire) moga trafic na ten sam takt:
  // liczniki dodaja sume przyrostow, nie nadpisuja sie nawzajem.
  // Zrodla wynikow (rejestry, L torow strumienia, quire) moga trafic na ten sam takt:
  // liczniki dodaja sume przyrostow, nie nadpisuja sie nawzajem. Tory za koncem tablicy
  // (ostatnia, niepelna grupa) nie licza sie.
  val sources = Seq((engine.io.regRsp.fire, engine.io.regRsp.flags)) ++
    engines.zipWithIndex.map { case (e, j) => (outFire && (if (j == 0) True else outCnt > j), e.io.output.fragment.flags) } ++
    Seq((quire.io.result.valid, quire.io.result.flags)) ++
    gemvExtra.map(q => (q.io.result.valid, q.io.result.flags))
  val incW = log2Up(L + 2 * P + 4) + 1
  def inc(pick : PositFlags => Bool) : UInt =
    sources.map { case (v, f) => (v && pick(f)).asUInt.resize(incW) }.reduce(_ + _)
  cntNar      := cntNar + inc(_.nar)
  cntSat      := cntSat + inc(_.sat)
  cntInexact  := cntInexact + inc(_.inexact)
  cntElements := cntElements + Mux(outFire, outCnt, U(0, lastCnt.getWidth bits)).resize(incW) +
                 quire.io.input.fire.asUInt.resize(incW) +
                 (if (withGemv) gq(1, 0).io.input.fire.asUInt.resize(incW) else U(0, incW bits))   // elementy A (kolumna 0)

  when(engine.io.regRsp.fire) {
    result := engine.io.regRsp.bits
    flags  := engine.io.regRsp.flags.asBits
    busy   := False
    done   := True
  }
  when(quireRound) { done := False }
  when(quire.io.result.valid && !isGemv) {
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
  bus.read(B(config(cfg, withMaster, withGemv), 32 bits), CONFIG)
  bus.read(B(if (withGemv) (gemvCols.toLong << 16) | gemvMaxK else 0L, 32 bits), GEMV_CFG)
  bus.read(flags ## B(0, 2 bits) ## quire.io.nar ## quire.io.busy ## anyInFlight ## error ## done ## busy, STATUS)
  bus.read(result, RESULT)
  bus.read(cntNar, CNT_NAR)
  bus.read(cntSat, CNT_SAT)
  bus.read(cntInexact, CNT_INEXACT)
  bus.read(cycles, CYCLES)
  bus.read(cntElements, CNT_ELEMENTS)
  bus.read(B(quireCfg(cfg), 32 bits), QUIRE_CFG)
  bus.read(B(engineCfg(cfg, L), 32 bits), ENGINE_CFG)
  for (k <- 0 until quire.qc.width / 32) bus.read(quire.io.raw(32 * k + 31 downto 32 * k), QUIRE + 4 * k)
}
