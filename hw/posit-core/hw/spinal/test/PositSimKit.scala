package posit

import spinal.core._
import spinal.core.sim._
import spinal.lib.sim._
import scala.collection.mutable
import scala.util.Random

// =====================================================================
//  Wspolne narzedzia testow jednostek Stream(PositOperands) -> Stream(Bits).
//  Dostepne tez w testach posit-vector i posit-kv260 (test->test).
//
//  Liczba wektorow: domyslnie szybki zestaw (sbt test w kilka minut),
//  POSIT_SIM_FULL=1 przywraca liczby z posit-spinal (300k / 100k).
//  Wyczerpujace posit<8,*> zawsze w calosci.
// =====================================================================
object PositSimKit {
  def full : Boolean = sys.env.get("POSIT_SIM_FULL").orElse(sys.props.get("posit.sim.full")).contains("1")

  /** count w trybie pelnym, count / div w szybkim (co najmniej 1000). */
  def vectors(count : Int, div : Int = 15) : Int = if (full) count else (count / div) max 1000

  type Pair = (BigInt, BigInt)

  def exhaustive(c : PositConfig) : Iterator[Pair] =
    for (a <- Iterator.range(0, 1 << c.n); b <- Iterator.range(0, 1 << c.n)) yield (BigInt(a), BigInt(b))

  /** Rownomierne wzorce bitow plus pary bliskie znoszenia sie (b blisko a albo -a). */
  def stress(c : PositConfig, count : Int, rnd : Random) : Iterator[Pair] = {
    val mask = (BigInt(1) << c.n) - 1
    Iterator.fill(count) {
      val a = BigInt(c.n, rnd)
      val d = BigInt(rnd.nextInt(81) - 40)
      rnd.nextInt(3) match {
        case 0 => (a, BigInt(c.n, rnd))
        case 1 => (a, (a + d) & mask)
        case _ => (a, (-a + d) & mask)
      }
    }
  }

  /** Wynik scoreboardu: ile wektorow sprawdzono i w ilu taktach. */
  case class Run(checked : Long, cycles : Long)

  /** Sterownik z losowymi przerwami w valid, losowy ready (readyFactor = P(ready)),
    * scoreboard wzgledem PositRef w kolejnosci transakcji.
    * extra: dodatkowe checkery (StreamConformance) odpalane po starcie zegara. */
  def drive(dut         : PositStreamComponent,
            op          : PositOp,
            vectors     : Iterator[Pair],
            label       : String,
            readyFactor : Float = 0.5f,
            extra       : ClockDomain => Unit = _ => ()) : Run = {
    val c   = dut.c
    val cd  = dut.clockDomain
    dut.io.cmd.valid #= false
    dut.io.rsp.ready #= false
    cd.forkStimulus(10)
    cd.waitSampling(4)
    extra(cd)

    val expected = mutable.Queue[(BigInt, BigInt, BigInt)]()
    var checked  = 0L
    var cycles   = 0L
    fork { while (true) { cd.waitSampling(); cycles += 1 } }

    StreamDriver(dut.io.cmd, cd) { cmd =>
      vectors.hasNext && {
        val (a, b) = vectors.next()
        cmd.a #= a
        cmd.b #= b
        expected.enqueue((a, b, PositRef(c, op, a, b)))
        true
      }
    }
    StreamReadyRandomizer(dut.io.rsp, cd).factor = readyFactor
    StreamMonitor(dut.io.rsp, cd) { rsp =>
      assert(expected.nonEmpty, s"$c $label: wynik bez polecenia")
      val (a, b, want) = expected.dequeue()
      val got = rsp.toBigInt
      assert(got == want, f"$c $label: a=0x$a%x b=0x$b%x -> 0x$got%x, oczekiwane 0x$want%x")
      checked += 1
    }

    cd.waitSamplingWhere(!vectors.hasNext && expected.isEmpty)
    Run(checked, cycles)
  }
}
