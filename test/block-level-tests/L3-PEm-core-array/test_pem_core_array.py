"""Black-box PEm array tests using real cores and the public flat interfaces.

Expected behavior comes from the PEm architecture and shared flag contract.
No hierarchical DUT reads, forces, or internal IRQ probes are used. Helpers,
tests, and explicit test-only divider generation stay in this one file.
"""

from collections import deque
from itertools import combinations, product
from pathlib import Path
import argparse
import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Event, FallingEdge, RisingEdge, Timer


SM_CTRL, SM_STATUS, PC, WRAP, SHIFT_CTRL, SHIFT_THRESH, CLKDIV = range(7)
X, Y, ISR, OSR, SHIFT_COUNT, INIT_PC, PIN_CTRL, PIN_OUT, PIN_DIR = range(7, 16)
SYNC_STATE, SYNC_SET, SYNC_CLEAR, SYNC_STATUS = range(4)
STEP, CONT_RUN, RESET = 1, 2, 4
RUNNING, STALLED, WAITING_SYNC, INVALID = 1, 2, 4, 8
NOP = 0xA042
NUM_CORES = int(os.environ.get("PEM_ARRAY_NUM_CORES", "4"))
NUM_SYNC_FLAGS = int(os.environ.get("PEM_ARRAY_NUM_SYNC_FLAGS", "8"))
ALL_CORES = (1 << NUM_CORES) - 1
CLOCK_PERIOD_NS = 10
SETTLE_PS = 1
RESET_VALUES = {address: 0 for address in range(16)}
RESET_VALUES.update({CLKDIV: 0x0010, SHIFT_COUNT: 0x0080})


def pack(values, width):
    return sum(value << (index * width) for index, value in enumerate(values))


def unpack(value, width):
    return [(value >> (index * width)) & ((1 << width) - 1) for index in range(NUM_CORES)]


def core_mask(cores):
    return sum(1 << core for core in cores)


def irq(flag, clear=False, wait=False, relative=False, delay=0):
    return 0xC000 | (delay << 8) | (int(clear) << 7) | (int(wait) << 6) | (int(relative) << 5) | flag


def wait_irq(flag, polarity=True, relative=False):
    return 0x2040 | (int(polarity) << 7) | (int(relative) << 4) | flag


def wait_pin(pin=0, polarity=True):
    return 0x2020 | (int(polarity) << 7) | pin


def mov(destination, source, operation=0):
    return 0xA000 | (destination << 5) | (operation << 3) | source


def flag_address(core, flag, relative):
    return (flag & 4) | (((flag & 3) + (core if relative else 0)) % 4)


def stalled_status(cause, running=True):
    return (RUNNING if running else 0) | STALLED | (WAITING_SYNC if cause == 2 else 0) | (cause << 5)


