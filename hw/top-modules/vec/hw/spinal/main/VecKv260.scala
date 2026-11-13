package posit.top.vec

import spinal.core._
import posit._
import posit.vector._
import posit.kv260._

// =====================================================================
//  Etap 2: PositVecAxi (rejestry + AXI4-Stream) jako IP do block designu
//  z AXI DMA na KV260.
//
//  sbt "top-vec/runMain posit.top.vec.VecKv260"            posit<32,2>, k = 4
//  sbt "top-vec/runMain posit.top.vec.VecKv260 8 2 1"      posit<8,2>, k = 1
//    argumenty: n es k  (n: 8, 16 albo 32 - tdata w pelnych bajtach)
//
//  Wynik: hw/gen/p<n>/PositVecAxi.v; dalej vivado/build.tcl -n <n>.
// =====================================================================
object VecKv260 {
  def config(n : Int = 32, es : Int = 2, k : Int = 4) : PositAluConfig =
    PositAluConfig(PositConfig(n, es), PositOp.all, divBitsPerCycle = k, pipe = PositPipeline.full)

  def main(args : Array[String]) : Unit = {
    val cfg = args.map(_.toInt) match {
      case Array(n, es, k) => config(n, es, k)
      case Array(n, es)    => config(n, es)
      case Array(n)        => config(n)
      case Array()         => config()
      case _               => sys.error("argumenty: [n [es [k]]]")
    }
    val rep = Kv260.spinal.copy(targetDirectory = s"hw/gen/p${cfg.c.n}").generateVerilog(new PositVecAxi(cfg))
    println(s"${rep.toplevelName}: $cfg, CONFIG = 0x${PositVecRegs.config(cfg).toHexString}, " +
            s"s_axis ${2 * cfg.c.n} b, m_axis ${cfg.c.n} b -> hw/gen/p${cfg.c.n}/${rep.toplevelName}.v")
  }
}
