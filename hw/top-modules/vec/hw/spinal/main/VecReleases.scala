package posit.top.vec

import java.io.{File, PrintWriter}

// =====================================================================
//  Bitstreamy do pakietow (extras positnp): jedna konfiguracja na zastosowanie.
//  Jedno zrodlo prawdy - RTL wszystkich konfiguracji naraz, kazda w swoim katalogu:
//
//  sbt "top-vec/runMain posit.top.vec.VecReleases"                      wszystkie
//  sbt "top-vec/runMain posit.top.vec.VecReleases posit16_8_ml ..."     wybrane
//  sbt "top-vec/runMain posit.top.vec.VecReleases --list"               lista
//
//  Wynik: hw/gen/release/<nazwa>/p<n>/PositVecAxi_p<n>.v + release.txt (silniki, bufor, extra);
//  dalej z vivado/: vivado -mode batch -source release_builds.tcl - buduje wszystkie po kolei.
// =====================================================================
object VecReleases {
  case class Release(name : String, extra : String, engines : Seq[Int], bufMb : Int, args : String, status : String)

  val base = "32:2:4:4:2 16:2:4:2:4 8:2:4:1:8"
  val all = Seq(
    Release("posit32_16_8_mem",  "kv260",        Seq(32, 16, 8), 64, base,
            "sprawdzona (5.4); przebudowa na 5.6"),
    Release("posit32_16_8_dsp",  "kv260-dsp",    Seq(32, 16, 8), 64, s"--fft 6:32 --gemv-cols 32=2,16=2,8=0 $base",
            "sprawdzona (5.6)"),
    Release("posit16_8_ml",      "kv260-ml",     Seq(16, 8),     96, "--gemv-cols 16=8,8=8 16:2:4:2:4 8:2:4:1:8",
            "nowa"),
    Release("posit32_16_linalg", "kv260-linalg", Seq(32, 16),    96, "--gemv-cols 32=8,16=4 --table-copies 0 32:2:4:4:2 16:2:4:2:4",
            "nowa"),
    Release("posit32_16_dsp",    "kv260-dsp v2", Seq(32, 16),    96, "--fft 6 --gemv-cols 2 32:2:4:4:2 16:2:4:2:4",
            "eksperyment: rdzen FFT tez w posit16"))

  val root = "hw/gen/release"

  def delete(f : File) : Unit = {
    if (f.isDirectory) Option(f.listFiles).foreach(_.foreach(delete))
    f.delete()
  }

  def main(args : Array[String]) : Unit = {
    if (args.contains("--list")) {
      all.foreach(r => println(f"${r.name}%-20s ${r.extra}%-14s ${r.engines.mkString(",")}%-9s ${r.bufMb}%4d MB  ${r.status}"))
      return
    }
    val pick = if (args.isEmpty) all else args.toSeq.map(n => all.find(_.name == n).getOrElse(
      sys.error(s"nieznana konfiguracja $n (sa: ${all.map(_.name).mkString(", ")})")))
    for (r <- pick) {
      val dir = new File(s"$root/${r.name}")
      delete(dir)
      println(s"== ${r.name} (${r.extra}): VecKv260 ${r.args}")
      VecKv260.main(r.args.split(" ").filter(_.nonEmpty) ++ Array("--gen-dir", dir.getPath))
      val got = r.engines.filterNot(n => new File(s"$dir/p$n/PositVecAxi_p$n.v").exists)
      require(got.isEmpty, s"${r.name}: brak RTL silnikow ${got.mkString(", ")}")
      val w = new PrintWriter(new File(dir, "release.txt"))
      w.println(s"name ${r.name}")
      w.println(s"extra ${r.extra}")
      w.println(s"engines ${r.engines.mkString(",")}")
      w.println(s"buf_mb ${r.bufMb}")
      w.println(s"args ${r.args}")
      w.println(s"status ${r.status}")
      w.close()
    }
    println(s"\nRTL: ${pick.map(_.name).mkString(", ")} -> $root/; dalej z vivado/: " +
            "vivado -mode batch -nojournal -nolog -source release_builds.tcl")
  }
}
