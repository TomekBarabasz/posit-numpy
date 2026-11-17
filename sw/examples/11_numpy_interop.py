"""NumPy interoperability: which NumPy functions work on PositArray, and how.

    python3 examples/11_numpy_interop.py [--sim | --hw [APP]]

PositArray is its own class (not an ndarray subclass) that implements NumPy's __array_ufunc__ and
__array_function__ protocols. Supported functions run in posit arithmetic; everything else raises
TypeError instead of silently computing on the raw bits as integers.
"""

import numpy as np

import _common
import positnp as pn


def main(argv=None):
    _common.start(__doc__, argv)
    x = pn.array(np.linspace(-1.0, 1.0, 12)).reshape(3, 4)

    _common.section("shape, indexing, assignment")
    print(f"  x.shape = {x.shape}, x.ndim = {x.ndim}, x.size = {x.size}, len(x) = {len(x)}")
    print(f"  x[1]     = {x[1]}")
    print(f"  x[:, 0]  = {x[:, 0]}")
    print(f"  x.T.shape = {x.T.shape}, x.ravel()[:3] = {x.ravel()[:3]}")
    y = x.copy()
    y[0, :] = 7.0                              # a Python number: rounded to the posit format of y
    y[y < 0] = 0.0                             # boolean masks work as in NumPy
    print(f"  after y[0, :] = 7; y[y < 0] = 0:\n{y.to_float()}")

    _common.section("ufuncs (np.add, np.sqrt, np.exp, ...)")
    v = pn.array([0.25, 1.0, 4.0])
    for f in (np.add, np.multiply):
        print(f"  np.{f.__name__}(v, 2) = {f(v, 2.0)}")
    for f in (np.negative, np.absolute, np.square, np.sqrt, np.exp, np.log, np.tanh):
        print(f"  np.{f.__name__}(v) = {f(v)}")
    print(f"  np.isnan(v / 0) = {np.isnan(v / 0.0)}")

    _common.section("array functions")
    a, b = pn.array([3.0, 1.0, 2.0]), pn.array([1.0, 1.0, 1.0])
    print(f"  np.concatenate = {np.concatenate([a, b])}")
    print(f"  np.stack       = {np.stack([a, b]).shape}")
    print(f"  np.where       = {np.where(a > 1.5, a, b)}")
    print(f"  np.argsort     = {np.argsort(a)}")
    print(f"  np.sum, np.mean, np.dot, np.linalg.norm = {float(np.sum(a))}, {float(np.mean(a)):.8g}, "
          f"{float(np.dot(a, b))}, {float(np.linalg.norm(a)):.8g}   (quire: exact, rounded once)")
    print(f"  np.sum(x, axis=1) = {np.sum(x, axis=1)}")
    print("  np.matmul, @, np.convolve, np.correlate, np.fft.fft: examples 20 - 22")

    _common.section("mixing with NumPy arrays and Python numbers")
    f = np.array([0.5, 0.5, 0.5])
    print(f"  posit + float64 ndarray = {a + f}   (the ndarray is rounded to posit32 first)")
    print(f"  np.asarray-like copies: a.to_float() = {a.to_float()}, a.astype(np.float32) = {a.astype(np.float32)}")
    print(f"  float(a[0]) = {float(a[0])}")

    _common.section("what is not supported (TypeError, never a silent integer result)")
    for name, call in (("np.prod(a)", lambda: np.prod(a)), ("np.cumsum(a)", lambda: np.cumsum(a)),
                       ("np.sin(a, out=a)", lambda: np.sin(a, out=a)),
                       ("a + pn.array(a, pn.P16)", lambda: a + pn.array(a, pn.P16))):
        try:
            call()
            print(f"  {name}: ok")
        except TypeError as e:
            msg = str(e) if len(str(e)) < 90 else str(e)[:60] + " ..."
            print(f"  {name}: TypeError: {msg}")


if __name__ == "__main__":
    main()
