# PEm core waveform decoding

The project includes decoded ASCII traces, desktop Surfer formats, and simulation-only probes.
The instruction trace shows the latched word as a mnemonic and operands,
alongside its PC, execution phase, commit qualifier, and individual pin levels.
The decoder reads instruction words; it does not assemble programs.

## Generate and open a waveform

Run this command from the repository root with Python 3.11+, cocotb, and Icarus
available on your PATH:

```sh
make -B -C test/block-level-tests/L4-PEm-core sim \
    USE_DIVIDER_MODEL=1 WAVES=1 \
    COCOTB_TEST_FILTER=test_integer_divider_spi_clock_data_and_sample_timing \
    COCOTB_RESULTS_FILE=sim_build/divider_model_0/spi-wave-results.xml
```

`USE_DIVIDER_MODEL=1` explicitly selects the test-only divider while the owner's
RTL is empty. `-B` rebuilds the waveform instrumentation. The selected test runs
its divisor/data cases sequentially in the same waveform.

The waveform path, relative to the repository root, is:

```text
test/block-level-tests/L4-PEm-core/sim_build/divider_model_0/pem_core.fst
```

### VS Code Surfer extension

Open the generated `pem_core.fst` from VS Code's Explorer. If another viewer
handles it, use **Open With... -> Surfer Waveform Viewer**.

1. Add `pem_core_wave.instruction_text` and `pem_core_wave.phase_text`.
2. Right-click each row and select the built-in **ASCII** format.
3. Add `pem_core_wave.clk`, `clk_en`, `instruction_pc`, and `commit`.
4. Add the desired `vioN_out`, `vioN_in`, or `vioN_oe` traces.

The extension uses Surfer's browser build, which does not discover local
`.surfer/decoders` TOML files. Text is therefore embedded in the waveform as
packed ASCII vectors. It is generated from the same TOML instruction tables
used by desktop Surfer, so both paths share the opcode/operand definitions.
Rebuild and regenerate older waveforms to obtain the text traces.

If your extension exposes **File -> Open command file**, load
`.surfer/pem_core_vscode.sucl` with an empty signal panel. It adds the text traces
with ASCII formatting, timing qualifiers, and all eight virtual output lanes.
The menu is version-dependent; manual signal selection also works.

### Desktop Surfer

With the desktop executable available on your PATH, run from the repo root:

```sh
surfer test/block-level-tests/L4-PEm-core/sim_build/divider_model_0/pem_core.fst \
    --command-file .surfer/pem_core.sucl
```

This command file starts with an empty signal panel and applies the native
formats found in `.surfer/decoders`. The ASCII traces also work in desktop
Surfer by selecting the ASCII format.

For a longer program covering the entire ISA, replace the filter with
`test_continuous_full_isa_sanity_during_host_wait`. For UART, use
`test_integer_divider_uart_frame_has_exact_bit_widths`.

## Decode an existing waveform in desktop Surfer

The `PEm` format also works with older core waveforms that lack the new probes:

1. Add `pem_core.instruction` to the signal panel.
2. Right-click its row and select the `PEm` format.
3. Add `pem_core.instruction_pc`, `pem_core.state_q`,
   `pem_core.instruction_commit`, `pem_core.clk`, and `pem_core.clk_en`.
4. Select `PEm-Phase` for `state_q` to display the five state names.

Decode the latched `instruction` to follow retained work. `i_imem_din` is memory
data at the current fetch address, which can differ from the executing word.

Examples of decoded labels:

```text
SET X, 7 [F=0x0]
OUT PINS, 1 [F=0x13]
JMP X--, 4 [F=0x0]
WAIT 1 IRQ 2 REL [F=0x0]
NOP [F=0x0]
RESERVED -> NOP (0xa018)
```

All eight opcode families are supported, including count-zero meaning eight
bits, PUSH/PULL conditions and blocking modes, MOV transforms, relative IRQ
indices, and the MOV Y,Y NOP alias. Reserved operand forms display their raw word
and NOP behavior. Their ordinary delay/side-set effects still apply.

## Read instruction timing

The `pem_core_wave` scope groups read-only aliases of the existing core signals:

| Trace | Meaning |
|---|---|
| `instruction_text`, `phase_text` | Decoded ASCII labels; select ASCII in the VS Code extension or desktop viewer. |
| `instruction`, `instruction_pc` | Latched instruction and its original address. |
| `phase` | FETCH, EXECUTE, DELAY_1, DELAY_2, or FROZEN. |
| `retained` | The core still retains active, delayed, or frozen work. When low, the instruction row holds an old/reset word. |
| `fetch` | Fetch qualifier; capture occurs at a rising system-clock edge. |
| `commit` | Commit qualifier; architectural effects occur at a rising system-clock edge. |
| `stalled` | Current retained instruction is blocked on a pin, IRQ, or Link endpoint. |
| `invalid` | The latched instruction has reserved operands; this is not the sticky status CSR bit. |
| `delay` | Effective encoded delay after applying the configured side-set width. |
| `delay_remaining` | Delay counter, meaningful during DELAY_1/DELAY_2. |
| `sideset_count`, `sideset_bits`, `sideset_base` | Configured width, unshifted instruction side-set value, and first lane. |
| `in_base`, `out_base` | Current starts of the instruction pin vectors. |

Read `fetch` and `commit` immediately before rising `clk` edges. These are
combinational qualifiers, so their visible width depends on enable/readiness;
a wide label or qualifier does not mean repeated execution. A retained
instruction remains visible through stalls and delay. Repeated execution of
the same word may not change its label; the fetch/commit rows distinguish each
execution.

`F` in the decoded text is the raw five-bit common field, not always the delay.
The decoder translates one bus and cannot read the PIN_CTRL CSR. The runtime
probes give its configured interpretation:

| Side-set count | Delay bits | Side-set bits |
|---:|---|---|
| 0 | word[12:8] | None |
| 1 | word[11:8] | word[12] |
| 2 | word[10:8] | word[12:11] |

Pin effects occur at commit, before the encoded delay. Side-set overrides
overlapping pin-level writes at that same commit. While stalled, the instruction
label remains visible and pins retain their prior values.

## Probe individual pins

In `pem_core_wave`, add any `vioN_in`, `vioN_out`, or `vioN_oe` signal for N=0-7.
Each is a separate one-bit trace. `vioN_out` is the core's output latch level;
`vioN_oe=1` enables driving that lane. With output enable low, the latch level
does not describe the external pin level. Physical GPIO mapping is outside
this block.

Convenience aliases match the existing protocol example programs:

| Alias | Virtual lane |
|---|---|
| `uart_tx` | Output 0 |
| `spi_mosi` | Output 0 |
| `spi_sclk` | Output 1 |
| `spi_cs_n` | Output 2 |
| `spi_miso` | Input 7 |

These names describe the examples' lane assignment. For another program's
mapping, select its corresponding `vioN_*` signals.

The probes are generated into ignored `sim_build` storage only for Icarus
`WAVES=1` builds. They observe signals without driving the core. Regression
assertions continue to use public ports and CSR readback.

