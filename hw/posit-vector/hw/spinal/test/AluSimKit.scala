package posit.vector

import spinal.core._
import spinal.core.sim._
import spinal.lib.sim._
import scala.collection.mutable
import scala.util.Random
import posit._
import posit.PositSimKit.Pair

// =====================================================================
//  Sterownik i scoreboard PositAlu: polecenia (op, a, b) z losowymi
//  przerwami w valid, losowy ready, wynik i flagi bit w bit z
//  PositRef.withFlags w kolejnosci transakcji.
//  Dostepne w testach posit-kv260 i top-modules (test->test).
// =====================================================================
object AluSimKit {
  /** Jedno polecenie ALU: kod operacji (PositAluOp) i argumenty. */
  case class Cmd(op : Int, a : BigInt, b : BigInt)

  def opOf(code : Int) : Option[PositOp] = PositAluOp.codes.collectFirst { case (op, `code`) => op }

  /** Oczekiwany wynik: dla operacji spoza `ops` (albo nieznanego kodu) NaR z flaga nar. */
  def expected(cfg : PositAluConfig, cmd : Cmd) : (BigInt, Int) = opOf(cmd.op).filter(cfg.has) match {
    case Some(op) => val (r, f) = PositRef.withFlags(cfg.c, op, cmd.a, cmd.b); (r, f.bits)
    case None     => (BigInt(1) << (cfg.c.n - 1), PositRef.Flags(nar = true).bits)
  }

  def cmds(op : PositOp, pairs : Iterator[Pair]) : Iterator[Cmd] = pairs.map { case (a, b) => Cmd(PositAluOp.code(op), a, b) }

  /** Losowa operacja z `ops` na kazda pare. */
  def mixed(ops : Seq[PositOp], pairs : Iterator[Pair], rnd : Random) : Iterator[Cmd] =
    pairs.map { case (a, b) => Cmd(PositAluOp.code(ops(rnd.nextInt(ops.size))), a, b) }

  /** fires: takt kazdego wyniku (wzgledem startu), do pomiaru przepustowosci. */
  case class Run(checked : Long, cycles : Long, flagCounts : Map[String, Long], fires : Vector[Long])

  def drive(dut         : PositAlu,
            cmds        : Iterator[Cmd],
            label       : String,
            readyFactor : Float = 0.5f,
            extra       : ClockDomain => Unit = _ => (),
            gaps        : Boolean = true) : Run = {
    val cfg = dut.cfg
    val cd  = dut.clockDomain
    dut.io.cmd.valid #= false
    dut.io.rsp.ready #= false
    cd.forkStimulus(10)
    cd.waitSampling(4)
    extra(cd)

    val queue   = mutable.Queue[(Cmd, BigInt, Int)]()
    var checked = 0L
    var cycles  = 0L
    val counts  = mutable.Map("nar" -> 0L, "sat" -> 0L, "inexact" -> 0L)
    fork { while (true) { cd.waitSampling(); cycles += 1 } }

    var fires   = Vector[Long]()
    val driver = StreamDriver(dut.io.cmd, cd) { p =>
      cmds.hasNext && {
        val x = cmds.next()
        p.op #= x.op
        p.a  #= x.a
        p.b  #= x.b
        val (r, f) = expected(cfg, x)
        queue.enqueue((x, r, f))
        true
      }
    }
    if (!gaps) driver.transactionDelay = () => 0
    StreamReadyRandomizer(dut.io.rsp, cd).factor = readyFactor
    StreamMonitor(dut.io.rsp, cd) { rsp =>
      assert(queue.nonEmpty, s"$label: wynik bez polecenia")
      val (x, want, wantF) = queue.dequeue()
      val got  = rsp.bits.toBigInt
      val gotF = (if (rsp.flags.nar.toBoolean) 1 else 0) | (if (rsp.flags.sat.toBoolean) 2 else 0) |
                 (if (rsp.flags.inexact.toBoolean) 4 else 0)
      assert(got == want && gotF == wantF,
        f"${cfg.c} $label: op=${x.op} a=0x${x.a}%x b=0x${x.b}%x -> 0x$got%x flags=$gotF%03d, oczekiwane 0x$want%x flags=$wantF%03d (bity: nar=1 sat=2 inexact=4)")
      if ((gotF & 1) != 0) counts("nar") += 1
      if ((gotF & 2) != 0) counts("sat") += 1
      if ((gotF & 4) != 0) counts("inexact") += 1
      checked += 1
      fires :+= cycles
    }

    cd.waitSamplingWhere(!cmds.hasNext && queue.isEmpty)
    Run(checked, cycles, counts.toMap, fires)
  }
}
