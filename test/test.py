# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles


@cocotb.test()
async def test_project(dut):
    dut._log.info("Start")

    # The wrapper uses CLK_PER_BIT=434.  A slow clock keeps this smoke test
    # practical while still exercising complete UART symbols.
    clock = Clock(dut.clk, 10, unit="us")
    cocotb.start_soon(clock.start())

    # Reset
    dut._log.info("Reset")
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1

    # ui_in is TX data, uio_in[1] is the one-cycle TX enable, and
    # uio_out[0] is the UART TX line.  Check an 8N1 frame, LSB first.
    value = 0xA5
    dut.ui_in.value = value
    dut.uio_in.value = 0b10
    await ClockCycles(dut.clk, 1)
    dut.uio_in.value = 0

    await ClockCycles(dut.clk, 216)
    assert int(dut.uio_out.value) & 1 == 0  # start bit
    for bit in range(8):
        await ClockCycles(dut.clk, 434)
        assert (int(dut.uio_out.value) & 1) == ((value >> bit) & 1)
    await ClockCycles(dut.clk, 434)
    assert int(dut.uio_out.value) & 1 == 1  # stop bit


# ---------------------------------------------------------------------------
# UART RX / loopback / pin tests for the tt_um_uoftasic_protocol_emulator
# wrapper (8N1, CLK_PER_BIT=434).  uio_in[0] = RX line, uio_in[1] = TX enable.
# ---------------------------------------------------------------------------

CLK_PER_BIT = 434
RX_PIN = 0
TX_EN_PIN = 1
TX_PIN = 0  # uio_out[0]


def set_uio(dut, rx=1, tx_en=0):
    """Drive uio_in, keeping the RX line idle-high unless told otherwise."""
    dut.uio_in.value = (rx << RX_PIN) | (tx_en << TX_EN_PIN)


async def reset(dut):
    clock = Clock(dut.clk, 10, unit="us")
    cocotb.start_soon(clock.start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    set_uio(dut)
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 3)


async def drive_rx_frame(dut, value, stop_bit=1, stop_cycles=CLK_PER_BIT):
    """Drive one 8N1 frame on the RX pin, LSB first, then return to idle."""
    set_uio(dut, rx=0)  # start bit
    await ClockCycles(dut.clk, CLK_PER_BIT)
    for bit in range(8):
        set_uio(dut, rx=(value >> bit) & 1)
        await ClockCycles(dut.clk, CLK_PER_BIT)
    set_uio(dut, rx=stop_bit)
    await ClockCycles(dut.clk, stop_cycles)
    set_uio(dut, rx=1)


@cocotb.test()
async def test_rx_receives_8n1_frames(dut):
    """Bytes driven onto uio_in[0] appear on uo_out after the stop bit."""
    await reset(dut)
    assert int(dut.uo_out.value) == 0, "uo_out should reset to 0"
    for value in (0x55, 0xA5, 0x00, 0xFF, 0x01, 0x80):
        await drive_rx_frame(dut, value)
        await ClockCycles(dut.clk, 2)
        got = int(dut.uo_out.value)
        assert got == value, f"RX: expected 0x{value:02X}, got 0x{got:02X}"