class ArrayBench:
    """A shared asynchronous instruction image and independent Link endpoints."""

    def __init__(self, dut):
        self.dut = dut
        self.program = [NOP] * 32
        self.virt_in = [0] * NUM_CORES
        self.tx_queues = [deque() for _ in range(NUM_CORES)]
        self.rx_ready = ALL_CORES
        self.events = []
        self.cycles = 0
        self.cycle_done = Event()
        for name in (
            "clk", "rst", "i_soft_reset", "i_halt_req", "i_csr_addr", "i_csr_din",
            "i_csr_wren", "i_imem_din", "i_tx_fifo_data", "i_tx_fifo_valid",
            "i_rx_fifo_ready", "i_virt_in", "i_sync_csr_addr", "i_sync_csr_din",
            "i_sync_csr_wren",
        ):
            getattr(dut, name).value = 0
        self.clock = Clock(dut.clk, CLOCK_PERIOD_NS, unit="ns")
        self.clock.start(start_high=False)
        self.environment = cocotb.start_soon(self.run_environment())

    def drive(self):
        addresses = [0] * NUM_CORES if int(self.dut.rst.value) else unpack(int(self.dut.o_imem_addr.value), 5)
        self.dut.i_imem_din.value = pack([self.program[address] for address in addresses], 16)
        self.dut.i_virt_in.value = pack(self.virt_in, 8)
        self.dut.i_tx_fifo_valid.value = core_mask(core for core, queue in enumerate(self.tx_queues) if queue)
        self.dut.i_tx_fifo_data.value = pack([queue[0] if queue else 0 for queue in self.tx_queues], 8)
        self.dut.i_rx_fifo_ready.value = self.rx_ready

    async def run_environment(self):
        while True:
            await Timer(SETTLE_PS, unit="ps")
            self.drive()
            await RisingEdge(self.dut.clk)
            tx_ready = int(self.dut.o_tx_fifo_ready.value)
            rx_valid = int(self.dut.o_rx_fifo_valid.value)
            fallback = int(self.dut.o_tx_fallback_event.value)
            drop = int(self.dut.o_rx_drop_event.value)
            tx_valid = int(self.dut.i_tx_fifo_valid.value)
            rx_ready = int(self.dut.i_rx_fifo_ready.value)
            tx_data = unpack(int(self.dut.i_tx_fifo_data.value), 8)
            rx_data = unpack(int(self.dut.o_rx_fifo_data.value), 8) if rx_valid else [0] * NUM_CORES
            resetting = ALL_CORES if int(self.dut.rst.value) else int(self.dut.i_soft_reset.value)
            if int(self.dut.i_csr_wren.value) and int(self.dut.i_csr_addr.value) == SM_CTRL:
                if int(self.dut.i_csr_din.value) & RESET:
                    resetting |= int(self.dut.i_csr_wren.value)
            assert not (tx_ready & ~tx_valid), "TX consumption without data"
            assert not (rx_valid & ~rx_ready), "RX production without space"
            assert not (fallback & tx_ready), "Fallback and TX consumption both asserted"
            assert not (drop & rx_valid), "Drop and RX production both asserted"
            assert not ((tx_ready | rx_valid | fallback | drop) & resetting), "Transfer on a core reset edge"
            for kind, mask, values in (
                ("tx", tx_ready, tx_data), ("rx", rx_valid, rx_data),
                ("fallback", fallback, [None] * NUM_CORES), ("drop", drop, [None] * NUM_CORES),
            ):
                for core in range(NUM_CORES):
                    if mask & (1 << core):
                        self.events.append((self.cycles + 1, kind, core, values[core]))
            await Timer(SETTLE_PS, unit="ps")
            for core in range(NUM_CORES):
                if tx_ready & (1 << core):
                    assert self.tx_queues[core].popleft() == tx_data[core]
            self.cycles += 1
            self.drive()
            await Timer(SETTLE_PS, unit="ps")
            self.cycle_done.set()
            await FallingEdge(self.dut.clk)

    async def tick(self, count=1):
        target = self.cycles + count
        while self.cycles < target:
            self.cycle_done.clear()
            await self.cycle_done.wait()

    async def reset(self):
        self.dut.i_csr_wren.value = 0
        self.dut.i_sync_csr_wren.value = 0
        self.dut.i_soft_reset.value = 0
        self.dut.i_halt_req.value = 0
        self.dut.rst.value = 1
        await self.tick(2)
        self.dut.rst.value = 0
        await self.tick()

    async def read(self, address):
        self.dut.i_csr_addr.value = address
        await Timer(SETTLE_PS, unit="ps")
        return unpack(int(self.dut.o_csr_dout.value), 16)

    async def expect(self, address, values):
        actual = await self.read(address)
        expected = [values] * NUM_CORES if isinstance(values, int) else list(values)
        assert actual == expected, f"CSR {address:X}: expected {expected}, got {actual}"

    async def expect_core(self, core, address, value):
        actual = (await self.read(address))[core]
        assert actual == value, f"Core {core}, CSR {address:X}: expected {value:X}, got {actual:X}"

    async def write(self, address, value, mask=ALL_CORES, rejected=0):
        await FallingEdge(self.dut.clk)
        self.dut.i_csr_addr.value = address
        self.dut.i_csr_din.value = value
        self.dut.i_csr_wren.value = mask
        await self.tick()
        self.dut.i_csr_wren.value = 0
        actual = int(self.dut.o_csr_error.value)
        assert actual == rejected, f"CSR {address:X} write mask {mask:X}: expected error {rejected:X}, got {actual:X}"
        await Timer(SETTLE_PS, unit="ps")

    async def sync_read(self, address=SYNC_STATE):
        self.dut.i_sync_csr_addr.value = address
        await Timer(SETTLE_PS, unit="ps")
        return int(self.dut.o_sync_csr_dout.value)

    async def expect_sync(self, flags, collision=None):
        actual = await self.sync_read()
        assert actual == flags, f"Sync flags: expected {flags:02X}, got {actual:04X}"
        if collision is not None:
            actual = await self.sync_read(SYNC_STATUS)
            assert actual == int(collision), f"Collision: expected {int(collision)}, got {actual}"

    async def sync_write(self, address, value, rejected=False):
        if address in (SYNC_SET, SYNC_CLEAR):
            assert not any(status & RUNNING for status in await self.read(SM_STATUS)), "Router must halt cores before mask writes"
        await FallingEdge(self.dut.clk)
        self.dut.i_sync_csr_addr.value = address
        self.dut.i_sync_csr_din.value = value
        self.dut.i_sync_csr_wren.value = 1
        await self.tick()
        self.dut.i_sync_csr_wren.value = 0
        assert int(self.dut.o_sync_csr_error.value) == int(rejected)
        await Timer(SETTLE_PS, unit="ps")

    async def seed_flags(self, flags):
        await self.sync_write(SYNC_CLEAR, 0xFF)
        await self.sync_write(SYNC_SET, flags)
        await self.sync_write(SYNC_STATUS, 1)
        await self.expect_sync(flags, False)

    async def soft_reset(self, mask=ALL_CORES):
        await FallingEdge(self.dut.clk)
        self.dut.i_soft_reset.value = mask
        await self.tick()
        self.dut.i_soft_reset.value = 0
        await Timer(SETTLE_PS, unit="ps")

    async def hold(self, mask=ALL_CORES):
        await FallingEdge(self.dut.clk)
        self.dut.i_halt_req.value = mask
        await self.tick(2)

    async def wait_halted(self, mask=ALL_CORES, limit=512):
        for elapsed in range(limit + 1):
            statuses = await self.read(SM_STATUS)
            if all(not status & RUNNING for core, status in enumerate(statuses) if mask & (1 << core)):
                return elapsed
            if elapsed < limit:
                await self.tick()
        raise AssertionError(f"Cores {mask:X} did not halt in {limit} clocks; status={statuses}")

    async def prepare(self, words):
        assert not any(status & RUNNING for status in await self.read(SM_STATUS))
        unique = list(dict.fromkeys(words.values()))
        assert len(unique) <= 32
        self.program = unique + [NOP] * (32 - len(unique))
        entries = {core: unique.index(word) for core, word in words.items()}
        for entry in range(len(unique)):
            mask = core_mask(core for core, address in entries.items() if address == entry)
            await self.write(INIT_PC, entry, mask)
        await self.soft_reset()
        return entries

    async def start(self, cores, command=STEP):
        await self.write(SM_CTRL, command, core_mask(cores))

    async def execute(self, words):
        entries = await self.prepare(words)
        await self.start(words)
        await self.wait_halted(core_mask(words))
        await self.completed(entries)
        return entries

    async def completed(self, entries):
        pcs = await self.read(PC)
        controls = await self.read(SM_CTRL)
        for core, entry in entries.items():
            assert pcs[core] == (entry + 1) % 32, f"Core {core}: instruction did not advance PC exactly once"
            assert controls[core] == 0, f"Core {core}: STEP was not consumed"

    async def step_current(self, mask=ALL_CORES):
        await self.write(SM_CTRL, STEP, mask)
        return await self.wait_halted(mask)

    def transfers(self, kind):
        return [(core, value) for _, event, core, value in self.events if event == kind]


