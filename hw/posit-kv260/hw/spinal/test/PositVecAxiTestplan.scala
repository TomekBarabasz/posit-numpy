package posit.kv260

import spinal.core._
import spinal.core.sim._
import spinal.lib.bus.amba4.axilite.sim.AxiLite4Driver
import scala.collection.mutable
import scala.io.Source
import scala.util.Random
import newhope.vertebra._
import newhope.vertebra.sim.{SimEnv, SimSeed}
import posit._
import posit.PositSimKit.stress
import posit.vector._
import PositAluRegs._
import PositVecRegs.{CNT_ELEMENTS, STREAM_CFG, STREAM_CFG_SCALAR_B, STATUS_STREAM_ACTIVE, CONFIG_HAS_STREAM}

// =====================================================================
//  PositVecAxi: rejestry AXI4-Lite (etap 1 + 2) i AXI4-Stream do AXI DMA.
//
//  Zrodlo planu: plan etapu 2, "Co przetestowac": tlast na ostatnim
//  elemencie, losowe przerwy w tvalid i losowy tready, dlugosci 1, 2,
//  1000 i nieparzyste, dwa wywolania pod rzad, zmiana operacji.
//  s_axis jest sterowane jak MM2S w AXI DMA (tlast na ostatnim slowie
//  transferu), m_axis odbierane jak S2MM. Wzorzec: PositRef.withFlags.
// =====================================================================
object PositVecAxiPlan {
  val plan : Seq[Testpoint] = Seq(
    Testpoint("vec_regs_python_match", Stage.V1,
      "Stale etapu 2 w sw/positnp/regs.py == PositVecRegs",
      checking = Seq("kazda stala z PositVecRegs.all ma w regs.py te sama wartosc")),

    Testpoint("vec_port_names", Stage.V1,
      "Porty rozpoznawalne przez IP packagera Vivado",
      checking = Seq("aclk, aresetn, s_axi_*, s_axis_{tdata,tlast,tvalid,tready}, m_axis_{tdata,tlast,tvalid,tready}",
                     "s_axis_tdata ma 2n bitow, m_axis_tdata n bitow")),

    Testpoint("vec_id_config", Stage.V1,
      "ID_VERSION i CONFIG etapu 2",
      checking = Seq("ID_VERSION == 0x50530200", "CONFIG: n, es, maska operacji, bit HAS_STREAM")),

    Testpoint("vec_arrays", Stage.V1,
      "Tablice przez AXI4-Stream jak z AXI DMA",
      stimulus = Seq("posit<16,2> i posit<32,2>, dlugosci 1, 2, 3, 1000 i losowe, kazda operacja",
                     "losowe przerwy w s_axis_tvalid, losowy m_axis_tready"),
      checking = Seq("m_axis_tdata == PositRef bit w bit, w kolejnosci",
                     "m_axis_tlast tylko na ostatnim elemencie", "CNT_ELEMENTS == liczba elementow")),

    Testpoint("vec_scalar_b", Stage.V1,
      "Tablica (+) skalar: STREAM_CFG.scalar_b, b z OPERAND_B",
      checking = Seq("wynik == PositRef(a, OPERAND_B), polowa b slowa strumienia ignorowana")),

    Testpoint("vec_counters", Stage.V2,
      "Liczniki po tablicy",
      checking = Seq("CNT_NAR / CNT_SAT / CNT_INEXACT == sumy flag z PositRef", "CTRL.clear zeruje tez CNT_ELEMENTS")),

    Testpoint("vec_reg_ops", Stage.V2,
      "Pojedyncze dzialania przez rejestry w silniku etapu 2, takze w trakcie tablicy",
      checking = Seq("RESULT i STATUS jak w etapie 1, bit w bit", "tablica w tym czasie bez zmian")),

    Testpoint("vec_stream_active", Stage.V2,
      "STATUS.stream_active",
      checking = Seq("1, gdy element czeka w potoku (m_axis_tready = 0)", "0 po odebraniu wszystkich wynikow")),

    Testpoint("vec_soft_reset", Stage.V2,
      "Miekki reset w trakcie tablicy",
      checking = Seq("po resecie: stream_active = 0, m_axis_tvalid = 0", "nastepna tablica liczy sie poprawnie"))
  )
}

class PositVecAxiTestplan extends TestplanSuite {
  import PositVecAxiPlan._
  override def testplan = plan

