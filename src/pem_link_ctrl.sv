// pem_link_ctrl -- link start/stop control
// Starts the link when link_en is seen high in IDLE: latch mode, sclk_sel and miso_dly, assert run_req.
// On link_en low: deassert run_req, wait for clk_idle (byte in flight finished, SCLKs stopped),
// then flush for one cycle. Merges sm_rst[s] and the global flush into sm_clr[s].
//
// link_en is level-sensitive: a link_en that goes high again while stopping is honoured only after
// the flush, and then restarts the link (config is latched at that point). A link_en already high
// at reset release starts the link.
//
// Contract with pem_link_clk: clk_idle must go low no later than 2 clk after run_req rises
// (registered output with at most one extra pipeline stage). The first STOP cycle ignores
// clk_idle so a stale high from a very short run cannot trigger an early flush.
//
// State owned: FSM state, mode_q, sclk_sel_q, miso_dly_q. run_req and flush are decoded
// directly from the state register (glitch-free within the clk domain, no input-to-output path).

`default_nettype none

module pem_link_ctrl #(
    parameter N = 4                     // Number of SMs
) (
    input  wire          clk,            // System clock
    input  wire          rst_n,          // Asynchronous reset, active low

    // ---- CSR inputs (clk domain) ----
    input  wire          cfg_link_en,    // Link enable
    input  wire          cfg_mode,       // Mode select, latched on start
    input  wire  [1:0]   cfg_sclk_sel,   // Divider select, latched on start
    input  wire  [1:0]   cfg_miso_dly,   // MISO sample delay, latched on start
    input  wire  [N-1:0] sm_rst,         // Per-SM link reset request

    // ---- From pem_link_clk ----
    input  wire          clk_idle,       // Both SCLKs stopped low on a byte boundary; safe to flush

    // ---- To other sub-blocks ----
    output logic         mode_q,         // Latched mode -> clk, rx
    output logic [1:0]   sclk_sel_q,     // Latched divider select -> clk
    output logic [1:0]   miso_dly_q,     // Latched MISO sample delay -> clk (passed through raw; clk maps 0 to 1)
    output logic         run_req,        // Run request -> clk; fall = finish byte in flight, then stop
    output logic [N-1:0] sm_clr,          // sm_rst[s] | global flush; clears rx and tx state in the same cycle
    output logic st_link_busy
);

    typedef enum logic [1:0] {
        IDLE  = 2'd0,   // Stopped; start when link_en is high
        RUN   = 2'd1,   // run_req = 1
        STOP  = 2'd2,   // run_req = 0, waiting for clk_idle
        FLUSH = 2'd3    // One-cycle global flush, then IDLE
    } state_t;

    state_t state_q, state_d;
    logic   stop_armed_q;   // Set after the first STOP cycle; clk_idle is only trusted once set
    logic   flush;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state_q      <= IDLE;
            stop_armed_q <= 1'b0;
            mode_q       <= 1'b0;
            sclk_sel_q   <= 2'd0;
            miso_dly_q   <= 2'd2;   // Matches the CSR default
        end else begin
            state_q      <= state_d;
            stop_armed_q <= (state_q == STOP);
            if (state_q == IDLE && cfg_link_en) begin
                mode_q     <= cfg_mode;
                sclk_sel_q <= cfg_sclk_sel;
                miso_dly_q <= cfg_miso_dly;
            end
        end
    end

    always_comb begin
        state_d = state_q;
        case (state_q)
            IDLE:    if (cfg_link_en)               state_d = RUN;
            RUN:     if (!cfg_link_en)              state_d = STOP;
            STOP:    if (stop_armed_q && clk_idle)  state_d = FLUSH;
            FLUSH:                                  state_d = IDLE;
            default:                                state_d = IDLE;
        endcase
    end

    assign run_req = (state_q == RUN);
    assign flush   = (state_q == FLUSH);
    assign sm_clr  = sm_rst | {N{flush}};
    assign st_link_busy = (state_q != IDLE);
endmodule

`default_nettype wire
