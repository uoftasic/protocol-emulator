# Block-level black-box tests: pem_core_array
.DEFAULT_GOAL := sim
SIM ?= icarus
TOPLEVEL_LANG ?= verilog
PYTHON ?= python3
USE_DIVIDER_MODEL ?= 0
NUM_CORES ?= 4
NUM_SYNC_FLAGS ?= 8

SRC_DIR = $(CURDIR)/../../../src/L1-PEm-system/L2-PEm-engine/L3-PEm-core-array
CORE_SRC_DIR = $(SRC_DIR)/L4-PEm-core
COCOTB_TOPLEVEL = pem_core_array
COCOTB_TEST_MODULES = test_pem_core_array
export PEM_ARRAY_NUM_CORES := $(NUM_CORES)
export PEM_ARRAY_NUM_SYNC_FLAGS := $(NUM_SYNC_FLAGS)

ifeq ($(USE_DIVIDER_MODEL),1)
SIM_BUILD = sim_build/divider_model_$(NUM_CORES)_$(NUM_SYNC_FLAGS)
DIVIDER_SOURCE = $(abspath $(SIM_BUILD))/clock_divider_model.sv
else
SIM_BUILD = sim_build/rtl_$(NUM_CORES)_$(NUM_SYNC_FLAGS)
DIVIDER_SOURCE = $(CORE_SRC_DIR)/clock_divider.sv
endif

VERILOG_SOURCES += $(SRC_DIR)/pem_core_array.sv $(CORE_SRC_DIR)/pem_core.sv $(DIVIDER_SOURCE)
COMPILE_ARGS += -I$(CORE_SRC_DIR)
ifeq ($(SIM),icarus)
COMPILE_ARGS += -g2012
COMPILE_ARGS += -P$(COCOTB_TOPLEVEL).NUM_CORES=$(NUM_CORES)
COMPILE_ARGS += -P$(COCOTB_TOPLEVEL).NUM_SYNC_FLAGS=$(NUM_SYNC_FLAGS)
endif
ifeq ($(SIM),verilator)
COMPILE_ARGS += -Wno-fatal -GNUM_CORES=$(NUM_CORES) -GNUM_SYNC_FLAGS=$(NUM_SYNC_FLAGS)
endif

ifeq ($(USE_DIVIDER_MODEL),1)
$(DIVIDER_SOURCE): test_pem_core_array.py
	$(PYTHON) test_pem_core_array.py --write-divider-model $@
endif

.PHONY: test-model
test-model:
	@echo "PEm array tests use a test-only divider; owner RTL integration remains pending."
	$(MAKE) sim USE_DIVIDER_MODEL=1

VENV ?= $(HOME)/cocotb-env
ifneq ($(wildcard $(VENV)/bin/cocotb-config),)
export PATH := $(VENV)/bin:$(PATH)
endif

include $(shell PATH="$(PATH)" cocotb-config --makefiles)/Makefile.sim