async def setup(dut):
    bench = ArrayBench(dut)
    await bench.reset()
    return bench


@cocotb.test()
async def test_reset_defaults_and_flat_port_widths(dut):
    bench = await setup(dut)
    widths = {
        "o_csr_dout": 16, "i_imem_din": 16, "o_imem_addr": 5,
        "i_tx_fifo_data": 8, "o_rx_fifo_data": 8, "i_virt_in": 8,
        "o_virt_out": 8, "o_virt_outen": 8, "i_soft_reset": 1,
        "i_halt_req": 1, "i_csr_wren": 1, "o_csr_error": 1,
    }
    for name, width in widths.items():
        assert len(getattr(dut, name)) == NUM_CORES * width, name
    for address, value in RESET_VALUES.items():
        await bench.expect(address, value)
    for address in range(4):
        assert await bench.sync_read(address) == 0
    assert int(dut.o_virt_out.value) == 0
    assert int(dut.o_virt_outen.value) == 0
    assert not bench.events


@cocotb.test()
async def test_core_csr_write_masks_readback_and_error_isolation(dut):
    bench = await setup(dut)
    values = [0] * NUM_CORES
    for core in range(NUM_CORES):
        values[core] = (0x35 + 29 * core) & 0xFF
        await bench.write(X, values[core], 1 << core)
        await bench.expect(X, values)
    await bench.write(X, 0xEA, 0)
    await bench.expect(X, values)
    selected = core_mask(range(0, NUM_CORES, 2))
    await bench.write(Y, 0xA7, selected)
    await bench.expect(Y, [0xA7 if selected & (1 << core) else 0 for core in range(NUM_CORES)])
    await bench.write(PC, 0xFFFF, selected, rejected=selected)
    await bench.expect(PC, 0)
    await bench.write(X, 0x015A, ALL_CORES, rejected=ALL_CORES)
    await bench.expect(X, values)
    await bench.tick()
    assert int(dut.o_csr_error.value) == 0
    await bench.expect_sync(0, False)


@cocotb.test()
async def test_shared_imem_independent_pcs_and_full_core_id_bytes(dut):
    bench = await setup(dut)
    for operation in range(3):
        await bench.execute({core: mov(1, 4, operation) for core in range(NUM_CORES)})
        values = list(range(NUM_CORES))
        if operation == 1:
            values = [value ^ 0xFF for value in values]
        elif operation == 2:
            values = [int(f"{value:08b}"[::-1], 2) for value in values]
        await bench.expect(X, values)
    await bench.expect_sync(0, False)
    assert not bench.events


@cocotb.test()
async def test_virtual_output_and_direction_slices_are_independent(dut):
    bench = await setup(dut)
    levels = [(0x96 + core * 17) & 0xFF for core in range(NUM_CORES)]
    enables = [(0x69 ^ (core * 31)) & 0xFF for core in range(NUM_CORES)]
    for core in range(NUM_CORES):
        await bench.write(PIN_OUT, levels[core], 1 << core)
        await bench.write(PIN_DIR, enables[core], 1 << core)
    assert int(dut.o_virt_out.value) == pack(levels, 8)
    assert int(dut.o_virt_outen.value) == pack(enables, 8)
    await bench.expect(PIN_OUT, levels)
    await bench.expect(PIN_DIR, enables)
    await bench.hold(1 << (NUM_CORES - 1))
    assert int(dut.o_virt_out.value) == pack(levels, 8)
    assert int(dut.o_virt_outen.value) == pack(enables, 8)


