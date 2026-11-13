package posit.vector

import spinal.core._
import spinal.core.sim._
import scala.util.Random
import newhope.vertebra._
import newhope.vertebra.sim.{SimEnv, SimSeed}
import posit._
import posit.PositSimKit.{exhaustive, stress}

// =====================================================================
//  PositQuire: dokladne sumy i iloczyny skalarne, jedno zaokraglenie.
//
//  Zrodlo planu: plan etapu 4, "Co przetestowac". Wzorzec: QuireRef
//  (quire jako BigInt, bez wyrownania, segmentow i przeniesien) i
//  PositRef.round na dokladnej wartosci. Sprawdzany jest wynik, flagi
//  i surowy quire (U2, quireWidth bitow).
// =====================================================================
object PositQuirePlan {
  val plan : Seq[Testpoint] = Seq(
    Testpoint("quire_ref_known", Stage.V1,
      "QuireRef na znanych przypadkach",
      checking = Seq("duza + mala - duza == mala dokladnie", "iloczyn dlugosci 1 == mul PositRef (wynik i flagi)",
                     "NaR w dowolnym elemencie -> NaR", "quireWidth(64) == 16 n dla es = 2")),

    Testpoint("quire_p8_single", Stage.V1,
      "posit<8,2>: wszystkie 65 536 par jako wektory dlugosci 1",
      stimulus = Seq("clear, jeden element dot, round; to samo dla sum (jeden element a)"),
      checking = Seq("dot: wynik i flagi == PositRef mul", "sum: wynik == a, bez flag (NaR: tylko nar)")),

    Testpoint("quire_random", Stage.V1,
      "Losowe wektory posit<16,2> i posit<32,2>, dot i sum",
      stimulus = Seq("dlugosci 1..300, losowe bity + pary bliskie znoszenia sie", "przerwy w valid"),
      checking = Seq("wynik i flagi == QuireRef.round", "surowy quire == QuireRef.raw")),

    Testpoint("quire_cancellation", Stage.V2,
      "Znoszenie sie i przeniesienia przez wszystkie segmenty",
      stimulus = Seq("maxpos^2 + minpos^2 - maxpos^2", "-minpos^2 + minpos^2 (pozyczka przez caly quire)",
                     "naprzemiennie +-maxpos^2 i +-minpos^2"),
      checking = Seq("wynik == QuireRef (np. dokladnie minpos^2 -> minpos z flaga sat)", "surowy quire == QuireRef.raw")),

    Testpoint("quire_nar", Stage.V2,
      "NaR w srodku wektora",
      checking = Seq("wynik NaR, io.nar = 1", "po clear nastepny wektor liczy sie normalnie")),

    Testpoint("quire_continue", Stage.V2,
      "Odczyt w trakcie i kontynuacja (tablice dluzsze niz jeden transfer DMA)",
      checking = Seq("odczyt po pierwszej czesci == QuireRef(czesc 1)", "odczyt po calosci == QuireRef(calosc)")),

    Testpoint("quire_permutation", Stage.V2,
      "Niezaleznosc od kolejnosci",
      checking = Seq("ten sam wektor po permutacji: te same bity wyniku i ten sam surowy quire")),

    Testpoint("quire_long", Stage.V2,
      "Dlugi wektor: zapas bitow przeniesien",
      stimulus = Seq("posit<16,2>: 200 000 iloczynow maxpos * maxpos"),
      checking = Seq("surowy quire == QuireRef.raw (bez przepelnienia)", "wynik maxpos z flaga sat")),

    Testpoint("quire_throughput", Stage.V2,
      "1 element / takt",
      checking = Seq("2000 elementow bez przerw przyjete w 2000 taktach (input.ready stale 1 poza odczytem)",
                     "odczyt (round) konczy sie w <= segs + 10 taktach od ustalenia"))
  )
}

class PositQuireTestplan extends TestplanSuite {
  import PositQuirePlan._
  override def testplan = plan

  val p8  = PositConfig(8)
  val p16 = PositConfig(16)
  val p32 = PositConfig(32)

  def sim(c : PositConfig) = SimEnv(Conventions.spinal, s"quire_${c.n}_${c.es}").compile(new PositQuire(PositQuireConfig(c)))
  lazy val q8  = sim(p8)
  lazy val q16 = sim(p16)
  lazy val q32 = sim(p32)

  type Term = (BigInt, Option[BigInt])     // (a, Some(b)) = a * b, (a, None) = a

  class Env(val d : PositQuire, val rnd : Random) {
    val c  = d.c
    val cd = d.clockDomain
    var cycles = 0L
    fork { while (true) { cd.waitSampling(); cycles += 1 } }

