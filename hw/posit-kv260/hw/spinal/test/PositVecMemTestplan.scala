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
//  Etap 6: PositVecAxi 5.0 z wlasnym masterem AXI4 (PositMemMover).
//
//  Pamiec: AxiMemModel - losowe ready / przerwy na kazdym kanale, dane
//  roznych ID w dowolnym przeplocie, sprawdzanie protokolu (valid do ready,
//  INCR, wyrownanie, <= 16 slow, bez przekraczania 4 KB, WLAST, zakres).
//  Sekwencja jak w backend_hw: OP / OPERAND_*, MEM_*, CTRL.mem_start,
//  MEM_STATUS.done. Wzorce: PositRef, QuireRef.
// =====================================================================
object PositVecMemPlan {
  val plan : Seq[Testpoint] = Seq(
    Testpoint("mem_regs", Stage.V1,
      "Wersja 5.0, CONFIG.has_master, rejestry MEM_*",
      checking = Seq("ID_VERSION == 0x50530500", "CONFIG bit 14 i maska z FMA3 (bit 12)",
                     "MEM_*: odczyt == zapis, po resecie pusty zakres [0, 0)")),

    Testpoint("mem_elementwise", Stage.V1,
      "Wszystkie operacje na tablicach w pamieci",
      stimulus = Seq("add, sub, mul, div, sqrt, FMA (c w OPERAND_C), AXPY, FMA3 (trzy tablice), F2P, P2F",
                     "dlugosci 1, 3, 4, 5, 17, 100, 1000 i losowe; bufory przecinajace granice 4 KB"),
      checking = Seq("wynik w pamieci == PositRef bit w bit", "bajty za wynikiem nietkniete (WSTRB)",
                     "protokol AXI4 bez naruszen")),

    Testpoint("mem_formats", Stage.V1,
      "posit16 i posit8: 8 i 16 elementow w slowie 128 b",
      checking = Seq("add, FMA3 i dot bit w bit, czesciowe ostatnie slowo")),

    Testpoint("mem_scalar_inplace", Stage.V1,
      "Tablica (+) skalar bez odczytu b; wynik w miejscu (DST == SRC_A)",
      checking = Seq("wynik == PositRef", "dzialanie w miejscu poprawne")),

    Testpoint("mem_reduce", Stage.V1,
      "sum / dot / sumsq z pamieci, bez zapisu",
      checking = Seq("RESULT i flagi == QuireRef po quire_round", "zero transakcji AW / W")),

    Testpoint("mem_reject", Stage.V1,
      "Polecenia odrzucone przed startem",
      stimulus = Seq("zrodlo albo cel poza [MEM_LO, MEM_HI), koniec za MEM_HI", "adres niewyrownany do 16 B", "len = 0"),
      checking = Seq("MEM_STATUS: done + err_range / err_align (len = 0: tylko done)", "zero transakcji AR / AW")),

    Testpoint("mem_slverr", Stage.V2,
      "SLVERR na odczycie i na zapisie",
      checking = Seq("MEM_STATUS.err_resp, polecenie konczy sie (bez zawieszenia)")),

    Testpoint("mem_irq", Stage.V2,
      "Przerwanie po poleceniu",
      checking = Seq("irq = done gdy irq_en; CTRL.mem_ack kasuje", "bez irq_en irq = 0")),

    Testpoint("mem_throughput", Stage.V2,
      "add 4096 elementow posit32 bez przerw w pamieci",
      checking = Seq("MEM_CYCLES <= 1,1 * len + 200 (1 element / takt mimo dwoch strumieni odczytu i zapisu)"))
  )
}

class PositVecMemTestplan extends TestplanSuite {
  import PositVecMemPlan._
  override def testplan = plan

  def cfgFor(c : PositConfig, units : Int = 2) =
    PositAluConfig(c, PositOp.all, divBitsPerCycle = 4, pipe = PositPipeline.full, sqrt = true, fma = true,
                   convert = c.n == 32, slowUnits = units, fma3 = true)
  val p8  = PositConfig(8)
  val p16 = PositConfig(16)
  val p32 = PositConfig(32)
  lazy val m32 = SimEnv(Conventions.spinal, "vecmem_32").compile(new PositVecAxi(cfgFor(p32), withMaster = true))
  lazy val m16 = SimEnv(Conventions.spinal, "vecmem_16").compile(new PositVecAxi(cfgFor(p16, 1), withMaster = true))
  lazy val m8  = SimEnv(Conventions.spinal, "vecmem_8").compile(new PositVecAxi(cfgFor(p8, 1), withMaster = true))

