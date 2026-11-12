package posit.vector

import spinal.core._
import spinal.core.sim._
import scala.util.Random
import newhope.vertebra._
import newhope.vertebra.sim.{SimEnv, SimSeed}
import posit._
import posit.PositSimKit.{exhaustive, stress, vectors}
import AluSimKit._

// =====================================================================
//  PositAlu: wspolny dekoder i enkoder, add/sub/mul/div pod kodem
//  operacji, flagi nar/sat/inexact.
//
//  Zrodlo planu: plan etapu 1 (posit-numpy na KV260). Wzorzec:
//  PositRef.withFlags; wynik i flagi bit w bit, w kolejnosci polecen.
//  StreamConformance dla portu "rsp".
// =====================================================================
object PositAluPlan {
  val plan : Seq[Testpoint] = Seq(
    Testpoint("alu_flags_ref", Stage.V1,
      "Flagi PositRef na znanych przypadkach (wzorzec sprawdzony, zanim posluzy za wzorzec)",
      checking = Seq("x / 0 i NaR: tylko nar", "maxpos * maxpos: sat i inexact, wynik maxpos",
                     "minpos * minpos: sat i inexact, wynik minpos", "1 / 3: tylko inexact",
                     "1 + 1, maxpos * 1, 0 * x: brak flag")),

    Testpoint("alu_p8_exhaustive", Stage.V1,
      "posit<8,2>: wszystkie 65 536 par dla kazdej operacji",
      stimulus = Seq("kazda para (a, b) z tym samym kodem operacji", "losowe przerwy w valid, losowy ready"),
      checking = Seq("wynik i flagi == PositRef.withFlags bit w bit, kolejnosc zachowana")),

    Testpoint("alu_mixed_ops", Stage.V1,
      "Losowa operacja w kazdym poleceniu, dzielenie przeplatane z add/sub/mul",
      stimulus = Seq("posit<16,2>, losowy kod operacji na kazda pare", "losowe przerwy w valid, losowy ready"),
      checking = Seq("wynik i flagi bit w bit", "kolejnosc wynikow == kolejnosc polecen mimo roznej latencji")),

    Testpoint("alu_p16_random", Stage.V2,
      "posit<16,2>, kazda operacja osobno",
      stimulus = Seq("losowe wzorce bitow + pary bliskie znoszenia sie"),
      checking = Seq("wynik i flagi bit w bit")),

    Testpoint("alu_p32_random", Stage.V2,
      "posit<32,2>, kazda operacja osobno, dzielnik k = 4",
      stimulus = Seq("losowe wzorce bitow + pary bliskie znoszenia sie"),
      checking = Seq("wynik i flagi bit w bit")),

    Testpoint("alu_div_latency", Stage.V2,
      "Dzielnik iteracyjny: k bitow ilorazu na takt",
      stimulus = Seq("posit<16,2>, k = 1, 3, 16 (jeden krok)", "ready stale 1, polecenia bez przerw"),
      checking = Seq("wynik bit w bit", "odstep kolejnych wynikow == PositAluConfig.divCycles (tolerancja 0)")),

    Testpoint("alu_pipeline_variants", Stage.V2,
      "PositPipeline.combinational i PositPipeline.full",
      stimulus = Seq("posit<16,2>, losowe operacje"),
      checking = Seq("wynik i flagi bit w bit w obu ukladach potoku")),

    Testpoint("alu_ops_subset", Stage.V2,
      "ALU zbudowane z czesci operacji (add, mul)",
      stimulus = Seq("wszystkie kody 0..15, takze sub, div i nieznane"),
      checking = Seq("add i mul bit w bit", "pozostale kody: NaR z flaga nar, bez zakleszczenia"))
  ) ++ StreamConformance.testpoints("rsp")
}

class PositAluTestplan extends TestplanSuite {
  import PositAluPlan._
  override def testplan = plan

  val p8  = PositConfig(8)
  val p16 = PositConfig(16)
  val p32 = PositConfig(32)

