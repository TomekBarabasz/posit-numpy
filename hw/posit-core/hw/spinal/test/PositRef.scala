package posit

/** Exact software model of the Posit Standard (2022), written independently of the RTL:
  * it builds the full bit string of the exact value and rounds it on n bits.
  */
object PositRef {
  /** Exact dyadic number m * 2^e. Sums and products of posits are always dyadic. */
  final case class Dyadic(m: BigInt, e: Int) {
    def +(o: Dyadic): Dyadic = {
      val lo = e min o.e
      Dyadic((m << (e - lo)) + (o.m << (o.e - lo)), lo)
    }
    def *(o: Dyadic): Dyadic = Dyadic(m * o.m, e + o.e)
    def unary_- : Dyadic     = Dyadic(-m, e)
    def -(o: Dyadic): Dyadic = this + -o

    /** Truncated quotient with plenty of bits, plus whether anything was left over. */
    def /(o: Dyadic): (Dyadic, Boolean) = {
      val extra  = 64 + o.m.bitLength
      val (q, r) = (m.abs << extra) /% o.m.abs
      (Dyadic(q * m.signum * o.m.signum, e - o.e - extra), r != 0)
    }
  }

  private def bitsToInt(bits: Seq[Boolean]): BigInt = bits.foldLeft(BigInt(0))((acc, b) => (acc << 1) + (if (b) 1 else 0))

  /** None stands for NaR. */
  def decode(c: PositConfig, raw: BigInt): Option[Dyadic] = {
    val n    = c.n
    val mask = (BigInt(1) << n) - 1
    val p    = raw & mask
    if (p == 0) Some(Dyadic(0, 0))
    else if (p == BigInt(1) << (n - 1)) None
    else {
      val neg  = p.testBit(n - 1)
      val body = (n - 2 to 0 by -1).map(((if (neg) -p else p) & mask).testBit)
      val run  = body.takeWhile(_ == body.head).length
      val k    = if (body.head) run - 1 else -run
      val rest = body.drop(run + 1)
      val e    = bitsToInt(rest.padTo(c.es, false).take(c.es)).toInt
      val frac = rest.drop(c.es)
      val sig  = bitsToInt(true +: frac)
      Some(Dyadic(if (neg) -sig else sig, (k << c.es) + e - frac.length))
    }
  }

  /** `inexact`: the true value lies strictly beyond x (a remainder was dropped). */
  def encode(c: PositConfig, x: Option[Dyadic], inexact: Boolean = false): BigInt = x match {
    case None                => BigInt(1) << (c.n - 1)
    case Some(d) if d.m == 0 => BigInt(0)
    case Some(d) =>
      val n     = c.n
      val mag   = d.m.abs
      val scale = d.e + mag.bitLength - 1
      val k     = Math.floorDiv(scale, 1 << c.es)
      val e     = scale - (k << c.es)
      val regime   = if (k >= 0) Seq.fill(k + 1)(true) :+ false else Seq.fill(-k)(false) :+ true
      val exponent = (c.es - 1 to 0 by -1).map(i => ((e >> i) & 1) == 1)
      val fraction = (mag.bitLength - 2 to 0 by -1).map(mag.testBit)
      val string   = (regime ++ exponent ++ fraction).padTo(n, false)

      val body    = bitsToInt(string.take(n - 1))
      val guard   = string(n - 1)
      val sticky  = string.drop(n).contains(true) || inexact
      val rounded = if (guard && (body.testBit(0) || sticky)) body + 1 else body
      val maxpos  = (BigInt(1) << (n - 1)) - 1
      val r       = rounded.min(maxpos).max(1) // never NaR, never zero
      if (d.m < 0) (-r) & ((BigInt(1) << n) - 1) else r
  }

  /** Flags of one rounding, as PositFlags in hardware (asBits: nar = 0, sat = 1, inexact = 2). */
  final case class Flags(nar: Boolean = false, sat: Boolean = false, inexact: Boolean = false) {
    def bits: Int = (if (nar) 1 else 0) | (if (sat) 2 else 0) | (if (inexact) 4 else 0)
  }

  /** Result and flags. `inexact`: the true value lies strictly beyond x (a division remainder). */
  def round(c: PositConfig, x: Option[Dyadic], inexact: Boolean = false): (BigInt, Flags) = x match {
    case None                => (encode(c, None), Flags(nar = true))
    case Some(d) if d.m == 0 => (BigInt(0), Flags())
    case Some(d) =>
      val r        = encode(c, x, inexact)
      val maxScale = (c.n - 2) << c.es
      val mag      = Dyadic(d.m.abs, d.e)
      val maxpos   = Dyadic(1, maxScale)
      val minpos   = Dyadic(1, -maxScale)
      def cmp(p: Dyadic, q: Dyadic): Int = (p - q).m.signum
      // |true| > maxpos: |x| > maxpos, or |x| == maxpos with a remainder; |true| < minpos iff |x| < minpos
      val sat   = cmp(mag, maxpos) > 0 || (cmp(mag, maxpos) == 0 && inexact) || cmp(mag, minpos) < 0
      val exact = !inexact && decode(c, r).exists(v => cmp(v, d) == 0)
      (r, Flags(sat = sat, inexact = !exact))
  }

  def withFlags(c: PositConfig, op: PositOp, a: BigInt, b: BigInt): (BigInt, Flags) = (decode(c, a), decode(c, b)) match {
    case (Some(x), Some(y)) =>
      op match {
        case PositOp.Add                => round(c, Some(x + y))
        case PositOp.Sub                => round(c, Some(x - y))
        case PositOp.Mul                => round(c, Some(x * y))
        case PositOp.Div if y.m == 0    => round(c, None)
        case PositOp.Div                => val (q, inexact) = x / y; round(c, Some(q), inexact)
      }
    case _ => round(c, None)
  }

  def apply(c: PositConfig, op: PositOp, a: BigInt, b: BigInt): BigInt = withFlags(c, op, a, b)._1
}

/** Exact quire model: an unbounded integer in units of 2^-quireFrac (minpos^2), NaR as a flag.
  * Independent of the hardware algorithm (no alignment, no segments, no carries).
  */
object QuireRef {
  import PositRef.{Dyadic, decode}

  /** a * b (dot) or a (sum) in quire units; None for NaR. */
  def term(c: PositConfig, a: BigInt, b: Option[BigInt]): Option[BigInt] = {
    val x = decode(c, a)
    val y = b.map(decode(c, _)).getOrElse(Some(Dyadic(1, 0)))
    for (u <- x; v <- y) yield {
      val p = u * v
      val e = p.e + c.quireFrac
      require(e >= 0 || (p.m % (BigInt(1) << -e)) == 0, s"$c: term below the quire LSB")
      if (e >= 0) p.m << e else p.m >> -e
    }
  }

  /** Sum of terms; (quire, nar). */
  def accumulate(c: PositConfig, terms: Iterator[(BigInt, Option[BigInt])]): (BigInt, Boolean) =
    terms.foldLeft((BigInt(0), false)) { case ((q, nar), (a, b)) =>
      term(c, a, b) match {
        case Some(t) => (q + t, nar)
        case None    => (q, true)
      }
    }

  def round(c: PositConfig, q: BigInt, nar: Boolean): (BigInt, PositRef.Flags) =
    if (nar) PositRef.round(c, None) else PositRef.round(c, Some(Dyadic(q, -c.quireFrac)))

  /** The quire as the hardware register holds it: width bits, two's complement. */
  def raw(c: PositConfig, q: BigInt, width: Int): BigInt = q & ((BigInt(1) << width) - 1)
}

