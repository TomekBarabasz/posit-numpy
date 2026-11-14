package posit.kv260

import spinal.core._
import spinal.core.sim._
import spinal.lib.bus.amba4.axi.Axi4
import scala.collection.mutable
import scala.util.Random

// =====================================================================
//  Model pamieci (slave AXI4) do testow mastera z etapu 6.
//
//  - pamiec bajtowa (rzadka), losowe ready na AR / AW / W, losowe przerwy
//    w R i B, losowe opoznienie serii,
//  - R: dane roznych ID przeplatane slowo po slowie i w dowolnej kolejnosci
//    (w obrebie jednego ID w kolejnosci) - tak, jak wolno slave'owi AXI4,
//  - sprawdzanie protokolu po stronie mastera: valid trzyma sie do ready
//    z niezmienionym ladunkiem, seria INCR, rozmiar = slowo, adres wyrownany,
//    dlugosc <= maxBurst, bez przekroczenia granicy 4 KB, WLAST na ostatnim
//    slowie serii, adresy w dozwolonym zakresie [lo, hi),
//  - wstrzykiwanie bledow: slowa pod adresami z `slverr` odpowiadaja SLVERR.
// =====================================================================
class AxiMemModel(axi : Axi4, cd : ClockDomain, rnd : Random, maxBurst : Int = 16) {
  val beatBytes = axi.config.dataWidth / 8
  val mem       = mutable.HashMap[Long, Int]()
  val slverr    = mutable.Set[Long]()          // adresy slow (wyrownane) z odpowiedzia SLVERR
  var lo = 0L
  var hi = 1L << 32
  var stall    = 0.3                           // prawdopodobienstwo braku ready / valid w takcie
  var maxDelay = 30                            // opoznienie serii odczytu / odpowiedzi B (takty)
  var idDelay  = Map[Int, Int]()               // dodatkowe opoznienie odczytu dla ID (testy kolejnosci)
  val violations = mutable.ArrayBuffer[String]()
  var arCount, awCount, rBeats, wBeats = 0L
  var lastW = (BigInt(0), BigInt(0))           // ostatnie slowo W (dane, WSTRB) - do diagnostyki
  var cycle = 0L

  def write8(a : Long, v : Int) : Unit = mem(a) = v & 0xff
  def read8(a : Long) : Int = mem.getOrElse(a, 0)
  def writeWords(addr : Long, bytesPer : Int, words : Seq[BigInt]) : Unit =
    for ((w, i) <- words.zipWithIndex; k <- 0 until bytesPer) write8(addr + i * bytesPer + k, ((w >> (8 * k)) & 0xff).toInt)
  def readWords(addr : Long, bytesPer : Int, count : Int) : Seq[BigInt] =
    (0 until count).map(i => (0 until bytesPer).map(k => BigInt(read8(addr + i * bytesPer + k)) << (8 * k)).sum)

  private def violation(s : String) : Unit = if (violations.size < 20) violations += f"takt $cycle: $s"

  private def checkBurst(kind : String, addr : Long, len : Int, size : Int, burst : Int) : Unit = {
    val beats = len + 1
    if (burst != 1) violation(s"$kind: burst $burst, nie INCR")
    if ((1 << size) != beatBytes) violation(s"$kind: size $size")
    if (addr % beatBytes != 0) violation(f"$kind: adres 0x$addr%x niewyrownany")
    if (beats > maxBurst) violation(s"$kind: seria $beats slow > $maxBurst")
    if ((addr % 4096) + beats * beatBytes > 4096) violation(f"$kind: seria 0x$addr%x + $beats slow przecina granice 4 KB")
    if (addr < lo || addr + beats * beatBytes > hi) violation(f"$kind: 0x$addr%x..+${beats * beatBytes} poza [0x$lo%x, 0x$hi%x)")
  }

  // ---- stabilnosc valid / ladunku po stronie mastera -----------------------------------------
  private class Hold(name : String, valid : => Boolean, ready : => Boolean, payload : => BigInt) {
    var pending = false
    var last = BigInt(0)
    def sample() : Unit = {
      if (pending) {
        if (!valid) violation(s"$name: valid opadl przed ready")
        else if (payload != last) violation(s"$name: ladunek zmienil sie przed ready")
      }
      pending = valid && !ready
      last = payload
    }
  }
  private def arPay = (axi.ar.addr.toBigInt << 16) | (axi.ar.len.toBigInt << 4) | axi.ar.id.toBigInt
  private def awPay = (axi.aw.addr.toBigInt << 16) | (axi.aw.len.toBigInt << 4) | axi.aw.id.toBigInt
  private def wPay  = (axi.w.data.toBigInt << 32) | (axi.w.strb.toBigInt << 1) | (if (axi.w.last.toBoolean) 1 else 0)
  private val holds = Seq(
    new Hold("AR", axi.ar.valid.toBoolean, axi.ar.ready.toBoolean, arPay),
    new Hold("AW", axi.aw.valid.toBoolean, axi.aw.ready.toBoolean, awPay),
    new Hold("W", axi.w.valid.toBoolean, axi.w.ready.toBoolean, wPay))

