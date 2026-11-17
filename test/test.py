# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
#
# Chip-level tests for tt_um_uoftasic_protocol_emulator through its real pins
# (pinout in info.yaml): UART RX on ui_in[2], UART TX on uo_out[1], 8N1 at
# 434 clocks per bit. Until the SPI host interface exists, the wrapper echoes
# every byte it receives. Detailed UART behaviour is covered by the
# block-level tests in test/block-level-tests/L2-uart-controller/.

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

CLK_PER_BIT = 434
RX_PIN = 2  # ui_in[2]
TX_PIN = 1  # uo_out[1]
FRAME_CLOCKS = 10 * CLK_PER_BIT  # 8N1


def set_rx(dut, level):
    dut.ui_in.value = level << RX_PIN


def tx_level(dut):
    return (int(dut.uo_out.value) >> TX_PIN) & 1


async def reset(dut):
    # A slow clock keeps these tests practical with CLK_PER_BIT=434.
    cocotb.start_soon(Clock(dut.clk, 10, unit="us").start())
    dut.ena.value = 1
    set_rx(dut, 1)
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 3)


async def drive_rx_frame(dut, value, stop_bit=1, stop_clocks=CLK_PER_BIT):
    """Drive one 8N1 frame on the RX pin, LSB first, then return to idle."""
    set_rx(dut, 0)
    await ClockCycles(dut.clk, CLK_PER_BIT)
    for bit in range(8):
        set_rx(dut, (value >> bit) & 1)
        await ClockCycles(dut.clk, CLK_PER_BIT)
    set_rx(dut, stop_bit)
    await ClockCycles(dut.clk, stop_clocks)
    set_rx(dut, 1)


async def sample_tx_frame(dut):
    """Wait for a start bit on the TX pin, sample mid-bit, return the byte."""
    while tx_level(dut):
        await ClockCycles(dut.clk, 1)
    await ClockCycles(dut.clk, CLK_PER_BIT // 2)
    assert tx_level(dut) == 0, "start bit not stable"
    value = 0
    for bit in range(8):
        await ClockCycles(dut.clk, CLK_PER_BIT)
        value |= tx_level(dut) << bit
    await ClockCycles(dut.clk, CLK_PER_BIT)
    assert tx_level(dut) == 1, "stop bit missing"
    return value


async def tx_frame_collector(dut, frames):
    while True:
        frames.append(await sample_tx_frame(dut))


@cocotb.test()
async def test_reset_state(dut):
    """After reset, TX idles high and every other output is held low."""
    await reset(dut)
    assert tx_level(dut) == 1, "UART TX should idle high"
    assert int(dut.uo_out.value) & ~(1 << TX_PIN) & 0xFF == 0, "unused uo_out pins not 0"
    assert int(dut.uio_out.value) == 0


@cocotb.test()
async def test_uio_pin_directions(dut):
    """No uio pad is used, so none may be driven as an output."""
    await reset(dut)
    assert int(dut.uio_oe.value) == 0, f"uio_oe=0b{int(dut.uio_oe.value):08b}, expected all inputs"


@cocotb.test()
async def test_echo(dut):
    """Each byte received on RX is transmitted back on TX."""
    await reset(dut)
    for value in (0x55, 0xA5, 0x00, 0xFF, 0x01, 0x80):
        monitor = cocotb.start_soon(sample_tx_frame(dut))
        await drive_rx_frame(dut, value)
        got = await monitor
        assert got == value, f"echo: sent 0x{value:02X}, got 0x{got:02X}"
        await ClockCycles(dut.clk, CLK_PER_BIT)


@cocotb.test()
async def test_echo_back_to_back(dut):
    """Frames sent with no idle gap are all echoed, in order."""
    await reset(dut)
    frames = []
    cocotb.start_soon(tx_frame_collector(dut, frames))
    sent = [0x12, 0xED, 0x7E, 0x81, 0x3C]
    for value in sent:
        await drive_rx_frame(dut, value)
    await ClockCycles(dut.clk, 2 * FRAME_CLOCKS)
    assert frames == sent, f"sent {[hex(v) for v in sent]}, echoed {[hex(v) for v in frames]}"


@cocotb.test()
async def test_bad_stop_bit_not_echoed(dut):
    """A frame with a 0 stop bit is discarded; the next good frame is echoed."""
    await reset(dut)
    frames = []
    cocotb.start_soon(tx_frame_collector(dut, frames))

    # Hold the stop bit low past the receiver's mid-bit sample, then release
    # before its re-armed start check, so the re-trigger is a false start.
    await drive_rx_frame(dut, 0xC3, stop_bit=0, stop_clocks=3 * CLK_PER_BIT // 4)
    await ClockCycles(dut.clk, 2 * FRAME_CLOCKS)
    assert frames == [], f"framing error was echoed: {frames}"

    await drive_rx_frame(dut, 0x96)
    await ClockCycles(dut.clk, FRAME_CLOCKS)
    assert frames == [0x96], f"RX did not recover after framing error: {frames}"


@cocotb.test()
async def test_short_glitch_not_echoed(dut):
    """A low pulse shorter than half a bit is a false start, not a frame."""
    await reset(dut)
    frames = []
    cocotb.start_soon(tx_frame_collector(dut, frames))
    set_rx(dut, 0)
    await ClockCycles(dut.clk, CLK_PER_BIT // 4)
    set_rx(dut, 1)
    await ClockCycles(dut.clk, 2 * FRAME_CLOCKS)
    assert frames == [], f"glitch produced output: {frames}"

    await drive_rx_frame(dut, 0x5A)
    await ClockCycles(dut.clk, FRAME_CLOCKS)
    assert frames == [0x5A]
