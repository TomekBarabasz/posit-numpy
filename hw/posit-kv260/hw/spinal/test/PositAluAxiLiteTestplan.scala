package posit.kv260

import spinal.core._
import spinal.core.sim._
import spinal.lib.bus.amba4.axilite.sim.AxiLite4Driver
import scala.io.Source
import scala.util.Random
import newhope.vertebra._
import newhope.vertebra.sim.{SimEnv, SimSeed}
import posit._
import posit.PositSimKit.stress
import posit.vector._
import PositAluRegs._

// =====================================================================
//  PositAluAxiLite: ALU za rejestrami AXI4-Lite (etap 1).
//
//  Zrodlo planu: plan etapu 1, "Co przetestowac" (symulacja ze
//  sterownikiem AXI4-Lite, ID/CONFIG, liczniki na znanych przypadkach).
//  Sterownik: AxiLite4Driver ze spinal.lib; sekwencja jak w Pythonie
//  (backend_hw.py): OP, A, B, CTRL.start, odpytywanie STATUS.done, RESULT.
//  Wzorzec: PositRef.withFlags.
// =====================================================================
object PositAluAxiLitePlan {
  val plan : Seq[Testpoint] = Seq(
    Testpoint("regs_python_match", Stage.V1,
      "Mapa rejestrow w sw/positnp/regs.py == PositAluRegs",
      checking = Seq("kazda stala z PositAluRegs.all ma w regs.py te sama wartosc")),

    Testpoint("axi_port_names", Stage.V1,
      "Nazwy portow rozpoznawalne przez IP packagera Vivado",
      checking = Seq("porty aclk, aresetn i s_axi_{aw,w,b,ar,r}* w wygenerowanym Verilogu",
                     "brak prefiksu io_ i portow clk / reset")),

    Testpoint("axi_id_config", Stage.V1,
      "ID_VERSION i CONFIG zgodne z parametrami generatora",
      checking = Seq("ID_VERSION == 0x50530100", "CONFIG: n, es i maska operacji")),

    Testpoint("axi_scalar_ops", Stage.V1,
      "Pojedyncze dzialania przez rejestry",
      stimulus = Seq("posit<16,2> i posit<8,2>, losowe pary dla kazdej operacji, sekwencja jak w backend_hw"),
      checking = Seq("RESULT == PositRef bit w bit", "STATUS[10:8] == flagi PositRef",
                     "STATUS: done = 1, busy = 0, error = 0 po wyniku")),

    Testpoint("axi_counters", Stage.V1,
      "Liczniki diagnostyczne na znanych przypadkach",
      stimulus = Seq("1 / 0, maxpos * maxpos, 1 / 3, 1 + 1", "CTRL.clear"),
      checking = Seq("kazdy przypadek podnosi wlasciwe liczniki dokladnie o 1, pozostale bez zmian",
                     "CYCLES: dzielenie trwa o divSteps / k + 1 taktow dluzej niz dodawanie",
                     "CTRL.clear zeruje wszystkie cztery liczniki")),

    Testpoint("axi_regs_readback", Stage.V2,
      "Rejestry OP, OPERAND_A, OPERAND_B",
      checking = Seq("odczyt == zapis na bitach [n-1:0], wyzsze bity czytane jako 0")),

    Testpoint("axi_illegal_op", Stage.V2,
      "Kod operacji spoza CONFIG.mask",
      stimulus = Seq("ALU zbudowane z add i mul", "start z kodem sub, div i 15"),
      checking = Seq("STATUS: error = 1, done = 1, busy = 0; RESULT i liczniki bez zmian",
                     "nastepne poprawne polecenie liczy sie i zeruje error")),

    Testpoint("axi_start_while_busy", Stage.V2,
      "Start i zapis argumentow w trakcie dzielenia",
      stimulus = Seq("posit<32,2>, k = 1 (dlugie dzielenie)", "w trakcie: nowe OP/A/B i drugi start"),
      checking = Seq("wynik == pierwsze polecenie", "drugi start zignorowany (jeden wynik, licznik taktow jednego dzielenia)")),

    Testpoint("axi_soft_reset", Stage.V2,
      "Miekki reset w trakcie dzielenia",
      stimulus = Seq("CTRL.soft_reset przy busy = 1"),
      checking = Seq("STATUS busy/done/error == 0 zaraz po resecie i 100 taktow pozniej", "kolejne polecenie liczy poprawnie (dzielnik nie zostaje w stanie busy)"))
  )
}

