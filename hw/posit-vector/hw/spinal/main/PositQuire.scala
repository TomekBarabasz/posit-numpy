package posit.vector

import spinal.core._
import spinal.lib._
import spinal.lib.fsm._
import posit._

// =====================================================================
//  PositQuire: dokladny akumulator sum i iloczynow skalarnych (plan, etap 4).
//
//  Quire to liczba stalopozycyjna w U2: LSB = minpos^2 = 2^-quireFrac,
//  szerokosc quireWidth (16 n dla es = 2: 512 b dla posit32). Kazdy iloczyn
//  dwoch positow (i kazdy posit) jest w niej dokladna liczba calkowita,
//  wiec suma jest dokladna, a zaokraglenie jest jedno, przy odczycie.
//
//  Akumulacja, 1 element / takt, bez backpressure (input.ready = 1 poza odczytem):
//    S1 dekoder a, b (sum: b = 1)  ->  S2 iloczyn znaczacych (DSP), suma skal
//    ->  S3 wyrownanie: iloczyn przesuniety na swoje miejsce w quire
//    ->  S4 akumulator: segmenty po segBits z rejestrami przeniesien.
//  Przeniesienie z segmentu i wchodzi do segmentu i+1 w nastepnym takcie,
//  wiec sumator ma segBits, a nie 512 bitow. Wartosc quire to segmenty plus
//  zalegle przeniesienia; ustala sie sama (dodawanie zera), gdy nic nie
//  wchodzi: najwyzej segs - 1 taktow po ostatnim elemencie.
//  Odejmowanie: segmenty + ~wyrownany + 1 (wejscie przeniesienia segmentu 0).
//
//  Odczyt (io.round), automat:
//    WAIT  potok pusty i przeniesienia zero  -> raw := quire
//    NEG   modul: ~quire + 1 tym samym trybem segmentowym (dla ujemnego)
//    FIND  najwyzszy niezerowy segment
//    CUT   wiodaca jedynka (LZC segmentu), ulamek + sticky z okna 2 segmentow
//    ENC   PositEncode.flagged -> io.result (wynik i flagi)
//  Quire zostaje nietkniety: kolejne elementy dodaja sie dalej (tablice
//  dluzsze niz jeden transfer DMA), zeruje go io.clear.
//  NaR: flaga lepka, wynik NaR do clear.
// =====================================================================
case class PositQuireConfig(c : PositConfig, segBits : Int = 64) {
  val width : Int = c.quireWidth(segBits)
  val segs  : Int = width / segBits
  val F     : Int = c.fracBits
  /** Bity ulamka przekazywane do enkodera (reszta trafia do sticky). */
  val fracW : Int = F + 4
  require(segBits >= fracW + 1, s"segBits $segBits: okno 2 segmentow musi miescic $fracW bitow ulamka")
  require(isPow2(segBits), s"segBits $segBits: potega dwojki")
  require(segs >= 2)
  override def toString = s"$c quire $width b = $segs x $segBits"
}

case class PositQuireIn(c : PositConfig) extends Bundle {
  val a, b = Bits(c.n bits)
  val dot  = Bool()        // 1: a * b, 0: a
}

class PositQuire(val qc : PositQuireConfig) extends Component {
  import qc._
  val c = qc.c
  val io = new Bundle {
    val input  = slave(Stream(PositQuireIn(c)))
    val clear  = in Bool()                       // impuls: quire := 0, NaR := 0
    val round  = in Bool()                       // impuls: zaokraglij biezacy stan
    val result = master(Flow(PositAluRsp(c)))
    val busy   = out Bool()                      // elementy w potoku, przeniesienia albo odczyt
    val nar    = out Bool()
    val raw    = out Bits(width bits)            // quire z ostatniego odczytu (U2)
    val count  = out UInt(32 bits)               // przyjete elementy od clear
  }

  // ---- S1: dekoder --------------------------------------------------------------------
  val one = PositDecoded(c)
  one.nar   := False
  one.zero  := False
  one.sign  := False
  one.scale := 0
  one.frac  := 0

  val v1 = RegNext(io.input.fire) init False
  val a1 = RegNextWhen(PositDecode(c, io.input.a), io.input.fire)
  val b1 = RegNextWhen(Mux(io.input.dot, PositDecode(c, io.input.b), one), io.input.fire)

  // ---- S2: iloczyn dokladny --------------------------------------------------------------
  val v2     = RegNext(v1) init False
  val prod2  = RegNext((U"1" @@ a1.frac) * (U"1" @@ b1.frac))          // 2F + 2 b
  val scale2 = RegNext(a1.scale.resize(c.scaleBits + 1) + b1.scale.resize(c.scaleBits + 1))
  val sign2  = RegNext(a1.sign ^ b1.sign)
  val zero2  = RegNext(a1.zero || b1.zero)
  val nar2   = RegNext(a1.nar || b1.nar)

  // ---- S3: wyrownanie: wartosc = prod * 2^(scale - 2F) = prod * 2^(scale + quireFrac - 2F) LSB quire
  val v3      = RegNext(v2) init False
  val shift   = (scale2 + c.quireFrac).asUInt.resize(log2Up(width + 2 * F))   // >= 0 dla kazdej pary positow
  val ext     = prod2.resize(width + 2 * F) |<< shift
  val aligned = RegNext(Mux(zero2 || nar2, B(0, width bits), ext(width + 2 * F - 1 downto 2 * F).asBits))
  val neg3    = RegNext(sign2 && !zero2 && !nar2)
  val nar3    = RegNext(nar2)

