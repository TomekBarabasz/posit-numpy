package posit.top.vec

import spinal.core._
import posit._
import posit.vector._
import posit.kv260._

// =====================================================================
//  PositVecAxi (rejestry + AXI4-Stream) jako IP do block designu z AXI DMA
//  na KV260 - jeden albo kilka silnikow (formatow) w jednym bitstreamie.
//
//  sbt "top-vec/runMain posit.top.vec.VecKv260"              posit<32,2>, k = 4
//  sbt "top-vec/runMain posit.top.vec.VecKv260 32 16 8"      trzy silniki
//  sbt "top-vec/runMain posit.top.vec.VecKv260 16:2:1"       posit<16,2>, k = 1
//  sbt "top-vec/runMain posit.top.vec.VecKv260 32:2:4:4"     posit<32,2>, k = 4, 4 tory div / sqrt / FMA
//  sbt "top-vec/runMain posit.top.vec.VecKv260 32:2:4:4:2 16:2:4:2:4 8:2:4:1:8"   szerszy silnik
//    silnik: n[:es[:k[:u[:L]]]], n = 8, 16 albo 32 (tdata w pelnych bajtach), jeden silnik na n;
//    k: bity dzielnika i pierwiastka na takt (4), u: tory wielotaktowe w kazdym torze (slowUnits, 1),
//    L: tory elementowe - elementy na takt (1; tylko z masterem; HP 128 b: sens do 64 / n)
//
//  Etap 6: domyslnie silnik z wlasnym masterem AXI4 (m_axi, irq, FMA3); --dma: stara sciezka
//  AXI4-Stream + AXI DMA (s_axis / m_axis), np. VecKv260 --dma 32 16.
//  Etap 7a: z masterem tez gemv (OP_XLOAD / OP_GEMV, bufor x na --gemv-k elementow, domyslnie 4096;
//  --gemv-k 0: bez gemv - bez drugiego quire i bufora). Wersja 5.2: --gemv-cols P (domyslnie 4) kolumn B naraz,
//  kazda z wlasna para quire i buforem x - P mnozen z dodawaniem na takt (koszt: 2 P quire).
//  Wersja 5.3: silniki posit8 / posit16 z masterem maja tablice funkcji (OP_TABLE, exp / log / sin ...
//  z positnp.funcs) w --table-copies C kopiach dwuportowej BRAM (domyslnie max(1, L / 2): 2 C = L odczytow
//  na takt; posit16: 32 BRAM36 na kopie); --table-copies 0: bez tablicy.
//  --gemv-cols P (wszystkie silniki) albo --gemv-cols 32=4,16=2,8=0 (na format; 0: bez gemv).
//  Wersja 5.5, opcjonalnie: --fft S[:n,n] - sprzetowe FFT radix-4 (PositFftCore, N = 4^2 .. 4^S) w silnikach
//  posit16 / posit32 z L <= 4, np. --fft 6:32 tylko w posit32 (domyslnie wylaczone; posit32: ok. 30 tys. LUT, 48 DSP -
//  w obu silnikach naraz z gemv x 4 bitstream zbliza sie do 117 tys. LUT xck26).
//
//  Wynik: hw/gen/p<n>/PositVecAxi_p<n>.v dla kazdego silnika; dalej vivado/build.tcl -engines "32 16 8"
//  (z --dma: build.tcl -io dma).
//  Nazwy modulow maja przedrostek p<n>_ (globalPrefix), wiec silniki roznych formatow
//  nie zderzaja sie nazwami w jednym projekcie Vivado.
// =====================================================================
object VecKv260 {
  var master = true      // etap 6: master AXI4; --dma przelacza na AXI4-Stream + AXI DMA
  var gemvK  = 4096      // etap 7a: pojemnosc bufora x (0: bez gemv)
  var gemvP  = 4         // 5.2: kolumny naraz
  val gemvPn = scala.collection.mutable.Map[Int, Int]()   // --gemv-cols 32=4,16=2,8=0: kolumny na format (0: bez gemv)
  var tableC = -1        // 5.3: kopie tablicy (-1: max(1, L / 2) dla n <= 16)
  var fftS   = 0         // 5.5: rdzen FFT do N = 4^fftS (0: bez)
  var fftNs  = Set(16, 32)   // formaty z rdzeniem FFT (--fft S:32)

  val lanes = scala.collection.mutable.Map[Int, Int]()     // n -> L z argumentow

  def config(n : Int = 32, es : Int = 2, k : Int = 4, units : Int = 1) : PositAluConfig =
    PositAluConfig(PositConfig(n, es), PositOp.all, divBitsPerCycle = k, pipe = PositPipeline.full,
                   sqrt = true, fma = true, convert = n == 32 && es == 2, slowUnits = units, fma3 = master)

