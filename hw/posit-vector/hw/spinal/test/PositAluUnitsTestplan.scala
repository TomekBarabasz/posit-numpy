package posit.vector

import spinal.core._
import spinal.core.sim._
import spinal.lib.sim._
import scala.collection.mutable
import scala.util.Random
import newhope.vertebra._
import newhope.vertebra.sim.{SimEnv, SimSeed}
import posit._
import posit.PositSimKit.stress
import AluSimKit._

// =====================================================================
//  PositAlu ze slowUnits > 1: kilka torow wielotaktowych (div, sqrt, FMA),
//  kolejne elementy na zmiane do kolejnych torow, wyniki w kolejnosci.
//
//  Wzorce: PositRef (AluSimKit.expected). Przepustowosc: przy ciaglym
//  strumieniu jednej operacji wielotaktowej L torow daje L wynikow na
//  <op>Cycles taktow (PositAluConfig.slowRate).
// =====================================================================
object PositAluUnitsPlan {
  val plan : Seq[Testpoint] = Seq(
    Testpoint("units_throughput", Stage.V1,
      "div, sqrt, FMA posit32 (k = 4) na 2 i 4 torach, ready = 1, polecenia bez przerw",
      checking = Seq("wyniki bit w bit", "takty na N elementow ~ N / slowRate (+ latencja), sredni odstep == cycles / L")),

    Testpoint("units_mixed_order", Stage.V1,
      "Wszystkie operacje przeplatane na 3 torach, losowe przerwy w valid i ready",
      checking = Seq("wynik i flagi bit w bit, kolejnosc polecen zachowana (szybkie czekaja za wolnymi)")),

    Testpoint("units_fast_unchanged", Stage.V2,
      "add / mul przy 4 torach",
      checking = Seq("1 element / takt jak przy jednym torze")),

    Testpoint("units_stream", Stage.V2,
      "PositStreamAlu z 4 torami: FIFO znacznikow miesci tyle elementow, ile ALU",
      checking = Seq("div w strumieniu: 4 razy szybciej niz jeden tor (+- 5%)", "wyniki i tlast bit w bit"))
  )
}

class PositAluUnitsTestplan extends TestplanSuite {
  import PositAluUnitsPlan._
  override def testplan = plan

  val p16 = PositConfig(16)
  val p32 = PositConfig(32)
  import PositAluOp.{SQRT, FMA, AXPY, F2P, P2F}

  def cfg(c : PositConfig, units : Int, pipe : PositPipeline = PositPipeline.full) =
    PositAluConfig(c, PositOp.all, divBitsPerCycle = 4, pipe = pipe, sqrt = true, fma = true, convert = c.n == 32,
                   slowUnits = units)
  def tag(x : PositAluConfig) = s"alu_u${x.slowUnits}_${x.c.n}_${x.pipe.latency}"
  def sim(x : PositAluConfig) = SimEnv(Conventions.spinal, tag(x)).compile(new PositAlu(x))

  def runOn(compiled : SimCompiled[PositAlu], label : String, gen : Random => Iterator[Cmd],
            readyFactor : Float = 0.5f, gaps : Boolean = true) : Run = {
    var r : Run = null
    compiled.doSim(label, seed = SimSeed(label)) { d =>
      r = drive(d, gen(new Random(SimSeed(label))), label, readyFactor, _ => (), gaps)
    }
    r
  }

  /** Sredni odstep miedzy wynikami w stanie ustalonym (bez pierwszych i ostatnich `skip`). */
  def meanGap(r : Run, skip : Int = 20) : Double = {
    val f = r.fires.slice(skip, r.fires.size - skip)
    (f.last - f.head).toDouble / (f.size - 1)
  }

  // ---- przepustowosc ----------------------------------------------------------------------

