# Simulation tests

This is a sample testbench for a Tiny Tapeout project. It uses [cocotb](https://docs.cocotb.org/en/stable/) to drive the DUT and check the outputs.
See below to get started or for more information, check the [website](https://tinytapeout.com/hdl/testing/).

## Top-level testbench setup

1. Edit [Makefile](Makefile) and modify `PROJECT_SOURCES` to point to your Verilog files.
2. Edit [tb.v](tb.v) and replace `tt_um_example` with your module name.

## How to run

From the repository root, with Icarus and the cocotb environment on `PATH`:

```sh
make -C test USE_DIVIDER_MODEL=1
```

The default target runs the top-level test and each directory with a Makefile
under `test/block-level-tests/`. A failing block fails the shared runner. Each
block writes its own `results.xml`; the top-level result remains in `test/`.
`make -C test clean USE_DIVIDER_MODEL=1` also cleans each block's selected build.

`USE_DIVIDER_MODEL=1` explicitly selects the PEm core's test-only Q12.4 divider.
The owner's `clock_divider.sv` is currently empty. Once its implementation is
available, omit this option to test the actual divider. Core ID defaults to zero;
use `CORE_ID=0`, `1`, `2`, or `3` to select a separately compiled core instance.

For block tests only:

```sh
make -C test block-tests USE_DIVIDER_MODEL=1
```

To run gatelevel simulation, first harden your project and copy
`runs/wokwi/results/final/verilog/gl/{your_module_name}.v` to
`test/gate_level_netlist.v`.

Then run:

```sh
make -C test -B GATES=yes
```

Gate-level simulation runs the top-level test only. For top-level RTL simulation
only, use `make -C test sim`.

If you wish to save the waveform in VCD format instead of FST format, edit tb.v to use `$dumpfile("tb.vcd");` and then run:

```sh
make -C test -B FST= USE_DIVIDER_MODEL=1
```

This will generate `tb.vcd` instead of `tb.fst`.

## PEm core functional coverage

Coverage is optional and disabled during ordinary `make` runs. Enable it through
the shared runner:

```sh
make -C test coverage USE_DIVIDER_MODEL=1
```

For the core alone:

```sh
make -C test/block-level-tests/L4-PEm-core coverage USE_DIVIDER_MODEL=1
```

These targets set `FUNCTIONAL_COVERAGE=1`. This option deliberately avoids
cocotb's reserved `COVERAGE` variable, which requests Python code coverage.
The functional collector uses the Python standard library and works with the
existing cocotb/Icarus setup.

All tests and coverage helpers remain in `L4-PEm-core/test_pem_core.py`. The
collector samples existing public-port events, CSR checks, and instruction
completion helpers. It adds no DUT cycles or internal signal access. Observations
from a test count only after that test passes; a failed test's observations are
discarded. Reports are rewritten after each test, so filtered and failed runs
show their own partial results. There is no coverage percentage failure threshold.

The report tracks 186 selected functional bins:

| Group | Bins |
|---|---:|
| Instruction families including NOP | 10 |
| Families with reserved encodings | 7 |
| CSR addresses read | 16 |
| Legal accepted/rejected CSR write outcomes | 28 |
| STEP, continuous run, halt, and three reset sources | 6 |
| Running/halted/invalid and active/frozen stall causes | 11 |
| Link transfers, diagnostics, autopull, and autopush | 6 |
| Sync set, clear, and WAIT-clear events | 3 |
| JMP predicates | 8 |
| WAIT polarity/source/relative modes | 6 |
| MOV sources, destinations, and transforms | 16 |
| SET destinations | 6 |
| IN/OUT direction crossed with counts 1-8 | 32 |
| ISR/OSR count values 0-8 | 18 |
| PUSH/PULL conditional and blocking modes | 8 |
| Zero/nonzero delay and side-set counts 0/1/2 | 5 |

This measures the selected scenarios exercised by passing black-box tests.
This functional target does not measure RTL line, branch, toggle, or FSM coverage. A 100% bin result
does not establish exhaustive instruction combinations or correctness of the
external divider, FIFO, sync, or mapping blocks.

Reports are generated under the selected core build:

```text
test/block-level-tests/L4-PEm-core/sim_build/divider_model_0/coverage/
    coverage.html
    coverage.txt
    coverage.json
```

With the actual divider, the directory is `sim_build/rtl_0/coverage/`; other
CORE_ID values replace the trailing zero. HTML and text show covered/missing
bins; JSON also includes individual hit counts and passing/failing test names.
Generated reports are ignored by Git. A run with coverage disabled leaves prior
reports untouched; use `clean` for a fresh selected build. A filtered run replaces
the report, so use the full suite to obtain the regression's coverage result.

The push workflow uses the shared coverage target with the explicit divider
model, includes block XML results in its summary, and uploads coverage reports
in the `test-results` artifact. UART and the top-level test run normally; the
functional collector currently covers only the PEm core.

On November 25, 2026, the shared runner passed 82 tests, with zero failures and
one skipped UART parity test in its default parity-disabled configuration.
All 71 core tests passed for each CORE_ID 0-3, and each ID covered 186/186 bins.
These runs used the explicit test-only divider.

Local warm runs with Icarus 12.0 and cocotb 2.0.1
averaged 23.88 seconds without coverage and 25.12 seconds with it: about
1.24 seconds (5.2%) extra for the shared suite. These are two samples per mode,
run sequentially with the same seed; compilation was excluded by using existing
builds. Runtime varies by machine. The simulation cycle count is unchanged.

## How to view the waveform file

Using GTKWave

```sh
gtkwave test/tb.fst test/tb.gtkw
```

Using Surfer

```sh
surfer test/tb.fst
```