    def clear() : Unit = { d.io.clear #= true; cd.waitSampling(); d.io.clear #= false }

    def feed(terms : Seq[Term], gaps : Boolean = true) : Unit = for ((a, b) <- terms) {
      if (gaps && rnd.nextInt(4) == 0) cd.waitSampling(1 + rnd.nextInt(2))
      d.io.input.valid #= true
      d.io.input.a #= a
      d.io.input.b #= b.getOrElse(BigInt(0))
      d.io.input.dot #= b.nonEmpty
      cd.waitSamplingWhere(d.io.input.ready.toBoolean)
      d.io.input.valid #= false
    }

    /** round -> (bity, flagi, surowy quire). */
    def round() : (BigInt, Int, BigInt) = {
      d.io.round #= true; cd.waitSampling(); d.io.round #= false
      cd.waitSamplingWhere(d.io.result.valid.toBoolean)
      val f = d.io.result.flags
      val flags = (if (f.nar.toBoolean) 1 else 0) | (if (f.sat.toBoolean) 2 else 0) | (if (f.inexact.toBoolean) 4 else 0)
      (d.io.result.bits.toBigInt, flags, d.io.raw.toBigInt)
    }

    def check(terms : Seq[Term], label : String) : Unit = {
      val (q, nar) = QuireRef.accumulate(c, terms.iterator)
      val (want, wantF) = QuireRef.round(c, q, nar)
      val (got, gotF, raw) = round()
      assert(got == want && gotF == wantF.bits,
        f"$c $label: 0x$got%x f=$gotF, oczekiwane 0x$want%x f=${wantF.bits} (${terms.size} skladnikow)")
      if (!nar) assert(raw == QuireRef.raw(c, q, d.qc.width), s"$c $label: surowy quire")
    }
  }

  def run(compiled : SimCompiled[PositQuire], label : String)(body : Env => Unit) : Unit =
    compiled.doSim(label, seed = SimSeed(label)) { d =>
      d.io.input.valid #= false
      d.io.clear #= false
      d.io.round #= false
      d.clockDomain.forkStimulus(10)
      d.clockDomain.waitSampling(4)
      body(new Env(d, new Random(SimSeed(label))))
    }

  def randomTerms(c : PositConfig, n : Int, dot : Boolean, rnd : Random) : Seq[Term] =
    stress(c, n, rnd).map { case (a, b) => (a, if (dot) Some(b) else None) }.toSeq

  // ---- bez symulacji --------------------------------------------------------------

  testpoint("quire_ref_known") {
    for (n <- Seq(8, 16, 32)) assert(PositConfig(n).quireWidth(64) == 16 * n)
    val c = p16
    val big = BigInt(0x7000); val small = BigInt(0x0123)
    val (q, nar) = QuireRef.accumulate(c, Iterator((big, None), (small, None), ((-big) & 0xffff, None)))
    assert(!nar && QuireRef.round(c, q, nar)._1 == small)
    for ((a, b) <- stress(c, 2000, new Random(1))) {
      val (q1, n1) = QuireRef.accumulate(c, Iterator((a, Some(b))))
      val r = QuireRef.round(c, q1, n1)
      assert(r == PositRef.withFlags(c, PositOp.Mul, a, b), f"a=0x$a%x b=0x$b%x")
    }
    assert(QuireRef.accumulate(c, Iterator((big, None), (BigInt(0x8000), None)))._2)
  }

  // ---- symulacje --------------------------------------------------------------------

  testpoint("quire_p8_single") {
    run(q8, "quire_p8_single") { e =>
      for ((a, b) <- exhaustive(p8)) {
        e.clear(); e.feed(Seq((a, Some(b))), gaps = false)
        val (got, f, _) = e.round()
        val (want, wf) = PositRef.withFlags(p8, PositOp.Mul, a, b)
        assert(got == want && f == wf.bits, f"dot 0x$a%x * 0x$b%x -> 0x$got%x f=$f, mul 0x$want%x f=${wf.bits}")
      }
      for (a <- 0 until 256) {
        e.clear(); e.feed(Seq((BigInt(a), None)), gaps = false)
        val (got, f, _) = e.round()
        assert(got == a && f == (if (a == 0x80) 1 else 0), f"sum 0x$a%x -> 0x$got%x f=$f")
      }
    }
  }

