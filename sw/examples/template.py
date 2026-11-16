"""Template for your own experiment: copy this file, keep the structure, change compute().

    cp examples/template.py examples/my_experiment.py
    python3 examples/my_experiment.py               # software (any PC)
    python3 examples/my_experiment.py --sim         # through the hardware driver, simulated engines
    sudo python3 examples/my_experiment.py --hw     # KV260

The pattern used by all the comparisons in this folder:
1. make the input data in float64;
2. round it to every format you compare, and compute the reference from the *rounded* inputs when you
   want the error of the arithmetic alone (or from the original float64 data for the total error);
3. run the same algorithm in each format - PositArray mostly looks like a NumPy array;
4. report relative errors (or correct digits) next to each other.
"""

import numpy as np

import _common
import positnp as pn

FORMATS = {
    "posit32": pn.P32, "posit16": pn.P16, "posit8": pn.P8,
    "float32": np.float32, "float16": np.float16,
}


def to_format(x, fmt):
    return pn.array(x, fmt) if isinstance(fmt, pn.PositConfig) else np.asarray(x, dtype=fmt)


def compute(x):
    """The algorithm under test - written once, runs on PositArray and on NumPy arrays alike.

    np.mean on posits rounds once from the exact quire sum. np.sum(x * x) / x.size would round the sum
    first (~1e4, where posit16 has only 9 fraction bits) and then the quotient - try it."""
    return np.mean(x * x)


def main(argv=None):
    _common.start(__doc__, argv)
    rnd = np.random.default_rng(0)
    data = rnd.standard_normal(10_000)
    reference = float(compute(data))                # float64

    rows = []
    for name, fmt in FORMATS.items():
        with np.errstate(all="ignore"):
            y = compute(to_format(data, fmt))
        e = _common.rel_err(y, reference)
        rows.append((name, f"{float(_common.f64(y)):.10g}", e, _common.digits(e)))
    _common.table(rows, ["format", "result", "relative error", "digits"])
    print(f"\nreference (float64): {reference:.10g};  posit flags: {pn.stats(pn.P32)}")


if __name__ == "__main__":
    main()
