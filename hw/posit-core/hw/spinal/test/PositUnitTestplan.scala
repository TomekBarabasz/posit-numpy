package posit

import spinal.core._
import spinal.core.sim._
import scala.util.Random
import newhope.vertebra._
import newhope.vertebra.sim.{SimEnv, SimSeed}
import PositSimKit._

// =====================================================================
//  posit-core: PositUnit (add/sub/mul/div) i PositDivider.
//
//  Zrodlo planu: tabela "Weryfikacja" z README posit-spinal; model
//  PositRef (dokladny, niezalezny od algorytmu RTL). Kazdy wynik bit w bit
//  z modelem, przy losowych przerwach w valid i losowym ready.
//  StreamConformance dla portu "rsp".
//
//  Warianty testpointu = operacje albo architektury dzielnika.
// =====================================================================
object PositUnitPlan {
  val plan : Seq[Testpoint] = Seq(
    Testpoint("posit_param_bounds", Stage.V1,
      "Szerokosci PositConfig wystarczaja, nielegalne formaty odrzucone",
      checking = Seq("scaleBits miesci skale wyniku mul/div skrajnych wartosci (2 * maxScale + 1)",
                     "PositConfig(n < es + 4) rzuca")),

    Testpoint("posit_ref_known", Stage.V1,
      "PositRef na znanych przypadkach (model sam jest testowany, zanim posluzy za wzorzec)",
      checking = Seq("1 + 1 = 2, 1 / 3 zaokraglone, x / 0 = NaR, NaR przechodzi przez kazda operacje",
                     "maxpos * maxpos = maxpos, minpos * minpos = minpos (nasycenie zamiast 0 i NaR)",
                     "remis zaokraglany do parzystej")),

    Testpoint("unit_p8_exhaustive", Stage.V1,
      "posit<8,2>, PositPipeline.full: wszystkie 65 536 par",
      stimulus = Seq("kazda para (a, b)", "losowe przerwy w valid, losowy ready"),
      checking = Seq("wynik == PositRef bit w bit, kolejnosc zachowana")),

    Testpoint("unit_p16_random", Stage.V2,
      "posit<16,2>, PositPipeline() (rejestr po operacji)",
      stimulus = Seq("losowe wzorce bitow + pary bliskie znoszenia sie (b ~ a, b ~ -a)"),
      checking = Seq("wynik == PositRef bit w bit")),

    Testpoint("unit_p32_random", Stage.V2,
      "posit<32,2>, PositPipeline.combinational",
      stimulus = Seq("losowe wzorce bitow + pary bliskie znoszenia sie"),
      checking = Seq("wynik == PositRef bit w bit")),

    Testpoint("unit_es_variants", Stage.V2,
      "Formaty spoza standardu: es = 0 i es = 3",
      stimulus = Seq("posit<8,0> add wyczerpujaco", "posit<12,3> mul losowo"),
      checking = Seq("wynik == PositRef bit w bit")),

    Testpoint("div_iterative", Stage.V2,
      "PositDivider(DivArch.Iterative(k))",
      stimulus = Seq("posit<8,2> k = 1 i k = 8 (jeden takt) wyczerpujaco", "posit<16,2> k = 3 losowo"),
      checking = Seq("wynik == PositRef bit w bit", "x / 0 = NaR, 0 / x = 0")),

    Testpoint("div_pipelined", Stage.V2,
      "PositDivider(DivArch.Pipelined(k))",
      stimulus = Seq("posit<16,2> k = 5", "posit<32,2> k = 4", "ready stale 1"),
      checking = Seq("wynik == PositRef bit w bit", "przepustowosc 1 wynik / takt przy ready = 1"))
  ) ++ StreamConformance.testpoints("rsp")
}

class PositUnitTestplan extends TestplanSuite {
  import PositUnitPlan._
  override def testplan = plan

  val p8  = PositConfig(8)
  val p16 = PositConfig(16)
  val p32 = PositConfig(32)

  def unitSim(c : PositConfig, op : PositOp, pipe : PositPipeline, tag : String) =
    SimEnv(Conventions.spinal, s"${op.name}_${c.n}_${c.es}_$tag").compile(new PositUnit(c, op, pipe))

  def divSim(c : PositConfig, arch : DivArch) = {
    val tag = arch match { case DivArch.Iterative(k) => s"iter$k"; case DivArch.Pipelined(k) => s"pipe$k" }
    SimEnv(Conventions.spinal, s"div_${c.n}_${c.es}_$tag").compile(new PositDivider(c, arch))
  }

