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
import AluSimKit.{expected, Cmd}

// =====================================================================
//  PositStreamAlu: tablice przez strumien Fragment(a, b), sciezka
//  rejestrowa na tym samym ALU, tryb tablica (+) skalar.
//
//  Zrodlo planu: plan etapu 2 (losowe przerwy w tvalid, losowy tready,
//  tlast na ostatnim elemencie). Wzorzec: PositRef.withFlags.
//  StreamConformance dla portu "out".
// =====================================================================
object PositStreamAluPlan {
  val plan : Seq[Testpoint] = Seq(
    Testpoint("stream_array_ops", Stage.V1,
      "Tablice o losowych dlugosciach, kazda operacja",
      stimulus = Seq("posit<16,2>, tablice 1..40 elementow jedna po drugiej", "losowe przerwy w valid, losowy ready"),
      checking = Seq("wynik i flagi == PositRef bit w bit, w kolejnosci", "last tylko na ostatnim elemencie kazdej tablicy")),

    Testpoint("stream_scalar_b", Stage.V1,
      "Tablica (+) skalar: b z io.b",
      stimulus = Seq("scalarB = 1, b w strumieniu losowe (ignorowane)"),
      checking = Seq("wynik == PositRef(a, io.b) bit w bit")),

    Testpoint("stream_lengths", Stage.V1,
      "Tablice dlugosci 1, 2, 3 i 1000 pod rzad, zmiana operacji miedzy tablicami",
      checking = Seq("liczba wynikow i polozenie last zgodne z dlugosciami", "wynik z operacji ustawionej dla danej tablicy")),

    Testpoint("stream_reg_interleave", Stage.V2,
      "Pojedyncze dzialania z rejestrow w trakcie strumienia",
      stimulus = Seq("losowe polecenia na io.reg w trakcie tablicy, losowy ready na obu wyjsciach"),
      checking = Seq("wyniki rejestrowe tylko na regRsp, strumieniowe tylko na out, oba bit w bit",
                     "zadna strona nie gubi ani nie dubluje wynikow")),

    Testpoint("stream_throughput", Stage.V2,
      "Przepustowosc add/sub/mul",
      stimulus = Seq("posit<32,2>, potok full, 2000 elementow bez przerw, ready = 1"),
      checking = Seq("2000 wynikow w <= 2000 + 20 taktach (1 element / takt)")),

    Testpoint("stream_in_flight", Stage.V2,
      "Sygnal inFlight",
      checking = Seq("inFlight = 1 od przyjecia pierwszego elementu do wydania ostatniego wyniku",
                     "inFlight = 0 po oproznieniu"))
  ) ++ StreamConformance.testpoints("out")
}

class PositStreamAluTestplan extends TestplanSuite {
  import PositStreamAluPlan._
  override def testplan = plan

  val p16 = PositConfig(16)
  val p32 = PositConfig(32)

  def sim(cfg : PositAluConfig, name : String) = SimEnv(Conventions.spinal, name).compile(new PositStreamAlu(cfg))

  lazy val s16   = sim(PositAluConfig(p16, divBitsPerCycle = 2, pipe = PositPipeline.full), "salu_16_k2_full")
  lazy val s32   = sim(PositAluConfig(p32, divBitsPerCycle = 4, pipe = PositPipeline.full), "salu_32_k4_full")

  /** Jeden element strumienia z oczekiwanym wynikiem. */
  case class Elem(a : BigInt, b : BigInt, last : Boolean, want : BigInt, wantF : Int)

  /** Tablice (op, scalarB, b, pary) -> elementy; io.op/scalarB/b przestawiane przy pierwszym elemencie tablicy. */
  case class Array_(op : PositOp, scalarB : Option[BigInt], pairs : Seq[(BigInt, BigInt)])