  // ---- S4: akumulator segmentowy -----------------------------------------------------------
  val seg   = Vec.fill(segs)(Reg(Bits(segBits bits)) init 0)
  val carry = Vec.fill(segs)(Reg(Bool()) init False)        // carry(i): do segmentu i+1
  val narQ  = RegInit(False)

  val addend = Mux(v3 && neg3, ~aligned, Mux(v3, aligned, B(0, width bits)))
  for (i <- 0 until segs) {
    val cin = if (i == 0) (v3 && neg3) else carry(i - 1)
    val sum = seg(i).asUInt +^ addend((i + 1) * segBits - 1 downto i * segBits).asUInt + cin.asUInt
    seg(i)   := sum(segBits - 1 downto 0).asBits
    carry(i) := sum.msb
  }
  when(v3 && nar3) { narQ := True }

  val count = Reg(UInt(32 bits)) init 0
  when(io.input.fire) { count := count + 1 }

  when(io.clear) {
    seg.foreach(_ := 0)
    carry.foreach(_ := False)
    narQ  := False
    count := 0
  }

  // przeniesienie z najwyzszego segmentu to zawiniecie U2: odrzucane, nie czeka na nie nikt
  val settled  = !carry.dropRight(1).reduce(_ || _)
  val pipeIdle = !v1 && !v2 && !v3

  // ---- odczyt --------------------------------------------------------------------------------
  val raw    = Reg(Bits(width bits)) init 0
  val mag    = Vec.fill(segs)(Reg(Bits(segBits bits)))
  val inc    = Vec.fill(segs)(Reg(Bool()))                 // segmentowa inkrementacja ~q + 1
  val negQ   = Reg(Bool())
  val top    = Reg(UInt(log2Up(segs) bits))
  val zeroQ  = Reg(Bool())
  val unr    = Reg(PositUnrounded(c, fracW))
  val result = Reg(Flow(PositAluRsp(c)))
  result.valid init False
  result.valid := False

  val fsm = new StateMachine {
    val IDLE, WAIT, NEG, FIND, CUT, ENC = new State
    setEntry(IDLE)

    IDLE.whenIsActive { when(io.round) { goto(WAIT) } }

    WAIT.whenIsActive {
      when(pipeIdle && settled) {                     // nowe elementy czekaja (ready = 0) do konca odczytu
        val q = seg.reverse.reduce(_ ## _)
        raw  := q
        negQ := q.msb
        for (i <- 0 until segs) {
          mag(i) := Mux(q.msb, ~seg(i), seg(i))
          inc(i) := (if (i == 0) q.msb else False)
        }
        goto(NEG)
      }
    }

    NEG.whenIsActive {
      for (i <- 0 until segs) {
        val s = mag(i).asUInt +^ inc(i).asUInt
        mag(i) := s(segBits - 1 downto 0).asBits
        if (i + 1 < segs) inc(i + 1) := s.msb
      }
      inc(0) := False
      when(!inc.reduce(_ || _)) { goto(FIND) }
    }

    FIND.whenIsActive {
      val nz = mag.map(_.orR)
      zeroQ := !nz.reduce(_ || _)
      top   := OHToUInt(OHMasking.last(nz.asBits))
      goto(CUT)
    }

    CUT.whenIsActive {
      val hi   = mag(top)
      val lo   = Mux(top === 0, B(0, segBits bits), mag(top - 1))
      val lz   = Lzc(hi).resize(log2Up(segBits + 1))
      val win  = (hi ## lo) |<< (lz + 1)                                  // bez wiodacej jedynki
      val below = (0 until segs).map(i => mag(i).orR && (top.resize(log2Up(segs) + 1) > (i + 1))).reduce(_ || _)
      val lead = top ## ((segBits - 1) - lz).resize(log2Up(segBits))        // pozycja wiodacej jedynki w quire
      val sc   = (B"0" ## lead).asSInt.resize(c.scaleBits + log2Up(width) + 2) - c.quireFrac
      val sMax = (1 << (c.scaleBits - 1)) - 1
      unr.nar    := narQ
      unr.zero   := zeroQ && !narQ
      unr.sign   := negQ
      unr.scale  := Mux(sc > sMax, S(sMax, c.scaleBits bits), sc.resize(c.scaleBits))   // > maxpos: i tak nasycenie
      unr.frac   := win(2 * segBits - 1 downto 2 * segBits - fracW).asUInt
      unr.sticky := win(2 * segBits - fracW - 1 downto 0).orR || below
      goto(ENC)
    }

    ENC.whenIsActive {
      val (bits, flags) = PositEncode.flagged(c, unr)
      result.valid        := True
      result.payload.bits  := bits
      result.payload.flags := flags
      goto(IDLE)
    }
  }

  io.input.ready := fsm.isActive(fsm.IDLE)
  io.result      := result
  io.busy        := !pipeIdle || !settled || !fsm.isActive(fsm.IDLE) || io.round
  io.nar         := narQ
  io.raw         := raw
  io.count       := count

  setDefinitionName(s"PositQuire_${c.n}_${c.es}")
}
