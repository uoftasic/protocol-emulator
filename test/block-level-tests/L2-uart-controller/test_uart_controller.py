"""Block-level tests for uart_controller.

cocotb drives the module ports directly (no Tiny Tapeout wrapper). The frame
format comes from the same env vars the Makefile passes as -P overrides, so
the tests follow whatever configuration was compiled.
"""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer

CLK_PER_BIT = int(os.environ.get("CLK_PER_BIT", 16))
DATA_WIDTH = int(os.environ.get("DATA_WIDTH", 8))
NUM_DATA_BITS = int(os.environ.get("NUM_DATA_BITS", DATA_WIDTH))
NUM_STOP_BITS = int(os.environ.get("NUM_STOP_BITS", 1))
PARITY_EN = int(os.environ.get("PARITY_EN", 0))

DATA_MASK = (1 << NUM_DATA_BITS) - 1
FRAME_BITS = 1 + NUM_DATA_BITS + PARITY_EN + NUM_STOP_BITS


def even_parity(value):
    return bin(value & DATA_MASK).count("1") & 1


def frame_bits(value, parity_flip=False, stop_bit=1):
    """Line levels for one frame, start bit first."""
    bits = [0] + [(value >> i) & 1 for i in range(NUM_DATA_BITS)]
    if PARITY_EN:
        bits.append(even_parity(value) ^ int(parity_flip))
    bits += [stop_bit] * NUM_STOP_BITS
    return bits


async def tick(dut, n=1):
    """Let the DUT act on n rising edges, then return at a safe point."""
    for _ in range(n):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)


async def setup(dut):
    """Start the clock, drive all inputs, apply sync reset."""
    Clock(dut.clk, 10, unit="ns").start()
    dut.i_uart_rx.value = 1
    dut.i_tx_data.value = 0
    dut.i_tx_en.value = 0
    dut.ares.value = 0
    dut.rst.value = 1
    await tick(dut, 2)
    dut.rst.value = 0
    await tick(dut)


async def drive_rx_frame(dut, value, parity_flip=False, stop_bit=1):
    """Drive one frame onto i_uart_rx, then return the line to idle."""
    for bit in frame_bits(value, parity_flip, stop_bit):
        dut.i_uart_rx.value = bit
        await tick(dut, CLK_PER_BIT)
    dut.i_uart_rx.value = 1


async def rx_ready_watch(dut, hits):
    """Record o_rx_data on every o_rx_ready pulse, checking it is one clock."""
    while True:
        await FallingEdge(dut.clk)
        if int(dut.o_rx_ready.value):
            hits.append(int(dut.o_rx_data.value))
            await FallingEdge(dut.clk)
            assert int(dut.o_rx_ready.value) == 0, "o_rx_ready held longer than one clock"


async def send_tx(dut, value):
    """Pulse i_tx_en for one clock with value on i_tx_data."""
    dut.i_tx_data.value = value
    dut.i_tx_en.value = 1
    await tick(dut)
    dut.i_tx_en.value = 0


