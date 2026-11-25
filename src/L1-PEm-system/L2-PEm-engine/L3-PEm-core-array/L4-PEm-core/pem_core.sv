`default_nettype none

module pem_core #(
    parameter logic [1:0] CORE_ID = 2'd0
) (
    input  logic        clk,
    input  logic        rst,
    input  logic        i_soft_reset,
    input  logic        i_halt_req,

    input  logic [3:0]  i_csr_addr,
    input  logic [15:0] i_csr_din,
    output logic [15:0] o_csr_dout,
    input  logic        i_csr_wren,
    output logic        o_csr_error,

    output logic [4:0]  o_imem_addr,
    input  logic [15:0] i_imem_din,

    input  logic [7:0]  i_tx_fifo_data,
    input  logic        i_tx_fifo_valid,
    output logic        o_tx_fifo_ready,
    output logic        o_tx_fallback_event,

    output logic [7:0]  o_rx_fifo_data,
    output logic        o_rx_fifo_valid,
    input  logic        i_rx_fifo_ready,
    output logic        o_rx_drop_event,

    input  logic [7:0]  i_virt_in,
    output logic [7:0]  o_virt_out,
    output logic [7:0]  o_virt_outen,

    output logic [2:0]  o_irq_addr,
    input  logic        i_irq_din,
    output logic        o_irq_dout,
    output logic        o_irq_wren,
    output logic        o_irq_wait_clear
);

    localparam logic [3:0] CSR_SM_CTRL      = 4'h0;
    localparam logic [3:0] CSR_SM_STATUS    = 4'h1;
    localparam logic [3:0] CSR_WRAP         = 4'h3;
    localparam logic [3:0] CSR_SHIFT_CTRL   = 4'h4;
    localparam logic [3:0] CSR_SHIFT_THRESH = 4'h5;
    localparam logic [3:0] CSR_CLKDIV       = 4'h6;
    localparam logic [3:0] CSR_INIT_PC      = 4'hC;
    localparam logic [3:0] CSR_PIN_CTRL     = 4'hD;

    logic        step_pending;
    logic        cont_run;
    logic        invalid_instruction;
    logic [10:0] wrap_ctrl;
    logic [3:0]  shift_ctrl;
    logic [5:0]  shift_thresh;
    logic [15:0] clkdiv;
    logic [4:0]  init_pc;
    logic [9:0]  pin_ctrl;

    logic       halted;
    logic       core_reset;
    logic       csr_access_allowed;
    logic       csr_write_accepted;
    logic       csr_write_rejected;
    logic [3:0] sideset_end;
    logic       pin_ctrl_valid;
    logic       clkdiv_write_accepted;
    logic       divider_reset;
    logic       divider_clk_en;
    logic       clk_en;

    assign core_reset = i_soft_reset ||
                        (i_csr_wren && i_csr_addr == CSR_SM_CTRL && i_csr_din[2]);
    assign halted = !step_pending && (!cont_run || i_halt_req);

    assign sideset_end = {1'b0, i_csr_din[2:0]} + {2'b0, i_csr_din[9:8]};
    assign pin_ctrl_valid = i_csr_din[9:8] != 2'd3 &&
                            (i_csr_din[9:8] == 2'd0 || sideset_end <= 4'd8);

    always_comb begin
        case (i_csr_addr)
            CSR_SM_CTRL, CSR_SM_STATUS: begin
                csr_access_allowed = 1'b1;
            end
            CSR_WRAP, CSR_SHIFT_CTRL, CSR_SHIFT_THRESH, CSR_INIT_PC: begin
                csr_access_allowed = halted;
            end
            CSR_CLKDIV: begin
                csr_access_allowed = halted && i_csr_din >= 16'h0010;
            end
            CSR_PIN_CTRL: begin
                csr_access_allowed = halted && pin_ctrl_valid;
            end
            default: begin
                csr_access_allowed = 1'b0;
            end
        endcase
    end

    assign csr_write_accepted = i_csr_wren && !rst && !core_reset && csr_access_allowed;
    assign csr_write_rejected = i_csr_wren && !rst && !core_reset && !csr_access_allowed;
    assign clkdiv_write_accepted = csr_write_accepted && i_csr_addr == CSR_CLKDIV;
    assign divider_reset = rst || core_reset || clkdiv_write_accepted;
    assign clk_en = divider_clk_en && !divider_reset;

    clock_divider u_clock_divider (
        .clk     (clk),
        .rst     (divider_reset),
        .divisor (clkdiv),
        .clk_en  (divider_clk_en)
    );

    always_comb begin
        case (i_csr_addr)
            CSR_SM_CTRL: begin
                o_csr_dout = {14'd0, cont_run, step_pending};
            end
            CSR_SM_STATUS: begin
                o_csr_dout = {12'd0, invalid_instruction, 2'b0, !halted};
            end
            CSR_WRAP: begin
                o_csr_dout = {5'd0, wrap_ctrl};
            end
            CSR_SHIFT_CTRL: begin
                o_csr_dout = {12'd0, shift_ctrl};
            end
            CSR_SHIFT_THRESH: begin
                o_csr_dout = {10'd0, shift_thresh};
            end
            CSR_CLKDIV: begin
                o_csr_dout = clkdiv;
            end
            CSR_INIT_PC: begin
                o_csr_dout = {11'd0, init_pc};
            end
            CSR_PIN_CTRL: begin
                o_csr_dout = {6'd0, pin_ctrl};
            end
            default: begin
                o_csr_dout = 16'd0;
            end
        endcase
    end

    assign o_imem_addr = 5'd0;
    assign o_virt_out = 8'd0;
    assign o_virt_outen = 8'd0;
    assign o_tx_fifo_ready = 1'b0;
    assign o_tx_fallback_event = 1'b0;
    assign o_rx_fifo_data = 8'd0;
    assign o_rx_fifo_valid = 1'b0;
    assign o_rx_drop_event = 1'b0;
    assign o_irq_addr = 3'd0;
    assign o_irq_dout = 1'b0;
    assign o_irq_wren = 1'b0;
    assign o_irq_wait_clear = 1'b0;

    always_ff @(posedge clk) begin
        o_csr_error <= csr_write_rejected;

        if (rst || core_reset) begin
            step_pending <= 1'b0;
            cont_run     <= 1'b0;
        end

        if (rst) begin
            invalid_instruction <= 1'b0;
            wrap_ctrl           <= 11'd0;
            shift_ctrl          <= 4'd0;
            shift_thresh        <= 6'd0;
            clkdiv              <= 16'h0010;
            init_pc             <= 5'd0;
            pin_ctrl            <= 10'd0;
        end else if (csr_write_accepted) begin
            case (i_csr_addr)
                CSR_SM_CTRL: begin
                    cont_run     <= i_csr_din[1];
                    step_pending <= i_csr_din[0] && !i_csr_din[1];
                end
                CSR_SM_STATUS: begin
                    if (i_csr_din[3]) begin
                        invalid_instruction <= 1'b0;
                    end
                end
                CSR_WRAP: begin
                    wrap_ctrl    <= i_csr_din[10:0];
                end
                CSR_SHIFT_CTRL: begin
                    shift_ctrl   <= i_csr_din[3:0];
                end
                CSR_SHIFT_THRESH: begin
                    shift_thresh <= i_csr_din[5:0];
                end
                CSR_CLKDIV: begin
                    clkdiv       <= i_csr_din;
                end
                CSR_INIT_PC: begin
                    init_pc      <= i_csr_din[4:0];
                end
                CSR_PIN_CTRL: begin
                    pin_ctrl     <= {i_csr_din[9:8], 1'b0, i_csr_din[6:4], 1'b0, i_csr_din[2:0]};
                end
                default: begin
                end
            endcase
        end
    end

endmodule

`default_nettype wire