  def runOn(sim : SimCompiled[_ <: PositStreamComponent], op : PositOp, label : String,
            vecs : Random => Iterator[Pair], readyFactor : Float = 0.5f,
            extra : (PositStreamComponent, ClockDomain) => Unit = (_, _) => ()) : Run = {
    var r : Run = null
    sim.doSim(label, seed = SimSeed(label)) { d =>
      r = drive(d, op, vecs(new Random(SimSeed(label))), label, readyFactor, cd => extra(d, cd))
    }
    info(f"${r.checked}%d wektorow, ${r.cycles}%d taktow")
    r
  }

  // ---- bez symulacji --------------------------------------------------------------

  testpoint("posit_param_bounds") {
    for (c <- Seq(PositConfig(8, 0), p8, PositConfig(12, 3), p16, PositConfig(24), p32)) {
      val maxScale = c.regimeMax << c.es
      assert(2 * maxScale + 1 < (1 << (c.scaleBits - 1)), s"$c: scaleBits = ${c.scaleBits} za malo")
    }
    assertThrows[IllegalArgumentException](PositConfig(5, 2))
  }

  testpoint("posit_ref_known") {
    def r(op : PositOp, a : Int, b : Int) = PositRef(p8, op, a, b).toInt
    val one = 0x40; val two = 0x48; val three = 0x4c; val nar = 0x80; val maxpos = 0x7f; val minpos = 0x01
    assert(r(PositOp.Add, one, one) == two)
    assert(r(PositOp.Div, two, two) == one)
    assert(r(PositOp.Div, one, 0) == nar)
    assert(r(PositOp.Div, 0, three) == 0)
    for (op <- PositOp.all) { assert(r(op, nar, one) == nar); assert(r(op, one, nar) == nar) }
    assert(r(PositOp.Mul, maxpos, maxpos) == maxpos)
    assert(r(PositOp.Mul, minpos, minpos) == minpos)
    // 1/3: dokladnie miedzy dwoma positami nie lezy, wynik niedokladny, ale skonczony i < 1
    val third = r(PositOp.Div, one, three)
    assert(third > 0 && third < one)
    // remis do parzystej: posit<8,2> w [1, 2) ma 3 bity ulamka; 1 + 1/16 lezy w polowie
    // miedzy 1 (0x40, parzysty) i 1 + 1/8 (0x41), wiec zaokragla sie do 0x40
    val sixteenth = PositRef.encode(p8, Some(PositRef.Dyadic(1, -4)))
    assert(r(PositOp.Add, one, sixteenth.toInt) == one)
  }

  // ---- PositUnit ------------------------------------------------------------------

  for (op <- PositOp.all) {
    lazy val sim8  = unitSim(p8, op, PositPipeline.full, "full")
    lazy val sim16 = unitSim(p16, op, PositPipeline(), "op")
    lazy val sim32 = unitSim(p32, op, PositPipeline.combinational, "comb")

    testpoint("unit_p8_exhaustive", op.name) {
      val r = runOn(sim8, op, s"p8_${op.name}", _ => exhaustive(p8))
      assert(r.checked == 65536)
    }
    testpoint("unit_p16_random", op.name) {
      runOn(sim16, op, s"p16_${op.name}", rnd => stress(p16, vectors(300000), rnd))
    }
    testpoint("unit_p32_random", op.name) {
      runOn(sim32, op, s"p32_${op.name}", rnd => stress(p32, vectors(100000), rnd))
    }
  }

  testpoint("unit_es_variants", "p8es0_add") {
    val c = PositConfig(8, 0)
    runOn(unitSim(c, PositOp.Add, PositPipeline(), "op"), PositOp.Add, "p8es0_add", _ => exhaustive(c))
  }
  testpoint("unit_es_variants", "p12es3_mul") {
    val c = PositConfig(12, 3)
    runOn(unitSim(c, PositOp.Mul, PositPipeline(), "op"), PositOp.Mul, "p12es3_mul", rnd => stress(c, vectors(50000), rnd))
  }

  // ---- PositDivider ---------------------------------------------------------------