  val p16 = PositConfig(16)
  val p32 = PositConfig(32)

  def sim(cfg : PositAluConfig, name : String) = SimEnv(Conventions.spinal, name).compile(new PositVecAxi(cfg))
  lazy val vec16 = sim(PositAluConfig(p16, divBitsPerCycle = 2, pipe = PositPipeline.full), "vec_16_k2")
  lazy val vec32 = sim(PositAluConfig(p32, divBitsPerCycle = 4, pipe = PositPipeline.full), "vec_32_k4")

  /** AXI4-Lite (jak backend_hw) + AXI4-Stream (jak AXI DMA). */
  class Env(val d : PositVecAxi, val rnd : Random) {
    val c   = d.c
    val cd  = d.clockDomain
    val axi = AxiLite4Driver(d.io.s_axi, cd)
    def rd(a : Int) : Long = axi.read(a).toLong
    def wr(a : Int, v : Long) : Unit = axi.write(a, v)

    var readyFactor = 0.5f
    val got = mutable.ArrayBuffer[(BigInt, Boolean)]()   // (tdata, tlast)
    d.io.m_axis.tready #= false
    cd.onSamplings {
      if (d.io.m_axis.tvalid.toBoolean && d.io.m_axis.tready.toBoolean)
        got += ((d.io.m_axis.tdata.toBigInt, d.io.m_axis.tlast.toBoolean))
      d.io.m_axis.tready #= rnd.nextFloat() < readyFactor
    }
    d.io.s_axis.tvalid #= false

    /** Jedna tablica przez s_axis (fork); zwraca watek. */
    def send(pairs : Seq[(BigInt, BigInt)], gaps : Boolean = true) = fork {
      for (((a, b), i) <- pairs.zipWithIndex) {
        if (gaps && rnd.nextInt(3) == 0) cd.waitSampling(1 + rnd.nextInt(3))
        d.io.s_axis.tdata #= (b << c.n) | a
        d.io.s_axis.tlast #= i == pairs.size - 1
        d.io.s_axis.tvalid #= true
        cd.waitSamplingWhere(d.io.s_axis.tready.toBoolean)
        d.io.s_axis.tvalid #= false
      }
    }

    /** Tablica: OP, STREAM_CFG, OPERAND_B, wyslanie, odbior, porownanie. */
    def array(op : PositOp, pairs : Seq[(BigInt, BigInt)], scalarB : Option[BigInt] = None) : Seq[PositRef.Flags] = {
      wr(OP, PositAluOp.code(op))
      wr(STREAM_CFG, if (scalarB.nonEmpty) 1L << STREAM_CFG_SCALAR_B else 0L)
      scalarB.foreach(b => wr(OPERAND_B, b.toLong))
      got.clear()
      send(pairs).join()
      cd.waitSamplingWhere(got.size == pairs.size)
      cd.waitSampling(5)
      assert(got.size == pairs.size, s"${got.size} wynikow na ${pairs.size} elementow")
      for ((((a, b), (r, last)), i) <- pairs.zip(got).zipWithIndex) yield {
        val (want, f) = PositRef.withFlags(c, op, a, scalarB.getOrElse(b))
        assert(r == want, f"$c ${op.name} #$i: a=0x$a%x b=0x${scalarB.getOrElse(b)}%x -> 0x$r%x, oczekiwane 0x$want%x")
        assert(last == (i == pairs.size - 1), s"tlast=$last na elemencie $i z ${pairs.size}")
        f
      }
    }
  }

  def run(compiled : SimCompiled[PositVecAxi], label : String)(body : Env => Unit) : Unit =
    compiled.doSim(label, seed = SimSeed(label)) { d =>
      d.clockDomain.forkStimulus(10)
      val e = new Env(d, new Random(SimSeed(label)))
      d.clockDomain.waitSampling(10)
      body(e)
    }

  // ---- bez symulacji --------------------------------------------------------------

  testpoint("vec_regs_python_match") {
    val Line = """^([A-Z_]+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\b.*""".r
    val py = Source.fromFile("../../sw/positnp/regs.py").getLines().collect { case Line(k, v) =>
      k -> (if (v.startsWith("0x")) java.lang.Long.parseLong(v.drop(2), 16) else v.toLong)
    }.toMap
    val diff = PositVecRegs.all.filter { case (k, v) => !py.get(k).contains(v) }
    assert(diff.isEmpty, "regs.py rozni sie od PositVecRegs: " + diff.map(_._1).mkString(", "))
  }

