`default_nettype none

module pem_core_array #(
    parameter int NUM_CORES      = 4,
    parameter int NUM_SYNC_FLAGS = 8
) (
    input  logic                     clk,
    input  logic                     rst,
    input  logic [NUM_CORES-1:0]     i_soft_reset,
    input  logic [NUM_CORES-1:0]     i_halt_req,

    input  logic [3:0]               i_csr_addr,
    input  logic [15:0]              i_csr_din,
    input  logic [NUM_CORES-1:0]     i_csr_wren,
    output logic [NUM_CORES*16-1:0]  o_csr_dout,
    output logic [NUM_CORES-1:0]     o_csr_error,

    output logic [NUM_CORES*5-1:0]   o_imem_addr,
    input  logic [NUM_CORES*16-1:0]  i_imem_din,

    input  logic [NUM_CORES*8-1:0]   i_tx_fifo_data,
    input  logic [NUM_CORES-1:0]     i_tx_fifo_valid,
    output logic [NUM_CORES-1:0]     o_tx_fifo_ready,
    output logic [NUM_CORES-1:0]     o_tx_fallback_event,

    output logic [NUM_CORES*8-1:0]   o_rx_fifo_data,
    output logic [NUM_CORES-1:0]     o_rx_fifo_valid,
    input  logic [NUM_CORES-1:0]     i_rx_fifo_ready,
    output logic [NUM_CORES-1:0]     o_rx_drop_event,

    input  logic [NUM_CORES*8-1:0]   i_virt_in,
    output logic [NUM_CORES*8-1:0]   o_virt_out,
    output logic [NUM_CORES*8-1:0]   o_virt_outen,

    input  logic [1:0]               i_sync_csr_addr,
    input  logic [15:0]              i_sync_csr_din,
    input  logic                     i_sync_csr_wren,
    output logic [15:0]              o_sync_csr_dout,
    output logic                     o_sync_csr_error
);

    // -------------------- Registers and internal state --------------------

    typedef enum logic [1:0] {
        SYNC_STATE  = 2'd0,
        SYNC_SET    = 2'd1,
        SYNC_CLEAR  = 2'd2,
        SYNC_STATUS = 2'd3
    } sync_csr_t;

    logic [NUM_CORES*3-1:0]     core_irq_addr;
    logic [NUM_CORES-1:0]       core_irq_din;
    logic [NUM_CORES-1:0]       core_irq_dout;
    logic [NUM_CORES-1:0]       core_irq_wren;
    logic [NUM_CORES-1:0]       core_irq_wait_clear;

    logic [NUM_SYNC_FLAGS-1:0]  sync_flags_q;
    logic [NUM_SYNC_FLAGS-1:0]  sync_flags_d;
    logic [NUM_SYNC_FLAGS-1:0]  request_seen;
    logic [NUM_SYNC_FLAGS-1:0]  wait_clear_seen;
    logic [NUM_SYNC_FLAGS-1:0]  explicit_write_seen;
    logic                      sync_collision;
    logic                      sync_collision_event;
    logic                      sync_collision_clear;
    logic                      sync_csr_write_accepted;
    logic                      sync_csr_write_rejected;

    // -------------------- Core instances --------------------

    generate
        if (NUM_CORES < 1 || NUM_CORES > 256) begin : g_invalid_num_cores
            initial begin
                $fatal(1, "NUM_CORES must be in the range 1-256 (got %0d)", NUM_CORES);
            end
        end

        if (NUM_SYNC_FLAGS != 8) begin : g_invalid_num_sync_flags
            initial begin
                $fatal(1, "NUM_SYNC_FLAGS must be 8 for the current ISA (got %0d)", NUM_SYNC_FLAGS);
            end
        end

        for (genvar core = 0; core < NUM_CORES; core++) begin : g_core
            assign core_irq_din[core] = sync_flags_q[core_irq_addr[core*3 +: 3]];

            pem_core #(
                .CORE_ID(core)
            ) u_core (
                .clk                 (clk),
                .rst                 (rst),
                .i_soft_reset        (i_soft_reset[core]),
                .i_halt_req          (i_halt_req[core]),

                .i_csr_addr          (i_csr_addr),
                .i_csr_din           (i_csr_din),
                .o_csr_dout          (o_csr_dout[core*16 +: 16]),
                .i_csr_wren          (i_csr_wren[core]),
                .o_csr_error         (o_csr_error[core]),

                .o_imem_addr         (o_imem_addr[core*5 +: 5]),
                .i_imem_din          (i_imem_din[core*16 +: 16]),

                .i_tx_fifo_data      (i_tx_fifo_data[core*8 +: 8]),
                .i_tx_fifo_valid     (i_tx_fifo_valid[core]),
                .o_tx_fifo_ready     (o_tx_fifo_ready[core]),
                .o_tx_fallback_event (o_tx_fallback_event[core]),

                .o_rx_fifo_data      (o_rx_fifo_data[core*8 +: 8]),
                .o_rx_fifo_valid     (o_rx_fifo_valid[core]),
                .i_rx_fifo_ready     (i_rx_fifo_ready[core]),
                .o_rx_drop_event     (o_rx_drop_event[core]),

                .i_virt_in           (i_virt_in[core*8 +: 8]),
                .o_virt_out          (o_virt_out[core*8 +: 8]),
                .o_virt_outen        (o_virt_outen[core*8 +: 8]),

                .o_irq_addr          (core_irq_addr[core*3 +: 3]),
                .i_irq_din           (core_irq_din[core]),
                .o_irq_dout          (core_irq_dout[core]),
                .o_irq_wren          (core_irq_wren[core]),
                .o_irq_wait_clear    (core_irq_wait_clear[core])
            );
        end
    endgenerate

    // -------------------- Synchronization CSR access --------------------

    // The router forwards SET/CLEAR writes only while all cores are halted.
    assign sync_csr_write_accepted = i_sync_csr_wren && !rst && i_sync_csr_addr != SYNC_STATE;
    assign sync_csr_write_rejected = i_sync_csr_wren && !rst && i_sync_csr_addr == SYNC_STATE;
    assign sync_collision_clear = sync_csr_write_accepted &&
                                  i_sync_csr_addr == SYNC_STATUS && i_sync_csr_din[0];

    always_comb begin
        case (i_sync_csr_addr)
            SYNC_STATE: begin
                o_sync_csr_dout = {{(16-NUM_SYNC_FLAGS){1'b0}}, sync_flags_q};
            end
            SYNC_STATUS: begin
                o_sync_csr_dout = {15'd0, sync_collision};
            end
            default: begin
                o_sync_csr_dout = 16'd0;
            end
        endcase
    end

    // -------------------- Synchronization arbitration --------------------

    always_comb begin
        sync_flags_d         = sync_flags_q;
        request_seen         = '0;
        wait_clear_seen      = '0;
        explicit_write_seen  = '0;
        sync_collision_event = 1'b0;

        if (sync_csr_write_accepted) begin
            case (i_sync_csr_addr)
                SYNC_SET: begin
                    sync_flags_d = sync_flags_q | i_sync_csr_din[NUM_SYNC_FLAGS-1:0];
                end
                SYNC_CLEAR: begin
                    sync_flags_d = sync_flags_q & ~i_sync_csr_din[NUM_SYNC_FLAGS-1:0];
                end
                default: begin
                end
            endcase
        end

        for (int flag = 0; flag < NUM_SYNC_FLAGS; flag++) begin
            for (int core = 0; core < NUM_CORES; core++) begin
                if (core_irq_wren[core] && core_irq_addr[core*3 +: 3] == flag[2:0]) begin
                    if (!request_seen[flag]) begin
                        sync_flags_d[flag] = core_irq_wait_clear[core] ? 1'b0 : core_irq_dout[core];
                        request_seen[flag] = 1'b1;
                    end

                    if (core_irq_wait_clear[core]) begin
                        if (explicit_write_seen[flag]) begin
                            sync_collision_event = 1'b1;
                        end
                        wait_clear_seen[flag] = 1'b1;
                    end else begin
                        if (explicit_write_seen[flag] || wait_clear_seen[flag]) begin
                            sync_collision_event = 1'b1;
                        end
                        explicit_write_seen[flag] = 1'b1;
                    end
                end
            end
        end
    end

    // -------------------- Shared state updates --------------------

    always_ff @(posedge clk) begin
        if (rst) begin
            sync_flags_q     <= '0;
            sync_collision   <= 1'b0;
            o_sync_csr_error <= 1'b0;
        end else begin
            sync_flags_q     <= sync_flags_d;
            o_sync_csr_error <= sync_csr_write_rejected;

            if (sync_collision_event) begin
                sync_collision <= 1'b1;
            end else if (sync_collision_clear) begin
                sync_collision <= 1'b0;
            end
        end
    end

endmodule

`default_nettype wire

