package posit.vector

import spinal.core._
import spinal.lib._
import posit._

// =====================================================================
//  PositAlu: jedna jednostka na wszystkie operacje (plan, etap 1).
//
//    cmd(op, a, b) -> decode x2 -> [add/sub | mul | div] -> encode -> rsp(bits, flags)
//
//  - jeden dekoder na argument i jeden enkoder (z flagami) na wszystkie
//    operacje; add i sub dziela sumator (sub = add(a, -b)),
//  - dzielenie iteracyjne (DivArch.Iterative): k bitow ilorazu na takt,
//    wiec latencja zalezy od operacji; strumien zostaje w kolejnosci, bo
//    etap operacji trzyma ready nisko, dopoki iloraz nie jest gotowy,
//  - wynik operacji ma wspolna szerokosc ulamka (najszersza z
//    zbudowanych); wezsze sa dopelniane zerami od dolu, co nie zmienia
//    wartosci, wiec enkoder zaokragla tak samo jak w PositUnit,
//  - kod operacji spoza `ops` daje NaR z flaga nar (opakowanie AXI i tak
//    go odrzuca, ALU ma tylko zachowac sie deterministycznie).
//
//  Etap 5 (opcjonalne w PositAluConfig): sqrt (rekurencja cyfra po cyfrze,
//  k bitow / takt, jak dzielnik), FMA a*b + c (dwa takty: iloczyn
//  zarejestrowany, potem suma; jedno zaokraglenie), konwersje float32 <-> posit
//  (tylko posit<32,2>; P2F omija enkoder positow). AXPY to w ALU to samo co
//  FMA - rozni sie tylko tym, skad PositStreamAlu bierze b i c.
//
//  Rejestry potoku jak w PositUnit: PositPipeline (po dekoderze, po
//  operacji, po enkoderze).
// =====================================================================

/** Kody operacji: te same w rejestrze OP, w Pythonie (sw/positnp/regs.py) i w CONFIG.opMask. */
object PositAluOp {
  val width = 4
  val codes : Map[PositOp, Int] = Map(PositOp.Add -> 0, PositOp.Sub -> 1, PositOp.Mul -> 2, PositOp.Div -> 3)
  // 4..6: redukcje quire (PositVecAxi), nie ALU
  val SQRT = 7    // sqrt(a)
  val FMA  = 8    // a * b + c
  val AXPY = 9    // w ALU = FMA; PositStreamAlu: b z rejestru, c ze strumienia
  val F2P  = 10   // a: bity float32 -> posit
  val P2F  = 11   // posit -> bity float32
  def code(op : PositOp) : Int = codes(op)
  def mask(ops : Seq[PositOp]) : Int = ops.map(op => 1 << code(op)).fold(0)(_ | _)
}

case class PositAluConfig(c               : PositConfig,
                          ops             : Seq[PositOp]  = PositOp.all,
                          divBitsPerCycle : Int           = 1,
                          pipe            : PositPipeline = PositPipeline(),
                          sqrt            : Boolean       = false,
                          fma             : Boolean       = false,
                          convert         : Boolean       = false) {
  require(ops.nonEmpty && ops.distinct.size == ops.size, s"ops: niepuste, bez powtorzen: $ops")
  require(divBitsPerCycle >= 1)
  require(!convert || (c.n == 32 && c.es == 2), s"$c: konwersje float32 sa dla posit<32,2>")
  def has(op : PositOp) : Boolean = ops.contains(op)
  def opMask : Int = PositAluOp.mask(ops) |
    (if (sqrt) 1 << PositAluOp.SQRT else 0) |
    (if (fma) (1 << PositAluOp.FMA) | (1 << PositAluOp.AXPY) else 0) |
    (if (convert) (1 << PositAluOp.F2P) | (1 << PositAluOp.P2F) else 0)
  def divSteps : Int = PositDivision.stepsFor(c, divBitsPerCycle)
  /** Takty na dzielenie przy ciaglym strumieniu (ready = 1, bez przerw w valid):
    * zaladowanie stanu, divSteps / k iteracji, takt wystawienia wyniku. */
  def divCycles : Int = divSteps / divBitsPerCycle + 2
  def sqrtSteps : Int = PositSqrt.stepsFor(c, divBitsPerCycle)
  def sqrtCycles : Int = sqrtSteps / divBitsPerCycle + 2
  /** FMA: zaladowanie iloczynu, jeden takt sumy, wystawienie. */
  def fmaCycles : Int = 3
  override def toString = s"$c ops=${ops.map(_.name).mkString(",")}" +
    (if (sqrt) ",sqrt" else "") + (if (fma) ",fma" else "") + (if (convert) ",f2p,p2f" else "") + s" k=$divBitsPerCycle $pipe"
}

