package posit

import spinal.core._

/** Operators work on decoded posits and return unrounded results:
  * every operator is exact up to its sticky bit, and the encoder rounds once.
  */
object PositOps {
  /** Guard, round and sticky positions below the fraction of the adder. */
  val guardBits = 3

  def mul(a: PositDecoded, b: PositDecoded): PositUnrounded = new Composite(a, "mul") {
    val c    = a.c
    val fw   = c.fracBits
    val prod = (U"1" @@ a.frac) * (U"1" @@ b.frac)    // in [1, 4), 2*fw fraction bits, exact
    val ovf  = prod.msb                               // product >= 2: renormalize by one

    val value = PositUnrounded(c, 2 * fw + 1)
    value.frac   := Mux(ovf, prod(2 * fw downto 0), prod(2 * fw - 1 downto 0) @@ U"0")
    value.scale  := a.scale + b.scale + ovf.asUInt.intoSInt
    value.sign   := a.sign ^ b.sign
    value.zero   := a.zero || b.zero
    value.nar    := a.nar || b.nar
    value.sticky := False
  }.value

  def add(a: PositDecoded, b: PositDecoded): PositUnrounded = new Composite(a, "add") {
    val c     = a.c
    val fw    = c.fracBits
    val w     = fw + 1 + guardBits
    val swap  = a.magnitude < b.magnitude
    val big   = Mux(swap, b, a)
    val small = Mux(swap, a, b)

    // Align the smaller operand; whatever falls off becomes the sticky bit.
    val dist     = (big.scale - small.scale).asUInt
    val distSat  = Mux(dist >= w, U(w, log2Up(w + 1) bits), dist.resize(log2Up(w + 1)))
    val sigBig   = U"1" @@ big.frac @@ U(0, guardBits bits)
    val sigSmall = Mux(small.zero, U(0, w bits), U"1" @@ small.frac @@ U(0, guardBits bits))
    val shifted  = (sigSmall @@ U(0, w bits)) |>> distSat
    val aligned  = shifted(2 * w - 1 downto w) | shifted(w - 1 downto 0).orR.asUInt.resize(w)

    val sum  = Mux(a.sign ^ b.sign, sigBig -^ aligned, sigBig +^ aligned) // >= 0: |big| >= |small|
    val lz   = Lzc(sum.asBits)
    val norm = sum |<< lz

    val value = PositUnrounded(c, w)
    value.frac   := norm(w - 1 downto 0)
    value.scale  := big.scale + 1 - lz.intoSInt
    value.sign   := big.sign
    value.zero   := (a.zero && b.zero) || sum === 0
    value.nar    := a.nar || b.nar
    value.sticky := False
  }.value

  def sub(a: PositDecoded, b: PositDecoded): PositUnrounded = add(a, -b)

  /** Combinational division; see PositDivider for iterative and pipelined versions. */
  def div(a: PositDecoded, b: PositDecoded): PositUnrounded = PositDivision.combinational(a, b)

  // Thanks to the encoding, these need no decoding at all.
  def neg(p: Bits): Bits = (-p.asSInt).asBits     // NaR and zero map to themselves
  def abs(p: Bits): Bits = p.asSInt.abs.asBits.resize(p.getWidth)
  def lt(p: Bits, q: Bits): Bool = p.asSInt < q.asSInt // NaR orders below every real
}

/** An operator as a value: a name for the generated module plus the hardware function. */
sealed abstract class PositOp(val name: String, val hw: (PositDecoded, PositDecoded) => PositUnrounded)

object PositOp {
  case object Add extends PositOp("add", PositOps.add)
  case object Sub extends PositOp("sub", PositOps.sub)
  case object Mul extends PositOp("mul", PositOps.mul)
  case object Div extends PositOp("div", PositOps.div)
  val all: Seq[PositOp] = Seq(Add, Sub, Mul, Div)
}
