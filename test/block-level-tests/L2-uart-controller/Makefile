# Makefile — block-level test: uart_controller
# See https://docs.cocotb.org/en/stable/quickstart.html for more info
#
# Frame config can be overridden, e.g.:
#   make                                  (8N1, default)
#   make PARITY_EN=1 NUM_STOP_BITS=2      (8E2)
#   make NUM_DATA_BITS=7 PARITY_EN=1      (7E1)

# defaults
SIM ?= icarus
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(CURDIR)/../../../src/L1-PEm-system/L2-uart-controller
PROJECT_SOURCES = uart-controller.sv

# DUT parameters (small CLK_PER_BIT keeps simulation fast)
CLK_PER_BIT   ?= 16
DATA_WIDTH    ?= 8
NUM_DATA_BITS ?= $(DATA_WIDTH)
NUM_STOP_BITS ?= 1
PARITY_EN     ?= 0
export CLK_PER_BIT DATA_WIDTH NUM_DATA_BITS NUM_STOP_BITS PARITY_EN

COCOTB_TOPLEVEL = uart_controller
COMPILE_ARGS += -g2012
COMPILE_ARGS += -P$(COCOTB_TOPLEVEL).CLK_PER_BIT=$(CLK_PER_BIT)
COMPILE_ARGS += -P$(COCOTB_TOPLEVEL).DATA_WIDTH=$(DATA_WIDTH)
COMPILE_ARGS += -P$(COCOTB_TOPLEVEL).NUM_DATA_BITS=$(NUM_DATA_BITS)
COMPILE_ARGS += -P$(COCOTB_TOPLEVEL).NUM_STOP_BITS=$(NUM_STOP_BITS)
COMPILE_ARGS += -P$(COCOTB_TOPLEVEL).PARITY_EN=$(PARITY_EN)

# RTL simulation only (gate-level is done at the top level).
# Separate build dir per config so parameter changes always recompile.
SIM_BUILD = sim_build/rtl_$(CLK_PER_BIT)_$(DATA_WIDTH)_$(NUM_DATA_BITS)_$(NUM_STOP_BITS)_$(PARITY_EN)
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

# Allow `include files from the source dir
COMPILE_ARGS += -I$(SRC_DIR)

# Test modules, comma-separated, no .py
COCOTB_TEST_MODULES = test_uart_controller

# Use a cocotb venv if one exists (override with: make VENV=/path/to/venv)
VENV ?= $(HOME)/cocotb-env
ifneq ($(wildcard $(VENV)/bin/cocotb-config),)
export PATH := $(VENV)/bin:$(PATH)
endif

include $(shell PATH="$(PATH)" cocotb-config --makefiles)/Makefile.sim