@cocotb.test()
async def test_virtual_input_slices_sample_each_core_own_byte(dut):
    bench = await setup(dut)
    bench.virt_in = [(0x81 + core * 23) & 0xFF for core in range(NUM_CORES)]
    await bench.execute({core: mov(1, 0) for core in range(NUM_CORES)})
    await bench.expect(X, bench.virt_in)
    await bench.execute({core: 0x4000 for core in range(NUM_CORES)})  # IN PINS,8.
    await bench.expect(ISR, bench.virt_in)
    await bench.expect(SHIFT_COUNT, 0x88)
    assert not bench.events


@cocotb.test()
async def test_link_pull_move_push_routes_bytes_to_correct_cores(dut):
    bench = await setup(dut)
    values = [(0xA5 ^ (core * 37)) & 0xFF for core in range(NUM_CORES)]
    bench.program[:3] = [0x80A0, mov(5, 7), 0x8020]  # PULL; MOV ISR,OSR; PUSH.
    for core, value in enumerate(values):
        bench.tx_queues[core].append(value)
    await bench.step_current()
    await bench.expect(OSR, values)
    assert bench.transfers("tx") == list(enumerate(values))
    await bench.step_current()
    await bench.expect(ISR, values)
    await bench.step_current()
    assert bench.transfers("rx") == list(enumerate(values))
    await bench.expect(ISR, 0)
    await bench.expect(PC, 3)
    assert not bench.transfers("fallback") and not bench.transfers("drop")


@cocotb.test()
async def test_tx_stall_is_local_and_release_consumes_one_byte(dut):
    bench = await setup(dut)
    target = NUM_CORES - 1
    bench.program[0] = 0x80A0
    for core in range(NUM_CORES - 1):
        bench.tx_queues[core].append((0x20 + core) & 0xFF)
    await bench.start(range(NUM_CORES))
    await bench.tick(3)
    await bench.expect_core(target, SM_STATUS, stalled_status(3))
    await bench.expect(PC, [1] * target + [0])
    assert len(bench.transfers("tx")) == target
    bench.tx_queues[target].append(0xE7)
    await bench.tick(2)
    await bench.expect(PC, 1)
    await bench.expect_core(target, OSR, 0xE7)
    assert bench.transfers("tx")[-1] == (target, 0xE7)
    assert len(bench.transfers("tx")) == NUM_CORES


@cocotb.test()
async def test_rx_backpressure_is_local_and_release_produces_once(dut):
    bench = await setup(dut)
    target = NUM_CORES - 1
    values = [(core * 11 + 0x51) & 0xFF for core in range(NUM_CORES)]
    bench.program[0] = 0x8020
    for core, value in enumerate(values):
        await bench.write(ISR, value, 1 << core)
    bench.rx_ready &= ~(1 << target)
    await bench.start(range(NUM_CORES))
    await bench.tick(3)
    await bench.expect_core(target, SM_STATUS, stalled_status(4))
    await bench.expect_core(target, ISR, values[target])
    assert bench.transfers("rx") == list(enumerate(values[:-1]))
    bench.rx_ready = ALL_CORES
    await bench.tick(2)
    assert bench.transfers("rx") == list(enumerate(values))
    await bench.expect(PC, 1)
    await bench.expect(ISR, 0)


@cocotb.test()
async def test_nonblocking_link_diagnostics_preserve_core_selection(dut):
    bench = await setup(dut)
    values = [(0x37 + 13 * core) & 0xFF for core in range(NUM_CORES)]
    bench.program[:2] = [0x8080, 0x8000]  # Nonblocking PULL and PUSH.
    for core, value in enumerate(values):
        await bench.write(X, value, 1 << core)
    await bench.step_current()
    await bench.expect(OSR, values)
    assert bench.transfers("fallback") == [(core, None) for core in range(NUM_CORES)]
    for core, value in enumerate(values):
        await bench.write(ISR, value, 1 << core)
    bench.rx_ready = 0
    await bench.step_current()
    assert bench.transfers("drop") == [(core, None) for core in range(NUM_CORES)]
    assert not bench.transfers("tx") and not bench.transfers("rx")


@cocotb.test()
async def test_wait_pin_stalls_only_the_selected_core(dut):
    bench = await setup(dut)
    target = NUM_CORES - 1
    bench.virt_in = [1] * target + [0]
    entries = await bench.prepare({core: wait_pin() for core in range(NUM_CORES)})
    await bench.start(entries)
    await bench.tick(3)
    await bench.expect(PC, [1] * target + [0])
    await bench.expect_core(target, SM_STATUS, stalled_status(1))
    bench.virt_in[target] = 1
    await bench.tick(2)
    await bench.completed(entries)
    await bench.expect_sync(0, False)


