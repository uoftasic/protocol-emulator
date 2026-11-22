// pem_link_rx -- MISO receive path and RX FIFOs (MCU -> SM direction)
// MISO deserializer; record parser (wait-header / SEQ / payload / skip); SEQ vs E[s] compare
// and E[s] update; nack_sent and NACK requests; N RX FIFOs with the SM rx_* handshake.
// FIFO full: drop rest of record (mode 0), or predict clk1_hold one byte ahead (mode 1).
// Header-error counter and RX FIFO levels.
// State owned: E[s], nack_sent[s], parser FSM, RX FIFOs, st_hdr_err

`default_nettype none

module pem_link_rx #(
    parameter N = 4,                     // Number of SMs
    parameter DEPTH = 8
) (
    input  wire           clk,          // System clock
    input  wire           rst_n,        // Asynchronous reset, active low

    // ---- SPI pin ----
    input  wire           miso,         // Serial data MCU -> PEm

    // ---- From pem_link_clk ----
    input  wire           miso_smp,     // Sample MISO this cycle
    input  wire           miso_last,    // This sample is the last bit (bit 0); the byte is complete after it

    // ---- From pem_link_ctrl ----
    input  wire           mode_q,       // Latched mode: FIFO full -> drop (0) or pause CLK1 (1)
    input  wire [N-1:0]   sm_clr,       // Clear SM s: RX FIFO, E[s], nack_sent[s]; an in-flight MISO record for s is skipped to its end

    // ---- SM-side RX handshake ----
    output wire [8*N-1:0] rx_data,      // Head byte of RX FIFO[s]
    output wire [N-1:0]   rx_valid,     // RX FIFO[s] is non-empty
    input  wire [N-1:0]   rx_ready,     // SM s pulls one byte this cycle

    // ---- To pem_link_clk ----
    // Mode 1 only. Set at the bit-7 sample of byte n, for byte n+1, from registered state:
    //   1 if byte n+1 will be a payload byte written to RX FIFO[s] (byte n is SEQ, or a
    //   non-last payload byte of an accepted record; after SEQ the record is assumed accepted)
    //   and RX FIFO[s] level + (byte n written to RX FIFO[s] ? 1 : 0) >= 8.
    // While CLK1 is paused: 1 only while the waiting byte is a payload byte for a full RX FIFO[s].
    output wire           clk1_hold,    // Do not clock the next MISO byte (predicted one byte ahead)

    // ---- To pem_link_tx ----
    output wire [N-1:0]   rx_pop,       // One-cycle pulse when SM s pulls a byte (rx_valid & rx_ready)
    output logic [N-1:0]  nack_req,    // One-cycle pulse requesting a NACK for SM s (only when nack_sent[s] = 0)
    output wire [8*N-1:0] exp_pos,      // Current E[s]: stream position of the next byte to accept

    // ---- CSR ----
    input  wire           clr_hdr_err,  // Single-cycle pulse: clear st_hdr_err
    output wire [7:0]     st_hdr_err,   // MISO format-error count, saturates at 255
    output wire [4*N-1:0] st_rx_level   // RX FIFO[s] occupancy, 0-8 bytes
);

    typedef enum logic [1:0] {WAIT, PARSE, RECEIVE, DISCARD} state_t;
    state_t state_d,state_q;
    logic [7:0] shift_reg, shift_databus;
    logic [8*N-1:0] expected_seq;
    logic [5:0] payload_len_q [N-1 : 0];
    logic [5:0] payload_len;
    logic shift_valid;
    logic [$clog2(N)-1 : 0] dest_fifo_q;
    logic [N-1 : 0] fifo_full, fifo_empty, fifo_rden, fifo_wren;
    logic [N-1 : 0] nack_sent;      // One NACK per gap: set with nack_req, cleared when a payload byte is accepted
    logic [7:0]     seq_diff;       // (SEQ - E[s]) mod 256

    // sm_clr for the SM of the in-flight record: the rest of the record is skipped silently
    logic clr_cur;                  // sm_clr hits the SM of the current record this cycle
    logic skip_q;                   // sm_clr hit the current record while waiting for its SEQ byte

    // Mode 1 clk1_hold
    logic       first_q;            // Next miso_smp is bit 7 of a new byte
    logic       done_q;             // The byte whose bit 7 was last sampled has been processed by the parser
    logic       hold_q;             // clk1_hold register
    logic       bit7_smp;
    logic       hold_pred;          // Prediction for byte n+1, taken at the bit-7 sample of byte n
    logic       hold_live;          // While paused: the waiting byte is a payload byte for a full FIFO
    logic [3:0] lvl_cur;            // RX FIFO level of the current record's SM

    assign fifo_rden = rx_ready & rx_valid;
    assign rx_valid = ~fifo_empty;
    assign rx_pop = fifo_rden;
    assign exp_pos = expected_seq;
    assign clr_cur = sm_clr[dest_fifo_q];

    // TODO: placeholder driver, not implemented yet
    assign st_hdr_err = 8'd0;

    // ---------------------------------------------------------------------
    // Mode 1: clk1_hold
    //   At the bit-7 sample of byte n, state_q describes byte n itself:
    //     PARSE   -> byte n is SEQ, byte n+1 is the first payload byte (record assumed accepted);
    //                byte n is not written, so hold if level >= DEPTH
    //     RECEIVE -> byte n is a payload byte that will be written; if it is not the last one,
    //                byte n+1 is payload too, so hold if level + 1 >= DEPTH
    //   Once byte n has been processed (done_q), state_q describes the waiting byte n+1, and the
    //   hold follows the live condition: released as soon as the FIFO has space, the record is
    //   dropped (SEQ mismatch) or the SM is cleared.
    //   hold_q = 1 at a CLK1 byte boundary <=> CLK1 is paused for that byte.
    // ---------------------------------------------------------------------
    assign bit7_smp  = miso_smp & first_q;
    assign lvl_cur   = st_rx_level[dest_fifo_q*4 +: 4];
    assign hold_pred = !clr_cur &&
                       ((state_q == PARSE   && !skip_q && lvl_cur >= DEPTH) ||
                        (state_q == RECEIVE && payload_len_q[dest_fifo_q] != '0 && lvl_cur >= DEPTH - 1));
    assign hold_live = (state_q == RECEIVE) && fifo_full[dest_fifo_q];
    assign clk1_hold = hold_q;

    always_ff @(posedge clk or negedge rst_n) begin : CLK1_HOLD
        if(!rst_n) begin
            first_q <= 1'b1;
            done_q  <= 1'b0;
            hold_q  <= 1'b0;
        end else begin
            if(miso_smp) first_q <= miso_last;

            if(bit7_smp)         done_q <= 1'b0;
            else if(shift_valid) done_q <= 1'b1;

            if(!mode_q)                hold_q <= 1'b0;
            else if(bit7_smp)          hold_q <= hold_pred;
            else if(hold_q && done_q)  hold_q <= hold_live;
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin : TRANSACTION_SM
        if(!rst_n) begin 
            state_q <= WAIT;
            payload_len_q <= '{default: '0};
            dest_fifo_q <= '0;
            expected_seq <= '0;
            nack_sent <= '0;
            skip_q <= 1'b0;
        end else begin
            state_q <= state_d;
            if(state_q != PARSE) skip_q <= 1'b0;
            else if(clr_cur)     skip_q <= 1'b1;
            if(shift_valid) begin
                if(state_q == WAIT && state_d == PARSE) begin
                    dest_fifo_q <= shift_databus[5:4];
                    payload_len_q[shift_databus[5:4]] <= payload_len;
                end
                // Count payload bytes only (not the SEQ byte), accepted or discarded
                if(state_q == RECEIVE || state_q == DISCARD) begin
                    payload_len_q[dest_fifo_q] <= payload_len_q[dest_fifo_q] - 1;
                end
            end
            // E[s] advances only on a byte actually written into RX FIFO[s]
            if(fifo_wren[dest_fifo_q]) expected_seq[dest_fifo_q*8 +: 8] <= expected_seq[dest_fifo_q*8 +: 8] + 1;
            // nack_sent: set together with nack_req, cleared when a payload byte is accepted
            foreach (nack_sent[j]) begin
                if(fifo_wren[j])     nack_sent[j] <= 1'b0;
                else if(nack_req[j]) nack_sent[j] <= 1'b1;
            end
            // sm_clr last so it wins over the updates above
            foreach (sm_clr[j]) begin
                if(sm_clr[j]) begin
                    expected_seq[8*j +: 8] <= '0;
                    nack_sent[j]           <= 1'b0;
                end
            end
        end
    end
    always_comb begin

        state_d = state_q;
        fifo_wren = '0;
        nack_req = '0;
        seq_diff = shift_databus - expected_seq[dest_fifo_q*8 +: 8];
        case (state_q)
            WAIT: begin
                // Header 10ss_llll; reserved length codes 12-15 are not a header
                if(shift_valid && shift_databus[7:6] == 2'b10 && shift_databus[3:0] < 4'd12) state_d = PARSE;
            end
            PARSE: begin
                if(shift_valid) begin
                    if(skip_q || clr_cur) begin
                        state_d = DISCARD;                  // SM cleared mid-record: skip silently
                    end else if(seq_diff == 8'd0) begin
                        state_d = RECEIVE;                  // diff = 0: accept
                    end else begin
                        state_d = DISCARD;                  // diff != 0: drop the whole record
                        // diff = 1-127: MCU is ahead, NACK once per gap
                        // diff = 128-255: retransmission of accepted bytes, drop silently
                        if(!seq_diff[7] && !nack_sent[dest_fifo_q]) nack_req[dest_fifo_q] = 1'b1;
                    end
                end
            end
            RECEIVE: begin
                if(clr_cur) begin
                    // SM cleared mid-record: write nothing, no NACK, skip the rest by its length
                    if(shift_valid && payload_len_q[dest_fifo_q] == '0) state_d = WAIT;
                    else                                                 state_d = DISCARD;
                end else if(shift_valid) begin
                    if(!fifo_full[dest_fifo_q]) begin
                        fifo_wren[dest_fifo_q] = 1'b1;     // accept this byte
                    end else if(!nack_sent[dest_fifo_q]) begin
                        nack_req[dest_fifo_q] = 1'b1;      // FIFO full: drop this byte and the rest, NACK
                    end
                    if(payload_len_q[dest_fifo_q] == '0) state_d = WAIT;           // last payload byte
                    else if(fifo_full[dest_fifo_q])     state_d = DISCARD;        // skip the rest
                end
            end
            DISCARD: begin
                if(shift_valid && payload_len_q[dest_fifo_q] == '0) state_d = WAIT;
            end
            default: state_d = WAIT;
        endcase
        // payload_len = L - 1 (remaining payload bytes after the first), so the last byte sees 0
        case(shift_databus[3:0])
            4'd8:    payload_len = 6'd11;   // L = 12
            4'd9:    payload_len = 6'd15;   // L = 16
            4'd10:   payload_len = 6'd23;   // L = 24
            4'd11:   payload_len = 6'd31;   // L = 32
            default: payload_len = {2'b00, shift_databus[3:0]};   // codes 0-7: L = code + 1
        endcase
    end

    genvar i;
    generate
        for(i = 0; i < N; i++) begin : FIFO_GEN
            pem_link_fifo #(.DEPTH(DEPTH), .WIDTH(8)) rx_fifo (
                .clk     (clk),
                .rst_n   (rst_n),
                .clr     (sm_clr[i]),
                // push
                .wr_en   (fifo_wren[i]),
                .wr_data (shift_databus),
                .full    (fifo_full[i]),
                // pop
                .rd_en   (fifo_rden[i]),
                .rd_data (rx_data[8*i +: 8]),
                .empty   (fifo_empty[i]),
                .level   (st_rx_level[4*i +: 4])
            );
        end
    endgenerate

    always_ff @(posedge clk or negedge rst_n) begin : SHIFT_REG 
        if(!rst_n) begin
            shift_reg <= 8'b0;
            shift_valid <= 1'b0;
        end
        else begin
            shift_valid <= miso_last;
            if(miso_smp) shift_reg <= {shift_reg[6:0], miso};
            if(miso_last) begin
                shift_databus <= {shift_reg[6:0], miso};
            end
        end
    end

`ifndef SYNTHESIS
    // Mode 1 must never see a payload byte for a full FIFO: that means the clk1_hold prediction failed
    always @(posedge clk)
        if(rst_n && mode_q && state_q == RECEIVE && shift_valid && !clr_cur && fifo_full[dest_fifo_q])
            $error("pem_link_rx: mode 1 payload byte for full RX FIFO[%0d], clk1_hold prediction failed", dest_fifo_q);
`endif
endmodule

`default_nettype wire
