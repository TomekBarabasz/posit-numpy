package posit.kv260

import spinal.core._
import spinal.core.sim._
import spinal.lib.bus.amba4.axilite.sim.AxiLite4Driver
import scala.util.Random
import newhope.vertebra._
import newhope.vertebra.sim.{SimEnv, SimSeed}
import posit._
import posit.PositSimKit.stress
import posit.vector._
import PositAluRegs._
import PositVecRegs._

// =====================================================================
//  PositVecAxi 3.0: quire przez rejestry i AXI4-Stream (etap 4).
//
//  Sekwencja jak w backend_hw (PositVecHw.reduce): OP = SUM/DOT/SUMSQ,
//  CTRL.quire_clear, tablica przez s_axis (jak MM2S, bez S2MM), czekanie
//  na CNT_ELEMENTS, CTRL.quire_round, STATUS.done, RESULT i QUIRE[k].
//  Wzorzec: QuireRef.
// =====================================================================
object PositVecQuirePlan {
  val plan : Seq[Testpoint] = Seq(
    Testpoint("vecq_config", Stage.V1,
      "Rejestry konfiguracji quire",
      checking = Seq("QUIRE_CFG: szerokosc 16 n i quireFrac", "CONFIG.has_quire, maska z bitami 4..6")),

    Testpoint("vecq_reduce", Stage.V1,
      "sum, dot i sumsq tablic przez s_axis",
      stimulus = Seq("posit<16,2> i posit<32,2>, dlugosci 1, 2, 3, 1000 i losowe", "losowe przerwy w tvalid"),
      checking = Seq("RESULT i flagi == QuireRef.round", "QUIRE[k] == QuireRef.raw", "CNT_ELEMENTS == liczba elementow",
                     "m_axis_tvalid = 0 przez caly czas (redukcja nie ma wynikow strumieniowych)")),

    Testpoint("vecq_continue", Stage.V2,
      "Dwie porcje, jeden quire (dwa transfery DMA)",
      checking = Seq("round po pierwszej porcji == QuireRef(porcja 1)", "round po obu == QuireRef(calosc)")),

    Testpoint("vecq_nar_clear", Stage.V2,
      "NaR w tablicy i zerowanie",
      checking = Seq("RESULT == NaR, STATUS.quire_nar = 1", "po quire_clear: quire_nar = 0, kolejna suma poprawna")),

    Testpoint("vecq_mixed", Stage.V2,
      "Na przemian tablice element po elemencie i redukcje",
      checking = Seq("mul tablic, potem dot, potem add tablic - wszystko bit w bit",
                     "pojedyncze dzialanie z rejestrow z kodem sum: STATUS.error"))
  )
}

class PositVecQuireTestplan extends TestplanSuite {
  import PositVecQuirePlan._
  override def testplan = plan

  val p16 = PositConfig(16)
  val p32 = PositConfig(32)
  def sim(c : PositConfig, name : String) =
    SimEnv(Conventions.spinal, name).compile(new PositVecAxi(PositAluConfig(c, divBitsPerCycle = 4, pipe = PositPipeline.full)))
  lazy val v16 = sim(p16, "vecq_16")
  lazy val v32 = sim(p32, "vecq_32")

  class Env(val d : PositVecAxi, val rnd : Random) {
    val c   = d.c
    val cd  = d.clockDomain
    val axi = AxiLite4Driver(d.io.s_axi, cd)
    val width = PositQuireConfig(c).width
    def rd(a : Int) : Long = axi.read(a).toLong
    def wr(a : Int, v : Long) : Unit = axi.write(a, v)
    var mValid = false
    d.io.m_axis.tready #= true
    d.io.s_axis.tvalid #= false
    cd.onSamplings { if (d.io.m_axis.tvalid.toBoolean) mValid = true }

    def send(pairs : Seq[(BigInt, BigInt)]) : Unit = {
      for (((a, b), i) <- pairs.zipWithIndex) {
        if (rnd.nextInt(3) == 0) cd.waitSampling(1 + rnd.nextInt(3))
        d.io.s_axis.tdata #= (b << c.n) | a
        d.io.s_axis.tlast #= i == pairs.size - 1
        d.io.s_axis.tvalid #= true
        cd.waitSamplingWhere(d.io.s_axis.tready.toBoolean)
        d.io.s_axis.tvalid #= false
      }
    }

    /** quire_clear (opcjonalnie), tablica, round; zwraca (wynik, flagi, quire). */
    def reduce(op : Int, pairs : Seq[(BigInt, BigInt)], clear : Boolean = true) : (BigInt, Int, BigInt) = {
      wr(OP, op)
      if (clear) wr(CTRL, 1 << CTRL_QUIRE_CLEAR)
      val before = rd(CNT_ELEMENTS)
      send(pairs)
      var n = 0
      while (rd(CNT_ELEMENTS) - before < pairs.size) { n += 1; assert(n < 1000, "CNT_ELEMENTS nie doszedl") }
      wr(CTRL, 1 << CTRL_QUIRE_ROUND)
      var s = rd(STATUS); n = 0
      while ((s & (1 << STATUS_DONE)) == 0) { n += 1; assert(n < 1000, "brak STATUS.done po quire_round"); s = rd(STATUS) }
      val raw = (0 until width / 32).map(k => BigInt(rd(QUIRE + 4 * k))).zipWithIndex
        .map { case (w, k) => w << (32 * k) }.sum
      (BigInt(rd(RESULT)), ((s >> STATUS_FLAGS_SHIFT) & 7).toInt, raw)
    }