class PositAluAxiLiteTestplan extends TestplanSuite {
  import PositAluAxiLitePlan._
  override def testplan = plan

  val p8  = PositConfig(8)
  val p16 = PositConfig(16)
  val p32 = PositConfig(32)

  def sim(cfg : PositAluConfig, name : String) = SimEnv(Conventions.spinal, name).compile(new PositAluAxiLite(cfg))

  /** Sterownik rejestrowy: ta sama sekwencja co w sw/positnp/backend_hw.py. */
  class Regs(d : PositAluAxiLite) {
    val cd  = d.clockDomain
    val axi = AxiLite4Driver(d.io.s_axi, cd)
    def rd(a : Int) : Long = axi.read(a).toLong
    def wr(a : Int, v : Long) : Unit = axi.write(a, v)
    def start() : Unit = wr(CTRL, 1 << CTRL_START)
    def status() : Long = rd(STATUS)
    def waitDone(limit : Int = 1000) : Long = {
      var s = status(); var n = 0
      while ((s & (1 << STATUS_DONE)) == 0) { n += 1; assert(n < limit, f"STATUS.done nie przyszlo (STATUS = 0x$s%x)"); s = status() }
      s
    }
    /** (wynik, flagi, STATUS) */
    def alu(op : Int, a : BigInt, b : BigInt) : (BigInt, Int, Long) = {
      wr(OP, op); wr(OPERAND_A, a.toLong); wr(OPERAND_B, b.toLong); start()
      val s = waitDone()
      (BigInt(rd(RESULT)), ((s >> STATUS_FLAGS_SHIFT) & 7).toInt, s)
    }
    def counters() : Seq[Long] = Seq(rd(CNT_NAR), rd(CNT_SAT), rd(CNT_INEXACT))
  }

  def withRegs(compiled : SimCompiled[PositAluAxiLite], label : String)(body : (PositAluAxiLite, Regs, Random) => Unit) : Unit =
    compiled.doSim(label, seed = SimSeed(label)) { d =>
      d.clockDomain.forkStimulus(10)
      val r = new Regs(d)
      d.clockDomain.waitSampling(10)
      body(d, r, new Random(SimSeed(label)))
    }

  // ---- bez symulacji --------------------------------------------------------------

  testpoint("regs_python_match") {
    val path = "../../sw/positnp/regs.py"   // test forkowany z hw/posit-kv260
    val Line = """^([A-Z_]+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\b.*""".r
    val py = Source.fromFile(path).getLines().collect { case Line(k, v) =>
      k -> (if (v.startsWith("0x")) java.lang.Long.parseLong(v.drop(2), 16) else v.toLong)
    }.toMap
    val diff = PositAluRegs.all.filter { case (k, v) => !py.get(k).contains(v) }
    assert(diff.isEmpty, "regs.py rozni sie od PositAluRegs: " +
      diff.map { case (k, v) => f"$k: Scala 0x$v%x, Python ${py.get(k).map(x => f"0x$x%x").getOrElse("brak")}" }.mkString("; "))
  }

  testpoint("axi_port_names") {
    val dir = "simWorkspace/port_names"
    val rep = Kv260.spinal.copy(targetDirectory = dir).generateVerilog(new PositAluAxiLite(PositAluConfig(p16)))
    val all = Source.fromFile(s"$dir/${rep.toplevelName}.v").mkString
    val top = all.indexOf(s"module ${rep.toplevelName} (")
    assert(top >= 0, s"brak modulu ${rep.toplevelName}")
    val v   = all.substring(top, all.indexOf(");", top))   // tylko naglowek top-levelu
    val ports = """(?m)^\s*(input|output)\s+wire\s+(\[[^\]]+\]\s+)?(\w+)""".r.findAllMatchIn(v).map(_.group(3)).toSet
    val want = Set("aclk", "aresetn") ++
      Seq("awvalid", "awready", "awaddr", "awprot", "wvalid", "wready", "wdata", "wstrb", "bvalid", "bready", "bresp",
          "arvalid", "arready", "araddr", "arprot", "rvalid", "rready", "rdata", "rresp").map("s_axi_" + _)
    assert(want.subsetOf(ports), s"brak portow: ${(want -- ports).toSeq.sorted.mkString(", ")}; sa: ${ports.toSeq.sorted.mkString(", ")}")
    assert(ports == want, s"nadmiarowe porty: ${(ports -- want).toSeq.sorted.mkString(", ")}")
  }