  /** "n[:es[:k[:u]]]" -> konfiguracja silnika. */
  def parse(spec : String) : PositAluConfig = spec.split(":").toSeq.map(_.trim.toInt) match {
    case Seq(n, es, k, u, l) => lanes(n) = l; config(n, es, k, u)
    case Seq(n, es, k, u) => config(n, es, k, u)
    case Seq(n, es, k) => config(n, es, k)
    case Seq(n, es)    => config(n, es)
    case Seq(n)        => config(n)
    case _             => sys.error(s"silnik '$spec': n[:es[:k[:u]]]")
  }

  def topName(n : Int) : String = s"PositVecAxi_p$n"

  /** Kolumny gemv silnika posit<n> (0: bez gemv): --gemv-cols P albo 32=4,16=2,8=0; --gemv-k 0 wylacza wszystkie. */
  def gemvOf(n : Int) : Int = if (!master || gemvK <= 0) 0 else gemvPn.getOrElse(n, gemvP)

  /** Rdzen FFT silnika posit<n>: z masterem, n >= 16, L <= 4. */
  def fftOf(n : Int) : Int = if (master && n >= 16 && fftNs.contains(n) && lanes.getOrElse(n, 1) <= 4) fftS else 0

  /** Kopie tablicy funkcji silnika posit<n>: tylko z masterem i n <= 16. */
  def tableCopies(n : Int) : Int = {
    val l = lanes.getOrElse(n, 1)
    if (!master || n > 16) 0 else if (tableC >= 0) tableC min ((l + 1) / 2) else ((l / 2) max 1)
  }

  def generate(cfg : PositAluConfig) : String = {
    val n   = cfg.c.n
    require(Seq(8, 16, 32).contains(n), s"n = $n: AXI DMA chce tdata w pelnych bajtach (8, 16, 32)")
    val rep = Kv260.spinal.copy(targetDirectory = s"hw/gen/p$n", globalPrefix = s"p${n}_")
      .generateVerilog(new PositVecAxi(cfg, withMaster = master,
        memCfg = Some(MemMoverConfig(n, lanes = lanes.getOrElse(n, 1))),
        withGemv = gemvOf(n) > 0, gemvMaxK = gemvK max 2, gemvCols = gemvOf(n) max 1,
        tableCopies = tableCopies(n), fftMaxS = fftOf(n)).setDefinitionName(topName(n)))
    require(master || lanes.getOrElse(n, 1) == 1, "L > 1 tylko z masterem AXI4 (bez --dma)")
    val gemv  = gemvOf(n) > 0
    val ports = (if (master) "m_axi 128 b, irq" else s"s_axis ${2 * n} b, m_axis $n b") + s", L=${lanes.getOrElse(n, 1)}" +
                (if (gemv) s", gemv K <= $gemvK x ${gemvOf(n)} kolumn" else "") +
                (if (tableCopies(n) > 0) s", tablica 2^$n x ${tableCopies(n)}" else "") +
                (if (fftOf(n) > 0) s", FFT do N = ${1 << (2 * fftOf(n))}" else "")
    println(s"${rep.toplevelName}: $cfg, CONFIG = 0x${PositVecRegs.config(cfg, master, gemv, tableCopies(n) > 0).toHexString}, " +
            s"$ports -> hw/gen/p$n/${rep.toplevelName}.v")
    rep.toplevelName
  }

  def main(args : Array[String]) : Unit = {
    master = !args.contains("--dma")
    val opts = Seq("--gemv-k", "--gemv-cols", "--table-copies", "--fft")
    val vals = opts.map(o => args.indexOf(o)).filter(_ >= 0).map(_ + 1).toSet
    for ((o, i) <- opts.map(o => (o, args.indexOf(o))) if i >= 0 && o == "--gemv-cols" && args(i + 1).contains("=")) {
      for (kv <- args(i + 1).split(",")) { val Array(k, v) = kv.split("="); gemvPn(k.trim.toInt) = v.trim.toInt }
    }
    for ((o, i) <- opts.map(o => (o, args.indexOf(o))) if i >= 0 && !(o == "--gemv-cols" && args(i + 1).contains("="))) {
      val raw = args(i + 1).split(":")
      if (o == "--fft" && raw.size > 1) fftNs = raw(1).split(",").map(_.trim.toInt).toSet
      val v = raw(0).toInt
      o match { case "--gemv-k" => gemvK = v; case "--gemv-cols" => gemvP = v; case "--fft" => fftS = v; case _ => tableC = v }
    }
    val specs = args.toSeq.zipWithIndex.collect { case (a, i) if a != "--dma" && !opts.contains(a) && !vals.contains(i) => a }
    val cfgs = (if (specs.isEmpty) Seq("32") else specs).map(parse)
    val dup  = cfgs.groupBy(_.c.n).collect { case (n, s) if s.size > 1 => n }
    require(dup.isEmpty, s"jeden silnik na format: n = ${dup.mkString(", ")} powtorzone")
    cfgs.foreach(generate)
    println(s"vivado/build.tcl -engines \"${cfgs.map(_.c.n).mkString(" ")}\"" + (if (master) "" else " -io dma"))
  }
}
