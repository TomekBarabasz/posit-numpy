package posit.kv260

import spinal.core._
import spinal.lib._
import posit._

// =====================================================================
//  PositFftCore (5.5): sprzetowe FFT radix-4 na posit, N = 4^S (16 .. 4^maxS),
//  jeden motylek na takt, dane w BRAM silnika.
//
//  Partia B transformat jednym poleceniem MEM: na transformate 2N elementow
//  (N czesci rzeczywistych, potem N urojonych), wynik w tym samym ukladzie.
//    LOAD     grupy po L (L = 1, 2, 4) -> pozycja rev4(n) (odwrocone cyfry w bazie 4)
//    COMP     S etapow DIT po N / 4 motylkow; miedzy etapami oproznienie potoku
//    STORE    pozycje k = 0 .. N - 1 po kolei (porzadek naturalny), grupy po L
//  Banki: pozycja i -> bank (suma cyfr i w bazie 4) mod 4, adres i >> 2. Cztery
//  wejscia motylka roznia sie jedna cyfra, cztery kolejne pozycje - cyfra 0:
//  zawsze cztery rozne banki (odczyt port A, zapis port B, bez konfliktow).
//  Twiddle: T[e] = W_N^e, e < 3N/4, w 3 kopiach (w1, w2, w3 na takt), ladowane
//  po jednym elemencie (3N/4 rzeczywistych, potem 3N/4 urojonych).
//  Motylek klasyczny, kazde dzialanie zaokraglone (PositOps, potok jak
//  PositPipeline.full: 3 takty na dzialanie), dokladnie jak positnp.fft.fft_core_model:
//    P_p = w_p y_p (re = wr yr - wi yi, im = wr yi + wi yr), s0 = P0 + P2, d0 = P0 - P2,
//    s1 = P1 + P3, d1 = P1 - P3; X0 = s0 + s1, X2 = s0 - s1, X1 = d0 - i d1, X3 = d0 + i d1.
// =====================================================================
case class PositFftConfig(c : PositConfig, maxS : Int, lanes : Int) {
  require(maxS >= 2 && maxS <= 7, s"maxS $maxS: N = 16 .. 16384")
  require(Seq(1, 2, 4).contains(lanes), s"FFT: L = 1, 2 albo 4 (jest $lanes)")
  val maxN  : Int = 1 << (2 * maxS)
  val depth : Int = maxN / 4                           // slowa banku
  val twN   : Int = 3 * maxN / 4                       // pozycje tablicy twiddli
}

class PositFftCore(val fc : PositFftConfig) extends Component {
  import fc._
  val n = c.n
  val L = lanes
  val io = new Bundle {
    val s        = in  UInt(3 bits)                    // N = 4^s, 2 <= s <= maxS
    val batches  = in  UInt(32 bits)
    val startFft = in  Bool()                          // impuls: partia z io.in -> io.out
    val startTw  = in  Bool()                          // impuls: tablica twiddli z io.tw
    val din      = slave(Stream(Bits(L * n bits)))
    val tw       = slave(Flow(Bits(n bits)))
    val dout     = master(Stream(Bits(L * n bits)))
    val busy     = out Bool()
  }

  // ---- pamieci ----------------------------------------------------------------------------------------
  val bankRe = Seq.fill(4)(Mem(Bits(n bits), depth))
  val bankIm = Seq.fill(4)(Mem(Bits(n bits), depth))
  val twRe   = (1 to 3).map(p => Mem(Bits(n bits), p * maxN / 4))   // kopia p: wykladniki p * e1 < p N / 4
  val twIm   = (1 to 3).map(p => Mem(Bits(n bits), p * maxN / 4))

