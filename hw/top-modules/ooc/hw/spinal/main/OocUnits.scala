package posit.top.ooc

import spinal.core._
import posit._
import posit.vector.PositAluConfig
import posit.kv260.{Kv260, MemMoverConfig, PositAluAxiLite, PositGen, PositVecAxi}
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
//    hw/gen/alu_p16/, alu_p32/  PositAluAxiLite z etapu 1 (k = 4, potok full),
//                   ta sama konfiguracja co top-modules/alu
//    hw/gen/vec_p16/, vec_p32/  PositVecAxi (rejestry, AXI4-Stream, quire, operacje etapu 5),
//                   ta sama konfiguracja co top-modules/vec (VecKv260.config)
//    hw/gen/vec_p32_u4/  to samo z 4 torami div / sqrt / FMA (slowUnits = 4): koszt torow
//    hw/gen/vec_p32_mem/ etap 6: PositVecAxi 5.0 z masterem AXI4 (m_axi 128 b, FMA3), bez AXI4-Stream
//    hw/gen/vec_p32_mem_L2/ to samo z 2 torami elementowymi (2 elementy / takt)
//    hw/gen/vec_p32_mem_L2_gemv/ etap 7a: L2 z gemv (bufor x 4096 elementow, drugi quire)
//    hw/gen/vec_p32_mem_L2_gemv4/ 5.2: L2 z gemv na 4 kolumny (4 bufory x, 8 quire)
//    hw/gen/vec_p16_mem_L4_table/ 5.3: posit16 x 4 z tablica funkcji w 2 kopiach (bez gemv): koszt tablicy
//
//  sbt "top-ooc/runMain posit.top.ooc.OocUnits"
//  Synteza: vivado/ooc_synth.tcl (opis w README.md).
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
    for (c <- formats)
      Kv260.spinal.copy(targetDirectory = s"hw/gen/alu_p${c.n}")
        .generateVerilog(new PositAluAxiLite(PositAluConfig(c, PositOp.all, divBitsPerCycle = 4, pipe = PositPipeline.full)))
    for (c <- formats)
      Kv260.spinal.copy(targetDirectory = s"hw/gen/vec_p${c.n}")
        .generateVerilog(new PositVecAxi(PositAluConfig(c, PositOp.all, divBitsPerCycle = 4, pipe = PositPipeline.full,
                                                        sqrt = true, fma = true, convert = c.n == 32 && c.es == 2)))
    Kv260.spinal.copy(targetDirectory = "hw/gen/vec_p32_u4")
      .generateVerilog(new PositVecAxi(PositAluConfig(PositConfig(32), PositOp.all, divBitsPerCycle = 4, pipe = PositPipeline.full,
                                                      sqrt = true, fma = true, convert = true, slowUnits = 4)))
    Kv260.spinal.copy(targetDirectory = "hw/gen/vec_p32_mem")
      .generateVerilog(new PositVecAxi(PositAluConfig(PositConfig(32), PositOp.all, divBitsPerCycle = 4, pipe = PositPipeline.full,
                                                      sqrt = true, fma = true, convert = true, fma3 = true), withMaster = true))
    Kv260.spinal.copy(targetDirectory = "hw/gen/vec_p32_mem_L2")
      .generateVerilog(new PositVecAxi(PositAluConfig(PositConfig(32), PositOp.all, divBitsPerCycle = 4, pipe = PositPipeline.full,
                                                      sqrt = true, fma = true, convert = true, fma3 = true), withMaster = true,
                                       memCfg = Some(MemMoverConfig(32, lanes = 2))))
    Kv260.spinal.copy(targetDirectory = "hw/gen/vec_p32_mem_L2_gemv")
      .generateVerilog(new PositVecAxi(PositAluConfig(PositConfig(32), PositOp.all, divBitsPerCycle = 4, pipe = PositPipeline.full,
                                                      sqrt = true, fma = true, convert = true, fma3 = true), withMaster = true,
                                       memCfg = Some(MemMoverConfig(32, lanes = 2)), withGemv = true, gemvMaxK = 4096))
    Kv260.spinal.copy(targetDirectory = "hw/gen/vec_p32_mem_L2_gemv4")
      .generateVerilog(new PositVecAxi(PositAluConfig(PositConfig(32), PositOp.all, divBitsPerCycle = 4, pipe = PositPipeline.full,
                                                      sqrt = true, fma = true, convert = true, fma3 = true), withMaster = true,
                                       memCfg = Some(MemMoverConfig(32, lanes = 2)), withGemv = true, gemvMaxK = 4096,
                                       gemvCols = 4))
    Kv260.spinal.copy(targetDirectory = "hw/gen/vec_p16_mem_L4_table")
      .generateVerilog(new PositVecAxi(PositAluConfig(PositConfig(16), PositOp.all, divBitsPerCycle = 4, pipe = PositPipeline.full,
                                                      sqrt = true, fma = true, fma3 = true), withMaster = true,
                                       memCfg = Some(MemMoverConfig(16, lanes = 4)), tableCopies = 2))
  }
}
