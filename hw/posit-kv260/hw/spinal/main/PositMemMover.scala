package posit.kv260

import spinal.core._
import spinal.lib._
import spinal.lib.bus.amba4.axi._

// =====================================================================
//  Etap 6: wlasny master AXI4 silnika - czyta argumenty z DDR i zapisuje
//  wyniki, bez AXI DMA.
//
//    polecenie (len, src[a, b, c], read, dst, write)
//      odczyt: do 3 strumieni (a, b, c), kazdy z wlasnym ID AXI; serie INCR
//              do maxBurst slow, nigdy przez granice 4 KB; slowa danych ->
//              FIFO strumienia -> elementy n-bitowe (najmlodsze najpierw)
//      zapis:  wyniki n-bitowe -> slowa danych (WSTRB w ostatnim) -> serie
//              INCR (granica 4 KB), odpowiedzi B liczone do konca polecenia
//
//  Bez zakleszczenia na kanale R: seria dla strumienia idzie na AR tylko,
//  gdy jego FIFO ma miejsce na wszystkie zamowione, a nieodebrane slowa
//  (kredyty), wiec R nigdy nie czeka na silnik - dane a i b moga przychodzic
//  w dowolnej kolejnosci i przeplocie miedzy ID.
//
//  Adresy: wyrownane do slowa danych (sprawdza PositVecAxi przed startem).
//  Odpowiedz inna niz OKAY na R albo B: respError (do konca polecenia
//  dane leca dalej, wynik nalezy odrzucic).
// =====================================================================
case class MemMoverConfig(n         : Int,
                          addrWidth : Int = 32,
                          dataWidth : Int = 128,
                          maxBurst  : Int = 16,
                          fifoBeats : Int = 64,
                          lenWidth  : Int = 32) {
  require(dataWidth % n == 0 && isPow2(dataWidth / n), s"dataWidth $dataWidth / n $n")
  require(isPow2(maxBurst) && maxBurst <= 256 && fifoBeats >= 2 * maxBurst)
  def beatBytes : Int = dataWidth / 8
  def perBeat : Int = dataWidth / n
  def sources : Int = 3
  def axi : Axi4Config = Axi4Config(addressWidth = addrWidth, dataWidth = dataWidth, idWidth = 2,
    useRegion = false, useLock = false, useQos = false, useCache = true, useProt = true)
  /** Slowa danych na `len` elementow. */
  def beats(len : UInt) : UInt = {
    val s = len.resize(lenWidth + 1) + (perBeat - 1)
    s(lenWidth downto log2Up(perBeat)).resize(lenWidth)
  }
}

case class MemCmd(cfg : MemMoverConfig) extends Bundle {
  val len   = UInt(cfg.lenWidth bits)                 // elementy, >= 1
  val src   = Vec(UInt(cfg.addrWidth bits), cfg.sources)
  val read  = Bits(cfg.sources bits)                  // ktore strumienie czytac (a zawsze)
  val dst   = UInt(cfg.addrWidth bits)
  val write = Bool()
}

class PositMemMover(val cfg : MemMoverConfig) extends Component {
  import cfg._
  val io = new Bundle {
    val axi       = master(Axi4(cfg.axi))
    val cmd       = slave(Stream(MemCmd(cfg)))
    val src       = Vec(master(Stream(Fragment(Bits(n bits)))), sources)   // last: ostatni element
    val res       = slave(Stream(Bits(n bits)))
    val busy      = out Bool()
    val done      = out Bool()                        // impuls: polecenie skonczone
    val respError = out Bool()                        // impuls: RRESP / BRESP != OKAY
  }

  val busy = RegInit(False)
  io.busy := busy
  io.cmd.ready := !busy
  val sizeField = U(log2Up(beatBytes), 3 bits)
  val cacheField = B"0011"                            // bufferable, modifiable (HP FPD, bez spojnosci)

  /** Najwiecej slow w serii od `addr`: limit serii, pozostale slowa, granica 4 KB. */
  def burstBeats(addr : UInt, left : UInt) : UInt = {
    val left4k = U(4096, 13 bits) - addr(11 downto 0).resize(13)
    val to4k = left4k(12 downto log2Up(beatBytes))
    val lim  = Mux(left < maxBurst, left.resize(lenWidth), U(maxBurst, lenWidth bits))
    Mux(to4k.resize(lenWidth) < lim, to4k.resize(lenWidth), lim)
  }

