package posit.vector

import spinal.core._
import spinal.core.sim._
import scala.util.Random
import newhope.vertebra._
import newhope.vertebra.sim.{SimEnv, SimSeed}
import posit._
import posit.PositSimKit.stress
import AluSimKit._

// =====================================================================
//  PositAlu, etap 5: sqrt, FMA / AXPY, konwersje float32 <-> posit32.
//
//  Zrodlo planu: plan etapu 5, "Co przetestowac". Wzorce: PositRef.sqrt,
//  .fma, .fromFloat32, .toFloat32 (te same skroty co ref.py i vecops).
// =====================================================================
object PositAluStage5Plan {
  val plan : Seq[Testpoint] = Seq(
    Testpoint("sqrt_exhaustive", Stage.V1,
      "sqrt: wszystkie posit<8,2> i posit<16,2>",
      checking = Seq("wynik i flagi == PositRef.sqrt", "ujemne -> NaR (flaga nar), 0 -> 0, NaR -> NaR")),

    Testpoint("sqrt_p32", Stage.V2,
      "sqrt posit<32,2> losowo i latencja",
      checking = Seq("wynik i flagi == PositRef.sqrt", "odstep wynikow przy ciaglym strumieniu == sqrtCycles")),

    Testpoint("fma_single_rounding", Stage.V1,
      "FMA zaokragla raz",
      stimulus = Seq("c bliskie -a*b (znoszenie sie), losowe trojki posit<16,2> i posit<32,2>"),
      checking = Seq("wynik i flagi == PositRef.fma", "sa przypadki, w ktorych mul, potem add daje inny wynik")),

    Testpoint("convert_f2p", Stage.V1,
      "float32 -> posit32",
      stimulus = Seq("0, -0, subnormale, najmniejsza i najwieksza normalna, NaN (cicha i sygnalizujaca), inf, -inf",
                     "wartosci przy maxpos i minpos, losowe wzorce bitow"),
      checking = Seq("wynik i flagi == PositRef.fromFloat32 (NaN, inf -> NaR; poza zakresem: nasycenie)")),

    Testpoint("convert_p2f", Stage.V1,
      "posit32 -> float32",
      stimulus = Seq("0, NaR, maxpos, minpos, +-1, losowe"),
      checking = Seq("bity == PositRef.toFloat32 (RNE, NaR -> 0x7fc00000)", "flaga inexact gdy float != posit")),

    Testpoint("stage5_mixed", Stage.V2,
      "Wszystkie operacje przeplatane w jednym strumieniu",
      stimulus = Seq("losowy kod (add..div, sqrt, fma, axpy, f2p, p2f), losowe przerwy w valid i ready"),
      checking = Seq("wynik i flagi bit w bit, kolejnosc zachowana mimo roznych latencji")),

    Testpoint("stage5_not_built", Stage.V2,
      "ALU bez operacji etapu 5",
      checking = Seq("kody 7..11 daja NaR z flaga nar", "opMask bez bitow 7..11"))
  )
}

class PositAluStage5Testplan extends TestplanSuite {
  import PositAluStage5Plan._
  override def testplan = plan

  val p8  = PositConfig(8)
  val p16 = PositConfig(16)
  val p32 = PositConfig(32)
  import PositAluOp.{SQRT, FMA, AXPY, F2P, P2F}

  def full(c : PositConfig, k : Int = 2, pipe : PositPipeline = PositPipeline.full) =
    PositAluConfig(c, PositOp.all, k, pipe, sqrt = true, fma = true, convert = c.n == 32)
  def tag(cfg : PositAluConfig) = s"alu5_${cfg.c.n}_k${cfg.divBitsPerCycle}_m${cfg.opMask}_${cfg.pipe.latency}"
  def sim(cfg : PositAluConfig) = SimEnv(Conventions.spinal, tag(cfg)).compile(new PositAlu(cfg))
  lazy val a8  = sim(full(p8))
  lazy val a16 = sim(full(p16))
  lazy val a32 = sim(full(p32, k = 4))

  def runOn(compiled : SimCompiled[PositAlu], label : String, gen : Random => Iterator[Cmd],
            readyFactor : Float = 0.5f, gaps : Boolean = true) : Run = {
    var r : Run = null
    compiled.doSim(label, seed = SimSeed(label)) { d =>
      r = drive(d, gen(new Random(SimSeed(label))), label, readyFactor, _ => (), gaps)
    }
    info(s"${r.checked} polecen, flagi ${r.flagCounts.toSeq.sorted.map { case (k, v) => s"$k=$v" }.mkString(" ")}")
    r
  }

  def unary(op : Int, xs : Iterator[BigInt]) : Iterator[Cmd] = xs.map(a => Cmd(op, a, 0))

  // ---- sqrt ---------------------------------------------------------------------------

