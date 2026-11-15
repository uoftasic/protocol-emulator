import cocotb 
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer


#random testbench
import random
from collections import deque

async def tick(dut, n=1):
    """let the DUT act on n rising edges, then return at a safe point."""
    for _ in range(n):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)


async def setup(dut):
    """Start the clock, drive all inputs, apply sync reset."""

    Clock(dut.clk, 10, unit="ns").start()

    dut.i_wren.value = 0
    dut.i_rden.value = 0
    dut.i_data_in.value = 0
    dut.ares.value = 0
    dut.rst.value = 1

    await tick(dut, 2)
    dut.rst.value = 0
    await tick(dut)


DEPTH = 16

async def push(dut, data):
    dut.i_data_in.value = data
    dut.i_wren.value = 1
    await tick(dut)
    dut.i_wren.value = 0

async def pop(dut):
    dut.i_rden.value = 1
    await tick(dut)
    dut.i_rden.value = 0
    return int(dut.o_data_out.value)


@cocotb.test()
async def test_reset(dut):
    await setup(dut)
    assert dut.o_fifo_empty.value == 1
    assert dut.o_fifo_full.value == 0
    assert dut.o_fifo_count.value == 0

@cocotb.test()
async def test_one_write_read(dut):
    await setup(dut)

    #push 0xAB
    dut.i_data_in.value = 0xAB
    dut.i_wren.value = 1
    await tick(dut)
    dut.i_wren.value = 0

    assert dut.o_fifo_empty.value == 0, "should not be empty after a write"
    assert dut.o_fifo_count.value == 1

    dut.i_rden.value = 1
    await tick(dut)
    dut.i_rden.value = 0

    got = int(dut.o_data_out.value)
    assert got == 0xAB, f"expected 0xAB, got {hex(got)}"
    assert dut.o_fifo_empty.value == 1, "should be empty again"


@cocotb.test()
async def test_full_and_overflow(dut):
    await setup(dut)

    # Fill: should hold DEPTH-1 entries
    for i in range(DEPTH - 1):
        assert dut.o_fifo_full.value == 0, f"full too early, after {i} pushes"
        await push(dut, i)

    assert dut.o_fifo_full.value == 1, "should be full"
    assert dut.o_fifo_count.value == DEPTH - 1

    # Overflow: this push must be dropped
    await push(dut, 0xFF)
    assert dut.o_fifo_count.value == DEPTH - 1, "count changed on overflow"

    # Drain: must get back exactly 0..14, in order, no 0xFF
    for i in range(DEPTH - 1):
        got = await pop(dut)
        assert got == i, f"pop {i}: expected {i}, got {got}"

    assert dut.o_fifo_empty.value == 1

@cocotb.test()
async def test_pop_when_empty(dut):
    await setup(dut)
    await push(dut, 0x5A)
    assert await pop(dut) == 0x5A          # now empty

    got = await pop(dut)                   # illegal pop
    assert got == 0x5A, "dout should hold its last value"
    assert dut.o_fifo_count.value == 0, "count went negative/wrapped"
    assert dut.o_fifo_empty.value == 1

@cocotb.test()
async def test_push_pop_same_cycle(dut):
    await setup(dut)
    await push(dut, 0x11)
    await push(dut, 0x22)

    # Both enables high for ONE cycle
    dut.i_data_in.value = 0x33
    dut.i_wren.value = 1
    dut.i_rden.value = 1
    await tick(dut)
    dut.i_wren.value = 0
    dut.i_rden.value = 0

    assert int(dut.o_data_out.value) == 0x11, "should pop oldest"
    assert dut.o_fifo_count.value == 2, "count should be unchanged"
    assert await pop(dut) == 0x22
    assert await pop(dut) == 0x33

@cocotb.test()
async def test_wraparound(dut):
    await setup(dut)
    for i in range(3 * DEPTH):             # pointers wrap past the end ~3 times
        await push(dut, i)
        got = await pop(dut)
        assert got == i, f"iter {i}: got {got}"

@cocotb.test()
async def test_async_reset(dut):
    await setup(dut)
    await push(dut, 1)
    await push(dut, 2)
    assert dut.o_fifo_count.value == 2

    dut.ares.value = 1
    await Timer(1, unit="ns")              # no rising edge in this 1 ns
    assert dut.o_fifo_empty.value == 1, "async reset didn't act immediately"
    assert dut.o_fifo_count.value == 0

    dut.ares.value = 0
    await tick(dut)
    await push(dut, 7)                     # still works after reset?
    assert await pop(dut) == 7


@cocotb.test()
async def test_random(dut):
    await setup(dut)
    model = deque()        # the "golden" FIFO
    last_out = 0           # o_data_out after reset

    for cycle in range(2000):
        wr = random.random() < 0.5
        rd = random.random() < 0.5
        data = random.randint(0, 255)

        # 1. Predict, using the state BEFORE the clock edge
        do_rd = rd and len(model) > 0
        do_wr = wr and len(model) < DEPTH - 1
        if do_rd:
            last_out = model.popleft()
        if do_wr:
            model.append(data)

        # 2. Drive the DUT and let it act
        dut.i_wren.value = int(wr)
        dut.i_rden.value = int(rd)
        dut.i_data_in.value = data
        await tick(dut)

        # 3. Compare everything
        assert int(dut.o_data_out.value) == last_out, f"cycle {cycle}: data"
        assert int(dut.o_fifo_count.value) == len(model), f"cycle {cycle}: count"
        assert int(dut.o_fifo_empty.value) == int(len(model) == 0), f"cycle {cycle}: empty"
        assert int(dut.o_fifo_full.value) == int(len(model) == DEPTH - 1), f"cycle {cycle}: full"

    dut.i_wren.value = 0
    dut.i_rden.value = 0