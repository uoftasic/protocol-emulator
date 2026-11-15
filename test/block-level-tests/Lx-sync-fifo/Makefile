# Makefile — block-level test: sync_fifo
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
SIM ?= icarus
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(CURDIR)/../../../src/Lx-sync-fifo
PROJECT_SOURCES = sync_fifo.v

# RTL simulation only (gate-level is done at the top level)
SIM_BUILD = sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

# Allow `include files from the source dir
COMPILE_ARGS += -I$(SRC_DIR)

# No tb.v wrapper needed: cocotb drives the module directly
COCOTB_TOPLEVEL = sync_fifo

# Test modules, comma-separated, no .py
COCOTB_TEST_MODULES = test_fifo

# Use a cocotb venv if one exists (override with: make VENV=/path/to/venv)
VENV ?= $(HOME)/cocotb-env
ifneq ($(wildcard $(VENV)/bin/cocotb-config),)
export PATH := $(VENV)/bin:$(PATH)
endif

include $(shell PATH="$(PATH)" cocotb-config --makefiles)/Makefile.sim