case class PositAluCmd(c : PositConfig) extends Bundle {
  val op     = UInt(PositAluOp.width bits)
  val a, b   = Bits(c.n bits)
  val addend = Bits(c.n bits)            // FMA / AXPY: a * b + addend
}

case class PositAluRsp(c : PositConfig) extends Bundle {
  val bits  = Bits(c.n bits)
  val flags = PositFlags()
}

class PositAlu(val cfg : PositAluConfig) extends Component {
  val c = cfg.c
  val io = new Bundle {
    val cmd = slave(Stream(PositAluCmd(c)))
    val rsp = master(Stream(PositAluRsp(c)))
  }

  case class Decoded() extends Bundle {
    val op      = UInt(PositAluOp.width bits)
    val a, b, d = PositDecoded(c)
    val rawA    = Bits(c.n bits)              // F2P: bity float32
  }

  def is(op : UInt, o : PositOp) : Bool = op === PositAluOp.code(o)
  def isCode(op : UInt, code : Int) : Bool = op === code

  val decoded = (io.cmd ~~ { x =>
    val d = Decoded()
    d.op   := x.op
    d.a    := PositDecode(c, x.a)
    d.b    := PositDecode(c, x.b)
    d.d    := (if (cfg.fma) PositDecode(c, x.addend) else PositDecode(c, B(0, c.n bits)))
    d.rawA := x.a
    d
  }).stageIf(cfg.pipe.afterDecode)

  /** Wynik etapu operacji: posit przed zaokragleniem albo gotowe bity (P2F). */
  case class OpOut(fracW : Int) extends Bundle {
    val u          = PositUnrounded(c, fracW)
    val raw        = Bool()
    val rawBits    = Bits(c.n bits)
    val rawInexact = Bool()
  }

