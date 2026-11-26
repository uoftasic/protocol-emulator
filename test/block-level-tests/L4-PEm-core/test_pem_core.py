"""Black-box regression for the complete PEm core ISA, CSRs, and sequencing.

Expectations come from .CONTEXT/CONTEXT.md, baseline v0.8. All DUT access is
through public ports, including architectural register reads through CSR.
Run `make` with the supplied divider, or `make test-model` to explicitly use
the test-only Q12.4 divider while its owner's source is empty.
Use `make coverage USE_DIVIDER_MODEL=1` for optional functional coverage.
"""

from collections import Counter
from functools import wraps
from pathlib import Path
import argparse
import html
import json
import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Event, FallingEdge, ReadOnly, RisingEdge, Timer
from cocotb.utils import get_sim_time


SM_CTRL, SM_STATUS, PC, WRAP, SHIFT_CTRL, SHIFT_THRESH, CLKDIV = range(7)
X, Y, ISR, OSR, SHIFT_COUNT, INIT_PC, PIN_CTRL, PIN_OUT, PIN_DIR = range(7, 16)

STEP = 0x0001
CONT_RUN = 0x0002
RESET = 0x0004
RUNNING = 0x0001
STALLED = 0x0002
WAITING_SYNC = 0x0004
INVALID = 0x0008
NOP = 0xA042  # MOV Y,Y, copy, zero common field.
WAIT_HIGH_PIN0 = 0x20A0
CORE_ID = int(os.environ.get("CORE_ID", "0"))
CLOCK_PERIOD_NS = 10
SETTLE_PS = 1

RESET_VALUES = {addr: 0 for addr in range(16)}
RESET_VALUES.update({CLKDIV: 0x0010, SHIFT_COUNT: 0x0080})
STATE_CSRS = (X, Y, ISR, OSR, PIN_OUT, PIN_DIR)
CONFIG_VALUES = {
    WRAP: (1 << 10) | (27 << 5) | 3,
    SHIFT_CTRL: 0x000F,
    SHIFT_THRESH: 0x002B,
    CLKDIV: 0x0038,
    INIT_PC: 11,
    PIN_CTRL: (2 << 8) | (5 << 4) | 3,
}
LEGAL_WRITES = {
    WRAP: 0x0441,
    SHIFT_CTRL: 0x000F,
    SHIFT_THRESH: 0x002B,
    CLKDIV: 0x0020,
    INIT_PC: 9,
    PIN_CTRL: 0x0103,
    **{addr: 0x005A for addr in STATE_CSRS},
}
STATE_SEED = {X: 0xA6, Y: 0x59, ISR: 0xC3, OSR: 0x96, PIN_OUT: 0xA5, PIN_DIR: 0x5A}


class FunctionalCoverage:
    """Small contract bins sampled through existing black-box checks."""

    def __init__(self):
        self.path = Path(os.environ["PEM_CORE_COVERAGE_DIR"])
        self.path.mkdir(parents=True, exist_ok=True)
        self.bins = {
            "instruction": ("JMP", "WAIT", "IN", "OUT", "PUSH", "PULL", "MOV", "IRQ", "SET", "NOP"),
            "reserved_instruction": ("WAIT", "IN", "OUT", "PUSH_PULL", "MOV", "IRQ", "SET"),
            "csr_read": tuple(f"0x{address:X}" for address in range(16)),
            "csr_write": tuple(
                f"0x{address:X}:{result}"
                for address in range(16)
                for result in ("accepted", "rejected")
                if (result == "accepted" and address not in (PC, SHIFT_COUNT))
                or (result == "rejected" and address not in (SM_CTRL, SM_STATUS))
            ),
            "control": ("STEP", "CONT_RUN", "HALT", "FULL_RESET", "LOCAL_RESET", "EXTERNAL_RESET"),
            "status": ("halted", "running", "invalid") + tuple(
                f"{phase}:{cause}"
                for phase in ("stalled", "frozen")
                for cause in ("pin", "irq", "tx", "rx")
            ),
            "link": ("tx", "rx", "fallback", "drop", "autopull", "autopush"),
            "sync": ("set", "clear", "wait_clear"),
            "jmp_condition": tuple(str(value) for value in range(8)),
            "wait_mode": ("PIN:0", "PIN:1", "IRQ:0:absolute", "IRQ:1:absolute",
                          "IRQ:0:relative", "IRQ:1:relative"),
            "mov_source": tuple(str(value) for value in (0, 1, 2, 3, 4, 6, 7)),
            "mov_destination": tuple(str(value) for value in (0, 1, 2, 5, 6, 7)),
            "mov_operation": ("copy", "invert", "reverse"),
            "set_destination": tuple(str(value) for value in (0, 1, 2, 4, 5, 7)),
            "shift": tuple(
                f"{family}:{direction}:{count}"
                for family in ("IN", "OUT")
                for direction in ("left", "right")
                for count in range(1, 9)
            ),
            "shift_count": tuple(
                f"{register}:{count}"
                for register in ("ISR", "OSR")
                for count in range(9)
            ),
            "push_pull_mode": tuple(
                f"{family}:conditional={conditional}:block={block}"
                for family in ("PUSH", "PULL")
                for conditional in range(2)
                for block in range(2)
            ),
            "common": ("delay_zero", "delay_nonzero", "sideset_0", "sideset_1", "sideset_2"),
        }
        self.counts = Counter()
        self.pending = Counter()
        self.passed = []
        self.failed = []
        self.write_report()

    def hit(self, group, value):
        if value not in self.bins[group]:
            raise AssertionError(f"Undefined functional coverage bin: {group}/{value}")
        self.pending[group, value] += 1

    def checked_status(self, value):
        self.hit("status", "running" if value & RUNNING else "halted")
        if value & INVALID:
            self.hit("status", "invalid")
        if value & STALLED:
            cause = {1: "pin", 2: "irq", 3: "tx", 4: "rx"}.get((value >> 5) & 7)
            if cause:
                self.hit("status", f'{"stalled" if value & RUNNING else "frozen"}:{cause}')

    def instruction(self, word, shift_ctrl, pin_ctrl):
        opcode, operands = word >> 13, word & 255
        selection = operands >> 5
        valid = True
        family = ("JMP", "WAIT", "IN", "OUT", "PUSH_PULL", "MOV", "IRQ", "SET")[opcode]
        if opcode == 1:
            source = (operands >> 5) & 3
            valid = (source == 1 and not operands & 0x18) or (source == 2 and not operands & 8)
        elif opcode == 2:
            valid = selection not in (4, 5) and not operands & 0x18
        elif opcode == 3:
            valid = selection != 7 and not operands & 0x18
        elif opcode == 4:
            valid = not operands & 31
        elif opcode == 5:
            valid = selection not in (3, 4) and (operands & 7) != 5 and ((operands >> 3) & 3) != 3
        elif opcode == 6:
            valid = not operands & 0x18
        elif opcode == 7:
            valid = selection not in (3, 6) and (selection not in (5, 7) or not operands & 0x18)
        if not valid:
            self.hit("reserved_instruction", family)
        else:
            if opcode == 4:
                family = "PULL" if operands & 128 else "PUSH"
            self.hit("instruction", family)
            if (word & 0xE0FF) == NOP:
                self.hit("instruction", "NOP")
            if opcode == 0:
                self.hit("jmp_condition", str(selection))
            elif opcode == 1:
                polarity = operands >> 7
                mode = f"PIN:{polarity}" if source == 1 else f'IRQ:{polarity}:{"relative" if operands & 16 else "absolute"}'
                self.hit("wait_mode", mode)
            elif opcode in (2, 3):
                direction_bit = 1 if opcode == 2 else 0
                direction = "right" if shift_ctrl & (1 << direction_bit) else "left"
                self.hit("shift", f"{family}:{direction}:{operands & 7 or 8}")
            elif opcode == 4:
                self.hit("push_pull_mode", f"{family}:conditional={(operands >> 6) & 1}:block={(operands >> 5) & 1}")
            elif opcode == 5:
                self.hit("mov_source", str(operands & 7))
                self.hit("mov_destination", str(selection))
                self.hit("mov_operation", ("copy", "invert", "reverse")[(operands >> 3) & 3])
            elif opcode == 7:
                self.hit("set_destination", str(selection))
        sideset = (pin_ctrl >> 8) & 3
        delay = (word >> 8) & ((1 << (5 - sideset)) - 1)
        self.hit("common", "delay_nonzero" if delay else "delay_zero")
        self.hit("common", f"sideset_{sideset}")

    def finish(self, name, passed):
        if passed:
            self.counts.update(self.pending)
            self.passed.append(name)
        else:
            self.failed.append(name)
        self.pending.clear()
        self.write_report()

    def write_report(self):
        groups = {}
        for group, values in self.bins.items():
            hits = {value: self.counts[group, value] for value in values}
            covered = sum(count > 0 for count in hits.values())
            groups[group] = {
                "covered": covered,
                "total": len(values),
                "percent": round(100 * covered / len(values), 2),
                "hits": hits,
                "missing": [value for value, count in hits.items() if not count],
            }
        covered = sum(group["covered"] for group in groups.values())
        total = sum(group["total"] for group in groups.values())
        summary = {
            "kind": "black-box functional coverage; not RTL line/branch coverage",
            "core_id": CORE_ID,
            "divider_model": os.environ.get("PEM_CORE_DIVIDER_MODEL") == "1",
            "passed_tests": self.passed,
            "failed_tests": self.failed,
            "covered": covered,
            "total": total,
            "percent": round(100 * covered / total, 2),
            "groups": groups,
        }
        (self.path / "coverage.json").write_text(json.dumps(summary, indent=2) + "\n\n")
        title = f"PEm core functional coverage — CORE_ID={CORE_ID}: {covered}/{total} bins ({summary['percent']}%)"
        lines = [
            title,
            f"Passing tests: {len(self.passed)}; failed tests: {len(self.failed)}",
            "Only passing-test observations count. No RTL line/branch coverage is measured.",
            f"Divider: {'test-only model' if summary['divider_model'] else 'owner implementation'}",
            "",
        ]
        rows = []
        for name, group in groups.items():
            missing = ", ".join(group["missing"]) or "none"
            lines.append(f"{name}: {group['covered']}/{group['total']} ({group['percent']}%); missing: {missing}")
            rows.append(f"<tr><td>{name}</td><td>{group['covered']}/{group['total']}</td>"
                        f"<td>{group['percent']}%</td><td>{html.escape(missing)}</td></tr>")
        (self.path / "coverage.txt").write_text("\n".join(lines) + "\n\n")
        document = f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><title>PEm functional coverage</title>
