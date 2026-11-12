package posit

import spinal.core._
import spinal.lib._

/** State of the digit recurrence a / b on significands: remainder, divisor, quotient so far. */
case class PositDivState(c: PositConfig, quotBits: Int) extends Bundle {
  val nar, zero, sign = Bool()
  val scale   = SInt(c.scaleBits bits)
  val divisor = UInt(c.fracBits + 1 bits) // 1.fb
  val rem     = UInt(c.fracBits + 2 bits) // always < 2 * divisor
  val quot    = UInt(quotBits bits)
}

/** Restoring radix-2 division, written once as a single step.
  * The same `step` builds a combinational, an iterative and a pipelined divider.
  */
object PositDivision {
  /** Quotient bits needed: the leading one, fracBits fraction bits and a guard bit. */
  def minSteps(c: PositConfig): Int = c.fracBits + 2

  /** Round the step count up to a multiple of k; extra quotient bits only add precision. */
  def stepsFor(c: PositConfig, k: Int): Int = (minSteps(c) + k - 1) / k * k

  def prepare(a: PositDecoded, b: PositDecoded, quotBits: Int): PositDivState = new Composite(a, "divPrepare") {
    val c     = a.c
    val sigA  = U"1" @@ a.frac
    val sigB  = U"1" @@ b.frac
    val below = sigA < sigB // quotient < 1: take one more dividend bit, one less in the scale

    val value = PositDivState(c, quotBits)
    value.rem     := Mux(below, sigA @@ U"0", U"0" @@ sigA)
    value.divisor := sigB
    value.quot    := 0
    value.scale   := a.scale - b.scale - below.asUInt.intoSInt
    value.sign    := a.sign ^ b.sign
    value.zero    := a.zero
    value.nar     := a.nar || b.nar || b.zero // x / 0 = NaR
  }.value

  /** One quotient bit. */
  def step(s: PositDivState): PositDivState = new Composite(s, "divStep") {
    val diff = s.rem -^ s.divisor // msb set = borrow
    val take = !diff.msb
    val kept = Mux(take, diff.resize(s.rem.getWidth), s.rem)

    val value = cloneOf(s)
    value := s
    value.rem.allowOverride()  := kept |<< 1
    value.quot.allowOverride() := s.quot(s.quotBits - 2 downto 0) @@ take
  }.value

  def steps(s: PositDivState, count: Int): PositDivState = (0 until count).foldLeft(s)((acc, _) => step(acc))

  /** The leading quotient bit is always one; a non-zero remainder is the sticky bit. */
  def finish(s: PositDivState): PositUnrounded = new Composite(s, "divFinish") {
    val value = PositUnrounded(s.c, s.quotBits - 1)
    value.frac   := s.quot(s.quotBits - 2 downto 0)
    value.sticky := s.rem =/= 0
    value.scale  := s.scale
    value.sign   := s.sign
    value.zero   := s.zero
    value.nar    := s.nar
  }.value

  /** Fully unrolled: one long combinational chain, usable as an operator of PositUnit. */
  def combinational(a: PositDecoded, b: PositDecoded): PositUnrounded = {
    val n = minSteps(a.c)
    finish(steps(prepare(a, b, n), n))
  }
}

/** How to spend the recurrence in time and in area. */
sealed trait DivArch
object DivArch {
  /** One division at a time, `bitsPerCycle` quotient bits per clock. Smallest. */
  case class Iterative(bitsPerCycle: Int = 1) extends DivArch
  /** A new division every clock, `bitsPerStage` quotient bits per pipeline stage. Fastest. */
  case class Pipelined(bitsPerStage: Int = 4) extends DivArch
}

class PositDivider(c: PositConfig, val arch: DivArch = DivArch.Iterative()) extends PositStreamComponent(c) {
  private def start(o: PositOperands, quotBits: Int) =
    PositDivision.prepare(PositDecode(c, o.a), PositDecode(c, o.b), quotBits)

  arch match {
    case DivArch.Pipelined(k) =>
      val total = PositDivision.stepsFor(c, k)
      val first = io.cmd ~~ (o => start(o, total))
      val last  = (0 until total / k).foldLeft(first)((s, _) => (s ~~ (st => PositDivision.steps(st, k))).stage())
      io.rsp << (last ~~ (st => PositEncode(c, PositDivision.finish(st))))

    case DivArch.Iterative(k) =>
      val total  = PositDivision.stepsFor(c, k)
      val state  = Reg(PositDivState(c, total))
      val busy   = RegInit(False)
      val done   = RegInit(False)
      val cycles = Counter(total / k)

      io.cmd.ready := !busy && (!done || io.rsp.ready) // accept the next one as the result leaves
      when(io.rsp.fire) { done := False }
      when(io.cmd.fire) {
        state := start(io.cmd.payload, total)
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

      io.rsp.valid   := done
      io.rsp.payload := PositEncode(c, PositDivision.finish(state))
  }

  setDefinitionName(arch match {
    case DivArch.Pipelined(k) => s"PositDivPipe${k}_${c.n}_${c.es}"
    case DivArch.Iterative(k) => s"PositDivIter${k}_${c.n}_${c.es}"
  })
}