  testpoint("vec_port_names") {
    val dir = "simWorkspace/vec_port_names"
    val rep = Kv260.spinal.copy(targetDirectory = dir).generateVerilog(new PositVecAxi(PositAluConfig(p32)))
    val all = Source.fromFile(s"$dir/${rep.toplevelName}.v").mkString
    val top = all.indexOf(s"module ${rep.toplevelName} (")
    val hdr = all.substring(top, all.indexOf(");", top))
    val ports = """(?m)^\s*(input|output)\s+wire\s+(\[(\d+):0\]\s+)?(\w+)""".r.findAllMatchIn(hdr)
      .map(m => m.group(4) -> Option(m.group(3)).map(_.toInt + 1).getOrElse(1)).toMap
    val axil = Seq("awvalid", "awready", "awaddr", "awprot", "wvalid", "wready", "wdata", "wstrb", "bvalid", "bready", "bresp",
                   "arvalid", "arready", "araddr", "arprot", "rvalid", "rready", "rdata", "rresp").map("s_axi_" + _)
    val axis = for (p <- Seq("s_axis", "m_axis"); s <- Seq("tdata", "tlast", "tvalid", "tready")) yield s"${p}_$s"
    val want = (Seq("aclk", "aresetn") ++ axil ++ axis).toSet
    assert(ports.keySet == want, s"brak: ${(want -- ports.keySet).toSeq.sorted}, nadmiar: ${(ports.keySet -- want).toSeq.sorted}")
    assert(ports("s_axis_tdata") == 64 && ports("m_axis_tdata") == 32, s"szerokosci tdata: $ports")
  }

  // ---- symulacje --------------------------------------------------------------------

  testpoint("vec_id_config") {
    run(vec16, "vec_id_config") { e =>
      assert(e.rd(ID_VERSION) == 0x50530200L, f"ID_VERSION = 0x${e.rd(ID_VERSION)}%x")
      val w = e.rd(CONFIG)
      assert((w & 0xff) == 16 && ((w >> 8) & 0xf) == 2 && (w >> 16) == 0xf && ((w >> CONFIG_HAS_STREAM) & 1) == 1, f"CONFIG = 0x$w%x")
    }
  }

  for ((name, compiled, c) <- Seq(("p16", () => vec16, p16), ("p32", () => vec32, p32))) {
    testpoint("vec_arrays", name) {
      run(compiled(), s"vec_arrays_$name") { e =>
        e.wr(CTRL, 1 << CTRL_CLEAR_COUNTERS)
        var total = 0L
        for (op <- PositOp.all; n <- Seq(1, 2, 3, 1000, 1 + e.rnd.nextInt(200))) {
          e.array(op, stress(c, n, e.rnd).toSeq)
          total += n
        }
        assert(e.rd(CNT_ELEMENTS) == total, s"CNT_ELEMENTS = ${e.rd(CNT_ELEMENTS)}, wyslano $total")
        info(s"$total elementow, ${4 * 5} tablic")
      }
    }
  }

  testpoint("vec_scalar_b") {
    run(vec16, "vec_scalar_b") { e =>
      for (op <- PositOp.all; _ <- 0 until 3)
        e.array(op, stress(p16, 1 + e.rnd.nextInt(100), e.rnd).toSeq, Some(BigInt(16, e.rnd)))
    }
  }

  testpoint("vec_counters") {
    run(vec16, "vec_counters") { e =>
      for (op <- PositOp.all) {
        e.wr(CTRL, 1 << CTRL_CLEAR_COUNTERS)
        assert(Seq(CNT_NAR, CNT_SAT, CNT_INEXACT, CNT_ELEMENTS, CYCLES).forall(e.rd(_) == 0), "CTRL.clear")
        // wymuszone przypadki brzegowe + losowe
        val edge = Seq((BigInt(0x4000), BigInt(0)), (BigInt(0x7fff), BigInt(0x7fff)), (BigInt(0x0001), BigInt(0x0001)), (BigInt(0x8000), BigInt(0x4000)))
        val fs = e.array(op, edge ++ stress(p16, 300, e.rnd).toSeq)
        val want = Seq(fs.count(_.nar), fs.count(_.sat), fs.count(_.inexact)).map(_.toLong)
        val got  = Seq(e.rd(CNT_NAR), e.rd(CNT_SAT), e.rd(CNT_INEXACT))
        assert(got == want, s"${op.name}: liczniki $got, oczekiwane $want")
        assert(e.rd(CYCLES) > 0)
      }
    }
  }

