# SPI Controller (L2)

## 1. Overview

`pem_link` is the SPI link controller between the PEm and the host MCU. The PEm is the SPI master; the MCU uses one or two SPI slaves with DMA ring buffers. Inside the module, each of the N state machines (SMs) owns one RX FIFO and one TX FIFO; the module distributes bytes received from the MCU to the SMs and packs SM output back to the MCU.

**Direction naming.** Named from the PEm's point of view. RX = MCU → PEm → SM, carried on MISO (the PEm receives from the MCU). TX = SM → PEm → MCU, carried on MOSI (the PEm transmits to the MCU). The PEm is master, so MISO is the MCU-driven line.

**Two operating modes**, selected by `mode` and latched on the rising edge of `link_en`:

| Mode | Use case | Clocks | RX FIFO full handling |
| --- | --- | --- | --- |
| 0 — Predictive | Several SMs run concurrently; MCU firmware predicts FIFO headroom from ACKs | CLK1 and CLK2 both free-running and identical | Drop the rest of the current record, send NACK |
| 1 — Pull | A single protocol, including Ethernet and high-speed SPI slave | CLK2 free-running; CLK1 runs only when a byte can be accepted | Pause CLK1 until space is available; no data loss |

## 2. Parameters and I/O

The module has four external pins (CLK1, MISO, CLK2, MOSI: three outputs, one input) and no chip select. Everything else is internal to the PEm: valid/ready byte streams to the SMs and flat `cfg_*` / `st_*` ports to the CSR block. Per-SM buses are packed: SM *s* occupies bits `[8s+7:8s]` of an 8N-bit bus, bit `[s]` of an N-bit bus, and `[4s+3:4s]` of a 4N-bit bus.

### 2.1 Parameters

| Parameter | Default | Range | Description |
| --- | --- | --- | --- |
| `N` | 4 | 1–4 | Number of SMs served by the link. The CSR supports up to 15 SMs, but the record header carries only a 2-bit SM index, so synthesis asserts `N <= 4`. SM index *s* = CSR bank number − 1. Per-SM FIFOs and link state are instantiated with `generate` over N. |
| FIFO depth | 8 (fixed) | — | Depth of every RX and TX FIFO, in bytes. |

### 2.2 System

| Port | Dir | Width | Description |
| --- | --- | --- | --- |
| `clk` | in | 1 | System clock; same clock domain as the SMs. Default 50 MHz. |
| `rst_n` | in | 1 | Asynchronous reset, active low. |

### 2.3 MCU side (SPI pins)

| Port | Dir | Width | Description |
| --- | --- | --- | --- |
| `sclk_rx` | out | 1 | CLK1, the clock for MISO. May be paused (held low) in mode 1. |
| `miso` | in | 1 | Serial data MCU → PEm (RX direction). |
| `sclk_tx` | out | 1 | CLK2, the clock for MOSI. In mode 0 it is identical to `sclk_rx`. |
| `mosi` | out | 1 | Serial data PEm → MCU (TX direction). |

### 2.4 PEm core side (SM interface)

| Port | Dir | Width | Description |
| --- | --- | --- | --- |
| `rx_data` | out | 8N | Head byte of RX FIFO[s]. |
| `rx_valid` | out | N | RX FIFO[s] is non-empty. |
| `rx_ready` | in | N | SM *s* pulls one byte this cycle. A transfer is `rx_valid[s] && rx_ready[s]`. |
| `tx_data` | in | 8N | Byte pushed by SM *s* into TX FIFO[s]. |
| `tx_valid` | in | N | SM *s* presents a byte this cycle. |
| `tx_ready` | out | N | TX FIFO[s] is not full. A transfer is `tx_valid[s] && tx_ready[s]`. |

Both directions use a standard valid/ready handshake, one byte per transfer, no high/low byte split.

### 2.5 CSR side — configuration inputs

