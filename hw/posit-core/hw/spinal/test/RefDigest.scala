package posit

import java.security.MessageDigest
import scala.io.Source
import newhope.vertebra._

// =====================================================================
//  Skroty SHA-256 wynikow PositRef - wspolny wzorzec dla modelu w Scali
//  i w Pythonie (sw/positnp/ref.py, sw/tests/test_ref.py).
//
//  Zestaw: wszystkie pary posit<8,2> i posit<8,0>, oraz pary z LCG
//  (ten sam generator w obu jezykach) dla posit<12,3>, <16,2>, <32,2>.
//  Linia skrotu: "<wynik hex> <flagi>\n" dla kazdej pary w kolejnosci.
//  Quire (etap 4, op qsum / qdot / qsumsq): pary z LCG dzielone na wektory
//  o dlugosciach 1, 2, ..., 37, 1, 2, ...; linia na wektor:
//  "<wynik hex> <flagi> <surowy quire hex, quireWidth(64) b U2 albo nar>\n".
//
//  Plik: sw/tests/ref_digests.txt. Po swiadomej zmianie modelu:
//    sbt "posit-core/Test/runMain posit.RefDigest"
// =====================================================================
object RefDigest {
  val path = "../../sw/tests/ref_digests.txt"   // wzgledem hw/posit-core (fork)

  /** PCG-owy LCG 64 b; para = dwie kolejne wartosci, n gornych bitow. */
  def lcg(n : Int, seed : Long, count : Int) : Iterator[(BigInt, BigInt)] = {
    val mod = BigInt(1) << 64
    var s = BigInt(seed)
    def next() : BigInt = { s = (s * BigInt("6364136223846793005") + BigInt("1442695040888963407")) % mod; s >> (64 - n) }
    Iterator.fill(count) { val a = next(); val b = next(); (a, b) }
  }

  case class Entry(n : Int, es : Int, op : String, set : String, count : Int)

  val quireOps = Seq("qsum", "qdot", "qsumsq")

  val entries : Seq[Entry] =
    (for (es <- Seq(2, 0); op <- PositOp.all) yield Entry(8, es, op.name, "all", 65536)) ++
    (for ((n, es, seed) <- Seq((12, 3, 3), (16, 2, 1), (32, 2, 2)); op <- PositOp.all)
      yield Entry(n, es, op.name, s"lcg$seed", 20000)) ++
    (for ((n, es, seed) <- Seq((8, 2, 4), (16, 2, 5), (32, 2, 6)); op <- quireOps)
      yield Entry(n, es, op, s"lcg$seed", 20000))

  /** Wektory quire: kolejne pary z LCG, dlugosci 1, 2, ..., 37, 1, 2, ... */
  def quireVectors(e : Entry) : Iterator[Seq[(BigInt, BigInt)]] = {
    val it = pairs(e)
    Iterator.from(0).map(i => it.take(1 + i % 37).toSeq).takeWhile(_.nonEmpty)
  }

  def quireDigest(e : Entry, md : MessageDigest) : Unit = {
    val c = PositConfig(e.n, e.es)
    val w = c.quireWidth(64)
    for (v <- quireVectors(e)) {
      val terms = v.iterator.map { case (a, b) => e.op match {
        case "qsum" => (a, None); case "qdot" => (a, Some(b)); case _ => (a, Some(a)) } }
      val (q, nar) = QuireRef.accumulate(c, terms)
      val (r, f) = QuireRef.round(c, q, nar)
      val raw = if (nar) "nar" else QuireRef.raw(c, q, w).toString(16)
      md.update(s"${r.toString(16)} ${f.bits} $raw\n".getBytes("US-ASCII"))
    }
  }

  def pairs(e : Entry) : Iterator[(BigInt, BigInt)] =
    if (e.set == "all") PositSimKit.exhaustive(PositConfig(e.n, e.es)) else lcg(e.n, e.set.drop(3).toLong, e.count)

  def digest(e : Entry) : String = {
    val md = MessageDigest.getInstance("SHA-256")
    if (quireOps.contains(e.op)) { quireDigest(e, md); return md.digest().map("%02x".format(_)).mkString }
    val c  = PositConfig(e.n, e.es)
    val op = PositOp.all.find(_.name == e.op).get
    for ((a, b) <- pairs(e)) {
      val (r, f) = PositRef.withFlags(c, op, a, b)
      md.update(s"${r.toString(16)} ${f.bits}\n".getBytes("US-ASCII"))
    }
    md.digest().map("%02x".format(_)).mkString
  }

  def line(e : Entry) : String = s"${e.n} ${e.es} ${e.op} ${e.set} ${e.count} ${digest(e)}"

  def main(args : Array[String]) : Unit = {
    val lines = "# n es op set count sha256(\"<wynik hex> <flagi>\\n\" po parach) - z RefDigest.scala" +: entries.map(line)
    java.nio.file.Files.write(java.nio.file.Paths.get(path), (lines.mkString("\n") + "\n").getBytes("UTF-8"))
    println(s"zapisano $path (${entries.size} skrotow)")
  }
}

class RefDigestTestplan extends TestplanSuite {
  override def testplan = Seq(
    Testpoint("ref_digest_match", Stage.V1,
      "PositRef (Scala) daje te same skroty co sw/tests/ref_digests.txt (wspolne z ref.py)",
      checking = Seq("kazdy wpis z RefDigest.entries jest w pliku z tym samym skrotem")))

  testpoint("ref_digest_match") {
    val file = Source.fromFile(RefDigest.path).getLines().filterNot(_.startsWith("#")).toSet
    val bad  = RefDigest.entries.map(RefDigest.line).filterNot(file.contains)
    assert(bad.isEmpty, s"rozne skroty (model zmieniony? sbt \"posit-core/Test/runMain posit.RefDigest\"): ${bad.mkString("; ")}")
  }
}
