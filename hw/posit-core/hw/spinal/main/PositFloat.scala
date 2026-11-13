package posit

import spinal.core._
import spinal.lib._

/** IEEE float32 <-> posit conversions as single-cycle logic (stage 5).
  *
  * fromFloat32: the float's exact value as PositUnrounded (sign, scale, 23-bit fraction; subnormals
  * normalised with an LZC), rounded by the ordinary posit encoder: one rounding, NaN and inf -> NaR,
  * values beyond maxpos / below minpos saturate (flags as for every other operation).
  * toFloat32: round the posit fraction to 23 bits, nearest even; posit<32,2> lies inside the normal
  * float32 range (2^-120 .. 2^120), so there is no overflow and no subnormal. NaR -> quiet NaN.
  */
object PositFloat {
  def fromFloat32(c: PositConfig, bits: Bits): PositUnrounded = new Composite(bits, "fromFloat32") {
    val sign = bits(31)
    val e    = bits(30 downto 23).asUInt
    val f    = bits(22 downto 0)
    val sub  = e === 0
    val lz   = Lzc(f).resize(5)                                        // subnormal: leading one of f
    val fSub = (f |<< (lz + 1)).asUInt                                 // drop it
    val w    = c.scaleBits + 10
    val sc   = Mux(sub, -(lz.resize(w).asSInt) - 127, e.resize(w).asSInt - 127)
    val lo   = -(BigInt(1) << (c.scaleBits - 1))
    val hi   = (BigInt(1) << (c.scaleBits - 1)) - 1
    val value = PositUnrounded(c, 23)
    value.sign   := sign
    value.frac   := Mux(sub, fSub, f.asUInt)
    // outside the scale range of the format: clamp, it saturates anyway (|scale| > 2 maxScale)
    value.scale  := Mux(sc < S(lo, w bits), S(lo, c.scaleBits bits), Mux(sc > S(hi, w bits), S(hi, c.scaleBits bits), sc.resize(c.scaleBits)))
    value.sticky := False
    value.zero   := sub && f === 0
    value.nar    := e === 255
  }.value

  /** (float32 bits, inexact). */
  def toFloat32(c: PositConfig, d: PositDecoded): (Bits, Bool) = {
    require(c.fracBits > 23 && c.maxScale < 127, s"$c: toFloat32 is built for posit<32,2>")
    val r = new Composite(d, "toFloat32") {
      val F     = c.fracBits
      val mant  = d.frac(F - 1 downto F - 23)
      val guard = d.frac(F - 24)
      val stk   = if (F > 24) d.frac(F - 25 downto 0).orR else False
      val up    = guard && (mant.lsb || stk)
      val m1    = mant +^ up.asUInt                                    // 24 bits: carry into the exponent
      val exp   = (d.scale.resize(10) + 127 + m1.msb.asUInt.intoSInt).asUInt.resize(8)
      val value = Bits(32 bits)
      value := d.sign ## exp ## m1(22 downto 0)
      when(d.zero) { value.clearAll() }
      when(d.nar)  { value := B"32'x7fc00000" }
      val inexact = !d.zero && !d.nar && (guard || stk)
    }
    (r.value, r.inexact)
  }
}