  for ((name, compiled, c) <- Seq(("p16", () => q16, p16), ("p32", () => q32, p32)); dot <- Seq(true, false)) {
    testpoint("quire_random", s"${name}_${if (dot) "dot" else "sum"}") {
      run(compiled(), s"quire_random_${name}_$dot") { e =>
        for (_ <- 0 until 60) {
          val terms = randomTerms(c, 1 + e.rnd.nextInt(300), dot, e.rnd)
          e.clear(); e.feed(terms); e.check(terms, "losowy")
        }
      }
    }
  }

  testpoint("quire_cancellation") {
    run(q32, "quire_cancel") { e =>
      val maxpos = BigInt(0x7fffffff); val minpos = BigInt(1)
      def negP(x : BigInt) = (-x) & 0xffffffffL
      val cases : Seq[Seq[Term]] = Seq(
        Seq((maxpos, Some(maxpos)), (minpos, Some(minpos)), (negP(maxpos), Some(maxpos))),
        Seq((negP(minpos), Some(minpos)), (minpos, Some(minpos))),
        Seq((negP(minpos), Some(minpos))),
        Seq.tabulate(40)(i => if (i % 2 == 0) (if (i % 4 == 0) maxpos else negP(maxpos), Some(maxpos))
                              else (if (i % 3 == 0) minpos else negP(minpos), Some(minpos))),
        Seq((negP(minpos), Some(minpos)), (maxpos, Some(maxpos)), (negP(maxpos), Some(maxpos)), (minpos, Some(minpos))))
      for ((terms, i) <- cases.zipWithIndex) {
        e.clear(); e.feed(terms, gaps = false); e.check(terms, s"przypadek $i")
        e.clear(); e.feed(terms); e.check(terms, s"przypadek $i z przerwami")
      }
    }
  }

  testpoint("quire_nar") {
    run(q16, "quire_nar") { e =>
      val terms = randomTerms(p16, 50, dot = true, e.rnd).updated(20, (BigInt(0x8000), Some(BigInt(0x4000))))
      e.clear(); e.feed(terms)
      val (got, f, _) = e.round()
      assert(got == 0x8000 && f == 1 && e.d.io.nar.toBoolean, "NaR")
      val next = randomTerms(p16, 50, dot = true, e.rnd)
      e.clear(); e.feed(next); e.check(next, "po clear")
    }
  }

  testpoint("quire_continue") {
    run(q32, "quire_continue") { e =>
      val part1 = randomTerms(p32, 150, dot = true, e.rnd)
      val part2 = randomTerms(p32, 150, dot = true, e.rnd)
      e.clear(); e.feed(part1); e.check(part1, "czesc 1")
      e.feed(part2); e.check(part1 ++ part2, "calosc")
    }
  }

  testpoint("quire_permutation") {
    run(q32, "quire_perm") { e =>
      val terms = randomTerms(p32, 300, dot = true, e.rnd)
      e.clear(); e.feed(terms); val r1 = e.round()
      e.clear(); e.feed(e.rnd.shuffle(terms)); val r2 = e.round()
      assert(r1 == r2, "permutacja zmienila wynik albo quire")
      e.clear(); e.feed(terms); e.check(terms, "wzorzec")
    }
  }

  testpoint("quire_long") {
    run(q16, "quire_long") { e =>
      val terms = Seq.fill(200000)((BigInt(0x7fff), Some(BigInt(0x7fff))))
      e.clear(); e.feed(terms, gaps = false); e.check(terms, "200k x maxpos^2")
    }
  }

  testpoint("quire_throughput") {
    run(q32, "quire_tput") { e =>
      val d = e.d
      val terms = randomTerms(p32, 2000, dot = true, e.rnd)
      e.clear()
      val t0 = e.cycles
      for ((a, b) <- terms) {
        d.io.input.valid #= true; d.io.input.a #= a; d.io.input.b #= b.get; d.io.input.dot #= true
        e.cd.waitSampling()
        assert(d.io.input.ready.toBoolean, "input.ready opadl w trakcie akumulacji")
      }
      d.io.input.valid #= false
      assert(e.cycles - t0 == terms.size, s"${terms.size} elementow w ${e.cycles - t0} taktach")
      e.cd.waitSamplingWhere(!d.io.busy.toBoolean)
      val t1 = e.cycles
      d.io.round #= true; e.cd.waitSampling(); d.io.round #= false
      e.cd.waitSamplingWhere(d.io.result.valid.toBoolean)
      assert(e.cycles - t1 <= d.qc.segs + 10, s"odczyt ${e.cycles - t1} taktow")
      info(s"odczyt ${e.cycles - t1} taktow (${d.qc})")
      e.check(terms, "przepustowosc")
    }
  }
}