  testpoint("vec_reg_ops") {
    run(vec16, "vec_reg_ops") { e =>
      def regOp(op : PositOp, a : BigInt, b : BigInt) : Unit = {
        e.wr(OP, PositAluOp.code(op)); e.wr(OPERAND_A, a.toLong); e.wr(OPERAND_B, b.toLong); e.wr(CTRL, 1 << CTRL_START)
        var s = e.rd(STATUS); var n = 0
        while ((s & (1 << STATUS_DONE)) == 0) { n += 1; assert(n < 1000); s = e.rd(STATUS) }
        val (want, f) = PositRef.withFlags(p16, op, a, b)
        assert(BigInt(e.rd(RESULT)) == want && ((s >> STATUS_FLAGS_SHIFT) & 7) == f.bits, s"${op.name}(0x${a.toString(16)}, 0x${b.toString(16)})")
      }
      for (op <- PositOp.all; (a, b) <- stress(p16, 50, e.rnd)) regOp(op, a, b)
      // dzialanie rejestrowe w trakcie tablicy; OP jest wspolny dla obu sciezek, wiec
      // rejestrowe liczy te sama operacje (mul) co tablica
      val pairs = stress(p16, 500, e.rnd).toSeq
      e.wr(OP, PositAluOp.code(PositOp.Mul)); e.wr(STREAM_CFG, 0)
      e.got.clear()
      e.readyFactor = 0.3f
      val t = e.send(pairs)
      e.wr(OPERAND_A, 0x4800); e.wr(OPERAND_B, 0x4800); e.wr(CTRL, 1 << CTRL_START)   // 2 * 2 rejestrowo
      t.join()
      e.cd.waitSamplingWhere(e.got.size == pairs.size)
      assert(BigInt(e.rd(RESULT)) == 0x5000, "rejestrowe 2 * 2 w trakcie tablicy")
      for (((a, b), (r, _)) <- pairs.zip(e.got)) assert(r == PositRef(p16, PositOp.Mul, a, b))
    }
  }

  testpoint("vec_stream_active") {
    run(vec16, "vec_stream_active") { e =>
      e.wr(OP, PositAluOp.code(PositOp.Add)); e.wr(STREAM_CFG, 0)
      e.readyFactor = 0.0f
      e.got.clear()
      e.send(Seq((BigInt(0x4000), BigInt(0x4000)), (BigInt(0x4800), BigInt(0x4000))), gaps = false).join()
      e.cd.waitSampling(10)
      assert(((e.rd(STATUS) >> STATUS_STREAM_ACTIVE) & 1) == 1, "stream_active przy zatrzymanym m_axis")
      e.readyFactor = 1.0f
      e.cd.waitSamplingWhere(e.got.size == 2)
      e.cd.waitSampling(5)
      assert(((e.rd(STATUS) >> STATUS_STREAM_ACTIVE) & 1) == 0, "stream_active po odebraniu wynikow")
    }
  }

  testpoint("vec_soft_reset") {
    run(vec16, "vec_soft_reset") { e =>
      e.wr(OP, PositAluOp.code(PositOp.Div)); e.wr(STREAM_CFG, 0)
      e.readyFactor = 0.0f
      e.got.clear()
      val t = e.send(stress(p16, 3, e.rnd).toSeq, gaps = false)
      e.cd.waitSampling(30)
      e.wr(CTRL, 1 << CTRL_SOFT_RESET)
      t.join()   // trzeci element moze czekac na tready: po resecie silnik go przyjmie
      e.cd.waitSampling(5)
      e.readyFactor = 1.0f
      e.cd.waitSampling(50)
      e.got.clear()
      assert(((e.rd(STATUS) >> STATUS_STREAM_ACTIVE) & 1) == 0 && !e.d.io.m_axis.tvalid.toBoolean, "po resecie")
      e.array(PositOp.Add, stress(p16, 100, e.rnd).toSeq)
    }
  }
}
