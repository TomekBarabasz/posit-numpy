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
      checking = Seq("ID_VERSION == 0x50530504", "CONFIG bit 14 i maska z FMA3 (bit 12)",
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

    Testpoint("mem_lanes", Stage.V1,
      "Szerszy silnik: L elementow na takt (posit32 x 2, posit16 x 4, posit8 x 8)",
      stimulus = Seq("add, mul, div, sqrt, AXPY, FMA3; dlugosci 1 .. 2L+1 i losowe, niepodzielne przez L", "sum / dot"),
      checking = Seq("wynik == PositRef bit w bit, bajty za tablica nietkniete (WSTRB ostatniej grupy)",
                     "CNT_ELEMENTS == len (tory za koncem tablicy sie nie licza)", "ENGINE_CFG[23:16] == L",
                     "add 4096 elementow w <= 1,1 * len / L + 200 taktach")),

    Testpoint("mem_throughput", Stage.V2,
      "add 4096 elementow posit32 bez przerw w pamieci",
      checking = Seq("MEM_CYCLES <= 1,1 * len + 200 (1 element / takt mimo dwoch strumieni odczytu i zapisu)")),

    Testpoint("mem_gemv", Stage.V1,
      "Etap 7a: Y = A X dokladnie (OP_XLOAD, OP_GEMV), posit32 x 1 / x 2, posit16 x 4, posit8 x 8; P = 1 i 4 kolumny",
      stimulus = Seq("M x K: 1x1, 1x5, 3x1, 2x2, 5x3, 7x17, 33x10, 9x maxK i losowe; K = 1 (zaokraglenia co takt)",
                     "elementy A z rozkladu stress (NaR, zera, skrajne skale), losowe opoznienia pamieci"),
      checking = Seq("y[i, c] == QuireRef.round(sum_j A[i, j] x_c[j]) bit w bit, wierszami, dla cols = 1 .. P",
                     "bajty za y nietkniete", "CONFIG.has_gemv i maska OP 13/14, GEMV_CFG == P << 16 | maxK, wersja 5.2",
                     "redukcje (dot) po gemv nadal poprawne")),

    Testpoint("mem_gemv_reject", Stage.V1,
      "gemv z niepasujacym ksztaltem",
      stimulus = Seq("MEM_LEN != MEM_WLEN * GEMV_K", "GEMV_K = 0", "GEMV_K > maxK", "XLOAD MEM_LEN != K * cols",
                     "GEMV_COLS = 0 albo > P"),
      checking = Seq("MEM_STATUS: done + err_shape, zero transakcji AR / AW")),

    Testpoint("mem_gemv_throughput", Stage.V2,
      "gemv 64 x 64 posit32 bez przerw w pamieci, P = 1 i P = 4 kolumny",
      checking = Seq("MEM_CYCLES <= 1,1 * M * K + 200 (1 element A / takt, zaokraglenie wiersza w cieniu nastepnego)",
                     "P = 4: 4 kolumny w tym samym czasie (4 MAC / takt)")),

    Testpoint("mem_table", Stage.V1,
      "5.3: y = T[a] z tablicy 2^n (OP_TABLE, TABLE_CTRL.load), posit16 x1 / x4, posit8 x8, 1 .. L / 2 kopii",
      stimulus = Seq("losowa tablica, potem druga (przeladowanie)", "dlugosci 1 .. 2L + 1, 97 i losowe",
                     "przeplot z add (zwykla sciezka po tablicy)"),
      checking = Seq("wynik == T[a] bit w bit, bajty za wynikiem nietkniete, CNT_ELEMENTS == len",
                     "TABLE_CFG == 2 C << 8 | n, CONFIG: bit OP 15, wersja 5.3",
                     "ladowanie z MEM_LEN != 2^n: err_shape, bez transakcji",
                     "przepustowosc: L elementow na max(1, L / 2C) taktow")),

    Testpoint("mem_fir", Stage.V1,
      "5.4: FIR y[i] = sum_j x[i + j] h[j] przez GEMV z oknem (GEMV_MODE.window), posit32 x1 / x2, posit16 x4, posit8 x8",
      stimulus = Seq("T odczepow 1 .. maxK - cols + 1, cols 1 .. P, M wierszy (cols wyjsc kazdy)",
                     "sygnal kilka razy dluzszy niz bufor kolowy (zawijanie, czekanie zapisu)",
                     "elementy z rozkladu stress, losowe opoznienia pamieci"),
      checking = Seq("y == QuireRef.round(sum_j x[i + j] h[j]) bit w bit dla kazdego wyjscia",
                     "sygnal czytany raz (slowa R == ceil(MEM_LEN / slowo))", "bajty za y nietkniete",
                     "MEM_LEN != (M - 1) * cols + K, M = 0, cols > K: err_shape",
                     "przepustowosc: M wierszy po K taktow (cols * T MAC na wiersz)")),

    Testpoint("mem_table_absent", Stage.V1,
      "Silnik bez tablicy",
      checking = Seq("TABLE_CFG == 0, bit OP 15 w CONFIG == 0"))
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
  val GEMV_MAXK = 64
  def gemvd(c : PositConfig, l : Int, cols : Int = 1) =
    SimEnv(Conventions.spinal, s"vecmem_gemv_${c.n}_L${l}_P$cols").compile(
      new PositVecAxi(cfgFor(c, 1), withMaster = true, memCfg = Some(MemMoverConfig(c.n, lanes = l)),
                      withGemv = true, gemvMaxK = GEMV_MAXK, gemvCols = cols))
  def tabled(c : PositConfig, l : Int, copies : Int) =
    SimEnv(Conventions.spinal, s"vecmem_table_${c.n}_L${l}_C$copies").compile(
      new PositVecAxi(cfgFor(c, 1), withMaster = true, memCfg = Some(MemMoverConfig(c.n, lanes = l)),
                      tableCopies = copies))
  def laned(c : PositConfig, l : Int) =
    SimEnv(Conventions.spinal, s"vecmem_${c.n}_L$l").compile(
      new PositVecAxi(cfgFor(c, 2), withMaster = true, memCfg = Some(MemMoverConfig(c.n, lanes = l))))

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

    /** Kolumny x (cols po K elementow, po kolei pod x) -> bufory (XLOAD), potem Y = A X (GEMV, M x cols
      * wierszami pod y); zwraca MEM_STATUS GEMV. */
    def gemv(m : Int, k : Int, a : Long, x : Long, y : Long, cols : Int = 1) : Long = {
      wr(GEMV_K, k)
      wr(GEMV_COLS, cols)
      wr(MEM_WLEN, m)
      val s0 = run(OP_XLOAD, k.toLong * cols, x)
      assert((s0 & 0x3c) == 0, f"XLOAD: MEM_STATUS 0x$s0%x")
      run(OP_GEMV, m.toLong * k, a, None, None, Some(y))
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

  def mem(e : Env) : AxiMemModel = e.mem

  def assertSeq(label : String, got : Seq[BigInt], want : Seq[BigInt]) : Unit = {
    val bad = got.zip(want).indexWhere { case (g, w) => g != w }
    assert(got.size == want.size && bad < 0, if (bad < 0) s"$label: dlugosc" else f"$label #$bad: 0x${got(bad)}%x, oczekiwane 0x${want(bad)}%x")
  }

  // ---- testpointy ---------------------------------------------------------------------------

  testpoint("mem_regs") {
    sim(m32, "mem_regs") { e =>
      assert(e.rd(ID_VERSION) == 0x50530504L, f"ID_VERSION 0x${e.rd(ID_VERSION)}%x")
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

  for ((c, l) <- Seq((p32, 2), (p16, 4), (p8, 8))) {
    testpoint("mem_lanes", s"p${c.n}_L$l") {
      sim(laned(c, l), s"mem_lanes_${c.n}_$l") { e =>
        assert(((e.rd(PositVecRegs.ENGINE_CFG) >> 16) & 0xff) == l, f"ENGINE_CFG 0x${e.rd(PositVecRegs.ENGINE_CFG)}%x")
        val lens = (1 to 2 * l + 1) ++ Seq(97, 1 + e.rnd.nextInt(3000))
        for (len <- lens) {
          val (a, b, cc, dst) = (e.addr(0, len), e.addr(1, len), e.addr(2, len), e.addr(3, len))
          val xs = stress(c, len, e.rnd).toSeq
          val cs = e.rand(len)
          e.put(a, xs.map(_._1)); e.put(b, xs.map(_._2)); e.put(cc, cs)
          val guard = Seq.fill(32)(BigInt(0x5a))
          def go(label : String, op : Int, want : Seq[BigInt], useB : Boolean = true, useC : Boolean = false) : Unit = {
            mem(e).writeWords(dst + e.bytes.toLong * len, 1, guard)
            e.wr(CTRL, 1 << CTRL_CLEAR_COUNTERS)
            val s = e.run(op, len, a, if (useB) Some(b) else None, if (useC) Some(cc) else None, Some(dst))
            assert((s & 0x1c) == 0, f"$label len=$len: MEM_STATUS 0x$s%x")
            assertSeq(s"$label L=$l len=$len", e.get(dst, len), want)
            assert(mem(e).readWords(dst + e.bytes.toLong * len, 1, 32) == guard, s"$label L=$l len=$len: zapis za koncem")
            assert(e.rd(CNT_ELEMENTS) == len, s"$label L=$l len=$len: CNT_ELEMENTS ${e.rd(CNT_ELEMENTS)}")
          }
          for (op <- Seq(PositOp.Add, PositOp.Mul, PositOp.Div))
            go(op.name, PositAluOp.code(op), xs.map { case (x, y) => PositRef(c, op, x, y) })
          go("sqrt", OP_SQRT, xs.map(p => PositRef.sqrt(c, p._1)._1), useB = false)
          val k = BigInt(c.n, e.rnd)
          e.wr(OPERAND_B, k.toLong)
          go("axpy", OP_AXPY, xs.map { case (x, y) => PositRef.fma(c, x, k, y)._1 })
          go("fma3", OP_FMA3, xs.zip(cs).map { case ((x, y), z) => PositRef.fma(c, x, y, z)._1 }, useC = true)
          for (op <- Seq(OP_SUM, OP_DOT)) {
            e.wr(CTRL, 1 << CTRL_QUIRE_CLEAR)
            e.run(op, len, a, if (op == OP_DOT) Some(b) else None)
            e.wr(CTRL, 1 << CTRL_QUIRE_ROUND)
            var st = e.rd(STATUS); while ((st & (1 << STATUS_DONE)) == 0) st = e.rd(STATUS)
            val (q, nar) = QuireRef.accumulate(c, xs.iterator.map { case (x, y) => (x, if (op == OP_DOT) Some(y) else None) })
            assert(BigInt(e.rd(RESULT)) == QuireRef.round(c, q, nar)._1, s"op=$op L=$l len=$len")
          }
        }
        e.mem.stall = 0.0
        e.mem.maxDelay = 20
        val len = 4096
        e.put(BASE, e.rand(len)); e.put(BASE + 0x10000, e.rand(len))
        e.run(PositAluOp.code(PositOp.Add), len, BASE, Some(BASE + 0x10000), None, Some(BASE + 0x20000))
        val cyc = e.rd(MEM_CYCLES)
        assert(cyc <= 1.1 * len / l + 200, s"$len elementow, L=$l: $cyc taktow")
        info(f"add posit${c.n}, L=$l: $len elementow w $cyc taktach (${len.toDouble / cyc}%.2f / takt)")
      }
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

  // ---- etap 7a: gemv ------------------------------------------------------------------------

  /** Y (M x cols, wierszami) = A (M x K) X, kolumna c w x(c * K until (c + 1) * K). */
  def gemvRef(c : PositConfig, a : Seq[BigInt], x : Seq[BigInt], m : Int, k : Int, cols : Int = 1) : Seq[BigInt] =
    for (i <- 0 until m; cc <- 0 until cols) yield {
      val (q, nar) = QuireRef.accumulate(c, (0 until k).iterator.map(j => (a(i * k + j), Some(x(cc * k + j)))))
      QuireRef.round(c, q, nar)._1
    }

  for ((c, l, pc) <- Seq((p32, 1, 1), (p32, 2, 1), (p32, 1, 4), (p32, 2, 4), (p16, 4, 4), (p8, 8, 4))) {
    testpoint("mem_gemv", s"p${c.n}_L${l}_P$pc") {
      sim(gemvd(c, l, pc), s"mem_gemv_${c.n}_${l}_$pc") { e =>
        val cfg = e.rd(CONFIG)
        assert(((cfg >> CONFIG_HAS_GEMV) & 1) == 1 && ((cfg >> (16 + OP_XLOAD)) & 1) == 1 &&
               ((cfg >> (16 + OP_GEMV)) & 1) == 1, f"CONFIG 0x$cfg%x")
        assert(e.rd(GEMV_CFG) == ((pc.toLong << 16) | GEMV_MAXK), s"GEMV_CFG ${e.rd(GEMV_CFG)}")
        assert(e.rd(GEMV_COLS) == 1, "GEMV_COLS po resecie")
        for ((r, v) <- Seq(GEMV_K -> 17L, MEM_WLEN -> 1234L, GEMV_COLS -> pc.toLong)) {
          e.wr(r, v); assert(e.rd(r) == v, f"rejestr 0x$r%x")
        }
        val shapes = Seq((1, 1), (1, 5), (3, 1), (2, 2), (5, 3), (7, 17), (33, 10), (9, GEMV_MAXK), (40, 1)) ++
          Seq.fill(3)((1 + e.rnd.nextInt(30), 1 + e.rnd.nextInt(GEMV_MAXK)))
        for (((m, k), t) <- shapes.zipWithIndex) {
          val cols = if (pc == 1) 1 else Seq(pc, 1, pc - 1, 2)(t % 4) max 1   // tez mniej kolumn niz P
          val (a, x, y) = (e.addr(0, m * k), e.addr(1, k * cols), e.addr(2, m * cols))
          val as = stress(c, m * k, e.rnd).toSeq.map(_._1)
          val xs = stress(c, k * cols, e.rnd).toSeq.map(_._2)
          e.put(a, as); e.put(x, xs)
          val guard = Seq.fill(32)(BigInt(0x5a))
          val ny = m * cols
          mem(e).writeWords(y + e.bytes.toLong * ny, 1, guard)
          e.wr(CTRL, 1 << CTRL_CLEAR_COUNTERS)
          val s = e.gemv(m, k, a, x, y, cols)
          assert((s & 0x3c) == 0, f"gemv ${m}x$k x $cols: MEM_STATUS 0x$s%x")
          assertSeq(s"gemv L=$l P=$pc ${m}x$k x $cols", e.get(y, ny), gemvRef(c, as, xs, m, k, cols))
          assert(mem(e).readWords(y + e.bytes.toLong * ny, 1, 32) == guard, s"gemv ${m}x$k x $cols: zapis za y")
        }
        // po gemv zwykla redukcja
        val len = 50
        val xs = stress(c, len, e.rnd).toSeq
        e.put(BASE, xs.map(_._1)); e.put(BASE + 0x10000, xs.map(_._2))
        e.wr(CTRL, 1 << CTRL_QUIRE_CLEAR)
        e.run(OP_DOT, len, BASE, Some(BASE + 0x10000))
        e.wr(CTRL, 1 << CTRL_QUIRE_ROUND)
        var st = e.rd(STATUS); while ((st & (1 << STATUS_DONE)) == 0) st = e.rd(STATUS)
        val (q, nar) = QuireRef.accumulate(c, xs.iterator.map { case (u, v) => (u, Some(v)) })
        assert(BigInt(e.rd(RESULT)) == QuireRef.round(c, q, nar)._1, "dot po gemv")
      }
    }
  }

  testpoint("mem_gemv_reject") {
    sim(gemvd(p32, 1), "mem_gemv_reject") { e =>
      val (ar0, aw0) = (e.mem.arCount, e.mem.awCount)
      def shape(label : String, m : Long, k : Long, len : Long) : Unit = {
        e.wr(GEMV_K, k); e.wr(MEM_WLEN, m)
        val s = e.run(OP_GEMV, len, BASE, None, None, Some(BASE + 0x1000))
        assert(((s >> MEM_ST_ERR_SHAPE) & 1) == 1 && (s & 1) == 0, f"$label: MEM_STATUS 0x$s%x")
      }
      shape("len != M * K", 4, 8, 31)
      shape("K = 0", 4, 0, 0 + 1)
      shape("K > maxK", 1, GEMV_MAXK + 1, GEMV_MAXK + 1)
      def xload(label : String, k : Long, cols : Long, len : Long) : Unit = {
        e.wr(GEMV_K, k); e.wr(GEMV_COLS, cols)
        val s = e.run(OP_XLOAD, len, BASE)
        assert(((s >> MEM_ST_ERR_SHAPE) & 1) == 1 && (s & 1) == 0, f"XLOAD $label: MEM_STATUS 0x$s%x")
      }
      xload("> maxK", GEMV_MAXK + 1, 1, GEMV_MAXK + 1)
      xload("len != K * cols", 8, 1, 9)
      xload("cols = 0", 8, 0, 8)
      xload("cols > P", 8, 2, 16)                 // P = 1
      e.wr(GEMV_COLS, 2); e.wr(GEMV_K, 4)
      shape("GEMV cols > P", 2, 4, 8)
      e.wr(GEMV_COLS, 1)
      e.cd.waitSampling(50)
      assert(e.mem.arCount == ar0 && e.mem.awCount == aw0, "transakcje po odrzuceniu")
    }
  }

  for (pc <- Seq(1, 4)) testpoint("mem_gemv_throughput", s"P$pc") {
    sim(gemvd(p32, 1, pc), s"mem_gemv_throughput_P$pc") { e =>
      e.mem.stall = 0.0
      e.mem.maxDelay = 20
      val (m, k) = (64, GEMV_MAXK)
      val as = e.rand(m * k); val xs = e.rand(k * pc)
      e.put(BASE, as); e.put(BASE + 0x10000, xs)
      assert((e.gemv(m, k, BASE, BASE + 0x10000, BASE + 0x20000, pc) & 0x3c) == 0)
      assertSeq(s"gemv 64x64 x $pc", e.get(BASE + 0x20000, m * pc), gemvRef(e.c, as, xs, m, k, pc))
      val cyc = e.rd(MEM_CYCLES)
      assert(cyc <= 1.1 * m * k + 200, s"gemv ${m}x$k x $pc: $cyc taktow")
      info(f"gemv posit32 ${m}x$k, $pc kolumn: $cyc taktow (${m * k.toDouble * pc / cyc}%.3f MAC / takt)")
    }
  }

  // ---- 5.3: funkcje z tablicy -----------------------------------------------------------------

  for ((c, l, copies) <- Seq((p16, 1, 1), (p16, 4, 1), (p16, 4, 2), (p8, 8, 4), (p8, 8, 1), (p8, 2, 1))) {
    testpoint("mem_table", s"p${c.n}_L${l}_C$copies") {
      sim(tabled(c, l, copies), s"mem_table_${c.n}_${l}_$copies") { e =>
        val size = 1 << c.n
        val cfg = e.rd(CONFIG)
        assert(((cfg >> (16 + OP_TABLE)) & 1) == 1, f"CONFIG 0x$cfg%x")
        assert(e.rd(TABLE_CFG) == ((2L * copies) << 8 | c.n), f"TABLE_CFG 0x${e.rd(TABLE_CFG)}%x")
        val tabAddr = BASE + 0x80000
        def load(t : Seq[BigInt]) : Unit = {
          e.put(tabAddr, t)
          e.wr(TABLE_CTRL, 1 << TABLE_CTRL_LOAD)
          val s = e.run(OP_TABLE, size, tabAddr)
          assert((s & 0x3c) == 0, f"ladowanie tablicy: MEM_STATUS 0x$s%x")
          e.wr(TABLE_CTRL, 0)
        }
        // odrzucenie: zla dlugosc ladowania
        e.wr(TABLE_CTRL, 1 << TABLE_CTRL_LOAD)
        val (ar0, aw0) = (e.mem.arCount, e.mem.awCount)
        val bad = e.run(OP_TABLE, size - 1, tabAddr)
        assert(((bad >> MEM_ST_ERR_SHAPE) & 1) == 1 && e.mem.arCount == ar0 && e.mem.awCount == aw0, f"MEM_STATUS 0x$bad%x")
        e.wr(TABLE_CTRL, 0)

        for (round <- 0 until 2) {
          val t = e.rand(size)
          load(t)
          val lens = (1 to 2 * l + 1) ++ Seq(97, 1 + e.rnd.nextInt(3000))
          for (len <- lens) {
            val (a, b, dst) = (e.addr(0, len), e.addr(1, len), e.addr(3, len))
            val xs = e.rand(len)
            e.put(a, xs)
            val guard = Seq.fill(32)(BigInt(0x5a))
            mem(e).writeWords(dst + e.bytes.toLong * len, 1, guard)
            e.wr(CTRL, 1 << CTRL_CLEAR_COUNTERS)
            val s = e.run(OP_TABLE, len, a, None, None, Some(dst))
            assert((s & 0x3c) == 0, f"tablica len=$len: MEM_STATUS 0x$s%x")
            assertSeq(s"T[a] L=$l C=$copies len=$len (tablica $round)", e.get(dst, len), xs.map(x => t(x.toInt)))
            assert(mem(e).readWords(dst + e.bytes.toLong * len, 1, 32) == guard, s"len=$len: zapis za koncem")
            assert(e.rd(CNT_ELEMENTS) == len, s"len=$len: CNT_ELEMENTS ${e.rd(CNT_ELEMENTS)}")
            if (len == 97) {                                   // zwykla sciezka po tablicy
              val ys = e.rand(len)
              e.put(b, ys)
              assert((e.run(PositAluOp.code(PositOp.Add), len, a, Some(b), None, Some(dst)) & 0x3c) == 0)
              assertSeq(s"add po tablicy len=$len", e.get(dst, len), xs.zip(ys).map { case (x, y) => PositRef(c, PositOp.Add, x, y) })
            }
          }
        }
        e.mem.stall = 0.0
        e.mem.maxDelay = 20
        val len = 4096
        val xs = e.rand(len)
        e.put(BASE, xs)
        e.run(OP_TABLE, len, BASE, None, None, Some(BASE + 0x20000))
        val cyc = e.rd(MEM_CYCLES)
        val perGroup = math.max(1, (l + 2 * copies - 1) / (2 * copies))
        assert(cyc <= 1.1 * len / l * perGroup + 200, s"$len elementow, L=$l, C=$copies: $cyc taktow")
        info(f"tablica posit${c.n}, L=$l, C=$copies: $len elementow w $cyc taktach (${len.toDouble / cyc}%.2f / takt)")
      }
    }
  }

  testpoint("mem_table_absent") {
    sim(m16, "mem_table_absent") { e =>
      assert(e.rd(TABLE_CFG) == 0 && ((e.rd(CONFIG) >> (16 + OP_TABLE)) & 1) == 0)
    }
  }

  // ---- 5.4: FIR przez GEMV z oknem --------------------------------------------------------------

  /** Bufory x dla FIR: kolumna p = odczepy przesuniete o p (h[m - p], zera poza), K = T + cols - 1. */
  def firTaps(h : Seq[BigInt], cols : Int) : Seq[BigInt] = {
    val k = h.size + cols - 1
    for (p <- 0 until cols; m <- 0 until k) yield if (m - p >= 0 && m - p < h.size) h(m - p) else BigInt(0)
  }
  def firRef(c : PositConfig, x : Seq[BigInt], h : Seq[BigInt], n : Int) : Seq[BigInt] =
    (0 until n).map { i =>
      val (q, nar) = QuireRef.accumulate(c, h.indices.iterator.map(j => (x(i + j), Some(h(j)))))
      QuireRef.round(c, q, nar)._1
    }

  for ((c, l, pc) <- Seq((p32, 1, 1), (p32, 2, 4), (p16, 4, 4), (p8, 8, 4))) {
    testpoint("mem_fir", s"p${c.n}_L${l}_P$pc") {
      sim(gemvd(c, l, pc), s"mem_fir_${c.n}_${l}_$pc") { e =>
        assert(e.rd(GEMV_MODE) == 0, "GEMV_MODE po resecie")
        val shapes = Seq((1, 1, 1), (1, pc, 5), (3, pc, 7), (5, 2 min pc, 33), (17, pc, 10),
                         (GEMV_MAXK - pc + 1, pc, 6), (10, pc, 200), (40, 3 min pc, 20)) ++
          Seq.fill(2)((1 + e.rnd.nextInt(GEMV_MAXK - pc + 1), 1 + e.rnd.nextInt(pc), 1 + e.rnd.nextInt(60)))
        for ((t, cols, m) <- shapes) {
          val k = t + cols - 1
          val nx = (m - 1) * cols + k
          val (xa, ha, ya) = (e.addr(0, nx), e.addr(1, k * cols), e.addr(2, m * cols))
          val xs = stress(c, nx, e.rnd).toSeq.map(_._1)
          val hs = stress(c, t, e.rnd).toSeq.map(_._2)
          e.put(xa, xs); e.put(ha, firTaps(hs, cols))
          val guard = Seq.fill(32)(BigInt(0x5a))
          val ny = m * cols
          mem(e).writeWords(ya + e.bytes.toLong * ny, 1, guard)
          e.wr(GEMV_K, k); e.wr(GEMV_COLS, cols); e.wr(MEM_WLEN, m)
          assert((e.run(OP_XLOAD, k.toLong * cols, ha) & 0x3c) == 0)
          e.wr(GEMV_MODE, 1 << GEMV_MODE_WINDOW)
          val r0 = e.mem.rBeats
          val s = e.run(OP_GEMV, nx, xa, None, None, Some(ya))
          val rb = e.mem.rBeats - r0
          e.wr(GEMV_MODE, 0)
          assert((s & 0x3c) == 0, f"fir T=$t cols=$cols M=$m: MEM_STATUS 0x$s%x")
          assertSeq(s"fir L=$l P=$pc T=$t cols=$cols M=$m", e.get(ya, ny), firRef(c, xs, hs, ny))
          assert(mem(e).readWords(ya + e.bytes.toLong * ny, 1, 32) == guard, s"fir T=$t: zapis za y")
          val perBeat = 16 / e.bytes
          assert(rb <= (nx + perBeat - 1) / perBeat + 1, s"fir T=$t: $rb slow R na $nx elementow (sygnal czytany raz)")
        }
        // odrzucenia
        e.wr(GEMV_MODE, 1 << GEMV_MODE_WINDOW)
        def bad(label : String, k : Long, cols : Long, m : Long, len : Long) : Unit = {
          e.wr(GEMV_K, k); e.wr(GEMV_COLS, cols); e.wr(MEM_WLEN, m)
          val st = e.run(OP_GEMV, len, BASE, None, None, Some(BASE + 0x10000))
          assert(((st >> MEM_ST_ERR_SHAPE) & 1) == 1 && (st & 1) == 0, f"$label: MEM_STATUS 0x$st%x")
        }
        bad("len", 8, 1, 4, 12)                       // (4 - 1) * 1 + 8 = 11
        bad("M = 0", 8, 1, 0, 7)
        if (pc > 1) bad("cols > K", 1, 2, 4, 7)
        e.wr(GEMV_MODE, 0)
      }
    }
  }

  testpoint("mem_fir", "throughput_P4") {
    sim(gemvd(p32, 1, 4), "mem_fir_throughput") { e =>
      e.mem.stall = 0.0
      e.mem.maxDelay = 20
      val (t, cols, m) = (GEMV_MAXK - 3, 4, 64)
      val k = t + cols - 1
      val nx = (m - 1) * cols + k
      val xs = e.rand(nx); val hs = e.rand(t)
      e.put(BASE, xs); e.put(BASE + 0x10000, firTaps(hs, cols))
      e.wr(GEMV_K, k); e.wr(GEMV_COLS, cols); e.wr(MEM_WLEN, m)
      e.run(OP_XLOAD, k.toLong * cols, BASE + 0x10000)
      e.wr(GEMV_MODE, 1 << GEMV_MODE_WINDOW)
      e.run(OP_GEMV, nx, BASE, None, None, Some(BASE + 0x20000))
      e.wr(GEMV_MODE, 0)
      assertSeq("fir 61 odczepow", e.get(BASE + 0x20000, m * cols), firRef(e.c, xs, hs, m * cols))
      val cyc = e.rd(MEM_CYCLES)
      assert(cyc <= 1.1 * m * k + 200, s"fir: $cyc taktow")
      info(f"FIR posit32, $t odczepow, $cols wyjsc na wiersz: ${m * cols} wyjsc w $cyc taktach " +
           f"(${m * cols * t.toDouble / cyc}%.2f MAC / takt, ${m * cols.toDouble / cyc}%.3f wyjscia / takt)")
    }
  }
}