CSR addresses and register packing are not defined yet; only signal semantics are. Defaults listed are the expected CSR reset values.

`cfg_mode`, `cfg_sclk_sel` and `cfg_miso_dly` change bit timing, so they are latched on the rising edge of `link_en`. The other fields are thresholds compared against counters; they are not latched, and a change takes effect from the next comparison. Changing them while running cannot misalign the link.

| Port | Dir | Width | Default | Description |
| --- | --- | --- | --- | --- |
| `cfg_link_en` | in | 1 | 0 | Link enable. See Start-up and shutdown. |
| `cfg_mode` | in | 1 | 0 | 0 = predictive, 1 = pull. Latched when `link_en` rises; changes while running are ignored. |
| `cfg_sclk_sel` | in | 2 | 0 | SCLK divider: 0 = ÷4, 1 = ÷8, 2 = ÷16, 3 = ÷32. Latched when `link_en` rises. |
| `cfg_miso_dly` | in | 2 | 2 | MISO sample delay *k*: MISO is sampled *k* `clk` cycles after each CLK1 falling edge. Value = *k* (1–3); 0 is treated as 1. Latched when `link_en` rises. See SPI framing and clocking. |
| `cfg_tx_batch` | in | 4 | 4 | TX batch threshold, 1–8 bytes. 0 is treated as 1; values above 8 as 8. |
| `cfg_tx_age` | in | 8 | 16 | Maximum time data may wait in TX FIFO[s], in MOSI byte times: once the oldest byte has waited this long, a record is sent even if the FIFO holds fewer than `tx_batch` bytes. 0 = send as soon as the FIFO is non-empty. |
| `cfg_ack_every` | in | 8N | 4 per SM | ACK byte-count threshold per SM. 0 = this SM sends no ACK at all (neither count- nor timeout-triggered). 32 is recommended for fixed-rate protocols. |
| `cfg_ack_timeout` | in | 8N | 32 per SM | ACK timeout per SM, in MOSI byte times, counted from the last ACK sent for this SM; 0 means 256. 256 is recommended for fixed-rate protocols. |
| `sm_rst` | in | N | — | Per-SM link reset. Driven by both the CSR per-SM reset and the global soft reset. See Per-SM reset. |
| `clr_hdr_err` | in | 1 | — | Single-cycle pulse; clears `st_hdr_err`. |
| `clr_nack` | in | N | — | Single-cycle pulse per SM; clears `st_nack_cnt[s]`. |

### 2.6 CSR side — status outputs

| Port | Dir | Width | Reset | Description |
| --- | --- | --- | --- | --- |
| `st_hdr_err` | out | 8 | 0 | MISO format-error count. Saturates at 255; does not wrap. |
| `st_nack_cnt` | out | 8N | 0 | NACKs sent per SM. Saturating. |
| `st_rx_level` | out | 4N | 0 | RX FIFO[s] occupancy, 0–8 bytes. Empty and full are derived from it. |
| `st_tx_level` | out | 4N | 0 | TX FIFO[s] occupancy, 0–8 bytes. |

## **3. Internal blocks**

### 3.1 Responsibilities

