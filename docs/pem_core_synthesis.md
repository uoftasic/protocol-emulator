# PEm core synthesis and diagrams

Run from the repository root with Python 3, Yosys, Graphviz, and Make available:

```bash
make -C test/block-level-tests/L4-PEm-core synth
```

Use `CORE_ID=1`, `2`, or `3` for another instance. Override tool paths with
`YOSYS=/path/to/yosys`, `DOT=/path/to/dot`, and `PYTHON=/path/to/python3`.
The target runs independently of cocotb. On Ubuntu, the system packages are
`yosys`, `graphviz`, and `make`.

For this workspace, locally extracted tools are available under `/tmp`:

```bash
PATH=/tmp/pem-core-synth-tools/bin:/tmp/pem-core-test-tools/bin:$PATH \
make -C test/block-level-tests/L4-PEm-core synth
```

Those temporary tools may need to be reinstalled after the temporary directory
is cleared. The helper is `test/block-level-tests/L4-PEm-core/synth_pem_core.py`.

Outputs are in ignored `test/block-level-tests/L4-PEm-core/sim_build/synth_0/`
(or the selected CORE_ID):

| File | Contents |
|---|---|
| `pem_core_blocks.png` / `.svg` | Compact view of actual Yosys RTL-netlist connections, with cells grouped by their source section. Counts at arrow tips are distinct incoming connected net bits; two-way links have a count at each end. Common clock fanout is summarized. Groups and positions are display annotations, not RTL module or physical placement boundaries. |
| `pem_core_fsm.png` / `.svg` | FSM extracted by Yosys with the original enum encoding. Edge conditions are simplified using the actual control equations and a complete truth table; the header defines its predicates. Exact original cubes remain in KISS2/JSON. |
| `pem_core_schematic.png` / `.svg` | Full Yosys register/operator schematic. The PNG has a size limit; use SVG to zoom into the complete circuit. |
| `pem_core_fsm.kiss2`, `fsm_transitions.json` | Machine-readable extracted states, input/output patterns, and control-input definitions. KISS2 vectors list the highest input index first. |
| `pem_core_rtl.json`, `block_connections.json` | Original lowered RTL netlist and all connections represented in the grouped view. |
| `pem_core_synth.v`, `pem_core_synth.json`, `synth_stat.txt` | Generic synthesized gate netlist and cell statistics. |
| `synth.ys`, `synth.log`, `manifest.json` | Exact Yosys commands, full log, source hash, CORE_ID, divider mode, tool version, and result summary. |

The empty divider source is represented by a black box with the approved
`clk`, `rst`, `i_enable`, `divisor[15:0]`, and `clk_en` ports. The core drives
`i_enable` while execution is active or a STEP/run start is allowed. The divider
output remains an unknown external source during synthesis, preserving the execution logic. When the
owner supplies a nonempty divider, the script reads that module automatically.

Yosys can skip automatic FSM extraction when recoding offers no benefit. The
script saves the original design, marks `state_q` for extraction in a separate
analysis copy, and exports that FSM without recoding. It then restores the
original design for schematic generation and synthesis. The chart removes
impossible combinations of dependent control inputs, such as simultaneous
RESET=0 and ACTIVE=0, before merging equivalent transition conditions. Both
reset sources are checked for equivalent effects in that simplification.
The chart states the universal RESET-to-FETCH transition once in its header;
drawn edges assume RESET is inactive. Complete conditions, including reset,
remain in JSON and KISS2.
IRQ set-and-wait uses
the separate `irq_set_issued` register, so its issue/acknowledgement phases do
not appear as extra sequencer states.

This is synthesis to generic gates. A target standard-cell library, physical
area, timing, placement, and actual-divider behavior are subsequent checks.

On 2026-11-25, Yosys 0.33 synthesized CORE_ID=0 successfully: zero structural
problems, 516 coarse RTL cells, and 2,355 generic cells including one divider
black box. The extracted sequencer has five states and 25 original transition
cubes. The explicit analysis-only FSM marking produces an advisory about
potential circuit enlargement if recoded; this flow does not recode it.

Yosys command references:

- `help show`
- `help fsm`
- `help fsm_export`
- `help synth`

