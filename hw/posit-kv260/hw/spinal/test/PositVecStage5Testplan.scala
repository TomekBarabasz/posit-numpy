package posit.kv260

import spinal.core._
import spinal.core.sim._
import spinal.lib.bus.amba4.axilite.sim.AxiLite4Driver
import scala.collection.mutable
import scala.util.Random
import newhope.vertebra._
import newhope.vertebra.sim.{SimEnv, SimSeed}
import posit._
import posit.PositSimKit.stress
import posit.vector._
import PositAluRegs._
import PositVecRegs.{OPERAND_C, OP_SQRT, OP_FMA, OP_AXPY, OP_F2P, OP_P2F, STREAM_CFG, CNT_ELEMENTS}

// =====================================================================
//  PositVecAxi 4.0: sqrt, FMA, AXPY i konwersje na tablicach (etap 5).
//
//  Sekwencja jak w backend_hw (PositVecHw.array_op): OP, OPERAND_B/C,
//  STREAM_CFG, tablica przez s_axis ({b, a}), wyniki z m_axis.
//  Wzorce: PositRef.sqrt, .fma, .fromFloat32, .toFloat32.
// =====================================================================
object PositVecStage5Plan {
  val plan : Seq[Testpoint] = Seq(
    Testpoint("vec5_config", Stage.V1,
      "Wersja 4.0, maska operacji, OPERAND_C",
      checking = Seq("ID_VERSION == PositVecRegs.idVersion (5.0)", "CONFIG.opMask: bity 7..11 (posit32)", "OPERAND_C: odczyt == zapis",
                     "ENGINE_CFG: k i slowUnits z konfiguracji")),

    Testpoint("vec5_sqrt", Stage.V1,
      "sqrt tablicy",
      checking = Seq("m_axis == PositRef.sqrt(a), polowa b ignorowana", "tlast na ostatnim, CNT_ELEMENTS")),

    Testpoint("vec5_fma", Stage.V1,
      "FMA: a * b + OPERAND_C",
      checking = Seq("m_axis == PositRef.fma(a, b, C) bit w bit")),

    Testpoint("vec5_axpy", Stage.V1,
      "AXPY: y = alfa * x + y, x w polowie a, y w polowie b, alfa w OPERAND_B",
      checking = Seq("m_axis == PositRef.fma(x, alfa, y)", "dziala tez z STREAM_CFG.scalar_b = 1")),

    Testpoint("vec5_convert", Stage.V1,
      "F2P i P2F na tablicach",
      checking = Seq("F2P: m_axis == PositRef.fromFloat32(a)", "P2F: m_axis == PositRef.toFloat32(a)")),

    Testpoint("vec5_reg_ops", Stage.V2,
      "sqrt i FMA przez rejestry (pojedyncze dzialania)",
      checking = Seq("RESULT i flagi == PositRef, FMA bierze c z OPERAND_C"))
  )
}

class PositVecStage5Testplan extends TestplanSuite {
  import PositVecStage5Plan._
  override def testplan = plan

  val p32 = PositConfig(32)
  val cfg = PositAluConfig(p32, divBitsPerCycle = 4, pipe = PositPipeline.full, sqrt = true, fma = true, convert = true)
  lazy val v32 = SimEnv(Conventions.spinal, "vec5_32").compile(new PositVecAxi(cfg))

  class Env(val d : PositVecAxi, val rnd : Random) {
    val c   = d.c
    val cd  = d.clockDomain
    val axi = AxiLite4Driver(d.io.s_axi, cd)
    def rd(a : Int) : Long = axi.read(a).toLong
    def wr(a : Int, v : Long) : Unit = axi.write(a, v)
    val got = mutable.ArrayBuffer[(BigInt, Boolean)]()
    d.io.m_axis.tready #= false
    d.io.s_axis.tvalid #= false
    cd.onSamplings {
      if (d.io.m_axis.tvalid.toBoolean && d.io.m_axis.tready.toBoolean) got += ((d.io.m_axis.tdata.toBigInt, d.io.m_axis.tlast.toBoolean))
      d.io.m_axis.tready #= rnd.nextFloat() < 0.6f
    }

    def array(op : Int, pairs : Seq[(BigInt, BigInt)], b : Long = 0, addend : Long = 0, scalarB : Boolean = false) : Seq[BigInt] = {
      wr(OP, op); wr(OPERAND_B, b); wr(OPERAND_C, addend); wr(STREAM_CFG, if (scalarB) 1 else 0)
      got.clear()
      for (((a, bb), i) <- pairs.zipWithIndex) {
        if (rnd.nextInt(3) == 0) cd.waitSampling(1 + rnd.nextInt(2))
        d.io.s_axis.tdata #= (bb << 32) | a
        d.io.s_axis.tlast #= i == pairs.size - 1
        d.io.s_axis.tvalid #= true
        cd.waitSamplingWhere(d.io.s_axis.tready.toBoolean)
        d.io.s_axis.tvalid #= false
      }
      cd.waitSamplingWhere(got.size == pairs.size)
      assert(got.map(_._2) == Seq.fill(pairs.size - 1)(false) :+ true, "tlast")
      got.map(_._1).toSeq
    }