  for (units <- Seq(2, 4)) {
    lazy val a32 = sim(cfg(p32, units, PositPipeline.combinational))
    testpoint("units_throughput", s"u$units") {
      val x = cfg(p32, units, PositPipeline.combinational)
      for ((name, op, cycles) <- Seq(("div", PositAluOp.code(PositOp.Div), x.divCycles),
                                     ("sqrt", SQRT, x.sqrtCycles), ("fma", FMA, x.fmaCycles))) {
        val r = runOn(a32, s"units_tput_${name}_u$units", rnd => Iterator.fill(2000) {
          val a = BigInt(32, rnd); val b = BigInt(32, rnd)
          Cmd(op, if (op == SQRT) a.clearBit(31) else a, b, BigInt(32, rnd))
        }, readyFactor = 1.0f, gaps = false)
        val want = 1.0 / x.slowRate(cycles)
        val got  = meanGap(r)
        assert(math.abs(got - want) < 0.02 * want + 0.01, f"$name, $units torow: sredni odstep $got%.3f, oczekiwany $want%.3f")
        info(f"$name: $units torow, $got%.2f taktu / element (jeden tor: $cycles)")
      }
    }
  }

  // ---- kolejnosc przy przeplocie -------------------------------------------------------------

  testpoint("units_mixed_order", "p32") {
    val codes = PositOp.all.map(PositAluOp.code) ++ Seq(SQRT, FMA, AXPY, F2P, P2F)
    runOn(sim(cfg(p32, 3)), "units_mixed_p32", rnd => stress(p32, 30000, rnd).map { case (a, b) =>
      Cmd(codes(rnd.nextInt(codes.size)), a, b, BigInt(32, rnd))
    })
  }
  testpoint("units_mixed_order", "p16") {
    val codes = PositOp.all.map(PositAluOp.code) ++ Seq(SQRT, FMA, AXPY)
    runOn(sim(cfg(p16, 3)), "units_mixed_p16", rnd => stress(p16, 30000, rnd).map { case (a, b) =>
      Cmd(codes(rnd.nextInt(codes.size)), a, b, BigInt(16, rnd))
    }, readyFactor = 0.8f)
  }

  testpoint("units_fast_unchanged") {
    for (op <- Seq(PositOp.Add, PositOp.Mul)) {
      val r = runOn(sim(cfg(p32, 4, PositPipeline.combinational)), s"units_fast_${op.name}",
                    rnd => cmds(op, stress(p32, 2000, rnd)), readyFactor = 1.0f, gaps = false)
      assert(meanGap(r) == 1.0, f"${op.name}: ${meanGap(r)}%.3f taktu / element")
    }
  }

  // ---- PositStreamAlu ---------------------------------------------------------------------------

  def streamDiv(units : Int) : Long = {
    val x = cfg(p32, units)
    val label = s"units_stream_u$units"
    var took = 0L
    SimEnv(Conventions.spinal, s"salu_u$units").compile(new PositStreamAlu(x)).doSim(label, seed = SimSeed(label)) { d =>
      val cd  = d.clockDomain
      val rnd = new Random(SimSeed(label))
      d.io.reg.valid #= false
      d.io.op #= PositAluOp.code(PositOp.Div)
      d.io.scalarB #= false
      d.io.b #= 0
      d.io.addend #= 0
      d.io.output.ready #= true
      d.io.regRsp.ready #= true
      cd.forkStimulus(10)
      cd.waitSampling(4)
      val pairs = stress(p32, 2000, rnd).toIndexedSeq
      val want  = mutable.Queue[(BigInt, Boolean)]()
      var seen, cycles = 0L
      fork { while (true) { cd.waitSampling(); cycles += 1 } }
      StreamMonitor(d.io.output, cd) { p =>
        val (w, last) = want.dequeue()
        assert(p.fragment.bits.toBigInt == w && p.last.toBoolean == last, s"$label #$seen")
        seen += 1
      }
      var i = 0
      val driver = StreamDriver(d.io.input, cd) { p =>
        i < pairs.size && {
          val (a, b) = pairs(i)
          p.fragment.a #= a; p.fragment.b #= b; p.last #= i == pairs.size - 1
          want.enqueue((PositRef(p32, PositOp.Div, a, b), i == pairs.size - 1))
          i += 1
          true
        }
      }
      driver.transactionDelay = () => 0
      val t0 = cycles
      cd.waitSamplingWhere(seen == pairs.size)
      took = cycles - t0
    }
    took
  }

  testpoint("units_stream") {
    val one  = streamDiv(1)
    val four = streamDiv(4)
    val ratio = one.toDouble / four
    assert(ratio > 3.8 && ratio < 4.2, f"div w strumieniu: 1 tor $one, 4 tory $four taktow (x$ratio%.2f)")
    info(f"2000 dzielen posit32: 1 tor $one taktow, 4 tory $four (x$ratio%.2f)")
  }
}
