package posit

import spinal.core._

// =====================================================================
//  Konwencje projektu dla generacji i symulacji - jedno miejsce.
//  Uzywaja go testy wszystkich blokow (przez SimEnv) i top-levele.
//  Symulacja i synteza musza widziec TEN SAM rodzaj resetu, inaczej test
//  sprawdza inne zachowanie niz to, ktore idzie na plytke.
//
//  KV260 / Vivado: reset synchroniczny, aktywny stanem niskim (aresetn),
//  PL na start 100 MHz (plan, etap 0). posit-spinal mial domyslny
//  SpinalConfig: reset asynchroniczny aktywny wysokim.
//
//  targetDirectory jest wzgledny: run i test sa forkowane z katalogu
//  bloku (build.sbt), wiec "hw/gen" = <blok>/hw/gen.
//
//  Top z innymi potrzebami bierze kopie:
//    Conventions.spinal.copy(defaultClockDomainFrequency = FixedFrequency(150 MHz))
// =====================================================================
object Conventions {
  def spinal = SpinalConfig(
    targetDirectory                = "hw/gen",
    defaultConfigForClockDomains   = ClockDomainConfig(resetKind = SYNC, resetActiveLevel = LOW),
    defaultClockDomainFrequency    = FixedFrequency(100 MHz),
    onlyStdLogicVectorAtTopLevelIo = false,
    inlineRom                      = true
  )
}
