package posit.kv260

import spinal.core._
import posit.Conventions

// =====================================================================
//  posit.kv260 - wszystko, co wie o KV260 i Vivado: AXI4-Lite (etap 1),
//  AXI4-Stream + DMA (etap 2), wlasny master AXI4 (etap 6), opakowania
//  IP i generatory RTL. Silnik wektorowy (posit.vector) nie zna magistral.
// =====================================================================
object Kv260 {
  /** SOM K26 (Zynq UltraScale+), oznaczenie dla syntezy OOC i projektu Vivado. */
  val part = "xck26-sfvc784-2LV-c"

  /** Zegar PL na start (pl_clk0); podnosimy po raporcie syntezy z etapu 0. */
  val plClock = 100 MHz

  def spinal = Conventions.spinal.copy(defaultClockDomainFrequency = FixedFrequency(plClock))
}
