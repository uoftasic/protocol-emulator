// pem_link -- SPI link controller between the PEm and the host MCU (top level)
// The PEm is the SPI master. RX = MCU -> PEm -> SM (on MISO); TX = SM -> PEm -> MCU (on MOSI).
// Per-SM bus packing: SM s occupies [8s+7:8s] of an 8N-bit bus, [s] of an N-bit bus,
// and [4s+3:4s] of a 4N-bit bus.
// This file only declares the I/O and wires the sub-blocks together.

`default_nettype none

module pem_link #(
    parameter N = 4                         // Number of SMs served (1-4; record header has a 2-bit SM index)
) (
    // ---------------- System ----------------
    input  wire           clk,              // System clock, same domain as the SMs (default 50 MHz)
    input  wire           rst_n,            // Asynchronous reset, active low

    // ---------------- MCU side (SPI pins) ----------------
    output wire           sclk_rx,          // CLK1: clock for MISO; may be paused (held low) in mode 1
    input  wire           miso,             // Serial data MCU -> PEm (RX direction)
    output wire           sclk_tx,          // CLK2: clock for MOSI; identical to sclk_rx in mode 0
    output wire           mosi,             // Serial data PEm -> MCU (TX direction)

    // ---------------- SM side: RX (link -> SM) ----------------
    output wire [8*N-1:0] rx_data,          // Head byte of RX FIFO[s]
    output wire [N-1:0]   rx_valid,         // RX FIFO[s] is non-empty
    input  wire [N-1:0]   rx_ready,         // SM s pulls one byte this cycle

    // ---------------- SM side: TX (SM -> link) ----------------
    input  wire [8*N-1:0] tx_data,          // Byte pushed by SM s into TX FIFO[s]
    input  wire [N-1:0]   tx_valid,         // SM s presents a byte this cycle
    output wire [N-1:0]   tx_ready,         // TX FIFO[s] is not full

    // ---------------- CSR configuration inputs ----------------
    input  wire           cfg_link_en,      // Link enable; rise latches mode/sclk_sel/miso_dly and starts, fall stops and flushes
    input  wire           cfg_mode,         // 0 = predictive, 1 = pull; latched on link_en rise
    input  wire [1:0]     cfg_sclk_sel,     // SCLK divider: 0=/4, 1=/8, 2=/16, 3=/32; latched on link_en rise
    input  wire [1:0]     cfg_miso_dly,     // MISO sample delay: sample k clk after each CLK1 falling edge, k = 1-3 (0 -> 1); latched on link_en rise
    input  wire [3:0]     cfg_tx_batch,     // TX batch threshold, 1-8 bytes (0 -> 1, >8 -> 8) (not latched)
    input  wire [7:0]     cfg_tx_age,       // Max wait in TX FIFO before sending below tx_batch, in MOSI byte times; 0 = send at once (not latched)
    input  wire [8*N-1:0] cfg_ack_every,    // Per-SM ACK byte-count threshold; 0 = no ACK at all for this SM (not latched)
    input  wire [8*N-1:0] cfg_ack_timeout,  // Per-SM ACK timeout in MOSI byte times since the last ACK sent; 0 means 256 (not latched)
    input  wire [N-1:0]   sm_rst,           // Per-SM link reset (CSR per-SM reset OR global soft reset)
    input  wire           clr_hdr_err,      // Single-cycle pulse: clear st_hdr_err
    input  wire [N-1:0]   clr_nack,         // Per-SM single-cycle pulse: clear st_nack_cnt[s]

    // ---------------- CSR status outputs ----------------
    output wire [7:0]     st_hdr_err,       // MISO format-error count, saturates at 255
    output wire [8*N-1:0] st_nack_cnt,      // Per-SM count of NACKs sent, saturating
    output wire [4*N-1:0] st_rx_level,      // RX FIFO[s] occupancy, 0-8 bytes
    output wire [4*N-1:0] st_tx_level       // TX FIFO[s] occupancy, 0-8 bytes
);

    // Elaboration-time range check on N: instantiate a non-existent module when out of range
    generate
        if (N < 1 || N > 4) begin : g_n_check
            pem_link_param_N_must_be_1_to_4 u_n_out_of_range ();
        end
    endgenerate

    // ---------------- Inter-block signals ----------------
    wire           mode_q;       // ctrl -> clk, rx: latched mode
    wire [1:0]     sclk_sel_q;   // ctrl -> clk: latched divider select
    wire [1:0]     miso_dly_q;   // ctrl -> clk: latched MISO sample delay (raw CSR value, 0 treated as 1 in clk)
    wire           run_req;      // ctrl -> clk: 1 = run the SCLKs; on fall, finish the byte in flight then stop
    wire           clk_idle;     // clk -> ctrl, tx: both SCLKs stopped low on a byte boundary
    wire [N-1:0]   sm_clr;       // ctrl -> rx, tx: sm_rst[s] OR global flush, clears both sides in one cycle

    wire           miso_smp;     // clk -> rx: sample MISO this cycle
    wire           miso_last;    // clk -> rx: this sample is the last bit (bit 0) of the byte
    wire           mosi_load;    // clk -> tx: MOSI byte boundary, load next byte; also the MOSI byte-time tick
    wire           mosi_shift;   // clk -> tx: SCLK falling edge, shift out the next MOSI bit
    wire           clk1_hold;    // rx -> clk: do not clock the next MISO byte; predicted one byte ahead (mode 1)

    wire [N-1:0]   rx_pop;       // rx -> tx: one-cycle pulse when SM s pulls a byte; TX increments C[s]
    wire [N-1:0]   nack_req;     // rx -> tx: one-cycle pulse requesting a NACK for SM s
    wire [8*N-1:0] exp_pos;      // rx -> tx: current E[s], read when TX loads the NACK's second byte

    // ---------------- Sub-block instances ----------------
    pem_link_ctrl #(.N(N)) u_ctrl (
        .clk          (clk),
        .rst_n        (rst_n),
        .cfg_link_en  (cfg_link_en),
        .cfg_mode     (cfg_mode),
        .cfg_sclk_sel (cfg_sclk_sel),
        .cfg_miso_dly (cfg_miso_dly),
        .sm_rst       (sm_rst),
        .clk_idle     (clk_idle),
        .mode_q       (mode_q),
        .sclk_sel_q   (sclk_sel_q),
        .miso_dly_q   (miso_dly_q),
        .run_req      (run_req),
        .sm_clr       (sm_clr)
    );

    pem_link_clk u_clk (
        .clk          (clk),
        .rst_n        (rst_n),
        .mode_q       (mode_q),
        .sclk_sel_q   (sclk_sel_q),
        .miso_dly_q   (miso_dly_q),
        .run_req      (run_req),
        .clk1_hold    (clk1_hold),
        .sclk_rx      (sclk_rx),
        .sclk_tx      (sclk_tx),
        .miso_smp     (miso_smp),
        .miso_last    (miso_last),
        .mosi_load    (mosi_load),
        .mosi_shift   (mosi_shift),
        .clk_idle     (clk_idle)
    );

    pem_link_rx #(.N(N)) u_rx (
        .clk          (clk),
        .rst_n        (rst_n),
        .miso         (miso),
        .miso_smp     (miso_smp),
        .miso_last    (miso_last),
        .mode_q       (mode_q),
        .sm_clr       (sm_clr),
        .rx_data      (rx_data),
        .rx_valid     (rx_valid),
        .rx_ready     (rx_ready),
        .clk1_hold    (clk1_hold),
        .rx_pop       (rx_pop),
        .nack_req     (nack_req),
        .exp_pos      (exp_pos),
        .clr_hdr_err  (clr_hdr_err),
        .st_hdr_err   (st_hdr_err),
        .st_rx_level  (st_rx_level)
    );

    pem_link_tx #(.N(N)) u_tx (
        .clk             (clk),
        .rst_n           (rst_n),
        .mosi            (mosi),
        .mosi_load       (mosi_load),
        .mosi_shift      (mosi_shift),
        .clk_idle        (clk_idle),
        .sm_clr          (sm_clr),
        .tx_data         (tx_data),
        .tx_valid        (tx_valid),
        .tx_ready        (tx_ready),
        .rx_pop          (rx_pop),
        .nack_req        (nack_req),
        .exp_pos         (exp_pos),
        .cfg_tx_batch    (cfg_tx_batch),
        .cfg_tx_age      (cfg_tx_age),
        .cfg_ack_every   (cfg_ack_every),
        .cfg_ack_timeout (cfg_ack_timeout),
        .clr_nack        (clr_nack),
        .st_nack_cnt     (st_nack_cnt),
        .st_tx_level     (st_tx_level)
    );

endmodule

`default_nettype wire
