`timescale 1ns/1ps
//MISO_SMP_PH is the sample phase, picks when we grab the MISO bit
//Sclk is a slower clocl that the protocol emulator generates form CLK, becuase we need MISO to run round trip in time


module pem_link_clk
(
    input wire clk, rst_n, //clk of PEM and async reset, comes from system
    input wire mode_q, run_req, // Determines the mode and if slck runs, comes from CSR side
    input wire clk1_hold,  // Comes from rx side
    input wire [1:0] sclk_sel_q, miso_dly_q,// These are just numbers we need for the calculations, which are written in at startup, comes from CSR side
    output wire clk_idle, // Goes to ctrl and and tx, signals both clocks are stopped
    output wire miso_smp, miso_last, // Goes to rx
    output wire mosi_load, mosi_shift, // Goes to tx
    output reg sclk_tx, sclk_rx //Communication timing with MCU, goes to MCU
);

    wire [4:0] div_m1  = (5'd4 << sclk_sel_q) - 5'd1;
    wire [4:0] half_m1 = (5'd2 << sclk_sel_q) - 5'd1;
    wire [1:0] k       = (miso_dly_q == 2'd0) ? 2'd1 : miso_dly_q;
    wire [4:0] smp_ph  = (half_m1 + {3'd0, k}) & div_m1;
    reg       running, clk1_run, stop_pend;
    reg [4:0] ph;
    reg [2:0] bit_cnt;
    wire start    = ~running & run_req;
    wire pre_rise = running & (ph == div_m1);
    wire pre_fall = running & (ph == half_m1);
    wire byte_end = pre_fall & (bit_cnt == 3'd0);
    wire stop_now = stop_pend | ~run_req;
    assign mosi_load  = start | (byte_end & ~stop_now);
    assign mosi_shift = pre_fall & (bit_cnt != 3'd0);
    assign miso_smp   = running & (ph == smp_ph) & (~mode_q | clk1_run);
    assign miso_last  = miso_smp & (bit_cnt == 3'd0);
    assign clk_idle   = ~running;
    wire       run_nx = start | (running & ~(byte_end & stop_now));
    wire [4:0] ph_nx  = start ? half_m1 + 5'd1 : (pre_rise ? 5'd0 : ph + {4'd0, running});
    wire       c1_nx  = (start | byte_end) ? ~(mode_q & clk1_hold) : clk1_run;
    wire       sck_nx = run_nx & (ph_nx <= half_m1);
    always @(posedge clk or negedge rst_n)
        if (!rst_n) begin
            {running, clk1_run, stop_pend, sclk_rx, sclk_tx} <= 5'b01000;
            ph <= 5'd0; bit_cnt <= 3'd7;
        end else begin
            running   <= run_nx;
            ph        <= ph_nx;
            clk1_run  <= c1_nx;
            bit_cnt   <= start ? 3'd7 : bit_cnt - {2'd0, pre_fall};
            stop_pend <= (start | (byte_end & stop_now)) ? 1'b0 : stop_pend | (running & ~run_req);
            sclk_tx   <= sck_nx;
            sclk_rx   <= sck_nx & (~mode_q | c1_nx);
        end
endmodule