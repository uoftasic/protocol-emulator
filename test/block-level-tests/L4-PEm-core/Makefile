# Makefile — block-level test: pem_core
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
.DEFAULT_GOAL := sim
SIM ?= icarus
TOPLEVEL_LANG ?= verilog
PYTHON ?= python3
VERILATOR ?= verilator
YOSYS ?= yosys
DOT ?= dot
USE_DIVIDER_MODEL ?= 0
CORE_ID ?= 0
export CORE_ID
SRC_DIR = $(CURDIR)/../../../src/L1-PEm-system/L2-PEm-engine/L3-PEm-core-array/L4-PEm-core/

# RTL simulation only (gate-level is done at the top level)
ifeq ($(USE_DIVIDER_MODEL),1)
SIM_BUILD = sim_build/divider_model_$(CORE_ID)
DIVIDER_SOURCE = $(CURDIR)/$(SIM_BUILD)/clock_divider_model.sv
else
SIM_BUILD = sim_build/rtl_$(CORE_ID)
DIVIDER_SOURCE = $(SRC_DIR)/clock_divider.sv
endif
VERILOG_SOURCES += $(SRC_DIR)/pem_core.sv $(DIVIDER_SOURCE)

# Allow `include files from the source dir
COMPILE_ARGS += -I$(SRC_DIR)
ifeq ($(SIM),icarus)
COMPILE_ARGS += -g2012
COMPILE_ARGS += -P$(COCOTB_TOPLEVEL).CORE_ID=$(CORE_ID)
endif
ifeq ($(SIM),verilator)
COMPILE_ARGS += -Wno-fatal
COMPILE_ARGS += -GCORE_ID=$(CORE_ID)
endif

# No tb.v wrapper needed: cocotb drives the module directly
COCOTB_TOPLEVEL = pem_core

# Test modules, comma-separated, no .py
COCOTB_TEST_MODULES = test_pem_core

.PHONY: lint synth test-model
lint:
	VERILATOR="$(VERILATOR)" $(PYTHON) lint_pem_core.py

synth:
	YOSYS="$(YOSYS)" DOT="$(DOT)" $(PYTHON) synth_pem_core.py --core-id $(CORE_ID)

test-model:
	@echo "PEm regression uses a test-only divider model; supplied divider integration is unverified."
	$(MAKE) sim USE_DIVIDER_MODEL=1

ifeq ($(USE_DIVIDER_MODEL),1)
$(DIVIDER_SOURCE): test_pem_core.py
	$(PYTHON) test_pem_core.py --write-divider-model $@
endif

ifneq ($(filter-out lint synth,$(or $(MAKECMDGOALS),sim)),)
# Use a cocotb venv if one exists (override with: make VENV=/path/to/venv)
VENV ?= $(HOME)/cocotb-env
ifneq ($(wildcard $(VENV)/bin/cocotb-config),)
export PATH := $(VENV)/bin:$(PATH)
endif

include $(shell PATH="$(PATH)" cocotb-config --makefiles)/Makefile.sim
endif

