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

    // -------------------- Registers and internal state --------------------

    localparam logic [3:0] CSR_SM_CTRL      = 4'h0;
    localparam logic [3:0] CSR_SM_STATUS    = 4'h1;
    localparam logic [3:0] CSR_PC           = 4'h2;
    localparam logic [3:0] CSR_WRAP         = 4'h3;
    localparam logic [3:0] CSR_SHIFT_CTRL   = 4'h4;
    localparam logic [3:0] CSR_SHIFT_THRESH = 4'h5;
    localparam logic [3:0] CSR_CLKDIV       = 4'h6;
    localparam logic [3:0] CSR_X            = 4'h7;
    localparam logic [3:0] CSR_Y            = 4'h8;
    localparam logic [3:0] CSR_ISR          = 4'h9;
    localparam logic [3:0] CSR_OSR          = 4'hA;
    localparam logic [3:0] CSR_SHIFT_COUNT  = 4'hB;
    localparam logic [3:0] CSR_INIT_PC      = 4'hC;
    localparam logic [3:0] CSR_PIN_CTRL     = 4'hD;
    localparam logic [3:0] CSR_PIN_OUT      = 4'hE;
    localparam logic [3:0] CSR_PIN_DIR      = 4'hF;

    localparam logic [2:0] OP_JMP       = 3'b000;
    localparam logic [2:0] OP_WAIT      = 3'b001;
    localparam logic [2:0] OP_IN        = 3'b010;
    localparam logic [2:0] OP_OUT       = 3'b011;
    localparam logic [2:0] OP_PUSH_PULL = 3'b100;
    localparam logic [2:0] OP_MOV       = 3'b101;
    localparam logic [2:0] OP_IRQ       = 3'b110;
    localparam logic [2:0] OP_SET       = 3'b111;

    typedef enum logic [2:0] {
        STATE_FETCH,
        STATE_EXECUTE,
        STATE_DELAY_1,
        STATE_DELAY_2,
        STATE_FROZEN
    } core_state_t;

    core_state_t state_q;
    core_state_t state_d;

    logic        step_pending;
    logic        cont_run;
    logic        halt_pending;
    logic        invalid_instruction;
    logic [10:0] wrap_ctrl;
    logic [3:0]  shift_ctrl;
    logic [5:0]  shift_thresh;
    logic [15:0] clkdiv;
    logic [4:0]  init_pc;
    logic [9:0]  pin_ctrl;

    logic [4:0]  pc;
    logic [4:0]  instruction_pc;
    logic [15:0] instruction;
    logic [7:0]  x;
    logic [7:0]  y;
    logic [7:0]  isr;
    logic [7:0]  osr;
    logic [3:0]  isr_count;
    logic [3:0]  osr_count;
    logic [7:0]  pin_out;
    logic [7:0]  pin_dir;
    logic [2:0]  in_base;
    logic [2:0]  out_base;
    logic [4:0]  delay_count;
    logic        irq_set_issued;

    logic        core_reset;
    logic        csr_idle;
    logic        csr_access_allowed;
    logic        csr_write_accepted;
    logic        csr_write_rejected;
    logic        control_write;
    logic [3:0]  sideset_end;
    logic        pin_ctrl_valid;
    logic        clkdiv_write_accepted;
    logic        divider_reset;
    logic        divider_clk_en;
    logic        clk_en;

    logic        local_halt_command;
    logic        halt_requested;
    logic        start_blocked;
    logic        start_allowed;
    logic        retained_instruction;
    logic        running;
    logic        halted;
    logic        stalled;
    logic        waiting_sync;
    logic [2:0]  stall_cause;
    logic [2:0]  blocked_cause;
    logic        instruction_invalid;
    logic        instruction_ready;
    logic        instruction_commit;
    logic        invalid_event;
    logic        fetch_accept;
    logic        resume_accept;
    logic        step_consumed;
    logic        execute_attempt;
    logic        irq_set_issue;
    logic        irq_relative;
    logic [2:0]  irq_index;
    logic [4:0]  instruction_delay;
    logic [7:0]  sideset_mask;
    logic [7:0]  sideset_value;
    logic [2:0]  source_select;
    logic [7:0]  source_value;
    logic [7:0]  mov_result;
    logic        jmp_taken;
    logic [3:0]  pull_threshold;
    logic [3:0]  push_threshold;
    logic        pull_required;
    logic        push_required;
    logic        out_refill;
    logic        in_autopush;
    logic [3:0]  bit_count;
    logic [7:0]  bit_mask;
    logic [7:0]  in_result;
    logic [3:0]  in_count;
    logic [7:0]  out_source;
    logic [7:0]  out_value;
    logic [7:0]  out_result;
    logic [3:0]  out_count;
    logic [7:0]  out_pin_mask;
    logic [7:0]  out_pin_value;
    logic [7:0]  set_pin_mask;
    logic [7:0]  set_pin_value;
    logic [7:0]  x_commit;
    logic [7:0]  y_commit;
    logic [7:0]  isr_commit;
    logic [7:0]  osr_commit;
    logic [3:0]  isr_count_commit;
    logic [3:0]  osr_count_commit;
    logic [7:0]  pin_out_commit;
    logic [7:0]  pin_dir_commit;
    logic [2:0]  in_base_commit;
    logic [2:0]  out_base_commit;
    logic [4:0]  pc_commit;

    function automatic logic [7:0] rotate_right (
        input logic [7:0] value,
        input logic [2:0] amount
    );
        rotate_right = (value >> amount) | (value << (4'd8 - {1'b0, amount}));
    endfunction

    function automatic logic [3:0] add_shift_count (
        input logic [3:0] previous,
        input logic [3:0] amount
    );
        logic [4:0] sum;
        sum = {1'b0, previous} + {1'b0, amount};
        add_shift_count = sum >= 5'd8 ? 4'd8 : sum[3:0];
    endfunction

    // -------------------- Clock divider --------------------

    assign core_reset = i_soft_reset ||
                        (i_csr_wren && i_csr_addr == CSR_SM_CTRL && i_csr_din[2]);

    assign clkdiv_write_accepted = csr_write_accepted && i_csr_addr == CSR_CLKDIV;
    assign divider_reset = rst || core_reset || clkdiv_write_accepted;
    assign clk_en = divider_clk_en && !divider_reset;

    clock_divider u_clock_divider (
        .clk     (clk),
        .rst     (divider_reset),
        .divisor (clkdiv),
        .clk_en  (divider_clk_en)
    );

    // -------------------- CSR access and control --------------------

    assign csr_idle = halted && !retained_instruction;
    assign csr_write_accepted = i_csr_wren && !rst && !core_reset && csr_access_allowed;
    assign csr_write_rejected = i_csr_wren && !rst && !core_reset && !csr_access_allowed;
    assign control_write = csr_write_accepted && i_csr_addr == CSR_SM_CTRL;

    assign sideset_end = {1'b0, i_csr_din[2:0]} + {2'b0, i_csr_din[9:8]};
    assign pin_ctrl_valid = i_csr_din[9:8] != 2'd3 &&
                            (i_csr_din[9:8] == 2'd0 || sideset_end <= 4'd8);

    always_comb begin
        case (i_csr_addr)
            CSR_SM_CTRL, CSR_SM_STATUS: begin
                csr_access_allowed = 1'b1;
            end
            CSR_WRAP, CSR_SHIFT_CTRL, CSR_SHIFT_THRESH, CSR_INIT_PC: begin
                csr_access_allowed = csr_idle;
            end
            CSR_CLKDIV: begin
                csr_access_allowed = csr_idle && i_csr_din >= 16'h0010;
            end
            CSR_PIN_CTRL: begin
                csr_access_allowed = csr_idle && pin_ctrl_valid;
            end
            CSR_X, CSR_Y, CSR_ISR, CSR_OSR, CSR_PIN_OUT, CSR_PIN_DIR: begin
                csr_access_allowed = csr_idle && i_csr_din[15:8] == 8'd0;
            end
            CSR_PC, CSR_SHIFT_COUNT: begin
                csr_access_allowed = 1'b0;
            end
            default: begin
                csr_access_allowed = 1'b0;
            end
        endcase
    end

    always_comb begin
        case (i_csr_addr)
            CSR_SM_CTRL: begin
                o_csr_dout = {14'd0, cont_run, step_pending};
            end
            CSR_SM_STATUS: begin
                o_csr_dout = {8'd0, stall_cause, 1'b0, invalid_instruction,
                              waiting_sync, stalled, running};
            end
            CSR_PC: begin
                o_csr_dout = {11'd0, pc};
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
            CSR_X: begin
                o_csr_dout = {8'd0, x};
            end
            CSR_Y: begin
                o_csr_dout = {8'd0, y};
            end
            CSR_ISR: begin
                o_csr_dout = {8'd0, isr};
            end
            CSR_OSR: begin
                o_csr_dout = {8'd0, osr};
            end
            CSR_SHIFT_COUNT: begin
                o_csr_dout = {8'd0, osr_count, isr_count};
            end
            CSR_INIT_PC: begin
                o_csr_dout = {11'd0, init_pc};
            end
            CSR_PIN_CTRL: begin
                o_csr_dout = {6'd0, pin_ctrl};
            end
            CSR_PIN_OUT: begin
                o_csr_dout = {8'd0, pin_out};
            end
            CSR_PIN_DIR: begin
                o_csr_dout = {8'd0, pin_dir};
            end
            default: begin
                o_csr_dout = 16'd0;
            end
        endcase
    end

    always_ff @(posedge clk) begin
        o_csr_error <= csr_write_rejected;
    end

    always_ff @(posedge clk) begin
        if (rst || core_reset) begin
            step_pending <= 1'b0;
            cont_run     <= 1'b0;
            halt_pending <= 1'b0;
        end else if (control_write) begin
            cont_run     <= i_csr_din[1];
            step_pending <= i_csr_din[0] && !i_csr_din[1];
            halt_pending <= i_csr_din[1:0] == 2'b00;
        end else if (step_consumed) begin
            step_pending <= 1'b0;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            invalid_instruction <= 1'b0;
        end else if (invalid_event) begin
            invalid_instruction <= 1'b1;
        end else if (csr_write_accepted && i_csr_addr == CSR_SM_STATUS && i_csr_din[3]) begin
            invalid_instruction <= 1'b0;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            wrap_ctrl    <= 11'd0;
            shift_ctrl   <= 4'd0;
            shift_thresh <= 6'd0;
            clkdiv       <= 16'h0010;
            init_pc      <= 5'd0;
            pin_ctrl     <= 10'd0;
        end else if (csr_write_accepted) begin
            case (i_csr_addr)
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

    // -------------------- Core decode and common commit --------------------

    assign local_halt_command = control_write && i_csr_din[1:0] == 2'b00;
    assign halt_requested = i_halt_req || halt_pending || local_halt_command;
    assign start_blocked = control_write &&
                           (!i_csr_din[0] || (cont_run && !i_csr_din[1]));
    assign start_allowed = (cont_run || step_pending) && !halt_requested && !start_blocked;

    assign retained_instruction = state_q != STATE_FETCH;
    assign running = (state_q != STATE_FETCH && state_q != STATE_FROZEN) ||
                     step_pending || (cont_run && !i_halt_req && !halt_pending);
    assign halted = !running;
    assign stall_cause = (state_q == STATE_EXECUTE || state_q == STATE_FROZEN) ?
                         blocked_cause : 3'd0;
    assign stalled = (state_q == STATE_EXECUTE || state_q == STATE_FROZEN) &&
                     stall_cause != 3'd0;
    assign waiting_sync = stalled && stall_cause == 3'd2;

    assign invalid_event = instruction_commit && instruction_invalid;
    assign step_consumed = (fetch_accept || resume_accept) && step_pending && !cont_run;
    assign pull_threshold = shift_thresh[2:0] == 3'd0 ? 4'd8 : {1'b0, shift_thresh[2:0]};
    assign push_threshold = shift_thresh[5:3] == 3'd0 ? 4'd8 : {1'b0, shift_thresh[5:3]};
    assign pull_required = (!shift_ctrl[2] && !instruction[6]) || osr_count >= pull_threshold;
    assign push_required = !instruction[6] || isr_count >= push_threshold;
    assign out_refill = shift_ctrl[2] && osr_count >= pull_threshold;
    assign bit_count = instruction[2:0] == 3'd0 ? 4'd8 : {1'b0, instruction[2:0]};
    assign bit_mask = 8'hFF >> (4'd8 - bit_count);
    assign in_count = add_shift_count(isr_count, bit_count);
    assign in_autopush = shift_ctrl[3] && in_count >= push_threshold;
    assign in_result = shift_ctrl[1] ?
                       (isr >> bit_count) | ((source_value & bit_mask) << (4'd8 - bit_count)) :
                       (isr << bit_count) | (source_value & bit_mask);
    assign out_source = out_refill ? i_tx_fifo_data : osr;
    assign out_value = shift_ctrl[0] ? out_source & bit_mask :
                      (out_source >> (4'd8 - bit_count)) & bit_mask;
    assign out_result = shift_ctrl[0] ? out_source >> bit_count : out_source << bit_count;
    assign out_count = add_shift_count(out_refill ? 4'd0 : osr_count, bit_count);
    assign out_pin_mask = rotate_right(bit_mask, 3'd0 - out_base);
    assign out_pin_value = rotate_right(out_value, 3'd0 - out_base);
    assign set_pin_mask = rotate_right(8'h1F, 3'd0 - out_base);
    assign set_pin_value = rotate_right({3'd0, instruction[4:0]}, 3'd0 - out_base);
    assign source_select = instruction[15:13] == OP_IN ? instruction[7:5] : instruction[2:0];
    assign irq_relative = instruction[15:13] == OP_WAIT ? instruction[4] : instruction[5];
    assign irq_index = {instruction[2], instruction[1:0] + (irq_relative ? CORE_ID : 2'd0)};
    assign execute_attempt = !rst && !core_reset && clk_en &&
                             ((state_q == STATE_EXECUTE && !(halt_requested && !instruction_ready)) ||
                              (state_q == STATE_FROZEN && start_allowed));
    assign irq_set_issue = execute_attempt && !instruction_invalid &&
                           instruction[15:13] == OP_IRQ && !instruction[7] &&
                           instruction[6] && !irq_set_issued;

    always_comb begin
        instruction_invalid = 1'b0;

        case (instruction[15:13])
            OP_JMP: begin
            end
            OP_WAIT: begin
                case (instruction[6:5])
                    2'd1: begin
                        instruction_invalid = instruction[4:3] != 2'd0;
                    end
                    2'd2: begin
                        instruction_invalid = instruction[3];
                    end
                    default: begin
                        instruction_invalid = 1'b1;
                    end
                endcase
            end
            OP_IN: begin
                instruction_invalid = instruction[4:3] != 2'd0 ||
                                      instruction[7:5] == 3'd4 || instruction[7:5] == 3'd5;
            end
            OP_OUT: begin
                instruction_invalid = instruction[4:3] != 2'd0 || instruction[7:5] == 3'd7;
            end
            OP_PUSH_PULL: begin
                instruction_invalid = instruction[4:0] != 5'd0;
            end
            OP_MOV: begin
                instruction_invalid = instruction[7:5] == 3'd3 ||
                                      instruction[7:5] == 3'd7 ||
                                      instruction[4:3] == 2'd3 ||
                                      instruction[2:0] == 3'd5;
            end
            OP_IRQ: begin
                instruction_invalid = instruction[4:3] != 2'd0;
            end
            OP_SET: begin
                instruction_invalid = instruction[7:5] == 3'd3 ||
                                      instruction[7:5] == 3'd6 ||
                                      ((instruction[7:5] == 3'd5 || instruction[7:5] == 3'd7) &&
                                       instruction[4:3] != 2'd0);
            end
            default: begin
                instruction_invalid = 1'b1;
            end
        endcase
    end

    always_comb begin
        instruction_ready = 1'b1;
        blocked_cause = 3'd0;

        if (!instruction_invalid) begin
            case (instruction[15:13])
                OP_WAIT: begin
                    if (instruction[6:5] == 2'd1) begin
                        instruction_ready = i_virt_in[instruction[2:0]] == instruction[7];
                        blocked_cause = instruction_ready ? 3'd0 : 3'd1;
                    end else begin
                        instruction_ready = i_irq_din == instruction[7];
                        blocked_cause = instruction_ready ? 3'd0 : 3'd2;
                    end
                end
                OP_IN: begin
                    instruction_ready = !in_autopush || i_rx_fifo_ready;
                    blocked_cause = instruction_ready ? 3'd0 : 3'd4;
                end
                OP_OUT: begin
                    instruction_ready = !out_refill || i_tx_fifo_valid;
                    blocked_cause = instruction_ready ? 3'd0 : 3'd3;
                end
                OP_PUSH_PULL: begin
                    if (instruction[7]) begin
                        instruction_ready = !pull_required || !instruction[5] || i_tx_fifo_valid;
                        blocked_cause = instruction_ready ? 3'd0 : 3'd3;
                    end else begin
                        instruction_ready = !push_required || !instruction[5] || i_rx_fifo_ready;
                        blocked_cause = instruction_ready ? 3'd0 : 3'd4;
                    end
                end
                OP_IRQ: begin
                    if (!instruction[7] && instruction[6]) begin
                        instruction_ready = irq_set_issued && !i_irq_din;
                        blocked_cause = irq_set_issued && i_irq_din ? 3'd2 : 3'd0;
                    end
                end
                default: begin
                end
            endcase
        end
    end

    always_comb begin
        case (source_select)
            3'd0: begin
                source_value = rotate_right(i_virt_in, in_base);
            end
            3'd1: begin
                source_value = x;
            end
            3'd2: begin
                source_value = y;
            end
            3'd3: begin
                source_value = 8'd0;
            end
            3'd4: begin
                source_value = {6'd0, CORE_ID};
            end
            3'd6: begin
                source_value = isr;
            end
            3'd7: begin
                source_value = osr;
            end
            default: begin
                source_value = 8'd0;
            end
        endcase

        case (instruction[4:3])
            2'd0: begin
                mov_result = source_value;
            end
            2'd1: begin
                mov_result = ~source_value;
            end
            2'd2: begin
                mov_result = {source_value[0], source_value[1], source_value[2], source_value[3],
                              source_value[4], source_value[5], source_value[6], source_value[7]};
            end
            default: begin
                mov_result = 8'd0;
            end
        endcase
    end

    always_comb begin
        case (instruction[7:5])
            3'd0: begin
                jmp_taken = 1'b1;
            end
            3'd1: begin
                jmp_taken = x == 8'd0;
            end
            3'd2: begin
                jmp_taken = x != 8'd0;
            end
            3'd3: begin
                jmp_taken = y == 8'd0;
            end
            3'd4: begin
                jmp_taken = y != 8'd0;
            end
            3'd5: begin
                jmp_taken = x != y;
            end
            3'd6: begin
                jmp_taken = i_virt_in[pin_ctrl[6:4]];
            end
            3'd7: begin
                jmp_taken = osr_count < pull_threshold;
            end
            default: begin
                jmp_taken = 1'b0;
            end
        endcase
    end

    always_comb begin
        instruction_delay = 5'd0;
        sideset_mask = 8'd0;
        sideset_value = 8'd0;

        case (pin_ctrl[9:8])
            2'd0: begin
                instruction_delay = instruction[12:8];
            end
            2'd1: begin
                instruction_delay = {1'b0, instruction[11:8]};
                sideset_mask = 8'h01 << pin_ctrl[2:0];
                sideset_value = {7'd0, instruction[12]} << pin_ctrl[2:0];
            end
            2'd2: begin
                instruction_delay = {2'd0, instruction[10:8]};
                sideset_mask = 8'h03 << pin_ctrl[2:0];
                sideset_value = {6'd0, instruction[12:11]} << pin_ctrl[2:0];
            end
            default: begin
            end
        endcase
    end

    always_comb begin
        x_commit = x;
        y_commit = y;
        isr_commit = isr;
        osr_commit = osr;
        isr_count_commit = isr_count;
        osr_count_commit = osr_count;
        pin_out_commit = pin_out;
        pin_dir_commit = pin_dir;
        in_base_commit = in_base;
        out_base_commit = out_base;
        pc_commit = wrap_ctrl[10] && instruction_pc == wrap_ctrl[9:5] ?
                    wrap_ctrl[4:0] : instruction_pc + 5'd1;

        if (!instruction_invalid) begin
            case (instruction[15:13])
                OP_IN: begin
                    if (in_autopush) begin
                        isr_commit = 8'd0;
                        isr_count_commit = 4'd0;
                    end else begin
                        isr_commit = in_result;
                        isr_count_commit = in_count;
                    end
                end
                OP_OUT: begin
                    osr_commit = out_result;
                    osr_count_commit = out_count;

                    case (instruction[7:5])
                        3'd0: begin
                            pin_out_commit = (pin_out & ~out_pin_mask) | out_pin_value;
                        end
                        3'd1: begin
                            x_commit = out_value;
                        end
                        3'd2: begin
                            y_commit = out_value;
                        end
                        3'd3: begin
                        end
                        3'd4: begin
                            pin_dir_commit = (pin_dir & ~out_pin_mask) | out_pin_value;
                        end
                        3'd5: begin
                            pc_commit = out_value[4:0];
                        end
                        3'd6: begin
                            isr_commit = out_value;
                            isr_count_commit = bit_count;
                        end
                        default: begin
                        end
                    endcase
                end
                OP_PUSH_PULL: begin
                    if (instruction[7]) begin
                        if (pull_required) begin
                            osr_commit = i_tx_fifo_valid ? i_tx_fifo_data : x;
                            osr_count_commit = 4'd0;
                        end
                    end else if (push_required) begin
                        isr_commit = 8'd0;
                        isr_count_commit = 4'd0;
                    end
                end
                OP_SET: begin
                    case (instruction[7:5])
                        3'd0: begin
                            pin_out_commit = (pin_out & ~set_pin_mask) | set_pin_value;
                        end
                        3'd1: begin
                            x_commit = {3'd0, instruction[4:0]};
                        end
                        3'd2: begin
                            y_commit = {3'd0, instruction[4:0]};
                        end
                        3'd4: begin
                            pin_dir_commit = (pin_dir & ~set_pin_mask) | set_pin_value;
                        end
                        3'd5: begin
                            in_base_commit = instruction[2:0];
                        end
                        3'd7: begin
                            out_base_commit = instruction[2:0];
                        end
                        default: begin
                        end
                    endcase
                end
                OP_MOV: begin
                    case (instruction[7:5])
                        3'd0: begin
                            pin_out_commit = rotate_right(mov_result, 3'd0 - out_base);
                        end
                        3'd1: begin
                            x_commit = mov_result;
                        end
                        3'd2: begin
                            y_commit = mov_result;
                        end
                        3'd4: begin
                            pc_commit = mov_result[4:0];
                        end
                        3'd5: begin
                            isr_commit = mov_result;
                            isr_count_commit = 4'd0;
                        end
                        3'd6: begin
                            osr_commit = mov_result;
                            osr_count_commit = 4'd0;
                        end
                        default: begin
                        end
                    endcase
                end
                OP_JMP: begin
                    if (jmp_taken) begin
                        pc_commit = instruction[4:0];
                    end

                    if (instruction[7:5] == 3'd2) begin
                        x_commit = x - 8'd1;
                    end else if (instruction[7:5] == 3'd4) begin
                        y_commit = y - 8'd1;
                    end
                end
                default: begin
                end
            endcase
        end

        pin_out_commit = (pin_out_commit & ~sideset_mask) | sideset_value;
    end

    assign o_imem_addr = pc;
    assign o_virt_out = pin_out;
    assign o_virt_outen = pin_dir;
    assign o_tx_fifo_ready = instruction_commit && !instruction_invalid && i_tx_fifo_valid &&
                             ((instruction[15:13] == OP_OUT && out_refill) ||
                              (instruction[15:13] == OP_PUSH_PULL && instruction[7] && pull_required));
    assign o_tx_fallback_event = instruction_commit && !instruction_invalid &&
                                 instruction[15:13] == OP_PUSH_PULL && instruction[7] &&
                                 pull_required && !i_tx_fifo_valid;
    assign o_rx_fifo_data = instruction[15:13] == OP_IN ? in_result : isr;
    assign o_rx_fifo_valid = instruction_commit && !instruction_invalid && i_rx_fifo_ready &&
                             ((instruction[15:13] == OP_IN && in_autopush) ||
                              (instruction[15:13] == OP_PUSH_PULL && !instruction[7] && push_required));
    assign o_rx_drop_event = instruction_commit && !instruction_invalid &&
                             instruction[15:13] == OP_PUSH_PULL && !instruction[7] &&
                             push_required && !i_rx_fifo_ready;
    assign o_irq_addr = irq_index;
    assign o_irq_wait_clear = instruction_commit && !instruction_invalid &&
                              instruction[15:13] == OP_WAIT && instruction[6:5] == 2'd2 && instruction[7];
    assign o_irq_wren = irq_set_issue || o_irq_wait_clear ||
                       (instruction_commit && !instruction_invalid && instruction[15:13] == OP_IRQ &&
                        (instruction[7] || !instruction[6]));
    assign o_irq_dout = instruction[15:13] == OP_IRQ && !instruction[7];

    // -------------------- State machine --------------------

    always_comb begin
        state_d = state_q;
        fetch_accept = 1'b0;
        resume_accept = 1'b0;
        instruction_commit = 1'b0;

        if (!rst && !core_reset) begin
            unique case (state_q)
                STATE_FETCH: begin
                    if (clk_en && start_allowed) begin
                        fetch_accept = 1'b1;
                        state_d = STATE_EXECUTE;
                    end
                end
                STATE_EXECUTE: begin
                    if (halt_requested && !instruction_ready) begin
                        state_d = STATE_FROZEN;
                    end else if (clk_en && instruction_ready) begin
                        instruction_commit = 1'b1;

                        if (instruction_delay == 5'd0) begin
                            state_d = STATE_FETCH;
                        end else begin
                            state_d = STATE_DELAY_1;
                        end
                    end
                end
                STATE_DELAY_1: begin
                    if (clk_en) begin
                        state_d = STATE_DELAY_2;
                    end
                end
                STATE_DELAY_2: begin
                    if (clk_en) begin
                        if (delay_count <= 5'd1) begin
                            state_d = STATE_FETCH;
                        end else begin
                            state_d = STATE_DELAY_1;
                        end
                    end
                end
                STATE_FROZEN: begin
                    if (clk_en && start_allowed) begin
                        resume_accept = 1'b1;

                        if (instruction_ready) begin
                            instruction_commit = 1'b1;

                            if (instruction_delay == 5'd0) begin
                                state_d = STATE_FETCH;
                            end else begin
                                state_d = STATE_DELAY_1;
                            end
                        end else begin
                            state_d = STATE_EXECUTE;
                        end
                    end
                end
                default: begin
                    state_d = STATE_FETCH;
                end
            endcase
        end
    end

    always_ff @(posedge clk) begin
        if (rst || core_reset) begin
            state_q <= STATE_FETCH;
        end else begin
            state_q <= state_d;
        end
    end

    // -------------------- Execution register updates --------------------

    always_ff @(posedge clk) begin
        if (rst || core_reset) begin
            pc             <= rst ? 5'd0 : init_pc;
            instruction_pc <= 5'd0;
            instruction    <= 16'd0;
            x              <= 8'd0;
            y              <= 8'd0;
            isr            <= 8'd0;
            osr            <= 8'd0;
            isr_count      <= 4'd0;
            osr_count      <= 4'd8;
            pin_out        <= 8'd0;
            pin_dir        <= 8'd0;
            in_base        <= 3'd0;
            out_base       <= 3'd0;
            delay_count    <= 5'd0;
            irq_set_issued <= 1'b0;
        end else begin
            if (csr_write_accepted) begin
                case (i_csr_addr)
                    CSR_X: begin
                        x         <= i_csr_din[7:0];
                    end
                    CSR_Y: begin
                        y         <= i_csr_din[7:0];
                    end
                    CSR_ISR: begin
                        isr       <= i_csr_din[7:0];
                        isr_count <= 4'd0;
                    end
                    CSR_OSR: begin
                        osr       <= i_csr_din[7:0];
                        osr_count <= 4'd0;
                    end
                    CSR_PIN_OUT: begin
                        pin_out   <= i_csr_din[7:0];
                    end
                    CSR_PIN_DIR: begin
                        pin_dir   <= i_csr_din[7:0];
                    end
                    default: begin
                    end
                endcase
            end

            if (fetch_accept) begin
                instruction_pc <= pc;
                instruction    <= i_imem_din;
                irq_set_issued <= 1'b0;
            end else if (irq_set_issue) begin
                irq_set_issued <= 1'b1;
            end

            if (instruction_commit) begin
                pc          <= pc_commit;
                x           <= x_commit;
                y           <= y_commit;
                isr         <= isr_commit;
                osr         <= osr_commit;
                isr_count   <= isr_count_commit;
                osr_count   <= osr_count_commit;
                pin_out     <= pin_out_commit;
                pin_dir     <= pin_dir_commit;
                in_base     <= in_base_commit;
                out_base    <= out_base_commit;
                delay_count <= instruction_delay;
            end else if (clk_en && state_q == STATE_DELAY_2 && delay_count != 5'd0) begin
                delay_count <= delay_count - 5'd1;
            end
        end
    end

endmodule

`default_nettype wire