  val BASE = 0x10000000L
  val SIZE = 1L << 20

  class Env(val d : PositVecAxi, val rnd : Random) {
    val c   = d.c
    val cd  = d.clockDomain
    val bytes = c.n / 8
    val axi = AxiLite4Driver(d.io.s_axi, cd)
    val mem = new AxiMemModel(d.io.m_axi, cd, rnd)
    mem.lo = BASE
    mem.hi = BASE + SIZE
    def rd(a : Int) : Long = axi.read(a).toLong
    def wr(a : Int, v : Long) : Unit = axi.write(a, v)
    wr(PositVecRegs.MEM_LO, BASE)
    wr(PositVecRegs.MEM_HI, BASE + SIZE)

    def put(addr : Long, xs : Seq[BigInt]) : Unit = mem.writeWords(addr, bytes, xs)
    def get(addr : Long, n : Int) : Seq[BigInt] = mem.readWords(addr, bytes, n)

    /** Jedno polecenie MEM: rejestry, start, czekanie na done; zwraca MEM_STATUS. */
    def run(op : Int, len : Long, a : Long, b : Option[Long] = None, cc : Option[Long] = None,
            dst : Option[Long] = None, irq : Boolean = false) : Long = {
      wr(OP, op)
      wr(MEM_LEN, len)
      wr(MEM_SRC_A, a)
      b.foreach(wr(MEM_SRC_B, _))
      cc.foreach(wr(MEM_SRC_C, _))
      dst.foreach(wr(MEM_DST, _))
      wr(MEM_CFG, (if (b.nonEmpty) 1 << MEM_CFG_READ_B else 0) | (if (cc.nonEmpty) 1 << MEM_CFG_READ_C else 0) |
                  (if (dst.nonEmpty) 1 << MEM_CFG_WRITE else 0) | (if (irq) 1 << MEM_CFG_IRQ_EN else 0))
      wr(CTRL, 1 << CTRL_MEM_START)
      var s = rd(MEM_STATUS); var n = 0
      while ((s & (1 << MEM_ST_DONE)) == 0) {
        n += 1; assert(n < 20000, f"MEM_STATUS.done nie przyszedl (0x$s%x), op=$op len=$len cfg=0x${rd(MEM_CFG)}%x")
        cd.waitSampling(10)
        s = rd(MEM_STATUS)
      }
      s
    }

    def checkProtocol(label : String) : Unit =
      assert(mem.violations.isEmpty, s"$label: naruszenia AXI4: ${mem.violations.mkString("; ")}")

    def rand(n : Int) : Seq[BigInt] = Seq.fill(n)(BigInt(c.n, rnd))
    /** Losowy adres w buforze, wyrownany do 16 B; czesto tuz przed granica 4 KB. */
    def addr(region : Int, n : Int) : Long = {
      val off = if (rnd.nextBoolean()) 4096 - 16 * (1 + rnd.nextInt(8)) else 16 * rnd.nextInt(64)
      BASE + region * 0x40000L + off
    }
  }

  def sim(compiled : SimCompiled[PositVecAxi], label : String)(body : Env => Unit) : Unit =
    compiled.doSim(label, seed = SimSeed(label)) { d =>
      // wszystkie wejscia w stanie spoczynku od pierwszego taktu (Verilator: inaczej losowe)
      d.io.s_axi.aw.valid #= false; d.io.s_axi.w.valid #= false; d.io.s_axi.ar.valid #= false
      d.io.s_axi.b.ready #= true; d.io.s_axi.r.ready #= true
      val m = d.io.m_axi
      m.ar.ready #= false; m.aw.ready #= false; m.w.ready #= false; m.r.valid #= false; m.b.valid #= false
      d.clockDomain.forkStimulus(10)
      SimTimeout(10L * 5000000)
      d.clockDomain.waitSampling(20)
      val e = new Env(d, new Random(SimSeed(label)))
      body(e)
      e.checkProtocol(label)
    }

