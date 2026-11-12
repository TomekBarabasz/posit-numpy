package posit.kv260

import spinal.core._
import posit._

/** Generator jednostek posit-core do RTL (Verilog + VHDL) z konwencjami KV260.
  * Top-levele (top-modules/) wolaja go ze swoja lista jednostek; samodzielnie:
  *   sbt "posit-kv260/runMain posit.kv260.PositGen"            posit<8,2>, <16,2>, <32,2> x add/sub/mul/div + dzielniki
  *   sbt "posit-kv260/runMain posit.kv260.PositGen 24 2 mul"   pojedyncza jednostka
  * Wynik: <katalog projektu sbt>/hw/gen.
  */
object PositGen {
  sealed trait Job { def component: Component }
  case class UnitJob(c: PositConfig, op: PositOp, pipe: PositPipeline = PositPipeline()) extends Job {
    def component = new PositUnit(c, op, pipe)
  }
  case class DividerJob(c: PositConfig, arch: DivArch) extends Job {
    def component = new PositDivider(c, arch)
  }

  /** Domyslny zestaw z posit-spinal: wszystkie operacje w trzech formatach + dzielniki sekwencyjne. */
  def defaultJobs: Seq[Job] =
    (for (n <- Seq(8, 16, 32); op <- PositOp.all) yield UnitJob(PositConfig(n), op)) ++
    (for (n <- Seq(16, 32); arch <- Seq(DivArch.Iterative(2), DivArch.Pipelined(4))) yield DividerJob(PositConfig(n), arch))

  def generate(jobs: Seq[Job], spinal: SpinalConfig = Kv260.spinal, vhdl: Boolean = true): Unit =
    for (j <- jobs) {
      spinal.generateVerilog(j.component)
      if (vhdl) spinal.generateVhdl(j.component)
    }

  def main(args: Array[String]): Unit = {
    val jobs = args match {
      case Array(n, es, op) => Seq(UnitJob(PositConfig(n.toInt, es.toInt), PositOp.all.find(_.name == op).get))
      case _                => defaultJobs
    }
    generate(jobs)
  }
}
