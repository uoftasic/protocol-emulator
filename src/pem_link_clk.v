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

    wire [4:0] div_m1 = (5'd4<<sclk_sel_q)

endmodule