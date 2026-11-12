// Wspolne ustawienia buildu (HwBuild) z submodule'a lib/ - ten sam plik,
// ktorego uzywa samodzielny build biblioteki.
Compile / unmanagedSourceDirectories += baseDirectory.value.getParentFile / "lib" / "sbt"
