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

  /** Quire size from the standard for es = 2 (16 n); quireWidth is the general one. */
  val quireBits: Int = 16 * n

  /** Largest |scale|: maxpos = 2^maxScale, minpos = 2^-maxScale. */
  val maxScale: Int = regimeMax << es

  /** Quire LSB = minpos^2 = 2^-quireFrac: every sum of products of posits is an exact quire integer. */
  val quireFrac: Int = 2 * maxScale

  /** Quire width: sign + 31 carry-guard bits + 4 maxScale bits (= 16 n for es = 2),
    * rounded up to a multiple of `segBits` (the segments of the accumulator). */
  def quireWidth(segBits: Int = 64): Int = (4 * maxScale + 32 + segBits - 1) / segBits * segBits

  /** Not a Real: 100...0, the single exception value. */
  def nar: Bits = B(BigInt(1) << (n - 1), n bits)

  override def toString = s"posit<$n,$es>"
}