  // ---- odczyt ---------------------------------------------------------------------------
  val readers = for (i <- 0 until sources) yield new Area {
    val active    = RegInit(False)
    val addr      = Reg(UInt(addrWidth bits))
    val beatsLeft = Reg(UInt(lenWidth bits)) init 0       // jeszcze do zamowienia
    val elemsLeft = Reg(UInt(lenWidth bits)) init 0       // jeszcze do oddania
    val inFlight  = Reg(UInt(log2Up(fifoBeats + 1) bits)) init 0
    val fifo      = StreamFifo(Bits(dataWidth bits), fifoBeats)
    val burst     = burstBeats(addr, beatsLeft)
    val room      = U(fifoBeats, log2Up(fifoBeats + 1) + 1 bits) - fifo.io.occupancy.resize(log2Up(fifoBeats + 1) + 1) -
                    inFlight.resize(log2Up(fifoBeats + 1) + 1)
    val canIssue  = active && beatsLeft =/= 0 && room >= burst.resize(log2Up(fifoBeats + 1) + 1)

    // elementy ze slow: najmlodsze bity najpierw
    val idx  = Reg(UInt(log2Up(perBeat) max 1 bits)) init 0
    val pop  = fifo.io.pop
    val out  = io.src(i)
    val lastElem = elemsLeft === 1
    out.valid    := pop.valid && elemsLeft =/= 0
    out.fragment := (if (perBeat == 1) pop.payload else pop.payload.subdivideIn(n bits)(idx.resize(log2Up(perBeat))))
    out.last     := lastElem
    pop.ready    := out.fire && (lastElem || (if (perBeat == 1) True else idx === perBeat - 1))
    when(out.fire) {
      elemsLeft := elemsLeft - 1
      idx := Mux(lastElem || (if (perBeat == 1) True else idx === perBeat - 1), U(0, idx.getWidth bits), idx + 1)
      when(lastElem) { active := False }
    }
    val idle = !active
  }

  // AR: jeden rejestr, karuzela po strumieniach z kredytem
  val ar = new Area {
    val valid = RegInit(False)
    val id    = Reg(UInt(2 bits))
    val addr  = Reg(UInt(addrWidth bits))
    val len   = Reg(UInt(8 bits))
    val rr    = Reg(UInt(2 bits)) init 0
    io.axi.ar.valid := valid
    io.axi.ar.addr  := addr
    io.axi.ar.id    := id
    io.axi.ar.len   := len
    io.axi.ar.size  := sizeField
    io.axi.ar.setBurstINCR()
    io.axi.ar.cache := cacheField
    io.axi.ar.prot  := B"000"
    when(io.axi.ar.fire) { valid := False }

    // kolejnosc sprawdzania od rr (modulo liczba strumieni); wybor jednego strumienia na takt
    def wrap(x : UInt) : UInt = Mux(x >= sources, x - sources, x).resize(2)
    val order  = (0 until sources).map(k => wrap(rr.resize(3) + k))
    val canAt  = order.map(o => readers.zipWithIndex.map { case (r, i) => o === i && r.canIssue }.reduce(_ || _))
    val any    = canAt.reduce(_ || _)
    val pickK  = OHToUInt(OHMasking.first(Vec(canAt).asBits))
    val pick   = order.zipWithIndex.map { case (o, k) => Mux(pickK === k, o, U(0, 2 bits)) }.reduce(_ | _)
    when((!valid || io.axi.ar.fire) && any) {
      valid := True
      id    := pick
      rr    := wrap(pick.resize(3) + 1)
      for ((r, i) <- readers.zipWithIndex) when(pick === i) {
        addr := r.addr
        len  := (r.burst - 1).resize(8)
        r.addr      := r.addr + (r.burst << log2Up(beatBytes)).resize(addrWidth)
        r.beatsLeft := r.beatsLeft - r.burst
      }
    }
  }

  // R: do FIFO strumienia z RID (miejsce zarezerwowane kredytem)
  io.axi.r.ready := True
  for ((r, i) <- readers.zipWithIndex) {
    r.fifo.io.push.valid   := io.axi.r.valid && io.axi.r.id === i
    r.fifo.io.push.payload := io.axi.r.data
  }
  // inFlight: zamowienie (+burst) i odbior (-1) moga trafic na ten sam takt
  for ((r, i) <- readers.zipWithIndex) {
    val add = Mux((!ar.valid || io.axi.ar.fire) && ar.any && ar.pick === i, r.burst.resize(r.inFlight.getWidth), U(0, r.inFlight.getWidth bits))
    val sub = (io.axi.r.valid && io.axi.r.id === i).asUInt.resize(r.inFlight.getWidth)
    r.inFlight := r.inFlight + add - sub
  }
  val rError = io.axi.r.valid && io.axi.r.resp =/= 0