  def assertSeq(label : String, got : Seq[BigInt], want : Seq[BigInt]) : Unit = {
    val bad = got.zip(want).indexWhere { case (g, w) => g != w }
    assert(got.size == want.size && bad < 0, if (bad < 0) s"$label: dlugosc" else f"$label #$bad: 0x${got(bad)}%x, oczekiwane 0x${want(bad)}%x")
  }

  // ---- testpointy ---------------------------------------------------------------------------

  testpoint("mem_regs") {
    sim(m32, "mem_regs") { e =>
      assert(e.rd(ID_VERSION) == 0x50530500L, f"ID_VERSION 0x${e.rd(ID_VERSION)}%x")
      val cfg = e.rd(CONFIG)
      assert(((cfg >> CONFIG_HAS_MASTER) & 1) == 1 && ((cfg >> (16 + OP_FMA3)) & 1) == 1, f"CONFIG 0x$cfg%x")
      for ((r, v) <- Seq(MEM_LEN -> 123L, MEM_SRC_A -> 0x1230L, MEM_SRC_B -> 0x4560L, MEM_SRC_C -> 0x7890L,
                         MEM_DST -> 0xabc0L, MEM_CFG -> 0xfL)) {
        e.wr(r, v); assert(e.rd(r) == v, f"rejestr 0x$r%x: 0x${e.rd(r)}%x")
      }
      assert(e.rd(MEM_LO) == BASE && e.rd(MEM_HI) == BASE + SIZE)
    }
  }

  testpoint("mem_elementwise") {
    sim(m32, "mem_elementwise") { e =>
      val c = e.c
      val lens = Seq(1, 3, 4, 5, 17, 100, 1000) ++ Seq.fill(3)(1 + e.rnd.nextInt(3000))
      for (len <- lens) {
        val (a, b, cc, dst) = (e.addr(0, len), e.addr(1, len), e.addr(2, len), e.addr(3, len))
        val xs = stress(c, len, e.rnd).toSeq
        val as = xs.map(_._1); val bs = xs.map(_._2); val cs = e.rand(len)
        e.put(a, as); e.put(b, bs); e.put(cc, cs)
        val guard = BigInt("5a5a5a5a", 16)
        def go(label : String, op : Int, want : Seq[BigInt], useB : Boolean = true, useC : Boolean = false, k : Long = 0) : Unit = {
          e.put(dst + 4L * len, Seq(guard))
          e.wr(OPERAND_C, k)
          val s = e.run(op, len, a, if (useB) Some(b) else None, if (useC) Some(cc) else None, Some(dst))
          assert((s & 0x1c) == 0, f"$label len=$len: MEM_STATUS 0x$s%x")
          assertSeq(s"$label len=$len", e.get(dst, len), want)
          assert(e.get(dst + 4L * len, 1).head == guard, s"$label len=$len: zapis za koncem tablicy")
        }
        for (op <- PositOp.all) go(op.name, PositAluOp.code(op), xs.map { case (x, y) => PositRef(c, op, x, y) })
        go("sqrt", OP_SQRT, as.map(PositRef.sqrt(c, _)._1), useB = false)
        val k = BigInt(32, e.rnd)
        go("fma", OP_FMA, xs.map { case (x, y) => PositRef.fma(c, x, y, k)._1 }, k = k.toLong)
        go("fma3", OP_FMA3, xs.zip(cs).map { case ((x, y), z) => PositRef.fma(c, x, y, z)._1 }, useC = true)
        e.wr(OPERAND_B, k.toLong)
        go("axpy", OP_AXPY, xs.map { case (x, y) => PositRef.fma(c, x, k, y)._1 })
        go("f2p", OP_F2P, as.map(PositRef.fromFloat32(c, _)._1), useB = false)
        go("p2f", OP_P2F, as.map(PositRef.toFloat32(c, _)._1), useB = false)
      }
      info(s"AR ${e.mem.arCount}, AW ${e.mem.awCount}, slowa R ${e.mem.rBeats}, W ${e.mem.wBeats}")
    }
  }

