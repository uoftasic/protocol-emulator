# Makefile — block-level test: pem_core
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
.DEFAULT_GOAL := sim
SIM ?= icarus
TOPLEVEL_LANG ?= verilog
PYTHON ?= python3
VERILATOR ?= verilator
SRC_DIR = $(CURDIR)/../../../src/L1-PEm-system/L2-PEm-engine/L3-PEm-core-array/L4-PEm-core/
PROJECT_SOURCES = pem_core.sv clock_divider.sv

# RTL simulation only (gate-level is done at the top level)
SIM_BUILD = sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

# Allow `include files from the source dir
COMPILE_ARGS += -g2012 -I$(SRC_DIR)

# No tb.v wrapper needed: cocotb drives the module directly
COCOTB_TOPLEVEL = pem_core

# Test modules, comma-separated, no .py
COCOTB_TEST_MODULES = test_pem_core

.PHONY: lint
lint:
	VERILATOR="$(VERILATOR)" $(PYTHON) lint_pem_core.py

ifneq ($(MAKECMDGOALS),lint)
# Use a cocotb venv if one exists (override with: make VENV=/path/to/venv)
VENV ?= $(HOME)/cocotb-env
ifneq ($(wildcard $(VENV)/bin/cocotb-config),)
export PATH := $(VENV)/bin:$(PATH)
endif

include $(shell PATH="$(PATH)" cocotb-config --makefiles)/Makefile.sim
endif

