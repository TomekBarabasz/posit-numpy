package posit

import spinal.core._
import spinal.lib._

/** Where registers cut the datapath. A cut is a Stream stage: payload, valid and backpressure. */
case class PositPipeline(afterDecode: Boolean = false, afterOp: Boolean = true, afterEncode: Boolean = false) {
  def latency: Int = Seq(afterDecode, afterOp, afterEncode).count(identity)
}

object PositPipeline {
  val combinational = PositPipeline(false, false, false)
  val full          = PositPipeline(true, true, true)
}

/** Every unit speaks the same protocol: operands in, encoded result out, both as Streams. */
abstract class PositStreamComponent(val c: PositConfig) extends Component {
  val io = new Bundle {
    val cmd = slave(Stream(PositOperands(c)))
    val rsp = master(Stream(Bits(c.n bits)))
  }
}

/** decode -> op -> round/encode, written as a composition of Stream transformations. */
class PositUnit(c: PositConfig, val op: PositOp, val pipe: PositPipeline = PositPipeline()) extends PositStreamComponent(c) {
  val decoded  = (io.cmd ~~ (o => PositDecodedPair.of(PositDecode(c, o.a), PositDecode(c, o.b)))).stageIf(pipe.afterDecode)
  val computed = (decoded ~~ (d => op.hw(d.a, d.b))).stageIf(pipe.afterOp)
  io.rsp << (computed ~~ (u => PositEncode(c, u))).stageIf(pipe.afterEncode)

  setDefinitionName(s"Posit${op.name.capitalize}_${c.n}_${c.es}")
}