@cocotb.test()
async def test_sync_mask_csrs_all_masks_upper_bits_and_zero_writes(dut):
    bench = await setup(dut)
    for mask in range(256):
        await bench.seed_flags(0x5A)
        await bench.sync_write(SYNC_SET, 0xFF00 | mask)
        await bench.expect_sync(0x5A | mask, False)
        await bench.sync_write(SYNC_CLEAR, 0xA500 | mask)
        await bench.expect_sync(0x5A & ~mask, False)
        assert await bench.sync_read(SYNC_SET) == 0
        assert await bench.sync_read(SYNC_CLEAR) == 0
    await bench.seed_flags(0xA5)
    await bench.sync_write(SYNC_SET, 0)
    await bench.sync_write(SYNC_CLEAR, 0)
    await bench.expect_sync(0xA5, False)


@cocotb.test()
async def test_sync_state_write_rejected_and_reads_have_no_effects(dut):
    bench = await setup(dut)
    await bench.seed_flags(0x96)
    for value in (0, 0xFFFF, 0x69):
        await bench.sync_write(SYNC_STATE, value, rejected=True)
        await bench.expect_sync(0x96, False)
        assert int(dut.o_csr_error.value) == 0
    await bench.tick()
    assert int(dut.o_sync_csr_error.value) == 0
    for _ in range(16):
        for address in range(4):
            await bench.sync_read(address)
    await bench.sync_write(SYNC_STATUS, 0xFFFE)
    await bench.expect_sync(0x96, False)
    await bench.expect(PC, 0)
    assert not bench.events


@cocotb.test()
async def test_absolute_irq_access_every_core_and_flag(dut):
    bench = await setup(dut)
    for core in range(NUM_CORES):
        for flag in range(8):
            await bench.seed_flags(0xA5)
            await bench.execute({core: irq(flag)})
            await bench.expect_sync(0xA5 | (1 << flag), False)
            await bench.execute({core: irq(flag, clear=True)})
            await bench.expect_sync(0xA5 & ~(1 << flag), False)
    assert not bench.events


@cocotb.test(skip=NUM_CORES < 2)
async def test_same_flag_pairs_all_priorities_values_and_flags(dut):
    bench = await setup(dut)
    for high, low in combinations(range(NUM_CORES), 2):
        for flag in range(8):
            for high_value, low_value in product(range(2), repeat=2):
                await bench.seed_flags(0x5A)
                await bench.execute({high: irq(flag, clear=not high_value), low: irq(flag, clear=not low_value)})
                expected = (0x5A & ~(1 << flag)) | (high_value << flag)
                await bench.expect_sync(expected, True)
                await bench.tick(2)
                await bench.expect_sync(expected, True)
    assert not bench.events


@cocotb.test()
async def test_distinct_flags_update_in_parallel_without_collision(dut):
    bench = await setup(dut)
    await bench.seed_flags(0x96)
    words = {core: irq(core, clear=bool(core % 2)) for core in range(min(NUM_CORES, 8))}
    entries = await bench.prepare(words)
    await bench.start(entries)
    await bench.tick()
    await bench.expect_sync(0x96, False)
    await bench.tick()
    expected = 0x96
    for core in words:
        expected = (expected & ~(1 << core)) | ((1 - core % 2) << core)
    await bench.completed(entries)
    await bench.expect_sync(expected, False)


@cocotb.test()
async def test_all_explicit_writers_choose_core_zero(dut):
    bench = await setup(dut)
    for flag in range(8):
        await bench.seed_flags(0)
        await bench.execute({core: irq(flag, clear=bool(core % 2)) for core in range(NUM_CORES)})
        await bench.expect_sync(1 << flag, NUM_CORES > 1)


@cocotb.test(skip=NUM_CORES < 2)
async def test_priority_does_not_override_writes_on_later_edges(dut):
    bench = await setup(dut)
    for flag in range(8):
        await bench.seed_flags(0)
        await bench.execute({0: irq(flag)})
        await bench.expect_sync(1 << flag, False)
        await bench.execute({NUM_CORES - 1: irq(flag, clear=True)})
        await bench.expect_sync(0, False)
        await bench.execute({NUM_CORES - 1: irq(flag)})
        await bench.expect_sync(1 << flag, False)
        await bench.execute({0: irq(flag, clear=True)})
        await bench.expect_sync(0, False)


@cocotb.test()
async def test_all_wait_clears_coalesce_and_finish_together(dut):
    bench = await setup(dut)
    for flag in range(8):
        await bench.seed_flags(0xFF)
        await bench.execute({core: wait_irq(flag) for core in range(NUM_CORES)})
        await bench.expect_sync(0xFF & ~(1 << flag), False)
        await bench.expect(SM_STATUS, 0)
        await bench.tick(3)
        await bench.expect_sync(0xFF & ~(1 << flag), False)


@cocotb.test(skip=NUM_CORES < 2)
async def test_wait_group_vs_explicit_writer_uses_smallest_member_id(dut):
    bench = await setup(dut)
    for explicit in range(NUM_CORES):
        waiters = [core for core in range(NUM_CORES) if core != explicit]
        for flag in range(8):
            for value in range(2):
                await bench.seed_flags(0xFF)
                words = {core: wait_irq(flag) for core in waiters}
                words[explicit] = irq(flag, clear=not value)
                await bench.execute(words)
                winner_value = value if explicit < min(waiters) else 0
                expected = (0xFF & ~(1 << flag)) | (winner_value << flag)
                await bench.expect_sync(expected, True)
                await bench.expect(SM_STATUS, 0)


