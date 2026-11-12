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
//  Rejestry potoku jak w PositUnit: PositPipeline (po dekoderze, po
//  operacji, po enkoderze).
// =====================================================================

/** Kody operacji: te same w rejestrze OP, w Pythonie (sw/positnp/regs.py) i w CONFIG.opMask. */
object PositAluOp {
  val width = 4
  val codes : Map[PositOp, Int] = Map(PositOp.Add -> 0, PositOp.Sub -> 1, PositOp.Mul -> 2, PositOp.Div -> 3)
  def code(op : PositOp) : Int = codes(op)
  def mask(ops : Seq[PositOp]) : Int = ops.map(op => 1 << code(op)).fold(0)(_ | _)
}

case class PositAluConfig(c               : PositConfig,
                          ops             : Seq[PositOp]  = PositOp.all,
                          divBitsPerCycle : Int           = 1,
                          pipe            : PositPipeline = PositPipeline()) {
  require(ops.nonEmpty && ops.distinct.size == ops.size, s"ops: niepuste, bez powtorzen: $ops")
  require(divBitsPerCycle >= 1)
  def has(op : PositOp) : Boolean = ops.contains(op)
  def opMask : Int = PositAluOp.mask(ops)
  def divSteps : Int = PositDivision.stepsFor(c, divBitsPerCycle)
  /** Takty na dzielenie przy ciaglym strumieniu (ready = 1, bez przerw w valid):
    * zaladowanie stanu, divSteps / k iteracji, takt wystawienia wyniku. */
  def divCycles : Int = divSteps / divBitsPerCycle + 2
  override def toString = s"$c ops=${ops.map(_.name).mkString(",")} k=$divBitsPerCycle $pipe"
}

case class PositAluCmd(c : PositConfig) extends Bundle {
  val op   = UInt(PositAluOp.width bits)
  val a, b = Bits(c.n bits)
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
    val op   = UInt(PositAluOp.width bits)
    val a, b = PositDecoded(c)
  }

  def is(op : UInt, o : PositOp) : Bool = op === PositAluOp.code(o)

  val decoded = (io.cmd ~~ { x =>
    val d = Decoded()
    d.op := x.op
    d.a  := PositDecode(c, x.a)
    d.b  := PositDecode(c, x.b)
    d
  }).stageIf(cfg.pipe.afterDecode)

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

    val widths = Seq(Option(addRes).map(_.fracWidth), Option(mulRes).map(_.fracWidth),
                     if (cfg.has(PositOp.Div)) Some(cfg.divSteps - 1) else None).flatten
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

    // add/sub/mul: jeden takt; kod nieznany albo niezbudowany: NaR
    val fast = PositUnrounded(c, fracW)
    fast.nar    := True
    fast.zero   := False
    fast.sign   := False
    fast.scale  := 0
    fast.frac   := 0
    fast.sticky := False
    if (cfg.has(PositOp.Add)) when(is(d.op, PositOp.Add)) { fast := widen(addRes) }
    if (cfg.has(PositOp.Sub)) when(is(d.op, PositOp.Sub)) { fast := widen(addRes) }
    if (cfg.has(PositOp.Mul)) when(is(d.op, PositOp.Mul)) { fast := widen(mulRes) }

    val out = Stream(PositUnrounded(c, fracW))

    if (cfg.has(PositOp.Div)) {
      val k      = cfg.divBitsPerCycle
      val isDiv  = is(d.op, PositOp.Div)
      val state  = Reg(PositDivState(c, cfg.divSteps))
      val busy   = RegInit(False)
      val done   = RegInit(False)
      val cycles = Counter(cfg.divSteps / k)

      when(in.valid && isDiv && !busy && !done) {
        state := PositDivision.prepare(d.a, d.b, cfg.divSteps)
        busy  := True
      }
      when(busy) {
        state := PositDivision.steps(state, k)
        cycles.increment()
        when(cycles.willOverflow) {
          busy := False
          done := True
        }
      }
      when(out.fire && isDiv) { done := False }

      out.valid   := in.valid && (!isDiv || done)
      out.payload := Mux(isDiv, widen(PositDivision.finish(state)), fast)
      in.ready    := out.ready && (!isDiv || done)
    } else {
      out.valid   := in.valid
      out.payload := fast
      in.ready    := out.ready
    }
  }

  val computed = opStage.out.stageIf(cfg.pipe.afterOp)

  io.rsp << (computed ~~ { u =>
    val r = PositAluRsp(c)
    val (bits, flags) = PositEncode.flagged(c, u)
    r.bits  := bits
    r.flags := flags
    r
  }).stageIf(cfg.pipe.afterEncode)

  setDefinitionName(s"PositAlu_${c.n}_${c.es}")
}