  for ((name, compiled, c) <- Seq(("p16", () => m16, p16), ("p8", () => m8, p8))) {
    testpoint("mem_formats", name) {
      sim(compiled(), s"mem_formats_$name") { e =>
        for (len <- Seq(1, 7, 8, 9, 15, 16, 17, 333, 1 + e.rnd.nextInt(2000))) {
          val (a, b, cc, dst) = (e.addr(0, len), e.addr(1, len), e.addr(2, len), e.addr(3, len))
          val xs = stress(c, len, e.rnd).toSeq
          val cs = e.rand(len)
          e.put(a, xs.map(_._1)); e.put(b, xs.map(_._2)); e.put(cc, cs)
          e.put(dst + e.bytes.toLong * len, Seq.fill(16)(BigInt(0x5a)))
          assert((e.run(PositAluOp.code(PositOp.Add), len, a, Some(b), None, Some(dst)) & 0x1c) == 0)
          assertSeq(s"add $len", e.get(dst, len), xs.map { case (x, y) => PositRef(c, PositOp.Add, x, y) })
          assert(e.get(dst + e.bytes.toLong * len, 16).forall(_ == 0x5a), s"len=$len: zapis za koncem (WSTRB)")
          assert((e.run(OP_FMA3, len, a, Some(b), Some(cc), Some(dst)) & 0x1c) == 0)
          assertSeq(s"fma3 $len", e.get(dst, len), xs.zip(cs).map { case ((x, y), z) => PositRef.fma(c, x, y, z)._1 })
          e.wr(CTRL, 1 << CTRL_QUIRE_CLEAR)
          e.run(OP_DOT, len, a, Some(b))
          e.wr(CTRL, 1 << CTRL_QUIRE_ROUND)
          var s = e.rd(STATUS); while ((s & (1 << STATUS_DONE)) == 0) s = e.rd(STATUS)
          val (q, nar) = QuireRef.accumulate(c, xs.iterator.map { case (x, y) => (x, Some(y)) })
          assert(BigInt(e.rd(RESULT)) == QuireRef.round(c, q, nar)._1, s"dot $len")
        }
      }
    }
  }

  testpoint("mem_scalar_inplace") {
    sim(m32, "mem_scalar_inplace") { e =>
      val c = e.c
      for (len <- Seq(1, 5, 64, 1 + e.rnd.nextInt(2000))) {
        val a = e.addr(0, len)
        val xs = e.rand(len)
        val k = BigInt(32, e.rnd)
        e.put(a, xs)
        e.wr(OPERAND_B, k.toLong)
        e.wr(STREAM_CFG, 1 << STREAM_CFG_SCALAR_B)
        assert((e.run(PositAluOp.code(PositOp.Mul), len, a, None, None, Some(a)) & 0x1c) == 0)   // w miejscu
        assertSeq(s"mul skalar w miejscu $len", e.get(a, len), xs.map(PositRef(c, PositOp.Mul, _, k)))
        e.wr(STREAM_CFG, 0)
      }
    }
  }

  testpoint("mem_reduce") {
    sim(m32, "mem_reduce") { e =>
      val c = e.c
      val aw0 = e.mem.awCount
      for (op <- Seq(OP_SUM, OP_DOT, OP_SUMSQ); len <- Seq(1, 3, 1000)) {
        val (a, b) = (e.addr(0, len), e.addr(1, len))
        val xs = stress(c, len, e.rnd).toSeq
        e.put(a, xs.map(_._1)); e.put(b, xs.map(_._2))
        e.wr(CTRL, 1 << CTRL_QUIRE_CLEAR)
        val s = e.run(op, len, a, if (op == OP_DOT) Some(b) else None)
        assert((s & 0x1c) == 0, f"MEM_STATUS 0x$s%x")
        e.wr(CTRL, 1 << CTRL_QUIRE_ROUND)
        var st = e.rd(STATUS); while ((st & (1 << STATUS_DONE)) == 0) st = e.rd(STATUS)
        val terms = xs.map { case (x, y) => op match { case OP_SUM => (x, None); case OP_DOT => (x, Some(y)); case _ => (x, Some(x)) } }
        val (q, nar) = QuireRef.accumulate(c, terms.iterator)
        val (want, wf) = QuireRef.round(c, q, nar)
        assert(BigInt(e.rd(RESULT)) == want && ((st >> STATUS_FLAGS_SHIFT) & 7) == wf.bits, s"op=$op len=$len")
      }
      assert(e.mem.awCount == aw0, "redukcja zapisywala")
    }
  }