@cocotb.test(skip=NUM_CORES < 2)
async def test_wait_one_reads_old_zero_and_completes_on_later_edge(dut):
    bench = await setup(dut)
    entries = await bench.prepare({0: irq(3), 1: wait_irq(3)})
    await bench.start(entries)
    await bench.tick(2)
    await bench.expect_sync(8, False)
    await bench.expect_core(0, PC, entries[0] + 1)
    await bench.expect_core(1, PC, entries[1])
    assert (await bench.read(SM_STATUS))[1] & RUNNING
    await bench.tick()
    await bench.completed(entries)
    await bench.expect_sync(0, False)


@cocotb.test(skip=NUM_CORES < 2)
async def test_wait_zero_reads_old_level_without_emitting_clear(dut):
    bench = await setup(dut)
    entries = await bench.prepare({0: irq(6), 1: wait_irq(6, polarity=False)})
    await bench.start(entries)
    await bench.tick(2)
    await bench.completed(entries)
    await bench.expect_sync(0x40, False)
    entries = await bench.prepare({0: irq(6, clear=True), 1: wait_irq(6, polarity=False)})
    await bench.start(entries)
    await bench.tick(2)
    await bench.expect_sync(0, False)
    await bench.expect_core(1, PC, entries[1])
    await bench.tick()
    await bench.completed(entries)
    await bench.expect_sync(0, False)


@cocotb.test()
async def test_relative_irq_mapping_every_core_flag_and_bank(dut):
    bench = await setup(dut)
    for core in range(NUM_CORES):
        for flag in range(8):
            selected = flag_address(core, flag, True)
            await bench.seed_flags(0)
            await bench.execute({core: irq(flag, relative=True)})
            await bench.expect_sync(1 << selected, False)
            await bench.execute({core: irq(flag, clear=True, relative=True)})
            await bench.expect_sync(0, False)
            await bench.seed_flags(0xFF)
            await bench.execute({core: wait_irq(flag, relative=True)})
            await bench.expect_sync(0xFF & ~(1 << selected), False)


@cocotb.test(skip=NUM_CORES < 5)
async def test_relative_offsets_repeat_every_four_core_ids(dut):
    bench = await setup(dut)
    for flag in range(8):
        await bench.seed_flags(0)
        await bench.execute({0: irq(flag, relative=True), 4: irq(flag, clear=True, relative=True)})
        await bench.expect_sync(1 << flag, True)
        await bench.seed_flags(0xFF)
        await bench.execute({0: wait_irq(flag, relative=True), 4: wait_irq(flag, relative=True)})
        await bench.expect_sync(0xFF & ~(1 << flag), False)


@cocotb.test(skip=NUM_CORES < 2)
async def test_collision_w1c_new_event_priority_and_rearming(dut):
    bench = await setup(dut)
    entries = await bench.prepare({0: irq(2), 1: irq(2)})
    await bench.start(entries)
    await bench.tick()
    await bench.sync_write(SYNC_STATUS, 1)
    await bench.completed(entries)
    await bench.expect_sync(4, True)
    await bench.sync_write(SYNC_STATUS, 0)
    await bench.sync_write(SYNC_STATUS, 0xFFFE)
    await bench.expect_sync(4, True)
    await bench.sync_write(SYNC_STATUS, 1)
    await bench.expect_sync(4, False)
    await bench.execute({0: irq(2, clear=True), 1: irq(2, clear=True)})
    await bench.expect_sync(0, True)
    await bench.sync_write(SYNC_STATUS, 1)
    await bench.expect_sync(0, False)


@cocotb.test(skip=NUM_CORES < 2)
async def test_losing_irq_set_wait_does_not_retry_or_commit_on_issue_edge(dut):
    bench = await setup(dut)
    entries = await bench.prepare({0: irq(1, clear=True), 1: irq(1, wait=True)})
    await bench.start(entries)
    await bench.tick(2)
    await bench.expect_sync(0, True)
    await bench.expect_core(0, PC, entries[0] + 1)
    await bench.expect_core(1, PC, entries[1])
    await bench.tick()
    await bench.completed(entries)
    for _ in range(4):
        await bench.tick()
        await bench.expect_sync(0, True)


@cocotb.test(skip=NUM_CORES < 2)
async def test_winning_irq_set_wait_checks_acknowledgement_on_later_edge(dut):
    bench = await setup(dut)
    bench.program[:3] = [irq(4, wait=True), irq(4, clear=True), irq(4, clear=True)]
    await bench.write(INIT_PC, 1, 1 << 1)
    await bench.soft_reset()
    await bench.start((0, 1))
    await bench.tick(2)
    await bench.expect_sync(0x10, True)
    await bench.expect_core(0, PC, 0)
    await bench.expect_core(0, SM_STATUS, stalled_status(2))
    await bench.expect_core(1, PC, 2)
    await bench.start((1,))
    await bench.tick(2)
    await bench.expect_sync(0, True)
    await bench.expect_core(0, PC, 0)
    await bench.tick()
    await bench.expect_core(0, PC, 1)
    await bench.expect_core(0, SM_STATUS, 0)
    await bench.expect_core(1, PC, 3)