| Block | Responsibilities | State owned |
| --- | --- | --- |
| `pem_link_ctrl` | Detect `link_en` rising and falling edges. On rising, latch `mode`, `sclk_sel` and `miso_dly`, and assert `run_req`. On falling, deassert `run_req`, wait for `clk_idle` (byte in flight done, SCLKs stopped), then flush. Merge `sm_rst[s]` and the global flush into `sm_clr[s]`. | `mode_q`, `sclk_sel_q`, `miso_dly_q`, `run_req` |
| `pem_link_clk` | Divider counter (÷4/8/16/32, 50% duty). Drive `sclk_rx` and `sclk_tx` (identical in mode 0). Count bits and bytes for both directions. Generate MISO sample strobes (`miso_dly_q` `clk` after each CLK1 falling edge) and MOSI load/shift strobes. In mode 1, sample `clk1_hold` at each CLK1 byte boundary and hold CLK1 low for that byte if set. When `run_req` falls, finish the current byte, stop both SCLKs and assert `clk_idle`. | Divider count, bit/byte counters, CLK1 run/hold state |
| `pem_link_rx` | MISO deserializer. Record parser (wait-header / SEQ / payload / skip). SEQ vs E[s] compare and E[s] update. `nack_sent` and NACK requests. RX FIFO × N and the SM `rx_*` handshake. FIFO full: drop the rest of the record (mode 0), or predict `clk1_hold` one byte ahead (mode 1). Header-error counter and RX FIFO levels. | E[s], `nack_sent[s]`, parser FSM, RX FIFOs, `st_hdr_err` |
| `pem_link_tx` | TX FIFO × N and the SM `tx_*` handshake, with oldest-byte age for `tx_age`. C[s] from `rx_pop`. ACK pending from `ack_every` / `ack_timeout`; NACK pending from `nack_req`. Scheduler: NACK > ACK > TX record (round-robin, `tx_batch` / `tx_age` eligibility, largest encodable length) > idle. Pad an in-flight record with `0x00` on `sm_clr`. MOSI serializer. NACK counters and TX FIFO levels. | C[s], last-reported C, ACK timers, NACK pending, TX FIFOs, scheduler state, `st_nack_cnt` |

### 3.2 Inter-block signals

| Signal | From → To | Width | Description |
| --- | --- | --- | --- |
| `rx_pop` | rx → tx | N | One-cycle pulse when SM *s* pulls a byte (`rx_valid[s] && rx_ready[s]`). TX increments C[s]. |
| `nack_req` | rx → tx | N | One-cycle pulse requesting a NACK for SM *s*. Issued only when `nack_sent[s]` = 0 (SEQ ahead, or mode 0 FIFO-full drop). TX sets NACK pending. |
| `exp_pos` | rx → tx | 8N | Current E[s]. TX takes it when loading the second NACK byte. |
| `clk1_hold` | rx → clk | 1 | Level: do not clock the next MISO byte. RX predicts it one byte ahead (see Interface rules). Sampled by clk at each CLK1 byte boundary; ignored in mode 0. |
| `miso_smp` | clk → rx | 1 | Sample MISO this `clk`: issued `miso_dly_q` `clk` cycles after each CLK1 falling edge (0 treated as 1). Not issued while CLK1 is held. |
| `miso_last` | clk → rx | 1 | Qualifies `miso_smp`: this sample is the last bit (bit 0) of the byte. |
| `mosi_load` | clk → tx | 1 | MOSI byte boundary: load the next byte. Also the "MOSI byte time" tick for `tx_age` and `ack_timeout`. |
| `mosi_shift` | clk → tx | 1 | SCLK falling edge: shift out the next MOSI bit. |
| `mode_q` | ctrl → clk, rx | 1 | Latched mode. |
| `sclk_sel_q` | ctrl → clk | 2 | Latched divider select. |
| `miso_dly_q` | ctrl → clk | 2 | Latched MISO sample delay, raw CSR value; clk maps 0 to 1. |
| `run_req` | ctrl → clk | 1 | 1 = run the SCLKs. On fall, clk finishes the byte in flight, then stops. |
| `clk_idle` | clk → ctrl, tx | 1 | Both SCLKs stopped low on a byte boundary. ctrl flushes after it; TX drives MOSI = 0 while it is high. Must go low within 2 `clk` of `run_req` rising; ctrl ignores it in the first stop cycle. |
| `sm_clr` | ctrl → rx, tx | N | `sm_rst[s]` OR global flush. Clears that SM's state in both blocks in the same cycle. |

### 3.3 External port assignment

