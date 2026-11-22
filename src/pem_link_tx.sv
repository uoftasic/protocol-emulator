// pem_link_tx -- TX FIFOs, ACK/NACK generation, MOSI scheduler and serializer (SM -> MCU direction)
// N TX FIFOs with the SM tx_* handshake and oldest-byte age (for tx_age); C[s] from rx_pop;
// ACK pending from ack_every / ack_timeout; NACK pending from nack_req.
// Scheduler: NACK > ACK > TX record (round-robin, tx_batch / tx_age eligibility,
// largest encodable length) > idle. Pads an in-flight record with 0x00 on sm_clr.
// MOSI serializer, NACK counters and TX FIFO levels.
// State owned: C[s], last-reported C, ACK timers, NACK pending, TX FIFOs, scheduler state, st_nack_cnt

`default_nettype none

module pem_link_tx #(
    parameter N = 4                         // Number of SMs
) (
    input  wire           clk,              // System clock
    input  wire           rst_n,            // Asynchronous reset, active low

    // ---- SPI pin ----
    output wire           mosi,             // Serial data PEm -> MCU; 0 while clk_idle = 1

    // ---- From pem_link_clk ----
    input  wire           mosi_load,        // MOSI byte boundary: load next byte / byte-time tick
    input  wire           mosi_shift,       // SCLK falling edge: shift out the next bit
    input  wire           clk_idle,         // SCLKs stopped; drive MOSI = 0

    // ---- From pem_link_ctrl ----
    input  wire [N-1:0]   sm_clr,           // Clear SM s: TX FIFO, C[s], last-reported, ACK/NACK pending;
                                            // in-flight TX record padded with 0x00, in-flight ACK/NACK second byte sent as 0x00

    // ---- SM-side TX handshake ----
    input  wire [8*N-1:0] tx_data,          // Byte pushed by SM s
    input  wire [N-1:0]   tx_valid,         // SM s presents a byte this cycle
    output wire [N-1:0]   tx_ready,         // TX FIFO[s] is not full

    // ---- From pem_link_rx ----
    input  wire [N-1:0]   rx_pop,           // SM s pulled a byte from RX FIFO[s]; increment C[s]
    input  wire [N-1:0]   nack_req,         // Set NACK pending for SM s
    input  wire [8*N-1:0] exp_pos,          // Current E[s], read when loading the NACK's second byte

    // ---- CSR configuration ----
    input  wire [3:0]     cfg_tx_batch,     // TX batch threshold, 1-8 bytes (0 -> 1, >8 -> 8)
    input  wire [7:0]     cfg_tx_age,       // Max wait in TX FIFO before sending below tx_batch, in MOSI byte times; 0 = send at once
    input  wire [8*N-1:0] cfg_ack_every,    // Per-SM ACK byte-count threshold; 0 = no ACK at all (count or timeout)
    input  wire [8*N-1:0] cfg_ack_timeout,  // Per-SM ACK timeout in MOSI byte times since the last ACK sent; 0 means 256

    // ---- CSR status ----
    input  wire [N-1:0]   clr_nack,         // Per-SM single-cycle pulse: clear st_nack_cnt[s]
    output wire [8*N-1:0] st_nack_cnt,      // Per-SM count of NACKs sent, saturating
    output wire [4*N-1:0] st_tx_level       // TX FIFO[s] occupancy, 0-8 bytes
);

    // TODO: implement. Placeholder drivers so no output is left floating.
    assign mosi        = 1'b0;
    assign tx_ready    = {N{1'b0}};
    assign st_nack_cnt = {(8*N){1'b0}};
    assign st_tx_level = {(4*N){1'b0}};

endmodule

`default_nettype wire