  testpoint("sqrt_exhaustive", "p8") {
    assert(runOn(a8, "sqrt_p8", _ => unary(SQRT, Iterator.range(0, 256).map(BigInt(_)))).checked == 256)
  }
  testpoint("sqrt_exhaustive", "p16") {
    assert(runOn(a16, "sqrt_p16", _ => unary(SQRT, Iterator.range(0, 65536).map(BigInt(_)))).checked == 65536)
  }
  testpoint("sqrt_p32") {
    runOn(a32, "sqrt_p32", rnd => unary(SQRT, Iterator.fill(20000)(BigInt(32, rnd))))
    val cfg = full(p32, k = 4, pipe = PositPipeline.combinational)
    val r = runOn(sim(cfg), "sqrt_p32_lat", rnd => unary(SQRT, Iterator.fill(500)(BigInt(31, rnd))), readyFactor = 1.0f, gaps = false)
    val gaps = r.fires.zip(r.fires.tail).map { case (x, y) => y - x }.distinct
    assert(gaps == Seq(cfg.sqrtCycles), s"odstepy $gaps, sqrtCycles ${cfg.sqrtCycles}")
    info(s"sqrt posit32, k=4: ${cfg.sqrtCycles} taktow (${cfg.sqrtSteps} krokow)")
  }

  // ---- FMA ----------------------------------------------------------------------------

  def fmaTriples(c : PositConfig, n : Int, rnd : Random) : Iterator[Cmd] = Iterator.fill(n) {
    val a = BigInt(c.n, rnd); val b = BigInt(c.n, rnd)
    val ab = PositRef(c, PositOp.Mul, a, b)
    val addend = rnd.nextInt(3) match {
      case 0 => BigInt(c.n, rnd)
      case 1 => (-ab) & ((BigInt(1) << c.n) - 1)                          // c = -round(a*b): znoszenie sie
      case _ => ((-ab) + rnd.nextInt(5) - 2) & ((BigInt(1) << c.n) - 1)
    }
    Cmd(if (rnd.nextBoolean()) FMA else AXPY, a, b, addend)
  }

  for ((name, compiled, c) <- Seq(("p16", () => a16, p16), ("p32", () => a32, p32))) {
    testpoint("fma_single_rounding", name) {
      val cmds = fmaTriples(c, 20000, new Random(7)).toSeq
      val differs = cmds.count { x =>
        val twoStep = PositRef(c, PositOp.Add, PositRef(c, PositOp.Mul, x.a, x.b), x.addend)
        twoStep != PositRef.fma(c, x.a, x.b, x.addend)._1
      }
      assert(differs > 100, s"tylko $differs trojek, w ktorych FMA rozni sie od mul + add")
      runOn(compiled(), s"fma_$name", _ => cmds.iterator)
      info(s"$differs z ${cmds.size} trojek: FMA != mul, potem add")
    }
  }

  // ---- konwersje ------------------------------------------------------------------------

  def f32(f : Float) : BigInt = BigInt(java.lang.Float.floatToRawIntBits(f).toLong & 0xffffffffL)

  testpoint("convert_f2p") {
    val edge = Seq(0x00000000L, 0x80000000L, 0x00000001L, 0x807fffffL, 0x00800000L, 0x7f7fffffL, 0xff7fffffL,
                   0x7fc00000L, 0x7fa00000L, 0xffc00001L, 0x7f800000L, 0xff800000L).map(BigInt(_)) ++
      Seq(1.0f, -1.0f, 1.3e36f, 1.4e36f, 3e38f, 7.6e-37f, 7.5e-37f, 1e-40f, 0.1f, 3.14159f).map(f32)
    runOn(a32, "f2p", rnd => unary(F2P, edge.iterator ++ Iterator.fill(30000)(BigInt(32, rnd))))
  }

  testpoint("convert_p2f") {
    val edge = Seq(0L, 0x80000000L, 0x7fffffffL, 0x00000001L, 0x40000000L, 0xc0000000L, 0x80000001L, 0xffffffffL).map(BigInt(_))
    runOn(a32, "p2f", rnd => unary(P2F, edge.iterator ++ Iterator.fill(30000)(BigInt(32, rnd))))
  }

  // ---- przeplot i brak operacji ------------------------------------------------------------

  testpoint("stage5_mixed") {
    val codes = Seq(0, 1, 2, 3, SQRT, FMA, AXPY, F2P, P2F)
    runOn(a32, "stage5_mixed", rnd => Iterator.fill(20000) {
      Cmd(codes(rnd.nextInt(codes.size)), BigInt(32, rnd), BigInt(32, rnd), BigInt(32, rnd))
    })
  }

  testpoint("stage5_not_built") {
    val cfg = PositAluConfig(p32, divBitsPerCycle = 4)
    assert((cfg.opMask >> 7) == 0, s"opMask 0x${cfg.opMask.toHexString}")
    runOn(sim(cfg), "stage5_absent", rnd => Iterator.fill(2000)(Cmd(7 + rnd.nextInt(5), BigInt(32, rnd), BigInt(32, rnd), BigInt(32, rnd))))
  }
}
