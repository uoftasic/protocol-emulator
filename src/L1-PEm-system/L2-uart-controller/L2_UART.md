# L2: UART controller

`uart_controller` ([uart-controller.sv](uart-controller.sv)) is a
parameterized UART transmitter and receiver in a single clock domain. It is
not connected to the top level (`src/project.v`) yet; it will be wired in
through `L1-PEm-system`.

## Frame format

Frames are sent LSB first on a line that idles high:

```
start (0) | NUM_DATA_BITS data | [parity] | NUM_STOP_BITS stop (1)
```

Parity, when enabled, is even parity. Every symbol lasts `CLK_PER_BIT` clock
cycles.

## Parameters

| Parameter       | Default      | Description |
| --------------- | ------------ | ----------- |
| `CLK_PER_BIT`   | 434          | Clock cycles per bit (clock frequency / baud rate). Must be at least 2. |
| `DATA_WIDTH`    | 8            | Width of `i_tx_data` and `o_rx_data`. |
| `NUM_DATA_BITS` | `DATA_WIDTH` | Data bits per frame; the low `NUM_DATA_BITS` bits of the data ports are used. Must not exceed `DATA_WIDTH`. |
| `NUM_STOP_BITS` | 1            | 1 or 2. |
| `PARITY_EN`     | 0            | 1 adds an even parity bit after the data. |

Invalid combinations stop elaboration with `Invalid UART parameters`.

## Ports

| Port         | Dir | Width        | Description |
| ------------ | --- | ------------ | ----------- |
| `clk`        | in  | 1            | Clock. |
| `rst`        | in  | 1            | Active-high synchronous reset. |
| `ares`       | in  | 1            | Active-high asynchronous reset. |
| `i_uart_rx`  | in  | 1            | Serial RX line. |
| `o_uart_tx`  | out | 1            | Serial TX line (idles high, high during reset). |
| `i_tx_data`  | in  | `DATA_WIDTH` | Byte to transmit, sampled when a request is accepted. |
| `i_tx_en`    | in  | 1            | Transmit request; accepted on any clock where `o_tx_busy` is low. |
| `o_tx_busy`  | out | 1            | High from the clock after a request is accepted until the last stop bit ends. |
| `o_rx_data`  | out | `DATA_WIDTH` | Last valid byte received. Holds its value until the next valid frame. |
| `o_rx_ready` | out | 1            | One-clock pulse when `o_rx_data` has been updated. |

## Behaviour

**Transmit.** When `i_tx_en` is high and `o_tx_busy` is low, the controller
latches `i_tx_data`, drives the start bit on the next clock, and raises
`o_tx_busy`. Requests made while busy are dropped, not queued, so a caller
that needs every byte sent must wait for `o_tx_busy` to fall (or put a FIFO
in front of the controller).

**Receive.** A falling edge on `i_uart_rx` starts a frame. The line is checked
again half a bit later; if it is high again the edge is treated as a glitch
and ignored. Every following bit is sampled at its middle. At the middle of
the last stop bit:

- if the stop bit is 1 and parity (when enabled) matched, `o_rx_data` is
  updated and `o_rx_ready` pulses for one clock;
- otherwise the frame is discarded and `o_rx_data` keeps its old value.

The receiver is ready for the next start bit straight after that sample, so
frames sent back to back with no idle time are all received.

**Reset.** `ares` (asynchronous) and `rst` (synchronous) both return TX to
idle-high, abort any frame in progress, and clear `o_rx_data`.

## Tests

Block-level cocotb tests are in
[test/block-level-tests/L2-uart-controller/](../../../test/block-level-tests/L2-uart-controller/).
They drive `uart_controller` directly with `CLK_PER_BIT=16` to keep the
simulation fast.

```sh
cd test/block-level-tests/L2-uart-controller
make                               # 8N1 (default)
make PARITY_EN=1 NUM_STOP_BITS=2   # 8E2
make NUM_DATA_BITS=7 PARITY_EN=1   # 7E1
```

| Test | Checks |
| ---- | ------ |
| `test_reset` | TX idles high, RX outputs are 0. |
| `test_tx_frame_timing` | Every symbol holds for exactly `CLK_PER_BIT` clocks. |
| `test_tx_busy` | `o_tx_busy` lasts exactly one frame; a request on the first idle clock is accepted. |
| `test_tx_frames` | Several payloads are transmitted correctly. |
| `test_tx_ignores_enable_while_busy` | A request during a frame is dropped, not queued or merged. |
| `test_rx_frames` | Several payloads are received; one `o_rx_ready` pulse each. |
| `test_rx_rejects_bad_stop_bit` | A frame with a 0 stop bit is discarded; RX recovers. |
| `test_rx_rejects_bad_parity` | A frame with wrong parity is discarded (parity builds only). |
| `test_rx_ignores_short_glitch` | A low pulse shorter than half a bit is not a frame. |
| `test_loopback_random` | TX wired to RX: 20 random bytes all come back in order. |
| `test_async_reset_mid_frame` | `ares` idles TX immediately and aborts a partial RX frame. |

## Future verification (UVM)

A reusable UVM environment should instantiate `uart_controller` directly
and provide:

1. A UART interface with `clk`, reset, RX, TX, transmit request/data, busy,
   receive data, and ready signals.
2. A sequencer/driver that sends randomized `i_tx_en` transactions,
   including back-to-back requests and requests while TX is busy.
3. A UART line monitor that reconstructs frames from `o_uart_tx`, checks
   start, data, parity, and stop bits, and publishes observed frames.
4. A UART line driver and monitor for RX stimulus, including baud-rate
   tolerance, false starts, bad parity, and invalid stop bits.
5. A scoreboard comparing transmitted and received transactions, and a
   coverage model crossing data width, stop-bit count, parity mode, payload
   patterns, and reset timing.
6. Assertions for idle-high TX, stable symbols for `CLK_PER_BIT` clocks,
   one-cycle `o_rx_ready`, ignored TX requests while busy, and no
   `o_rx_ready` for malformed frames.

Run the same sequences against several parameter sets (for example 7E1, 8N1
and 8E2) using a reference frame model in the scoreboard.
