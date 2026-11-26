# Makefile
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
SIM ?= icarus
PYTHON ?= python3
FUNCTIONAL_COVERAGE ?= 0
FST ?= -fst # Use more efficient FST format
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(CURDIR)/../src
PROJECT_SOURCES = project.v
BLOCK_DIRS := $(sort $(patsubst %/Makefile,%,$(wildcard $(CURDIR)/block-level-tests/*/Makefile)))

VENV ?= $(HOME)/cocotb-env
ifneq ($(wildcard $(VENV)/bin/cocotb-config),)
export PATH := $(VENV)/bin:$(PATH)
endif

.PHONY: block-tests block-clean coverage $(BLOCK_DIRS)
block-tests: $(BLOCK_DIRS)

$(BLOCK_DIRS):
	$(MAKE) -C "$@" COCOTB_RESULTS_FILE=results.xml FUNCTIONAL_COVERAGE=$(FUNCTIONAL_COVERAGE)

block-clean:
	@for directory in $(BLOCK_DIRS); do $(MAKE) -C "$$directory" clean || exit $$?; done

coverage:
	$(MAKE) all FUNCTIONAL_COVERAGE=1

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

else

# Gate level simulation:
SIM_BUILD				= sim_build/gl
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DSIM
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13g2/libs.ref/sg13g2_io/verilog/sg13g2_io.v
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13g2/libs.ref/sg13g2_stdcell/verilog/sg13g2_stdcell.v

# this gets copied in by the GDS action workflow
VERILOG_SOURCES += $(CURDIR)/gate_level_netlist.v

endif

# Allow sharing configuration between design and testbench via `include`:
COMPILE_ARGS 		+= -I$(SRC_DIR)

# Include the testbench sources:
VERILOG_SOURCES += $(CURDIR)/tb.v
TOPLEVEL = tb

# List test modules to run, separated by commas and without the .py suffix:
COCOTB_TEST_MODULES = test

# include cocotb's make rules to take care of the simulator setup
include $(shell cocotb-config --makefiles)/Makefile.sim

ifneq ($(GATES),yes)
all: block-tests
clean:: block-clean
endif
.DEFAULT_GOAL := all