| Block | Top-level ports |
| --- | --- |
| All | `clk`, `rst_n` |
| `pem_link_ctrl` | `cfg_link_en`, `cfg_mode`, `cfg_sclk_sel`, `cfg_miso_dly`, `sm_rst` |
| `pem_link_clk` | `sclk_rx`, `sclk_tx` |
| `pem_link_rx` | `miso`, `rx_data`, `rx_valid`, `rx_ready`, `st_hdr_err`, `clr_hdr_err`, `st_rx_level` |
| `pem_link_tx` | `mosi`, `tx_data`, `tx_valid`, `tx_ready`, `cfg_tx_batch`, `cfg_tx_age`, `cfg_ack_every`, `cfg_ack_timeout`, `st_nack_cnt`, `clr_nack`, `st_tx_level` |

### 3.4 Interface rules

- **NACK set beats clear.** If `nack_req[s]` arrives in the same cycle TX starts sending a NACK for SM *s*, pending stays set. `nack_sent` in RX prevents duplicate requests for one gap.
- **`exp_pos` is sampled late.** TX reads E[s] when it loads the NACK's second byte. E[s] is stable then, because RX drops every record with SEQ ≠ E[s] until the MCU resends.
- **`sm_clr` hits both sides in one cycle.** Otherwise RX could restart at position 0 while TX still reports an old C[s].
- **`clk1_hold` prediction (mode 1).** RX decides one byte ahead. When it samples bit 7 of byte *n*, it sets `clk1_hold` for byte *n*+1 from registered state only (parser state and RX FIFO level), without waiting for the rest of byte *n*:
  - `clk1_hold` = 1 if byte *n*+1 will be a payload byte written to RX FIFO[s], and RX FIFO[s] level, plus 1 if byte *n* is itself written to RX FIFO[s], is ≥ 8.
  - Byte *n*+1 counts as a written payload byte if byte *n* is a SEQ byte, or a payload byte of an accepted record other than its last. For a SEQ byte the SEQ vs E[s] result is not known yet, so the record is assumed accepted.
  - The clk block samples `clk1_hold` at the byte boundary before byte *n*+1, so RX has a whole byte time to compute it.
- **Conservative pauses.** The prediction can pause one byte time when no pause was needed: the SEQ byte belongs to a record that is then dropped, or the SM pulls a byte during byte *n*. Neither loses data, and a pause requires at least 7 bytes in the FIFO, so the SM is never starved.
- **While paused.** RX drives `clk1_hold` = 1 only while the waiting byte is a payload byte written to RX FIFO[s] and RX FIFO[s] is full. CLK1 resumes at the first CLK1 byte boundary where `clk1_hold` = 0; CLK2 and MOSI are unaffected.

## 4. Module Behaviour (applies to both Mode 0 and Mode 1)

### 4.1 SPI framing and clocking

- **Frame format:** SPI mode 0 (CPOL = 0, CPHA = 0), 8-bit bytes, MSB first. No CS: the MCU SPI slave uses a software chip select held permanently asserted, and byte boundaries are counted from the first SCLK rising edge after enable. The selected MCU must support continuous CPHA = 0 transfers without toggling NSS between bytes.
- **SCLK generation:** SCLK = `clk` / divider, 50% duty cycle, divider chosen by `sclk_sel`. At `clk` = 50 MHz: ÷4 = 12.5 MHz, ÷8 = 6.25 MHz, ÷16 = 3.125 MHz, ÷32 = 1.5625 MHz. At ÷4, SCLK is high for 2 `clk` and low for 2 `clk`. The divider counter runs only while `link_en` = 1.
- **MOSI timing:** MOSI changes on the SCLK falling edge. Bit 7 of the first byte is driven before the first rising edge.
- **MISO sampling:** the MCU changes MISO on the CLK1 falling edge; the PEm samples it `miso_dly` = *k* `clk` cycles later (*k* = 1–3, 0 treated as 1). This gives the MCU *k* × `clk` period (20/40/60 ns at 50 MHz) for pad delays, board delay and its own output delay. *k* ≤ 3 always samples before the next falling edge (≥ 4 `clk` away at every divider), so hold time is never at risk. At ÷4, *k* = 2 is the CLK1 rising edge, i.e. standard SPI sampling. The window is absolute time, so slower dividers give no extra setup margin; a *k* that works at ÷4 works at every divider. It is a CSR field rather than a parameter so it can be tuned on silicon once the MCU timing is measured.

