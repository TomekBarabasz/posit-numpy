package posit.top.ooc

import spinal.core._
import posit._
import posit.kv260.{Kv260, PositGen}
import posit.kv260.PositGen.{DividerJob, UnitJob}

// =====================================================================
//  Etap 0: jednostki posit-core do syntezy poza kontekstem (OOC) na
//  xck26 - tabela LUT / FF / DSP / Fmax dla kazdego ustawienia potoku.
//
//  Kazdy wariant potoku w osobnym katalogu, bo nazwa modulu
//  (PositAdd_16_2) nie zawiera potoku:
//    hw/gen/comb/   PositPipeline.combinational
//    hw/gen/op/     PositPipeline()  (rejestr po operacji, domyslny)
//    hw/gen/full/   PositPipeline.full
//    hw/gen/div/    dzielniki sekwencyjne (Iterative, Pipelined)
//
//  sbt "top-ooc/runMain posit.top.ooc.OocUnits"
//  Skrypt Tcl syntezy: vivado/ (etap 0, do napisania).
// =====================================================================
object OocUnits {
  val formats   = Seq(PositConfig(16), PositConfig(32))
  val pipelines = Seq("comb" -> PositPipeline.combinational, "op" -> PositPipeline(), "full" -> PositPipeline.full)
  val dividers  = Seq(DivArch.Iterative(1), DivArch.Iterative(4), DivArch.Pipelined(4))

  def main(args: Array[String]): Unit = {
    for ((tag, pipe) <- pipelines)
      PositGen.generate(for (c <- formats; op <- PositOp.all) yield UnitJob(c, op, pipe),
                        Kv260.spinal.copy(targetDirectory = s"hw/gen/$tag"), vhdl = false)
    PositGen.generate(for (c <- formats; a <- dividers) yield DividerJob(c, a),
                      Kv260.spinal.copy(targetDirectory = "hw/gen/div"), vhdl = false)
  }
}