  testpoint("div_iterative", "p8_k1") {
    runOn(divSim(p8, DivArch.Iterative(1)), PositOp.Div, "div_p8_iter1", _ => exhaustive(p8))
  }
  testpoint("div_iterative", "p8_k8") {
    runOn(divSim(p8, DivArch.Iterative(8)), PositOp.Div, "div_p8_iter8", _ => exhaustive(p8))
  }
  testpoint("div_iterative", "p16_k3") {
    runOn(divSim(p16, DivArch.Iterative(3)), PositOp.Div, "div_p16_iter3", rnd => stress(p16, vectors(100000), rnd))
  }
  testpoint("div_pipelined", "p16_k5") {
    runOn(divSim(p16, DivArch.Pipelined(5)), PositOp.Div, "div_p16_pipe5", rnd => stress(p16, vectors(100000), rnd))
  }
  testpoint("div_pipelined", "p32_k4") {
    val n = vectors(100000)
    // ready stale 1: potok przyjmuje co takt, wiec takty ~ liczba wektorow / P(valid) + glebokosc
    val r = runOn(divSim(p32, DivArch.Pipelined(4)), PositOp.Div, "div_p32_pipe4", rnd => stress(p32, n, rnd), readyFactor = 1.0f)
    assert(r.checked == n)
  }

  // ---- StreamConformance (rsp) ----------------------------------------------------

  def rspHandle(d : PositStreamComponent) =
    StreamPortHandle(d.io.rsp.valid, d.io.rsp.ready, d.io.rsp.payload, isInput = false, "rsp")

  // Potok z rejestrami i dzielnik sekwencyjny: tam payload moze trzymac stan.
  lazy val confUnit = unitSim(p16, PositOp.Mul, PositPipeline.full, "full")
  lazy val confDiv  = divSim(p16, DivArch.Iterative(2))

  testpoint("rsp_payload_stable", "mul_full") {
    runOn(confUnit, PositOp.Mul, "conf_stable_mul", rnd => stress(p16, 5000, rnd), readyFactor = 0.2f,
          extra = (d, cd) => StreamConformance.payloadStable(cd, rspHandle(d)))
  }
  testpoint("rsp_payload_stable", "div_iter2") {
    runOn(confDiv, PositOp.Div, "conf_stable_div", rnd => stress(p16, 2000, rnd), readyFactor = 0.2f,
          extra = (d, cd) => StreamConformance.payloadStable(cd, rspHandle(d)))
  }

  testpoint("rsp_backpressure", "mul_full") {
    runOn(confUnit, PositOp.Mul, "conf_bp_mul", rnd => stress(p16, 5000, rnd), readyFactor = 0.05f,
          extra = (d, cd) => StreamConformance.noStall(cd, rspHandle(d), limit = 2000))
  }
  testpoint("rsp_backpressure", "div_iter2") {
    runOn(confDiv, PositOp.Div, "conf_bp_div", rnd => stress(p16, 2000, rnd), readyFactor = 0.05f,
          extra = (d, cd) => StreamConformance.noStall(cd, rspHandle(d), limit = 2000))
  }

  // Reset synchroniczny: pierwsze zbocze w resecie widzi jeszcze stan sprzed resetu,
  // wiec valid sprawdzane od drugiego zbocza (jak w CmvnTestplan).
  for ((variant, sim) <- Seq[(String, () => SimCompiled[_ <: PositStreamComponent])](
         "mul_full" -> (() => confUnit), "div_iter2" -> (() => confDiv))) {
    testpoint("rsp_reset_quiet", variant) {
      sim().doSim(s"conf_reset_$variant", seed = SimSeed(s"conf_reset_$variant")) { d =>
        val cd = d.clockDomain
        d.io.cmd.valid #= false
        d.io.rsp.ready #= false
        cd.forkStimulus(10)
        cd.waitSampling(4)
        // zaladuj potok i wstrzymaj wyjscie
        d.io.cmd.valid #= true
        d.io.cmd.a #= 0x4000
        d.io.cmd.b #= 0x4000
        cd.waitSamplingWhere(d.io.rsp.valid.toBoolean)
        d.io.cmd.valid #= false
        cd.assertReset()
        cd.waitActiveEdge(2)
        assert(!d.io.rsp.valid.toBoolean, "rsp.valid w trakcie resetu")
        cd.waitActiveEdge(2)
        assert(!d.io.rsp.valid.toBoolean, "rsp.valid w trakcie resetu")
        cd.deassertReset()
        cd.waitSampling(2)
        assert(!d.io.rsp.valid.toBoolean, "rsp.valid po resecie bez polecenia")
      }
    }
  }

  unimplemented("rsp_stress_with_rand_reset", "V3: losowy reset w trakcie ruchu")
}