@cocotb.test()
async def test_rx_rejects_bad_stop_bit(dut):
    """A frame whose stop bit is 0 must not update uo_out; RX then recovers."""
    await reset(dut)
    await drive_rx_frame(dut, 0x3C)
    await ClockCycles(dut.clk, 2)
    assert int(dut.uo_out.value) == 0x3C

    # Stop bit held low past the receiver's mid-bit sample, then released
    # before the receiver's re-armed start-bit check (~1 bit later), so the
    # re-triggered start is also rejected as a false start.
    await drive_rx_frame(dut, 0xC3, stop_bit=0, stop_cycles=3 * CLK_PER_BIT // 4)
    await ClockCycles(dut.clk, 3 * CLK_PER_BIT)
    got = int(dut.uo_out.value)
    assert got == 0x3C, f"framing error leaked to uo_out: 0x{got:02X}"

    await drive_rx_frame(dut, 0x96)
    await ClockCycles(dut.clk, 2)
    assert int(dut.uo_out.value) == 0x96, "RX did not recover after framing error"


@cocotb.test()
async def test_rx_ignores_short_glitch(dut):
    """A low pulse shorter than half a bit is a false start, not a frame."""
    await reset(dut)
    set_uio(dut, rx=0)
    await ClockCycles(dut.clk, CLK_PER_BIT // 4)
    set_uio(dut, rx=1)
    await ClockCycles(dut.clk, 12 * CLK_PER_BIT)
    assert int(dut.uo_out.value) == 0, "glitch produced a received byte"

    await drive_rx_frame(dut, 0x5A)
    await ClockCycles(dut.clk, 2)
    assert int(dut.uo_out.value) == 0x5A


async def sample_tx_frame(dut):
    """Wait for a start bit on uio_out[0] and return the 8-bit payload.

    Samples mid-bit and checks the stop bit, like a real UART receiver.
    """
    while int(dut.uio_out.value) & (1 << TX_PIN):
        await ClockCycles(dut.clk, 1)
    await ClockCycles(dut.clk, CLK_PER_BIT // 2)
    assert int(dut.uio_out.value) & 1 == 0, "start bit not stable"
    value = 0
    for bit in range(8):
        await ClockCycles(dut.clk, CLK_PER_BIT)
        value |= (int(dut.uio_out.value) & 1) << bit
    await ClockCycles(dut.clk, CLK_PER_BIT)
    assert int(dut.uio_out.value) & 1 == 1, "stop bit missing"
    return value


@cocotb.test()
async def test_tx_ignores_enable_while_busy(dut):
    """A TX request during an active frame is dropped, not queued or merged."""
    await reset(dut)
    dut.ui_in.value = 0xA5
    set_uio(dut, tx_en=1)
    await ClockCycles(dut.clk, 1)
    set_uio(dut)
    monitor = cocotb.start_soon(sample_tx_frame(dut))

    # Mid-frame request with different data.
    await ClockCycles(dut.clk, 4 * CLK_PER_BIT)
    dut.ui_in.value = 0xFF
    set_uio(dut, tx_en=1)
    await ClockCycles(dut.clk, 1)
    set_uio(dut)

    got = await monitor
    assert got == 0xA5, f"TX frame corrupted by busy request: 0x{got:02X}"
    # Line must stay idle: the busy request was not queued.
    for _ in range(12 * CLK_PER_BIT):
        await ClockCycles(dut.clk, 1)
        assert int(dut.uio_out.value) & 1 == 1, "dropped request was transmitted"


@cocotb.test()
async def test_tx_rx_loopback(dut):
    """TX output wired back to RX input: every byte sent is received."""
    await reset(dut)

    # The loopback coroutine is the only writer of uio_in: two coroutines
    # read-modify-writing it on the same edge race (cocotb defers writes),
    # which silently drops the TX-enable pulse.
    # A send request is a one-shot flag consumed by the writer, so the
    # TX-enable pulse is exactly one clock regardless of coroutine order.
    tx_req = [0]

    async def loopback():
        while True:
            await ClockCycles(dut.clk, 1)
            set_uio(dut, rx=int(dut.uio_out.value) & 1, tx_en=tx_req[0])
            tx_req[0] = 0

    cocotb.start_soon(loopback())
    for value in (0x12, 0xED, 0x7E, 0x81):
        dut.ui_in.value = value
        tx_req[0] = 1
        await ClockCycles(dut.clk, 10 * CLK_PER_BIT + 8)
        got = int(dut.uo_out.value)
        assert got == value, f"loopback: sent 0x{value:02X}, got 0x{got:02X}"


@cocotb.test()
async def test_uio_pin_directions(dut):
    """Pins used as inputs (RX, TX enable) must not be driven as outputs.

    On silicon, uio_oe[n]=1 makes the pad an output; uio_in[n] then reads
    back our own uio_out[n] and an external driver would fight it.
    """
    await reset(dut)
    oe = int(dut.uio_oe.value)
    for name, pin in (("UART RX", RX_PIN), ("TX enable", TX_EN_PIN)):
        assert not (oe >> pin) & 1, (
            f"uio[{pin}] is read as {name} but uio_oe[{pin}]=1 drives it as an output"
        )
    assert oe & (1 << TX_PIN), f"UART TX pin uio[{TX_PIN}] is not enabled as an output"