<style>body {{font: 15px sans-serif; margin: 24px; color: #172033;}}
table {{border-collapse: collapse;}} th, td {{padding: 8px 14px; border: 1px solid #cbd5e1; text-align: left;}}
th {{background: #e2e8f0;}}</style>
<h1>{html.escape(title)}</h1>
<p>{html.escape(lines[1])}. {html.escape(lines[3])}.</p>
<p>Only passing-test observations count. Bins come from checked instruction completion,
CSR accesses/readback, and public Link/sync/reset events. This is functional coverage,
not RTL line/branch coverage or a proof of correctness.</p>
<table><tr><th>Group</th><th>Hit bins</th><th>Coverage</th><th>Missing bins</th></tr>{"".join(rows)}</table>
</html>

'''
        (self.path / "coverage.html").write_text(document)


FUNCTIONAL_COVERAGE = None
if __name__ != "__main__" and os.environ.get("PEM_CORE_COVERAGE") == "1":
    FUNCTIONAL_COVERAGE = FunctionalCoverage()


def core_test():
    if FUNCTIONAL_COVERAGE is None:
        return cocotb.test()

    def decorate(function):
        @wraps(function)
        async def measured(dut):
            passed = False
            try:
                await function(dut)
                passed = True
            finally:
                FUNCTIONAL_COVERAGE.finish(function.__name__, passed)
        return cocotb.test()(measured)
    return decorate


def mov_word(destination, operation, source, common=0):
    return (5 << 13) | (common << 8) | (destination << 5) | (operation << 3) | source


def set_word(destination, immediate, common=0):
    return (7 << 13) | (common << 8) | (destination << 5) | immediate


def jmp_word(condition, target, common=0):
    return (common << 8) | (condition << 5) | target


def wait_word(polarity, source, index, common=0):
    return (1 << 13) | (common << 8) | (polarity << 7) | (source << 5) | index


def in_word(source, count, common=0):
    return (2 << 13) | (common << 8) | (source << 5) | (count & 7)


def out_word(destination, count, common=0):
    return (3 << 13) | (common << 8) | (destination << 5) | (count & 7)


def push_pull_word(pull, conditional=0, block=1, common=0):
    return (4 << 13) | (common << 8) | (pull << 7) | (conditional << 6) | (block << 5)


def irq_word(flag, clear=0, wait=0, relative=0, common=0):
    return (6 << 13) | (common << 8) | (clear << 7) | (wait << 6) | (relative << 5) | flag


def effective_flag(flag, relative):
    return (flag & 4) | (((flag & 3) + (CORE_ID if relative else 0)) & 3)


def blocked_status(cause, running=True):
    return (RUNNING if running else 0) | STALLED | (WAITING_SYNC if cause == 2 else 0) | (cause << 5)


def shift_in(previous, source, count, right):
    bits = source & ((1 << count) - 1)
    if right:
        return (previous >> count) | (bits << (8 - count))
    return ((previous << count) | bits) & 0xFF


def shift_out(previous, count, right):
    if right:
        return previous & ((1 << count) - 1), previous >> count
    return previous >> (8 - count), (previous << count) & 0xFF


def pin_vector(value, base):
    return sum(((value >> ((base + bit) % 8)) & 1) << bit for bit in range(8))


def write_pin_vector(previous, value, base, width):
    for bit in range(width):
        lane = (base + bit) % 8
        previous = (previous & ~(1 << lane)) | (((value >> bit) & 1) << lane)
    return previous


def mov_transform(value, operation):
    if operation == 0:
        return value
    if operation == 1:
        return value ^ 0xFF
    return int(f"{value:08b}"[::-1], 2)


def common_effects(common, count, base, old_pins):
    delay_width = 5 - count
    delay = common & ((1 << delay_width) - 1)
    mask = ((1 << count) - 1) << base
    sideset = (common >> delay_width) << base
    return delay, (old_pins & ~mask) | sideset


class PEmBench:
    """Drive a common clock and an asynchronous IMEM through module ports."""

    def __init__(self, dut, allow_transfers=False):
        self.dut = dut
        self.program = [NOP] * 32
        self.cycles = 0
        self.last_rising_ps = 0
        self.allow_transfers = allow_transfers
        self.events = []
        self.event_cycles = []
        self.tx_queue = None
        self.flags = None
        self.apply_irq_writes = True
        self.coverage_instruction = None
        self.coverage_shift_ctrl = 0
        self.coverage_pin_ctrl = 0
        self.coverage_reset_seen = False
        self.cycle_done = Event()
        for name in (
            "clk", "rst", "i_soft_reset", "i_halt_req", "i_csr_addr",
            "i_csr_din", "i_csr_wren", "i_imem_din", "i_tx_fifo_data",
            "i_tx_fifo_valid", "i_rx_fifo_ready", "i_virt_in", "i_irq_din",
        ):
            getattr(dut, name).value = 0
        self.clock = Clock(dut.clk, CLOCK_PERIOD_NS, unit="ns")
        self.clock.start(start_high=False)
        self.environment_task = cocotb.start_soon(self.run_environment())

    async def tick(self, count=1):
        target = self.cycles + count
        while self.cycles < target:
            self.cycle_done.clear()
            await self.cycle_done.wait()

    async def run_environment(self):
        while True:
            await Timer(SETTLE_PS, unit="ps")
            self.drive_environment()
            await RisingEdge(self.dut.clk)
            self.last_rising_ps = int(get_sim_time(unit="ps"))
            if not self.allow_transfers:
                self.assert_no_transfers()
            edge_events = self.capture_transfers()
            await Timer(SETTLE_PS, unit="ps")
            for kind, value in edge_events:
                if kind == "tx" and self.tx_queue is not None:
                    assert self.tx_queue.pop(0) == value
                if kind == "irq" and self.flags is not None and self.apply_irq_writes:
                    index, level, _ = value
                    self.flags = (self.flags & ~(1 << index)) | (level << index)
            self.cycles += 1
            self.drive_environment()
            await Timer(SETTLE_PS, unit="ps")
            if not self.allow_transfers:
                self.assert_no_transfers()
            self.cycle_done.set()
            await FallingEdge(self.dut.clk)

    def drive_environment(self):
        self.drive_endpoints()
        address = 0 if int(self.dut.rst.value) else int(self.dut.o_imem_addr.value)
        self.dut.i_imem_din.value = self.program[address]

    def drive_endpoints(self):
        if self.tx_queue is not None:
            self.dut.i_tx_fifo_valid.value = bool(self.tx_queue)
            self.dut.i_tx_fifo_data.value = self.tx_queue[0] if self.tx_queue else 0
        if self.flags is not None:
            self.dut.i_irq_din.value = (self.flags >> int(self.dut.o_irq_addr.value)) & 1

    def capture_transfers(self):
        self.coverage_reset_seen = False
        edge_events = []
        if int(self.dut.o_tx_fifo_ready.value):
            assert int(self.dut.i_tx_fifo_valid.value), "TX request without a byte"
            edge_events.append(("tx", int(self.dut.i_tx_fifo_data.value)))
        if int(self.dut.o_rx_fifo_valid.value):
            assert int(self.dut.i_rx_fifo_ready.value), "RX transfer without space"
            edge_events.append(("rx", int(self.dut.o_rx_fifo_data.value)))
        if int(self.dut.o_tx_fallback_event.value):
            assert not int(self.dut.o_tx_fifo_ready.value)
            edge_events.append(("fallback", None))
        if int(self.dut.o_rx_drop_event.value):
            assert not int(self.dut.o_rx_fifo_valid.value)
            edge_events.append(("drop", None))
        if int(self.dut.o_irq_wait_clear.value):
            assert int(self.dut.o_irq_wren.value) and not int(self.dut.o_irq_dout.value)
        if int(self.dut.o_irq_wren.value):
            edge_events.append(("irq", (
                int(self.dut.o_irq_addr.value), int(self.dut.o_irq_dout.value),
                int(self.dut.o_irq_wait_clear.value),
            )))
        if int(self.dut.rst.value) or int(self.dut.i_soft_reset.value) or (
            int(self.dut.i_csr_wren.value) and int(self.dut.i_csr_addr.value) == SM_CTRL
            and int(self.dut.i_csr_din.value) & RESET
        ):
            assert not edge_events, "reset edge produced a transfer/request"
            if FUNCTIONAL_COVERAGE is not None:
                self.coverage_reset_seen = True
                self.coverage_instruction = None
                if int(self.dut.rst.value):
                    self.coverage_shift_ctrl = 0
                    self.coverage_pin_ctrl = 0
                    FUNCTIONAL_COVERAGE.hit("control", "FULL_RESET")
                else:
                    FUNCTIONAL_COVERAGE.hit("control", "EXTERNAL_RESET" if int(self.dut.i_soft_reset.value) else "LOCAL_RESET")
        if FUNCTIONAL_COVERAGE is not None:
            for kind, value in edge_events:
                if kind == "irq":
                    FUNCTIONAL_COVERAGE.hit("sync", "wait_clear" if value[2] else "set" if value[1] else "clear")
                else:
                    FUNCTIONAL_COVERAGE.hit("link", kind)
                    if self.coverage_instruction is not None:
                        opcode = self.coverage_instruction[0] >> 13
                        if (kind, opcode) in (("tx", 3), ("rx", 2)):
                            FUNCTIONAL_COVERAGE.hit("link", "autopull" if kind == "tx" else "autopush")
        self.events.extend(edge_events)
        self.event_cycles.extend([self.cycles + 1] * len(edge_events))
        return edge_events

    def assert_no_transfers(self):
        for name in (
            "o_tx_fifo_ready", "o_tx_fallback_event", "o_rx_fifo_valid",
            "o_rx_drop_event", "o_irq_wren", "o_irq_wait_clear",
        ):
            assert int(getattr(self.dut, name).value) == 0, f"unexpected {name}"

    async def reset(self):
        self.dut.i_csr_wren.value = 0
        self.dut.i_soft_reset.value = 0
        self.dut.i_halt_req.value = 0
        self.dut.rst.value = 1
        await self.tick(2)
        self.dut.rst.value = 0
        await self.tick()

    async def read(self, address):
        self.dut.i_csr_addr.value = address
        await Timer(SETTLE_PS, unit="ps")
        value = int(self.dut.o_csr_dout.value)
        if FUNCTIONAL_COVERAGE is not None:
            FUNCTIONAL_COVERAGE.hit("csr_read", f"0x{address:X}")
        return value

    async def expect(self, address, expected):
        actual = await self.read(address)
        assert actual == expected, (
            f"CSR 0x{address:X}: expected 0x{expected:04X}, got 0x{actual:04X}"
        )
        if FUNCTIONAL_COVERAGE is not None:
            if address == SM_STATUS:
                FUNCTIONAL_COVERAGE.checked_status(actual)
            elif address == SHIFT_COUNT:
                FUNCTIONAL_COVERAGE.hit("shift_count", f"ISR:{actual & 15}")
                FUNCTIONAL_COVERAGE.hit("shift_count", f"OSR:{(actual >> 4) & 15}")

    async def expect_bank(self, values):
        for address, expected in values.items():
            await self.expect(address, expected)

    async def snapshot(self):
        return {address: await self.read(address) for address in (*STATE_CSRS, SHIFT_COUNT, PC)}

    async def expect_state(self, expected):
        await self.expect_bank(expected)
        assert int(self.dut.o_virt_out.value) == expected[PIN_OUT]
        assert int(self.dut.o_virt_outen.value) == expected[PIN_DIR]

    async def write(self, address, value, rejected=False):
        await FallingEdge(self.dut.clk)
        self.dut.i_csr_addr.value = address
        self.dut.i_csr_din.value = value
        self.dut.i_csr_wren.value = 1
        await self.tick()
        self.dut.i_csr_wren.value = 0
        actual = int(self.dut.o_csr_error.value)
        assert actual == int(rejected), (
            f"CSR write 0x{address:X}=0x{value:04X}: error={actual}, "
            f"expected {int(rejected)}"
        )
        if FUNCTIONAL_COVERAGE is not None and not self.coverage_reset_seen:
            FUNCTIONAL_COVERAGE.hit("csr_write", f'0x{address:X}:{"rejected" if actual else "accepted"}')
            if not actual:
                if address == SHIFT_CTRL:
                    self.coverage_shift_ctrl = value & 15
                elif address == PIN_CTRL:
                    self.coverage_pin_ctrl = value & 0x3FF
                elif address == SM_CTRL and not value & RESET:
                    FUNCTIONAL_COVERAGE.hit("control", "CONT_RUN" if value & CONT_RUN else "STEP" if value & STEP else "HALT")
        await Timer(SETTLE_PS, unit="ps")

    def cover_start(self, word):
        if FUNCTIONAL_COVERAGE is not None:
            self.coverage_instruction = (word, self.coverage_shift_ctrl, self.coverage_pin_ctrl)

    def cover_complete(self):
        if FUNCTIONAL_COVERAGE is not None and self.coverage_instruction is not None:
            FUNCTIONAL_COVERAGE.instruction(*self.coverage_instruction)
            self.coverage_instruction = None

    async def wait_halted(self, limit=256):
        for elapsed in range(limit + 1):
            if not (await self.read(SM_STATUS)) & RUNNING:
                return elapsed
            if elapsed != limit:
                await self.tick()
        raise AssertionError(f"RUNNING did not clear within {limit} clocks")

    async def step(self, word=NOP, limit=256):
        self.program[await self.read(PC)] = word
        self.cover_start(word)
        await self.write(SM_CTRL, STEP)
        await self.expect(SM_CTRL, STEP)
        assert (await self.read(SM_STATUS)) & RUNNING, "pending STEP is not running"
        elapsed = await self.wait_halted(limit)
        await self.expect(SM_CTRL, 0)
        self.cover_complete()
        return elapsed


async def setup(dut, allow_transfers=False):
    bench = PEmBench(dut, allow_transfers=allow_transfers)
    await bench.reset()
    return bench


async def record_pin_transitions(dut, transitions):
    while True:
        await dut.o_virt_out.value_change
        await ReadOnly()
        transitions.append((int(get_sim_time(unit="ps")), int(dut.o_virt_out.value)))


async def wait_until_ps(deadline):
    remaining = deadline - int(get_sim_time(unit="ps"))
    assert remaining >= 0, f"missed pin observation deadline by {-remaining} ps"
    if remaining:
        await Timer(remaining, unit="ps")


async def seed_invalid(bench):
    await bench.step(mov_word(3, 0, 2))
    await bench.expect(SM_STATUS, INVALID)


async def seed_state(bench):
    for address, value in STATE_SEED.items():
        await bench.write(address, value)
    return {**STATE_SEED, SHIFT_COUNT: 0, PC: await bench.read(PC)}


async def check_timed_instruction(bench, word, expected, delay):
    before = await bench.snapshot()
    sticky = (await bench.read(SM_STATUS)) & INVALID
    bench.program[before[PC]] = word
    bench.cover_start(word)
    await bench.write(SM_CTRL, STEP)
    await bench.expect_state(before)
    await bench.tick()
    await bench.expect_state(before)
    await bench.expect(SM_CTRL, 0)
    await bench.expect(SM_STATUS, RUNNING | sticky)
    await bench.tick()
    await bench.expect_state(expected)
    await bench.expect(SM_STATUS, (RUNNING if delay else 0) | sticky)
    for elapsed in range(1, 2 * delay + 1):
        await bench.tick()
        await bench.expect_state(expected)
        await bench.expect(SM_STATUS, (RUNNING if elapsed < 2 * delay else 0) | sticky)
    bench.cover_complete()


@core_test()
async def test_reset_bank_and_idle_outputs(dut):
    b = await setup(dut)
    await b.expect_bank(RESET_VALUES)
    assert int(dut.o_imem_addr.value) == 0
    assert int(dut.o_virt_out.value) == 0
    assert int(dut.o_virt_outen.value) == 0
    assert int(dut.o_csr_error.value) == 0
    for value in (0x00, 0xFF, 0xA5, 0x5A):
        dut.i_virt_in.value = value
        dut.i_tx_fifo_data.value = value
        dut.i_tx_fifo_valid.value = value & 1
        dut.i_rx_fifo_ready.value = (value >> 1) & 1
        dut.i_irq_din.value = (value >> 2) & 1
        await b.tick(3)
        await b.expect_bank(RESET_VALUES)


@core_test()
async def test_configuration_fields_and_reserved_bits(dut):
    b = await setup(dut)
    for address, value, expected in (
        (WRAP, 0xFFFF, 0x07FF),
        (SHIFT_CTRL, 0xFFFF, 0x000F),
        (SHIFT_THRESH, 0xFFFF, 0x003F),
        (INIT_PC, 0xFFFF, 0x001F),
        (PIN_CTRL, 0xFCFF, 0x0077),
        (SM_CTRL, 0xFFF8, 0x0000),
        (SM_STATUS, 0xFFF7, 0x0000),
    ):
        await b.write(address, value)
        await b.expect(address, expected)
    await b.expect(PC, 0)  # INIT_PC alone does not move PC.
    await b.write(SM_CTRL, RESET)
    await b.expect(PC, 31)
    await b.expect(INIT_PC, 31)
    await b.expect(SM_CTRL, 0)


@core_test()
async def test_writable_byte_state_and_atomic_rejection(dut):
    b = await setup(dut)
    for address in STATE_CSRS:
        for value in (0x00, 0x01, 0x80, 0xA5, 0xFF):
            await b.write(address, value)
            await b.expect(address, value)
            if address == PIN_OUT:
                assert int(dut.o_virt_out.value) == value
            if address == PIN_DIR:
                assert int(dut.o_virt_outen.value) == value
        counts = await b.read(SHIFT_COUNT)
        for value in (0x0100, 0xFF00, 0xFFFF):
            await b.write(address, value, rejected=True)
            await b.expect(address, 0x00FF)
            await b.expect(SHIFT_COUNT, counts)
            await b.tick()
            assert int(dut.o_csr_error.value) == 0, "error must clear on a non-write edge"


@core_test()
async def test_read_only_csrs_and_shift_count_loads(dut):
    b = await setup(dut)
    for address, expected in ((PC, 0), (SHIFT_COUNT, 0x80)):
        for value in (0, 0xFFFF):
            await b.write(address, value, rejected=True)
            await b.expect(address, expected)
    await b.write(OSR, 0x01A5, rejected=True)
    await b.expect(OSR, 0)
    await b.expect(SHIFT_COUNT, 0x80)
    await b.write(ISR, 0x5A)
    await b.expect(SHIFT_COUNT, 0x80)
    await b.write(OSR, 0xA5)
    await b.expect(SHIFT_COUNT, 0)
    await b.write(OSR, 0x01FF, rejected=True)
    await b.expect(OSR, 0xA5)
    await b.expect(SHIFT_COUNT, 0)


@core_test()
async def test_clkdiv_validation_and_clock_independent_csr_access(dut):
    b = await setup(dut)
    for value in (0x0010, 0x0011, 0x0018, 0x0020, 0xFFFF):
        await b.write(CLKDIV, value)
        await b.expect(CLKDIV, value)
        for invalid in range(0x10):
            await b.write(CLKDIV, invalid, rejected=True)
            await b.expect(CLKDIV, value)
    # These writes and resets must work without waiting for an execution enable.
    await b.write(X, 0xA5)
    await b.expect(X, 0xA5)
    await b.write(INIT_PC, 17)
    await b.write(SM_CTRL, RESET)
    await b.expect(X, 0)
    await b.expect(PC, 17)
    await b.expect(CLKDIV, 0xFFFF)


@core_test()
async def test_pin_ctrl_range_validation_is_atomic(dut):
    b = await setup(dut)
    previous = 0
    for count in range(4):
        for base in range(8):
            jmp_pin = 7 - base
            value = 0xFC88 | (count << 8) | (jmp_pin << 4) | base
            legal = count <= 2 and (count == 0 or base + count <= 8)
            await b.write(PIN_CTRL, value, rejected=not legal)
            if legal:
                previous = (count << 8) | (jmp_pin << 4) | base
            await b.expect(PIN_CTRL, previous)


@core_test()
async def test_soft_reset_retains_configuration_and_sticky_event(dut):
    b = await setup(dut)
    for external in (False, True):
        await b.reset()
        await seed_invalid(b)
        for address, value in CONFIG_VALUES.items():
            await b.write(address, value)
        for address in STATE_CSRS:
            await b.write(address, 0xA5)
        await b.expect(SHIFT_COUNT, 0)
        if external:
            dut.i_soft_reset.value = 1
            await b.write(INIT_PC, 29)  # Reset suppresses a simultaneous config write.
            dut.i_soft_reset.value = 0
        else:
            await b.write(SM_CTRL, 0xFFFF)  # RESET wins over STEP and CONT_RUN.
        expected = dict(RESET_VALUES)
        expected.update(CONFIG_VALUES)
        expected.update({PC: CONFIG_VALUES[INIT_PC], SM_STATUS: INVALID})
        await b.expect_bank(expected)
        assert int(dut.o_virt_out.value) == 0
        assert int(dut.o_virt_outen.value) == 0
        await b.tick(10)
        await b.expect_bank(expected)


@core_test()
async def test_full_reset_wins_and_clears_dirty_bank(dut):
    b = await setup(dut)
    await seed_invalid(b)
    for address, value in CONFIG_VALUES.items():
        await b.write(address, value)
    for address in STATE_CSRS:
        await b.write(address, 0xFF)
    dut.rst.value = 1
    dut.i_soft_reset.value = 1
    await b.write(SM_CTRL, 0xFFFF)
    await b.expect_bank(RESET_VALUES)
    assert int(dut.o_csr_error.value) == 0
    dut.rst.value = 0
    dut.i_soft_reset.value = 0
    await b.tick(4)
    await b.expect_bank(RESET_VALUES)


@core_test()
async def test_csr_reads_have_no_side_effects(dut):
    b = await setup(dut)
    await seed_invalid(b)
    await b.write(Y, 0xA5)
    await b.write(PIN_OUT, 0x5A)
    expected = {address: await b.read(address) for address in range(16)}
    for _ in range(4):
        await b.expect_bank(expected)
        await b.tick()
    await b.expect(SM_STATUS, INVALID)


@core_test()
async def test_nop_step_fetch_commit_and_full_delay(dut):
    b = await setup(dut)
    dut.i_tx_fifo_data.value = 0xA5
    dut.i_tx_fifo_valid.value = 1
    dut.i_rx_fifo_ready.value = 1
    dut.i_irq_din.value = 1
    dut.i_virt_in.value = 0xFF
    for delay in (0, 1, 2, 7, 31):
        await b.reset()
        for address in STATE_CSRS:
            await b.write(address, 0xA5)
        preserved = {address: 0xA5 for address in STATE_CSRS}
        preserved[SHIFT_COUNT] = 0
        b.program[0] = NOP | (delay << 8)
        await b.write(SM_CTRL, STEP)
        await b.expect(PC, 0)
        await b.expect(SM_CTRL, STEP)
        await b.tick()  # One fetch enable consumes STEP, without completing work.
        await b.expect(SM_CTRL, 0)
        await b.expect(SM_STATUS, RUNNING)
        await b.expect(PC, 0)
        await b.tick()  # One execute enable commits PC.
        await b.expect(PC, 1)
        await b.expect(SM_STATUS, RUNNING if delay else 0)
        for elapsed in range(1, 2 * delay + 1):
            await b.tick()
            await b.expect(PC, 1)
            await b.expect(SM_STATUS, RUNNING if elapsed < 2 * delay else 0)
            await b.expect_bank(preserved)
        await b.expect_bank(preserved)
        await b.tick(4)
        await b.expect(PC, 1)


@core_test()
async def test_nop_modulo_pc_and_configured_wrap(dut):
    b = await setup(dut)
    for expected in list(range(1, 32)) + [0]:
        await b.step()
        await b.expect(PC, expected)
        assert int(dut.o_imem_addr.value) == expected
    for bottom, top, start, enabled in (
        (1, 3, 1, True), (7, 7, 7, True), (5, 31, 31, True),
        (1, 3, 3, False), (7, 7, 31, False),
    ):
        await b.write(WRAP, (int(enabled) << 10) | (top << 5) | bottom)
        await b.write(INIT_PC, start)
        await b.write(SM_CTRL, RESET)
        expected = start
        for _ in range(5):
            await b.step()
            expected = bottom if enabled and expected == top else (expected + 1) & 31
            await b.expect(PC, expected)


@core_test()
async def test_continuous_nop_has_no_extra_fetch_cycle(dut):
    b = await setup(dut)
    for delay in (0, 1, 7):
        await b.reset()
        b.program[:] = [NOP | (delay << 8)] * 32
        await b.write(SM_CTRL, STEP | CONT_RUN)
        await b.expect(SM_CTRL, CONT_RUN)
        period = 2 + 2 * delay
        for elapsed in range(1, 5 * period + 3):
            await b.tick()
            commits = 0 if elapsed < 2 else 1 + (elapsed - 2) // period
            await b.expect(PC, commits & 31)
            await b.expect(SM_STATUS, RUNNING)
            await b.expect(SM_CTRL, CONT_RUN)
        await b.write(SM_CTRL, 0)
        await b.wait_halted()


@core_test()
async def test_sideset_field_split_and_unselected_pin_retention(dut):
    b = await setup(dut)
    await b.write(Y, 0xA5)
    await b.write(PIN_DIR, 0xC3)
    for count, bases in ((0, (0, 7)), (1, (0, 3, 7)), (2, (0, 3, 6))):
        for base in bases:
            await b.write(PIN_CTRL, (count << 8) | base)
            for common in range(32):
                old_pins = 0x5A if common & 1 else 0xA5
                await b.write(PIN_OUT, old_pins)
                old_pc = await b.read(PC)
                delay, expected_pins = common_effects(common, count, base, old_pins)
                b.program[old_pc] = NOP | (common << 8)
                await b.write(SM_CTRL, STEP)
                await b.tick()
                assert int(dut.o_virt_out.value) == old_pins, "side-set before commit"
                await b.tick()
                await b.expect(PC, (old_pc + 1) & 31)
                await b.expect(PIN_OUT, expected_pins)
                assert int(dut.o_virt_out.value) == expected_pins
                for elapsed in range(1, 2 * delay + 1):
                    dut.i_virt_in.value = elapsed & 0xFF
                    await b.tick()
                    assert int(dut.o_virt_out.value) == expected_pins, "side-set changed in delay"
                    await b.expect(SM_STATUS, RUNNING if elapsed < 2 * delay else 0)
                await b.expect(SM_STATUS, 0)
                await b.expect(Y, 0xA5)
                await b.expect(PIN_DIR, 0xC3)
                assert int(dut.o_virt_outen.value) == 0xC3


@core_test()
async def test_all_reserved_mov_operands_commit_as_invalid_nop(dut):
    b = await setup(dut)
    await b.write(PIN_CTRL, (2 << 8) | 3)
    await b.write(WRAP, (1 << 10) | (3 << 5) | 1)
    for address in STATE_CSRS:
        await b.write(address, 0xA5)
    preserved = {address: 0xA5 for address in (X, Y, ISR, OSR, PIN_DIR)}
    preserved[SHIFT_COUNT] = 0
    for operands in range(256):
        destination, operation, source = operands >> 5, (operands >> 3) & 3, operands & 7
        if destination not in (3, 7) and operation != 3 and source != 5:
            continue
        await b.write(SM_STATUS, INVALID)
        await b.expect(SM_STATUS, 0)
        old_pc = await b.read(PC)
        old_pins = await b.read(PIN_OUT)
        common = operands & 31
        _, expected_pins = common_effects(common, 2, 3, old_pins)
        await b.step(mov_word(destination, operation, source, common))
        await b.expect(PC, 1 if old_pc == 3 else (old_pc + 1) & 31)
        await b.expect(SM_STATUS, INVALID)
        await b.expect(PIN_OUT, expected_pins)
        await b.expect_bank(preserved)
        assert int(dut.o_csr_error.value) == 0, "instruction error is not a CSR error"
    await b.step()
    await b.expect(SM_STATUS, INVALID)  # Legal NOP must not clear the sticky event.


@core_test()
async def test_invalid_event_wins_w1c_and_live_status_is_read_only(dut):
    b = await setup(dut)
    await seed_invalid(b)
    b.program[await b.read(PC)] = mov_word(1, 3, 2)
    await b.write(SM_CTRL, STEP)
    await b.tick()  # Fetch, observed through STEP consumption.
    await b.expect(SM_CTRL, 0)
    await b.write(SM_STATUS, INVALID)  # W1C and a new invalid commit on the same edge.
    await b.expect(SM_STATUS, INVALID)
    await b.write(SM_STATUS, 0xFFF7)
    await b.expect(SM_STATUS, INVALID)
    await b.write(SM_STATUS, 0xFFFF)
    await b.expect(SM_STATUS, 0)
    b.program[:] = [NOP] * 32
    await b.write(SM_CTRL, CONT_RUN)
    await b.write(SM_STATUS, 0xFFFF)
    await b.expect(SM_STATUS, RUNNING)
    await b.write(SM_CTRL, 0)
    await b.wait_halted()


@core_test()
async def test_fresh_step_wins_simultaneous_consumption(dut):
    b = await setup(dut)
    await b.write(SM_CTRL, STEP)
    await b.write(SM_CTRL, STEP)  # Old request fetches; fresh request remains queued.
    await b.expect(PC, 0)
    await b.expect(SM_CTRL, STEP)
    await b.tick()
    await b.expect(PC, 1)
    await b.expect(SM_STATUS, RUNNING)
    await b.tick()
    await b.expect(SM_CTRL, 0)
    await b.expect(PC, 1)
    await b.tick()
    await b.expect(PC, 2)
    await b.expect(SM_STATUS, 0)
    await b.tick(6)
    await b.expect(PC, 2)


@core_test()
async def test_step_queued_during_delay_coalesces(dut):
    b = await setup(dut)
    b.program[0] = NOP | (3 << 8)
    await b.write(SM_CTRL, STEP)
    await b.tick()
    await b.expect(SM_CTRL, 0)
    await b.write(SM_CTRL, STEP)  # Queue during the first instruction's commit.
    await b.expect(PC, 1)
    for _ in range(3):
        await b.write(SM_CTRL, STEP)
        await b.expect(PC, 1)
        await b.expect(SM_CTRL, STEP)
    await b.wait_halted()
    await b.expect(PC, 2)
    await b.expect(SM_CTRL, 0)
    await b.tick(8)
    await b.expect(PC, 2)


@core_test()
async def test_cancel_and_continuous_to_step_block_old_request(dut):
    b = await setup(dut)
    await b.write(SM_CTRL, STEP)
    await b.write(SM_CTRL, 0)  # Cancellation coincides with the possible fetch edge.
    await b.expect(SM_CTRL, 0)
    await b.expect(SM_STATUS, 0)
    await b.tick(6)
    await b.expect(PC, 0)
    await b.write(SM_CTRL, CONT_RUN)
    await b.tick(2)
    await b.expect(PC, 1)
    await b.write(SM_CTRL, STEP)  # Must not fetch through the previous CONT_RUN.
    await b.expect(PC, 1)
    await b.expect(SM_CTRL, STEP)
    await b.tick()
    await b.expect(PC, 1)
    await b.expect(SM_CTRL, 0)
    await b.tick()
    await b.expect(PC, 2)
    await b.expect(SM_STATUS, 0)


@core_test()
async def test_local_halt_and_global_freeze_drain_ready_work(dut):
    b = await setup(dut)
    for external in (False, True):
        await b.reset()
        await b.write(PIN_CTRL, 1 << 8)
        b.program[:] = [NOP | (0x13 << 8)] * 32  # Side-set=1, delay=3.
        await b.write(SM_CTRL, CONT_RUN)
        await b.tick()
        await b.expect(PC, 0)
        if external:
            dut.i_halt_req.value = 1
            await b.tick()
        else:
            await b.write(SM_CTRL, 0)
        await b.expect(PC, 1)
        await b.expect(PIN_OUT, 1)
        for elapsed in range(1, 7):
            await b.tick()
            await b.expect(PC, 1)
            await b.expect(PIN_OUT, 1)
            await b.expect(SM_STATUS, RUNNING if elapsed < 6 else 0)
        await b.tick(4)
        await b.expect(PC, 1)
        await b.expect(SM_CTRL, CONT_RUN if external else 0)
        if external:
            dut.i_halt_req.value = 0
        else:
            await b.write(SM_CTRL, CONT_RUN)
        await b.tick(2)
        await b.expect(PC, 2)


@core_test()
async def test_global_hold_preserves_and_coalesces_pending_commands(dut):
    b = await setup(dut)
    dut.i_halt_req.value = 1
    for _ in range(4):
        await b.write(SM_CTRL, STEP)
        await b.expect(SM_CTRL, STEP)
        await b.expect(SM_STATUS, RUNNING)
        await b.expect(PC, 0)
    await b.write(X, 0xA5, rejected=True)
    dut.i_halt_req.value = 0
    await b.wait_halted()
    await b.expect(PC, 1)
    await b.tick(4)
    await b.expect(PC, 1)
    dut.i_halt_req.value = 1
    await b.write(SM_CTRL, STEP | CONT_RUN)
    await b.expect(SM_CTRL, CONT_RUN)
    await b.expect(SM_STATUS, 0)
    await b.write(X, 0x5A)  # Safe idle under global hold has no retained work.
    await b.expect(X, 0x5A)
    await b.tick(4)
    await b.expect(PC, 1)
    dut.i_halt_req.value = 0
    await b.tick(2)
    await b.expect(PC, 2)
    dut.i_halt_req.value = 1
    await b.tick(3)
    await b.expect(PC, 2)
    await b.expect(SM_CTRL, CONT_RUN)
    await b.expect(SM_STATUS, 0)


@core_test()
async def test_configuration_rejected_for_pending_active_and_delay_work(dut):
    b = await setup(dut)
    b.program[0] = NOP | (31 << 8)
    await b.write(SM_CTRL, STEP)
    for address, value in LEGAL_WRITES.items():
        old_value = await b.read(address)
        await b.write(address, value, rejected=True)
        await b.expect(address, old_value)
        assert (await b.read(SM_STATUS)) & RUNNING
    await b.wait_halted()
    await b.expect(PC, 1)
    for address, value in LEGAL_WRITES.items():
        await b.write(address, value)
        await b.expect(address, value)


@core_test()
async def test_frozen_blocked_work_blocks_configuration_until_reset(dut):
    b = await setup(dut)
    # Retained WAIT PIN must block configuration until explicitly aborted.
    await b.write(PIN_CTRL, 1 << 8)
    b.program[0] = WAIT_HIGH_PIN0 | (0x10 << 8)
    await b.write(SM_CTRL, STEP)
    await b.tick(3)
    await b.expect(PC, 0)
    await b.expect(PIN_OUT, 0)
    assert (await b.read(SM_STATUS)) & RUNNING
    await b.write(SM_CTRL, 0)
    assert not (await b.read(SM_STATUS)) & RUNNING
    for address, value in LEGAL_WRITES.items():
        old_value = await b.read(address)
        await b.write(address, value, rejected=True)
        await b.expect(address, old_value)
    await b.write(SM_CTRL, STEP)
    await b.tick(3)
    await b.expect(SM_CTRL, 0)
    await b.expect(PC, 0)
    await b.expect(PIN_OUT, 0)
    await b.write(SM_CTRL, RESET)
    await b.expect(SM_STATUS, 0)
    await b.write(X, 0xA5)
    await b.expect(X, 0xA5)
    await b.write(PIN_CTRL, 0)
    await b.step()
    await b.expect(PC, 1)


@core_test()
async def test_reset_aborts_fetch_commit_and_delay(dut):
    b = await setup(dut)
    for reset_kind in ("full", "local", "external"):
        for elapsed in (0, 1, 3):
            await b.reset()
            await b.write(INIT_PC, 9)
            await b.write(PIN_CTRL, 1 << 8)
            b.program[0] = NOP | (0x13 << 8)
            await b.write(SM_CTRL, STEP)
            await b.tick(elapsed)
            if reset_kind == "full":
                dut.rst.value = 1
                await b.tick()
                dut.rst.value = 0
            elif reset_kind == "local":
                await b.write(SM_CTRL, RESET | STEP | CONT_RUN)
            else:
                dut.i_soft_reset.value = 1
                await b.tick()
                dut.i_soft_reset.value = 0
            expected_pc = 0 if reset_kind == "full" else 9
            await b.expect(PC, expected_pc)
            await b.expect(SM_CTRL, 0)
            await b.expect(SM_STATUS, 0)
            await b.expect(PIN_OUT, 0)
            await b.expect(SHIFT_COUNT, 0x80)
            await b.tick(12)
            await b.expect(PC, expected_pc)
            await b.expect(PIN_OUT, 0)


@core_test()
async def test_programmed_divider_integer_and_fractional_execution_cadence(dut):
    b = await setup(dut)
    for divisor in (0x10, 0x11, 0x18, 0x1F, 0x20, 0x38, 0xFF):
        await b.reset()
        b.program[:] = [NOP] * 32
        await b.write(CLKDIV, divisor)
        await b.write(SM_CTRL, RESET)
        await b.write(SM_CTRL, CONT_RUN)
        commits = []
        previous_pc = 0
        limit = 40 * ((divisor + 15) // 16) + 8
        for _ in range(limit):
            await b.tick()
            current_pc = await b.read(PC)
            if current_pc != previous_pc:
                assert current_pc == (previous_pc + 1) & 31
                commits.append(b.cycles)
                previous_pc = current_pc
                if len(commits) == 17:
                    break
        assert len(commits) == 17, f"divider 0x{divisor:04X}: execution timed out"
        # Two divider ticks per NOP. Fractional phase may shift the first
        # commit, but 32 enable intervals always span exactly 2*divisor clocks.
        gaps = [right - left for left, right in zip(commits, commits[1:])]
        minimum = (2 * divisor) // 16
        maximum = (2 * divisor + 15) // 16
        assert all(minimum <= gap <= maximum for gap in gaps), (divisor, gaps)
        assert commits[-1] - commits[0] == 2 * divisor, (divisor, gaps)
        await b.write(SM_CTRL, 0)
        await b.wait_halted(limit)


@core_test()
async def test_integer_dividers_exact_pin_cadence_without_drift(dut):
    b = await setup(dut)
    period_ps = CLOCK_PERIOD_NS * 1000
    for divisor in (*range(1, 10), 16, 31):
        await b.reset()
        b.program[:2] = [set_word(0, 1), set_word(0, 0)]
        await b.write(WRAP, (1 << 10) | (1 << 5))
        await b.write(PIN_DIR, 1)
        await b.write(CLKDIV, divisor << 4)
        transitions = []
        monitor = cocotb.start_soon(record_pin_transitions(dut, transitions))
        try:
            await b.write(SM_CTRL, CONT_RUN)
            start_ps = b.last_rising_ps
            interval_ps = 2 * divisor * period_ps
            await wait_until_ps(start_ps + 64 * interval_ps + 2 * SETTLE_PS)
            expected = [
                (start_ps + (index + 1) * interval_ps, 1 - index % 2)
                for index in range(64)
            ]
            assert transitions == expected, (divisor, transitions, expected)
            await b.expect_bank({PC: 0, PIN_OUT: 0, SM_STATUS: RUNNING})
        finally:
            monitor.cancel()
        await b.write(SM_CTRL, RESET)


@core_test()
async def test_integer_divider_boundaries_and_idle_step_latency(dut):
    b = await setup(dut)
    period_ps = CLOCK_PERIOD_NS * 1000
    for divisor in (*range(1, 10), 15, 16, 17, 255, 256, 4095):
        await b.reset()
        b.program[:2] = [set_word(0, 1), set_word(0, 0)]
        await b.write(CLKDIV, divisor << 4)
        transitions = []
        monitor = cocotb.start_soon(record_pin_transitions(dut, transitions))
        try:
            for index in range(2):
                await b.write(SM_CTRL, STEP)
                start_ps = b.last_rising_ps
                await b.tick(divisor - 1)
                await b.expect_bank({SM_CTRL: STEP, PC: index, PIN_OUT: index, SM_STATUS: RUNNING})
                await b.tick()
                await b.expect_bank({SM_CTRL: 0, PC: index, PIN_OUT: index, SM_STATUS: RUNNING})
                await b.tick(divisor - 1)
                await b.expect_bank({PC: index, PIN_OUT: index, SM_STATUS: RUNNING})
                assert len(transitions) == index, "STEP committed before its second enable"
                await b.tick()
                await b.expect_bank({PC: index + 1, PIN_OUT: 1 - index, SM_STATUS: 0})
                assert transitions[-1] == (start_ps + 2 * divisor * period_ps, 1 - index)
                if index == 0:
                    await b.tick(divisor + 7)
                    assert len(transitions) == 1, "idle divider caused a pin transition"
        finally:
            monitor.cancel()


@core_test()
async def test_integer_dividers_delay_and_sideset_pin_timing(dut):
    b = await setup(dut)
    period_ps = CLOCK_PERIOD_NS * 1000
    for divisor in (2, 3, 5, 9):
        for sideset_count in range(3):
            await b.reset()
            delay_width = 5 - sideset_count
            delays = (0, 1, (1 << delay_width) - 1, 2)
            side_high = (1 << sideset_count) - 1
            b.program[:4] = [
                set_word(0, 1 - index % 2,
                         common=delays[index] | ((side_high if index % 2 else 0) << delay_width))
                for index in range(4)
            ]
            await b.write(WRAP, (1 << 10) | (3 << 5))
            await b.write(PIN_CTRL, (sideset_count << 8) | 1)
            await b.write(PIN_DIR, (1 << (sideset_count + 1)) - 1)
            await b.write(CLKDIV, divisor << 4)
            transitions = []
            monitor = cocotb.start_soon(record_pin_transitions(dut, transitions))
            try:
                await b.write(SM_CTRL, CONT_RUN)
                start_ps = b.last_rising_ps
                elapsed_enables = 0
                expected = []
                for index in range(16):
                    elapsed_enables += 2
                    pins = 1 if index % 2 == 0 else side_high << 1
                    expected.append((start_ps + elapsed_enables * divisor * period_ps, pins))
                    elapsed_enables += 2 * delays[index % 4]
                await wait_until_ps(start_ps + elapsed_enables * divisor * period_ps + 2 * SETTLE_PS)
                assert transitions == expected, (divisor, sideset_count, transitions, expected)
                await b.expect_bank({PC: 0, PIN_OUT: side_high << 1, SM_STATUS: RUNNING})
            finally:
                monitor.cancel()
            await b.write(SM_CTRL, RESET)


@core_test()
async def test_integer_divider_uart_frame_has_exact_bit_widths(dut):
    b = await setup(dut)
    period_ps = CLOCK_PERIOD_NS * 1000
    for divisor in (3, 5, 9):
        for data in (0x55, 0xA6):
            await b.reset()
            bits = [0] + [(data >> index) & 1 for index in range(8)] + [1]
            b.program[:11] = [
                set_word(0, 0, common=3),
                *[out_word(0, 1, common=3) for _ in range(8)],
                set_word(0, 1, common=3),
                WAIT_HIGH_PIN0,
            ]
            await b.write(SHIFT_CTRL, 1)
            await b.write(OSR, data)
            await b.write(PIN_DIR, 1)
            await b.write(PIN_OUT, 1)
            await b.write(CLKDIV, divisor << 4)
            transitions = []
            monitor = cocotb.start_soon(record_pin_transitions(dut, transitions))
            try:
                await b.write(SM_CTRL, CONT_RUN)
                start_ps = b.last_rising_ps + 2 * divisor * period_ps
                bit_ps = 8 * divisor * period_ps
                previous = 1
                expected = []
                for index, bit in enumerate(bits):
                    if bit != previous:
                        expected.append((start_ps + index * bit_ps, bit))
                    previous = bit
                    await wait_until_ps(start_ps + index * bit_ps + bit_ps // 2)
                    assert int(dut.o_virt_out.value) == bit, (divisor, data, index)
                await wait_until_ps(start_ps + 10 * bit_ps + 2 * divisor * period_ps + 2 * SETTLE_PS)
                assert transitions == expected, (divisor, data, transitions, expected)
                await b.expect_bank({
                    PC: 10, PIN_OUT: 1, OSR: 0, SHIFT_COUNT: 0x80,
                    SM_STATUS: blocked_status(1),
                })
            finally:
                monitor.cancel()
            await b.write(SM_CTRL, RESET)


@core_test()
async def test_integer_divider_spi_clock_data_and_sample_timing(dut):
    b = await setup(dut)
    period_ps = CLOCK_PERIOD_NS * 1000
    for divisor in (3, 5, 9):
        for tx_data, rx_data in ((0xA5, 0x3C), (0x3C, 0xA6)):
            await b.reset()
            # VIO0=MOSI, VIO1=SCLK, VIO2=CS_N, VIO7=MISO; mode 0.
            b.program[:20] = [
                set_word(5, 7), set_word(0, 0, common=3),
                *[word for _ in range(8) for word in (
                    out_word(0, 1, common=3), in_word(0, 1, common=16 | 3),
                )],
                set_word(0, 4, common=3), wait_word(1, 1, 6),
            ]
            dut.i_virt_in.value = 0
            await b.write(PIN_CTRL, (1 << 8) | 1)
            await b.write(OSR, tx_data)
            await b.write(PIN_DIR, 7)
            await b.write(PIN_OUT, 4)
            await b.write(CLKDIV, divisor << 4)
            transitions = []
            monitor = cocotb.start_soon(record_pin_transitions(dut, transitions))
            try:
                await b.write(SM_CTRL, CONT_RUN)
                start_ps = b.last_rising_ps
                half_bit_ps = 8 * divisor * period_ps
                select_ps = start_ps + 4 * divisor * period_ps
                expected = [(select_ps, 0)]
                previous = 0
                for index in range(8):
                    low_ps = select_ps + (2 * index + 1) * half_bit_ps
                    high_ps = low_ps + half_bit_ps
                    mosi = (tx_data >> (7 - index)) & 1
                    if mosi != previous:
                        expected.append((low_ps, mosi))
                    expected.append((high_ps, mosi | 2))
                    previous = mosi | 2
                    await wait_until_ps(low_ps + SETTLE_PS)
                    assert int(dut.o_virt_out.value) == mosi, (divisor, tx_data, index, "setup")
                    dut.i_virt_in.value = ((rx_data >> (7 - index)) & 1) << 7
                    await wait_until_ps(high_ps + SETTLE_PS)
                    assert int(dut.o_virt_out.value) == mosi | 2, (divisor, tx_data, index, "sample")
                expected.append((select_ps + 17 * half_bit_ps, 4))
                await wait_until_ps(start_ps + 150 * divisor * period_ps + 2 * SETTLE_PS)
                assert transitions == expected, (divisor, tx_data, transitions, expected)
                await b.expect_bank({
                    PC: 19, PIN_OUT: 4, PIN_DIR: 7, ISR: rx_data, OSR: 0,
                    SHIFT_COUNT: 0x88, SM_STATUS: blocked_status(1),
                })
            finally:
                monitor.cancel()
            await b.write(SM_CTRL, RESET)


@core_test()
async def test_set_x_y_all_immediates_preserve_other_state(dut):
    b = await setup(dut)
    for destination, address in ((1, X), (2, Y)):
        for immediate in range(32):
            expected = await seed_state(b)
            expected[address] = immediate
            expected[PC] = (expected[PC] + 1) & 31
            assert await b.step(set_word(destination, immediate)) == 2
            await b.expect_state(expected)
            await b.expect(SM_STATUS, 0)


@core_test()
async def test_set_pins_and_directions_all_bases_and_immediates(dut):
    b = await setup(dut)
    for base in range(8):
        await b.step(set_word(7, base))
        for destination, address in ((0, PIN_OUT), (4, PIN_DIR)):
            for immediate in range(32):
                expected = await seed_state(b)
                expected[address] = write_pin_vector(expected[address], immediate, base, 5)
                expected[PC] = (expected[PC] + 1) & 31
                assert await b.step(set_word(destination, immediate)) == 2
                await b.expect_state(expected)
                await b.expect(SM_STATUS, 0)


@core_test()
async def test_set_base_selectors_and_reset_through_pin_operations(dut):
    b = await setup(dut)
    dut.i_virt_in.value = 0x96
    for base in range(8):
        await b.step(set_word(5, base))
        await b.step(mov_word(1, 0, 0))
        await b.expect(X, pin_vector(0x96, base))
        await b.step(set_word(7, base))
        await b.write(PIN_OUT, 0)
        await b.step(set_word(0, 0x15))
        await b.expect(PIN_OUT, write_pin_vector(0, 0x15, base, 5))
    for reset_kind in ("full", "local", "external"):
        await b.step(set_word(5, 6))
        await b.step(set_word(7, 5))
        if reset_kind == "full":
            await b.reset()
        elif reset_kind == "local":
            await b.write(SM_CTRL, RESET)
        else:
            dut.i_soft_reset.value = 1
            await b.tick()
            dut.i_soft_reset.value = 0
        await b.step(mov_word(1, 0, 0))
        await b.expect(X, 0x96)
        await b.step(set_word(0, 0x15))
        await b.expect(PIN_OUT, 0x15)


@core_test()
async def test_all_reserved_set_operands_are_atomic_invalid_nops(dut):
    b = await setup(dut)
    dut.i_virt_in.value = 0x96
    await b.step(set_word(5, 2))
    await b.step(set_word(7, 5))
    await b.write(PIN_CTRL, (2 << 8) | 4)
    for destination in (3, 5, 6, 7):
        for immediate in range(32):
            if destination in (5, 7) and immediate < 8:
                continue
            await b.write(SM_STATUS, INVALID)
            expected = await seed_state(b)
            await b.write(WRAP, (1 << 10) | (expected[PC] << 5) | 3)
            common = immediate & 3
            delay, expected[PIN_OUT] = common_effects(common, 2, 4, expected[PIN_OUT])
            expected[PC] = 3
            assert await b.step(set_word(destination, immediate, common)) == 2 + 2 * delay
            await b.expect_state(expected)
            await b.expect(SM_STATUS, INVALID)
            # Observe both retained base selectors using legal pin operations.
            await b.step(mov_word(1, 0, 0))
            await b.expect(X, pin_vector(0x96, 2))
            await b.step(mov_word(0, 0, 2))
            expected_pins = write_pin_vector(0, STATE_SEED[Y], 5, 8) & ~0x30
            await b.expect(PIN_OUT, expected_pins)
            await b.expect(SM_STATUS, INVALID)


@core_test()
async def test_mov_all_legal_destinations_operations_and_sources(dut):
    b = await setup(dut)
    dut.i_virt_in.value = 0x96
    await b.step(set_word(5, 3))
    await b.step(set_word(7, 6))
    source_values = {
        0: pin_vector(0x96, 3), 1: STATE_SEED[X], 2: STATE_SEED[Y],
        3: 0, 4: CORE_ID, 6: STATE_SEED[ISR], 7: STATE_SEED[OSR],
    }
    register_destinations = {1: X, 2: Y, 5: ISR, 6: OSR}
    for destination in (0, 1, 2, 4, 5, 6):
        for operation in range(3):
            for source, old_source in source_values.items():
                expected = await seed_state(b)
                value = mov_transform(old_source, operation)
                expected[PC] = value & 31 if destination == 4 else (expected[PC] + 1) & 31
                if destination == 0:
                    expected[PIN_OUT] = write_pin_vector(0, value, 6, 8)
                elif destination in register_destinations:
                    expected[register_destinations[destination]] = value
                assert await b.step(mov_word(destination, operation, source)) == 2
                await b.expect_state(expected)
                await b.expect(SM_STATUS, 0)


@core_test()
async def test_mov_transforms_all_byte_values(dut):
    b = await setup(dut)
    for value in range(256):
        await b.write(ISR, value)
        for operation in range(3):
            assert await b.step(mov_word(1, operation, 6)) == 2
            await b.expect(X, mov_transform(value, operation))
            await b.expect(ISR, value)
            await b.expect(SM_STATUS, 0)


@core_test()
async def test_mov_pin_vectors_all_base_pairs_and_transforms(dut):
    b = await setup(dut)
    for in_base in range(8):
        await b.step(set_word(5, in_base))
        for out_base in range(8):
            await b.step(set_word(7, out_base))
            for value in (0x96, 0x81):
                dut.i_virt_in.value = value
                for operation in range(3):
                    await b.write(PIN_OUT, 0x5A)
                    await b.step(mov_word(0, operation, 0))
                    result = mov_transform(pin_vector(value, in_base), operation)
                    expected = write_pin_vector(0, result, out_base, 8)
                    await b.expect(PIN_OUT, expected)
                    assert int(dut.o_virt_out.value) == expected
                    await b.expect(PIN_DIR, 0)


@core_test()
async def test_mov_shift_register_loads_and_osr_reads_do_not_autopull(dut):
    b = await setup(dut)
    dut.i_tx_fifo_data.value = 0xFF
    dut.i_tx_fifo_valid.value = 1
    await b.write(SHIFT_CTRL, 4)
    await b.step(mov_word(1, 0, 7))
    await b.expect(X, 0)
    await b.expect(SHIFT_COUNT, 0x80)
    await b.write(X, 0xA5)
    await b.step(mov_word(6, 0, 1))
    await b.expect(OSR, 0xA5)
    await b.expect(SHIFT_COUNT, 0)
    await b.step(mov_word(5, 0, 7))
    await b.expect(ISR, 0xA5)
    await b.expect(SHIFT_COUNT, 0)
    await b.step(mov_word(6, 1, 7))
    await b.expect(OSR, 0x5A)
    await b.expect(ISR, 0xA5)
    await b.expect(SHIFT_COUNT, 0)


@core_test()
async def test_mov_pc_all_targets_override_wrap_and_mask_upper_bits(dut):
    b = await setup(dut)
    for target in range(32):
        await b.write(X, 0xE0 | target)
        old_pc = await b.read(PC)
        await b.write(WRAP, (1 << 10) | (old_pc << 5) | ((target + 1) & 31))
        await b.step(mov_word(4, 0, 1))
        await b.expect(PC, target)
        await b.expect(X, 0xE0 | target)
        assert int(dut.o_imem_addr.value) == target


@core_test()
async def test_mov_core_id_is_reset_independent(dut):
    b = await setup(dut)
    for reset_kind in ("full", "local", "external"):
        if reset_kind == "full":
            await b.reset()
        elif reset_kind == "local":
            await b.write(SM_CTRL, RESET)
        else:
            dut.i_soft_reset.value = 1
            await b.tick()
            dut.i_soft_reset.value = 0
        for operation in range(3):
            await b.step(mov_word(2, operation, 4))
            await b.expect(Y, mov_transform(CORE_ID, operation))


@core_test()
async def test_jmp_conditions_all_targets_and_wrap_priority(dut):
    b = await setup(dut)
    await seed_state(b)
    await b.write(PIN_CTRL, 6 << 4)
    for condition in range(8):
        for x_value, y_value, pin_high in (
            (0, 0, 0), (1, 0, 1), (0, 1, 0), (0xFF, 0xFF, 1), (0x80, 0x7F, 0),
        ):
            for target in range(32):
                await b.write(X, x_value)
                await b.write(Y, y_value)
                dut.i_virt_in.value = pin_high << 6
                expected = await b.snapshot()
                bottom = (target + 1) & 31
                await b.write(WRAP, (1 << 10) | (expected[PC] << 5) | bottom)
                taken = (
                    True, x_value == 0, x_value != 0, y_value == 0,
                    y_value != 0, x_value != y_value, bool(pin_high), True,
                )[condition]
                expected[PC] = target if taken else bottom
                expected[X] = (x_value - 1) & 0xFF if condition == 2 else x_value
                expected[Y] = (y_value - 1) & 0xFF if condition == 4 else y_value
                assert await b.step(jmp_word(condition, target)) == 2
                await b.expect_state(expected)
                await b.expect(SM_STATUS, 0)


@core_test()
async def test_jmp_osre_uses_count_and_decoded_pull_threshold(dut):
    b = await setup(dut)
    await b.write(WRAP, (1 << 10) | 5)
    for pull in range(8):
        for push in range(8):
            await b.write(SHIFT_THRESH, (push << 3) | pull)
            await b.write(SM_CTRL, RESET)
            await b.expect(OSR, 0)
            await b.expect(SHIFT_COUNT, 0x80)
            await b.step(jmp_word(7, 19))
            await b.expect(PC, 5)
            await b.expect(SHIFT_COUNT, 0x80)
            await b.write(OSR, 0)
            await b.step(jmp_word(7, 19))
            await b.expect(PC, 19)
            await b.expect(SHIFT_COUNT, 0)
            await b.expect(OSR, 0)


@core_test()
async def test_jmp_pin_selects_jmp_vp_independently_of_in_base(dut):
    b = await setup(dut)
    for selected in range(8):
        await b.step(set_word(5, (selected + 3) % 8))
        await b.write(PIN_CTRL, selected << 4)
        for pin_high in (False, True):
            dut.i_virt_in.value = (1 << selected) if pin_high else 0xFF ^ (1 << selected)
            old_pc = await b.read(PC)
            await b.step(jmp_word(6, 13))
            await b.expect(PC, 13 if pin_high else (old_pc + 1) & 31)


@core_test()
async def test_mov_pins_and_jmp_pin_sample_at_commit(dut):
    b = await setup(dut)
    await b.step(set_word(5, 3))
    await b.write(PIN_CTRL, 5 << 4)
    for word in (mov_word(1, 0, 0), jmp_word(6, 17)):
        for before, at_commit in ((0, 0x96), (0xFF, 0)):
            old_pc = await b.read(PC)
            b.program[old_pc] = word
            dut.i_virt_in.value = before
            await b.write(SM_CTRL, STEP)
            await b.tick()
            await b.expect(PC, old_pc)
            dut.i_virt_in.value = at_commit
            await b.tick()
            if word == mov_word(1, 0, 0):
                await b.expect(X, pin_vector(at_commit, 3))
                await b.expect(PC, (old_pc + 1) & 31)
            else:
                await b.expect(PC, 17 if at_commit & (1 << 5) else (old_pc + 1) & 31)
            await b.expect(SM_STATUS, 0)


@core_test()
async def test_new_instruction_commit_delay_and_sideset_priority(dut):
    b = await setup(dut)
    await b.step(set_word(7, 5))
    for count, base in ((1, 7), (2, 6)):
        await b.write(PIN_CTRL, (count << 8) | base)
        for kind in ("set_pins", "set_dirs", "mov_pins", "jmp_taken", "jmp_not_taken"):
            for common in range(32):
                expected = await seed_state(b)
                expected[PC] = (expected[PC] + 1) & 31
                if kind == "set_pins":
                    word = set_word(0, 0x15, common)
                    expected[PIN_OUT] = write_pin_vector(expected[PIN_OUT], 0x15, 5, 5)
                elif kind == "set_dirs":
                    word = set_word(4, 0x15, common)
                    expected[PIN_DIR] = write_pin_vector(expected[PIN_DIR], 0x15, 5, 5)
                elif kind == "mov_pins":
                    word = mov_word(0, 0, 1, common)
                    expected[PIN_OUT] = write_pin_vector(0, expected[X], 5, 8)
                elif kind == "jmp_taken":
                    word = jmp_word(2, 9, common)
                    expected[X] -= 1
                    expected[PC] = 9
                else:
                    word = jmp_word(1, 9, common)
                delay, expected[PIN_OUT] = common_effects(common, count, base, expected[PIN_OUT])
                await check_timed_instruction(b, word, expected, delay)


@core_test()
async def test_reset_suppresses_new_instruction_commits_and_aborts_delay(dut):
    b = await setup(dut)
    words = (
        set_word(0, 31), set_word(1, 31), set_word(2, 31), set_word(4, 31),
        set_word(5, 6), set_word(7, 5), mov_word(5, 0, 1),
        mov_word(6, 0, 1), mov_word(4, 0, 1), jmp_word(2, 13),
    )
    for reset_kind in ("full", "local", "external"):
        for word in words:
            for elapsed in (1, 3):
                await b.reset()
                await b.write(INIT_PC, 9)
                await b.write(PIN_CTRL, 1 << 8)
                await seed_state(b)
                b.program[0] = word | (0x13 << 8)
                await b.write(SM_CTRL, STEP)
                await b.tick(elapsed)
                if reset_kind == "full":
                    dut.rst.value = 1
                    await b.tick()
                    dut.rst.value = 0
                elif reset_kind == "local":
                    await b.write(SM_CTRL, RESET)
                else:
                    dut.i_soft_reset.value = 1
                    await b.tick()
                    dut.i_soft_reset.value = 0
                expected = {address: 0 for address in STATE_CSRS}
                expected.update({PC: 0 if reset_kind == "full" else 9, SHIFT_COUNT: 0x80})
                await b.expect_state(expected)
                await b.expect(SM_CTRL, 0)
                await b.expect(SM_STATUS, 0)
                await b.tick(10)
                await b.expect_state(expected)


@core_test()
async def test_continuous_set_jmp_decrement_loop_and_mov(dut):
    b = await setup(dut)
    b.program[:4] = [set_word(1, 3), jmp_word(2, 1), mov_word(2, 0, 1), NOP]
    await b.write(SM_CTRL, CONT_RUN)
    previous = (0, 0, 0)
    for expected in ((1, 3, 0), (1, 2, 0), (1, 1, 0), (1, 0, 0), (2, 0xFF, 0), (3, 0xFF, 0xFF)):
        await b.tick()
        await b.expect_bank(dict(zip((PC, X, Y), previous)))
        await b.tick()
        await b.expect_bank(dict(zip((PC, X, Y), expected)))
        await b.expect(SM_STATUS, RUNNING)
        previous = expected
    await b.write(SM_CTRL, 0)
    await b.expect(SM_STATUS, 0)
    await b.tick(6)
    await b.expect_bank(dict(zip((PC, X, Y), previous)))


@core_test()
async def test_rejected_csr_write_does_not_change_mov_source_or_commit(dut):
    b = await setup(dut)
    for destination, address in ((2, Y), (6, OSR)):
        await b.reset()
        await b.write(X, 0xA5)
        b.program[0] = mov_word(destination, 0, 1)
        await b.write(SM_CTRL, STEP)
        await b.tick()
        await b.write(X, 0x5A, rejected=True)
        await b.expect(X, 0xA5)
        await b.expect(address, 0xA5)
        await b.expect(PC, 1)
        await b.expect(SHIFT_COUNT, 0 if destination == 6 else 0x80)
        await b.expect(SM_STATUS, 0)


@core_test()
async def test_fractional_divider_jmp_decrements_once_and_drains_delay(dut):
    b = await setup(dut)
    await b.write(CLKDIV, 0x38)
    await b.write(X, 2)
    b.program[0] = jmp_word(2, 13, common=3)
    await b.write(SM_CTRL, STEP)
    for _ in range(32):
        await b.tick()
        if await b.read(X) == 1:
            break
        await b.expect(X, 2)
        await b.expect(PC, 0)
    else:
        raise AssertionError("JMP did not commit within 32 clocks")
    await b.expect(PC, 13)
    for elapsed in range(1, 22):
        await b.tick()
        await b.expect(X, 1)
        await b.expect(PC, 13)
        await b.expect(SM_STATUS, RUNNING if elapsed < 21 else 0)
    await b.tick(20)
    await b.expect(X, 1)
    await b.expect(PC, 13)


async def begin_instruction(bench, word, command=STEP):
    before = await bench.snapshot()
    bench.program[before[PC]] = word
    bench.cover_start(word)
    await bench.write(SM_CTRL, command)
    await bench.tick()
    await bench.expect_state(before)
    await bench.expect(SM_CTRL, 0 if command == STEP else CONT_RUN)
    return before


async def check_stall(bench, before, cause, clocks=3, running=True):
    events = len(bench.events)
    for _ in range(clocks):
        await bench.expect_state(before)
        await bench.expect(SM_STATUS, blocked_status(cause, running))
        await bench.tick()
    await bench.expect_state(before)
    assert len(bench.events) == events, "stalled instruction produced an effect"


async def check_delay(bench, expected, delay):
    events = len(bench.events)
    await bench.expect_state(expected)
    await bench.expect(SM_STATUS, RUNNING if delay else 0)
    for elapsed in range(1, 2 * delay + 1):
        await bench.tick()
        await bench.expect_state(expected)
        await bench.expect(SM_STATUS, RUNNING if elapsed < 2 * delay else 0)
        assert len(bench.events) == events, "delay repeated an external request"
    bench.cover_complete()


async def load_osr_count(bench, count):
    await bench.write(SHIFT_CTRL, 0)
    await bench.write(OSR, 0xA5)
    if count:
        await bench.step(out_word(3, count))
    expected = (0xA5 << count) & 0xFF
    await bench.expect(OSR, expected)
    await bench.expect(SHIFT_COUNT, (count << 4) | ((await bench.read(SHIFT_COUNT)) & 15))
    return expected


async def load_isr_count(bench, count):
    await bench.write(SHIFT_CTRL, 0)
    await bench.write(X, 0x5A)
    await bench.write(ISR, 0x96)
    if count:
        await bench.step(in_word(1, count))
    expected = shift_in(0x96, 0x5A, count, False) if count else 0x96
    await bench.expect(ISR, expected)
    await bench.expect(SHIFT_COUNT, ((await bench.read(SHIFT_COUNT)) & 0xF0) | count)
    return expected


@core_test()
async def test_wait_pin_all_polarities_indices_and_input_bases(dut):
    b = await setup(dut)
    await b.write(PIN_CTRL, (2 << 8) | 6)
    for base in range(8):
        await b.step(set_word(5, base))
        for polarity in range(2):
            for index in range(8):
                await b.write(PIN_OUT, 0x15)
                dut.i_virt_in.value = (0xA5 & ~(1 << index)) | ((1 - polarity) << index)
                before = await begin_instruction(b, wait_word(polarity, 1, index, common=26))
                await check_stall(b, before, 1)
                # Every unrelated lane changes; only the direct selected lane releases WAIT.
                dut.i_virt_in.value = (0x5A & ~(1 << index)) | ((1 - polarity) << index)
                await check_stall(b, before, 1, clocks=2)
                dut.i_virt_in.value = (0x5A & ~(1 << index)) | (polarity << index)
                expected = {**before, PC: (before[PC] + 1) & 31, PIN_OUT: 0xD5}
                await b.tick()
                await check_delay(b, expected, 2)
                # A condition satisfied at the first execute attempt adds no wait tick.
                assert await b.step(wait_word(polarity, 1, index)) == 2
                await b.expect(SM_STATUS, 0)


@core_test()
async def test_wait_irq_all_flags_relative_modes_and_polarities(dut):
    b = await setup(dut, allow_transfers=True)
    await b.write(PIN_CTRL, (2 << 8) | 6)
    for relative in range(2):
        for flag in range(8):
            for polarity in range(2):
                index = effective_flag(flag, relative)
                b.flags = (0xA5 & ~(1 << index)) | ((1 - polarity) << index)
                await b.write(PIN_OUT, 0x15)
                start = len(b.events)
                before = await begin_instruction(b, wait_word(polarity, 2, flag | (relative << 4), 26))
                assert int(dut.o_irq_addr.value) == index
                await check_stall(b, before, 2)
                b.flags ^= 0xFF ^ (1 << index)
                await check_stall(b, before, 2, clocks=2)
                b.flags = (b.flags & ~(1 << index)) | (polarity << index)
                await b.tick()
                expected = {**before, PC: (before[PC] + 1) & 31, PIN_OUT: 0xD5}
                await check_delay(b, expected, 2)
                assert b.events[start:] == ([('irq', (index, 0, 1))] if polarity else [])
                assert not (b.flags & (1 << index))
                start = len(b.events)
                b.flags = (b.flags & ~(1 << index)) | (polarity << index)
                assert await b.step(wait_word(polarity, 2, flag | (relative << 4))) == 2
                assert b.events[start:] == ([('irq', (index, 0, 1))] if polarity else [])


@core_test()
async def test_remaining_reserved_operands_are_atomic_invalid_nops(dut):
    b = await setup(dut)
    dut.i_tx_fifo_valid.value = 1
    dut.i_tx_fifo_data.value = 0x3C
    dut.i_rx_fifo_ready.value = 1
    dut.i_irq_din.value = 1
    await b.write(PIN_CTRL, (2 << 8) | 6)
    await b.write(WRAP, (1 << 10) | (31 << 5) | 3)
    await b.write(SHIFT_CTRL, 15)
    tested = 0
    for opcode in (1, 2, 3, 4, 6):
        for operands in range(256):
            selection = operands >> 5
            if opcode == 1:
                source = (operands >> 5) & 3
                valid = (source == 1 and not (operands & 0x18)) or (
                    source == 2 and not (operands & 8)
                )
            elif opcode == 2:
                valid = selection not in (4, 5) and not (operands & 0x18)
            elif opcode == 3:
                valid = selection != 7 and not (operands & 0x18)
            elif opcode == 4:
                valid = not (operands & 31)
            else:
                valid = not (operands & 0x18)
            if valid:
                continue
            before = await seed_state(b)
            await b.write(SM_STATUS, INVALID)
            expected = {**before, PC: 3 if before[PC] == 31 else before[PC] + 1, PIN_OUT: 0xE5}
            assert await b.step((opcode << 13) | (25 << 8) | operands) == 4
            await b.expect_state(expected)
            await b.expect(SM_STATUS, INVALID)
            tested += 1
    assert tested == 1056


@core_test()
async def test_out_all_destinations_counts_directions_and_pin_bases(dut):
    b = await setup(dut)
    for base in range(8):
        await b.step(set_word(7, base))
        for right in range(2):
            await b.write(SHIFT_CTRL, right)
            for count in range(1, 9):
                for destination in range(7):
                    expected = await seed_state(b)
                    value, remaining = shift_out(expected[OSR], count, right)
                    expected.update({OSR: remaining, SHIFT_COUNT: count << 4, PC: (expected[PC] + 1) & 31})
                    if destination in (0, 4):
                        address = PIN_OUT if destination == 0 else PIN_DIR
                        expected[address] = write_pin_vector(expected[address], value, base, count)
                    elif destination in (1, 2):
                        expected[X if destination == 1 else Y] = value
                    elif destination == 5:
                        expected[PC] = value & 31
                    elif destination == 6:
                        expected.update({ISR: value, SHIFT_COUNT: (count << 4) | count})
                    assert await b.step(out_word(destination, count)) == 2
                    await b.expect_state(expected)
                    await b.expect(SM_STATUS, 0)


@core_test()
async def test_in_all_sources_counts_directions_and_pin_bases(dut):
    b = await setup(dut)
    dut.i_virt_in.value = 0x96
    for base in range(8):
        await b.step(set_word(5, base))
        for right in range(2):
            await b.write(SHIFT_CTRL, right << 1)
            for count in range(1, 9):
                for source in (0, 1, 2, 3, 6, 7):
                    expected = await seed_state(b)
                    value = {0: pin_vector(0x96, base), 1: expected[X], 2: expected[Y],
                             3: 0, 6: expected[ISR], 7: expected[OSR]}[source]
                    expected.update({ISR: shift_in(expected[ISR], value, count, right),
                                     SHIFT_COUNT: count, PC: (expected[PC] + 1) & 31})
                    assert await b.step(in_word(source, count)) == 2
                    await b.expect_state(expected)
                    await b.expect(SM_STATUS, 0)


@core_test()
async def test_shift_datapaths_all_byte_values(dut):
    b = await setup(dut)
    for right in range(2):
        await b.write(SHIFT_CTRL, right | (right << 1))
        for count in (1, 7, 8):
            for value in range(256):
                await b.write(OSR, value)
                await b.step(out_word(1, count))
                extracted, remaining = shift_out(value, count, right)
                await b.expect(X, extracted)
                await b.expect(OSR, remaining)
                await b.write(X, value)
                await b.write(ISR, 0xA5)
                await b.step(in_word(1, count))
                await b.expect(ISR, shift_in(0xA5, value, count, right))
                await b.expect(SHIFT_COUNT, (count << 4) | count)


@core_test()
async def test_shift_counts_saturate_and_all_load_clear_effects(dut):
    b = await setup(dut, allow_transfers=True)
    for right in range(2):
        await b.write(SHIFT_CTRL, right | (right << 1))
        await b.write(OSR, 0xA5)
        await b.write(ISR, 0x5A)
        count = 0
        for amount in (3, 4, 2, 8, 1):
            await b.step(in_word(3, amount))
            await b.step(out_word(3, amount))
            count = min(count + amount, 8)
            await b.expect(SHIFT_COUNT, (count << 4) | count)
        await b.write(OSR, 0x0100, rejected=True)
        await b.write(ISR, 0x0100, rejected=True)
        await b.expect(SHIFT_COUNT, 0x88)
        await b.step(mov_word(5, 0, 1))
        await b.expect(SHIFT_COUNT, 0x80)
        await b.step(mov_word(6, 0, 2))
        await b.expect(SHIFT_COUNT, 0)
        await b.step(out_word(6, 7))
        await b.expect(SHIFT_COUNT, 0x77)
        dut.i_rx_fifo_ready.value = 1
        await b.step(push_pull_word(0))
        await b.expect(SHIFT_COUNT, 0x70)
        dut.i_tx_fifo_valid.value = 1
        await b.step(push_pull_word(1))
        await b.expect(SHIFT_COUNT, 0)


@core_test()
async def test_pull_condition_block_fallback_and_autopull_fence(dut):
    b = await setup(dut, allow_transfers=True)
    await b.write(X, 0x53)
    for encoded_threshold in range(8):
        threshold = encoded_threshold or 8
        await b.write(SHIFT_THRESH, encoded_threshold)
        for count in range(9):
            for autopull in range(2):
                for conditional in range(2):
                    for block in range(2):
                        for available in range(2):
                            await load_osr_count(b, count)
                            await b.write(SHIFT_CTRL, autopull << 2)
                            dut.i_tx_fifo_valid.value = available
                            dut.i_tx_fifo_data.value = 0x3C
                            before = await b.snapshot()
                            start = len(b.events)
                            required = count >= threshold if (autopull or conditional) else True
                            word = push_pull_word(1, conditional, block)
                            if required and block and not available:
                                before = await begin_instruction(b, word)
                                await check_stall(b, before, 3)
                                dut.i_tx_fifo_valid.value = 1
                                await b.tick()
                                result, expected_events = 0x3C, [('tx', 0x3C)]
                            else:
                                assert await b.step(word) == 2
                                result = 0x3C if available else 0x53
                                expected_events = [('tx', 0x3C)] if available else [('fallback', None)]
                            expected = {**before, PC: (before[PC] + 1) & 31}
                            if required:
                                expected.update({OSR: result, SHIFT_COUNT: before[SHIFT_COUNT] & 15})
                            await b.expect_state(expected)
                            await b.expect(SM_STATUS, 0)
                            assert b.events[start:] == (expected_events if required else [])


@core_test()
async def test_push_condition_block_drop_and_full_byte_transfer(dut):
    b = await setup(dut, allow_transfers=True)
    for encoded_threshold in range(8):
        threshold = encoded_threshold or 8
        await b.write(SHIFT_THRESH, encoded_threshold << 3)
        for count in range(9):
            for conditional in range(2):
                for block in range(2):
                    for available in range(2):
                        value = await load_isr_count(b, count)
                        dut.i_rx_fifo_ready.value = available
                        before = await b.snapshot()
                        start = len(b.events)
                        required = not conditional or count >= threshold
                        word = push_pull_word(0, conditional, block)
                        if required and block and not available:
                            before = await begin_instruction(b, word)
                            await check_stall(b, before, 4)
                            dut.i_rx_fifo_ready.value = 1
                            await b.tick()
                            expected_events = [('rx', value)]
                        else:
                            assert await b.step(word) == 2
                            expected_events = [('rx', value)] if available else [('drop', None)]
                        expected = {**before, PC: (before[PC] + 1) & 31}
                        if required:
                            expected.update({ISR: 0, SHIFT_COUNT: before[SHIFT_COUNT] & 0xF0})
                        await b.expect_state(expected)
                        await b.expect(SM_STATUS, 0)
                        assert b.events[start:] == (expected_events if required else [])


@core_test()
async def test_autopull_all_threshold_count_and_shift_boundaries(dut):
    b = await setup(dut, allow_transfers=True)
    for encoded_threshold in range(8):
        threshold = encoded_threshold or 8
        await b.write(SHIFT_THRESH, encoded_threshold)
        for old_count in range(9):
            for count in range(1, 9):
                for right in range(2):
                    previous = await load_osr_count(b, old_count)
                    await b.write(SHIFT_CTRL, 4 | right)
                    refill = old_count >= threshold
                    b.tx_queue = [0x5A, 0xC3] if refill else []
                    before = await b.snapshot()
                    start = len(b.events)
                    value, remaining = shift_out(0x5A if refill else previous, count, right)
                    new_count = min((0 if refill else old_count) + count, 8)
                    assert await b.step(out_word(1, count)) == 2
                    expected = {**before, X: value, OSR: remaining,
                                SHIFT_COUNT: (new_count << 4) | (before[SHIFT_COUNT] & 15),
                                PC: (before[PC] + 1) & 31}
                    await b.expect_state(expected)
                    assert b.events[start:] == ([('tx', 0x5A)] if refill else [])
                    assert b.tx_queue == ([0xC3] if refill else [])


@core_test()
async def test_autopush_all_threshold_count_and_shift_boundaries(dut):
    b = await setup(dut, allow_transfers=True)
    for encoded_threshold in range(8):
        threshold = encoded_threshold or 8
        await b.write(SHIFT_THRESH, encoded_threshold << 3)
        for old_count in range(9):
            for count in range(1, 9):
                for right in range(2):
                    previous = await load_isr_count(b, old_count)
                    await b.write(SHIFT_CTRL, 8 | (right << 1))
                    new_count = min(old_count + count, 8)
                    push = new_count >= threshold
                    dut.i_rx_fifo_ready.value = push
                    before = await b.snapshot()
                    start = len(b.events)
                    value = shift_in(previous, 0x5A, count, right)
                    assert await b.step(in_word(1, count)) == 2
                    expected = {**before, ISR: 0 if push else value,
                                SHIFT_COUNT: (before[SHIFT_COUNT] & 0xF0) | (0 if push else new_count),
                                PC: (before[PC] + 1) & 31}
                    await b.expect_state(expected)
                    assert b.events[start:] == ([('rx', value)] if push else [])


@core_test()
async def test_autopull_consumes_one_byte_before_shift_without_post_refill(dut):
    b = await setup(dut, allow_transfers=True)
    b.tx_queue = [0xAB, 0xCD]
    await b.write(SHIFT_CTRL, 5)
    await b.write(SHIFT_THRESH, 4)
    assert await b.step(out_word(1, 3)) == 2
    await b.expect_bank({X: 3, OSR: 0x15, SHIFT_COUNT: 0x30})
    assert b.events == [('tx', 0xAB)] and b.tx_queue == [0xCD]
    assert await b.step(out_word(2, 3)) == 2
    await b.expect_bank({Y: 5, OSR: 2, SHIFT_COUNT: 0x60})
    assert b.events == [('tx', 0xAB)], "OUT performed a post-shift refill"
    assert await b.step(out_word(1, 8)) == 2
    await b.expect_bank({X: 0xCD, OSR: 0, SHIFT_COUNT: 0x80})
    assert b.events == [('tx', 0xAB), ('tx', 0xCD)] and b.tx_queue == []
    before = await begin_instruction(b, out_word(1, 8, common=3))
    await check_stall(b, before, 3)
    b.tx_queue[:] = [0xEF, 0x96]
    await b.tick()
    expected = {**before, X: 0xEF, OSR: 0, SHIFT_COUNT: 0x80, PC: (before[PC] + 1) & 31}
    await check_delay(b, expected, 3)
    assert b.events == [('tx', 0xAB), ('tx', 0xCD), ('tx', 0xEF)]
    assert b.tx_queue == [0x96]


@core_test()
async def test_autopush_stall_samples_pins_only_at_successful_commit(dut):
    b = await setup(dut, allow_transfers=True)
    for right in range(2):
        await b.write(PIN_CTRL, (2 << 8) | 6)
        await b.step(set_word(5, 7))
        previous = await load_isr_count(b, 2)
        await b.write(SHIFT_THRESH, 4 << 3)
        await b.write(SHIFT_CTRL, 8 | (right << 1))
        await b.write(PIN_OUT, 0x15)
        dut.i_rx_fifo_ready.value = 0
        dut.i_virt_in.value = 0x11
        start = len(b.events)
        before = await begin_instruction(b, in_word(0, 3, common=26))
        for pins in (0x22, 0x44, 0x88, 0xFF):
            dut.i_virt_in.value = pins
            await check_stall(b, before, 4, clocks=2)
        dut.i_virt_in.value = 0x96
        dut.i_rx_fifo_ready.value = 1
        await b.tick()
        value = shift_in(previous, pin_vector(0x96, 7), 3, right)
        expected = {**before, ISR: 0, SHIFT_COUNT: before[SHIFT_COUNT] & 0xF0,
                    PC: (before[PC] + 1) & 31, PIN_OUT: 0xD5}
        await check_delay(b, expected, 2)
        assert b.events[start:] == [('rx', value)]


@core_test()
async def test_out_pc_overrides_wrap_for_all_targets_and_out_isr_count(dut):
    b = await setup(dut)
    for target in range(32):
        current = await b.read(PC)
        await b.write(WRAP, (1 << 10) | (current << 5) | ((target + 1) & 31))
        await b.write(OSR, target | 0xE0)
        assert await b.step(out_word(5, 8)) == 2
        await b.expect(PC, target)
        await b.expect(SHIFT_COUNT, 0x80)
        await b.write(WRAP, 0)
    for count in range(1, 9):
        await b.write(OSR, 0xA5)
        await b.write(ISR, 0xFF)
        await b.step(out_word(6, count))
        await b.expect(ISR, 0xA5 >> (8 - count))
        await b.expect(SHIFT_COUNT, (count << 4) | count)


@core_test()
async def test_jmp_osre_all_architecturally_reachable_count_boundaries(dut):
    b = await setup(dut)
    for encoded_threshold in range(8):
        await b.write(SHIFT_THRESH, encoded_threshold)
        for count in range(9):
            await load_osr_count(b, count)
            current = await b.read(PC)
            target = (current + 9) & 31
            await b.step(jmp_word(7, target))
            await b.expect(PC, target if count < (encoded_threshold or 8) else (current + 1) & 31)


@core_test()
async def test_irq_set_clear_all_indices_relative_modes_and_clear_ignores_wait(dut):
    b = await setup(dut, allow_transfers=True)
    await b.write(PIN_CTRL, (2 << 8) | 6)
    for relative in range(2):
        for flag in range(8):
            for clear, wait in ((0, 0), (1, 0), (1, 1)):
                index = effective_flag(flag, relative)
                b.flags = 0xA5
                initial = b.flags
                before = await seed_state(b)
                start = len(b.events)
                expected = {**before, PC: (before[PC] + 1) & 31, PIN_OUT: 0xE5}
                assert await b.step(irq_word(flag, clear, wait, relative, common=26)) == 6
                await b.expect_state(expected)
                assert b.events[start:] == [('irq', (index, 1 - clear, 0))]
                assert b.flags == (initial & ~(1 << index)) | ((1 - clear) << index)


@core_test()
async def test_irq_set_wait_issues_once_and_commits_after_acknowledgement(dut):
    b = await setup(dut, allow_transfers=True)
    await b.write(PIN_CTRL, (2 << 8) | 6)
    for relative in range(2):
        for flag in range(8):
            index = effective_flag(flag, relative)
            b.flags = 0
            await b.write(PIN_OUT, 0x15)
            start = len(b.events)
            before = await begin_instruction(b, irq_word(flag, wait=1, relative=relative, common=26))
            await b.tick()
            await b.expect_state(before)
            assert b.events[start:] == [('irq', (index, 1, 0))]
            assert b.event_cycles[start] == b.cycles, "set request missed the normal issue tick"
            await check_stall(b, before, 2, clocks=7)
            b.flags &= ~(1 << index)
            await b.tick()
            expected = {**before, PC: (before[PC] + 1) & 31, PIN_OUT: 0xD5}
            await check_delay(b, expected, 2)
            assert b.events[start:] == [('irq', (index, 1, 0))], "set-and-wait reissued its request"


@core_test()
async def test_irq_set_wait_dropped_set_completes_on_later_enable_tick(dut):
    b = await setup(dut, allow_transfers=True)
    for divisor in (0x10, 0x38):
        await b.reset()
        b.flags = 0
        b.apply_irq_writes = False  # Model a set request losing external arbitration.
        await b.write(CLKDIV, divisor)
        await b.write(PIN_CTRL, (1 << 8) | 7)
        before = await b.snapshot()
        start = len(b.events)
        b.program[before[PC]] = irq_word(5, wait=1, common=16)
        await b.write(SM_CTRL, STEP)
        for _ in range(32):
            await b.tick()
            if len(b.events) > start:
                break
        else:
            raise AssertionError("set-and-wait did not issue")
        issued_cycle = b.event_cycles[start]
        await b.expect_state(before)
        assert b.events[start:] == [('irq', (5, 1, 0))]
        assert b.flags == 0
        await b.wait_halted()
        await b.expect(PC, (before[PC] + 1) & 31)
        await b.expect(PIN_OUT, 0x80)
        assert b.cycles - issued_cycle in ((1,) if divisor == 0x10 else (3, 4))
        assert b.events[start:] == [('irq', (5, 1, 0))]


async def prepare_blocked_instruction(bench, family, command=STEP, common=26):
    bench.tx_queue = []
    bench.flags = 0
    bench.dut.i_rx_fifo_ready.value = 0
    bench.dut.i_virt_in.value = 0
    await bench.write(SHIFT_CTRL, 0)
    await bench.write(SHIFT_THRESH, 0)
    await bench.write(PIN_CTRL, (2 << 8) | 6)
    await bench.write(PIN_OUT, 0x15)
    await bench.write(ISR, 0x96)
    await bench.write(X, 0x53)
    if family == "out":
        await load_osr_count(bench, 8)
        await bench.write(SHIFT_CTRL, 5)
    elif family == "in":
        await bench.write(SHIFT_CTRL, 8)
    words = {
        "pin": wait_word(1, 1, 0, common=common),
        "flag": wait_word(1, 2, 6 | 16, common=common),
        "pull": push_pull_word(1, common=common),
        "push": push_pull_word(0, common=common),
        "out": out_word(0, 4, common=common),
        "in": in_word(0, 8, common=common),
        "irq": irq_word(6, wait=1, relative=1, common=common),
    }
    before = await begin_instruction(bench, words[family], command)
    if family == "irq":
        await bench.tick()
        await bench.expect_state(before)
    return before, {"pin": 1, "flag": 2, "pull": 3, "push": 4, "out": 3, "in": 4, "irq": 2}[family]


def release_blocked_instruction(bench, family):
    bench.tx_queue[:] = [0x3C]
    bench.dut.i_rx_fifo_ready.value = 1
    bench.dut.i_virt_in.value = 0xB3
    bench.flags = (1 << effective_flag(6, 1)) if family == "flag" else 0


def blocked_completion(before, family):
    expected = {**before, PC: (before[PC] + 1) & 31, PIN_OUT: 0xD5}
    events = []
    if family == "pull":
        expected.update({OSR: 0x3C, SHIFT_COUNT: before[SHIFT_COUNT] & 15})
        events = [('tx', 0x3C)]
    elif family == "push":
        expected.update({ISR: 0, SHIFT_COUNT: before[SHIFT_COUNT] & 0xF0})
        events = [('rx', before[ISR])]
    elif family == "out":
        expected.update({OSR: 3, SHIFT_COUNT: 0x40 | (before[SHIFT_COUNT] & 15), PIN_OUT: 0xDC})
        events = [('tx', 0x3C)]
    elif family == "in":
        expected.update({ISR: 0, SHIFT_COUNT: before[SHIFT_COUNT] & 0xF0})
        events = [('rx', 0xB3)]
    elif family == "flag":
        events = [('irq', (effective_flag(6, 1), 0, 1))]
    return expected, events


@core_test()
async def test_every_stall_family_freezes_and_resumes_retained_work(dut):
    b = await setup(dut, allow_transfers=True)
    # Both common fields drive the same side-set value; delay is zero or two.
    for external, common in ((False, 24), (False, 26), (True, 24), (True, 26)):
        for family in ("pin", "flag", "pull", "push", "out", "in", "irq"):
            await b.reset()
            before, cause = await prepare_blocked_instruction(
                b, family, CONT_RUN if external else STEP, common=common,
            )
            await check_stall(b, before, cause)
            start = len(b.events)
            if external:
                dut.i_halt_req.value = 1
                await b.tick()
                await b.expect(SM_CTRL, CONT_RUN)
            else:
                await b.write(SM_CTRL, 0)
            await check_stall(b, before, cause, running=False)
            await b.write(SHIFT_CTRL, 0, rejected=True)
            b.program[before[PC]] = set_word(1, 7)  # Resuming must execute the captured word.
            release_blocked_instruction(b, family)
            await b.tick(4)
            await b.expect_state(before)
            await b.expect(SM_STATUS, 0)
            assert len(b.events) == start
            if external:
                dut.i_halt_req.value = 0
            else:
                await b.write(SM_CTRL, STEP)
            await b.tick()
            expected, events = blocked_completion(before, family)
            if external:
                await b.expect_state(expected)
                for _ in range(2 * (common & 7)):
                    await b.tick()
                    await b.expect_state(expected)
                await b.write(SM_CTRL, 0)
                await b.expect(SM_STATUS, 0)
            else:
                await check_delay(b, expected, common & 7)
            assert b.events[start:] == events


@core_test()
async def test_halt_before_irq_issue_suppresses_set_until_resume(dut):
    b = await setup(dut, allow_transfers=True)
    b.flags = 0
    before = await begin_instruction(b, irq_word(3, wait=1))
    await b.write(SM_CTRL, 0)
    await b.expect_state(before)
    assert b.events == []
    await b.tick(4)
    await b.expect(SM_STATUS, 0)
    await b.write(SM_CTRL, STEP)
    await b.tick()
    assert b.events == [('irq', (3, 1, 0))]
    await check_stall(b, before, 2)
    b.flags = 0
    await b.tick()
    await b.expect(PC, 1)
    await b.expect(SM_STATUS, 0)
    assert b.events == [('irq', (3, 1, 0))]


async def apply_core_reset(bench, kind):
    if kind == "full":
        bench.dut.rst.value = 1
        await bench.tick()
        bench.dut.rst.value = 0
    elif kind == "local":
        await bench.write(SM_CTRL, RESET)
    else:
        bench.dut.i_soft_reset.value = 1
        await bench.tick()
        bench.dut.i_soft_reset.value = 0


@core_test()
async def test_reset_suppresses_all_same_edge_link_and_sync_effects(dut):
    b = await setup(dut, allow_transfers=True)
    for kind in ("full", "local", "external"):
        for family in ("pull", "push", "out", "in", "fallback", "drop", "set", "clear", "wait_clear"):
            await b.reset()
            b.tx_queue = [] if family == "fallback" else [0x3C]
            b.flags = 0xFF
            dut.i_rx_fifo_ready.value = family != "drop"
            dut.i_virt_in.value = 0xB3
            await b.write(INIT_PC, 9)
            await b.write(ISR, 0x96)
            await b.write(PIN_CTRL, (2 << 8) | 6)
            if family == "out":
                await b.write(SHIFT_CTRL, 5)
            elif family == "in":
                await b.write(SHIFT_CTRL, 8)
            words = {
                "pull": push_pull_word(1, common=26), "push": push_pull_word(0, common=26),
                "out": out_word(0, 8, common=26), "in": in_word(0, 8, common=26),
                "fallback": push_pull_word(1, block=0, common=26),
                "drop": push_pull_word(0, block=0, common=26),
                "set": irq_word(3, common=26), "clear": irq_word(3, clear=1, common=26),
                "wait_clear": wait_word(1, 2, 3, common=26),
            }
            await begin_instruction(b, words[family])
            start = len(b.events)
            pending_tx, flags = list(b.tx_queue), b.flags
            await apply_core_reset(b, kind)
            expected = {address: 0 for address in STATE_CSRS}
            expected.update({SHIFT_COUNT: 0x80, PC: 0 if kind == "full" else 9})
            await b.expect_state(expected)
            await b.expect(SM_CTRL, 0)
            await b.expect(SM_STATUS, 0)
            await b.tick(12)
            await b.expect_state(expected)
            assert b.events[start:] == []
            assert b.tx_queue == pending_tx and b.flags == flags


@core_test()
async def test_reset_after_irq_issue_aborts_wait_without_undoing_flag_write(dut):
    b = await setup(dut, allow_transfers=True)
    for kind in ("full", "local", "external"):
        await b.reset()
        b.flags = 0
        start = len(b.events)
        before, _ = await prepare_blocked_instruction(b, "irq")
        index = effective_flag(6, 1)
        assert b.events[start:] == [('irq', (index, 1, 0))]
        await check_stall(b, before, 2)
        await apply_core_reset(b, kind)
        await b.tick(10)
        await b.expect_bank({SM_STATUS: 0, SM_CTRL: 0, PIN_OUT: 0, SHIFT_COUNT: 0x80, PC: 0})
        assert b.flags == 1 << index
        assert b.events[start:] == [('irq', (index, 1, 0))]
        # A subsequent instruction must start with a fresh issue phase.
        b.flags = 0
        b.program[0] = irq_word(1, wait=1)
        await b.write(SM_CTRL, STEP)
        await b.tick(2)
        assert b.events[-1] == ('irq', (1, 1, 0))
        b.flags = 0
        await b.tick()
        await b.expect(PC, 1)
        await b.expect(SM_STATUS, 0)


@core_test()
async def test_nonblocking_and_conditional_noops_apply_all_common_fields_once(dut):
    b = await setup(dut, allow_transfers=True)
    await b.write(PIN_CTRL, (2 << 8) | 6)
    await b.write(X, 0x53)
    for family in ("fallback", "drop", "pull_condition", "pull_fence", "push_condition"):
        for common in range(32):
            await b.write(SHIFT_CTRL, 4 if family == "pull_fence" else 0)
            await b.write(ISR, 0xC3)
            await b.write(OSR, 0x96)
            await b.write(PIN_OUT, 0xA5)
            dut.i_tx_fifo_valid.value = family == "pull_fence"
            dut.i_tx_fifo_data.value = 0x3C
            dut.i_rx_fifo_ready.value = 0
            before = await b.snapshot()
            delay, pins = common_effects(common, 2, 6, before[PIN_OUT])
            words = {
                "fallback": push_pull_word(1, block=0, common=common),
                "drop": push_pull_word(0, block=0, common=common),
                "pull_condition": push_pull_word(1, conditional=1, common=common),
                "pull_fence": push_pull_word(1, common=common),
                "push_condition": push_pull_word(0, conditional=1, common=common),
            }
            expected = {**before, PC: (before[PC] + 1) & 31, PIN_OUT: pins}
            events = []
            if family == "fallback":
                expected[OSR] = 0x53
                events = [('fallback', None)]
            elif family == "drop":
                expected[ISR] = 0
                events = [('drop', None)]
            start = len(b.events)
            before_cycle = b.cycles
            assert await b.step(words[family]) == 2 + 2 * delay
            await b.expect_state(expected)
            assert b.events[start:] == events
            if events:
                assert b.event_cycles[start] == before_cycle + 3  # Command edge, fetch, commit.


@core_test()
async def test_in_osr_source_never_autopulls_and_explicit_push_is_not_fenced(dut):
    b = await setup(dut, allow_transfers=True)
    await b.write(OSR, 0x96)
    await b.step(out_word(3, 3))
    await b.write(SHIFT_THRESH, 1)
    await b.write(SHIFT_CTRL, 4)
    dut.i_tx_fifo_valid.value = 0
    before = await b.snapshot()
    assert await b.step(in_word(7, 5)) == 2
    await b.expect(OSR, before[OSR])
    await b.expect(ISR, shift_in(before[ISR], before[OSR], 5, False))
    await b.expect(SHIFT_COUNT, 0x35)
    assert b.events == []
    await b.write(SHIFT_CTRL, 8)
    await b.write(ISR, 0xA5)
    dut.i_rx_fifo_ready.value = 1
    await b.step(push_pull_word(0))
    assert b.events == [('rx', 0xA5)]
    await b.expect(ISR, 0)
    await b.expect(SHIFT_COUNT, 0x30)


@core_test()
async def test_step_queued_during_every_stall_waits_for_commit_and_full_delay(dut):
    b = await setup(dut, allow_transfers=True)
    for family in ("pin", "flag", "pull", "push", "out", "in", "irq"):
        await b.reset()
        before, cause = await prepare_blocked_instruction(b, family)
        await b.write(SM_CTRL, STEP)
        await b.write(SM_CTRL, STEP)
        await b.expect(SM_CTRL, STEP)
        await check_stall(b, before, cause)
        next_pc = (before[PC] + 1) & 31
        b.program[next_pc] = set_word(1, 7)
        start = len(b.events)
        release_blocked_instruction(b, family)
        await b.tick()
        expected, events = blocked_completion(before, family)
        await b.expect_state(expected)
        for _ in range(4):
            await b.tick()
            await b.expect_state(expected)
            await b.expect(SM_CTRL, STEP)
            await b.expect(SM_STATUS, RUNNING)
        await b.tick()
        await b.expect_state(expected)
        await b.expect(SM_CTRL, 0)
        await b.tick()
        expected.update({X: 7, PC: (next_pc + 1) & 31, PIN_OUT: expected[PIN_OUT] & 0x3F})
        await b.expect_state(expected)
        await b.expect(SM_STATUS, 0)
        await b.tick(6)
        await b.expect_state(expected)
        assert b.events[start:] == events


@core_test()
async def test_ready_link_work_drains_through_local_and_external_halt(dut):
    b = await setup(dut, allow_transfers=True)
    for external in (False, True):
        for family in ("pull", "push", "out", "in", "flag"):
            await b.reset()
            before, _ = await prepare_blocked_instruction(b, family)
            release_blocked_instruction(b, family)
            start = len(b.events)
            if external:
                dut.i_halt_req.value = 1
                await b.tick()
            else:
                await b.write(SM_CTRL, 0)
            expected, events = blocked_completion(before, family)
            await check_delay(b, expected, 2)
            assert b.events[start:] == events
            await b.tick(6)
            await b.expect_state(expected)
            assert b.events[start:] == events


@core_test()
async def test_fractional_divider_link_and_sync_requests_only_on_enabled_edges(dut):
    b = await setup(dut, allow_transfers=True)
    for family in ("pull", "push", "out", "in", "flag", "irq"):
        await b.reset()
        # Set up the blocked instruction at divide-by-one, then reset to
        # discard it before selecting a slower divider and starting again.
        retained, _ = await prepare_blocked_instruction(b, family)
        b.program[0] = b.program[retained[PC]]
        await b.write(SM_CTRL, RESET)
        await b.write(CLKDIV, 0x38)
        await b.write(SM_CTRL, RESET)
        await b.write(PIN_OUT, 0x15)
        await b.write(ISR, 0x96)
        b.flags = 0
        before = await b.snapshot()
        start = len(b.events)
        await b.write(SM_CTRL, STEP)
        if family != "irq":
            release_blocked_instruction(b, family)
        for _ in range(48):
            await b.tick()
            if len(b.events) > start:
                break
        else:
            raise AssertionError(f"{family} request did not occur")
        transfer_cycle = b.event_cycles[start]
        if family == "irq":
            await b.expect_state(before)
            index = effective_flag(6, 1)
            assert b.events[start:] == [('irq', (index, 1, 0))]
            await b.tick(9)
            await b.expect_state(before)
            assert b.events[start:] == [('irq', (index, 1, 0))]
            b.flags = 0
            for _ in range(8):
                await b.tick()
                if await b.read(PC) != before[PC]:
                    break
            else:
                raise AssertionError("IRQ wait did not acknowledge")
            commit_cycle = b.cycles
        else:
            commit_cycle = transfer_cycle
        expected, events = blocked_completion(before, family)
        await b.expect_state(expected)
        await b.wait_halted(limit=48)
        assert b.cycles - commit_cycle == 14, "two delay units did not spend four divider ticks"
        await b.expect_state(expected)
        assert b.events[start:] == ([('irq', (effective_flag(6, 1), 1, 0))] if family == "irq" else events)


@core_test()
async def test_continuous_program_exercises_complete_isa_and_external_endpoints(dut):
    b = await setup(dut, allow_transfers=True)
    b.tx_queue = []
    b.flags = 0
    dut.i_virt_in.value = 0x15
    dut.i_rx_fifo_ready.value = 0
    await b.write(SHIFT_CTRL, 1)
    b.program[:13] = [
        push_pull_word(1), out_word(0, 4), out_word(1, 4), mov_word(5, 0, 1),
        in_word(0, 4), push_pull_word(0), irq_word(3), wait_word(1, 2, 3),
        irq_word(6, wait=1, relative=1), wait_word(1, 1, 0),
        set_word(2, 2), jmp_word(4, 11), mov_word(1, 0, 4),
    ]
    await b.write(SM_CTRL, CONT_RUN)
    await b.tick(4)
    await b.expect_bank({PC: 0, SM_STATUS: blocked_status(3), SHIFT_COUNT: 0x80})
    assert b.events == []
    b.tx_queue.append(0xA6)
    await b.tick()
    await b.expect_bank({PC: 1, OSR: 0xA6, SHIFT_COUNT: 0})
    assert b.events == [('tx', 0xA6)]
    for expected in (
        {PC: 2, PIN_OUT: 6, OSR: 0x0A, SHIFT_COUNT: 0x40},
        {PC: 3, X: 0x0A, OSR: 0, SHIFT_COUNT: 0x80},
        {PC: 4, ISR: 0x0A, SHIFT_COUNT: 0x80},
        {PC: 5, ISR: 0xA5, SHIFT_COUNT: 0x84},
    ):
        await b.tick(2)
        await b.expect_bank(expected)
    await b.tick(5)
    await b.expect_bank({PC: 5, ISR: 0xA5, SHIFT_COUNT: 0x84, SM_STATUS: blocked_status(4)})
    assert b.events == [('tx', 0xA6)]
    dut.i_rx_fifo_ready.value = 1
    await b.tick()
    await b.expect_bank({PC: 6, ISR: 0, SHIFT_COUNT: 0x80})
    assert b.events[-1] == ('rx', 0xA5)
    await b.tick(2)
    await b.expect(PC, 7)
    assert b.events[-1] == ('irq', (3, 1, 0))
    await b.tick(2)
    await b.expect(PC, 8)
    assert b.events[-1] == ('irq', (3, 0, 1))
    await b.tick(2)
    index = effective_flag(6, 1)
    await b.expect(PC, 8)
    assert b.events[-1] == ('irq', (index, 1, 0))
    await b.tick(5)
    await b.expect(SM_STATUS, blocked_status(2))
    b.flags = 0
    await b.tick()
    await b.expect(PC, 9)
    await b.tick(2)
    await b.expect(PC, 10)
    await b.tick(2)
    await b.expect_bank({PC: 11, Y: 2})
    for expected in ((11, 1), (11, 0), (12, 0xFF)):
        await b.tick(2)
        await b.expect_bank(dict(zip((PC, Y), expected)))
    await b.tick(2)
    await b.expect_bank({PC: 13, X: CORE_ID, PIN_OUT: 6, SHIFT_COUNT: 0x80})
    await b.write(SM_CTRL, 0)
    await b.expect(SM_STATUS, 0)
    assert b.events == [('tx', 0xA6), ('rx', 0xA5), ('irq', (3, 1, 0)),
                        ('irq', (3, 0, 1)), ('irq', (index, 1, 0))]


@core_test()
async def test_free_running_clock_advances_during_csr_reads_and_host_waits(dut):
    b = await setup(dut)
    await b.write(SM_CTRL, CONT_RUN)
    start_cycles = b.cycles
    start_time = int(get_sim_time(unit="ps"))
    edge_times = []

    async def observe_clock():
        for _ in range(10):
            await FallingEdge(dut.clk)
            edge_times.append(int(get_sim_time(unit="ps")))
            await RisingEdge(dut.clk)
            edge_times.append(int(get_sim_time(unit="ps")))

    observer = cocotb.start_soon(observe_clock())
    for _ in range(20):
        await b.read(PC)
        await Timer(1, unit="ns")
    assert b.cycles - start_cycles == 2, "CSR polling stopped the clock"
    remaining = 10 * CLOCK_PERIOD_NS * 1000 - (int(get_sim_time(unit="ps")) - start_time)
    await Timer(remaining, unit="ps")
    await observer
    assert b.cycles - start_cycles == 10, "host wait stopped the clock"
    assert len(edge_times) == 20
    assert all(right - left == CLOCK_PERIOD_NS * 500
               for left, right in zip(edge_times, edge_times[1:])), edge_times
    await b.expect(PC, 5)
    # A request arriving in the low phase waits for the next falling edge.
    # The intervening fetch must drain when the halt write is accepted.
    await Timer(7, unit="ns")
    await b.write(SM_CTRL, 0)
    halted_cycles = b.cycles
    await Timer(3 * CLOCK_PERIOD_NS, unit="ns")
    assert b.cycles - halted_cycles == 3, "halt stopped the system clock"
    await b.expect_bank({PC: 6, SM_STATUS: 0})


@core_test()
async def test_continuous_full_isa_sanity_during_host_wait(dut):
    b = await setup(dut, allow_transfers=True)
    data = [(0x21 + 7 * index) & 0xFF for index in range(32)]
    b.tx_queue = list(data)
    b.flags = 0
    dut.i_virt_in.value = 0x3C
    dut.i_rx_fifo_ready.value = 1
    b.program[:10] = [
        push_pull_word(1), out_word(0, 8), in_word(0, 8), push_pull_word(0),
        irq_word(3), wait_word(1, 2, 3), mov_word(2, 0, 4),
        set_word(1, 3), jmp_word(2, 8), jmp_word(0, 0),
    ]
    await b.write(CLKDIV, 0x18)
    await b.write(SM_CTRL, CONT_RUN)
    start_cycles = b.cycles
    # Each loop has 26 execution enables: four JMP X-- attempts plus the
    # other nine instructions. Divide-by-1.5 takes 39 system clocks.
    await Timer(len(data) * 39 * CLOCK_PERIOD_NS, unit="ns")
    assert b.cycles - start_cycles == len(data) * 39
    expected_events = []
    for value in data:
        expected_events.extend([
            ('tx', value), ('rx', 0x3C), ('irq', (3, 1, 0)), ('irq', (3, 0, 1)),
        ])
    assert b.events == expected_events
    assert b.tx_queue == []
    assert b.flags == 0
    await b.expect_bank({
        PC: 0, X: 0xFF, Y: CORE_ID, ISR: 0, OSR: 0,
        SHIFT_COUNT: 0x80, PIN_OUT: data[-1], SM_STATUS: RUNNING,
    })
    await b.write(SM_CTRL, 0)
    await Timer(10 * CLOCK_PERIOD_NS, unit="ns")
    await b.expect_bank({PC: 0, PIN_OUT: data[-1], SM_STATUS: 0})
    assert b.events == expected_events


def write_divider_model(destination):
    """Generate an explicit simulation dependency; never edit the owner's RTL."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        """// Test-only Q12.4 enable model from the architecture contract.
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

    assign sum = {1'b0, phase} + 17'h00010;
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

"""
    )
    print("Using a TEST-ONLY divider model; supplied divider integration is unverified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-divider-model", type=Path, required=True)
    args = parser.parse_args()
    write_divider_model(args.write_divider_model)