    def terms(op : Int, pairs : Seq[(BigInt, BigInt)]) : Seq[(BigInt, Option[BigInt])] = pairs.map { case (a, b) =>
      op match { case OP_SUM => (a, None); case OP_DOT => (a, Some(b)); case _ => (a, Some(a)) } }

    def check(op : Int, pairs : Seq[(BigInt, BigInt)], got : (BigInt, Int, BigInt), label : String) : Unit = {
      val (q, nar) = QuireRef.accumulate(c, terms(op, pairs).iterator)
      val (want, wf) = QuireRef.round(c, q, nar)
      assert(got._1 == want && got._2 == wf.bits, f"$c $label op=$op: 0x${got._1}%x f=${got._2}, oczekiwane 0x$want%x f=${wf.bits}")
      if (!nar) assert(got._3 == QuireRef.raw(c, q, width), s"$c $label op=$op: QUIRE[k]")
    }
  }

  def run(compiled : SimCompiled[PositVecAxi], label : String)(body : Env => Unit) : Unit =
    compiled.doSim(label, seed = SimSeed(label)) { d =>
      d.clockDomain.forkStimulus(10)
      val e = new Env(d, new Random(SimSeed(label)))
      d.clockDomain.waitSampling(10)
      body(e)
    }

  testpoint("vecq_config") {
    run(v32, "vecq_config") { e =>
      assert(e.rd(QUIRE_CFG) == ((240L << 16) | 512), f"QUIRE_CFG = 0x${e.rd(QUIRE_CFG)}%x")
      val w = e.rd(CONFIG)
      assert(((w >> CONFIG_HAS_QUIRE) & 1) == 1 && ((w >> 16) & REDUCE_MASK) == REDUCE_MASK, f"CONFIG = 0x$w%x")
    }
  }

  for ((name, compiled, c) <- Seq(("p16", () => v16, p16), ("p32", () => v32, p32))) {
    testpoint("vecq_reduce", name) {
      run(compiled(), s"vecq_reduce_$name") { e =>
        e.wr(CTRL, 1 << CTRL_CLEAR_COUNTERS)
        var total = 0L
        for (op <- Seq(OP_SUM, OP_DOT, OP_SUMSQ); n <- Seq(1, 2, 3, 1000, 1 + e.rnd.nextInt(300))) {
          val pairs = stress(c, n, e.rnd).toSeq
          e.check(op, pairs, e.reduce(op, pairs), s"n=$n")
          total += n
        }
        assert(e.rd(CNT_ELEMENTS) == total, s"CNT_ELEMENTS ${e.rd(CNT_ELEMENTS)}, wyslano $total")
        assert(!e.mValid, "m_axis_tvalid w trakcie redukcji")
      }
    }
  }

  testpoint("vecq_continue") {
    run(v32, "vecq_continue") { e =>
      val p1 = stress(p32, 300, e.rnd).toSeq
      val p2 = stress(p32, 300, e.rnd).toSeq
      e.check(OP_DOT, p1, e.reduce(OP_DOT, p1), "porcja 1")
      e.check(OP_DOT, p1 ++ p2, e.reduce(OP_DOT, p2, clear = false), "obie porcje")
    }
  }

  testpoint("vecq_nar_clear") {
    run(v16, "vecq_nar") { e =>
      val pairs = stress(p16, 100, e.rnd).toSeq.updated(40, (BigInt(0x8000), BigInt(0x4000)))
      val r = e.reduce(OP_SUM, pairs)
      assert(r._1 == 0x8000 && r._2 == 1 && ((e.rd(STATUS) >> STATUS_QUIRE_NAR) & 1) == 1, "NaR")
      val next = stress(p16, 100, e.rnd).toSeq
      val r2 = e.reduce(OP_SUM, next)
      assert(((e.rd(STATUS) >> STATUS_QUIRE_NAR) & 1) == 0, "quire_nar po clear")
      e.check(OP_SUM, next, r2, "po clear")
    }
  }

  testpoint("vecq_mixed") {
    run(v16, "vecq_mixed") { e =>
      val d = e.d
      val got = scala.collection.mutable.ArrayBuffer[BigInt]()
      e.cd.onSamplings { if (d.io.m_axis.tvalid.toBoolean && d.io.m_axis.tready.toBoolean) got += d.io.m_axis.tdata.toBigInt }
      def array(op : PositOp, pairs : Seq[(BigInt, BigInt)]) : Unit = {
        e.wr(OP, PositAluOp.code(op)); e.wr(STREAM_CFG, 0); got.clear()
        e.send(pairs)
        e.cd.waitSamplingWhere(got.size == pairs.size)
        for (((a, b), r) <- pairs.zip(got)) assert(r == PositRef(p16, op, a, b), s"${op.name}")
      }
      array(PositOp.Mul, stress(p16, 200, e.rnd).toSeq)
      val pairs = stress(p16, 200, e.rnd).toSeq
      e.check(OP_DOT, pairs, e.reduce(OP_DOT, pairs), "dot w srodku")
      array(PositOp.Add, stress(p16, 200, e.rnd).toSeq)
      e.wr(OP, OP_SUM); e.wr(CTRL, 1 << CTRL_START)
      val s = e.rd(STATUS)
      assert((s & 7) == ((1 << STATUS_DONE) | (1 << STATUS_ERROR)), f"start z OP_SUM: STATUS 0x$s%x")
    }
  }
}
