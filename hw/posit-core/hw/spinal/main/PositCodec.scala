package posit

import spinal.core._

/** Bits -> (sign, scale, frac). */
object PositDecode {
  def apply(c: PositConfig, p: Bits): PositDecoded = new Composite(p, "decode") {
    import c._
    val mag  = Mux(p.msb, -p.asSInt, p.asSInt).asBits  // |p|; NaR maps to itself
    val body = mag(n - 2 downto 0)                      // regime ++ exponent ++ fraction
    val r0   = body.msb                                 // regime polarity
    val run  = Lzc(Mux(r0, ~body, body))                // regime run length, 1 .. n-1
    val runS = run.resize(scaleBits - es).asSInt
    val k    = Mux(r0, runS - 1, -runS)
    val tail = body |<< (run +^ 1)                      // drop regime and terminator

    val value = PositDecoded(c)
    value.sign  := p.msb
    value.zero  := !p.orR
    value.nar   := p === nar
    value.scale := (if (es > 0) (k ## tail(n - 2 downto n - 1 - es)).asSInt else k)
    value.frac  := tail(n - 2 - es downto 2).asUInt
  }.value
}

/** Exceptions of one rounding, for the diagnostic counters.
  *   nar      the result is NaR (an operand was NaR, or x / 0)
  *   sat      the exact result was nonzero and beyond maxpos or below minpos, so it was clamped
  *   inexact  the result differs from the exact value (rounding or saturation)
  * Bit order of asBits: nar = 0, sat = 1, inexact = 2.
  */
case class PositFlags() extends Bundle {
  val nar, sat, inexact = Bool()
}

/** (sign, scale, frac, sticky) -> Bits, rounded as the standard requires:
  * round to nearest, ties to even on the encoding, never to zero and never to NaR.
  */
object PositEncode {
  def apply(c: PositConfig, u: PositUnrounded): Bits = flagged(c, u)._1

  /** The encoding and the flags of the same rounding. */
  def flagged(c: PositConfig, u: PositUnrounded): (Bits, PositFlags) = {
    val e = new Composite(u, "encode") {
      import c._
      require(u.fracWidth + es >= 2)

      val k     = u.scale >> es                            // regime value (floor division)
      val pos   = !k.msb                                   // k >= 0
      val shift = Mux(pos, k, ~k).asUInt.resize(log2Up(n)) // k, or -k-1 when k < 0

      // Lay out "r !r exponent fraction" and shift right arithmetically:
      // the sign extension writes the regime run, !r becomes its terminator.
      val head    = if (es > 0) pos ## !pos ## u.scale(es - 1 downto 0) else pos ## !pos
      val laid    = (head ## u.frac ## B(0, n - 3 bits)).asSInt >> shift
      val w       = laid.getWidth
      val body    = laid(w - 1 downto w - n + 1).asUInt
      val guard   = laid(w - n)
      val sticky  = laid(w - n - 1 downto 0).asBits.orR || u.sticky
      val rounded = body + (guard && (body.lsb || sticky)).asUInt

      val above = k >= regimeMax  // at or beyond maxpos
      val below = k < -regimeMax  // below minpos

      val mag = UInt(n - 1 bits)
      mag := rounded
      when(above) { mag.setAll() } // beyond maxpos: saturate, never NaR
      when(below) { mag := 1 }     // below minpos: saturate, never zero

      val value  = Bits(n bits)
      val signed = (False ## mag).asSInt
      value := Mux(u.sign, -signed, signed).asBits
      when(u.zero) { value.clearAll() }
      when(u.nar)  { value := nar }

      // maxpos = 2^(regimeMax * 2^es) exactly: the one value at k = regimeMax that is not clamped
      val lowScale  = if (es > 0) u.scale(es - 1 downto 0) === 0 else True
      val exactMax  = k === regimeMax && lowScale && u.frac === 0 && !u.sticky
      val special   = u.zero || u.nar
      val flags     = PositFlags()
      flags.nar     := u.nar
      flags.sat     := !special && ((above && !exactMax) || below)
      flags.inexact := !special && Mux(above || below, !exactMax, guard || sticky)
    }
    (e.value, e.flags)
  }
}