@cocotb.test(skip=NUM_CORES < 2)
async def test_irq_set_wait_halt_resume_preserves_one_time_issue(dut):
    bench = await setup(dut)
    bench.program[:3] = [irq(5, wait=True), irq(5, clear=True), irq(5, clear=True)]
    await bench.write(INIT_PC, 1, 1 << 1)
    await bench.soft_reset()
    await bench.start((0, 1))
    await bench.tick(2)
    await bench.hold(1)
    await bench.expect_core(0, SM_STATUS, stalled_status(2, running=False))
    await bench.expect_sync(0x20, True)
    await bench.start((1,))
    await bench.tick(2)
    await bench.expect_sync(0, True)
    await bench.sync_write(SYNC_STATUS, 1)
    await bench.start((0,))
    await FallingEdge(dut.clk)
    dut.i_halt_req.value = 0
    await bench.tick()
    await bench.expect_core(0, PC, 1)
    await bench.expect_core(0, SM_STATUS, 0)
    await bench.expect_sync(0, False)


@cocotb.test()
async def test_core_soft_resets_preserve_flags_collision_and_configuration(dut):
    bench = await setup(dut)
    await bench.execute({core: irq(0) for core in range(NUM_CORES)})
    await bench.sync_write(SYNC_SET, 0xA5)
    values = [(0x29 + core) & 0xFF for core in range(NUM_CORES)]
    for core, value in enumerate(values):
        await bench.write(X, value, 1 << core)
    await bench.write(CLKDIV, 0x0038)
    await bench.soft_reset(1 << (NUM_CORES - 1))
    values[-1] = 0
    await bench.expect(X, values)
    await bench.expect(CLKDIV, 0x0038)
    await bench.expect_sync(0xA5, NUM_CORES > 1)
    await bench.write(SM_CTRL, RESET, 1)
    values[0] = 0
    await bench.expect(X, values)
    await bench.expect_sync(0xA5, NUM_CORES > 1)
    await bench.soft_reset()
    await bench.expect(X, 0)
    await bench.expect(CLKDIV, 0x0038)
    await bench.expect_sync(0xA5, NUM_CORES > 1)
    await bench.reset()
    await bench.expect_sync(0, False)
    await bench.expect(CLKDIV, 0x0010)


@cocotb.test(skip=NUM_CORES < 2)
async def test_reset_on_commit_removes_only_reset_core_sync_request(dut):
    bench = await setup(dut)
    low = NUM_CORES - 1
    for kind, target in product(("external", "local"), (0, low)):
        await bench.seed_flags(0)
        entries = await bench.prepare({0: irq(7), low: irq(7, clear=True)})
        await bench.start(entries)
        await bench.tick()
        await FallingEdge(dut.clk)
        if kind == "external":
            dut.i_soft_reset.value = 1 << target
        else:
            dut.i_csr_addr.value = SM_CTRL
            dut.i_csr_din.value = RESET
            dut.i_csr_wren.value = 1 << target
        await bench.tick()
        dut.i_soft_reset.value = 0
        dut.i_csr_wren.value = 0
        await bench.expect_sync(0 if target == 0 else 0x80, False)
        for core, entry in entries.items():
            await bench.expect_core(core, PC, entry if core == target else entry + 1)
        await bench.wait_halted()


@cocotb.test()
async def test_full_reset_suppresses_sync_and_link_commit_edge_effects(dut):
    bench = await setup(dut)
    for word in (irq(3), 0x80A0, 0x8020):
        await bench.reset()
        await bench.seed_flags(0xA5)
        bench.program[0] = word
        for core in range(NUM_CORES):
            bench.tx_queues[core].clear()
            bench.tx_queues[core].append((0x61 + core) & 0xFF)
        await bench.write(ISR, 0xD3)
        await bench.start(range(NUM_CORES))
        await bench.tick()
        before = len(bench.events)
        await FallingEdge(dut.clk)
        dut.rst.value = 1
        dut.i_sync_csr_addr.value = SYNC_SET
        dut.i_sync_csr_din.value = 0xFF
        dut.i_sync_csr_wren.value = 1
        await bench.tick()
        dut.i_sync_csr_wren.value = 0
        dut.rst.value = 0
        await bench.tick()
        await bench.expect_sync(0, False)
        for address, value in RESET_VALUES.items():
            await bench.expect(address, value)
        assert len(bench.events) == before
        assert int(dut.o_sync_csr_error.value) == 0


@cocotb.test()
async def test_running_core_rejects_state_writes_without_blocking_idle_peers(dut):
    bench = await setup(dut)
    bench.program[0] = wait_pin()
    await bench.start((0,))
    await bench.tick(2)
    await bench.write(X, 0x5A, ALL_CORES, rejected=1)
    await bench.expect(X, [0] + [0x5A] * (NUM_CORES - 1))
    await bench.expect_core(0, SM_STATUS, stalled_status(1))
    await bench.sync_write(SYNC_STATE, 0xFF, rejected=True)
    await bench.sync_write(SYNC_STATUS, 1)
    await bench.expect_sync(0, False)
    await bench.expect_core(0, PC, 0)
    bench.virt_in[0] = 1
    await bench.tick(2)
    await bench.expect_core(0, PC, 1)


