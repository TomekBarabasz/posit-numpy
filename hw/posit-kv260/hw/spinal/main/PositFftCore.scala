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
//    wczytanie  grupy po L (L = 1, 2, 4) -> pozycja rev4(n) (odwrocone cyfry w bazie 4)
//    liczenie   S etapow DIT po N / 4 motylkow; miedzy etapami oproznienie potoku
//    zapis      pozycje k = 0 .. N - 1 po kolei (porzadek naturalny), grupy po L
//  Trzy zestawy bankow, fazy naraz (5.6): w slocie t wczytanie transformaty t, liczenie t - 1 i zapis t - 2,
//  kazda w swoim zestawie; po slocie role sie obracaja. Slot = max(wczytanie, liczenie, zapis).
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
    val conj     = in  Bool()                          // czesc urojona negowana przy wczytaniu i zapisie: conj(fft(conj x))
    val din      = slave(Stream(Bits(L * n bits)))
    val tw       = slave(Flow(Bits(n bits)))
    val dout     = master(Stream(Bits(L * n bits)))
    val busy     = out Bool()
  }

  // ---- pamieci: 3 zestawy bankow (wczytanie / liczenie / zapis naraz, role obracane co transformate) -------
  val SETS   = 3
  val bankRe = Seq.fill(SETS)(Seq.fill(4)(Mem(Bits(n bits), depth)))
  val bankIm = Seq.fill(SETS)(Seq.fill(4)(Mem(Bits(n bits), depth)))
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

  // ---- automat: sloty; w slocie t wczytanie transformaty t, liczenie t - 1, zapis t - 2 -------------------
  val running  = RegInit(False)
  val twl      = RegInit(False)
  val batches  = Reg(UInt(32 bits)) init 0
  val slot     = Reg(UInt(33 bits)) init 0
  val ldSet    = Reg(UInt(2 bits)) init 0
  val cpSet    = Reg(UInt(2 bits)) init 1
  val stSet    = Reg(UInt(2 bits)) init 2
  val ldAct    = slot < batches.resize(33)
  val cpAct    = slot >= 1 && slot <= batches.resize(33)
  val stAct    = slot >= 2 && slot <= batches.resize(33) + 1
  val ldDone, cpDone, stDone = RegInit(False)
  val eIdx     = Reg(UInt(posW + 2 bits)) init 0          // wczytanie: element 0 .. 2N - 1
  val stage    = Reg(UInt(3 bits)) init 0
  val bfly     = Reg(UInt(posW bits)) init 0
  val draining = RegInit(False)
  val drainCnt = Reg(UInt(5 bits)) init 0
  val stK      = Reg(UInt(posW + 2 bits)) init 0          // zapis: element 0 .. 2N - 1 (re, potem im)
  val twIdx    = Reg(UInt(posW + 2 bits)) init 0
  io.busy := running || twl

  val newSlot = Bool()
  newSlot := False
  when(io.startTw) { twl := True; twIdx := 0; sReg := io.s }
  when(io.startFft) {
    sReg    := io.s
    batches := io.batches
    slot    := 0
    ldSet := 0; cpSet := 1; stSet := 2
    running := io.batches =/= 0
    newSlot := True
  }

  // ---- ladowanie twiddli: e < 3N/4 rzeczywiste, potem urojone; kopia p bierze e < p N / 4 -------------
  val twHalf = ((nMask.resize(posW + 2) + 1) * 3 >> 2).resize(posW + 1)        // 3N / 4
  val twIsIm = twIdx >= twHalf
  val twE    = Mux(twIsIm, twIdx - twHalf, twIdx)
  for (p <- 1 to 3) {
    val lim = ((nMask.resize(posW + 2) + 1) * p >> 2).resize(posW + 1)
    val en  = twl && io.tw.valid && twE < lim
    twRe(p - 1).write(twE.resize(log2Up(p * maxN / 4)), io.tw.payload, enable = en && !twIsIm)
    twIm(p - 1).write(twE.resize(log2Up(p * maxN / 4)), io.tw.payload, enable = en && twIsIm)
  }
  when(twl && io.tw.valid) {
    twIdx := twIdx + 1
    when(twIdx + 1 === (twHalf << 1)) { twl := False }
  }

  // ---- wczytanie: grupa L elementow -> L roznych bankow zestawu ldSet -----------------------------------
  val nTot   = (nMask.resize(posW + 1) + 1)                                  // N
  io.din.ready := running && ldAct && !ldDone
  val ldIm   = eIdx >= nTot
  val ldN    = (Mux(ldIm, eIdx - nTot, eIdx)).resize(posW)
  val ldPos  = (0 until L).map(j => rev4((ldN + j).resize(posW), sReg))
  val ldBank = ldPos.map(bankOf)
  val ldAddr = Vec.fill(4)(UInt(log2Up(depth) bits))
  val ldData = Vec.fill(4)(Bits(n bits))
  val ldEn   = Vec.fill(4)(Bool())
  for (k <- 0 until 4) { ldAddr(k) := 0; ldData(k) := 0; ldEn(k) := False }
  when(io.din.fire) {
    for (k <- 0 until 4; j <- 0 until L) when(ldBank(j) === k) {
      ldAddr(k) := (ldPos(j) >> 2).resize(log2Up(depth))
      val v = io.din.payload(j * n, n bits)
      ldData(k) := Mux(io.conj && ldIm, (-v.asSInt).asBits, v)
      ldEn(k)   := True
    }
    eIdx := eIdx + L
    when(eIdx + L === (nTot << 1)) { ldDone := True }
  }

  // ---- liczenie: wydanie motylka (zestaw cpSet) ---------------------------------------------------------
  val issue = running && cpAct && !cpDone && !draining
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
  val twShift = ((sReg - 1 - stage) << 1).resize(4)                      // e1 = k * 4^(S - 1 - stage)
  val e1 = (kLow << twShift).resize(posW + 1)
  val ds0 = bankOf(i0)
  // adres banku k: pozycja i_p z p = k - ds0; i_p >> 2 = (i0 >> 2) | (p << (2 stage - 2))
  val rdAddr = Vec((0 until 4).map { k =>
    val p = (U(k, 2 bits) - ds0)
    sel(ss => if (ss == 0) (i0 >> 2).resize(log2Up(depth))
              else ((i0 >> 2).resize(posW) | (p.resize(posW) << (2 * ss - 2)).resize(posW)).resize(log2Up(depth)))
  })

  // ---- zapis: odczyt w porzadku naturalnym (zestaw stSet) ----------------------------------------------
  val stIm     = stK >= nTot
  val stN      = Mux(stIm, stK - nTot, stK).resize(posW)
  val outFull  = RegInit(False)
  val outBuf   = Vec.fill(L)(Reg(Bits(n bits)) init 0)
  val outFire  = outFull && io.dout.ready
  val stAdv    = !outFull || outFire
  val stRun    = running && stAct && !stDone
  val stIssue  = stRun && stAdv && stK < (nTot << 1)
  val stAddr   = (stN >> 2).resize(log2Up(depth))

  // ---- porty pamieci: A odczyt (liczenie albo zapis), B zapis (wczytanie albo liczenie) -----------------
  val qRe = (0 until SETS).map(ss => (0 until 4).map(k =>
    bankRe(ss)(k).readSync(Mux(stSet === ss, stAddr, rdAddr(k)),
                           enable = Mux(stSet === ss, stRun && stAdv, cpSet === ss && issue))))
  val qIm = (0 until SETS).map(ss => (0 until 4).map(k =>
    bankIm(ss)(k).readSync(Mux(stSet === ss, stAddr, rdAddr(k)),
                           enable = Mux(stSet === ss, stRun && stAdv, cpSet === ss && issue))))
  val w = (1 to 3).map(p => {
    val ep = (e1 * p).resize(log2Up(p * maxN / 4))
    (twRe(p - 1).readSync(ep, enable = issue), twIm(p - 1).readSync(ep, enable = issue))
  })
  def bankOut(q : Seq[Seq[Bits]], set : UInt, b : UInt) : Bits = Vec(q.map(row => Vec(row)(b)))(set)

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
  val y  = (0 until 4).map { p =>                        // wejscie p z banku (ds0 + p) mod 4 zestawu cpSet
    val b = (ds1 + p).resize(2)
    (bankOut(qRe, cpSet, b), bankOut(qIm, cpSet, b))
  }
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
  val cpAddr = Vec.fill(4)(UInt(log2Up(depth) bits))
  val cpRe, cpIm = Vec.fill(4)(Bits(n bits))
  val cpEn = Vec.fill(4)(Bool())
  for (k <- 0 until 4) { cpAddr(k) := 0; cpRe(k) := 0; cpIm(k) := 0; cpEn(k) := False }
  when(vX) {
    for (q <- 0 until 4) {
      val b = (dsX + q).resize(2)
      val addr = Vec((0 until maxS).map(ss =>
        if (ss == 0) (iX >> 2).resize(log2Up(depth))
        else ((iX >> 2).resize(posW) | (U(q, posW bits) << (2 * ss - 2)).resize(posW)).resize(log2Up(depth))
      ))(stX.resize(log2Up(maxS) max 1))
      for (k <- 0 until 4) when(b === k) { cpAddr(k) := addr; cpRe(k) := X(q)._1; cpIm(k) := X(q)._2; cpEn(k) := True }
    }
  }
  // port B zestawu: wczytanie (ldSet) albo wyniki motylkow (cpSet); czesc re / im wczytania wg ldIm
  for (ss <- 0 until SETS; k <- 0 until 4) {
    val isLd = ldSet === ss
    val addr = Mux(isLd, ldAddr(k), cpAddr(k))
    bankRe(ss)(k).write(addr, Mux(isLd, ldData(k), cpRe(k)), enable = Mux(isLd, ldEn(k) && !ldIm, cpSet === ss && cpEn(k)))
    bankIm(ss)(k).write(addr, Mux(isLd, ldData(k), cpIm(k)), enable = Mux(isLd, ldEn(k) && ldIm, cpSet === ss && cpEn(k)))
  }

  when(issue) {
    bfly := bfly + 1
    when(bfly === nQuart) { draining := True; drainCnt := 0 }
  }
  when(draining) {
    drainCnt := drainCnt + 1
    when(drainCnt === LB + 2) {                         // ostatni zapis etapu wykonany
      draining := False
      bfly := 0
      when(stage === sReg - 1) { cpDone := True } otherwise { stage := stage + 1 }
    }
  }

  // ---- zapis: dane po 1 takcie -> grupa wyjsciowa ---------------------------------------------------------
  val sv1  = RegInit(False)
  val sIm1 = Reg(Bool()) init False
  val sN1  = Reg(UInt(posW bits)) init 0
  when(stAdv) { sv1 := stIssue; sIm1 := stIm; sN1 := stN }
  when(stIssue) { stK := stK + L }
  when(outFire) { outFull := False }
  when(sv1 && stAdv) {
    for (j <- 0 until L) {
      val b = bankOf((sN1 + j).resize(posW))
      val vi = bankOut(qIm, stSet, b)
      outBuf(j) := Mux(sIm1, Mux(io.conj, (-vi.asSInt).asBits, vi), bankOut(qRe, stSet, b))
    }
    outFull := True
  }
  io.dout.valid   := outFull
  io.dout.payload := Cat(outBuf)
  when(stRun && stK === (nTot << 1) && !sv1 && !outFull) { stDone := True }

  // ---- koniec slotu: wszystkie czynne fazy gotowe -> obrot rol -----------------------------------------------
  val slotEnd = running && (ldDone || !ldAct) && (cpDone || !cpAct) && (stDone || !stAct) && !io.startFft
  when(slotEnd) {
    slot  := slot + 1
    ldSet := stSet; cpSet := ldSet; stSet := cpSet
    newSlot := True
    when(slot + 1 === batches.resize(33) + 2) { running := False }
  }
  when(newSlot) {
    ldDone := False; cpDone := False; stDone := False
    eIdx := 0; stage := 0; bfly := 0; draining := False; stK := 0
  }

  setDefinitionName(s"PositFftCore_${c.n}_${c.es}")
}