  case class Env(d : PositStreamAlu, cd : ClockDomain, rnd : Random) {
    val outQ    = mutable.Queue[Elem]()
    val regQ    = mutable.Queue[(Cmd, BigInt, Int)]()
    var outSeen = 0L
    var regSeen = 0L
    val outReady = StreamReadyRandomizer(d.io.output, cd)
    val regReady = StreamReadyRandomizer(d.io.regRsp, cd)

    StreamMonitor(d.io.output, cd) { p =>
      assert(outQ.nonEmpty, "out: wynik bez elementu")
      val e = outQ.dequeue()
      val got  = p.fragment.bits.toBigInt
      val gotF = flagBits(p.fragment.flags)
      assert(got == e.want && gotF == e.wantF,
        f"out: a=0x${e.a}%x b=0x${e.b}%x -> 0x$got%x f=$gotF, oczekiwane 0x${e.want}%x f=${e.wantF}")
      assert(p.last.toBoolean == e.last, s"out: last=${p.last.toBoolean}, oczekiwane ${e.last} (element #$outSeen)")
      outSeen += 1
    }
    StreamMonitor(d.io.regRsp, cd) { p =>
      assert(regQ.nonEmpty, "regRsp: wynik bez polecenia")
      val (x, want, wantF) = regQ.dequeue()
      val got = p.bits.toBigInt
      assert(got == want && flagBits(p.flags) == wantF, f"regRsp: op=${x.op} a=0x${x.a}%x b=0x${x.b}%x -> 0x$got%x, oczekiwane 0x$want%x")
      regSeen += 1
    }

    /** Wysyla tablice jedna po drugiej; zmiana io.op/scalarB/b dopiero, gdy ALU puste (jak w backend_hw). */
    def sendArrays(arrays : Seq[Array_], gaps : Boolean = true) : Unit = {
      for (arr <- arrays) {
        cd.waitSamplingWhere(outQ.isEmpty && !d.io.inFlight.toBoolean)
        d.io.op #= PositAluOp.code(arr.op)
        d.io.scalarB #= arr.scalarB.nonEmpty
        d.io.b #= arr.scalarB.getOrElse(BigInt(0))
        for (((a, b), i) <- arr.pairs.zipWithIndex) {
          val last = i == arr.pairs.size - 1
          val bEff = arr.scalarB.getOrElse(b)
          val (want, wantF) = PositRef.withFlags(d.c, arr.op, a, bEff)
          if (gaps) cd.waitSampling(rnd.nextInt(3))
          d.io.input.valid #= true
          d.io.input.fragment.a #= a
          d.io.input.fragment.b #= b
          d.io.input.last #= last
          outQ.enqueue(Elem(a, b, last, want, wantF.bits))
          cd.waitSamplingWhere(d.io.input.ready.toBoolean)
          d.io.input.valid #= false
        }
      }
      cd.waitSamplingWhere(outQ.isEmpty && regQ.isEmpty)
    }
  }

  def flagBits(f : PositFlags) : Int =
    (if (f.nar.toBoolean) 1 else 0) | (if (f.sat.toBoolean) 2 else 0) | (if (f.inexact.toBoolean) 4 else 0)

  def run(compiled : SimCompiled[PositStreamAlu], label : String)(body : Env => Unit) : Unit =
    compiled.doSim(label, seed = SimSeed(label)) { d =>
      val cd = d.clockDomain
      d.io.input.valid #= false
      d.io.reg.valid #= false
      d.io.op #= 0
      d.io.scalarB #= false
      d.io.b #= 0
      cd.forkStimulus(10)
      cd.waitSampling(4)
      body(Env(d, cd, new Random(SimSeed(label))))
    }

  def randomArrays(c : PositConfig, rnd : Random, count : Int, maxLen : Int, ops : Seq[PositOp]) : Seq[Array_] =
    Seq.fill(count) {
      Array_(ops(rnd.nextInt(ops.size)), None, stress(c, 1 + rnd.nextInt(maxLen), rnd).toSeq)
    }

  // ---- testpointy ---------------------------------------------------------------------

  for (op <- PositOp.all) {
    testpoint("stream_array_ops", op.name) {
      run(s16, s"salu_arrays_${op.name}") { e =>
        val arrays = randomArrays(p16, e.rnd, 60, 40, Seq(op))
        e.sendArrays(arrays)
        assert(e.outSeen == arrays.map(_.pairs.size).sum)
        info(s"${arrays.size} tablic, ${e.outSeen} elementow")
      }
    }
  }

  testpoint("stream_scalar_b") {
    run(s16, "salu_scalar_b") { e =>
      val arrays = for (op <- PositOp.all; _ <- 0 until 5)
        yield Array_(op, Some(BigInt(16, e.rnd)), stress(p16, 1 + e.rnd.nextInt(60), e.rnd).toSeq)
      e.sendArrays(arrays)
      info(s"${e.outSeen} elementow")
    }
  }

  testpoint("stream_lengths") {
    run(s16, "salu_lengths") { e =>
      val ops = PositOp.all
      val arrays = Seq(1, 2, 3, 1000, 1, 7).zipWithIndex.map { case (n, i) =>
        Array_(ops(i % ops.size), None, stress(p16, n, e.rnd).toSeq)
      }
      e.sendArrays(arrays)
      assert(e.outSeen == arrays.map(_.pairs.size).sum)
    }
  }

  testpoint("stream_reg_interleave") {
    run(s16, "salu_interleave") { e =>
      val d = e.d
      val regs = AluSimKit.mixed(PositOp.all, stress(p16, 400, e.rnd), e.rnd).toSeq
      // polecenia rejestrowe z losowymi przerwami, rownolegle ze strumieniem
      val regThread = fork {
        for (x <- regs) {
          e.cd.waitSampling(e.rnd.nextInt(8))
          d.io.reg.valid #= true
          d.io.reg.op #= x.op
          d.io.reg.a #= x.a
          d.io.reg.b #= x.b
          val (r, f) = expected(d.cfg, x)
          e.regQ.enqueue((x, r, f))
          e.cd.waitSamplingWhere(d.io.reg.ready.toBoolean)
          d.io.reg.valid #= false
        }
      }
      e.sendArrays(randomArrays(p16, e.rnd, 40, 50, PositOp.all))
      regThread.join()
      e.cd.waitSamplingWhere(e.regQ.isEmpty)
      assert(e.regSeen == regs.size)
      info(s"strumien ${e.outSeen}, rejestry ${e.regSeen}")
    }
  }