@cocotb.test()
async def test_independent_integer_and_fractional_dividers_order_sync_edges(dut):
    bench = await setup(dut)
    words = {core: irq(2, clear=bool(core % 2)) for core in range(NUM_CORES)}
    entries = await bench.prepare(words)
    for core in range(NUM_CORES):
        await bench.write(CLKDIV, 16 + 8 * core, 1 << core)
    await bench.start(entries)
    for elapsed in range(1, NUM_CORES + 5):
        await bench.tick()
        committed = [core for core in range(NUM_CORES) if elapsed >= 2 + core]
        expected = 0 if not committed else (1 - committed[-1] % 2) << 2
        await bench.expect_sync(expected, False)
        pcs = await bench.read(PC)
        for core, entry in entries.items():
            assert pcs[core] == entry + int(core in committed), f"Core {core} committed at the wrong divider edge"
    await bench.completed(entries)


@cocotb.test(skip=NUM_CORES < 2)
async def test_delay_draining_does_not_repeat_a_sync_write(dut):
    bench = await setup(dut)
    bench.program[:2] = [irq(0, delay=3), irq(0, clear=True)]
    await bench.write(INIT_PC, 1, 1 << 1)
    await bench.soft_reset()
    await bench.start((0,))
    await bench.tick(2)
    await bench.expect_sync(1, False)
    await bench.start((1,))
    await bench.tick(2)
    await bench.expect_sync(0, False)
    assert (await bench.read(SM_STATUS))[0] & RUNNING
    await bench.wait_halted()
    await bench.expect_sync(0, False)
    await bench.expect_core(0, PC, 1)
    await bench.expect_core(1, PC, 2)


@cocotb.test()
async def test_reserved_irq_forms_never_change_flags_or_collide(dut):
    bench = await setup(dut)
    await bench.seed_flags(0x69)
    for word in (0xC008, 0xC010, 0xC0FF):
        await bench.execute({core: word for core in range(NUM_CORES)})
        await bench.expect_sync(0x69, False)
        await bench.expect(SM_STATUS, INVALID)


@cocotb.test()
async def test_continuous_token_ring_program_exercises_sync_link_and_pins(dut):
    bench = await setup(dut)
    active = min(NUM_CORES, 4)
    rounds = 3
    values = [[(0x80 + 19 * core + 7 * iteration) & 0xFF for iteration in range(rounds)] for core in range(active)]
    for core in range(active):
        base = 7 * core
        bench.program[base:base + 7] = [
            wait_irq(core), 0x80A0, mov(5, 7), mov(0, 7),
            0x8020, irq((core + 1) % active), base,
        ]
        bench.tx_queues[core].extend(values[core])
        await bench.write(INIT_PC, base, 1 << core)
        await bench.write(CLKDIV, (16, 32, 56, 80)[core], 1 << core)
    await bench.soft_reset()
    await bench.write(PIN_DIR, 0xFF, core_mask(range(active)))
    await bench.seed_flags(1)
    await bench.start(range(active), CONT_RUN)
    await Timer(20_000, unit="ns")
    expected = [(core, values[core][iteration]) for iteration in range(rounds) for core in range(active)]
    assert bench.transfers("tx") == expected
    assert bench.transfers("rx") == expected
    assert not bench.transfers("fallback") and not bench.transfers("drop")
    await bench.expect_sync(0, False)
    await bench.expect(PIN_OUT, [row[-1] for row in values] + [0] * (NUM_CORES - active))
    assert int(dut.o_virt_out.value) == pack([row[-1] for row in values] + [0] * (NUM_CORES - active), 8)
    await bench.expect_core(0, SM_STATUS, stalled_status(3))
    for core in range(1, active):
        await bench.expect_core(core, SM_STATUS, stalled_status(2))
    await bench.hold(core_mask(range(active)))
    await bench.wait_halted()
    await bench.expect_sync(0, False)
    assert len(bench.transfers("rx")) == active * rounds


def write_divider_model(destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("""// Explicit test-only Q12.4 divider from the enable/hold contract.
`default_nettype none

module clock_divider (
    input  logic        clk,
    input  logic        rst,
    input  logic        i_enable,
    input  logic [15:0] divisor,
    output logic        clk_en
);
    logic [15:0] phase;
    logic [16:0] sum;
    logic [16:0] remainder;

    assign sum = {1'b0, phase} + 17'd16;
    assign remainder = sum - {1'b0, divisor};
    assign clk_en = !rst && i_enable && sum >= {1'b0, divisor};

    always_ff @(posedge clk) begin
        if (rst) begin
            phase <= 16'd0;
        end else if (i_enable) begin
            if (clk_en) begin
                phase <= remainder[15:0];
            end else begin
                phase <= sum[15:0];
            end
        end
    end
endmodule

`default_nettype wire

""")
    print("Using a TEST-ONLY divider; actual divider integration remains unverified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-divider-model", type=Path, required=True)
    args = parser.parse_args()
    write_divider_model(args.write_divider_model)