  // ---- pomocnicze: pozycje w bazie 4 ---------------------------------------------------------------
  val posW = 2 * maxS
  def digits(x : UInt) : Seq[UInt] = (0 until maxS).map(i => x(2 * i + 1 downto 2 * i))
  def bankOf(x : UInt) : UInt = digits(x).reduce(_ + _)                    // 2 bity: suma mod 4
  def rev4(x : UInt, s : UInt) : UInt = {                                   // odwrocenie s cyfr (s z io.s)
    val opts = (0 to maxS).map { ss =>
      if (ss == 0) U(0, posW bits)
      else Cat((0 until ss).map(j => x(2 * (ss - 1 - j) + 1 downto 2 * (ss - 1 - j)))).asUInt.resize(posW)
    }
    Vec(opts)(s.resize(log2Up(maxS + 1)))
  }
  val sReg   = Reg(UInt(3 bits)) init 2
  val nMask  = Vec((0 to maxS).map(ss => U((BigInt(1) << (2 * ss)) - 1, posW bits)))(sReg.resize(log2Up(maxS + 1)))
  val nQuart = (nMask >> 2)                                                  // N / 4 - 1

  // ---- automat ---------------------------------------------------------------------------------------
  object St extends SpinalEnum { val IDLE, LOAD, COMP, DRAIN, STORE, TWL = newElement() }
  val state    = RegInit(St.IDLE)
  val bLeft    = Reg(UInt(32 bits)) init 0
  val eIdx     = Reg(UInt(posW + 2 bits)) init 0          // LOAD: element 0 .. 2N - 1 (2N = 2^(posW + 1) dla N = maxN)
  val stage    = Reg(UInt(3 bits)) init 0
  val bfly     = Reg(UInt(posW bits)) init 0
  val drainCnt = Reg(UInt(5 bits)) init 0
  val twIdx    = Reg(UInt(posW + 2 bits)) init 0
  io.busy := state =/= St.IDLE

  when(io.startTw) { state := St.TWL; twIdx := 0; sReg := io.s }
  when(io.startFft) {
    sReg  := io.s
    bLeft := io.batches
    eIdx  := 0
    state := Mux(io.batches === 0, St.IDLE, St.LOAD)
  }

  // ---- ladowanie twiddli: e < 3N/4 rzeczywiste, potem urojone; kopia p bierze e < p N / 4 -------------
  val twHalf = ((nMask.resize(posW + 2) + 1) * 3 >> 2).resize(posW + 1)        // 3N / 4
  val twIsIm = twIdx >= twHalf
  val twE    = Mux(twIsIm, twIdx - twHalf, twIdx)
  for (p <- 1 to 3) {
    val lim = ((nMask.resize(posW + 2) + 1) * p >> 2).resize(posW + 1)
    val en  = state === St.TWL && io.tw.valid && twE < lim
    twRe(p - 1).write(twE.resize(log2Up(p * maxN / 4)), io.tw.payload, enable = en && !twIsIm)
    twIm(p - 1).write(twE.resize(log2Up(p * maxN / 4)), io.tw.payload, enable = en && twIsIm)
  }
  when(state === St.TWL && io.tw.valid) {
    twIdx := twIdx + 1
    when(twIdx + 1 === (twHalf << 1)) { state := St.IDLE }
  }

  // ---- LOAD: grupa L elementow -> L roznych bankow ------------------------------------------------------
  io.din.ready := state === St.LOAD
  val nTot  = (nMask.resize(posW + 1) + 1)                                   // N
  val ldIm  = eIdx >= nTot
  val ldN   = (Mux(ldIm, eIdx - nTot, eIdx)).resize(posW)
  val ldPos = (0 until L).map(j => rev4((ldN + j).resize(posW), sReg))
  val ldBank = ldPos.map(bankOf)
  val wrAddr = Vec.fill(4)(UInt(log2Up(depth) bits))
  val wrRe, wrIm = Vec.fill(4)(Bits(n bits))
  val wrEnRe, wrEnIm = Vec.fill(4)(Bool())
  for (k <- 0 until 4) { wrAddr(k) := 0; wrRe(k) := 0; wrIm(k) := 0; wrEnRe(k) := False; wrEnIm(k) := False }
  when(io.din.fire) {
    for (k <- 0 until 4; j <- 0 until L) when(ldBank(j) === k) {
      wrAddr(k) := (ldPos(j) >> 2).resize(log2Up(depth))
      wrRe(k)   := io.din.payload(j * n, n bits)
      wrIm(k)   := io.din.payload(j * n, n bits)
      wrEnRe(k) := !ldIm
      wrEnIm(k) := ldIm
    }
    eIdx := eIdx + L
    when(eIdx + L === (nTot << 1)) { state := St.COMP; stage := 0; bfly := 0 }
  }

