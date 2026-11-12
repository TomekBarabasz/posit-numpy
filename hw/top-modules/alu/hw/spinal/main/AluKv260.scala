package posit.top.alu

import spinal.core._
import posit._
import posit.vector._
import posit.kv260._

// =====================================================================
//  Etap 1: PositAluAxiLite jako IP do block designu na KV260.
//
//  sbt "top-alu/runMain posit.top.alu.AluKv260"            posit<32,2>, k = 4
//  sbt "top-alu/runMain posit.top.alu.AluKv260 8 2 1"      posit<8,2>, k = 1 (bitstream do testu wyczerpujacego)
//    argumenty: n es k  (k = bity ilorazu na takt)
//
//  Wynik: hw/gen/p<n>/PositAluAxiLite.v; dalej vivado/build.tcl -n <n>.
//  Potok: PositPipeline.full - przy MMIO latencja jest bez znaczenia,
//  a rejestry miedzy dekoderem, operacja i enkoderem odciazaja 100 MHz.
// =====================================================================
object AluKv260 {
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
    val rep = Kv260.spinal.copy(targetDirectory = s"hw/gen/p${cfg.c.n}").generateVerilog(new PositAluAxiLite(cfg))
    println(s"${rep.toplevelName}: $cfg, CONFIG = 0x${PositAluRegs.config(cfg).toHexString}, " +
            s"dzielenie ${cfg.divCycles} taktow -> hw/gen/p${cfg.c.n}/${rep.toplevelName}.v")
  }
}