  // ---- symulacje --------------------------------------------------------------------

  lazy val axi16  = sim(PositAluConfig(p16, divBitsPerCycle = 2), "axi_alu_16_k2")
  lazy val axi8   = sim(PositAluConfig(p8), "axi_alu_8_k1")
  lazy val axi32  = sim(PositAluConfig(p32, divBitsPerCycle = 1), "axi_alu_32_k1")
  lazy val axiSub = sim(PositAluConfig(p16, ops = Seq(PositOp.Add, PositOp.Mul)), "axi_alu_16_addmul")

  testpoint("axi_id_config") {
    withRegs(axi16, "axi_id_config") { (d, r, _) =>
      assert(r.rd(ID_VERSION) == 0x50530100L, f"ID_VERSION = 0x${r.rd(ID_VERSION)}%x")
      val cfgWord = r.rd(CONFIG)
      assert((cfgWord & 0xff) == 16 && ((cfgWord >> 8) & 0xf) == 2 && (cfgWord >> 16) == 0xf, f"CONFIG = 0x$cfgWord%x")
      assert(r.status() == 0, "STATUS po resecie")
    }
  }

  for ((name, compiled, c, count) <- Seq(("p16", () => axi16, p16, 300), ("p8", () => axi8, p8, 300))) {
    testpoint("axi_scalar_ops", name) {
      withRegs(compiled(), s"axi_scalar_$name") { (d, r, rnd) =>
        for (op <- PositOp.all; (a, b) <- stress(c, count, rnd)) {
          val (got, flags, s) = r.alu(PositAluOp.code(op), a, b)
          val (want, wantF)  = PositRef.withFlags(c, op, a, b)
          assert(got == want && flags == wantF.bits,
            f"$c ${op.name}: a=0x$a%x b=0x$b%x -> 0x$got%x flagi $flags, oczekiwane 0x$want%x flagi ${wantF.bits}")
          assert((s & 7) == (1 << STATUS_DONE), f"STATUS = 0x$s%x")
        }
        info(s"${4 * count} dzialan")
      }
    }
  }

  testpoint("axi_counters") {
    withRegs(axi16, "axi_counters") { (d, r, _) =>
      val one = 0x4000; val three = 0x4c00; val maxpos = 0x7fff
      val add = PositAluOp.code(PositOp.Add); val mul = PositAluOp.code(PositOp.Mul); val div = PositAluOp.code(PositOp.Div)
      def step(op : Int, a : Int, b : Int, delta : Seq[Long]) : Long = {
        val before = r.counters()
        r.wr(CTRL, 1 << CTRL_CLEAR_COUNTERS)
        assert(r.counters() == Seq(0L, 0L, 0L) && r.rd(CYCLES) == 0, "CTRL.clear nie wyzerowal licznikow")
        r.alu(op, a, b)
        assert(r.counters() == delta, s"op=$op a=$a b=$b: liczniki ${r.counters()}, oczekiwane $delta")
        r.rd(CYCLES)
      }
      step(div, one, 0, Seq(1, 0, 0))             // 1 / 0 = NaR
      step(mul, maxpos, maxpos, Seq(0, 1, 1))     // nasycenie
      step(div, one, three, Seq(0, 0, 1))         // 1/3 niedokladne
      val tAdd = step(add, one, one, Seq(0, 0, 0))
      val tDiv = step(div, one, one, Seq(0, 0, 0))
      val cfg  = d.cfg
      assert(tDiv - tAdd == cfg.divSteps / cfg.divBitsPerCycle + 1, s"CYCLES: add $tAdd, div $tDiv, divSteps ${cfg.divSteps}, k ${cfg.divBitsPerCycle}")
      info(s"CYCLES: add $tAdd, div $tDiv")
    }
  }