  def tag(cfg : PositAluConfig) : String = {
    val p = cfg.pipe
    val pipe = Seq(p.afterDecode, p.afterOp, p.afterEncode).map(if (_) "1" else "0").mkString
    s"alu_${cfg.c.n}_${cfg.c.es}_k${cfg.divBitsPerCycle}_p${pipe}_m${cfg.opMask}"
  }
  def sim(cfg : PositAluConfig) = SimEnv(Conventions.spinal, tag(cfg)).compile(new PositAlu(cfg))

  def runOn(compiled : SimCompiled[PositAlu], label : String, gen : Random => Iterator[Cmd],
            readyFactor : Float = 0.5f, extra : (PositAlu, ClockDomain) => Unit = (_, _) => (),
            gaps : Boolean = true) : Run = {
    var r : Run = null
    compiled.doSim(label, seed = SimSeed(label)) { d =>
      r = drive(d, gen(new Random(SimSeed(label))), label, readyFactor, cd => extra(d, cd), gaps)
    }
    info(s"${r.checked} polecen, ${r.cycles} taktow, flagi ${r.flagCounts.toSeq.sorted.map { case (k, v) => s"$k=$v" }.mkString(" ")}")
    r
  }

  // ---- bez symulacji --------------------------------------------------------------

  testpoint("alu_flags_ref") {
    import PositRef.Flags
    def f(op : PositOp, a : Int, b : Int) = { val (r, fl) = PositRef.withFlags(p8, op, a, b); (r.toInt, fl) }
    val one = 0x40; val three = 0x4c; val nar = 0x80; val maxpos = 0x7f; val minpos = 0x01
    assert(f(PositOp.Div, one, 0)        == ((nar, Flags(nar = true))))
    assert(f(PositOp.Add, nar, one)      == ((nar, Flags(nar = true))))
    assert(f(PositOp.Mul, maxpos, maxpos) == ((maxpos, Flags(sat = true, inexact = true))))
    assert(f(PositOp.Mul, minpos, minpos) == ((minpos, Flags(sat = true, inexact = true))))
    assert(f(PositOp.Div, one, three)._2  == Flags(inexact = true))
    assert(f(PositOp.Add, one, one)       == ((0x48, Flags())))
    assert(f(PositOp.Mul, maxpos, one)    == ((maxpos, Flags())))
    assert(f(PositOp.Mul, 0, three)       == ((0, Flags())))
    // NaR ujemny: -maxpos * maxpos tez nasyca (do -maxpos)
    assert(f(PositOp.Mul, 0x81, maxpos)._2 == Flags(sat = true, inexact = true))
  }

  // ---- symulacje --------------------------------------------------------------------

  lazy val alu8  = sim(PositAluConfig(p8))
  lazy val alu16 = sim(PositAluConfig(p16, divBitsPerCycle = 2))
  lazy val alu32 = sim(PositAluConfig(p32, divBitsPerCycle = 4))

  for (op <- PositOp.all) {
    testpoint("alu_p8_exhaustive", op.name) {
      val r = runOn(alu8, s"alu_p8_${op.name}", _ => cmds(op, exhaustive(p8)))
      assert(r.checked == 65536)
    }
  }

  testpoint("alu_mixed_ops", "p16_k2") {
    runOn(alu16, "alu_mixed_p16", rnd => mixed(PositOp.all, stress(p16, vectors(100000), rnd), rnd))
  }

  for (op <- PositOp.all) {
    testpoint("alu_p16_random", op.name) {
      runOn(alu16, s"alu_p16_${op.name}", rnd => cmds(op, stress(p16, vectors(100000), rnd)))
    }
    testpoint("alu_p32_random", op.name) {
      runOn(alu32, s"alu_p32_${op.name}", rnd => cmds(op, stress(p32, vectors(100000), rnd)))
    }
  }

