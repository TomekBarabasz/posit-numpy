import spinal.core._
import spinal.lib._

package object posit {
  implicit class StreamStaging[T <: Data](s: Stream[T]) {
    /** A pipeline register as an elaboration-time decision. */
    def stageIf(cond: Boolean): Stream[T] = if (cond) s.stage() else s
  }
}
