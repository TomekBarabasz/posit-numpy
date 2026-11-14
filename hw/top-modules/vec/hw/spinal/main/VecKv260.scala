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
//    silnik: n[:es[:k[:u]]], n = 8, 16 albo 32 (tdata w pelnych bajtach), jeden silnik na n;
//    k: bity dzielnika i pierwiastka na takt (4), u: tory wielotaktowe (slowUnits, 1)
//
//  Etap 6: domyslnie silnik z wlasnym masterem AXI4 (m_axi, irq, FMA3); --dma: stara sciezka
//  AXI4-Stream + AXI DMA (s_axis / m_axis), np. VecKv260 --dma 32 16.
//
//  Wynik: hw/gen/p<n>/PositVecAxi_p<n>.v dla kazdego silnika; dalej vivado/build.tcl -engines "32 16 8"
//  (z --dma: build.tcl -io dma).
//  Nazwy modulow maja przedrostek p<n>_ (globalPrefix), wiec silniki roznych formatow
//  nie zderzaja sie nazwami w jednym projekcie Vivado.
// =====================================================================
object VecKv260 {
  var master = true      // etap 6: master AXI4; --dma przelacza na AXI4-Stream + AXI DMA

  def config(n : Int = 32, es : Int = 2, k : Int = 4, units : Int = 1) : PositAluConfig =
    PositAluConfig(PositConfig(n, es), PositOp.all, divBitsPerCycle = k, pipe = PositPipeline.full,
                   sqrt = true, fma = true, convert = n == 32 && es == 2, slowUnits = units, fma3 = master)

  /** "n[:es[:k[:u]]]" -> konfiguracja silnika. */
  def parse(spec : String) : PositAluConfig = spec.split(":").toSeq.map(_.trim.toInt) match {
    case Seq(n, es, k, u) => config(n, es, k, u)
    case Seq(n, es, k) => config(n, es, k)
    case Seq(n, es)    => config(n, es)
    case Seq(n)        => config(n)
    case _             => sys.error(s"silnik '$spec': n[:es[:k[:u]]]")
  }

  def topName(n : Int) : String = s"PositVecAxi_p$n"

  def generate(cfg : PositAluConfig) : String = {
    val n   = cfg.c.n
    require(Seq(8, 16, 32).contains(n), s"n = $n: AXI DMA chce tdata w pelnych bajtach (8, 16, 32)")
    val rep = Kv260.spinal.copy(targetDirectory = s"hw/gen/p$n", globalPrefix = s"p${n}_")
      .generateVerilog(new PositVecAxi(cfg, withMaster = master).setDefinitionName(topName(n)))
    val ports = if (master) "m_axi 128 b, irq" else s"s_axis ${2 * n} b, m_axis $n b"
    println(s"${rep.toplevelName}: $cfg, CONFIG = 0x${PositVecRegs.config(cfg, master).toHexString}, " +
            s"$ports -> hw/gen/p$n/${rep.toplevelName}.v")
    rep.toplevelName
  }

  def main(args : Array[String]) : Unit = {
    master = !args.contains("--dma")
    val specs = args.toSeq.filter(_ != "--dma")
    val cfgs = (if (specs.isEmpty) Seq("32") else specs).map(parse)
    val dup  = cfgs.groupBy(_.c.n).collect { case (n, s) if s.size > 1 => n }
    require(dup.isEmpty, s"jeden silnik na format: n = ${dup.mkString(", ")} powtorzone")
    cfgs.foreach(generate)
    println(s"vivado/build.tcl -engines \"${cfgs.map(_.c.n).mkString(" ")}\"" + (if (master) "" else " -io dma"))
  }
}