  for (op <- Seq(PositOp.Add, PositOp.Mul)) {
    testpoint("stream_throughput", op.name) {
      run(s32, s"salu_tput_${op.name}") { e =>
        val d = e.d
        e.outReady.factor = 1.0f
        val pairs = stress(p32, 2000, e.rnd).toSeq
        d.io.op #= PositAluOp.code(op)
        var t0 = 0L
        var cycles = 0L
        fork { while (true) { e.cd.waitSampling(); cycles += 1 } }
        val driver = StreamDriver(d.io.input, e.cd) { p =>
          if (e.outQ.size + e.outSeen.toInt >= pairs.size) false else {
            val i = e.outQ.size + e.outSeen.toInt
            val (a, b) = pairs(i)
            p.fragment.a #= a; p.fragment.b #= b; p.last #= i == pairs.size - 1
            val (w, f) = PositRef.withFlags(p32, op, a, b)
            e.outQ.enqueue(Elem(a, b, i == pairs.size - 1, w, f.bits))
            if (i == 0) t0 = cycles
            true
          }
        }
        driver.transactionDelay = () => 0
        e.cd.waitSamplingWhere(e.outSeen == pairs.size)
        val took = cycles - t0
        assert(took <= pairs.size + 20, s"$took taktow na ${pairs.size} elementow")
        info(s"${pairs.size} elementow w $took taktach")
      }
    }
  }

  testpoint("stream_in_flight") {
    run(s16, "salu_in_flight") { e =>
      val d = e.d
      assert(!d.io.inFlight.toBoolean, "inFlight przed pierwszym elementem")
      e.outReady.factor = 0.0f            // wynik zostaje w potoku
      d.io.op #= PositAluOp.code(PositOp.Div)
      d.io.input.valid #= true
      d.io.input.fragment.a #= 0x4000; d.io.input.fragment.b #= 0x4c00; d.io.input.last #= true
      val (w, f) = PositRef.withFlags(p16, PositOp.Div, 0x4000, 0x4c00)
      e.outQ.enqueue(Elem(0x4000, 0x4c00, true, w, f.bits))
      e.cd.waitSamplingWhere(d.io.input.ready.toBoolean)
      d.io.input.valid #= false
      for (_ <- 0 until 30) { e.cd.waitSampling(); assert(d.io.inFlight.toBoolean, "inFlight opadl przed wydaniem wyniku") }
      e.outReady.factor = 1.0f
      e.cd.waitSamplingWhere(e.outQ.isEmpty)
      e.cd.waitSampling(2)
      assert(!d.io.inFlight.toBoolean, "inFlight po oproznieniu")
    }
  }

  // ---- StreamConformance (out) ------------------------------------------------------------

  def outHandle(d : PositStreamAlu) = StreamPortHandle(d.io.output.valid, d.io.output.ready, d.io.output.payload, isInput = false, "out")

  testpoint("out_payload_stable") {
    run(s16, "salu_conf_stable") { e =>
      e.outReady.factor = 0.2f
      StreamConformance.payloadStable(e.cd, outHandle(e.d))
      e.sendArrays(randomArrays(p16, e.rnd, 20, 30, PositOp.all))
    }
  }

  testpoint("out_backpressure") {
    run(s16, "salu_conf_bp") { e =>
      e.outReady.factor = 0.05f
      StreamConformance.noStall(e.cd, outHandle(e.d), limit = 2000)
      e.sendArrays(randomArrays(p16, e.rnd, 20, 30, PositOp.all))
    }
  }

  testpoint("out_reset_quiet") {
    run(s16, "salu_conf_reset") { e =>
      val d = e.d
      e.outReady.factor = 0.0f
      d.io.op #= PositAluOp.code(PositOp.Add)
      d.io.input.valid #= true
      d.io.input.fragment.a #= 0x4000; d.io.input.fragment.b #= 0x4000; d.io.input.last #= true
      e.cd.waitSamplingWhere(d.io.output.valid.toBoolean)
      d.io.input.valid #= false
      e.cd.assertReset()
      e.cd.waitActiveEdge(2)
      assert(!d.io.output.valid.toBoolean, "out.valid w trakcie resetu")
      e.cd.deassertReset()
      e.cd.waitSampling(2)
      assert(!d.io.output.valid.toBoolean && !d.io.inFlight.toBoolean, "po resecie: out.valid albo inFlight")
    }
  }

  unimplemented("out_stress_with_rand_reset", "V3: losowy reset w trakcie tablicy")
}