### 4.2 Start-up, shutdown and re-alignment

1. After reset, `link_en` = 0: both SCLKs are held low, and MOSI is 0.
2. The MCU sets up its DMA, enables its SPI slave while SCLK is low, writes `mode`, `sclk_sel`, `miso_dly` and the other configuration over UART, and finally sets `link_en` = 1.
3. On the rising edge of `link_en`, the PEm latches `mode`, `sclk_sel` and `miso_dly`, starts the divider from 0 and starts clocking. Both sides count bytes from the first SCLK rising edge.
4. When `link_en` is cleared, the byte in flight completes, then the clocks stop. All FIFOs are flushed, and every SM's link state (E, C, last-reported value, pending flags) is cleared.
   `link_en` is level-sensitive: if it is set again before the stop completes, the PEm first finishes the stop and flush, then restarts and latches the configuration at that point. The MCU must therefore have its SPI slave reset and ready before setting `link_en` again, not merely before the restart it expects.
5. **Re-alignment:** after byte slip (the MCU slave counted too many or too few bits), clear `link_en`, have the MCU reset its SPI slave, then repeat steps 2 and 3. Without CS this is the only re-alignment mechanism.

### 4.3 FIFOs

Each SM has an 8-byte RX FIFO and an 8-byte TX FIFO, 8 bits wide. The SM pulls or pushes one byte per transfer.

When TX FIFO[s] is full, `tx_ready[s]` = 0. An SM using a blocking PUSH stalls; an SM using a non-blocking PUSH drops the byte and records the overflow itself. This module only reports occupancy; it has no FIFO-full interrupt and no sticky overflow bit.

### 4.4 Length code

Both directions use the same 4-bit length code `llll`:

| Code | 0–7 | 8 | 9 | 10 | 11 | 12–15 |
| --- | --- | --- | --- | --- | --- | --- |
| Payload bytes | 1–8 (code + 1) | 12 | 16 | 24 | 32 | Reserved |

### 4.5 MCU → PEm packets (MISO)

MISO carries a stream of data records and idle bytes. A data record is a header, a SEQ byte, then the payload:

| Byte | Content |
| --- | --- |
| Header | `10ss llll`: `ss` = SM index, `llll` = length code |
| SEQ | Position of the first payload byte in SM *s*'s RX byte stream, modulo 256 |
| Payload | L bytes, L given by the length code |

For each SM the parser keeps an 8-bit **expected position E[s]**: the stream position of the next byte it will accept. E[s] resets to 0.

**Parser rules**

1. **Waiting for header:** any byte not matching `10xxxxxx` is discarded. `0x00` is the normal idle byte. Any other non-header value, a reserved length code (12–15), or `ss >= N` is a format error and increments `st_hdr_err`. With a reserved length code only that one byte is discarded. With `ss >= N` the whole record is skipped using its length code. (With N = 4, `ss >= N` cannot occur.)
2. **After SEQ:** compute `diff = (SEQ − E[s]) mod 256`.
    - `diff = 0` — accept: write the payload byte by byte into RX FIFO[s]; E[s] increments on each byte written.
    - `diff = 1–127` — MCU is ahead (bytes were lost): drop the whole record; if no NACK has been raised for this gap yet, set NACK pending for SM *s*.
    - `diff = 128–255` — retransmission of bytes already accepted: drop the whole record silently.
3. **RX FIFO full during an accepted record:** handled differently in each mode (see sections 5 and 6).