    def check(label : String, got : Seq[BigInt], want : Seq[BigInt]) : Unit =
      for (((g, w), i) <- got.zip(want).zipWithIndex) assert(g == w, f"$label #$i: 0x$g%x, oczekiwane 0x$w%x")
  }

  def run(label : String)(body : Env => Unit) : Unit =
    v32.doSim(label, seed = SimSeed(label)) { d =>
      d.clockDomain.forkStimulus(10)
      val e = new Env(d, new Random(SimSeed(label)))
      d.clockDomain.waitSampling(10)
      body(e)
    }

  testpoint("vec5_config") {
    run("vec5_config") { e =>
      assert(e.rd(ID_VERSION) == PositVecRegs.idVersion, f"ID_VERSION 0x${e.rd(ID_VERSION)}%x")
      assert(((e.rd(CONFIG) >> 16) & 0x0f80) == 0x0f80, f"CONFIG 0x${e.rd(CONFIG)}%x")
      assert(e.rd(PositVecRegs.ENGINE_CFG) == PositVecRegs.engineCfg(cfg), f"ENGINE_CFG 0x${e.rd(PositVecRegs.ENGINE_CFG)}%x")
      e.wr(OPERAND_C, 0x12345678L)
      assert(e.rd(OPERAND_C) == 0x12345678L)
    }
  }

  testpoint("vec5_sqrt") {
    run("vec5_sqrt") { e =>
      val pairs = stress(p32, 1000, e.rnd).toSeq
      e.wr(CTRL, 1 << CTRL_CLEAR_COUNTERS)
      e.check("sqrt", e.array(OP_SQRT, pairs), pairs.map { case (a, _) => PositRef.sqrt(p32, a)._1 })
      assert(e.rd(CNT_ELEMENTS) == 1000)
    }
  }

  testpoint("vec5_fma") {
    run("vec5_fma") { e =>
      for (_ <- 0 until 3) {
        val cc = BigInt(32, e.rnd)
        val pairs = stress(p32, 500, e.rnd).toSeq
        e.check("fma", e.array(OP_FMA, pairs, addend = cc.toLong), pairs.map { case (a, b) => PositRef.fma(p32, a, b, cc)._1 })
      }
    }
  }

  testpoint("vec5_axpy") {
    run("vec5_axpy") { e =>
      for (scalarB <- Seq(false, true)) {
        val alpha = BigInt(32, e.rnd)
        val pairs = stress(p32, 500, e.rnd).toSeq                 // (x, y)
        e.check(s"axpy scalarB=$scalarB", e.array(OP_AXPY, pairs, b = alpha.toLong, scalarB = scalarB),
                pairs.map { case (x, y) => PositRef.fma(p32, x, alpha, y)._1 })
      }
    }
  }

  testpoint("vec5_convert") {
    run("vec5_convert") { e =>
      val floats = Seq.fill(1000)((BigInt(32, e.rnd), BigInt(32, e.rnd)))
      e.check("f2p", e.array(OP_F2P, floats), floats.map { case (a, _) => PositRef.fromFloat32(p32, a)._1 })
      val posits = stress(p32, 1000, e.rnd).toSeq
      e.check("p2f", e.array(OP_P2F, posits), posits.map { case (a, _) => PositRef.toFloat32(p32, a)._1 })
    }
  }

  testpoint("vec5_reg_ops") {
    run("vec5_reg_ops") { e =>
      def reg(op : Int, a : BigInt, b : BigInt, cc : BigInt) : (BigInt, Int) = {
        e.wr(OP, op); e.wr(OPERAND_A, a.toLong); e.wr(OPERAND_B, b.toLong); e.wr(OPERAND_C, cc.toLong); e.wr(CTRL, 1 << CTRL_START)
        var s = e.rd(STATUS); var n = 0
        while ((s & (1 << STATUS_DONE)) == 0) { n += 1; assert(n < 1000); s = e.rd(STATUS) }
        (BigInt(e.rd(RESULT)), ((s >> STATUS_FLAGS_SHIFT) & 7).toInt)
      }
      for ((a, b) <- stress(p32, 100, e.rnd)) {
        val cc = BigInt(32, e.rnd)
        val (r1, f1) = reg(OP_SQRT, a, b, cc)
        val (w1, wf1) = PositRef.sqrt(p32, a)
        assert(r1 == w1 && f1 == wf1.bits, f"sqrt 0x$a%x")
        val (r2, f2) = reg(OP_FMA, a, b, cc)
        val (w2, wf2) = PositRef.fma(p32, a, b, cc)
        assert(r2 == w2 && f2 == wf2.bits, f"fma 0x$a%x 0x$b%x 0x$cc%x")
      }
    }
  }
}
