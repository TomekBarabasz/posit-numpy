package posit

import spinal.core._

/** Format posit<n, es>.
  *
  * The Posit Standard (2022) fixes es = 2 for every size; other values are kept for experiments.
  * Every width in the generator is derived from these two numbers.
  */
case class PositConfig(n: Int, es: Int = 2) {
  require(es >= 0 && n >= es + 4, s"posit<$n,$es>: need at least one fraction bit (n >= es + 4)")

  /** Fraction bits of the longest fraction (the regime takes at least 2 bits). */
  val fracBits: Int = n - 3 - es

  /** Signed scale = k * 2^es + e, with headroom for the results of add and mul. */
  val scaleBits: Int = log2Up((n - 1) << (es + 1)) + 2

  /** |k| of maxpos and of minpos. */
  val regimeMax: Int = n - 2

  /** Quire size from the standard (for the exact MAC, next step). */
  val quireBits: Int = 16 * n

  /** Not a Real: 100...0, the single exception value. */
  def nar: Bits = B(BigInt(1) << (n - 1), n bits)

  override def toString = s"posit<$n,$es>"
}
