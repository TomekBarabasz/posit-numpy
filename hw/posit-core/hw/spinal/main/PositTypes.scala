package posit

import spinal.core._

/** A decoded posit: (-1)^sign * 1.frac * 2^scale, plus the two special values. */
case class PositDecoded(c: PositConfig) extends Bundle {
  val nar, zero, sign = Bool()
  val scale = SInt(c.scaleBits bits)
  val frac  = UInt(c.fracBits bits)

  /** Posits are monotonic, so (scale, frac) orders magnitudes. Zero decodes to the smallest scale. */
  def magnitude: SInt = (scale ## frac).asSInt

  def unary_- : PositDecoded = {
    val r = PositDecoded(c)
    r := this
    r.sign.allowOverride() := !sign
    r
  }
}

/** A result before rounding: (-1)^sign * 1.frac * 2^scale, exact except for the sticky bit. */
case class PositUnrounded(c: PositConfig, fracWidth: Int) extends Bundle {
  val nar, zero, sign = Bool()
  val scale  = SInt(c.scaleBits bits)
  val frac   = UInt(fracWidth bits)
  val sticky = Bool()
}

/** Raw operands, as they arrive on the bus. */
case class PositOperands(c: PositConfig) extends Bundle {
  val a, b = Bits(c.n bits)
}

case class PositDecodedPair(c: PositConfig) extends Bundle {
  val a, b = PositDecoded(c)
}

object PositDecodedPair {
  def of(a: PositDecoded, b: PositDecoded): PositDecodedPair = {
    val p = PositDecodedPair(a.c)
    p.a := a
    p.b := b
    p
  }
}
