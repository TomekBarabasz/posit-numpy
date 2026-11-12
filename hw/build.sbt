// hw/build.sbt - posit-numpy: arytmetyka posit, silnik wektorowy i IP dla KV260.
// Biblioteka testowa: submodule lib/ (github.com/TomekBarabasz/fpga-hw-lib),
// wciagana ze zrodel jako osobny build sbt (ProjectRef) - edycja w lib/ jest
// widoczna od razu, bez publishLocal. Z lib bierzemy tylko vertebre.
// Wspolne ustawienia (layout, fork, przelaczniki symulacji): lib/sbt/HwBuild.scala,
// dolaczone do meta-buildu przez project/build.sbt.
//
// Hierarchia (podprojekt -> pakiet):
//   posit-core    -> posit         format, dekoder/enkoder, add/sub/mul/div, dzielniki
//   posit-vector  -> posit.vector  silnik wektorowy (ALU, strumienie, quire)
//   posit-kv260   -> posit.kv260   AXI4-Lite/Stream/AXI4, opakowania IP, generatory
//   top-modules/* -> posit.top.*   top-levele do Vivado (odpowiednik demo/ w voice-changer)

import HwBuild._

inThisBuild(buildSettings)

// ---- biblioteki (lib/build.sbt) ------------------------------------------------

def libRef(id: String) = ProjectRef(file("lib"), id)

// Infrastruktura weryfikacji (TestplanSuite, SimEnv, StreamConformance).
// Kod syntezowalny jej nie widzi: zaleznosc tylko test->compile.
lazy val vertebra = libRef("vertebra")

// ---- bloki ---------------------------------------------------------------------

// Uklad <blok>/hw/spinal/{main,test}; test i run forkowane z katalogu bloku,
// wiec Conventions.spinal.targetDirectory = "hw/gen" laduje w <blok>/hw/gen.
def block(id: String) = Project(id, file(id))
  .settings(hwLayout, forkSettings, libraryDependencies ++= spinal, publish / skip := true)
  .dependsOn(vertebra % "test->compile")

lazy val positCore   = block("posit-core")
lazy val positVector = block("posit-vector").dependsOn(positCore % "compile->compile;test->test")   // PositRef w testach
lazy val positKv260  = block("posit-kv260").dependsOn(positCore % "compile->compile;test->test", positVector)

// ---- top-modules ---------------------------------------------------------------

// Top-level nie jest publikowany - to koncowy produkt (bitstream), nie biblioteka.
// Projekt sbt: top-<id>, katalog top-modules/<id>, pakiet posit.top.<id>.
// Nowy top: jedna linijka ponizej + dopisanie do aggregate w root.
def topModule(id: String) = Project(s"top-$id", file(s"top-modules/$id"))
  .settings(hwLayout, forkSettings, libraryDependencies ++= spinal, publish / skip := true)
  .dependsOn(positKv260 % "compile->compile;test->test", vertebra % "test->compile")

lazy val topOoc = topModule("ooc")
lazy val topAlu = topModule("alu")

// sbt test z hw/ odpala testy wszystkich blokow i top-leveli. Testy lib/ (voice-changer)
// celowo poza aggregate; w razie potrzeby: sbt "{lib}vertebra/test".
lazy val root = (project in file("."))
  .aggregate(positCore, positVector, positKv260, topOoc, topAlu)
  .settings(publish / skip := true)
