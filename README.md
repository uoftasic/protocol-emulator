![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# Tiny Tapeout Verilog Project Template

## UART controller

The design contains a parameterized `uart_controller` with LSB-first UART
frames, configurable data width, one or two stop bits, and optional even
parity. `i_tx_en` and `o_rx_ready` are one-clock events. The Tiny Tapeout
adapter is configured for 8N1 at 434 clocks per bit:

- `ui_in[7:0]`: transmit data
- `uio_in[0]`: UART RX
- `uio_in[1]`: one-cycle transmit enable
- `uio_out[0]`: UART TX
- `uo_out[7:0]`: received data

### Scalable UVM verification

A reusable UVM environment should instantiate the `uart_controller` directly
and provide:

1. A UART interface with `clk`, reset, RX, TX, transmit request/data, receive
   data, and ready signals.
2. A programmer sequencer/driver that sends randomized `i_tx_en` transactions,
   including back-to-back requests and requests while TX is active.
3. A UART line monitor that reconstructs frames from `o_uart_tx`, checks start,
   data, parity, and stop bits, and publishes observed frames.
4. A UART line driver and monitor for RX stimulus, including baud-rate
   tolerance, false starts, bad parity, and invalid stop bits.
5. A scoreboard that compares transmitted and received transactions and a
   coverage model crossing data width, stop-bit count, parity mode, payload
   patterns, and reset timing.
6. Assertions for idle-high TX, stable symbols for `CLK_PER_BIT` clocks,
   one-cycle `o_rx_ready`, ignored TX requests while busy, and no
   `o_rx_ready` for malformed frames.

Run the same sequence against multiple parameter sets (for example 7E1,
8N1, and 8O2) and use a reference UART frame model in the scoreboard rather
than hard-coding the 8N1 wrapper configuration.

- [Read the documentation for project](docs/info.md)

## What is Tiny Tapeout?

Tiny Tapeout is an educational project that aims to make it easier and cheaper than ever to get your digital and analog designs manufactured on a real chip.

To learn more and get started, visit https://tinytapeout.com.

## Set up your Verilog project

1. Add your Verilog files to the `src` folder.
2. Edit the [info.yaml](info.yaml) and update information about your project, paying special attention to the `source_files` and `top_module` properties. If you are upgrading an existing Tiny Tapeout project, check out our [online info.yaml migration tool](https://tinytapeout.github.io/tt-yaml-upgrade-tool/).
3. Edit [docs/info.md](docs/info.md) and add a description of your project.
4. Adapt the testbench to your design. See [test/README.md](test/README.md) for more information.

The GitHub action will automatically build the ASIC files using [LibreLane](https://www.zerotoasiccourse.com/terminology/librelane/).

## Enable GitHub actions to build the results page

- [Enabling GitHub Pages](https://tinytapeout.com/faq/#my-github-action-is-failing-on-the-pages-part)

## Resources

- [FAQ](https://tinytapeout.com/faq/)
- [Digital design lessons](https://tinytapeout.com/digital_design/)
- [Learn how semiconductors work](https://tinytapeout.com/siliwiz/)
- [Join the community](https://tinytapeout.com/discord)
- [Build your design locally](https://www.tinytapeout.com/guides/local-hardening/)

## What next?

- [Submit your design to the next shuttle](https://app.tinytapeout.com/).
- Edit [this README](README.md) and explain your design, how it works, and how to test it.
- Share your project on your social network of choice:
  - LinkedIn [#tinytapeout](https://www.linkedin.com/search/results/content/?keywords=%23tinytapeout) [@TinyTapeout](https://www.linkedin.com/company/100708654/)
  - Mastodon [#tinytapeout](https://chaos.social/tags/tinytapeout) [@matthewvenn](https://chaos.social/@matthewvenn)
  - X (formerly Twitter) [#tinytapeout](https://twitter.com/hashtag/tinytapeout) [@tinytapeout](https://twitter.com/tinytapeout)
  - Bluesky [@tinytapeout.com](https://bsky.app/profile/tinytapeout.com)