  // ---- COMP: wydanie motylka --------------------------------------------------------------------------
  val issue = state === St.COMP
  val sOpts = (0 until maxS)
  def sel[T <: Data](f : Int => T) : T = Vec(sOpts.map(f))(stage.resize(log2Up(maxS) max 1))
  // i0 = cyfra 0 wstawiona na pozycji `stage` w numer motylka; k = mlodsze `stage` cyfr
  val i0 = sel(ss => {
    val lowBits = 2 * ss
    val hi = (bfly >> lowBits) << (lowBits + 2)
    val lo = if (lowBits == 0) U(0, posW bits) else (bfly & U((BigInt(1) << lowBits) - 1, posW bits))
    (hi.resize(posW) | lo.resize(posW))
  })
  val kLow = sel(ss => if (ss == 0) U(0, posW bits) else (bfly & U((BigInt(1) << (2 * ss)) - 1, posW bits)))
  // e1 = k * 4^(S - 1 - stage)
  val twShift = ((sReg - 1 - stage) << 1).resize(4)
  val e1 = (kLow << twShift).resize(posW + 1)
  val ds0 = bankOf(i0)
  // adres banku k: pozycja i_p z p = k - ds0; i_p >> 2 = (i0 >> 2) | (p << (2 stage - 2))
  val rdAddr = Vec((0 until 4).map { k =>
    val p = (U(k, 2 bits) - ds0)
    sel(ss => if (ss == 0) (i0 >> 2).resize(log2Up(depth))
              else ((i0 >> 2).resize(posW) | (p.resize(posW) << (2 * ss - 2)).resize(posW)).resize(log2Up(depth)))
  })

  // ---- STORE: odczyt w porzadku naturalnym -------------------------------------------------------------
  val stK      = Reg(UInt(posW + 2 bits)) init 0          // element 0 .. 2N - 1 (re, potem im)
  val stIm     = stK >= nTot
  val stN      = Mux(stIm, stK - nTot, stK).resize(posW)
  val outFull  = RegInit(False)
  val outBuf   = Vec.fill(L)(Reg(Bits(n bits)) init 0)
  val outFire  = outFull && io.dout.ready
  val stAdv    = !outFull || outFire
  val stIssue  = state === St.STORE && stAdv && stK < (nTot << 1)
  val stAddr   = (stN >> 2).resize(log2Up(depth))

  // ---- porty pamieci: A odczyt (COMP / STORE), B zapis (LOAD / COMP) -------------------------------
  val rdEn   = issue || (state === St.STORE && stAdv)      // STORE: odczyt stoi razem z rejestrem wyjsciowym
  val addrA  = Vec((0 until 4).map(k => Mux(state === St.STORE, stAddr, rdAddr(k))))
  val qRe    = (0 until 4).map(k => bankRe(k).readSync(addrA(k), enable = rdEn))
  val qIm    = (0 until 4).map(k => bankIm(k).readSync(addrA(k), enable = rdEn))
  val w      = (1 to 3).map(p => {
    val ep = (e1 * p).resize(log2Up(p * maxN / 4))
    (twRe(p - 1).readSync(ep, enable = issue), twIm(p - 1).readSync(ep, enable = issue))
  })

  // ---- motylek: potok, 3 takty na dzialanie --------------------------------------------------------------
  def dec(a : Bits) = PositDecode(c, a)
  def op3(a : Bits, b : Bits, f : (PositDecoded, PositDecoded) => PositUnrounded) : Bits = {
    val da = RegNext(dec(a)); val db = RegNext(dec(b))
    val u  = RegNext(f(da, db))
    RegNext(PositEncode(c, u))
  }
  def mul(a : Bits, b : Bits) = op3(a, b, PositOps.mul)
  def add(a : Bits, b : Bits) = op3(a, b, PositOps.add)
  def sub(a : Bits, b : Bits) = op3(a, b, PositOps.sub)
  def dly(x : Bits, k : Int) : Bits = Delay(x, k)