async def sample_tx_frame(dut):
    """Wait for a start bit on o_uart_tx, sample mid-bit, check framing."""
    while int(dut.o_uart_tx.value):
        await tick(dut)
    await tick(dut, CLK_PER_BIT // 2)
    assert int(dut.o_uart_tx.value) == 0, "start bit not stable"
    value = 0
    for bit in range(NUM_DATA_BITS):
        await tick(dut, CLK_PER_BIT)
        value |= int(dut.o_uart_tx.value) << bit
    if PARITY_EN:
        await tick(dut, CLK_PER_BIT)
        assert int(dut.o_uart_tx.value) == even_parity(value), "bad TX parity"
    for _ in range(NUM_STOP_BITS):
        await tick(dut, CLK_PER_BIT)
        assert int(dut.o_uart_tx.value) == 1, "stop bit missing"
    return value


@cocotb.test()
async def test_reset(dut):
    await setup(dut)
    assert int(dut.o_uart_tx.value) == 1, "TX should idle high"
    assert int(dut.o_rx_ready.value) == 0
    assert int(dut.o_rx_data.value) == 0


@cocotb.test()
async def test_tx_frame_timing(dut):
    """Every symbol of a TX frame holds for exactly CLK_PER_BIT clocks."""
    await setup(dut)
    value = 0xA5 & DATA_MASK
    await send_tx(dut, value)
    # Start bit is driven on the same edge that accepts i_tx_en.
    for i, expected in enumerate(frame_bits(value)):
        for c in range(CLK_PER_BIT):
            got = int(dut.o_uart_tx.value)
            assert got == expected, f"frame bit {i}, clock {c}: expected {expected}, got {got}"
            await tick(dut)
    assert int(dut.o_uart_tx.value) == 1, "line not idle after frame"


@cocotb.test()
async def test_tx_busy(dut):
    """o_tx_busy covers the whole frame, and a request is taken once it drops."""
    await setup(dut)
    assert int(dut.o_tx_busy.value) == 0, "busy while idle"
    await send_tx(dut, 0x5A & DATA_MASK)
    busy_clocks = 0
    while int(dut.o_tx_busy.value):
        busy_clocks += 1
        await tick(dut)
    assert busy_clocks == FRAME_BITS * CLK_PER_BIT, f"busy for {busy_clocks} clocks"
    assert int(dut.o_uart_tx.value) == 1, "line not idle when busy dropped"

    # A request on the first clock busy is low must start a new frame.
    monitor = cocotb.start_soon(sample_tx_frame(dut))
    await send_tx(dut, 0xA5 & DATA_MASK)
    assert int(dut.o_tx_busy.value) == 1, "request right after busy dropped was ignored"
    assert await monitor == 0xA5 & DATA_MASK


@cocotb.test()
async def test_tx_frames(dut):
    await setup(dut)
    for value in (0x55, 0xA5, 0x00, 0xFF, 0x01, 0x80):
        value &= DATA_MASK
        monitor = cocotb.start_soon(sample_tx_frame(dut))
        await send_tx(dut, value)
        got = await monitor
        assert got == value, f"TX: expected 0x{value:02X}, got 0x{got:02X}"
        # Monitor returns mid stop bit; let the frame finish before the next send.
        await tick(dut, CLK_PER_BIT)


@cocotb.test()
async def test_tx_ignores_enable_while_busy(dut):
    """A TX request during an active frame is dropped, not queued or merged."""
    await setup(dut)
    monitor = cocotb.start_soon(sample_tx_frame(dut))
    await send_tx(dut, 0xA5 & DATA_MASK)
    await tick(dut, 4 * CLK_PER_BIT)
    await send_tx(dut, 0xFF & DATA_MASK)

    got = await monitor
    assert got == 0xA5 & DATA_MASK, f"TX frame corrupted by busy request: 0x{got:02X}"
    for _ in range(2 * FRAME_BITS * CLK_PER_BIT):
        await tick(dut)
        assert int(dut.o_uart_tx.value) == 1, "dropped request was transmitted"


@cocotb.test()
async def test_rx_frames(dut):
    await setup(dut)
    hits = []
    cocotb.start_soon(rx_ready_watch(dut, hits))
    values = [v & DATA_MASK for v in (0x55, 0xA5, 0x00, 0xFF, 0x01, 0x80)]
    for value in values:
        await drive_rx_frame(dut, value)
        await tick(dut, 2)
        got = int(dut.o_rx_data.value)
        assert got == value, f"RX: expected 0x{value:02X}, got 0x{got:02X}"
    assert hits == values, f"o_rx_ready pulses {hits} != frames {values}"


@cocotb.test()
async def test_rx_rejects_bad_stop_bit(dut):
    """A frame whose final stop bit is 0 is discarded; RX then recovers."""
    await setup(dut)
    hits = []
    cocotb.start_soon(rx_ready_watch(dut, hits))
    await drive_rx_frame(dut, 0x3C & DATA_MASK)
    await tick(dut, 2)

    # Hold the bad stop bit past the mid-bit sample, then release before the
    # re-armed start check so the re-trigger is rejected as a false start.
    bits = frame_bits(0xC3 & DATA_MASK, stop_bit=0)
    for bit in bits[:-1]:
        dut.i_uart_rx.value = bit
        await tick(dut, CLK_PER_BIT)
    dut.i_uart_rx.value = 0
    await tick(dut, 3 * CLK_PER_BIT // 4)
    dut.i_uart_rx.value = 1
    await tick(dut, 3 * CLK_PER_BIT)
    assert int(dut.o_rx_data.value) == 0x3C & DATA_MASK, "framing error leaked to o_rx_data"

    await drive_rx_frame(dut, 0x96 & DATA_MASK)
    await tick(dut, 2)
    assert int(dut.o_rx_data.value) == 0x96 & DATA_MASK, "RX did not recover"
    assert hits == [0x3C & DATA_MASK, 0x96 & DATA_MASK], f"unexpected o_rx_ready pulses: {hits}"


@cocotb.test(skip=not PARITY_EN)
async def test_rx_rejects_bad_parity(dut):
    await setup(dut)
    hits = []
    cocotb.start_soon(rx_ready_watch(dut, hits))
    await drive_rx_frame(dut, 0x5A & DATA_MASK, parity_flip=True)
    await tick(dut, 2 * CLK_PER_BIT)
    assert hits == [], "frame with bad parity was accepted"
    await drive_rx_frame(dut, 0x5A & DATA_MASK)
    await tick(dut, 2)
    assert hits == [0x5A & DATA_MASK], "RX did not recover after parity error"


@cocotb.test()
async def test_rx_ignores_short_glitch(dut):
    """A low pulse shorter than half a bit is a false start, not a frame."""
    await setup(dut)
    hits = []
    cocotb.start_soon(rx_ready_watch(dut, hits))
    dut.i_uart_rx.value = 0
    await tick(dut, CLK_PER_BIT // 4)
    dut.i_uart_rx.value = 1
    await tick(dut, (FRAME_BITS + 2) * CLK_PER_BIT)
    assert hits == [], "glitch produced a received byte"

    await drive_rx_frame(dut, 0x5A & DATA_MASK)
    await tick(dut, 2)
    assert hits == [0x5A & DATA_MASK]


@cocotb.test()
async def test_loopback_random(dut):
    """o_uart_tx wired to i_uart_rx: every random byte sent is received."""
    await setup(dut)
    hits = []
    cocotb.start_soon(rx_ready_watch(dut, hits))

    async def loopback():
        while True:
            await RisingEdge(dut.clk)
            dut.i_uart_rx.value = int(dut.o_uart_tx.value)

    cocotb.start_soon(loopback())
    sent = []
    for _ in range(20):
        value = random.randint(0, DATA_MASK)
        sent.append(value)
        await send_tx(dut, value)
        await tick(dut, (FRAME_BITS + 1) * CLK_PER_BIT)
    assert hits == sent, f"loopback mismatch: sent {sent}, got {hits}"


@cocotb.test()
async def test_async_reset_mid_frame(dut):
    """ares returns TX to idle immediately and aborts a partial RX frame."""
    await setup(dut)
    hits = []
    cocotb.start_soon(rx_ready_watch(dut, hits))
    await send_tx(dut, 0x00)
    dut.i_uart_rx.value = 0
    await tick(dut, 3 * CLK_PER_BIT)
    assert int(dut.o_uart_tx.value) == 0

    dut.ares.value = 1
    await Timer(1, unit="ns")  # no rising edge in this 1 ns
    assert int(dut.o_uart_tx.value) == 1, "async reset didn't idle TX immediately"
    dut.i_uart_rx.value = 1
    await tick(dut)
    dut.ares.value = 0
    await tick(dut, (FRAME_BITS + 2) * CLK_PER_BIT)
    assert hits == [], "aborted RX frame produced data"
    assert int(dut.o_uart_tx.value) == 1, "TX resumed after reset"

    await drive_rx_frame(dut, 0x42 & DATA_MASK)
    await tick(dut, 2)
    assert hits == [0x42 & DATA_MASK]