  testpoint("axi_regs_readback") {
    withRegs(axi16, "axi_regs_readback") { (d, r, rnd) =>
      for (_ <- 0 until 50) {
        val (op, a, b) = (rnd.nextInt(16).toLong, rnd.nextLong() & 0xffffffffL, rnd.nextLong() & 0xffffffffL)
        r.wr(OP, op); r.wr(OPERAND_A, a); r.wr(OPERAND_B, b)
        assert(r.rd(OP) == op && r.rd(OPERAND_A) == (a & 0xffff) && r.rd(OPERAND_B) == (b & 0xffff),
          f"odczyt: OP=0x${r.rd(OP)}%x A=0x${r.rd(OPERAND_A)}%x B=0x${r.rd(OPERAND_B)}%x, zapis 0x$op%x 0x$a%x 0x$b%x")
      }
    }
  }

  testpoint("axi_illegal_op") {
    withRegs(axiSub, "axi_illegal_op") { (d, r, _) =>
      val (first, _, _) = r.alu(PositAluOp.code(PositOp.Add), 0x4000, 0x4000)
      for (code <- Seq(PositAluOp.code(PositOp.Sub), PositAluOp.code(PositOp.Div), 15)) {
        r.wr(OP, code); r.start()
        val s = r.waitDone()
        assert((s & 7) == ((1 << STATUS_DONE) | (1 << STATUS_ERROR)), f"kod $code: STATUS = 0x$s%x")
        assert(BigInt(r.rd(RESULT)) == first, "RESULT zmieniony przez niedozwolony kod")
      }
      assert(r.counters() == Seq(0L, 0L, 0L))
      val (got, _, s) = r.alu(PositAluOp.code(PositOp.Mul), 0x4800, 0x4800)   // 2 * 2
      assert(got == 0x5000 && (s & 7) == (1 << STATUS_DONE), f"po bledzie: 0x$got%x, STATUS 0x$s%x")
    }
  }

  testpoint("axi_start_while_busy") {
    withRegs(axi32, "axi_start_while_busy") { (d, r, _) =>
      val div = PositAluOp.code(PositOp.Div)
      val (a, b) = (BigInt(0x40000000L), BigInt(0x4c000000L))   // 1 / 3
      r.wr(CTRL, 1 << CTRL_CLEAR_COUNTERS)
      r.wr(OP, div); r.wr(OPERAND_A, a.toLong); r.wr(OPERAND_B, b.toLong); r.start()
      assert((r.status() & (1 << STATUS_BUSY)) != 0, "brak busy po starcie dzielenia")
      r.wr(OP, PositAluOp.code(PositOp.Add)); r.wr(OPERAND_A, 0x48000000L); r.start()
      r.waitDone()
      d.clockDomain.waitSampling(50)
      assert(BigInt(r.rd(RESULT)) == PositRef(p32, PositOp.Div, a, b), "wynik nie z pierwszego polecenia")
      assert(r.counters() == Seq(0L, 0L, 1L), s"liczniki ${r.counters()}: wiecej niz jeden wynik?")
    }
  }

  testpoint("axi_soft_reset") {
    withRegs(axi32, "axi_soft_reset") { (d, r, _) =>
      val div = PositAluOp.code(PositOp.Div)
      r.wr(OP, div); r.wr(OPERAND_A, 0x40000000L); r.wr(OPERAND_B, 0x4c000000L); r.start()
      assert((r.status() & (1 << STATUS_BUSY)) != 0, "brak busy po starcie dzielenia")
      r.wr(CTRL, 1 << CTRL_SOFT_RESET)
      assert((r.status() & 7) == 0, f"STATUS po miekkim resecie: 0x${r.status()}%x")
      d.clockDomain.waitSampling(100)
      assert((r.status() & 7) == 0, "wynik przerwanego dzielenia pojawil sie po resecie")
      val (got, _, s) = r.alu(div, 0x48000000L, 0x48000000L)   // 2 / 2
      assert(got == 0x40000000L && (s & 7) == (1 << STATUS_DONE), f"po resecie: 0x$got%x, STATUS 0x$s%x")
    }
  }
}