  // ---- etap operacji ---------------------------------------------------------------
  val opStage = new Area {
    val in = decoded
    val d  = in.payload

    // add i sub na jednym sumatorze
    val addRes = (cfg.has(PositOp.Add) || cfg.has(PositOp.Sub)) generate {
      val bAdj = PositDecoded(c)
      bAdj := d.b
      if (cfg.has(PositOp.Sub)) when(is(d.op, PositOp.Sub)) { bAdj.sign := !d.b.sign }
      PositOps.add(d.a, bAdj)
    }
    val mulRes = cfg.has(PositOp.Mul) generate PositOps.mul(d.a, d.b)
    val f2pRes = cfg.convert generate PositFloat.fromFloat32(c, d.rawA)

    val widths = Seq(Option(addRes).map(_.fracWidth), Option(mulRes).map(_.fracWidth), Option(f2pRes).map(_.fracWidth),
                     if (cfg.has(PositOp.Div)) Some(cfg.divSteps - 1) else None,
                     if (cfg.sqrt) Some(cfg.sqrtSteps - 1) else None,
                     if (cfg.fma) Some(c.fracBits + 4) else None).flatten
    val fracW  = widths.max

    def widen(u : PositUnrounded) : PositUnrounded = {
      val r = PositUnrounded(c, fracW)
      r.nar    := u.nar
      r.zero   := u.zero
      r.sign   := u.sign
      r.scale  := u.scale
      r.sticky := u.sticky
      r.frac   := (if (u.fracWidth == fracW) u.frac else u.frac @@ U(0, fracW - u.fracWidth bits))
      r
    }
    def posit(u : PositUnrounded) : OpOut = {
      val o = OpOut(fracW)
      o.u          := widen(u)
      o.raw        := False
      o.rawBits    := 0
      o.rawInexact := False
      o
    }

    // jednotaktowe; kod nieznany albo niezbudowany: NaR
    val fast = OpOut(fracW)
    fast.u.nar    := True
    fast.u.zero   := False
    fast.u.sign   := False
    fast.u.scale  := 0
    fast.u.frac   := 0
    fast.u.sticky := False
    fast.raw        := False
    fast.rawBits    := 0
    fast.rawInexact := False
    if (cfg.has(PositOp.Add)) when(is(d.op, PositOp.Add)) { fast := posit(addRes) }
    if (cfg.has(PositOp.Sub)) when(is(d.op, PositOp.Sub)) { fast := posit(addRes) }
    if (cfg.has(PositOp.Mul)) when(is(d.op, PositOp.Mul)) { fast := posit(mulRes) }
    if (cfg.convert) {
      when(isCode(d.op, PositAluOp.F2P)) { fast := posit(f2pRes) }
      when(isCode(d.op, PositAluOp.P2F)) {
        val (fb, inexact) = PositFloat.toFloat32(c, d.a)
        fast.u.nar      := d.a.nar        // flaga nar; bity z rawBits
        fast.raw        := True
        fast.rawBits    := fb
        fast.rawInexact := inexact
      }
    }

    val out = Stream(OpOut(fracW))

    /** Jednostka wielotaktowa: start przy pierwszym takcie polecenia, `iters` krokow, done do wydania wyniku. */
    case class Slow(isOp : Bool, done : Bool, result : OpOut)
    def iterative[T <: Data](isOp : Bool, init : => T, step : T => T, iters : Int, finish : T => PositUnrounded) : Slow = {
      val area = new Area {
        val first  = init
        val state  = Reg(cloneOf(first))
        val busy   = RegInit(False)
        val done   = RegInit(False)
        val cycles = Counter(iters)
        when(in.valid && isOp && !busy && !done) {
          state := first
          busy  := True
        }
        when(busy) {
          state := step(state)
          cycles.increment()
          when(cycles.willOverflow) {
            busy := False
            done := True
          }
        }
        when(out.fire && isOp) { done := False }
        val result = posit(finish(state))
      }
      Slow(isOp, area.done, area.result)
    }

    val k = cfg.divBitsPerCycle
    val slow = Seq(
      cfg.has(PositOp.Div) generate iterative(is(d.op, PositOp.Div), PositDivision.prepare(d.a, d.b, cfg.divSteps),
        (s : PositDivState) => PositDivision.steps(s, k), cfg.divSteps / k, PositDivision.finish),
      cfg.sqrt generate iterative(isCode(d.op, PositAluOp.SQRT), PositSqrt.prepare(d.a, cfg.sqrtSteps),
        (s : PositSqrtState) => PositSqrt.steps(s, k), cfg.sqrtSteps / k, PositSqrt.finish),
      cfg.fma generate iterative(isCode(d.op, PositAluOp.FMA) || isCode(d.op, PositAluOp.AXPY),
        PositFma.prepare(d.a, d.b, d.d), (s : PositFmaState) => s, 1, PositFma.finish)
    ).filter(_ != null)

    if (slow.nonEmpty) {
      val isSlow   = slow.map(_.isOp).reduce(_ || _)
      val slowDone = slow.map(u => u.isOp && u.done).reduce(_ || _)
      val payload  = OpOut(fracW)
      payload := fast
      for (u <- slow) when(u.isOp) { payload := u.result }
      out.valid   := in.valid && (!isSlow || slowDone)
      out.payload := payload
      in.ready    := out.ready && (!isSlow || slowDone)
    } else {
      out.valid   := in.valid
      out.payload := fast
      in.ready    := out.ready
    }
  }

  val computed = opStage.out.stageIf(cfg.pipe.afterOp)

  io.rsp << (computed ~~ { o =>
    val r = PositAluRsp(c)
    val (bits, flags) = PositEncode.flagged(c, o.u)
    r.bits  := bits
    r.flags := flags
    when(o.raw) {
      r.bits          := o.rawBits
      r.flags.sat     := False
      r.flags.inexact := o.rawInexact
    }
    r
  }).stageIf(cfg.pipe.afterEncode)

  setDefinitionName(s"PositAlu_${c.n}_${c.es}")
}
