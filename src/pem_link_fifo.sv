// pem_link_fifo -- per-SM byte FIFO for pem_link (RX and TX)
//
//   * First-word-fall-through: rd_data is the head entry whenever empty = 0;
//     no read latency. A pop is rd_en & ~empty.
//   * Holds exactly DEPTH entries (occupancy counter, no "one empty slot").
//   * Push is ignored when full, pop is ignored when empty. Push and pop may
//     happen in the same cycle. A push while full is dropped even if a pop
//     happens in the same cycle, so "full" means the same thing to the caller
//     as it does inside the FIFO.
//   * clr: synchronous clear (sm_clr), has priority over push and pop.
//   * rst_n: asynchronous reset, active low.
//   * Memory contents are not reset; they are unreachable until rewritten.

`default_nettype none

module pem_link_fifo #(
    parameter DEPTH = 8,                           // Entries, >= 2; need not be a power of two
    parameter WIDTH = 8                            // Bits per entry
) (
    input  wire                         clk,
    input  wire                         rst_n,     // Asynchronous reset, active low
    input  wire                         clr,       // Synchronous clear, active high

    // Push
    input  wire                         wr_en,
    input  wire [WIDTH-1:0]             wr_data,
    output wire                         full,

    // Pop
    input  wire                         rd_en,
    output wire [WIDTH-1:0]             rd_data,   // Head entry; valid while empty = 0
    output wire                         empty,

    output wire [$clog2(DEPTH+1)-1:0]   level      // Occupancy, 0..DEPTH
);

    localparam PTR_W = $clog2(DEPTH);
    localparam CNT_W = $clog2(DEPTH + 1);

    logic [WIDTH-1:0] mem [DEPTH];
    logic [PTR_W-1:0] wr_ptr, rd_ptr;
    logic [CNT_W-1:0] cnt;

    assign full    = (cnt == CNT_W'(DEPTH));
    assign empty   = (cnt == '0);
    assign level   = cnt;
    assign rd_data = mem[rd_ptr];

    wire do_wr = wr_en & ~full;
    wire do_rd = rd_en & ~empty;

    function automatic logic [PTR_W-1:0] ptr_inc(input logic [PTR_W-1:0] p);
        return (p == PTR_W'(DEPTH - 1)) ? '0 : p + 1'b1;
    endfunction

    always_ff @(posedge clk) begin
        if (do_wr && !clr) mem[wr_ptr] <= wr_data;
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr <= '0;
            rd_ptr <= '0;
            cnt    <= '0;
        end else if (clr) begin
            wr_ptr <= '0;
            rd_ptr <= '0;
            cnt    <= '0;
        end else begin
            if (do_wr) wr_ptr <= ptr_inc(wr_ptr);
            if (do_rd) rd_ptr <= ptr_inc(rd_ptr);
            case ({do_wr, do_rd})
                2'b10:   cnt <= cnt + 1'b1;
                2'b01:   cnt <= cnt - 1'b1;
                default: ;
            endcase
        end
    end

endmodule

`default_nettype wire
