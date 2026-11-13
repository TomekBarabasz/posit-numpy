package posit

import spinal.core._
import spinal.lib._

/** State of the square-root digit recurrence: radicand bits still to bring down, remainder, root so far. */
case class PositSqrtState(c: PositConfig, steps: Int) extends Bundle {
  val nar, zero = Bool()
  val scale = SInt(c.scaleBits bits)
  val rad   = UInt(2 * steps bits)   // radicand, consumed two bits per step from the top
  val rem   = UInt(steps + 3 bits)   // partial remainder
  val root  = UInt(steps bits)
}

/** Restoring radix-2 square root, written once as a single step (one root bit), like PositDivision.
  *
  * sqrt(1.f * 2^s): for an odd s the significand is doubled (value in [1, 4)) and s becomes even,
  * the root of the significand is in [1, 2) and the scale is s / 2. The root has `steps` bits
  * (leading one, fraction, guard bits); a non-zero remainder is the sticky bit, so the encoder
  * rounds correctly, once. Negative non-zero input -> NaR.
  */
object PositSqrt {
  /** Root bits needed: the leading one, fracBits fraction bits and two guard bits. */
  def minSteps(c: PositConfig): Int = c.fracBits + 3

  def stepsFor(c: PositConfig, k: Int): Int = (minSteps(c) + k - 1) / k * k

  def prepare(a: PositDecoded, steps: Int): PositSqrtState = new Composite(a, "sqrtPrepare") {
    val c     = a.c
    val odd   = a.scale.lsb
    val sig   = (U"1" @@ a.frac).resize(c.fracBits + 2)
    val sig2  = Mux(odd, sig |<< 1, sig)                             // value in [1, 4), F frac bits
    val value = PositSqrtState(c, steps)
    value.rad   := (sig2 << (2 * steps - 2 - c.fracBits)).resize(2 * steps)   // root lands in [2^(steps-1), 2^steps)
    value.rem   := 0
    value.root  := 0
    value.scale := ((a.scale - odd.asUInt.intoSInt) >> 1).resize(c.scaleBits)
    value.zero  := a.zero
    value.nar   := a.nar || (a.sign && !a.zero)
  }.value

  /** One root bit. */
  def step(s: PositSqrtState): PositSqrtState = new Composite(s, "sqrtStep") {
    val w     = s.rem.getWidth
    val pulled = ((s.rem |<< 2) | s.rad(s.rad.high downto s.rad.high - 1).resize(w))
    val trial = ((s.root.resize(w) |<< 2) | 1)
    val take  = pulled >= trial
    val value = cloneOf(s)
    value := s
    value.rad.allowOverride()  := s.rad |<< 2
    value.rem.allowOverride()  := Mux(take, pulled - trial, pulled)
    value.root.allowOverride() := (s.root |<< 1) | take.asUInt.resize(s.root.getWidth)
  }.value

  def steps(s: PositSqrtState, count: Int): PositSqrtState = (0 until count).foldLeft(s)((acc, _) => step(acc))

  /** The leading root bit is always one; a non-zero remainder is the sticky bit. */
  def finish(s: PositSqrtState): PositUnrounded = new Composite(s, "sqrtFinish") {
    val n     = s.root.getWidth
    val value = PositUnrounded(s.c, n - 1)
    value.frac   := s.root(n - 2 downto 0)
    value.sticky := s.rem =/= 0
    value.scale  := s.scale
    value.sign   := False
    value.zero   := s.zero
    value.nar    := s.nar
  }.value
}