  // Latencja dzielenia: ALU bez rejestrow potoku, ready = 1, polecenia bez przerw.
  for (k <- Seq(1, 3, 16)) {
    testpoint("alu_div_latency", s"k$k") {
      val cfg = PositAluConfig(p16, divBitsPerCycle = k, pipe = PositPipeline.combinational)
      val r   = runOn(sim(cfg), s"alu_div_lat_k$k", rnd => cmds(PositOp.Div, stress(p16, 2000, rnd)),
                      readyFactor = 1.0f, gaps = false)
      val gaps = r.fires.zip(r.fires.tail).map { case (x, y) => y - x }.distinct.sorted
      assert(gaps == Seq(cfg.divCycles), s"k=$k: odstepy wynikow $gaps, divCycles = ${cfg.divCycles}")
      info(s"k=$k: ${cfg.divCycles} taktow na dzielenie (${cfg.divSteps} krokow)")
    }
  }

  for ((name, pipe) <- Seq("comb" -> PositPipeline.combinational, "full" -> PositPipeline.full)) {
    testpoint("alu_pipeline_variants", name) {
      runOn(sim(PositAluConfig(p16, divBitsPerCycle = 3, pipe = pipe)), s"alu_pipe_$name",
            rnd => mixed(PositOp.all, stress(p16, vectors(60000), rnd), rnd))
    }
  }

  testpoint("alu_ops_subset", "add_mul") {
    val cfg = PositAluConfig(p16, ops = Seq(PositOp.Add, PositOp.Mul))
    runOn(sim(cfg), "alu_subset", rnd => stress(p16, vectors(30000), rnd).map { case (a, b) => Cmd(rnd.nextInt(16), a, b) })
  }

  // ---- StreamConformance (rsp) ----------------------------------------------------

  def rspHandle(d : PositAlu) = StreamPortHandle(d.io.rsp.valid, d.io.rsp.ready, d.io.rsp.payload, isInput = false, "rsp")

  lazy val confAlu = sim(PositAluConfig(p16, divBitsPerCycle = 2, pipe = PositPipeline.full))

  testpoint("rsp_payload_stable") {
    runOn(confAlu, "alu_conf_stable", rnd => mixed(PositOp.all, stress(p16, 5000, rnd), rnd), readyFactor = 0.2f,
          extra = (d, cd) => StreamConformance.payloadStable(cd, rspHandle(d)))
  }

  testpoint("rsp_backpressure") {
    runOn(confAlu, "alu_conf_bp", rnd => mixed(PositOp.all, stress(p16, 5000, rnd), rnd), readyFactor = 0.05f,
          extra = (d, cd) => StreamConformance.noStall(cd, rspHandle(d), limit = 2000))
  }

  // Reset synchroniczny: valid sprawdzane od drugiego zbocza w resecie (jak w PositUnitTestplan).
  testpoint("rsp_reset_quiet") {
    confAlu.doSim("alu_conf_reset", seed = SimSeed("alu_conf_reset")) { d =>
      val cd = d.clockDomain
      d.io.cmd.valid #= false
      d.io.rsp.ready #= false
      cd.forkStimulus(10)
      cd.waitSampling(4)
      d.io.cmd.valid #= true
      d.io.cmd.op #= PositAluOp.code(PositOp.Div)
      d.io.cmd.a #= 0x4000
      d.io.cmd.b #= 0x4c00
      cd.waitSamplingWhere(d.io.rsp.valid.toBoolean)
      d.io.cmd.valid #= false
      cd.assertReset()
      cd.waitActiveEdge(2)
      assert(!d.io.rsp.valid.toBoolean, "rsp.valid w trakcie resetu")
      cd.deassertReset()
      cd.waitSampling(2)
      assert(!d.io.rsp.valid.toBoolean, "rsp.valid po resecie bez polecenia")
      // po resecie dzielnik nie moze zostac w stanie busy/done: kolejne polecenie liczy sie od zera
      d.io.rsp.ready #= true
      d.io.cmd.valid #= true
      d.io.cmd.a #= 0x4000
      d.io.cmd.b #= 0x4000
      cd.waitSamplingWhere(d.io.cmd.ready.toBoolean)
      d.io.cmd.valid #= false
      cd.waitSamplingWhere(d.io.rsp.valid.toBoolean)
      assert(d.io.rsp.bits.toBigInt == 0x4000, s"1/1 po resecie: 0x${d.io.rsp.bits.toBigInt.toString(16)}")
    }
  }

  unimplemented("rsp_stress_with_rand_reset", "V3: losowy reset w trakcie ruchu")
}