  // ---- zapis ----------------------------------------------------------------------------
  val writer = new Area {
    val active    = RegInit(False)
    val addr      = Reg(UInt(addrWidth bits))
    val beatsLeft = Reg(UInt(lenWidth bits)) init 0     // slowa do zamowienia na AW
    val elemsLeft = Reg(UInt(lenWidth bits)) init 0     // wyniki do przyjecia
    val bursts    = Reg(UInt(lenWidth bits)) init 0     // serie wyslane na AW, bez B
    val lenQ      = StreamFifo(UInt(8 bits), 8)         // dlugosci serii dla W (WLAST)

    // AW
    val awValid = RegInit(False)
    val awAddr  = Reg(UInt(addrWidth bits))
    val awLen   = Reg(UInt(8 bits))
    val burst   = burstBeats(addr, beatsLeft)
    io.axi.aw.valid := awValid
    io.axi.aw.addr  := awAddr
    io.axi.aw.id    := 0
    io.axi.aw.len   := awLen
    io.axi.aw.size  := sizeField
    io.axi.aw.setBurstINCR()
    io.axi.aw.cache := cacheField
    io.axi.aw.prot  := B"000"
    lenQ.io.push.valid   := False
    lenQ.io.push.payload := (burst - 1).resize(8)
    when(io.axi.aw.fire) { awValid := False }
    when((!awValid || io.axi.aw.fire) && active && beatsLeft =/= 0 && lenQ.io.push.ready) {
      awValid   := True
      awAddr    := addr
      awLen     := (burst - 1).resize(8)
      lenQ.io.push.valid := True
      addr      := addr + (burst << log2Up(beatBytes)).resize(addrWidth)
      beatsLeft := beatsLeft - burst
    }

    // pakowanie wynikow w slowa: rejestr slowa + licznik miejsc
    val acc     = Reg(Bits(dataWidth bits)) init 0
    val fill    = Reg(UInt(log2Up(perBeat + 1) bits)) init 0
    val full    = RegInit(False)
    val strbAcc = Reg(Bits(beatBytes bits)) init 0
    val beat    = Stream(Bits(dataWidth + beatBytes bits))
    beat.valid   := full
    beat.payload := strbAcc ## acc
    io.res.ready := active && (!full || beat.ready)
    when(beat.fire) { full := False; fill := 0; strbAcc := 0 }
    when(io.res.fire) {
      val base  = Mux(full, U(0, fill.getWidth bits), fill)   // pelne slowo wychodzi w tym takcie: nowe od zera
      val slot  = base.resize(log2Up(perBeat) max 1)
      val bytesPer = n / 8 max 1
      val laneStrb = B((BigInt(1) << (n / 8 max 1)) - 1, bytesPer bits)
      for (k <- 0 until perBeat) when(slot === k) {
        acc(k * n, n bits) := io.res.payload
      }
      val strbNew = Bits(beatBytes bits)
      strbNew := Mux(full, B(0, beatBytes bits), strbAcc)
      for (k <- 0 until perBeat) when(slot === k) {
        strbNew(k * bytesPer, bytesPer bits) := laneStrb
      }
      strbAcc   := strbNew
      fill      := base + 1
      elemsLeft := elemsLeft - 1
      when(base === perBeat - 1 || elemsLeft === 1) { full := True }
    }

    // W: slowa po kolei, WLAST z dlugosci serii
    val inBurst = Reg(UInt(8 bits)) init 0
    io.axi.w.valid := beat.valid && lenQ.io.pop.valid
    io.axi.w.data  := beat.payload(dataWidth - 1 downto 0)
    io.axi.w.strb  := beat.payload(dataWidth + beatBytes - 1 downto dataWidth)
    io.axi.w.last  := inBurst === lenQ.io.pop.payload
    beat.ready     := io.axi.w.ready && lenQ.io.pop.valid
    lenQ.io.pop.ready := io.axi.w.fire && io.axi.w.last
    when(io.axi.w.fire) { inBurst := Mux(io.axi.w.last, U(0, 8 bits), inBurst + 1) }

    // B
    io.axi.b.ready := True
    val bError = io.axi.b.valid && io.axi.b.resp =/= 0
    val issued = io.axi.aw.fire
    bursts := bursts + issued.asUInt.resize(lenWidth) - io.axi.b.fire.asUInt.resize(lenWidth)
    val finished = active && beatsLeft === 0 && elemsLeft === 0 && !full && !awValid && bursts === 0 &&
                   !io.axi.b.valid && !lenQ.io.pop.valid
  }

  // ---- polecenie --------------------------------------------------------------------------
  io.done := False
  io.respError := rError || writer.bError
  when(io.cmd.fire) {
    busy := True
    val beatsN = beats(io.cmd.len)
    for ((r, i) <- readers.zipWithIndex) {
      val on = io.cmd.read(i) || (if (i == 0) True else False)
      r.active    := on
      r.addr      := io.cmd.src(i)
      r.beatsLeft := Mux(on, beatsN, U(0, lenWidth bits))
      r.elemsLeft := Mux(on, io.cmd.len, U(0, lenWidth bits))
      r.idx       := 0
    }
    writer.active    := io.cmd.write
    writer.addr      := io.cmd.dst
    writer.beatsLeft := Mux(io.cmd.write, beatsN, U(0, lenWidth bits))
    writer.elemsLeft := Mux(io.cmd.write, io.cmd.len, U(0, lenWidth bits))
  }
  val readsDone = readers.map(r => r.idle && r.inFlight === 0).reduce(_ && _)
  when(busy && !io.cmd.fire && readsDone && (!writer.active || writer.finished)) {
    busy := False
    writer.active := False
    io.done := True
  }
}
