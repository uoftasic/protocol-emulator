// -----------------------------------------------------------------------------
// sync_fifo.v
// Synchronous FIFO (single clock domain)
//
// Behaviour summary
//   * Push : on a rising clk edge with i_wren=1 and o_fifo_full=0,
//            i_data_in is written to mem[write_ptr] and write_ptr advances.
//   * Pull : on a rising clk edge with i_rden=1 and o_fifo_empty=0,
//            mem[read_ptr] is registered onto o_data_out (1-cycle latency)
//            and read_ptr advances.
//   * Push and pull in the same cycle are both allowed (subject to the
//     full / empty checks above).
//   * Full  : write_ptr == read_ptr - 1   (modulo FIFO_DEPTH, per spec).
//             NOTE: this "one empty slot" scheme means the FIFO holds at most
//             FIFO_DEPTH-1 entries.
//   * Empty : write_ptr == read_ptr
//
// Edge cases (per spec)
//   1. Pull when empty : o_data_out holds its value, read_ptr does not move.
//   2. Push when full  : data is dropped, write_ptr does not move.
//
// Resets
//   * ares : active-high asynchronous reset
//   * rst  : active-high synchronous reset
//   Both clear the pointers and o_data_out. Memory contents are not cleared
//   (they are unreachable until rewritten, so this saves area).
//
// FIFO_DEPTH may be any value >= 2 (power of two not required).
// -----------------------------------------------------------------------------

module sync_fifo #(
    parameter FIFO_DEPTH      = 16,
    parameter FIFO_DATA_WIDTH = 8
) (
    // General
    input  wire                               clk,    // \=[-tpr0y fzhtyuh]
    input  wire                               rst,    // active-high sync reset
    input  wire                               ares,   // active-high async reset

    // Push interface
    input  wire [FIFO_DATA_WIDTH-1:0]         i_data_in,
    input  wire                               i_wren,
    output wire                               o_fifo_full,
    output wire [$clog2(FIFO_DEPTH+1)-1:0]    o_fifo_count,

    // Pull interface
    input  wire                               i_rden,
    output reg  [FIFO_DATA_WIDTH-1:0]         o_data_out,
    output wire                               o_fifo_empty
);

    // -------------------------------------------------------------------------
    // Local parameters
    // -------------------------------------------------------------------------
    localparam PTR_W = (FIFO_DEPTH > 1) ? $clog2(FIFO_DEPTH) : 1;

    // -------------------------------------------------------------------------
    // Storage and pointers
    // -------------------------------------------------------------------------
    reg [FIFO_DATA_WIDTH-1:0] mem [0:FIFO_DEPTH-1];

    reg  [PTR_W-1:0] write_ptr;
    reg  [PTR_W-1:0] read_ptr;

    // Next-pointer values with wrap-around (works for non power-of-2 depths)
    wire [PTR_W-1:0] write_ptr_nxt = (write_ptr == FIFO_DEPTH-1) ? {PTR_W{1'b0}}
                                                                 : write_ptr + 1'b1;
    wire [PTR_W-1:0] read_ptr_nxt  = (read_ptr  == FIFO_DEPTH-1) ? {PTR_W{1'b0}}
                                                                 : read_ptr  + 1'b1;

    // -------------------------------------------------------------------------
    // Status flags
    // -------------------------------------------------------------------------
    // full when write_ptr == read_ptr - 1  <=>  write_ptr + 1 == read_ptr
    assign o_fifo_full  = (write_ptr_nxt == read_ptr);
    assign o_fifo_empty = (write_ptr     == read_ptr);

    // Number of stored entries: (write_ptr - read_ptr) mod FIFO_DEPTH
    assign o_fifo_count = (write_ptr >= read_ptr) ? (write_ptr - read_ptr)
                                                  : (FIFO_DEPTH - read_ptr + write_ptr);

    // Qualified enables: ignore push when full, pull when empty
    wire do_write = i_wren & ~o_fifo_full;
    wire do_read  = i_rden & ~o_fifo_empty;

    // -------------------------------------------------------------------------
    // Memory write (no reset needed on the array)
    // -------------------------------------------------------------------------
    always @(posedge clk) begin
        if (do_write)
            mem[write_ptr] <= i_data_in;
    end

    // -------------------------------------------------------------------------
    // Pointers and output register
    // -------------------------------------------------------------------------
    always @(posedge clk or posedge ares) begin
        if (ares) begin
            write_ptr  <= {PTR_W{1'b0}};
            read_ptr   <= {PTR_W{1'b0}};
            o_data_out <= {FIFO_DATA_WIDTH{1'b0}};
        end else if (rst) begin
            write_ptr  <= {PTR_W{1'b0}};
            read_ptr   <= {PTR_W{1'b0}};
            o_data_out <= {FIFO_DATA_WIDTH{1'b0}};
        end else begin
            if (do_write)
                write_ptr <= write_ptr_nxt;

            if (do_read) begin
                o_data_out <= mem[read_ptr];
                read_ptr   <= read_ptr_nxt;
            end
        end
    end

endmodule