### 4.6 PEm → MCU packets (MOSI)

MOSI carries three item types plus the idle byte `0x00`:

| Item | Bytes | Meaning |
| --- | --- | --- |
| NACK | `0xE0 + s`, E[s] | SM *s* lost RX data; the MCU must resend from position E[s] |
| ACK | `0xD0 + s`, C[s] | Cumulative bytes SM *s* has pulled from RX FIFO[s], modulo 256 |
| TX record | `10ss llll` + payload | TX data produced by SM *s*. Same length code as MISO, no SEQ byte |
| Idle | `0x00` | Nothing to send |

**C[s]** is an 8-bit counter that increments on every `rx_valid[s] && rx_ready[s]`. It counts bytes the SM has consumed, not bytes written into the FIFO.

**Scheduler.** At each MOSI byte boundary, if the previous item has finished, the scheduler picks the next item by priority. A selected item is sent to completion without interleaving.

1. **NACK** — among SMs with NACK pending, lowest index first. The second byte is E[s] at the moment it is transmitted.
2. **ACK** — among SMs with ACK pending, lowest index first. The second byte is C[s] at the moment of selection; that value is stored as the SM's *last-reported* value.
3. **TX record** — round-robin among SMs whose TX FIFO is non-empty and *eligible*: the FIFO holds at least `tx_batch` bytes, or its oldest byte has waited `tx_age` MOSI byte times. The record length is the largest encodable length not exceeding the current FIFO occupancy (with an 8-byte FIFO, codes 0–7 only).
4. **Idle** — send `0x00`.

### 4.7 ACK / NACK mechanism

**ACK (flow-control credit).** Evaluated per SM. ACK is pending when either:

- `C[s] − last_reported[s] >= ack_every[s]`, or
- `C[s] − last_reported[s] > 0` and `ack_timeout[s]` MOSI byte times have passed since the last ACK sent for this SM (0 = 256). Before the first ACK, the timer counts from link start or from the last `sm_clr[s]`.

`ack_every[s]` = 0 disables both ACK triggers for that SM. From C[s] the MCU knows how many bytes have left the RX FIFO and therefore how much FIFO space remains.

**NACK (loss recovery).** NACK is kept in both modes.

- NACK pending is set when a record arrives ahead of E[s] (`diff = 1–127`), or when bytes of an accepted record are dropped because the RX FIFO is full (mode 0 only).
- A per-SM bit `nack_sent` limits this to one NACK per gap: it is set together with NACK pending and cleared each time a payload byte is accepted for that SM. While `nack_sent` = 1, further out-of-order records are dropped without raising a new NACK.
- The NACK carries the current E[s]; the MCU resends starting at that position. Records that overlap already accepted bytes are dropped silently, so the stream resynchronizes on the first record whose SEQ equals E[s].
- Each NACK sent increments `st_nack_cnt[s]` (saturating).

## 5. Mode 0 — Predictive

Mode 0 runs several SMs at once and relies on the MCU to avoid RX overflow by predicting FIFO headroom from ACKs; overflow is recovered with NACK and retransmission.

- **Clocks:** `sclk_tx` is an exact copy of `sclk_rx`. Both run continuously while `link_en` = 1, and MISO and MOSI transfer one byte each per byte time, in lock-step.
- **Single-SPI boards:** because the two clocks are identical, an MCU with one SPI slave can use CLK1 for both MISO and MOSI. Mode 0 works with an MCU model that is unaware of the mode and connected to CLK1 only.
- **RX FIFO full:** if RX FIFO[s] is full when a payload byte of an accepted record arrives, that byte and the rest of the record are dropped. NACK pending and `nack_sent` are set for SM *s*. E[s] stays at the first dropped byte, so the NACK tells the MCU exactly where to resume.
- **Edge case:** a record that exactly fills the FIFO to 8 bytes is accepted completely; the next payload byte that finds the FIFO full triggers the drop and the NACK.
- **ACK usage:** the MCU should keep each SM's in-flight bytes (sent minus last ACKed C[s]) at or below 8. `ack_every` / `ack_timeout` set how fresh that credit information is.

