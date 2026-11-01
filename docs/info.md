<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

The Protocol Emulator is a configurable hardware peripheral designed to emulate multiple communication protocols using dedicated state machines.

A host device communicates with the Protocol Emulator over SPI to send and receive data. Internally, multiple protocol-emulation cores handle their respective tasks. Data exchanged between the cores and the host is managed through buffering and arbitration logic, allowing multiple cores to share the host interface.

The system is configurable through Control and Status Registers (CSRs), which allow the host to configure the emulator and control its operation.

## How to test

The project includes both block-level and system-level tests using Verilog/SystemVerilog simulation and Cocotb.

Block-level tests: Test individual modules independently, including the protocol-emulation cores and SPI controller. Each block has its own test directory and Makefile.

System-level tests: Verify the interaction between the individual modules and the overall system.

Full-chip tests: Test the top-level TinyTapeout design through its standard testbench.

To run tests, navigate to the test/ directory and use the corresponding Makefile targets.

## External hardware

The Protocol Emulator uses two host interfaces:

Host SPI: Used to send data to and receive data from the Protocol Emulator.

Host UART: Used to program and configure the emulator's Control and Status Registers (CSRs).