  // ---- odczyt ---------------------------------------------------------------------------------
  private case class RBurst(id : Int, addr : Long, beats : Int, readyAt : Long, var sent : Int = 0)
  private val reads = mutable.ArrayBuffer[RBurst]()
  private var rCur : Option[RBurst] = None

  // ---- zapis ----------------------------------------------------------------------------------
  private case class WBurst(id : Int, addr : Long, beats : Int)
  private val aws    = mutable.Queue[WBurst]()
  private val wq     = mutable.Queue[(BigInt, BigInt, Boolean)]()
  private val bq     = mutable.Queue[(Int, Long, Int)]()     // id, gotowe od taktu, resp
  private var bCur : Option[(Int, Int)] = None

  axi.ar.ready #= false
  axi.aw.ready #= false
  axi.w.ready  #= false
  axi.r.valid  #= false
  axi.b.valid  #= false

  private def driveR(b : RBurst) : Unit = {
    val a = b.addr + b.sent * beatBytes
    axi.r.valid #= true
    axi.r.id    #= b.id
    axi.r.data  #= (0 until beatBytes).map(k => BigInt(read8(a + k)) << (8 * k)).sum
    axi.r.resp  #= (if (slverr.contains(a)) 2 else 0)
    axi.r.last  #= b.sent == b.beats - 1
  }

  private def applyWrites() : Unit = {
    while (aws.nonEmpty && wq.size >= aws.head.beats) {
      val b = aws.dequeue()
      var err = 0
      for (i <- 0 until b.beats) {
        val (data, strb, last) = wq.dequeue()
        if (last != (i == b.beats - 1)) violation(s"W: WLAST na slowie $i serii ${b.beats}")
        val a = b.addr + i * beatBytes
        if (slverr.contains(a)) err = 2
        for (k <- 0 until beatBytes if strb.testBit(k)) write8(a + k, ((data >> (8 * k)) & 0xff).toInt)
      }
      bq.enqueue((b.id, cycle + 1 + rnd.nextInt(maxDelay + 1), err))
    }
  }

  cd.onSamplings {
    cycle += 1
    holds.foreach(_.sample())

    // AR
    if (axi.ar.valid.toBoolean && axi.ar.ready.toBoolean) {
      val (id, addr, len) = (axi.ar.id.toInt, axi.ar.addr.toLong, axi.ar.len.toInt)
      checkBurst("AR", addr, len, axi.ar.size.toInt, axi.ar.burst.toInt)
      reads += RBurst(id, addr, len + 1, cycle + 1 + rnd.nextInt(maxDelay + 1) + idDelay.getOrElse(id, 0))
      arCount += 1
    }
    // R
    rCur match {
      case Some(b) if axi.r.valid.toBoolean && axi.r.ready.toBoolean =>
        b.sent += 1
        rBeats += 1
        if (b.sent == b.beats) reads -= b
        rCur = None
      case Some(_) => // trzyma valid do ready
      case None =>
    }
    if (rCur.isEmpty) {
      // pierwsza seria kazdego ID (w ID kolejnosc), gotowa; wybor losowy -> przeplot miedzy ID
      val heads = reads.groupBy(_.id).values.map(_.head).filter(_.readyAt <= cycle).toSeq
      if (heads.nonEmpty && rnd.nextDouble() >= stall) {
        val b = heads(rnd.nextInt(heads.size))
        rCur = Some(b)
        driveR(b)
      } else axi.r.valid #= false
    }
    // AW
    if (axi.aw.valid.toBoolean && axi.aw.ready.toBoolean) {
      val (id, addr, len) = (axi.aw.id.toInt, axi.aw.addr.toLong, axi.aw.len.toInt)
      checkBurst("AW", addr, len, axi.aw.size.toInt, axi.aw.burst.toInt)
      aws.enqueue(WBurst(id, addr, len + 1))
      awCount += 1
    }
    // W
    if (axi.w.valid.toBoolean && axi.w.ready.toBoolean) {
      wq.enqueue((axi.w.data.toBigInt, axi.w.strb.toBigInt, axi.w.last.toBoolean))
      lastW = (axi.w.data.toBigInt, axi.w.strb.toBigInt)
      wBeats += 1
    }
    applyWrites()
    // B
    bCur match {
      case Some(_) if axi.b.valid.toBoolean && axi.b.ready.toBoolean => bCur = None
      case _ =>
    }
    if (bCur.isEmpty) {
      if (bq.nonEmpty && bq.head._2 <= cycle && rnd.nextDouble() >= stall) {
        val (id, _, resp) = bq.dequeue()
        bCur = Some((id, resp))
        axi.b.valid #= true
        axi.b.id    #= id
        axi.b.resp  #= resp
      } else axi.b.valid #= false
    }

    axi.ar.ready #= rnd.nextDouble() >= stall
    axi.aw.ready #= rnd.nextDouble() >= stall
    axi.w.ready  #= rnd.nextDouble() >= stall
  }

  def idle : Boolean = reads.isEmpty && rCur.isEmpty && aws.isEmpty && wq.isEmpty && bq.isEmpty && bCur.isEmpty
}