  val v1   = RegNext(issue) init False                  // dane z pamieci w tym takcie
  val ds1  = RegNext(ds0)
  val i01  = RegNext(i0)
  val st1  = RegNext(stage)
  def yIn(p : Int) : (Bits, Bits) = {                   // wejscie p z banku (ds0 + p) mod 4
    val b = (ds1 + p).resize(2)
    (Vec(qRe)(b), Vec(qIm)(b))
  }
  val y  = (0 until 4).map(yIn)
  val P  = Seq((dly(y(0)._1, 6), dly(y(0)._2, 6))) ++ (1 to 3).map { p =>
    val (yr, yi) = y(p); val (wr, wi) = w(p - 1)
    (sub(mul(wr, yr), mul(wi, yi)), add(mul(wr, yi), mul(wi, yr)))
  }
  val (s0r, s0i) = (add(P(0)._1, P(2)._1), add(P(0)._2, P(2)._2))
  val (d0r, d0i) = (sub(P(0)._1, P(2)._1), sub(P(0)._2, P(2)._2))
  val (s1r, s1i) = (add(P(1)._1, P(3)._1), add(P(1)._2, P(3)._2))
  val (d1r, d1i) = (sub(P(1)._1, P(3)._1), sub(P(1)._2, P(3)._2))
  val X = Seq((add(s0r, s1r), add(s0i, s1i)), (add(d0r, d1i), sub(d0i, d1r)),
              (sub(s0r, s1r), sub(s0i, s1i)), (sub(d0r, d1i), add(d0i, d1r)))
  val LB  = 12
  val vX  = Delay(v1, LB, init = False)
  val dsX = Delay(ds1, LB)
  val iX  = Delay(i01, LB)
  val stX = Delay(st1, LB)
  when(vX) {
    for (q <- 0 until 4) {
      val b = (dsX + q).resize(2)
      val addr = Vec((0 until maxS).map(ss =>
        if (ss == 0) (iX >> 2).resize(log2Up(depth))
        else ((iX >> 2).resize(posW) | (U(q, posW bits) << (2 * ss - 2)).resize(posW)).resize(log2Up(depth))
      ))(stX.resize(log2Up(maxS) max 1))
      for (k <- 0 until 4) when(b === k) {
        wrAddr(k) := addr; wrRe(k) := X(q)._1; wrIm(k) := X(q)._2; wrEnRe(k) := True; wrEnIm(k) := True
      }
    }
  }
  for (k <- 0 until 4) {
    bankRe(k).write(wrAddr(k), wrRe(k), enable = wrEnRe(k))
    bankIm(k).write(wrAddr(k), wrIm(k), enable = wrEnIm(k))
  }

  when(issue) {
    bfly := bfly + 1
    when(bfly === nQuart) { state := St.DRAIN; drainCnt := 0 }
  }
  when(state === St.DRAIN) {
    drainCnt := drainCnt + 1
    when(drainCnt === LB + 2) {                         // ostatni zapis etapu wykonany
      bfly := 0
      when(stage === sReg - 1) { state := St.STORE; stK := 0 } otherwise { stage := stage + 1; state := St.COMP }
    }
  }

  // ---- STORE: dane po 1 takcie -> grupa wyjsciowa ---------------------------------------------------------
  val sv1  = RegInit(False)
  val sIm1 = Reg(Bool()) init False
  val sN1  = Reg(UInt(posW bits)) init 0
  when(stAdv) { sv1 := stIssue; sIm1 := stIm; sN1 := stN }
  when(stIssue) { stK := stK + L }
  when(outFire) { outFull := False }
  when(sv1 && stAdv) {
    for (j <- 0 until L) {
      val b = bankOf((sN1 + j).resize(posW))
      outBuf(j) := Mux(sIm1, Vec(qIm)(b), Vec(qRe)(b))
    }
    outFull := True
  }
  io.dout.valid   := outFull
  io.dout.payload := Cat(outBuf)
  // koniec transformaty: wszystko wyslane
  when(state === St.STORE && stK === (nTot << 1) && !sv1 && !outFull) {
    bLeft := bLeft - 1
    eIdx  := 0
    state := Mux(bLeft === 1, St.IDLE, St.LOAD)
  }

  setDefinitionName(s"PositFftCore_${c.n}_${c.es}")
}