## 6. Mode 1 — Pull

Mode 1 runs a single protocol (including Ethernet and high-speed SPI slave) and never loses RX data to a full FIFO: the PEm stalls CLK1 instead.

- **Clocks:** `sclk_tx` (CLK2) runs continuously and clocks MOSI. Before each MISO byte, `sclk_rx` (CLK1) is either run or held low according to the pause rule below. CLK1 stops and restarts only on byte boundaries and stays aligned to CLK2.
- **Pause rule:** CLK1 is held low for a byte time when that byte would be written to a full RX FIFO[s], and stays low until the FIFO has space. RX predicts this one byte ahead (see `clk1_hold` prediction in Interface rules). Headers, SEQ bytes and idle bytes never cause a pause; a record that is dropped can cause at most one pause, before its first payload byte.
- **RX FIFO full:** no data is dropped for FIFO-full reasons. A record that exactly fills the FIFO to 8 bytes pauses CLK1 before the next payload byte.
- **NACK still active:** NACK on sequence gaps (`diff = 1–127`) remains, e.g. after a reset of one side.
- **ACK optional:** the MCU may set `ack_every[s]` = 0 to disable ACKs, since back-pressure is provided by CLK1.
- **Requires two MCU SPI slaves:** one on CLK1/MISO and one on CLK2/MOSI.
- **Halted SM:** SM halt is not an input to this module. A halted SM stops pulling, its FIFO and link state are preserved, and if its RX FIFO fills, CLK1 stays paused indefinitely. To recover, either the SM resumes pulling (CLK1 restarts at the next byte boundary once the FIFO has space), or the MCU writes `sm_rst[s]` over UART, which empties the FIFO and releases the pause (a half-received record is skipped, see Per-SM reset). UART configuration, CLK2 and MOSI are not affected by the pause.
- **Throughput target:** with an SM pulling continuously at 10 Mbit/s and the MCU writing whole frames in advance, RX must not under-run, no NACK must occur, and CLK1 may pause only on byte boundaries.

## 7. Per-SM reset

`sm_rst[s]` resets one SM's link without touching the others or any configuration.

- Flushes RX FIFO[s] and TX FIFO[s].
- Clears E[s], C[s], the last-reported value, and the NACK/ACK pending flags (including `nack_sent`).
- If the MISO parser is in the middle of a record for SM *s* (its SEQ or payload), the rest of that record is still counted by its length code but discarded: nothing is written to RX FIFO[s], no NACK is raised, and `st_hdr_err` does not count. The parser then waits for the next header. In mode 1, a CLK1 pause for that record is released, since RX FIFO[s] is now empty.
- If a TX record for SM *s* is being transmitted on MOSI, the remaining payload bytes are padded with `0x00` so the record length stays valid.
- If an ACK or NACK for SM *s* has sent its first byte but not its second, the second byte is sent as `0x00`. This matches the cleared state: ACK 0 = nothing pulled since reset, NACK 0 = resend from position 0.
- Afterwards the SM's byte stream restarts at position 0, and the MCU restarts its SEQ at 0.
- Both the CSR per-SM reset and the global soft reset must drive `sm_rst`.
- Before loading an imem program that uses a different protocol, assert `sm_rst` for the affected SMs to discard stale data.

## 8. MCU firmware conventions

These do not affect the RTL but are required for correct operation.

- Write each record's payload and SEQ into the DMA ring first, and its header last, so a partially written record is read as idle bytes.
- In mode 1, write all records of an Ethernet frame into the ring before writing the header of the first record; otherwise idle bytes can appear in the middle of the frame and under-run the SM.
- On NACK, resend SM *s* data starting at position E[s].