  testpoint("mem_reject") {
    sim(m32, "mem_reject") { e =>
      val add = PositAluOp.code(PositOp.Add)
      val (ar0, aw0) = (e.mem.arCount, e.mem.awCount)
      val cases = Seq(
        ("zrodlo przed zakresem", BASE - 16, BASE, MEM_ST_ERR_RANGE),
        ("cel za zakresem", BASE, BASE + SIZE, MEM_ST_ERR_RANGE),
        ("koniec za MEM_HI", BASE + SIZE - 32, BASE, MEM_ST_ERR_RANGE),
        ("zrodlo niewyrownane", BASE + 4, BASE, MEM_ST_ERR_ALIGN),
        ("cel niewyrownany", BASE, BASE + 8, MEM_ST_ERR_ALIGN))
      for ((label, a, dst, bit) <- cases) {
        val s = e.run(add, 100, a, Some(BASE + 0x1000), None, Some(dst))
        assert(((s >> bit) & 1) == 1 && (s & 1) == 0, f"$label: MEM_STATUS 0x$s%x")
      }
      val s0 = e.run(add, 0, BASE, Some(BASE), None, Some(BASE))
      assert((s0 & 0x1c) == 0, f"len=0: MEM_STATUS 0x$s0%x")
      e.cd.waitSampling(50)
      assert(e.mem.arCount == ar0 && e.mem.awCount == aw0, s"transakcje po odrzuceniu: AR ${e.mem.arCount - ar0}, AW ${e.mem.awCount - aw0}")
    }
  }

  testpoint("mem_slverr") {
    sim(m32, "mem_slverr") { e =>
      val add = PositAluOp.code(PositOp.Add)
      val len = 500
      val (a, b, dst) = (BASE, BASE + 0x10000, BASE + 0x20000)
      e.mem.slverr += a + 16 * 40
      val s1 = e.run(add, len, a, Some(b), None, Some(dst))
      assert(((s1 >> MEM_ST_ERR_RESP) & 1) == 1, f"odczyt SLVERR: MEM_STATUS 0x$s1%x")
      e.mem.slverr.clear()
      e.mem.slverr += dst + 16 * 7
      val s2 = e.run(add, len, a, Some(b), None, Some(dst))
      assert(((s2 >> MEM_ST_ERR_RESP) & 1) == 1, f"zapis SLVERR: MEM_STATUS 0x$s2%x")
      e.mem.slverr.clear()
      val s3 = e.run(add, len, a, Some(b), None, Some(dst))
      assert((s3 & 0x1c) == 0, f"po bledach: MEM_STATUS 0x$s3%x")
    }
  }

  testpoint("mem_irq") {
    sim(m32, "mem_irq") { e =>
      val add = PositAluOp.code(PositOp.Add)
      e.run(add, 50, BASE, Some(BASE), None, Some(BASE + 0x1000))
      assert(!e.d.io.irq.toBoolean, "irq bez irq_en")
      e.run(add, 50, BASE, Some(BASE), None, Some(BASE + 0x1000), irq = true)
      assert(e.d.io.irq.toBoolean, "brak irq po poleceniu")
      e.wr(CTRL, 1 << CTRL_MEM_ACK)
      e.cd.waitSampling(2)
      assert(!e.d.io.irq.toBoolean && ((e.rd(MEM_STATUS) >> MEM_ST_DONE) & 1) == 0, "CTRL.mem_ack nie skasowal")
    }
  }

  testpoint("mem_throughput") {
    sim(m32, "mem_throughput") { e =>
      e.mem.stall = 0.0
      e.mem.maxDelay = 20
      val len = 4096
      e.put(BASE, e.rand(len)); e.put(BASE + 0x10000, e.rand(len))
      e.run(PositAluOp.code(PositOp.Add), len, BASE, Some(BASE + 0x10000), None, Some(BASE + 0x20000))
      val cyc = e.rd(MEM_CYCLES)
      assert(cyc <= 1.1 * len + 200, s"$len elementow w $cyc taktach")
      info(f"add posit32: $len elementow w $cyc taktach (${len.toDouble / cyc}%.3f / takt)")
    }
  }
}
