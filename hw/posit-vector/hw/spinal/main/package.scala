package posit

// =====================================================================
//  posit.vector - silnik wektorowy (plan: etapy 1, 2, 4, 5).
//  Niezalezny od magistral: wejscie i wyjscie to Stream/Flow z
//  spinal.lib, opakowania AXI sa w posit.kv260.
//
//  Planowane:
//    PositAlu        wspolny dekoder i enkoder, mux add/sub/mul/div,
//                    flagi NaR / nasycenie / niedokladny     (etap 1)
//    PositStreamAlu  strumien par -> strumien wynikow, tryb
//                    tablica (+) skalar                       (etap 2)
//    Quire           akumulator 16*n bitow, sum/dot/sumsq     (etap 4)
// =====================================================================
package object vector
