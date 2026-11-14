package posit.vector

import spinal.core._
import spinal.lib._
import posit._

// =====================================================================
//  PositStreamAlu: PositAlu dla tablic (plan, etap 2).
//
//    input  : Stream(Fragment(a, b)) -> ALU (op z io.op)        -> output : Stream(Fragment(wynik, flagi))
//    reg : Stream(PositAluCmd)     -> ALU (ta sama instancja)   -> regRsp
//
//  - tablica (+) tablica: a i b z elementu strumienia; tablica (+) skalar
//    (io.scalarB): b z io.b dla kazdego elementu,
//  - FMA (etap 5): a * b + io.addend; AXPY: a * io.b + b ze strumienia;
//    FMA3 (etap 6, cfg.fma3): a * b + c3, c3 z elementu strumienia (trzecia tablica)
//    (y = alfa * x + y: x w polowie a, y w polowie b, alfa w OPERAND_B),
//  - `last` przechodzi z wejscia na wyjscie przy tym samym elemencie
//    (S2MM w AXI DMA konczy transfer na tlast),
//  - sciezka rejestrowa (pojedyncze dzialania z etapu 1) dzieli ALU ze
//    strumieniem: arbiter z pierwszenstwem rejestrow, a FIFO znacznikow
//    (zrodlo, last) w kolejnosci polecen mowi, dokad idzie kazdy wynik.
//    ALU zachowuje kolejnosc, wiec znacznik i wynik spotykaja sie w StreamJoin,
//  - FIFO znacznikow pilnuje tez liczby elementow w ALU (inFlight);
//    glebokosc z zapasem ponad liczbe rejestrow potoku.
// =====================================================================
case class PositPair(c : PositConfig, withC : Boolean = false) extends Bundle {
  val a, b = Bits(c.n bits)
  val c3   = withC generate Bits(c.n bits)       // FMA3 (etap 6): trzecia tablica, a * b + c3
}

class PositStreamAlu(val cfg : PositAluConfig, tagDepth : Int = 0) extends Component {
  val c = cfg.c
  val io = new Bundle {
    val input   = slave(Stream(Fragment(PositPair(c, cfg.fma3))))
    val output  = master(Stream(Fragment(PositAluRsp(c))))
    val op      = in UInt(PositAluOp.width bits)   // operacja dla strumienia
    val scalarB = in Bool()                         // tablica (+) skalar: b = io.b
    val b       = in Bits(c.n bits)
    val addend  = in Bits(cfg.c.n bits)             // FMA: a * b + io.addend; AXPY: a * io.b + b ze strumienia

    val reg     = slave(Stream(PositAluCmd(c)))
    val regRsp  = master(Stream(PositAluRsp(c)))

    val inFlight = out Bool()                       // jakikolwiek element w ALU
  }

  case class Tag() extends Bundle {
    val fromStream, last = Bool()
  }
  case class Tagged() extends Bundle {
    val cmd = PositAluCmd(c)
    val tag = Tag()
  }

  val alu = new PositAlu(cfg)

  val fromReg = io.reg ~~ { x =>
    val t = Tagged()
    t.cmd            := x
    t.tag.fromStream := False
    t.tag.last       := False
    t
  }
  val fromIn = io.input ~~ { x =>
    val t = Tagged()
    val axpy = io.op === PositAluOp.AXPY
    t.cmd.op         := io.op
    t.cmd.a          := x.fragment.a
    t.cmd.b          := Mux(io.scalarB || axpy, io.b, x.fragment.b)
    t.cmd.addend     := Mux(axpy, x.fragment.b, io.addend)
    if (cfg.fma3) when(io.op === PositAluOp.FMA3) { t.cmd.addend := x.fragment.c3 }
    t.tag.fromStream := True
    t.tag.last       := x.last
    t
  }

  // rejestry maja pierwszenstwo: pojedyncze dzialanie nie czeka na koniec tablicy
  val merged = StreamArbiterFactory().lowerFirst.noLock.on(Seq(fromReg, fromIn))

  // ALU i FIFO znacznikow przyjmuja polecenie w tym samym takcie
  val (toAlu, toTags) = StreamFork2(merged, synchronous = true)
  alu.io.cmd << toAlu.translateWith(toAlu.cmd)

  // znaczniki: tyle, ile elementow miesci ALU, zeby wszystkie tory wielotaktowe mialy prace
  val tags = StreamFifo(Tag(), if (tagDepth > 0) tagDepth else scala.math.max(8, cfg.maxInFlight))
  tags.io.push << toTags.translateWith(toTags.tag)
  io.inFlight := tags.io.occupancy =/= 0

  val joined = StreamJoin(alu.io.rsp, tags.io.pop)
  val routed = StreamDemux(joined, joined.payload._2.fromStream.asUInt, 2)

  io.regRsp << routed(0).translateWith(routed(0).payload._1)
  io.output << routed(1).translateWith {
    val f = Fragment(PositAluRsp(c))
    f.fragment := routed(1).payload._1
    f.last     := routed(1).payload._2.last
    f
  }

  setDefinitionName(s"PositStreamAlu_${c.n}_${c.es}")
}
